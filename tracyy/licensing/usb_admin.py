from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import platform
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from tracyy.licensing import build_secrets, volume_scan
from tracyy.licensing.usb_identity import (
    list_connected_usb_hardware_identities,
    normalize_serial,
    read_usb_hardware_identity,
)

APP_NAME = "Tracyy"
TOKEN_DIRECTORY = "TRACYY_ADMIN"
TOKEN_FILENAME = "tracyy_admin_usb.lic"
STATE_FILENAME = "usb_admin_state.dat"
DEFAULT_OFFLINE_SECONDS = 72 * 3600
DEFAULT_WARNING_SECONDS = 5 * 3600
TOKEN_CACHE_REVERIFY_SECONDS = 30.0

PUBLIC_KEY_BASE64 = "peyCSWfGa2bMmy2NeDhzxZuvQ5uSCKMOTKTfCx0H6VM="

TOKEN_V3 = 3
TOKEN_V3_CONTEXT = b"TRACYY_ADMIN_USB_V3"
TOKEN_V3_HKDF_INFO = b"TRACYY-ADMIN-USB-V3-AES-KEY"
TOKEN_V4 = 4
TOKEN_V4_CONTEXT = b"TRACYY_ADMIN_USB_V4"
TOKEN_V4_HKDF_INFO = b"TRACYY-ADMIN-USB-V4-AES-KEY"


@dataclass(frozen=True)
class USBRuntimeState:
    mode: str
    connected: bool = False
    active: bool = False
    deadline: float | None = None
    remaining_seconds: float = 0.0
    warning: bool = False
    block_on_exit: bool = False
    token_id: str = ""
    message: str = ""


def _state_root() -> Path:
    system = platform.system().lower()
    if system == "darwin":
        root = Path.home() / "Library" / "Application Support" / APP_NAME
    elif system == "windows":
        root = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
    else:
        root = (
            Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / APP_NAME
        )
    root.mkdir(parents=True, exist_ok=True)
    return root


def state_path() -> Path:
    return _state_root() / STATE_FILENAME


class _ScanBudget:
    """Deadline bookkeeping for one pass over the mounted volumes.

    Every call made against a mount point can block forever if the volume has
    stopped answering, so each one runs behind a per-volume deadline. The pass
    as a whole gets a smaller total on top of that, so the number of mounted
    volumes cannot turn a bounded startup delay into an unbounded one.

    ``incomplete`` records that at least one volume did not answer. That is
    "not scanned yet", not "no dongle here", and callers have to keep the two
    apart: a slow volume must never be reported as a missing licence.
    """

    def __init__(self, total: float | None = None) -> None:
        self.started = time.monotonic()
        self.total = volume_scan.SCAN_BUDGET_SECONDS if total is None else total
        self.incomplete = False

    def remaining(self) -> float:
        spent = time.monotonic() - self.started
        return max(0.05, min(volume_scan.PROBE_SECONDS, self.total - spent))

    def probe(self, key: str, work: Callable[[], Any], default: Any) -> Any:
        """Run ``work`` against a volume, or give up on it and return ``default``."""
        try:
            outcome = volume_scan.run_with_deadline(key, work, self.remaining())
        except Exception:
            return default
        if outcome is volume_scan.PENDING:
            self.incomplete = True
            return default
        return outcome


class AdminUSBManager:
    def __init__(self, machine_id: str) -> None:
        self.machine_id = str(machine_id)
        # Kept for UI compatibility. USB itself no longer creates a 48-hour grace.
        self.offline_seconds = DEFAULT_OFFLINE_SECONDS
        self.warning_seconds = DEFAULT_WARNING_SECONDS
        self._session_authorized = False
        self._last_token_id = ""
        self._cached_token_path: Path | None = None
        self._cached_token_root: Path | None = None
        self._cached_token_payload: dict[str, Any] | None = None
        self._cached_token_signature: tuple[Any, ...] | None = None
        self._cached_token_verified_at = 0.0
        self.last_detection_error = ""
        self.last_token_path = ""
        # True when a mounted volume did not answer inside the scan deadline.
        self.last_scan_incomplete = False
        self._last_runtime_state: USBRuntimeState | None = None

    @staticmethod
    def _public_key() -> Ed25519PublicKey:
        return Ed25519PublicKey.from_public_bytes(
            base64.b64decode(PUBLIC_KEY_BASE64, validate=True)
        )

    @staticmethod
    def _windows_removable_roots(budget: _ScanBudget) -> Iterable[Path]:
        # Some SanDisk controllers report DRIVE_FIXED instead of removable.
        # Security still requires a valid signature and matching USB serial.
        remote = volume_scan.windows_remote_drive_letters()
        for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
            # A mapped drive whose server is gone blocks in exists() the same
            # way a stale SMB mount does, and never holds a dongle anyway.
            if letter in remote:
                continue
            root = Path(f"{letter}:\\")
            if budget.probe(f"exists:{root}", root.exists, False):
                yield root

    @classmethod
    def _mounted_volume_roots(
        cls,
        base: Path,
        budget: _ScanBudget,
        depth: int = 1,
    ) -> list[Path]:
        """Mount points under ``base``, minus the ones no dongle can be on.

        ``base`` itself is on the boot disk, but its entries are the mount
        points, so both listing it and testing an entry go through ``budget``.

        Linux used to reach these through ``rglob("*")``, which descends into
        the contents of every mounted share. ``depth`` bounds that to the
        levels real removable media is actually mounted at.
        """
        entries = budget.probe(f"list:{base}", lambda: sorted(base.iterdir()), [])

        roots: list[Path] = []
        for entry in entries:
            # SMB/NFS/AFP is never an Admin USB dongle, and a stale one blocks
            # every call made against it. Decided from the mount table, so the
            # volume is never touched at all.
            if volume_scan.is_network_path(entry):
                continue
            if not budget.probe(f"dir:{entry}", entry.is_dir, False):
                continue
            roots.append(entry)
            if depth > 1:
                roots.extend(cls._mounted_volume_roots(entry, budget, depth - 1))
        return roots

    @classmethod
    def _usb_roots(cls, budget: _ScanBudget) -> list[Path]:
        roots: list[Path] = []
        test_root = str(os.environ.get("TRACYY_TEST_USB_ROOT", "")).strip()
        if test_root and not getattr(sys, "frozen", False):
            # Configured explicitly, so it is taken as given rather than
            # filtered by filesystem type.
            roots.append(Path(test_root))
        system = platform.system().lower()
        if system == "windows":
            roots.extend(cls._windows_removable_roots(budget))
        elif system == "darwin":
            roots.extend(cls._mounted_volume_roots(Path("/Volumes"), budget))
        else:
            for base in (Path("/media"), Path("/run/media"), Path("/mnt")):
                roots.extend(cls._mounted_volume_roots(base, budget, depth=2))
        unique: list[Path] = []
        seen: set[str] = set()
        for root in roots:
            # normpath, not resolve(): resolving a path is itself one of the
            # calls that hangs on a stale mount, and this only needs a key.
            key = os.path.normpath(str(root))
            if key not in seen:
                seen.add(key)
                unique.append(root)
        return unique

    @classmethod
    def usb_roots(cls) -> list[Path]:
        return cls._usb_roots(_ScanBudget())

    @classmethod
    def _scan_root(cls, root: Path) -> list[Path]:
        """Token files present on one volume.

        Every call in here blocks indefinitely on a volume that has stopped
        answering, so it only ever runs behind :meth:`_ScanBudget.probe`.
        """
        found: list[Path] = []

        for path in (root / TOKEN_DIRECTORY / TOKEN_FILENAME, root / TOKEN_FILENAME):
            try:
                if path.is_file():
                    found.append(path)
            except Exception:
                continue

        try:
            children = list(root.iterdir())
        except Exception:
            children = []

        for child in children:
            try:
                if child.is_file() and child.name.lower() == TOKEN_FILENAME.lower():
                    found.append(child)
                elif child.is_dir() and child.name.upper() == TOKEN_DIRECTORY:
                    for nested in child.iterdir():
                        if nested.is_file() and nested.name.lower() == TOKEN_FILENAME.lower():
                            found.append(nested)
            except Exception:
                continue

        return found

    @classmethod
    def _candidate_token_paths(cls, budget: _ScanBudget) -> list[Path]:
        paths: list[Path] = []
        seen: set[str] = set()

        def add(path: Path) -> None:
            # normpath rather than resolve(), which blocks on a stale mount.
            # Both were only ever a dedup key, and both case-fold the same.
            key = os.path.normpath(str(path)).lower()
            if key not in seen:
                seen.add(key)
                paths.append(path)

        for root in cls._usb_roots(budget):
            found = budget.probe(f"scan:{root}", lambda root=root: cls._scan_root(root), [])
            for path in found:
                add(path)

        return paths

    @classmethod
    def candidate_token_paths(cls) -> list[Path]:
        """Token files found on the mounted volumes.

        Volumes that do not answer within the scan deadline are left out, so
        an empty list means "none seen", not "none exist".
        """
        return cls._candidate_token_paths(_ScanBudget())

    @staticmethod
    def _token_file_signature(path: Path, root: Path) -> tuple[Any, ...]:
        token_stat = path.stat()
        root_stat = root.stat()
        return (
            str(path.resolve()),
            str(root.resolve()),
            int(getattr(token_stat, "st_dev", 0)),
            int(getattr(token_stat, "st_ino", 0)),
            int(token_stat.st_size),
            int(getattr(token_stat, "st_mtime_ns", 0)),
            int(getattr(root_stat, "st_dev", 0)),
            int(getattr(root_stat, "st_ino", 0)),
        )

    def _remember(self, state: USBRuntimeState) -> USBRuntimeState:
        """Keep the last state a completed scan established."""
        self._last_runtime_state = state
        return state

    def _clear_token_cache(self) -> None:
        self._cached_token_path = None
        self._cached_token_root = None
        self._cached_token_payload = None
        self._cached_token_signature = None
        self._cached_token_verified_at = 0.0

    def _cached_valid_token(
        self,
        now: float,
        budget: _ScanBudget,
    ) -> tuple[Path, dict[str, Any]] | None:
        path = self._cached_token_path
        root = self._cached_token_root
        payload = self._cached_token_payload
        signature = self._cached_token_signature
        if path is None or root is None or payload is None or signature is None:
            return None

        def unchanged() -> bool:
            if not path.is_file() or not root.exists():
                return False
            return self._token_file_signature(path, root) == signature

        try:
            fresh = volume_scan.run_with_deadline(
                f"cached:{path}",
                unchanged,
                budget.remaining(),
            )
        except Exception:
            self._clear_token_cache()
            return None

        if fresh is volume_scan.PENDING:
            # The volume stopped answering. That is not proof the dongle is
            # gone, so the cache is kept and the caller rescans instead.
            budget.incomplete = True
            return None
        if not fresh:
            self._clear_token_cache()
            return None
        if now - self._cached_token_verified_at >= TOKEN_CACHE_REVERIFY_SECONDS:
            return None
        return path, dict(payload)

    @staticmethod
    def _token_parameters(version: int) -> tuple[bytes, bytes]:
        if version == TOKEN_V3:
            return TOKEN_V3_CONTEXT, TOKEN_V3_HKDF_INFO
        if version == TOKEN_V4:
            return TOKEN_V4_CONTEXT, TOKEN_V4_HKDF_INFO
        raise ValueError("Unsupported Tracyy Admin USB token version.")

    @classmethod
    def _decrypt_record(cls, record: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        version = int(record.get("version", 0))
        context, hkdf_info = cls._token_parameters(version)
        try:
            ephemeral_public_raw = base64.b64decode(
                record["ephemeral_public_key"], validate=True
            )
            salt = base64.b64decode(record["salt"], validate=True)
            nonce = base64.b64decode(record["nonce"], validate=True)
            ciphertext = base64.b64decode(record["ciphertext"], validate=True)
            signature = base64.b64decode(record["signature"], validate=True)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Invalid encrypted Tracyy Admin USB token format.") from exc
        if len(ephemeral_public_raw) != 32 or len(salt) != 16 or len(nonce) != 12:
            raise ValueError("Invalid encrypted Tracyy Admin USB parameters.")
        signed_data = (
            context + bytes([version]) + ephemeral_public_raw + salt + nonce + ciphertext
        )
        try:
            cls._public_key().verify(signature, signed_data)
        except InvalidSignature as exc:
            raise ValueError("Invalid Tracyy Admin USB signature.") from exc
        # Supplied at build time, never carried in this file: the repository is
        # published, and a key in a published file is a key anybody can read.
        private_key = X25519PrivateKey.from_private_bytes(build_secrets.usb_token_private_key())
        shared_secret = private_key.exchange(
            X25519PublicKey.from_public_bytes(ephemeral_public_raw)
        )
        aes_key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            info=hkdf_info,
        ).derive(shared_secret)
        try:
            plaintext = AESGCM(aes_key).decrypt(nonce, ciphertext, context)
            payload = json.loads(plaintext.decode("utf-8"))
        except Exception as exc:
            raise ValueError("Cannot decrypt Tracyy Admin USB token.") from exc
        if not isinstance(payload, dict):
            raise ValueError("Invalid decrypted Tracyy Admin USB payload.")
        return version, payload

    @classmethod
    def verify_token_file(
        cls,
        path: Path,
        usb_root: Path | None = None,
    ) -> dict[str, Any]:
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(
            record,
            dict,
        ):
            raise ValueError("Invalid Tracyy Admin USB token format.")

        version, payload = cls._decrypt_record(record)

        if (
            str(
                payload.get(
                    "product",
                    "",
                )
            ).upper()
            != "TRACYY"
        ):
            raise ValueError("Token is not issued for Tracyy.")

        token_id = str(
            payload.get(
                "token_id",
                "",
            )
        ).strip()
        if not token_id:
            raise ValueError("Admin USB token ID is missing.")

        root = usb_root or (
            path.parent.parent if path.parent.name.upper() == TOKEN_DIRECTORY else path.parent
        )

        if version == TOKEN_V4:
            if (
                str(
                    payload.get(
                        "role",
                        "",
                    )
                ).upper()
                != "ADMIN_BETA_USB"
            ):
                raise ValueError("Token does not have Tracyy Beta USB permission.")

            if (
                str(
                    payload.get(
                        "status",
                        "",
                    )
                ).upper()
                != "BETA"
            ):
                raise ValueError("Admin USB token is not BETA.")

            canonical_length = int(
                payload.get(
                    "usb_serial_canonical_length",
                    0,
                )
                or 0
            )

            canonical_hashes_value = payload.get(
                "usb_serial_canonical_hashes",
                [],
            )
            canonical_hashes: list[str] = []

            if isinstance(
                canonical_hashes_value,
                list,
            ):
                for value in canonical_hashes_value:
                    current = str(value or "").strip().upper()
                    if current:
                        canonical_hashes.append(current)

            canonical_hashes = list(dict.fromkeys(canonical_hashes))

            # New cross-platform V4 format:
            # normalize the OS serial and compare SHA-256 of its first 20 chars.
            use_canonical_prefix = bool(canonical_hashes and canonical_length >= 8)

            # Backward compatibility for older V4 tokens that hashed the
            # complete OS-reported serial.
            legacy_full_hashes: list[str] = []

            hashes_value = payload.get(
                "usb_serial_hashes",
                [],
            )
            if isinstance(
                hashes_value,
                list,
            ):
                for value in hashes_value:
                    current = str(value or "").strip().upper()
                    if current:
                        legacy_full_hashes.append(current)

            legacy_single_hash = (
                str(
                    payload.get(
                        "usb_serial_hash",
                        "",
                    )
                    or ""
                )
                .strip()
                .upper()
            )
            if legacy_single_hash:
                legacy_full_hashes.append(legacy_single_hash)

            legacy_full_hashes = list(dict.fromkeys(legacy_full_hashes))

            if not use_canonical_prefix and not legacy_full_hashes:
                raise ValueError("Admin USB token is missing its serial identity hashes.")

            matched_serial = ""
            matched_canonical = ""
            detected_serials: list[str] = []

            def serial_matches(
                serial_value: str,
            ) -> tuple[bool, str]:
                serial = normalize_serial(serial_value)
                if not serial:
                    return False, ""

                detected_serials.append(serial)

                if use_canonical_prefix:
                    if len(serial) < canonical_length:
                        return False, ""

                    canonical = serial[:canonical_length]
                    canonical_hash = (
                        hashlib.sha256(canonical.encode("utf-8")).hexdigest().upper()
                    )

                    if any(
                        hmac.compare_digest(
                            expected,
                            canonical_hash,
                        )
                        for expected in canonical_hashes
                    ):
                        return True, canonical

                if legacy_full_hashes:
                    full_hash = hashlib.sha256(serial.encode("utf-8")).hexdigest().upper()

                    if any(
                        hmac.compare_digest(
                            expected,
                            full_hash,
                        )
                        for expected in legacy_full_hashes
                    ):
                        return True, serial

                return False, ""

            # Preferred exact-volume lookup.
            try:
                exact = read_usb_hardware_identity(root)
                is_match, canonical = serial_matches(exact.serial)
                if is_match:
                    matched_serial = normalize_serial(exact.serial)
                    matched_canonical = canonical
            except Exception:
                pass

            # macOS fallback: match the connected physical USB descriptors.
            if not matched_serial:
                for identity in list_connected_usb_hardware_identities():
                    is_match, canonical = serial_matches(identity.serial)
                    if is_match:
                        matched_serial = normalize_serial(identity.serial)
                        matched_canonical = canonical
                        break

            if not matched_serial:
                if use_canonical_prefix:
                    raise ValueError(
                        "Token V4 is valid, but this USB does not match "
                        "the registered Admin USB license. USB identity "
                        "details are hidden for security. Recreate the "
                        "token with the Canonical-20 Creator only when "
                        "using the original Admin USB."
                    )

                raise ValueError(
                    "This is an older V4 token using an incompatible "
                    "USB identity format. Recreate it with the "
                    "Canonical-20 Creator using the original Admin USB."
                )

            verified = dict(payload)
            verified["_verified_usb_serial"] = matched_serial
            verified["_verified_usb_canonical_serial"] = matched_canonical
            return verified

        if version == TOKEN_V3:
            raise ValueError(
                "OLD USB LICENSE V3 DETECTED. Recreate it with the "
                "Tracyy Canonical-20 V4 BETA Creator."
            )

        raise ValueError("Unsupported Tracyy Admin USB token version.")

    def detect_valid_token(
        self,
    ) -> tuple[Path, dict[str, Any]] | None:
        now = time.time()
        self.last_detection_error = ""
        self.last_token_path = ""
        self.last_scan_incomplete = False
        budget = _ScanBudget()

        cached = self._cached_valid_token(now, budget)
        if cached is not None:
            self.last_token_path = str(cached[0])
            return cached

        preferred = [self._cached_token_path] if self._cached_token_path is not None else []
        candidates = preferred + [
            path for path in self._candidate_token_paths(budget) if path not in preferred
        ]

        found_file = False
        errors: list[str] = []

        for path in candidates:
            if not budget.probe(f"file:{path}", path.is_file, False):
                continue

            found_file = True
            self.last_token_path = str(path)
            root = (
                path.parent.parent
                if path.parent.name.upper() == TOKEN_DIRECTORY
                else path.parent
            )

            try:
                payload = self.verify_token_file(path, root)
                signature = self._token_file_signature(path, root)
            except Exception as exc:
                errors.append(f"{path}: {exc}")
                continue

            self._cached_token_path = path
            self._cached_token_root = root
            self._cached_token_payload = dict(payload)
            self._cached_token_signature = signature
            self._cached_token_verified_at = now
            return path, dict(payload)

        self.last_scan_incomplete = budget.incomplete

        # A volume that never answered has not proved the dongle is absent,
        # so the previously verified token is kept for the next attempt.
        if not budget.incomplete:
            self._clear_token_cache()

        if found_file:
            self.last_detection_error = " | ".join(errors) or (
                "Admin USB token exists but verification failed."
            )
        elif budget.incomplete:
            self.last_detection_error = (
                "Một volume đang gắn chưa phản hồi nên chưa quét xong. "
                "Tracyy sẽ thử lại ở lần quét kế tiếp."
            )
        else:
            self.last_detection_error = (
                "Không tìm thấy TRACYY_ADMIN/tracyy_admin_usb.lic trên volume đang gắn."
            )

        return None

    def clear_state(self) -> None:
        try:
            state_path().unlink(missing_ok=True)
        except Exception:
            pass
        self._session_authorized = False
        self._last_token_id = ""
        self._clear_token_cache()

    def startup_state(
        self,
        now: float | None = None,
    ) -> USBRuntimeState:
        try:
            state_path().unlink(missing_ok=True)
        except Exception:
            pass

        token = self.detect_valid_token()

        if token is None:
            self._session_authorized = False
            return self._remember(
                USBRuntimeState(
                    mode="NONE",
                    message=(
                        self.last_detection_error or "No valid Tracyy Admin USB connected."
                    ),
                )
            )

        token_id = str(token[1].get("token_id", ""))
        self._session_authorized = True
        self._last_token_id = token_id

        return self._remember(
            USBRuntimeState(
                mode="USB_CONNECTED",
                connected=True,
                active=True,
                token_id=token_id,
                message=("Admin USB verified — Tracyy is running in BETA mode."),
            )
        )

    def poll(
        self,
        now: float | None = None,
    ) -> USBRuntimeState:
        token = self.detect_valid_token()

        if token is not None:
            token_id = str(token[1].get("token_id", ""))
            self._session_authorized = True
            self._last_token_id = token_id

            return self._remember(
                USBRuntimeState(
                    mode="USB_CONNECTED",
                    connected=True,
                    active=True,
                    token_id=token_id,
                    message=("Admin USB verified — Tracyy is running in BETA mode."),
                )
            )

        if self.last_scan_incomplete and self._last_runtime_state is not None:
            # A volume did not answer in time. "Not scanned yet" is not
            # "removed", so hold the last known state until one of the two is
            # actually established.
            return self._last_runtime_state

        if self._session_authorized:
            return self._remember(
                USBRuntimeState(
                    mode="USB_REMOVED_SESSION",
                    connected=False,
                    active=True,
                    token_id=self._last_token_id,
                    message=(
                        "Admin USB removed. BETA remains active only until "
                        "Tracyy closes. The next launch restores the last "
                        "Sheet license state."
                    ),
                )
            )

        return self._remember(
            USBRuntimeState(
                mode="NONE",
                message=(self.last_detection_error or "No valid Tracyy Admin USB connected."),
            )
        )

    def force_rescan(
        self,
    ) -> USBRuntimeState:
        """
        Discard the verified-token cache and scan mounted volumes and
        connected hardware serials again.

        This does not contact Google Sheet and does not modify the saved
        Sheet license state.
        """
        self._clear_token_cache()
        self.last_detection_error = ""
        self.last_token_path = ""
        # Collected probe results are dropped so a volume is looked at again
        # rather than answered from the last pass. Probes still in flight stay
        # tracked: starting a second one on a mount already known not to
        # answer would only park another thread.
        volume_scan.forget_completed()
        return self.poll()

    def prepare_shutdown(self, current: USBRuntimeState | None = None) -> None:
        # Never persist USB BETA authority. Next launch must use USB presence or Sheet cache.
        self.clear_state()

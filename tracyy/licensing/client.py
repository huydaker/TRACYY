from __future__ import annotations

import hashlib
import hmac
import json
import os
import platform
import secrets
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tracyy.core import paths
from tracyy.licensing.usb_admin import AdminUSBManager, USBRuntimeState

try:
    from tracyy.licensing.secure_loader import (
        load_license_config,
    )
except Exception:

    def load_license_config() -> dict[str, Any]:
        raise RuntimeError("Secure license loader is unavailable.")


try:
    import certifi
except ImportError:
    certifi = None


APP_NAME = "Tracyy"
APP_VERSION = "0.1-license-prototype"
OFFLINE_GRACE_HOURS = 72
CACHE_MAGIC = b"TRACYY-LIC-2\x00"
CACHE_VERSION = 2
CACHE_CLOCK_TOLERANCE_SECONDS = 300
_CACHE_APP_PEPPER = b"Tracyy-License-Cache-v2-2026"


def application_data_root() -> Path:
    """The Tracyy application-internal license-cache folder.

    V2 derives every root from :mod:`tracyy.core.paths`; V1 counted
    ``__file__`` parents, which silently relocated the cache whenever a module
    moved.
    """
    if paths.is_frozen():
        executable = Path(sys.executable).resolve()

        # macOS: Tracyy.app/Contents/Resources/Tracyy/license_cache.dat
        for parent in executable.parents:
            if parent.suffix.lower() == ".app":
                root = parent / "Contents" / "Resources" / "Tracyy"
                root.mkdir(parents=True, exist_ok=True)
                return root

        # Windows: beside Tracyy.exe.
        root = executable.parent
        root.mkdir(parents=True, exist_ok=True)
        return root

    # Source/development: inside the Tracyy source folder.
    root = paths.app_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


def data_root() -> Path:
    # Retained for compatibility with older callers.
    return application_data_root()


def config_path() -> Path:
    if paths.is_frozen():
        return application_data_root() / "license_config.json"

    # Editable config lives beside the application folder, not inside it, so a
    # reinstall does not overwrite it.
    return paths.app_root().parent / "license_config.json"


def cache_path() -> Path:
    return application_data_root() / "license_cache.dat"


def stable_machine_id() -> str:
    raw = "|".join(
        [
            platform.system(),
            platform.machine(),
            platform.node(),
            str(uuid.getnode()),
        ]
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return digest[:32].upper()


def machine_name() -> str:
    return socket.gethostname() or platform.node() or "Unknown Machine"


@dataclass
class LicenseResult:
    status: str
    message: str
    machine_id: str
    machine_name: str
    online: bool
    checked_at: float
    expires_at: float | None = None
    last_online_check: float | None = None
    offline_valid_until: float | None = None
    offline_grace_hours: float = 0.0
    source: str = "SERVER"
    usb_connected: bool = False
    usb_token_id: str = ""
    block_on_exit: bool = False

    @property
    def active(self) -> bool:
        return self.status.upper() in {"ACTIVE", "BETA"}


class LicenseClient:
    # Cache the most recent process result for normal callers, while
    # allowing explicit online refreshes when the application is running.
    _startup_result: LicenseResult | None = None

    @classmethod
    def clear_process_cache(cls) -> None:
        cls._startup_result = None

    def __init__(self) -> None:
        self.machine_id = stable_machine_id()
        self.machine_name = machine_name()
        self._last_error = ""
        self.last_server_response: dict[str, Any] = {}
        self.config = self._load_config()
        self.usb_admin = AdminUSBManager(self.machine_id)
        self.usb_runtime_state = USBRuntimeState(mode="NONE")

    def _config_candidates(self) -> list[Path]:
        candidates: list[Path] = []

        # 1. User-editable config beside the application folder:
        # <parent>/license_config.json
        application_root = paths.app_root()
        candidates.append(application_root.parent / "license_config.json")

        # 2. Inside the application folder, retained for older layouts.
        candidates.append(application_root / "license_config.json")

        # 3. PyInstaller executable location.
        if paths.is_frozen():
            executable = Path(sys.executable).resolve()

            # Windows onedir/onefile:
            # Tracyy/Tracyy.exe
            candidates.append(executable.parent / "license_config.json")
            candidates.append(executable.parent.parent / "license_config.json")

            # macOS bundle:
            # Tracyy.app/Contents/MacOS/Tracyy
            # External editable file:
            # parent of Tracyy.app/license_config.json
            bundle_path = executable
            for parent in executable.parents:
                if parent.suffix.lower() == ".app":
                    bundle_path = parent
                    break

            if bundle_path.suffix.lower() == ".app":
                candidates.append(bundle_path.parent / "license_config.json")
                candidates.append(
                    bundle_path / "Contents" / "Resources" / "license_config.json"
                )

        # 4. Current working directory and its parent.
        cwd = Path.cwd().resolve()
        candidates.append(cwd / "license_config.json")
        candidates.append(cwd.parent / "license_config.json")

        # Remove duplicates while preserving order.
        unique_candidates: list[Path] = []
        seen: set[str] = set()

        for candidate in candidates:
            key = str(candidate)
            if key not in seen:
                seen.add(key)
                unique_candidates.append(candidate)

        return unique_candidates

    def _config_path(self) -> Path:
        candidates = self._config_candidates()

        for candidate in candidates:
            if candidate.is_file():
                return candidate

        # Always point users to the preferred editable location.
        return candidates[0]

    def config_debug_info(self) -> dict:
        selected = self._config_path()
        return {
            "selected": str(selected),
            "exists": selected.is_file(),
            "candidates": [str(path) for path in self._config_candidates()],
            "frozen": bool(getattr(sys, "frozen", False)),
            "executable": str(Path(sys.executable).resolve()),
            "cwd": str(Path.cwd().resolve()),
        }

    def _load_config(self) -> dict[str, Any]:
        try:
            data = dict(load_license_config())
            endpoint = str(data.get("apps_script_url", "")).strip()
            enabled = bool(data.get("enabled", False))
            # Production allowance is fixed in code.
            # Encrypted config cannot override the fixed 72-hour policy.
            offline_hours = OFFLINE_GRACE_HOURS
        except Exception:
            return {
                "enabled": False,
                "apps_script_url": "",
                "offline_grace_hours": 0,
            }

        return {
            **data,
            "enabled": enabled,
            "apps_script_url": endpoint,
            "offline_grace_hours": offline_hours,
        }

    def _ssl_context(self) -> ssl.SSLContext:
        if certifi is not None:
            return ssl.create_default_context(cafile=certifi.where())

        return ssl.create_default_context()

    def _cache_keys(
        self,
        salt: bytes,
    ) -> tuple[bytes, bytes]:
        material = (
            self.machine_id.encode("utf-8")
            + b"|"
            + platform.system().encode("utf-8")
            + b"|"
            + _CACHE_APP_PEPPER
        )
        derived = hashlib.pbkdf2_hmac(
            "sha256",
            material,
            salt,
            120_000,
            dklen=64,
        )
        return derived[:32], derived[32:]

    @staticmethod
    def _xor_stream(
        data: bytes,
        key: bytes,
        nonce: bytes,
    ) -> bytes:
        output = bytearray(len(data))
        offset = 0
        counter = 0

        while offset < len(data):
            block = hmac.new(
                key,
                nonce
                + counter.to_bytes(
                    8,
                    "big",
                ),
                hashlib.sha256,
            ).digest()
            length = min(
                len(block),
                len(data) - offset,
            )
            for index in range(length):
                output[offset + index] = data[offset + index] ^ block[index]
            offset += length
            counter += 1

        return bytes(output)

    def _load_cache(self) -> dict[str, Any]:
        try:
            raw = cache_path().read_bytes()

            minimum_size = len(CACHE_MAGIC) + 1 + 16 + 16 + 32
            if len(raw) < minimum_size:
                return {}

            if not raw.startswith(CACHE_MAGIC):
                return {}

            version_offset = len(CACHE_MAGIC)
            version = raw[version_offset]
            if version != CACHE_VERSION:
                return {}

            salt_start = version_offset + 1
            salt = raw[salt_start : salt_start + 16]
            nonce = raw[salt_start + 16 : salt_start + 32]
            tag = raw[-32:]
            ciphertext = raw[salt_start + 32 : -32]

            encryption_key, mac_key = self._cache_keys(salt)
            authenticated = raw[:-32]
            expected_tag = hmac.new(
                mac_key,
                authenticated,
                hashlib.sha256,
            ).digest()

            if not hmac.compare_digest(
                tag,
                expected_tag,
            ):
                return {}

            plaintext = self._xor_stream(
                ciphertext,
                encryption_key,
                nonce,
            )
            payload = json.loads(plaintext.decode("utf-8"))
            if not isinstance(payload, dict):
                return {}

            if str(payload.get("machine_id", "")) != self.machine_id:
                return {}

            if (
                int(
                    payload.get(
                        "cache_version",
                        0,
                    )
                    or 0
                )
                != CACHE_VERSION
            ):
                return {}

            return payload
        except Exception:
            return {}

    def _save_cache(
        self,
        payload: dict[str, Any],
    ) -> None:
        cache_payload = dict(payload)
        cache_payload["machine_id"] = self.machine_id
        cache_payload["cache_version"] = CACHE_VERSION

        plaintext = json.dumps(
            cache_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

        salt = secrets.token_bytes(16)
        nonce = secrets.token_bytes(16)
        encryption_key, mac_key = self._cache_keys(salt)
        ciphertext = self._xor_stream(
            plaintext,
            encryption_key,
            nonce,
        )

        authenticated = CACHE_MAGIC + bytes([CACHE_VERSION]) + salt + nonce + ciphertext
        tag = hmac.new(
            mac_key,
            authenticated,
            hashlib.sha256,
        ).digest()

        destination = cache_path()
        destination.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        temporary = destination.with_suffix(".dat.tmp")
        temporary.write_bytes(authenticated + tag)
        temporary.replace(destination)

    def _request_payload(self) -> dict[str, Any]:
        return {
            "action": "check_license",
            "app_name": APP_NAME,
            "app_version": APP_VERSION,
            "machine_id": self.machine_id,
            "machine_name": self.machine_name,
            "os": platform.platform(),
            "architecture": platform.machine(),
            "username": os.environ.get(
                "USER",
                os.environ.get("USERNAME", ""),
            ),
            "timestamp": int(time.time()),
        }

    def _remember_startup_result(
        self,
        result: LicenseResult,
    ) -> LicenseResult:
        type(self)._startup_result = result
        return result

    def _license_result_from_usb_state(
        self,
        state: USBRuntimeState,
        now: float,
        sheet_result: LicenseResult | None = None,
    ) -> LicenseResult:
        base = sheet_result
        return LicenseResult(
            status="BETA",
            message=state.message,
            machine_id=self.machine_id,
            machine_name=self.machine_name,
            online=bool(base.online) if base is not None else False,
            checked_at=now,
            expires_at=None,
            last_online_check=(base.last_online_check if base is not None else None),
            offline_valid_until=(base.offline_valid_until if base is not None else None),
            offline_grace_hours=float(OFFLINE_GRACE_HOURS),
            source=("ADMIN_USB_CONNECTED" if state.connected else "ADMIN_USB_SESSION_REMOVED"),
            usb_connected=state.connected,
            usb_token_id=state.token_id,
            block_on_exit=False,
        )

    def server_reachable(
        self,
        *,
        timeout: float = 2.0,
    ) -> bool:
        # Connectivity probe only. No license request or cache update.
        endpoint = str(
            self.config.get(
                "apps_script_url",
                "",
            )
        ).strip()
        if not endpoint:
            return False

        try:
            parsed = urllib.parse.urlparse(endpoint)
            host = str(parsed.hostname or "").strip()
            if not host:
                return False

            if parsed.port is not None:
                port = int(parsed.port)
            elif parsed.scheme.lower() == "http":
                port = 80
            else:
                port = 443

            with socket.create_connection(
                (host, port),
                timeout=max(
                    0.25,
                    float(timeout),
                ),
            ):
                return True
        except (
            OSError,
            ValueError,
        ):
            return False

    def scan_google_sheet(
        self,
    ) -> LicenseResult:
        """
        Query Google Sheet immediately and return its real license state.

        This does not apply the Admin USB override. The Sheet result is
        always saved first so the next launch without USB restores the
        latest authoritative Sheet status.
        """
        now = time.time()
        enabled = bool(
            self.config.get(
                "enabled",
                False,
            )
        )
        endpoint = str(
            self.config.get(
                "apps_script_url",
                "",
            )
            or ""
        ).strip()

        if not enabled or not endpoint:
            raise RuntimeError("Google Sheet license server is not configured.")

        try:
            request_data = json.dumps(self._request_payload()).encode("utf-8")
            request = urllib.request.Request(
                endpoint,
                data=request_data,
                headers={
                    "Content-Type": "application/json",
                },
                method="POST",
            )

            with urllib.request.urlopen(
                request,
                timeout=8.0,
                context=self._ssl_context(),
            ) as response:
                response_payload = json.loads(response.read().decode("utf-8"))

        except (
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
            OSError,
        ) as error:
            self._last_error = str(error)
            raise RuntimeError(
                "Không kết nối hoặc xác minh được Google Sheet: "
                + (self._last_error or type(error).__name__)
            ) from error

        if not isinstance(
            response_payload,
            dict,
        ):
            raise RuntimeError("Google Sheet trả về dữ liệu không hợp lệ.")

        self.last_server_response = dict(response_payload)

        status = (
            str(
                response_payload.get(
                    "status",
                    "BLOCKED",
                )
            )
            .strip()
            .upper()
        )

        valid_statuses = {
            "BETA",
            "ACTIVE",
            "EXPIRED",
            "BLOCKED",
            "REVOKED",
        }
        if status not in valid_statuses:
            status = "BLOCKED"

        message = str(
            response_payload.get(
                "message",
                "",
            )
            or ""
        )
        deadline = now + float(OFFLINE_GRACE_HOURS) * 3600.0

        cache_payload = {
            "status": status,
            "message": message,
            "machine_id": self.machine_id,
            "machine_name": self.machine_name,
            "last_online_check": now,
            "last_seen_time": now,
            "offline_valid_until": deadline,
            "server_payload": response_payload,
        }
        self._save_cache(cache_payload)
        self._last_error = ""

        return LicenseResult(
            status=status,
            message=message,
            machine_id=self.machine_id,
            machine_name=self.machine_name,
            online=True,
            checked_at=now,
            expires_at=deadline,
            last_online_check=now,
            offline_valid_until=deadline,
            offline_grace_hours=float(OFFLINE_GRACE_HOURS),
            source="SERVER",
        )

    def check(
        self,
        *,
        force_refresh: bool = False,
    ) -> LicenseResult:
        process_result = type(self)._startup_result
        if process_result is not None and not force_refresh:
            return process_result

        now = time.time()
        self.usb_runtime_state = self.usb_admin.startup_state(now)
        usb_available = bool(self.usb_runtime_state.active)

        enabled = bool(self.config.get("enabled", False))
        endpoint = str(self.config.get("apps_script_url", "") or "").strip()
        server_result: LicenseResult | None = None

        if enabled and endpoint:
            try:
                request_data = json.dumps(self._request_payload()).encode("utf-8")
                request = urllib.request.Request(
                    endpoint,
                    data=request_data,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(
                    request,
                    timeout=8.0,
                    context=self._ssl_context(),
                ) as response:
                    response_payload = json.loads(response.read().decode("utf-8"))

                self.last_server_response = dict(
                    response_payload if isinstance(response_payload, dict) else {}
                )
                status = str(response_payload.get("status", "EXPIRED")).strip().upper()
                if status not in {"BETA", "ACTIVE", "EXPIRED", "BLOCKED", "REVOKED"}:
                    status = "BLOCKED"
                message = str(response_payload.get("message", "") or "")
                deadline = now + float(OFFLINE_GRACE_HOURS) * 3600.0

                # Always save the actual Sheet state before applying a USB override.
                cache_payload = {
                    "status": status,
                    "message": message,
                    "machine_id": self.machine_id,
                    "machine_name": self.machine_name,
                    "last_online_check": now,
                    "last_seen_time": now,
                    "offline_valid_until": deadline,
                    "server_payload": response_payload,
                }
                self._save_cache(cache_payload)
                server_result = LicenseResult(
                    status=status,
                    message=message,
                    machine_id=self.machine_id,
                    machine_name=self.machine_name,
                    online=True,
                    checked_at=now,
                    expires_at=deadline,
                    last_online_check=now,
                    offline_valid_until=deadline,
                    offline_grace_hours=float(OFFLINE_GRACE_HOURS),
                    source="SERVER",
                )
            except (
                urllib.error.URLError,
                TimeoutError,
                json.JSONDecodeError,
                OSError,
            ) as error:
                self._last_error = str(error)

        # USB is a deliberate BETA override, including when Sheet says BLOCKED.
        if usb_available:
            return self._remember_startup_result(
                self._license_result_from_usb_state(
                    self.usb_runtime_state,
                    now,
                    server_result,
                )
            )

        # No USB: a reachable Sheet is authoritative.
        if server_result is not None:
            return self._remember_startup_result(server_result)

        # Offline/no USB: restore exactly the last Sheet state from encrypted cache.
        cache = self._load_cache()
        cached_status = str(cache.get("status", "") or "").strip().upper()
        valid_statuses = {"BETA", "ACTIVE", "EXPIRED", "BLOCKED", "REVOKED"}
        cache_matches_machine = str(cache.get("machine_id", "")) == self.machine_id

        if cached_status in valid_statuses and cache_matches_machine:
            grace_hours = float(OFFLINE_GRACE_HOURS)
            last_online_check = float(cache.get("last_online_check", 0.0) or 0.0)
            offline_valid_until = float(cache.get("offline_valid_until", 0.0) or 0.0)
            last_seen_time = float(cache.get("last_seen_time", 0.0) or 0.0)

            if last_online_check > 0.0:
                # Recalculate from the authoritative online timestamp.
                # This migrates old 48-hour cache records to the
                # current 72-hour offline policy.
                offline_valid_until = last_online_check + grace_hours * 3600.0

            clock_rolled_back = (
                last_seen_time > 0.0 and now + CACHE_CLOCK_TOLERANCE_SECONDS < last_seen_time
            )
            timed_status = cached_status in {"ACTIVE", "BETA", "EXPIRED"}
            grace_expired = timed_status and (
                last_online_check <= 0.0
                or offline_valid_until <= 0.0
                or now >= offline_valid_until
            )

            if clock_rolled_back or grace_expired:
                reason = (
                    "System clock moved backwards. Connect online and restart Tracyy."
                    if clock_rolled_back
                    else f"Offline license exceeded {grace_hours:g} hours."
                )
                return self._remember_startup_result(
                    LicenseResult(
                        status="BLOCKED",
                        message=reason,
                        machine_id=self.machine_id,
                        machine_name=self.machine_name,
                        online=False,
                        checked_at=now,
                        expires_at=offline_valid_until or None,
                        last_online_check=last_online_check or None,
                        offline_valid_until=offline_valid_until or None,
                        offline_grace_hours=grace_hours,
                        source="CACHE_TIMEOUT" if grace_expired else "CACHE_CLOCK_ERROR",
                    )
                )

            cache["last_seen_time"] = max(last_seen_time, now)
            if offline_valid_until > 0.0:
                cache["offline_valid_until"] = offline_valid_until
            try:
                self._save_cache(cache)
            except Exception:
                pass

            return self._remember_startup_result(
                LicenseResult(
                    status=cached_status,
                    message=str(cache.get("message", "") or "")
                    or "Đang sử dụng trạng thái license đã lưu từ Sheet.",
                    machine_id=self.machine_id,
                    machine_name=self.machine_name,
                    online=False,
                    checked_at=now,
                    expires_at=offline_valid_until or None,
                    last_online_check=last_online_check or None,
                    offline_valid_until=offline_valid_until or None,
                    offline_grace_hours=grace_hours,
                    source="CACHE",
                )
            )

        # First install, no Sheet connection, no valid USB.
        reason = (
            "Lần đầu cài đặt chưa xác minh được với Sheet và không có Admin USB hợp lệ. "
            "License mặc định: BLOCKED."
        )
        if not enabled or not endpoint:
            reason += " Cấu hình license server không hợp lệ."
        elif self._last_error:
            reason += f" Chi tiết: {self._last_error}"
        return self._remember_startup_result(
            LicenseResult(
                status="BLOCKED",
                message=reason,
                machine_id=self.machine_id,
                machine_name=self.machine_name,
                online=False,
                checked_at=now,
                source="FIRST_INSTALL_OFFLINE",
            )
        )

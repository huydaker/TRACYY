"""Build-time key material, deliberately kept out of the source tree.

Two keys unlock the licence layer: the AES key for ``license_config.enc``, and
the X25519 private key that decrypts an admin USB token. The build needs both;
the repository must carry neither. A key committed to a published repository is
a key anybody can read, and splitting one across several byte arrays in the
same file obfuscates it rather than protecting it — the function that
reassembles it ships alongside.

Both values are read from the environment, or from ``license_secrets.json``
sitting wherever ``license_config.enc`` sits. That file is gitignored and
bundled by the PyInstaller spec exactly as the encrypted config already is, so
a build keeps working and a clone does not carry the keys.

Nothing here raises at import time. A missing key surfaces only when the
licence layer actually asks for it, where both callers already treat failure as
"no licence configured" and fall back to the cached state rather than crashing.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from pathlib import Path
from typing import Any

from tracyy.core import paths

__all__ = ["SECRETS_FILENAME", "MissingBuildSecret", "build_key", "usb_token_private_key"]

SECRETS_FILENAME = "license_secrets.json"

#: Environment overrides, so CI can supply a key without writing a file.
BUILD_KEY_ENV = "TRACYY_LICENSE_BUILD_KEY"
USB_TOKEN_KEY_ENV = "TRACYY_USB_TOKEN_KEY"

_KEY_LENGTH = 32


class MissingBuildSecret(RuntimeError):
    """Raised when a key is needed and neither the env nor the file has it."""


def _candidates() -> list[Path]:
    """Every place the secrets file may live, most specific first.

    Deliberately the same search order ``secure_loader`` uses for
    ``license_config.enc``: the two files are produced together and a build
    that finds one should find the other.
    """
    found: list[Path] = []

    bundle = paths.bundle_dir()
    if bundle is not None:
        found.append(bundle / SECRETS_FILENAME)

    found.append(paths.resource_root() / SECRETS_FILENAME)
    found.append(paths.app_root() / SECRETS_FILENAME)
    found.append(Path(__file__).resolve().parent / SECRETS_FILENAME)

    unique: list[Path] = []
    seen: set[Path] = set()
    for candidate in found:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)
    return unique


def _secrets_file() -> dict[str, Any]:
    for path in _candidates():
        try:
            if not path.is_file():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return {}


def _decoded(value: str, field: str, decode) -> bytes:
    try:
        raw = decode(value.strip())
    except (ValueError, binascii.Error) as exc:
        raise MissingBuildSecret(f"{field} không giải mã được.") from exc
    if len(raw) != _KEY_LENGTH:
        raise MissingBuildSecret(f"{field} dài {len(raw)} byte, phải là {_KEY_LENGTH}.")
    return raw


def _resolve(env_name: str, field: str, decode, hint: str) -> bytes:
    value = str(os.environ.get(env_name, "")).strip()
    if value:
        return _decoded(value, env_name, decode)

    value = str(_secrets_file().get(field, "") or "").strip()
    if value:
        return _decoded(value, field, decode)

    raise MissingBuildSecret(
        f"Thiếu khoá build-time: đặt biến môi trường {env_name} ({hint}), "
        f"hoặc để trường {field!r} trong {SECRETS_FILENAME}."
    )


def build_key() -> bytes:
    """AES-256 key for ``license_config.enc``, as 32 bytes."""
    return _resolve(
        BUILD_KEY_ENV,
        "license_build_key_hex",
        bytes.fromhex,
        "64 ký tự hex",
    )


def usb_token_private_key() -> bytes:
    """X25519 private key for admin USB tokens, as 32 bytes."""
    return _resolve(
        USB_TOKEN_KEY_ENV,
        "usb_token_private_key_b64",
        lambda value: base64.b64decode(value, validate=True),
        "base64 của 32 byte",
    )

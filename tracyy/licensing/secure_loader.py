from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from tracyy.core import paths
from tracyy.licensing import build_secrets

_MAGIC = b"TRLCFG3\x00"
_AAD = b"TRACYY-LICENSE-CONFIG-V3"


def _resource_candidates() -> list[Path]:
    """Every place the encrypted config may live, most specific first.

    V2 asks :mod:`tracyy.core.paths` instead of counting ``__file__`` parents,
    so moving this module inside the package cannot move the config with it.
    """
    candidates: list[Path] = []

    bundle = paths.bundle_dir()
    if bundle is not None:
        candidates.append(bundle / "license_config.enc")

    candidates.append(paths.resource_root() / "license_config.enc")
    candidates.append(paths.app_root() / "license_config.enc")
    # Retained for older layouts that kept the file beside the source module.
    candidates.append(Path(__file__).resolve().parent / "license_config.enc")

    seen: set[Path] = set()
    unique: list[Path] = []
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(resolved)
    return unique


def _reconstruct_key() -> bytes:
    """The AES key, supplied at build time rather than carried in this file.

    It used to be four byte arrays here, XORed back together a few lines down.
    That is obfuscation, not protection: anyone holding the file holds both the
    fragments and the function that reassembles them.
    """
    return build_secrets.build_key()


def load_license_config() -> dict[str, Any]:
    source = next(
        (p for p in _resource_candidates() if p.is_file()),
        None,
    )
    if source is None:
        raise RuntimeError("Encrypted license configuration is missing.")

    raw = source.read_bytes()
    if not raw.startswith(_MAGIC):
        raise RuntimeError("Encrypted license configuration is invalid.")

    nonce = raw[len(_MAGIC) : len(_MAGIC) + 12]
    ciphertext = raw[len(_MAGIC) + 12 :]
    if len(nonce) != 12 or not ciphertext:
        raise RuntimeError("Encrypted license configuration is truncated.")

    plaintext = AESGCM(_reconstruct_key()).decrypt(
        nonce,
        ciphertext,
        _AAD,
    )
    data = json.loads(plaintext.decode("utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("License configuration payload is invalid.")

    enabled = bool(data.get("enabled", False))
    endpoint = str(data.get("apps_script_url", "")).strip()
    if not enabled or not endpoint:
        raise RuntimeError("License configuration is incomplete.")

    return data

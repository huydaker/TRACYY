#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json

from tracyy.licensing.client import LicenseClient
from tracyy.licensing.usb_admin import AdminUSBManager
from tracyy.licensing.usb_identity import list_connected_usb_hardware_identities

CANONICAL_SERIAL_LENGTH = 20


def main() -> int:
    client = LicenseClient()
    manager = AdminUSBManager(client.machine_id)

    print("=== CONNECTED USB SERIAL CANDIDATES ===")
    identities = list_connected_usb_hardware_identities()
    if not identities:
        print("Không tìm thấy USB có hardware serial.")
    else:
        for index, identity in enumerate(identities, start=1):
            normalized = identity.normalized()
            serial = normalized.get(
                "serial",
                "",
            )
            canonical = serial[:CANONICAL_SERIAL_LENGTH]
            canonical_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest().upper()
            print(f"{index}. full_serial={serial}")
            print("   canonical_20=" + canonical)
            print("   canonical_sha256=" + canonical_hash)
            print(
                "   device="
                + json.dumps(
                    normalized,
                    ensure_ascii=False,
                )
            )

    print("\n=== TOKEN FILES ===")
    existing = [path for path in manager.candidate_token_paths() if path.is_file()]
    if not existing:
        print("Không tìm thấy TRACYY_ADMIN/tracyy_admin_usb.lic")
    else:
        for path in existing:
            print(path)

    print("\n=== VERIFICATION ===")
    token = manager.detect_valid_token()
    if token is None:
        print("FAILED")
        print(manager.last_detection_error)
        return 1

    print("PASS — LICENSE BETA")
    print("Token:", token[0])
    print("Token ID:", token[1].get("token_id", ""))
    print(
        "Verified serial:",
        token[1].get("_verified_usb_serial", ""),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

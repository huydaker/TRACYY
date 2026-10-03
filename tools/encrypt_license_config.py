"""Mã hoá license_config.json thành license_config.enc và xoay khoá build.

Khoá mới được ghi vào ``license_secrets.json`` ở thư mục gốc dự án — file nằm
trong .gitignore. Trước đây script này ghi khoá ngược vào một file .py dưới dạng
bốn mảng byte để XOR lại; cách đó chỉ làm rối chứ không bảo vệ, vì hàm ghép khoá
nằm ngay cạnh các mảnh. Xem tracyy/licensing/build_secrets.py.

Chạy:  python tools/encrypt_license_config.py
Đặt license_config.json cạnh script này trước khi chạy.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"TRLCFG3\x00"
AAD = b"TRACYY-LICENSE-CONFIG-V3"

SECRETS_FILENAME = "license_secrets.json"


def main() -> int:
    here = Path(__file__).resolve().parent
    project_root = here.parent
    source = here / "license_config.json"
    if not source.is_file():
        print("[ERROR] Đặt license_config.json cạnh script rồi chạy lại.")
        return 1

    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[ERROR] JSON không hợp lệ: {exc}")
        return 1

    endpoint = str(data.get("apps_script_url", "")).strip()
    if not bool(data.get("enabled", False)) or not endpoint:
        print("[ERROR] enabled phải là true và apps_script_url không được rỗng.")
        return 1

    plaintext = json.dumps(
        data,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")

    key = secrets.token_bytes(32)
    nonce = secrets.token_bytes(12)
    encrypted = AESGCM(key).encrypt(nonce, plaintext, AAD)

    # Khoá USB token không đổi ở đây — chỉ khoá config được xoay, nên đọc lại
    # file cũ để giữ nguyên các trường khác.
    secrets_path = project_root / SECRETS_FILENAME
    stored: dict[str, object] = {}
    if secrets_path.is_file():
        try:
            loaded = json.loads(secrets_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                stored = loaded
        except ValueError:
            print(f"[WARN] {SECRETS_FILENAME} hỏng, sẽ ghi đè.")

    stored["_comment"] = (
        "Khoá build-time của Tracyy. KHÔNG commit file này. "
        "Xem tracyy/licensing/build_secrets.py."
    )
    stored["license_build_key_hex"] = key.hex()

    (project_root / "license_config.enc").write_bytes(MAGIC + nonce + encrypted)
    secrets_path.write_text(
        json.dumps(stored, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    secrets_path.chmod(0o600)

    print(f"[OK] Đã mã hoá thành {project_root / 'license_config.enc'}")
    print(f"[OK] Khoá build mới đã ghi vào {secrets_path}")
    if "usb_token_private_key_b64" not in stored:
        print(f"[WARN] {SECRETS_FILENAME} chưa có usb_token_private_key_b64 —")
        print("       admin USB token sẽ không đọc được cho tới khi thêm vào.")
    print("[SECURITY] Xoá license_config.json và sao lưu license_secrets.json.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

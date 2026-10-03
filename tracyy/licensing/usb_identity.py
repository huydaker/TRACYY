from __future__ import annotations

import hashlib
import json
import os
import platform
import plistlib
import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tracyy.licensing import volume_scan


@dataclass(frozen=True)
class USBHardwareIdentity:
    serial: str
    vendor_id: str = ""
    product_id: str = ""
    manufacturer: str = ""
    product_name: str = ""

    def normalized(self) -> dict[str, str]:
        return {
            "serial": normalize_serial(self.serial),
            "vendor_id": normalize_hex(self.vendor_id),
            "product_id": normalize_hex(self.product_id),
            "manufacturer": normalize_text(self.manufacturer),
            "product_name": normalize_text(self.product_name),
        }

    def serial_hash(self) -> str:
        serial = normalize_serial(self.serial)
        if not serial:
            raise RuntimeError("USB không có hardware serial hợp lệ.")
        return hashlib.sha256(serial.encode("utf-8")).hexdigest().upper()

    def fingerprint(self) -> str:
        # Compatibility alias. V4 binds only to the cross-platform serial.
        return self.serial_hash()


def normalize_text(value: Any) -> str:
    return " ".join(str(value or "").strip().upper().split())


def normalize_serial(value: Any) -> str:
    # Windows/macOS may differ only in casing or separators.
    return re.sub(r"[^A-Z0-9]", "", str(value or "").strip().upper())


def normalize_hex(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return f"{value & 0xFFFF:04X}"
    text = normalize_text(value)
    match = re.search(r"0X([0-9A-F]{1,4})", text)
    if match:
        return match.group(1).zfill(4)
    match = re.search(r"\\b([0-9A-F]{4})\\b", text)
    if match:
        return match.group(1)
    if text.isdigit():
        return f"{int(text) & 0xFFFF:04X}"
    return ""


def _mount_point(root: Path) -> str:
    """The volume's path, resolved when the volume is willing to answer.

    ``resolve()`` blocks indefinitely on a mount whose server has gone away.
    Both callers only need something to match against a device table, and the
    unresolved mount point matches there too, so a volume that does not answer
    costs the symlink resolution rather than the whole call.
    """
    try:
        resolved = volume_scan.run_with_deadline(
            f"identity:{root}", lambda: str(root.resolve())
        )
    except Exception:
        return str(root)
    if resolved is volume_scan.PENDING:
        return str(root)
    return str(resolved)


def _walk(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _first_text(node: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = node.get(key)
        if value is None:
            continue
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        text = str(value).strip()
        if text:
            return text
    return ""


def _run(command: list[str], timeout: int = 30) -> bytes:
    proc = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Lệnh lỗi: {' '.join(command)}\\n{detail}")
    return proc.stdout


def _diskutil_info(target: str) -> dict[str, Any]:
    return plistlib.loads(_run(["/usr/sbin/diskutil", "info", "-plist", target], 20))


def _mac_ioreg_nodes() -> list[dict[str, Any]]:
    commands = (
        [
            "/usr/sbin/ioreg",
            "-a",
            "-p",
            "IOUSB",
            "-l",
            "-w",
            "0",
        ],
        [
            "/usr/sbin/ioreg",
            "-a",
            "-r",
            "-c",
            "IOUSBHostDevice",
            "-l",
        ],
    )

    nodes: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    errors: list[str] = []

    for command in commands:
        try:
            tree = plistlib.loads(
                _run(
                    command,
                    30,
                )
            )
        except Exception as exc:
            errors.append(str(exc))
            continue

        for node in _walk(tree):
            serial = _first_text(
                node,
                (
                    "USB Serial Number",
                    "kUSBSerialNumberString",
                    "USBSerialNumber",
                    "serial_num",
                    "serial_number",
                    "Serial Number",
                ),
            )
            if not serial:
                continue

            product = _first_text(
                node,
                (
                    "USB Product Name",
                    "kUSBProductString",
                    "_name",
                    "product_name",
                    "Product Name",
                ),
            )
            manufacturer = _first_text(
                node,
                (
                    "USB Vendor Name",
                    "kUSBVendorString",
                    "manufacturer",
                    "vendor_name",
                    "Manufacturer",
                ),
            )
            key = (
                normalize_serial(serial),
                normalize_text(manufacturer),
                normalize_text(product),
            )
            if key in seen:
                continue

            seen.add(key)
            nodes.append(node)

    if nodes:
        return nodes

    raise RuntimeError(
        "macOS không đọc được serial thiết bị USB từ I/O Registry"
        + (": " + " | ".join(errors) if errors else ".")
    )


def _node_identity(node: dict[str, Any]) -> USBHardwareIdentity:
    identity = USBHardwareIdentity(
        serial=_first_text(
            node,
            (
                "USB Serial Number",
                "kUSBSerialNumberString",
                "USBSerialNumber",
                "serial_num",
                "serial_number",
                "Serial Number",
            ),
        ),
        vendor_id=next(
            (
                str(node[k])
                for k in ("idVendor", "vendor_id", "vendorID", "Vendor ID")
                if k in node
            ),
            "",
        ),
        product_id=next(
            (
                str(node[k])
                for k in ("idProduct", "product_id", "productID", "Product ID")
                if k in node
            ),
            "",
        ),
        manufacturer=_first_text(
            node,
            (
                "USB Vendor Name",
                "kUSBVendorString",
                "manufacturer",
                "vendor_name",
                "Manufacturer",
            ),
        ),
        product_name=_first_text(
            node,
            (
                "USB Product Name",
                "kUSBProductString",
                "_name",
                "product_name",
                "Product Name",
            ),
        ),
    )
    _validate(identity)
    return identity


def _node_matches_identifiers(node: dict[str, Any], identifiers: set[str]) -> bool:
    if not identifiers:
        return False
    for nested in _walk(node):
        for key in (
            "BSD Name",
            "bsd_name",
            "DeviceIdentifier",
            "device_identifier",
            "ParentWholeDisk",
            "BSD Major",
            "IOBSDName",
        ):
            value = str(nested.get(key) or "").strip()
            if value and value in identifiers:
                return True
    return False


def _mac_identity(root: Path) -> USBHardwareIdentity:
    override = os.environ.get("TRACYY_TEST_USB_IDENTITY_JSON", "").strip()
    if override:
        identity = USBHardwareIdentity(**json.loads(override))
        _validate(identity)
        return identity

    root_real = _mount_point(root)
    identifiers: set[str] = set()
    try:
        info = _diskutil_info(root_real)
        for key in ("DeviceIdentifier", "ParentWholeDisk"):
            value = info.get(key)
            if value:
                identifiers.add(str(value))
        device = info.get("DeviceIdentifier")
        if device:
            try:
                whole = _diskutil_info(f"/dev/{device}")
                for key in ("DeviceIdentifier", "ParentWholeDisk"):
                    value = whole.get(key)
                    if value:
                        identifiers.add(str(value))
            except Exception:
                pass
    except Exception:
        pass

    nodes = _mac_ioreg_nodes()
    matched = [node for node in nodes if _node_matches_identifiers(node, identifiers)]
    if len(matched) == 1:
        return _node_identity(matched[0])
    if len(matched) > 1:
        matched.sort(key=lambda node: len(list(_walk(node))))
        return _node_identity(matched[0])

    # Some macOS versions do not expose BSD mapping in the IOUSB subtree.
    # A unique USB-storage serial is still safe when only one candidate exists.
    identities: list[USBHardwareIdentity] = []
    for node in nodes:
        try:
            identities.append(_node_identity(node))
        except Exception:
            continue
    unique: dict[str, USBHardwareIdentity] = {
        item.normalized()["serial"]: item for item in identities
    }
    if len(unique) == 1:
        return next(iter(unique.values()))

    # Prefer a unique SanDisk identity when other USB peripherals are attached.
    sandisk = [
        item
        for item in unique.values()
        if "SANDISK" in f"{item.manufacturer} {item.product_name}".upper()
    ]
    if len(sandisk) == 1:
        return sandisk[0]

    raise RuntimeError(
        "Không xác định được đúng hardware serial của USB chứa token trên macOS. "
        "Hãy cắm trực tiếp USB và tháo các USB lưu trữ khác khi tạo license."
    )


def _windows_identity(root: Path) -> USBHardwareIdentity:
    override = os.environ.get("TRACYY_TEST_USB_IDENTITY_JSON", "").strip()
    if override:
        identity = USBHardwareIdentity(**json.loads(override))
        _validate(identity)
        return identity

    drive = str(root.drive or root)[:2]
    script = rf"""
$logical = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='{drive}'"
$partition = Get-CimAssociatedInstance -InputObject $logical -Association Win32_LogicalDiskToPartition | Select-Object -First 1
$disk = Get-CimAssociatedInstance -InputObject $partition -Association Win32_DiskDriveToDiskPartition | Select-Object -First 1
[PSCustomObject]@{{
  serial = ($disk.SerialNumber -as [string]).Trim()
  pnp = $disk.PNPDeviceID
  model = $disk.Model
  manufacturer = $disk.Manufacturer
}} | ConvertTo-Json -Compress
"""
    executables = ("powershell.exe", "powershell", "pwsh.exe", "pwsh")
    raw = b""
    last_error: Exception | None = None
    for executable in executables:
        try:
            raw = _run(
                [
                    executable,
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    script,
                ],
                25,
            )
            break
        except Exception as exc:
            last_error = exc
    if not raw:
        raise RuntimeError("Windows không đọc được hardware serial USB.") from last_error
    data = json.loads(raw.decode("utf-8", errors="replace"))
    pnp = str(data.get("pnp") or "")
    vid_match = re.search(r"VID[_&:=\\-]?([0-9A-F]{4})", pnp.upper())
    pid_match = re.search(r"PID[_&:=\\-]?([0-9A-F]{4})", pnp.upper())
    identity = USBHardwareIdentity(
        serial=str(data.get("serial") or ""),
        vendor_id=vid_match.group(1) if vid_match else "",
        product_id=pid_match.group(1) if pid_match else "",
        manufacturer=str(data.get("manufacturer") or ""),
        product_name=str(data.get("model") or ""),
    )
    _validate(identity)
    return identity


def _linux_identity(root: Path) -> USBHardwareIdentity:
    override = os.environ.get("TRACYY_TEST_USB_IDENTITY_JSON", "").strip()
    if override:
        identity = USBHardwareIdentity(**json.loads(override))
        _validate(identity)
        return identity
    raw = _run(["lsblk", "-J", "-o", "MOUNTPOINT,SERIAL,VENDOR,MODEL,TRAN"], 20)
    data = json.loads(raw.decode("utf-8", errors="replace"))
    target = _mount_point(root)
    for node in _walk(data):
        if str(node.get("mountpoint") or "") == target:
            identity = USBHardwareIdentity(
                serial=str(node.get("serial") or ""),
                manufacturer=str(node.get("vendor") or ""),
                product_name=str(node.get("model") or ""),
            )
            _validate(identity)
            return identity
    raise RuntimeError("Không tìm thấy USB trong lsblk.")


def _validate(identity: USBHardwareIdentity) -> None:
    serial = normalize_serial(identity.serial)
    if not serial or serial in {"0", "00", "0000", "00000000", "UNKNOWN", "NONE"}:
        raise RuntimeError(
            "USB này không cung cấp hardware serial thật. "
            "Tracyy không dùng Volume UUID hoặc tên ổ đĩa làm license."
        )
    if len(serial) < 8:
        raise RuntimeError("Hardware serial USB quá ngắn, không đủ tin cậy.")


def read_usb_hardware_identity(root: Path) -> USBHardwareIdentity:
    system = platform.system().lower()
    if system == "darwin":
        return _mac_identity(root)
    if system == "windows":
        return _windows_identity(root)
    return _linux_identity(root)


def _deduplicate_identities(
    identities: Iterable[USBHardwareIdentity],
) -> list[USBHardwareIdentity]:
    output: list[USBHardwareIdentity] = []
    seen: set[str] = set()

    for identity in identities:
        try:
            _validate(identity)
        except Exception:
            continue

        serial = normalize_serial(identity.serial)
        if serial in seen:
            continue

        seen.add(serial)
        output.append(identity)

    return output


def _mac_system_profiler_nodes() -> list[dict[str, Any]]:
    try:
        raw = _run(
            [
                "/usr/sbin/system_profiler",
                "SPUSBDataType",
                "-json",
                "-detailLevel",
                "full",
            ],
            45,
        )
        data = json.loads(
            raw.decode(
                "utf-8",
                errors="replace",
            )
        )
    except Exception:
        return []

    nodes: list[dict[str, Any]] = []

    for node in _walk(data):
        serial = _first_text(
            node,
            (
                "serial_num",
                "serial_number",
                "USB Serial Number",
                "Serial Number",
            ),
        )
        if serial:
            nodes.append(node)

    return nodes


def _identity_serial_aliases(
    identity: USBHardwareIdentity,
) -> list[str]:
    """
    Return normalized aliases for one OS-reported USB identity.

    The primary value is always the exact normalized hardware serial.
    A hex-to-ASCII alias is added only when the whole serial is valid
    hexadecimal and decodes to a printable, non-trivial identifier.
    """
    primary = normalize_serial(identity.serial)
    output: list[str] = []

    if primary:
        output.append(primary)

    if (
        primary
        and len(primary) % 2 == 0
        and re.fullmatch(
            r"[0-9A-F]+",
            primary,
        )
    ):
        try:
            decoded_bytes = bytes.fromhex(primary)
            decoded = decoded_bytes.decode("ascii").strip("\x00 \t\r\n")
            decoded_normalized = normalize_serial(decoded)
            printable = all(32 <= byte <= 126 or byte == 0 for byte in decoded_bytes)

            if printable and len(decoded_normalized) >= 8 and decoded_normalized != primary:
                output.append(decoded_normalized)
        except Exception:
            pass

    return list(dict.fromkeys(output))


def _mac_connected_identities() -> list[USBHardwareIdentity]:
    identities: list[USBHardwareIdentity] = []

    nodes: list[dict[str, Any]] = []

    try:
        nodes.extend(_mac_ioreg_nodes())
    except Exception:
        pass

    nodes.extend(_mac_system_profiler_nodes())

    for node in nodes:
        try:
            identity = _node_identity(node)
        except Exception:
            continue

        for serial in _identity_serial_aliases(identity):
            identities.append(
                USBHardwareIdentity(
                    serial=serial,
                    vendor_id=identity.vendor_id,
                    product_id=identity.product_id,
                    manufacturer=identity.manufacturer,
                    product_name=identity.product_name,
                )
            )

    return _deduplicate_identities(identities)


def _windows_connected_identities() -> list[USBHardwareIdentity]:
    script = r"""
$items = @()

$drives = Get-CimInstance Win32_DiskDrive |
    Where-Object {
        $_.InterfaceType -eq "USB" -or
        $_.PNPDeviceID -like "USBSTOR*"
    }

foreach ($disk in $drives) {
    $items += [PSCustomObject]@{
        disk_serial = (
            $disk.SerialNumber -as [string]
        ).Trim()
        pnp = [string]$disk.PNPDeviceID
        model = [string]$disk.Model
        manufacturer = [string]$disk.Manufacturer
    }
}

$items | ConvertTo-Json -Compress -Depth 4
"""

    raw = b""
    last_error: Exception | None = None

    for executable in (
        "powershell.exe",
        "powershell",
        "pwsh.exe",
        "pwsh",
    ):
        try:
            raw = _run(
                [
                    executable,
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    script,
                ],
                30,
            )
            break
        except Exception as exc:
            last_error = exc

    if not raw:
        raise RuntimeError("Windows không liệt kê được USB hardware serial.") from last_error

    parsed = json.loads(
        raw.decode(
            "utf-8",
            errors="replace",
        )
    )
    rows = (
        parsed
        if isinstance(
            parsed,
            list,
        )
        else [parsed]
    )
    identities: list[USBHardwareIdentity] = []

    for row in rows:
        if not isinstance(
            row,
            dict,
        ):
            continue

        pnp = str(row.get("pnp") or "")
        vid_match = re.search(
            r"VID[_&:=\\-]?([0-9A-F]{4})",
            pnp.upper(),
        )
        pid_match = re.search(
            r"PID[_&:=\\-]?([0-9A-F]{4})",
            pnp.upper(),
        )

        serial_candidates: list[str] = []

        disk_serial = normalize_serial(row.get("disk_serial"))
        if disk_serial:
            serial_candidates.append(disk_serial)

        if "\\" in pnp:
            pnp_serial = pnp.rsplit(
                "\\",
                1,
            )[-1]
            pnp_serial = re.sub(
                r"&\d+$",
                "",
                pnp_serial,
            )
            pnp_serial = normalize_serial(pnp_serial)
            if pnp_serial:
                serial_candidates.append(pnp_serial)

        for serial in dict.fromkeys(serial_candidates):
            identity = USBHardwareIdentity(
                serial=serial,
                vendor_id=(vid_match.group(1) if vid_match else ""),
                product_id=(pid_match.group(1) if pid_match else ""),
                manufacturer=str(row.get("manufacturer") or ""),
                product_name=str(row.get("model") or ""),
            )

            for alias in _identity_serial_aliases(identity):
                identities.append(
                    USBHardwareIdentity(
                        serial=alias,
                        vendor_id=identity.vendor_id,
                        product_id=identity.product_id,
                        manufacturer=identity.manufacturer,
                        product_name=identity.product_name,
                    )
                )

    return _deduplicate_identities(identities)


def _linux_connected_identities() -> list[USBHardwareIdentity]:
    raw = _run(
        ["lsblk", "-J", "-o", "SERIAL,VENDOR,MODEL,TRAN"],
        20,
    )
    data = json.loads(raw.decode("utf-8", errors="replace"))
    identities: list[USBHardwareIdentity] = []

    for node in _walk(data):
        if str(node.get("tran") or "").lower() != "usb":
            continue

        identities.append(
            USBHardwareIdentity(
                serial=str(node.get("serial") or ""),
                manufacturer=str(node.get("vendor") or ""),
                product_name=str(node.get("model") or ""),
            )
        )

    return _deduplicate_identities(identities)


def list_connected_usb_hardware_identities() -> list[USBHardwareIdentity]:
    override = os.environ.get(
        "TRACYY_TEST_CONNECTED_USB_IDENTITIES_JSON",
        "",
    ).strip()

    if override:
        values = json.loads(override)
        return _deduplicate_identities(USBHardwareIdentity(**item) for item in values)

    system = platform.system().lower()

    if system == "darwin":
        return _mac_connected_identities()

    if system == "windows":
        return _windows_connected_identities()

    return _linux_connected_identities()

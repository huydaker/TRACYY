"""The wire format between Tracyy Live machines.

One module both ends import, so a field can never be spelled two ways. Plain
data and plain functions — no Qt, no sockets — because the host, the client
and the tests all need it and none of them should drag the others in.

The transport is deliberately boring: JSON over HTTP POST, one request per
message. A show LAN is not a place to debug a bespoke binary protocol, and
every failure mode here already has a well-known name and a well-known error.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

__all__ = [
    "DEFAULT_PORT",
    "HEARTBEAT_SECONDS",
    "PATH_DOCUMENT",
    "PATH_HEARTBEAT",
    "PATH_HELLO",
    "PATH_JOIN",
    "PATH_LEAVE",
    "PATH_MANIFEST",
    "PATH_MEDIA",
    "PATH_SUBMIT_OP",
    "PROTOCOL_VERSION",
    "REQUEST_TIMEOUT",
    "SyncError",
    "decode",
    "encode",
    "server_url",
    "split_address",
]

#: Bumped when a change makes an older build unable to understand a newer one.
#: Sent on every join so the mismatch is reported as a mismatch, instead of as
#: a missing field somewhere deep in a handler.
#:
#: 3 replaced the presence field ``track_path`` with ``track_id`` plus a
#: display-only ``track_name``. A removal, not an addition: an older build
#: keeps sending a path nobody reads any more, so its row would simply show
#: no track at all — the silent half-working state this number exists to
#: prevent. Refusing the join and naming the mismatch is the kinder failure.
PROTOCOL_VERSION = 3

DEFAULT_PORT = 8090

PATH_HELLO = "/live/hello"
PATH_JOIN = "/live/join"
PATH_HEARTBEAT = "/live/heartbeat"
PATH_LEAVE = "/live/leave"
#: Added without a version bump: an older build never posts here, and every
#: other message only gained optional fields it already ignores.
PATH_SUBMIT_OP = "/live/submit_op"
#: What music the session has, and the bytes of it. Additive like submit_op:
#: an older build never asks, and never has to be told these exist.
PATH_DOCUMENT = "/live/document"
PATH_MANIFEST = "/live/manifest"
PATH_MEDIA = "/live/media"

#: Music requests carry a chunk of a file back, so they are allowed far longer
#: than a heartbeat. The manifest is slower still the first time it is asked
#: for, because that is when a show's worth of audio gets hashed.
MEDIA_TIMEOUT = 20.0
MANIFEST_TIMEOUT = 120.0

#: How often a client checks in. Also how quickly a new arrival shows up in
#: everyone else's list, so it is short enough to feel immediate.
HEARTBEAT_SECONDS = 1.5

#: A request that has not answered by now is treated as a failure. Short on
#: purpose: on a LAN anything slower than this is not slow, it is broken, and
#: waiting longer only delays telling the user so.
REQUEST_TIMEOUT = 4.0


class SyncError(Exception):
    """A failure with a message meant for the person, not the log.

    Every raise site writes what went wrong *and what to do*, because these
    strings go straight onto the Sync page — "Connection refused" alone sends
    somebody to check their cable when the host simply is not hosting.
    """


def encode(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def decode(raw: bytes) -> dict[str, Any]:
    """Parse a message body, or raise ``SyncError`` describing what arrived.

    Anything on this socket could be another program entirely — a browser, a
    port scanner, the wrong port typed by hand — so a body that is not a JSON
    object is a normal event to report, not an exception to propagate.
    """
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SyncError("Máy chính trả về dữ liệu không đọc được.") from exc

    if not isinstance(value, dict):
        raise SyncError("Địa chỉ này không phải một phiên Tracyy Live.")
    return value


def split_address(address: str) -> tuple[str, int]:
    """Turn what a person typed into a host and a port.

    Accepts ``192.168.1.9``, ``192.168.1.9:8090`` and a pasted
    ``http://192.168.1.9:8090/`` — all three are what actually gets typed or
    pasted, and rejecting two of them teaches nothing.
    """
    text = str(address or "").strip()
    if not text:
        raise SyncError("Chưa nhập địa chỉ máy chính.")

    try:
        parsed = urlsplit(text if "://" in text else f"http://{text}")
        host = parsed.hostname
        port = parsed.port if parsed.port is not None else DEFAULT_PORT
    except ValueError:
        raise SyncError("Địa chỉ hoặc cổng không hợp lệ. Ví dụ 192.168.1.20:8090.") from None
    if parsed.scheme not in {"http", "tracyy"}:
        raise SyncError("Tracyy Live dùng địa chỉ IP và cổng HTTP, ví dụ 192.168.1.20:8090.")
    if (
        not host
        or parsed.username is not None
        or parsed.password is not None
        or any(char.isspace() for char in host)
        or parsed.netloc.endswith(":")
    ):
        raise SyncError("Địa chỉ máy chính không hợp lệ.")
    if host in {"0.0.0.0", "::"}:
        raise SyncError("Nhập IP mạng của máy chính; 0.0.0.0 không phải địa chỉ để kết nối.")
    if not 1 <= port <= 65535:
        raise SyncError(f"Cổng {port} nằm ngoài khoảng cho phép.")
    return host, port


def server_url(address: str) -> str:
    host, port = split_address(address)
    return f"http://{'[' + host + ']' if ':' in host else host}:{port}"

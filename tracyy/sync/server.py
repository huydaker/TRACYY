"""The host side of a Tracyy Live session.

An HTTP server holding one thing: who is in the session right now. It does not
touch the application's project, does not import Qt, and never calls back into
the GUI — the window reads :meth:`LiveServer.snapshot` from its own thread on
the tick it already runs. That one rule is what keeps a request from another
machine off the thread that draws and decodes audio.

Cue operations commit here now, one small record at a time — the same order
of bytes as a heartbeat, which is why they may. Whole-show snapshots and
media still will not: when they arrive this moves into its own process, the
way the LAN cue viewer already is, because serialising a whole show holds the
GIL long enough to make the audio callback late.
"""

from __future__ import annotations

import base64
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .document import Operation, SyncDocument, capability_for
from .media import MEDIA_CHUNK_BYTES, MediaLibrary
from .model import PEER_COLOURS, SyncRole, role_allows
from .protocol import (
    PATH_DOCUMENT,
    PATH_HEARTBEAT,
    PATH_HELLO,
    PATH_JOIN,
    PATH_LEAVE,
    PATH_MANIFEST,
    PATH_MEDIA,
    PATH_SUBMIT_OP,
    PROTOCOL_VERSION,
    SyncError,
    decode,
    encode,
)
from .store import store_for

__all__ = ["LiveServer"]

#: A peer that has not checked in for this long is dropped. Several heartbeats
#: of slack: a laptop that garbage-collects or sleeps a moment should not
#: vanish from everybody's list and come straight back.
PEER_TIMEOUT = 8.0

#: Refused before reading, so a wrong port or a stray browser cannot make the
#: host allocate whatever a request claims its body is.
MAX_BODY_BYTES = 64 * 1024


class LiveServer:
    """Accepts joins and heartbeats, and remembers who is in."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._http: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._peers: dict[str, dict] = {}
        # Membership outlives presence; sleeping must not discard a grant.
        self._members: dict[str, dict] = {}
        self._tokens: dict[str, str] = {}
        self._join_order = 0
        self._session_name = ""
        self._code = ""
        self._host_id = ""
        # Minted per hosting run. session_id changing on every restart is what
        # stops a client's leftover operations from replaying into a session
        # they were never written against. Neither is the client-local show_id
        # that names a ShowStore cache folder — no two machines agree on that.
        self._session_id = ""
        self._document_id = ""
        self._document: SyncDocument | None = None
        #: The only files this host will ever hand out. A request names a
        #: track in the session, never a path, so there is nothing to walk
        #: out of and no way to ask for a file that is not in the show.
        self._media = MediaLibrary()
        #: Records committed by somebody else that the machine running this
        #: host has not shown yet. Without it the host holds the authoritative
        #: document and displays a stale copy of it: everybody else sees the
        #: edit, the person who owns the show does not.
        self._host_inbox: list[dict] = []

    # -- lifecycle --------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._http is not None

    @property
    def port(self) -> int:
        http = self._http
        return int(http.server_address[1]) if http is not None else 0

    def start(
        self,
        bind_address: str,
        port: int,
        session_name: str,
        code: str,
        host_machine_id: str,
        host_display_name: str = "Máy chính",
        journal_path: str | Path | None = None,
        show_id: str = "",
    ) -> None:
        """Begin accepting connections, or raise ``SyncError`` saying why not.

        ``journal_path`` names where committed operations are persisted; left
        unset, the journal lands in this document's own ShowStore folder under
        the cache root. Tests pass a tmp path so nothing touches the cache.
        """
        if self.running:
            return

        with self._lock:
            self._session_name = str(session_name or "")
            self._code = str(code or "").strip().upper()
            self._host_id = str(host_machine_id or "")
            self._peers.clear()
            self._members.clear()
            self._tokens.clear()
            self._join_order = 0
            self._session_id = secrets.token_hex(8)
            self._document_id = secrets.token_hex(8)
            # Minted per run like the others unless the caller has something
            # steadier to offer. The clients name their download folder after
            # it, so a fresh one every time the host restarts means every
            # laptop in the room fetches the whole show again.
            self._show_id = str(show_id or "").strip() or self._document_id
            if journal_path is None:
                store = store_for(self._document_id)
                assert store is not None  # token_hex never fails store_for
                journal_path = store.journal_path
            self._document = SyncDocument(
                self._session_id, self._document_id, journal_path
            )

            # The host goes in the table with everybody else. Leaving it out
            # meant a client's list showed only itself: it could see that it
            # had joined, but not who it had joined.
            if self._host_id:
                self._peers[self._host_id] = {
                    "machine_id": self._host_id,
                    "display_name": str(host_display_name or "Máy chính"),
                    "role": SyncRole.OWNER.value,
                    "colour": PEER_COLOURS[0],
                    "track_id": "",
                    "track_name": "",
                    "editing_cue_id": "",
                    "editing_column": "",
                    "position_seconds": 0.0,
                    "last_seen": time.monotonic(),
                }

        try:
            server_type = _IPv6PeerServer if ":" in bind_address else _PeerServer
            http = server_type((str(bind_address or "0.0.0.0"), int(port)), _Handler)
            http.owner = self
        except OSError as exc:
            raise SyncError(_bind_message(exc, port)) from exc

        self._http = http
        self._thread = threading.Thread(
            target=http.serve_forever,
            kwargs={"poll_interval": 0.2},
            name="TracyyLiveServer",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        http, thread = self._http, self._thread
        self._http = None
        self._thread = None

        if http is not None:
            http.shutdown()
            http.server_close()
        if thread is not None:
            thread.join(timeout=3.0)

        with self._lock:
            self._peers.clear()
            self._members.clear()
            self._tokens.clear()
            self._session_id = ""
            self._document_id = ""
            self._document = None

    # -- what the window reads -------------------------------------------

    def snapshot(self) -> list[dict]:
        """Everyone currently in, most recently silent already removed.

        Read from the GUI thread; the pruning happens here rather than on a
        timer of its own so there is exactly one place that decides who is
        still present.
        """
        cutoff = time.monotonic() - PEER_TIMEOUT
        with self._lock:
            # The host never heartbeats — it is this process — so it is exempt
            # from the timeout rather than dropping itself after eight seconds.
            gone = [
                machine_id
                for machine_id, peer in self._peers.items()
                if peer["last_seen"] < cutoff and machine_id != self._host_id
            ]
            for machine_id in gone:
                self._peers.pop(machine_id, None)
            return [dict(peer) for peer in self._peers.values()]

    def set_role(self, machine_id: str, role: str) -> bool:
        """Change a peer's role. The client picks it up on its next heartbeat."""
        try:
            resolved = SyncRole(role)
        except ValueError:
            return False
        if resolved == SyncRole.OWNER:
            return False
        with self._lock:
            if str(machine_id) == self._host_id:
                # The host cannot be demoted out of its own session; there
                # would be nobody left able to grant the role back.
                return False
            peer = self._peers.get(str(machine_id))
            if peer is None:
                return False
            peer["role"] = resolved.value
            return True

    def drop(self, machine_id: str) -> None:
        with self._lock:
            if str(machine_id) != self._host_id:
                self._peers.pop(str(machine_id), None)

    def set_host_presence(
        self,
        display_name: str,
        track_id: str,
        track_name: str,
        position: float,
        editing_cue_id: str = "",
        editing_column: str = "",
    ) -> None:
        """Mirror the host's own name and position into the table it serves."""
        with self._lock:
            peer = self._peers.get(self._host_id)
            if peer is None:
                return
            if display_name:
                peer["display_name"] = str(display_name)
            peer["track_id"] = str(track_id or "")
            peer["track_name"] = str(track_name or "")[:120]
            peer["editing_cue_id"] = str(editing_cue_id or "")[:120]
            peer["editing_column"] = str(editing_column or "")[:32]
            try:
                peer["position_seconds"] = float(position)
            except (TypeError, ValueError):
                pass
            peer["last_seen"] = time.monotonic()

    def take_host_ops(self) -> list[dict]:
        """Commits from other machines, for this machine's own window.

        Only other machines: the host applies its own edits as it makes them,
        the same way a client does, so handing them back would be asking the
        window to do the same work twice.
        """
        with self._lock:
            records, self._host_inbox = self._host_inbox, []
            return records

    def seed_document(self, tracks: dict[str, list[dict]]) -> None:
        """Put the cues this machine already had into the session."""
        with self._lock:
            if self._document is not None:
                self._document.seed(tracks)

    def publish_media(self, tracks: dict[str, str]) -> None:
        """Say which songs this machine can serve, as ``track_id -> path``."""
        self._media.publish(tracks)

    @property
    def revision(self) -> int:
        document = self._document
        return document.revision if document is not None else 0

    def commit_local(self, op: dict) -> dict:
        """Commit an operation made on the machine that is hosting.

        Same document, same journal, same revision counter as anything
        arriving over HTTP. The host does not get a private door into its own
        show: two ways to write would be two sets of rules, and only one of
        them would ever be tested.
        """
        with self._lock:
            document = self._document
            if document is None:
                return {"ok": False, "error": "Phiên đã đóng.", "reason": "closed"}

            body = dict(op)
            body.setdefault("session_id", self._session_id)
            body.setdefault("document_id", self._document_id)
            body.setdefault("base_revision", document.revision)
            try:
                parsed = Operation.from_wire(body)
            except ValueError as exc:
                return {"ok": False, "error": str(exc), "reason": "invalid"}

            result = document.submit(parsed, author=self._host_id)
            if not result.ok:
                return {"ok": False, "error": result.error, "reason": result.reason}
            return {"ok": True, "revision": result.revision, "record": result.record}

    # -- what the handler calls ------------------------------------------

    def _hello(self) -> dict:
        with self._lock:
            return {
                "ok": True,
                "protocol": PROTOCOL_VERSION,
                "session_name": self._session_name,
                "session_id": self._session_id,
                "document_id": self._document_id,
                # The code is never returned. Answering "is this a Tracyy
                # host?" must not also answer "and here is the code", or the
                # code stops being a check at all.
                "needs_code": bool(self._code),
            }

    def _join(self, payload: dict) -> dict:
        machine_id = str(payload.get("machine_id", "") or "").strip()
        if not machine_id:
            raise SyncError("Máy tham gia không gửi mã máy.")

        try:
            protocol = int(payload.get("protocol", 0))
        except (TypeError, ValueError):
            protocol = 0
        if protocol != PROTOCOL_VERSION:
            raise SyncError(
                "Hai máy chạy hai phiên bản Tracyy khác nhau. "
                "Cập nhật cho cùng phiên bản rồi thử lại."
            )

        with self._lock:
            if self._code and str(payload.get("code", "")).strip().upper() != self._code:
                raise SyncError("Mã phiên không đúng.")
            if machine_id == self._host_id:
                raise SyncError("Mã máy trùng với máy chính. Hãy kết nối từ máy khác.")
            self.snapshot()
            token = str(payload.get("token", "") or "")
            existing = self._members.get(machine_id)
            authenticated = bool(token) and secrets.compare_digest(
                token, self._tokens.get(machine_id, "")
            )
            if existing is not None and not authenticated:
                if machine_id in self._peers:
                    raise SyncError(
                        "Mã máy này đang kết nối ở cửa sổ khác. Rời phiên ở đó rồi thử lại."
                    )
                # A fresh installation may reuse a machine id, but cannot
                # inherit another connection's edit permission without its token.
                existing = None
            if existing is None:
                # The first colour belongs to the host.
                colour = PEER_COLOURS[(self._join_order + 1) % len(PEER_COLOURS)]
                self._join_order += 1
                existing = {
                    "machine_id": machine_id,
                    "display_name": str(payload.get("display_name", "") or "Không tên"),
                    "role": SyncRole.VIEWER.value,
                    "colour": colour,
                    "track_id": "",
                    "track_name": "",
                    "editing_cue_id": "",
                    "editing_column": "",
                    "position_seconds": 0.0,
                }
                self._members[machine_id] = existing
            else:
                # A rejoin after a dropped link keeps the role it was granted;
                # having to hand somebody their permissions back every time
                # their laptop slept is not a workflow.
                existing["display_name"] = str(
                    payload.get("display_name", "") or existing["display_name"]
                )

            existing["last_seen"] = time.monotonic()
            self._peers[machine_id] = existing
            # Repeated joins use the same private credential, so losing the
            # first reply does not leave the client unable to authenticate.
            token = token or secrets.token_urlsafe(32)
            self._tokens[machine_id] = token
            return {
                "ok": True,
                "protocol": PROTOCOL_VERSION,
                "session_name": self._session_name,
                "session_id": self._session_id,
                "document_id": self._document_id,
                "token": token,
                "you": dict(existing),
                "peers": [dict(peer) for peer in self._peers.values()],
            }

    def _heartbeat(self, payload: dict) -> dict:
        machine_id = str(payload.get("machine_id", "") or "").strip()
        with self._lock:
            self.snapshot()
            peer = self._peers.get(machine_id)
            if peer is None:
                # Told to rejoin rather than silently re-added: the client has
                # to learn its role again, and re-adding it here would give it
                # viewer rights without saying so.
                return {"ok": False, "rejoin": True}

            self._authenticate(machine_id, payload)

            peer["last_seen"] = time.monotonic()
            if "display_name" in payload:
                peer["display_name"] = str(payload.get("display_name") or "Không tên")[:32]
            if "track_id" in payload:
                peer["track_id"] = str(payload.get("track_id", "") or "")
            if "track_name" in payload:
                peer["track_name"] = str(payload.get("track_name", "") or "")[:120]
            if "editing_cue_id" in payload:
                peer["editing_cue_id"] = str(payload.get("editing_cue_id", "") or "")[:120]
            if "editing_column" in payload:
                peer["editing_column"] = str(payload.get("editing_column", "") or "")[:32]
            if "position_seconds" in payload:
                try:
                    peer["position_seconds"] = float(payload.get("position_seconds", 0.0))
                except (TypeError, ValueError):
                    pass
            try:
                peer["acked_revision"] = max(0, int(payload.get("acked_revision", 0)))
            except (TypeError, ValueError):
                pass

            document = self._document
            return {
                "ok": True,
                "session_name": self._session_name,
                "session_id": self._session_id,
                "document_id": self._document_id,
                "show_id": self._show_id,
                "you": dict(peer),
                "peers": [dict(other) for other in self._peers.values()],
                # Committed operations this peer has not acknowledged yet.
                # The beat that already carries presence carries these too —
                # each record is one cue edit, heartbeat-sized, never a
                # serialised show.
                "revision": document.revision if document is not None else 0,
                "ops": (
                    document.pending_since(int(peer.get("acked_revision", 0)))
                    if document is not None
                    else []
                ),
            }

    def _submit_op(self, payload: dict) -> dict:
        """Commit one document operation, or refuse it with a reason.

        The order of checks is the security model: authenticate before the
        document is touched, then resolve the author from those credentials —
        never from a field the client filled in — then ask the role table,
        and only then let the operation near the journal.
        """
        machine_id = str(payload.get("machine_id", "") or "").strip()
        with self._lock:
            self.snapshot()
            peer = self._peers.get(machine_id)
            if peer is None:
                # Same contract as the heartbeat: told to rejoin rather than
                # silently trusted, because a peer that timed out has to learn
                # its role again before it may write anything.
                return {
                    "ok": False,
                    "rejoin": True,
                    "error": "Kết nối đã gián đoạn. Hãy nối lại phiên.",
                    "reason": "not_joined",
                }
            self._authenticate(machine_id, payload)
            document = self._document
            if document is None:
                raise SyncError("Phiên đã đóng.")

            try:
                op = Operation.from_wire(payload.get("op") or {})
            except ValueError as exc:
                return {"ok": False, "error": str(exc), "reason": "invalid"}

            if not role_allows(peer["role"], capability_for(op.kind)):
                return {
                    "ok": False,
                    "error": "Bạn không có quyền thực hiện thao tác này. "
                    "Hỏi chủ phiên để được cấp.",
                    "reason": "forbidden",
                }

            result = document.submit(op, author=machine_id)
            if not result.ok:
                return {"ok": False, "error": result.error, "reason": result.reason}

            if result.record is not None:
                self._host_inbox.append(dict(result.record))
            peer["last_seen"] = time.monotonic()
            return {
                "ok": True,
                "revision": result.revision,
                "op_id": op.op_id,
                "record": result.record,
            }

    def _document_snapshot(self, payload: dict) -> dict:
        """The whole cue document, for a machine that just arrived."""
        machine_id = str(payload.get("machine_id", "") or "").strip()
        with self._lock:
            self._authenticate(machine_id, payload)
            document = self._document
            if document is None:
                return {"ok": False, "error": "Phiên đã đóng.", "reason": "closed"}
            snapshot = document.snapshot()
        return {"ok": True, **snapshot}

    def _manifest(self, payload: dict) -> dict:
        """Every track in the show, with the hash that identifies its bytes.

        The first call after a project is opened hashes the files, which for a
        real show is seconds of disk. It runs on the request thread rather
        than the session lock, so the people already in the session keep
        beating while a new arrival waits for its answer.
        """
        machine_id = str(payload.get("machine_id", "") or "").strip()
        with self._lock:
            self._authenticate(machine_id, payload)
        return {"ok": True, "media": self._media.manifest()}

    def _media_bytes(self, payload: dict) -> dict:
        """One chunk of one track, base64 in the same JSON as everything else.

        Base64 costs a third more bytes than a raw stream would. It is worth
        it here: one encoding, one parser and one error path for every message
        on this socket, on a LAN where the link is not the scarce thing.
        """
        machine_id = str(payload.get("machine_id", "") or "").strip()
        with self._lock:
            self._authenticate(machine_id, payload)

        track_id = str(payload.get("track_id", "") or "")
        try:
            offset = max(0, int(payload.get("offset", 0)))
        except (TypeError, ValueError):
            offset = 0

        block = self._media.read_chunk(track_id, offset, MEDIA_CHUNK_BYTES)
        return {
            "ok": True,
            "offset": offset,
            "data": base64.b64encode(block).decode("ascii"),
        }

    def _leave(self, payload: dict) -> dict:
        machine_id = str(payload.get("machine_id", "") or "")
        with self._lock:
            self._authenticate(machine_id, payload)
            self.drop(machine_id)
        return {"ok": True}

    def _authenticate(self, machine_id: str, payload: dict) -> None:
        token = str(payload.get("token", "") or "")
        expected = self._tokens.get(machine_id, "")
        if not expected or not secrets.compare_digest(token, expected):
            raise SyncError("Kết nối không còn hợp lệ. Hãy nối lại phiên.")


def _bind_message(exc: OSError, port: int) -> str:
    """Turn a bind failure into something the person can act on.

    "Address already in use" sends somebody to reboot; naming the likely cause
    and the fix does not.
    """
    import errno

    if exc.errno == errno.EADDRINUSE:
        return (
            f"Cổng {port} đang bị chương trình khác dùng. "
            "Đổi cổng hoặc tắt chương trình đó rồi tạo lại phiên."
        )
    if exc.errno in {errno.EACCES, errno.EPERM}:
        return (
            f"Không được phép mở cổng {port}. "
            "Trên Windows hãy cho phép Tracyy trong Firewall khi hệ thống hỏi."
        )
    if exc.errno == errno.EADDRNOTAVAIL:
        return "Địa chỉ mạng này không còn khả dụng. Chọn lại mạng rồi thử lại."
    return f"Không mở được cổng {port}: {exc}"


class _PeerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    owner: LiveServer | None = None


class _IPv6PeerServer(_PeerServer):
    address_family = socket.AF_INET6


class _Handler(BaseHTTPRequestHandler):
    #: Silences the default access log. A show laptop's console is not a place
    #: for a line per heartbeat, three times a second per machine.
    def log_message(self, *_args) -> None:
        return

    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(5.0)

    def do_POST(self) -> None:
        owner = getattr(self.server, "owner", None)
        if owner is None:
            self._send(503, {"ok": False, "error": "Phiên đã đóng."})
            return

        routes = {
            PATH_HELLO: lambda _payload: owner._hello(),
            PATH_JOIN: owner._join,
            PATH_HEARTBEAT: owner._heartbeat,
            PATH_SUBMIT_OP: owner._submit_op,
            PATH_DOCUMENT: owner._document_snapshot,
            PATH_MANIFEST: owner._manifest,
            PATH_MEDIA: owner._media_bytes,
            PATH_LEAVE: owner._leave,
        }
        handler = routes.get(self.path)
        if handler is None:
            self._send(404, {"ok": False, "error": "Đường dẫn không đúng."})
            return

        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            length = 0
        if length < 0 or length > MAX_BODY_BYTES:
            self.close_connection = True
            self._send(413, {"ok": False, "error": "Gói tin quá lớn."})
            return

        try:
            payload = decode(self.rfile.read(length)) if length else {}
            self._send(200, handler(payload))
        except SyncError as exc:
            # A refused join is a normal event with a reason, not a server
            # fault: 200 with ok=false keeps the reason readable on the client
            # instead of turning into a bare HTTP error.
            self._send(200, {"ok": False, "error": str(exc)})
        except Exception as exc:  # pragma: no cover - defensive
            self._send(500, {"ok": False, "error": f"Lỗi máy chính: {exc}"})

    def do_GET(self) -> None:
        """Answer a browser plainly instead of looking broken.

        Somebody will paste the address into a browser to check it. A 404 with
        no words reads as "the host is broken"; this reads as "you found it,
        now use the app".
        """
        self.send_response(200)
        body = (
            "Tracyy Live đang chạy ở đây.\n"
            "Mở Tracyy trên máy của bạn, vào mục Sync và nhập địa chỉ này.\n"
        ).encode()
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send(self, status: int, payload: dict) -> None:
        body = encode(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

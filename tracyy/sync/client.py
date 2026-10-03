"""The client side of a Tracyy Live session.

One background thread joins the host and then checks in on a fixed beat. It
never touches Qt and never calls back into the window: it writes its latest
state behind a lock, and the window reads :meth:`LiveClient.snapshot` on the
tick it already runs. Crossing back into Qt from here is the mistake that
produces widgets updated from the wrong thread, and the symptom is a crash
minutes later somewhere unrelated.

A dropped link is not an error to give up on. The thread keeps trying on the
same beat, because the usual cause is a laptop lid or a moment of bad WiFi,
and a session that has to be rejoined by hand every time is one nobody uses.
"""

from __future__ import annotations

import base64
import secrets
import threading
import time
import urllib.error
import urllib.request
from http.client import HTTPException

from .media import MediaEntry
from .protocol import (
    HEARTBEAT_SECONDS,
    MANIFEST_TIMEOUT,
    MEDIA_TIMEOUT,
    PATH_DOCUMENT,
    PATH_HEARTBEAT,
    PATH_HELLO,
    PATH_JOIN,
    PATH_LEAVE,
    PATH_MANIFEST,
    PATH_MEDIA,
    PATH_SUBMIT_OP,
    PROTOCOL_VERSION,
    REQUEST_TIMEOUT,
    SyncError,
    decode,
    encode,
    server_url,
)

__all__ = ["LiveClient", "probe_host"]


def _post(base: str, path: str, payload: dict, timeout: float = REQUEST_TIMEOUT) -> dict:
    """One request, with every failure turned into a sentence worth reading."""
    request = urllib.request.Request(
        f"{base}{path}",
        data=encode(payload),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        # LAN traffic must go directly to the host, including on machines
        # whose system HTTP proxy does not exclude private addresses.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=timeout) as response:
            return decode(response.read())
    except urllib.error.HTTPError as exc:
        raise SyncError(f"Máy chính trả lời lỗi {exc.code}.") from exc
    except TimeoutError as exc:
        raise SyncError(
            "Máy chính không trả lời. Kiểm tra hai máy có cùng mạng không."
        ) from exc
    except urllib.error.URLError as exc:
        raise SyncError(_reach_message(exc, base)) from exc
    except OSError as exc:
        raise SyncError(f"Không nối được tới {base}: {exc}") from exc
    except HTTPException as exc:
        raise SyncError("Máy chính ngắt phản hồi. Đang thử nối lại.") from exc


def _reach_message(exc: urllib.error.URLError, base: str) -> str:
    """Why the host could not be reached, in terms of what to go and check.

    "Connection refused" sends somebody to inspect a cable. Naming the two
    causes that actually produce it — nobody is hosting, or Windows is
    blocking the port — sends them to the right machine.
    """
    reason = getattr(exc, "reason", exc)
    text = str(reason)

    if isinstance(reason, TimeoutError) or "timed out" in text.lower():
        return "Máy chính không trả lời. Kiểm tra hai máy có cùng mạng không."
    if "refused" in text.lower():
        return (
            f"{base} không nhận kết nối. Máy chính chưa tạo phiên, "
            "hoặc Firewall trên máy đó đang chặn Tracyy."
        )
    if "unreachable" in text.lower() or "not known" in text.lower():
        return f"Không tìm thấy {base} trong mạng. Kiểm tra lại địa chỉ."
    return f"Không nối được tới {base}: {text}"


def probe_host(address: str) -> dict:
    """Ask an address whether it is hosting, without joining.

    Called before the join so a wrong address fails in one round trip with a
    clear reason, instead of after a join attempt that reports something about
    codes when the real problem is that nothing is listening.
    """
    return _post(server_url(address), PATH_HELLO, {"protocol": PROTOCOL_VERSION})


class LiveClient:
    """Joins a host and keeps checking in until told to stop."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = "stopped"
        self._error = ""
        self._session_name = ""
        self._peers: list[dict] = []
        self._you: dict = {}
        self._presence: dict = {}
        self._credentials: dict[tuple[str, str, str], str] = {}
        self._notify: dict[threading.Event, bool] = {}
        self._last_reply = 0.0
        # Connection state a caller-thread request needs. Kept as fields
        # rather than _run locals so submit_operation can reach them; written
        # only behind the same generation guard replies pass through, so a
        # superseded connection's token never leaks into the current one.
        self._base = ""
        self._machine_id = ""
        self._token = ""
        self._session_id = ""
        self._document_id = ""
        self._show_id = ""
        self._acked_revision = 0
        #: Committed records the host has sent that the UI has not applied
        #: yet. The worker thread fills it; the Qt tick drains it. Nothing in
        #: here has been acknowledged, so a host that never hears back simply
        #: sends them again — which is what makes a dropped reply harmless.
        self._inbox: list[dict] = []

    # -- lifecycle --------------------------------------------------------

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self, address: str, code: str, machine_id: str, display_name: str) -> None:
        base = server_url(address)
        previous = self._thread
        self.stop()
        # Each worker owns its cancellation event. Reusing and clearing one
        # lets a late response from the previous host revive the old worker.
        stop = threading.Event()
        with self._lock:
            self._stop = stop
            self._notify[stop] = True
            self._state = "connecting"
            self._error = ""
            self._session_name = ""
            self._peers = []
            self._you = {}
            self._presence = {"display_name": str(display_name)}
            self._last_reply = 0.0
            self._base = base
            self._machine_id = str(machine_id)
            self._token = ""
            self._session_id = ""
            self._document_id = ""
            self._acked_revision = 0

        self._thread = threading.Thread(
            target=self._run,
            args=(base, str(code or "").strip().upper(), str(machine_id), stop, previous),
            name="TracyyLiveClient",
            daemon=True,
        )
        self._thread.start()

    def stop(self, notify: bool = True) -> None:
        """Stop checking in, telling the host on the way out when it can.

        The goodbye is best-effort and short: if the link is already gone the
        host drops this machine on its own timeout a few seconds later, and
        blocking the GUI to say farewell to a machine that cannot hear it is
        the wrong trade.
        """
        with self._lock:
            if self._thread is not None and not self._stop.is_set():
                self._notify[self._stop] = notify
            self._stop.set()
            self._state = "stopped"
            self._error = ""
            self._peers = []
            self._you = {}

    # -- what the window reads -------------------------------------------

    def snapshot(self) -> dict:
        with self._lock:
            if (
                self._state == "joined"
                and time.monotonic() - self._last_reply
                > REQUEST_TIMEOUT + 2 * HEARTBEAT_SECONDS
            ):
                self._state = "disconnected"
                self._error = "Chưa nhận được phản hồi mới từ máy chính. Cue đang bị khoá."
                self._peers = []
            return {
                "state": self._state,
                "error": self._error,
                "session_name": self._session_name,
                "peers": [dict(peer) for peer in self._peers],
                "you": dict(self._you),
                "show_id": self._show_id,
            }

    def fetch_manifest(self) -> list[MediaEntry]:
        """What music the session has. Empty when it cannot be asked right now.

        Slow on the far side the first time — the host hashes the show — so
        this must never be called from the thread that draws.
        """
        with self._lock:
            if self._state != "joined" or not self._token:
                return []
            base, machine_id, token = self._base, self._machine_id, self._token

        try:
            reply = _post(
                base,
                PATH_MANIFEST,
                {"machine_id": machine_id, "token": token},
                timeout=MANIFEST_TIMEOUT,
            )
        except SyncError:
            return []
        if not reply.get("ok"):
            return []

        entries = []
        for raw in reply.get("media") or ():
            entry = MediaEntry.from_wire(raw)
            if entry is not None:
                entries.append(entry)
        return entries

    def fetch_media_chunk(self, track_id: str, offset: int) -> bytes:
        """Bytes of one track from ``offset``, or empty when there are none."""
        with self._lock:
            if self._state != "joined" or not self._token:
                return b""
            base, machine_id, token = self._base, self._machine_id, self._token

        try:
            reply = _post(
                base,
                PATH_MEDIA,
                {
                    "machine_id": machine_id,
                    "token": token,
                    "track_id": str(track_id),
                    "offset": int(offset),
                },
                timeout=MEDIA_TIMEOUT,
            )
        except SyncError:
            return b""
        if not reply.get("ok"):
            return b""
        try:
            return base64.b64decode(str(reply.get("data") or ""), validate=True)
        except (ValueError, TypeError):
            # Garbled on the way, or not what it claimed to be. Returning
            # nothing leaves the fragment on disk for the next attempt.
            return b""

    def fetch_document(self) -> dict:
        """The whole cue document as the host has it, or an empty dict.

        Asked for once, when this machine joins: the journal only carries what
        changed during the session, and most of a show existed before anybody
        connected.
        """
        with self._lock:
            if self._state != "joined" or not self._token:
                return {}
            base, machine_id, token = self._base, self._machine_id, self._token

        try:
            reply = _post(
                base,
                PATH_DOCUMENT,
                {"machine_id": machine_id, "token": token},
                timeout=MANIFEST_TIMEOUT,
            )
        except SyncError:
            return {}
        if not reply.get("ok"):
            return {}
        return reply

    def take_ops(self) -> list[dict]:
        """Committed records waiting to be applied, oldest first.

        Handing them over is not acknowledging them: the caller has to apply
        them and then say so with :meth:`confirm_applied`. Losing the window
        between those two is the difference between a cue that arrives twice
        and a cue that never arrives at all.
        """
        with self._lock:
            records, self._inbox = self._inbox, []
            return records

    def confirm_applied(self, revision: int) -> None:
        """Say that everything up to ``revision`` is in the document here."""
        try:
            reached = int(revision)
        except (TypeError, ValueError):
            return
        with self._lock:
            self._acked_revision = max(self._acked_revision, reached)

    def set_presence(
        self,
        track_id: str = "",
        track_name: str = "",
        position_seconds: float = 0.0,
        display_name: str | None = None,
        editing_cue_id: str = "",
        editing_column: str = "",
    ) -> None:
        """What this machine is looking at, sent with the next heartbeat.

        Stored rather than sent: presence changes far faster than the beat,
        and a request per mouse move would be a request per mouse move.
        """
        with self._lock:
            self._presence.update(
                {
                    "track_id": str(track_id or ""),
                    "track_name": str(track_name or ""),
                    "editing_cue_id": str(editing_cue_id or ""),
                    "editing_column": str(editing_column or ""),
                    "position_seconds": float(position_seconds or 0.0),
                }
            )
            if display_name is not None:
                self._presence["display_name"] = str(display_name)

    def submit_operation(self, op: dict) -> dict:
        """Submit one document operation and wait for its answer.

        Runs on the caller's thread rather than the heartbeat thread — a cue
        edit needs a yes or no now, and queueing it behind the beat would add
        most of a beat of latency for nothing. The reply is returned to the
        caller and touches no shared state, which is why no generation guard
        is needed here: a reply from a connection that has since been
        replaced can mislead nobody but the caller that made it.
        """
        with self._lock:
            if self._state != "joined" or not self._token:
                return {
                    "ok": False,
                    "error": "Chưa kết nối tới máy chính.",
                    "reason": "offline",
                }
            base, machine_id, token = self._base, self._machine_id, self._token
            session_id, document_id = self._session_id, self._document_id

        body = dict(op)
        # Filled in for the caller, who knows what it wants to change but not
        # which hosting run it is talking to. setdefault, so a test may pin a
        # stale id on purpose and watch it be refused.
        body.setdefault("session_id", session_id)
        body.setdefault("document_id", document_id)
        # The version of the document this machine has actually applied — not
        # the newest one the host mentioned. That is what makes the staleness
        # check on the far side mean anything.
        with self._lock:
            body.setdefault("base_revision", self._acked_revision)
        try:
            return _post(
                base,
                PATH_SUBMIT_OP,
                {"machine_id": machine_id, "token": token, "op": body},
            )
        except SyncError as exc:
            return {"ok": False, "error": str(exc), "reason": "network"}

    # -- the thread -------------------------------------------------------

    def _run(
        self,
        base: str,
        code: str,
        machine_id: str,
        stop: threading.Event,
        previous: threading.Thread | None,
    ) -> None:
        # Retire a previous connection off the GUI thread before joining a
        # new one. Its farewell must not remove a newly established session.
        while previous is not None and previous.is_alive():
            previous.join(timeout=0.05)
        if stop.is_set():
            with self._lock:
                self._notify.pop(stop, None)
            return
        key = (base, code, machine_id)
        with self._lock:
            token = self._credentials.setdefault(key, secrets.token_urlsafe(32))
        joined = False
        try:
            while not stop.is_set():
                try:
                    with self._lock:
                        presence = dict(self._presence)
                        acked_revision = self._acked_revision
                    if not joined:
                        reply = _post(
                            base,
                            PATH_JOIN,
                            {
                                "protocol": PROTOCOL_VERSION,
                                "machine_id": machine_id,
                                "display_name": presence.get("display_name", "Máy này"),
                                "code": code,
                                "token": token,
                            },
                        )
                        if not reply.get("ok"):
                            self._fail(
                                str(reply.get("error") or "Máy chính từ chối."), stop, "refused"
                            )
                            return
                        token = str(reply.get("token", "") or "")
                        if reply.get("protocol") != PROTOCOL_VERSION or not token:
                            self._fail(
                                "Hai máy cần dùng cùng phiên bản Tracyy có hỗ trợ Sync mới.",
                                stop,
                                "refused",
                            )
                            return
                        with self._lock:
                            self._credentials[key] = token
                            if self._stop is stop and not stop.is_set():
                                self._token = token
                        joined = True
                    else:
                        reply = _post(
                            base,
                            PATH_HEARTBEAT,
                            {
                                **presence,
                                "machine_id": machine_id,
                                "token": token,
                                "acked_revision": acked_revision,
                            },
                        )
                        if reply.get("rejoin"):
                            joined = False
                            raise SyncError(
                                "Đang xác nhận lại kết nối với máy chính. Cue đang bị khoá."
                            )
                        if not reply.get("ok"):
                            raise SyncError(
                                str(reply.get("error") or "Máy chính chưa xác nhận kết nối.")
                            )
                    self._apply(reply, stop, machine_id)
                except (SyncError, TypeError, ValueError, KeyError) as exc:
                    self._fail(
                        str(exc) or "Phản hồi của máy chính không hợp lệ.", stop, "disconnected"
                    )
                    joined = False
                stop.wait(HEARTBEAT_SECONDS)
        finally:
            with self._lock:
                notify = self._notify.pop(stop, False)
            if notify and token:
                try:
                    _post(
                        base,
                        PATH_LEAVE,
                        {"machine_id": machine_id, "token": token},
                        timeout=1.0,
                    )
                except SyncError:
                    pass

    def _apply(self, reply: dict, stop: threading.Event, machine_id: str) -> None:
        you, peers = reply.get("you"), reply.get("peers")
        if (
            not isinstance(you, dict)
            or you.get("machine_id") != machine_id
            or not isinstance(peers, list)
            or any(not isinstance(p, dict) for p in peers)
        ):
            raise SyncError("Máy chính trả về danh sách người kết nối không hợp lệ.")
        local = [peer for peer in peers if peer.get("machine_id") == machine_id]
        if len(local) != 1 or local[0].get("role") != you.get("role"):
            raise SyncError("Máy chính chưa xác nhận quyền của máy này. Cue đang bị khoá.")
        with self._lock:
            if self._stop is not stop or stop.is_set():
                return
            self._state = "joined"
            self._error = ""
            self._last_reply = time.monotonic()
            self._session_name = str(reply.get("session_name", "") or "")
            self._session_id = str(reply.get("session_id", "") or "")
            self._document_id = str(reply.get("document_id", "") or "")
            self._show_id = str(reply.get("show_id", "") or "")
            self._you = dict(you)
            self._peers = [dict(peer) for peer in peers]
            # Queue what arrived. The revision is deliberately NOT taken
            # from the reply: acknowledging a revision this machine has not
            # applied would tell the host to stop sending the very records
            # that are missing. Only confirm_applied moves it, and only after
            # the cue table really holds them.
            for record in reply.get("ops") or ():
                if not isinstance(record, dict):
                    continue
                try:
                    revision = int(record.get("revision"))
                except (TypeError, ValueError):
                    continue
                if revision <= self._acked_revision:
                    continue
                if any(int(held["revision"]) == revision for held in self._inbox):
                    # A resend of something already waiting. The host repeats
                    # anything unacknowledged every beat, so this is the
                    # normal case, not an error.
                    continue
                self._inbox.append(dict(record))
            self._inbox.sort(key=lambda record: int(record["revision"]))

    def _fail(self, message: str, stop: threading.Event, state: str) -> None:
        with self._lock:
            if self._stop is not stop or stop.is_set():
                return
            self._state = state
            self._error = message
            self._peers = []

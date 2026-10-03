"""The document a Tracyy Live session agrees on, and the log that proves it.

One host holds one :class:`SyncDocument`: the cue state every machine in the
session is converging on, a monotonic revision counter, and an append-only
journal on disk. Every change arrives as an :class:`Operation` and either
commits — journal line written and fsynced, revision assigned — or is refused
with a reason. There is no third outcome: an ACK is only ever sent after the
bytes are on disk, so a crash between "accepted" and "durable" cannot exist.

Deliberately free of Qt and of HTTP. The server glues this to the wire and
holds the lock; the tests drive it directly. Callers serialise access —
:class:`SyncDocument` itself does not lock, because the one writer that exists
(:class:`~tracyy.sync.server.LiveServer`) already does.

Identity note: ``document_id`` here is minted by the host per hosting run and
shared with every client. It is *not* the client-local ``show_id`` that names
a :class:`~tracyy.sync.store.ShowStore` cache folder — two machines never
agree on that one. Reconciling the two is a reconnect/catch-up concern for a
later phase, not this module's.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .model import Capability

__all__ = [
    "CommitResult",
    "MutationOrigin",
    "Operation",
    "OperationKind",
    "SyncDocument",
    "capability_for",
]

log = logging.getLogger(__name__)

#: Journal record layout version, stamped on every record rather than in a
#: header line — a header would be one more line that can be the torn one.
#: Replay skips records with a version it does not know instead of crashing.
SCHEMA_VERSION = 1

#: How many committed operations one reply may carry. A client far behind
#: catches up over several heartbeats instead of receiving one giant reply
#: that stalls the handler thread; full resync belongs to a later phase.
MAX_PENDING_BATCH = 200

#: How many ``op_id``s are remembered for dedup. Retries after a lost ACK
#: arrive within seconds, so anything this deep is not a retry any more.
#: Known, accepted limit: a client whose partition outlasts this many other
#: commits before it retries would get a duplicate — stated rather than
#: engineered away, because engineering it away means unbounded memory.
MAX_SEEN_OPS = 2000

#: Free-text fields arrive from another machine; a bound keeps one hostile or
#: broken client from growing the journal a megabyte per keystroke.
_MAX_TEXT = 200

_TEXT_FIELDS = ("name", "label", "type", "color")


class OperationKind(str, Enum):
    """What one operation does to the document. Only cues, for now."""

    ADD_CUE = "add_cue"
    DELETE_CUE = "delete_cue"
    MOVE_CUE = "move_cue"
    UPDATE_CUE_FIELDS = "update_cue_fields"


class MutationOrigin(str, Enum):
    """Why a mutation is happening. Defined now, routed later.

    The next phase funnels every cue write — a local click, an undo, an
    operation from another machine — through one entrypoint, and that
    entrypoint must know which of the three it is handling: a local edit
    becomes an operation to submit, an undo becomes an inverse operation
    checked against revision, and a remote apply must never be re-submitted.
    Today's controllers do not use this yet; it exists so the seam has a name
    before anybody wires two of the three through different doors.
    """

    LOCAL_UI = "local_ui"
    UNDO = "undo"
    REMOTE_APPLY = "remote_apply"


def capability_for(kind: OperationKind | str) -> Capability:
    """Which capability a role needs to submit ``kind``.

    A table rather than a constant so playlist and media operations have a
    place to differ when they arrive; today every kind is a cue edit.
    """
    resolved = OperationKind(kind)
    return {
        OperationKind.ADD_CUE: Capability.EDIT_CUES,
        OperationKind.DELETE_CUE: Capability.EDIT_CUES,
        OperationKind.MOVE_CUE: Capability.EDIT_CUES,
        OperationKind.UPDATE_CUE_FIELDS: Capability.EDIT_CUES,
    }[resolved]


@dataclass(frozen=True)
class Operation:
    """One proposed change, exactly as a client submits it.

    Deliberately has no author field. The server stamps the author from the
    credentials it verified; a field the client fills in would be a field the
    client can lie in, and every reader after that would have to remember not
    to trust it.
    """

    op_id: str
    session_id: str
    document_id: str
    base_revision: int
    track_id: str
    cue_id: str
    kind: OperationKind
    payload: dict = field(default_factory=dict)

    def to_wire(self) -> dict:
        return {
            "op_id": self.op_id,
            "session_id": self.session_id,
            "document_id": self.document_id,
            "base_revision": int(self.base_revision),
            "track_id": self.track_id,
            "cue_id": self.cue_id,
            "kind": self.kind.value,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_wire(cls, payload: dict) -> Operation:
        """Rebuild an operation from the wire, or raise ``ValueError`` saying why.

        Everything here arrives from another machine. Anything missing or of
        the wrong shape is refused before it can reach the document — a
        malformed packet is a normal event to report, not state to guess at.
        """
        if not isinstance(payload, dict):
            raise ValueError("Thao tác gửi lên không đúng định dạng.")

        try:
            kind = OperationKind(str(payload.get("kind", "")))
        except ValueError:
            raise ValueError("Loại thao tác không được hỗ trợ.") from None

        try:
            base_revision = int(payload.get("base_revision"))
        except (TypeError, ValueError):
            raise ValueError("Thao tác thiếu revision gốc.") from None
        if base_revision < 0:
            raise ValueError("Revision gốc không hợp lệ.")

        fields = {}
        for name in ("op_id", "session_id", "document_id", "track_id", "cue_id"):
            value = payload.get(name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError("Thao tác gửi lên thiếu hoặc sai trường bắt buộc.")
            fields[name] = value.strip()

        op_payload = payload.get("payload", {})
        if not isinstance(op_payload, dict):
            raise ValueError("Nội dung thao tác không đúng định dạng.")

        return cls(base_revision=base_revision, kind=kind, payload=dict(op_payload), **fields)


@dataclass(frozen=True)
class CommitResult:
    """What became of one submitted operation.

    Plain data rather than the HTTP reply dict, so the document can be tested
    with asserts and the server owns the translation to the wire.
    """

    ok: bool
    revision: int = 0
    error: str = ""
    #: Machine-readable cause on refusal: ``invalid``, ``stale_revision`` or
    #: ``journal``. Empty on success. The server adds its own reasons —
    #: ``forbidden``, ``wrong_session`` — for the checks it performs itself.
    reason: str = ""
    record: dict | None = None


class SyncDocument:
    """The authoritative cue state for one hosted session.

    Commit order inside :meth:`submit` is the contract everything else leans
    on: dedup first (a retry of a lost ACK must succeed even though the world
    moved on), then validation, then the journal write, and only after a
    successful fsync does any in-memory state change. A journal failure
    therefore leaves the document exactly as it was — no revision consumed,
    no cue half-added, no ACK.
    """

    def __init__(self, session_id: str, document_id: str, journal_path: str | Path) -> None:
        self.session_id = str(session_id)
        self.document_id = str(document_id)
        self._journal_path = Path(journal_path)
        self._revision = 0
        #: track_id -> cue_id -> cue fields. Cue ids arrive already minted by
        #: whichever machine authored the add; the document only checks them
        #: for collisions, it never re-mints.
        self._cues: dict[str, dict[str, dict]] = {}
        #: (track_id, cue_id) -> revision that last changed that cue. This is
        #: what makes staleness per-cue: an old ``base_revision`` only blocks
        #: an operation whose target somebody else has since changed, not
        #: every operation in the room.
        self._last_touched: dict[tuple[str, str], int] = {}
        #: (track_id, cue_id) -> who made that last change. Staleness is about
        #: somebody else having moved on without you; a machine cannot be
        #: behind its own edit, and refusing one would mean a person who adds
        #: a cue has to wait for it to come back before they may name it.
        self._last_touched_author: dict[tuple[str, str], str] = {}
        #: op_id -> the CommitResult its first submission produced. Insertion
        #: order doubles as commit order, which is what the eviction walks.
        self._seen_ops: dict[str, CommitResult] = {}
        #: Every committed record in revision order. Kept separately from the
        #: dedup table, which is capped: a client far behind must still be
        #: able to catch up, while a retry older than the cap may not.
        self._committed: list[dict] = []
        self._replay()

    # -- what the server reads -------------------------------------------

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def journal_path(self) -> Path:
        return self._journal_path

    def cues(self, track_id: str) -> list[dict]:
        """This track's cues in time order, copied so callers cannot write back."""
        held = self._cues.get(str(track_id), {})
        return sorted((dict(cue) for cue in held.values()), key=lambda cue: cue["seconds"])

    def pending_since(self, revision: int) -> list[dict]:
        """Committed records newer than ``revision``, oldest first, bounded.

        Answered from memory rather than re-read from disk: the heartbeat
        calls this several times a second and the journal is the durability
        record, not a query index.
        """
        try:
            floor = max(0, int(revision))
        except (TypeError, ValueError):
            floor = 0
        newer = [record for record in self._committed if record["revision"] > floor]
        return [dict(record) for record in newer[:MAX_PENDING_BATCH]]

    def seed(self, tracks: dict[str, list[dict]]) -> None:
        """Load the show that existed before anybody joined.

        Nothing is journalled and no revision is spent: this is not a change
        somebody made, it is the ground the session starts from. The project
        file is what makes it durable, and re-hosting the same project seeds
        the same state again.

        Without this the document begins empty while the host's window is full
        of cues, and every attempt to edit one of them is refused — correctly,
        and incomprehensibly — with "that cue does not exist".
        """
        if self._revision:
            # Changes have already been committed against this document;
            # replacing the ground under them would rewrite history.
            return
        self._cues = {}
        for track_id, cues in (tracks or {}).items():
            track_key = str(track_id)
            if not track_key:
                continue
            held = self._cues.setdefault(track_key, {})
            for cue in cues or ():
                cue_id = str(cue.get("id") or "")
                if not cue_id:
                    continue
                try:
                    seconds = float(cue.get("seconds") or 0.0)
                except (TypeError, ValueError):
                    continue
                held[cue_id] = {
                    "id": cue_id,
                    "seconds": seconds,
                    **{
                        name: str(cue.get(name, "") or "")[:_MAX_TEXT]
                        for name in _TEXT_FIELDS
                    },
                }

    def snapshot(self) -> dict:
        """The whole document, for a machine that has none of it yet.

        A machine joining an hour into a rehearsal cannot be caught up by
        replaying the journal: most of the show was there before the session
        started, and what was not may be thousands of operations. It is handed
        the state and the revision that state is at, and carries on from
        there.
        """
        return {
            "revision": self._revision,
            "tracks": {
                track_id: [dict(cue) for cue in self.cues(track_id)]
                for track_id in self._cues
            },
        }

    # -- committing ------------------------------------------------------

    def submit(self, op: Operation, author: str) -> CommitResult:
        """Commit one operation, or refuse it with a reason.

        Dedup is checked before anything else on purpose: a client that lost
        the ACK retries the same ``op_id`` seconds later, by which time other
        commits may have made its ``base_revision`` stale. The retry must get
        the original answer back, not a fresh rejection for a change that is
        already in the document.
        """
        seen = self._seen_ops.get(op.op_id)
        if seen is not None:
            return seen

        error = self._validate(op, author)
        if error is not None:
            return error

        record = op.to_wire()
        record.update(
            {
                "schema": SCHEMA_VERSION,
                "revision": self._revision + 1,
                "author": str(author),
                # Wall clock, for a person reading the journal later. Ordering
                # is the revision; clocks on a show LAN agree about nothing.
                "committed_at": time.time(),
            }
        )

        try:
            self._append(record)
        except OSError as exc:
            log.warning("journal write failed: %s", exc)
            return CommitResult(
                ok=False,
                error="Máy chính không ghi được nhật ký thao tác. Thao tác chưa được nhận.",
                reason="journal",
            )

        self._apply(record)
        self._revision = record["revision"]
        self._committed.append(record)
        result = CommitResult(ok=True, revision=self._revision, record=record)
        self._remember(op.op_id, result)
        return result

    # -- internals -------------------------------------------------------

    def _validate(self, op: Operation, author: str) -> CommitResult | None:
        """Why this operation cannot commit, or None when it can."""
        if op.session_id != self.session_id or op.document_id != self.document_id:
            return CommitResult(
                ok=False, error="Thao tác thuộc phiên khác.", reason="wrong_session"
            )
        if op.base_revision > self._revision:
            return CommitResult(
                ok=False,
                error="Thao tác tham chiếu revision chưa tồn tại.",
                reason="invalid",
            )

        # Stale before existence: an edit racing a delete should hear "somebody
        # changed this cue after the version you saw", not "no such cue".
        key = (op.track_id, op.cue_id)
        touched = self._last_touched.get(key, 0)
        if op.base_revision < touched and self._last_touched_author.get(key, "") != author:
            return CommitResult(
                ok=False,
                error="Cue này đã được người khác sửa sau bản bạn đang xem.",
                reason="stale_revision",
            )

        exists = op.cue_id in self._cues.get(op.track_id, {})
        problem = ""
        if op.kind is OperationKind.ADD_CUE:
            if exists:
                problem = "Cue này đã tồn tại trên máy chính."
            else:
                problem = _check_seconds(op.payload) or _check_text_fields(
                    op.payload, required=False
                )
        elif not exists:
            problem = "Cue này không còn tồn tại trên máy chính."
        elif op.kind is OperationKind.MOVE_CUE:
            problem = _check_seconds(op.payload)
        elif op.kind is OperationKind.UPDATE_CUE_FIELDS:
            problem = _check_text_fields(op.payload, required=True)

        if problem:
            return CommitResult(ok=False, error=problem, reason="invalid")
        return None

    def _apply(self, record: dict) -> None:
        """Apply an already-validated record to the in-memory state."""
        track_id, cue_id = str(record["track_id"]), str(record["cue_id"])
        kind = OperationKind(record["kind"])
        payload = record.get("payload", {})
        cues = self._cues.setdefault(track_id, {})

        if kind is OperationKind.ADD_CUE:
            cues[cue_id] = {
                "id": cue_id,
                "seconds": float(payload["seconds"]),
                **{name: str(payload.get(name, "") or "") for name in _TEXT_FIELDS},
            }
        elif kind is OperationKind.DELETE_CUE:
            cues.pop(cue_id, None)
        elif kind is OperationKind.MOVE_CUE:
            cues[cue_id]["seconds"] = float(payload["seconds"])
        elif kind is OperationKind.UPDATE_CUE_FIELDS:
            cues[cue_id].update(
                {name: str(payload[name]) for name in _TEXT_FIELDS if name in payload}
            )

        self._last_touched[(track_id, cue_id)] = int(record["revision"])
        self._last_touched_author[(track_id, cue_id)] = str(record.get("author", ""))

    def _append(self, record: dict) -> None:
        """One journal line, on disk before anyone hears about it.

        Flushed and fsynced per commit. Cue edits happen at human speed; the
        cost of an fsync is nothing next to the cost of an ACK for bytes that
        a power cut then proves were never written.
        """
        self._journal_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._journal_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _remember(self, op_id: str, result: CommitResult) -> None:
        self._seen_ops[op_id] = result
        while len(self._seen_ops) > MAX_SEEN_OPS:
            self._seen_ops.pop(next(iter(self._seen_ops)))

    def _replay(self) -> None:
        """Rebuild state from the journal, skipping what a crash tore.

        Only the last line can be torn — everything before it was fsynced —
        so a line that fails to parse is logged and dropped, never raised: a
        host that refuses to start because it crashed last time is a host
        that stays down for the whole show.
        """
        try:
            raw = self._journal_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            log.warning("journal unreadable, starting empty: %s", exc)
            return

        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                log.warning("skipping torn journal line")
                continue
            if not isinstance(record, dict) or record.get("schema") != SCHEMA_VERSION:
                log.warning("skipping journal record with unknown schema")
                continue
            try:
                revision = int(record["revision"])
                self._apply(record)
            except (KeyError, TypeError, ValueError):
                log.warning("skipping malformed journal record")
                continue
            self._revision = max(self._revision, revision)
            self._committed.append(record)
            self._remember(
                str(record.get("op_id", "")),
                CommitResult(ok=True, revision=revision, record=record),
            )


def _check_seconds(payload: dict) -> str:
    """Why ``payload['seconds']`` is unusable, or empty when it is fine."""
    value = payload.get("seconds")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "Thời gian cue không hợp lệ."
    if not math.isfinite(float(value)) or float(value) < 0:
        return "Thời gian cue không hợp lệ."
    return ""


def _check_text_fields(payload: dict, required: bool) -> str:
    """Why the text fields are unusable, or empty when they are fine."""
    present = [name for name in _TEXT_FIELDS if name in payload]
    if required and not present:
        return "Thao tác không thay đổi trường nào."
    unknown = set(payload) - set(_TEXT_FIELDS) - {"seconds"}
    if unknown:
        return "Thao tác chứa trường không được hỗ trợ."
    for name in present:
        value = payload[name]
        if not isinstance(value, str) or len(value) > _MAX_TEXT:
            return "Nội dung cue quá dài hoặc sai định dạng."
    return ""

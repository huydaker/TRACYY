"""The document commit pipeline, driven directly.

No Qt, no sockets, no server: these tests hold the same object the server
holds and prove the contract everything above it leans on — a duplicate never
commits twice, a refusal names its reason, and nothing is acknowledged that
the journal did not keep.
"""

from __future__ import annotations

import json

import pytest

from tracyy.sync import document as document_module
from tracyy.sync.document import (
    Operation,
    OperationKind,
    SyncDocument,
    capability_for,
)
from tracyy.sync.model import Capability


def _doc(tmp_path) -> SyncDocument:
    return SyncDocument("phien-1", "tailieu-1", tmp_path / "journal.ndjson")


def _add(cue_id: str, op_id: str = "", base: int = 0, seconds: float = 10.0) -> Operation:
    return Operation(
        op_id=op_id or f"op-add-{cue_id}",
        session_id="phien-1",
        document_id="tailieu-1",
        base_revision=base,
        track_id="t1",
        cue_id=cue_id,
        kind=OperationKind.ADD_CUE,
        payload={"seconds": seconds, "name": f"Cue {cue_id}"},
    )


def _move(cue_id: str, op_id: str, base: int, seconds: float) -> Operation:
    return Operation(
        op_id=op_id,
        session_id="phien-1",
        document_id="tailieu-1",
        base_revision=base,
        track_id="t1",
        cue_id=cue_id,
        kind=OperationKind.MOVE_CUE,
        payload={"seconds": seconds},
    )


# -- committing ---------------------------------------------------------


def test_a_cue_commits_and_lands_in_the_journal(tmp_path) -> None:
    doc = _doc(tmp_path)
    result = doc.submit(_add("c1", seconds=12.5), author="laptop-2")

    assert result.ok and result.revision == 1
    assert doc.revision == 1
    assert [cue["id"] for cue in doc.cues("t1")] == ["c1"]

    lines = (tmp_path / "journal.ndjson").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["revision"] == 1
    assert record["author"] == "laptop-2"


def test_the_author_is_stamped_by_the_server_not_the_wire(tmp_path) -> None:
    """The envelope has no author field at all; whoever authenticated is the
    author, and a client cannot claim to be somebody else."""
    wire = _add("c1").to_wire()
    assert "author" not in wire
    assert "actor_id" not in wire

    doc = _doc(tmp_path)
    result = doc.submit(Operation.from_wire(wire), author="may-da-xac-thuc")
    assert result.record["author"] == "may-da-xac-thuc"


def test_a_duplicate_op_id_does_not_create_a_second_cue(tmp_path) -> None:
    """The task's first acceptance bar: a retried submission after a lost ACK
    must return the original answer, not commit again."""
    doc = _doc(tmp_path)
    first = doc.submit(_add("c1"), author="laptop-2")
    second = doc.submit(_add("c1"), author="laptop-2")

    assert second.ok and second.revision == first.revision
    assert doc.revision == 1
    assert len(doc.cues("t1")) == 1
    lines = (tmp_path / "journal.ndjson").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1


def test_a_retry_still_succeeds_after_the_world_moved_on(tmp_path) -> None:
    """Dedup runs before the staleness check on purpose: by the time a lost
    ACK is retried, other commits may have made its base_revision old, and
    the retry must not be refused for a change already in the document."""
    doc = _doc(tmp_path)
    original = doc.submit(_add("c1", op_id="op-mat-ack"), author="laptop-2")
    doc.submit(_add("c2", base=1), author="laptop-3")
    doc.submit(_add("c3", base=2), author="laptop-3")

    retry = doc.submit(_add("c1", op_id="op-mat-ack"), author="laptop-2")
    assert retry.ok and retry.revision == original.revision
    assert doc.revision == 3


# -- refusals, each with its reason ------------------------------------


def test_an_op_for_another_session_is_refused(tmp_path) -> None:
    doc = _doc(tmp_path)
    stray = Operation(
        op_id="op-la",
        session_id="phien-cu",
        document_id="tailieu-1",
        base_revision=0,
        track_id="t1",
        cue_id="c1",
        kind=OperationKind.ADD_CUE,
        payload={"seconds": 1.0},
    )
    result = doc.submit(stray, author="laptop-2")
    assert not result.ok and result.reason == "wrong_session"
    assert doc.revision == 0


def test_a_stale_edit_to_a_touched_cue_is_refused(tmp_path) -> None:
    """Staleness is per cue: an old base_revision only blocks an operation
    whose target somebody else changed since, not everything in the room."""
    doc = _doc(tmp_path)
    doc.submit(_add("c1"), author="laptop-2")
    doc.submit(_move("c1", "op-doi-1", base=1, seconds=20.0), author="laptop-3")

    stale = doc.submit(_move("c1", "op-doi-cu", base=1, seconds=5.0), author="laptop-2")
    assert not stale.ok and stale.reason == "stale_revision"
    assert doc.cues("t1")[0]["seconds"] == 20.0

    # Same old base_revision, untouched cue: allowed through.
    fresh_target = doc.submit(_add("c2", base=1), author="laptop-2")
    assert fresh_target.ok


def test_an_edit_racing_a_delete_hears_stale_not_missing(tmp_path) -> None:
    doc = _doc(tmp_path)
    doc.submit(_add("c1"), author="laptop-2")
    delete = Operation(
        op_id="op-xoa",
        session_id="phien-1",
        document_id="tailieu-1",
        base_revision=1,
        track_id="t1",
        cue_id="c1",
        kind=OperationKind.DELETE_CUE,
    )
    doc.submit(delete, author="laptop-3")

    late = doc.submit(_move("c1", "op-tre", base=1, seconds=9.0), author="laptop-2")
    assert not late.ok and late.reason == "stale_revision"


@pytest.mark.parametrize(
    "payload",
    [
        {"seconds": "not a number"},
        {"seconds": float("nan")},
        {"seconds": -3.0},
        {"seconds": True},
    ],
)
def test_a_bad_time_is_refused(tmp_path, payload) -> None:
    doc = _doc(tmp_path)
    op = Operation(
        op_id="op-hong",
        session_id="phien-1",
        document_id="tailieu-1",
        base_revision=0,
        track_id="t1",
        cue_id="c1",
        kind=OperationKind.ADD_CUE,
        payload=payload,
    )
    result = doc.submit(op, author="laptop-2")
    assert not result.ok and result.reason == "invalid"
    assert doc.revision == 0


def test_an_update_must_change_something_it_knows(tmp_path) -> None:
    doc = _doc(tmp_path)
    doc.submit(_add("c1"), author="laptop-2")

    empty = Operation(
        op_id="op-rong",
        session_id="phien-1",
        document_id="tailieu-1",
        base_revision=1,
        track_id="t1",
        cue_id="c1",
        kind=OperationKind.UPDATE_CUE_FIELDS,
        payload={},
    )
    assert doc.submit(empty, author="laptop-2").reason == "invalid"

    unknown = Operation(
        op_id="op-la",
        session_id="phien-1",
        document_id="tailieu-1",
        base_revision=1,
        track_id="t1",
        cue_id="c1",
        kind=OperationKind.UPDATE_CUE_FIELDS,
        payload={"volume": "11"},
    )
    assert doc.submit(unknown, author="laptop-2").reason == "invalid"


@pytest.mark.parametrize(
    "broken",
    [
        {},
        {"kind": "add_cue"},
        {"kind": "launch_pyro", "op_id": "x", "session_id": "s", "document_id": "d",
         "base_revision": 0, "track_id": "t", "cue_id": "c"},
        {"kind": "add_cue", "op_id": "", "session_id": "s", "document_id": "d",
         "base_revision": 0, "track_id": "t", "cue_id": "c"},
        {"kind": "add_cue", "op_id": "x", "session_id": "s", "document_id": "d",
         "base_revision": "soon", "track_id": "t", "cue_id": "c"},
    ],
)
def test_a_malformed_wire_operation_is_refused_before_the_document(broken) -> None:
    with pytest.raises(ValueError):
        Operation.from_wire(broken)


# -- durability ---------------------------------------------------------


def test_a_journal_failure_produces_no_ack_and_no_state_change(tmp_path) -> None:
    """The third acceptance bar. A directory where the journal file should be
    makes every append fail the way a full or read-only disk would."""
    journal = tmp_path / "journal.ndjson"
    journal.mkdir()
    doc = SyncDocument("phien-1", "tailieu-1", journal)

    result = doc.submit(_add("c1"), author="laptop-2")
    assert not result.ok and result.reason == "journal"
    assert doc.revision == 0
    assert doc.cues("t1") == []
    assert doc.pending_since(0) == []

    # And the failed attempt burned nothing: the same op commits once the
    # journal can be written again.
    doc2 = SyncDocument("phien-1", "tailieu-1", tmp_path / "ok.ndjson")
    assert doc2.submit(_add("c1"), author="laptop-2").ok


def test_replay_keeps_everything_before_a_torn_last_line(tmp_path) -> None:
    journal = tmp_path / "journal.ndjson"
    doc = _doc(tmp_path)
    doc.submit(_add("c1", seconds=5.0), author="laptop-2")
    doc.submit(_add("c2", base=1, seconds=8.0), author="laptop-2")

    with open(journal, "a", encoding="utf-8") as handle:
        handle.write('{"schema": 1, "revision": 3, "kind": "add_cu')  # torn mid-write

    revived = SyncDocument("phien-1", "tailieu-1", journal)
    assert revived.revision == 2
    assert [cue["id"] for cue in revived.cues("t1")] == ["c1", "c2"]
    # The dedup memory survives the restart too: the same retry after a host
    # crash must not double the cue.
    again = revived.submit(_add("c1", seconds=5.0), author="laptop-2")
    assert again.ok and again.revision == 1
    assert revived.revision == 2


# -- ordering and catch-up ---------------------------------------------


def test_two_authors_interleave_by_revision(tmp_path) -> None:
    doc = _doc(tmp_path)
    doc.submit(_add("a1"), author="laptop-2")
    doc.submit(_add("b1", base=0), author="laptop-3")
    doc.submit(_add("a2", base=2), author="laptop-2")

    records = doc.pending_since(0)
    assert [record["revision"] for record in records] == [1, 2, 3]
    assert [record["author"] for record in records] == ["laptop-2", "laptop-3", "laptop-2"]
    assert len(doc.cues("t1")) == 3


def test_pending_since_returns_only_newer_ops_and_is_bounded(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(document_module, "MAX_PENDING_BATCH", 3)
    doc = _doc(tmp_path)
    for index in range(6):
        doc.submit(_add(f"c{index}", base=index), author="laptop-2")

    batch = doc.pending_since(2)
    assert [record["revision"] for record in batch] == [3, 4, 5]
    assert doc.pending_since(6) == []


def test_the_dedup_table_is_bounded_and_says_so(tmp_path, monkeypatch) -> None:
    """The cap is a stated trade, not an accident: an op_id evicted from the
    table is no longer recognised as a retry, and this test pins both the
    bound and that consequence so neither changes silently."""
    monkeypatch.setattr(document_module, "MAX_SEEN_OPS", 4)
    doc = _doc(tmp_path)
    for index in range(6):
        doc.submit(_add(f"c{index}", base=index), author="laptop-2")

    assert len(doc._seen_ops) == 4

    evicted_retry = doc.submit(_add("c0", op_id="op-add-c0", base=6), author="laptop-2")
    assert not evicted_retry.ok  # treated as new, refused as an existing cue
    assert evicted_retry.reason == "invalid"
    assert doc.revision == 6


# -- the role table hook ------------------------------------------------


def test_every_cue_operation_needs_the_cue_capability() -> None:
    for kind in OperationKind:
        assert capability_for(kind) is Capability.EDIT_CUES


def test_you_are_never_stale_against_your_own_edit(tmp_path) -> None:
    """Add a cue, then name it, without waiting to hear your own commit back.

    The staleness rule is about somebody else having moved on without you. A
    machine cannot be behind its own change, and refusing this would mean the
    person who adds a cue has to wait for it to travel to the host and back
    before they are allowed to type a name into it.
    """
    document = SyncDocument("s-1", "d-1", tmp_path / "journal.ndjson")
    first = document.submit(
        Operation(
            op_id="op-1", session_id="s-1", document_id="d-1", base_revision=0,
            track_id="t-1", cue_id="c-1", kind=OperationKind.ADD_CUE,
            payload={"seconds": 4.0, "name": "Cue 01"},
        ),
        author="laptop-1",
    )
    assert first.ok and first.revision == 1

    same_machine = document.submit(
        Operation(
            op_id="op-2", session_id="s-1", document_id="d-1", base_revision=0,
            track_id="t-1", cue_id="c-1", kind=OperationKind.UPDATE_CUE_FIELDS,
            payload={"label": "Khói vào"},
        ),
        author="laptop-1",
    )
    assert same_machine.ok, same_machine.error

    # Somebody else on the same stale base is still refused.
    other_machine = document.submit(
        Operation(
            op_id="op-3", session_id="s-1", document_id="d-1", base_revision=0,
            track_id="t-1", cue_id="c-1", kind=OperationKind.UPDATE_CUE_FIELDS,
            payload={"label": "Khác"},
        ),
        author="laptop-2",
    )
    assert not other_machine.ok
    assert other_machine.reason == "stale_revision"

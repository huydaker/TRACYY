"""Submitting document operations across a real socket.

The real HTTP server and the real client thread, the way test_sync_network
does it: the checks that matter here — a viewer refused by the server rather
than by a greyed-out button, a heartbeat carrying what a lagging peer missed,
a stale hosting run's identity refused — only exist at the boundary.
"""

from __future__ import annotations

import socket
import time

import pytest

from tracyy.sync.client import LiveClient
from tracyy.sync.protocol import PROTOCOL_VERSION
from tracyy.sync.server import LiveServer


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait(check, timeout: float = 8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.05)
    return None


def _start_host(tmp_path, name: str = "Show Thử") -> tuple[LiveServer, str]:
    server = LiveServer()
    port = _free_port()
    server.start(
        "127.0.0.1",
        port,
        name,
        "ABCD23",
        "host-machine",
        "Chủ Phiên",
        journal_path=tmp_path / f"journal-{port}.ndjson",
    )
    return server, f"127.0.0.1:{port}"


@pytest.fixture
def host(tmp_path):
    server, address = _start_host(tmp_path)
    try:
        yield server, address
    finally:
        server.stop()


@pytest.fixture
def client():
    live = LiveClient()
    try:
        yield live
    finally:
        live.stop(notify=False)


def _editor_joined(server, client, address, machine_id="laptop-2", name="Hùng") -> None:
    client.start(address, "ABCD23", machine_id, name)
    assert _wait(lambda: client.snapshot()["state"] == "joined"), client.snapshot()
    assert server.set_role(machine_id, "editor")
    assert _wait(lambda: client.snapshot()["you"].get("role") == "editor")


def _add_cue_op(op_id: str, cue_id: str, base: int = 0, seconds: float = 12.5) -> dict:
    return {
        "op_id": op_id,
        "base_revision": base,
        "track_id": "t1",
        "cue_id": cue_id,
        "kind": "add_cue",
        "payload": {"seconds": seconds, "name": "Intro"},
    }


# -- the round trip ----------------------------------------------------


def test_a_submitted_cue_round_trips_with_a_revision(host, client) -> None:
    server, address = host
    _editor_joined(server, client, address)

    reply = client.submit_operation(_add_cue_op("op-1", "c1"))
    assert reply.get("ok"), reply
    assert reply["revision"] == 1
    assert reply["record"]["author"] == "laptop-2"

    cues = server._document.cues("t1")
    assert [cue["id"] for cue in cues] == ["c1"]
    assert server._document.pending_since(0)[0]["op_id"] == "op-1"


def test_submitting_before_joining_stays_off_the_network(client) -> None:
    reply = client.submit_operation(_add_cue_op("op-1", "c1"))
    assert not reply.get("ok")
    assert reply.get("reason") == "offline"


# -- authority lives on the server -------------------------------------


def test_a_viewer_is_refused_by_the_server_not_the_button(host, client) -> None:
    """The client-side capability guard is UX; this is the check that holds
    when a modified client skips it."""
    server, address = host
    client.start(address, "ABCD23", "laptop-2", "Hùng")
    assert _wait(lambda: client.snapshot()["state"] == "joined")

    reply = client.submit_operation(_add_cue_op("op-1", "c1"))
    assert not reply.get("ok")
    assert reply.get("reason") == "forbidden"
    assert server._document.revision == 0
    assert not server._document.journal_path.exists()


def test_a_switched_client_writes_with_the_new_hosts_grant_not_the_old(
    tmp_path, host, client
) -> None:
    """Reconnecting to a different host must not carry the old host's role or
    credentials along: the second host said viewer, so the second host
    refuses, whatever the first one had granted."""
    server_a, address_a = host
    _editor_joined(server_a, client, address_a)
    assert client.submit_operation(_add_cue_op("op-a", "c1")).get("ok")

    server_b, address_b = _start_host(tmp_path, name="Show Khác")
    try:
        client.start(address_b, "ABCD23", "laptop-2", "Hùng")
        assert _wait(lambda: client.snapshot()["session_name"] == "Show Khác")

        refused = client.submit_operation(_add_cue_op("op-b", "c2"))
        assert not refused.get("ok")
        assert refused.get("reason") == "forbidden"
        assert server_b._document.revision == 0
        # And host A kept exactly the one operation from before the switch.
        assert server_a._document.revision == 1
    finally:
        server_b.stop()


def test_an_op_for_an_older_hosting_run_is_refused(host, client) -> None:
    server, address = host
    _editor_joined(server, client, address)

    stray = _add_cue_op("op-cu", "c1")
    stray["session_id"] = "phien-truoc"
    stray["document_id"] = "tailieu-truoc"
    reply = client.submit_operation(stray)

    assert not reply.get("ok")
    assert reply.get("reason") == "wrong_session"
    assert server._document.revision == 0


# -- the heartbeat carries the news ------------------------------------


def test_a_lagging_peer_receives_ops_on_its_heartbeat(host, client) -> None:
    server, address = host
    _editor_joined(server, client, address)

    joined = server._join(
        {
            "protocol": PROTOCOL_VERSION,
            "machine_id": "laptop-3",
            "display_name": "Mai",
            "code": "ABCD23",
        }
    )
    token = joined["token"]

    assert client.submit_operation(_add_cue_op("op-1", "c1")).get("ok")

    behind = server._heartbeat(
        {"machine_id": "laptop-3", "token": token, "acked_revision": 0}
    )
    assert behind["ok"]
    assert [record["op_id"] for record in behind["ops"]] == ["op-1"]
    assert behind["revision"] == 1

    caught_up = server._heartbeat(
        {"machine_id": "laptop-3", "token": token, "acked_revision": 1}
    )
    assert caught_up["ops"] == []


def test_the_real_client_acknowledges_only_what_it_applied(host, client) -> None:
    """The acknowledgement follows the apply, not the reply.

    This used to move as soon as a heartbeat mentioned a revision, back when
    nothing applied the records. Now that the cue table does, saying "I have
    revision 1" before writing it down would tell the host to stop sending the
    one record that went missing — and nothing downstream would ever notice.
    """
    server, address = host
    _editor_joined(server, client, address)
    assert client.submit_operation(_add_cue_op("op-1", "c1")).get("ok")

    def _peer_acked() -> bool:
        peers = {peer["machine_id"]: peer for peer in server.snapshot()}
        return peers.get("laptop-2", {}).get("acked_revision", 0) >= 1

    records = _wait(lambda: client.take_ops())
    assert records, "the commit never came back on a heartbeat"
    # Taken but not applied: the host is still owed an answer.
    time.sleep(0.4)
    assert not _peer_acked(), server.snapshot()

    client.confirm_applied(records[-1]["revision"])
    assert _wait(_peer_acked), server.snapshot()


# -- durability seen from the wire --------------------------------------


def test_a_retry_of_a_lost_ack_does_not_double_the_cue(host, client) -> None:
    server, address = host
    _editor_joined(server, client, address)

    first = client.submit_operation(_add_cue_op("op-1", "c1"))
    second = client.submit_operation(_add_cue_op("op-1", "c1"))

    assert first.get("ok") and second.get("ok")
    assert second["revision"] == first["revision"]
    assert len(server._document.cues("t1")) == 1


def test_the_journal_survives_a_host_process_restart(tmp_path, client) -> None:
    """Same journal path across two hosting runs: the second run replays the
    first run's commits, and the old run's identity no longer commits."""
    server, address = _start_host(tmp_path)
    journal = server._document.journal_path
    try:
        _editor_joined(server, client, address)
        assert client.submit_operation(_add_cue_op("op-1", "c1")).get("ok")
    finally:
        client.stop(notify=False)
        server.stop()

    revived = LiveServer()
    port = _free_port()
    revived.start(
        "127.0.0.1", port, "Show Thử", "ABCD23", "host-machine", "Chủ Phiên",
        journal_path=journal,
    )
    try:
        assert revived._document.revision == 1
        assert [cue["id"] for cue in revived._document.cues("t1")] == ["c1"]
    finally:
        revived.stop()

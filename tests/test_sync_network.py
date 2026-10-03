"""A host and a client on one machine, actually talking.

These run the real HTTP server and the real client thread against each other
over a real socket. Mocking either side would test the mock: the failures that
matter here — a refused connection, a wrong code, a peer that stopped
answering — only exist at the boundary.
"""

from __future__ import annotations

import socket
import time

import pytest

from tracyy.sync.client import LiveClient, probe_host
from tracyy.sync.protocol import DEFAULT_PORT, PROTOCOL_VERSION, SyncError, split_address
from tracyy.sync.server import LiveServer


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _joined(server) -> bool:
    """The client is in, not merely the host.

    ``server.snapshot()`` is never empty — the host is always in its own list
    — so waiting on "is there anybody" passes before the client has connected
    and every assertion after it tests nothing.
    """
    return len(server.snapshot()) >= 2


def _wait(check, timeout: float = 8.0):
    """Poll ``check`` until it returns something truthy, or give up.

    The client runs on its own beat, so every assertion about it is about a
    state that arrives shortly — never one that is already there.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.05)
    return None


@pytest.fixture
def host():
    server = LiveServer()
    port = _free_port()
    server.start("127.0.0.1", port, "Show Thử", "ABCD23", "host-machine", "Chủ Phiên")
    try:
        yield server, f"127.0.0.1:{port}"
    finally:
        server.stop()


@pytest.fixture
def client():
    live = LiveClient()
    try:
        yield live
    finally:
        live.stop(notify=False)


# -- addresses ---------------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("192.168.1.9:8090", ("192.168.1.9", 8090)),
        ("192.168.1.9", ("192.168.1.9", DEFAULT_PORT)),
        ("http://192.168.1.9:8090/", ("192.168.1.9", 8090)),
        ("tracyy://192.168.1.9:8090/ABCD23", ("192.168.1.9", 8090)),
        ("  192.168.1.9:8090  ", ("192.168.1.9", 8090)),
    ],
)
def test_addresses_are_accepted_in_the_forms_people_type(typed, expected) -> None:
    assert split_address(typed) == expected


@pytest.mark.parametrize("typed", ["", "   ", "192.168.1.9:abc", "192.168.1.9:70000"])
def test_a_bad_address_says_what_is_wrong(typed) -> None:
    with pytest.raises(SyncError) as raised:
        split_address(typed)
    assert str(raised.value)


# -- finding a host ----------------------------------------------------


def test_probe_finds_a_running_host(host) -> None:
    _server, address = host
    reply = probe_host(address)
    assert reply["ok"]
    assert reply["protocol"] == PROTOCOL_VERSION
    assert reply["session_name"] == "Show Thử"


def test_probe_never_hands_out_the_code(host) -> None:
    """Answering "are you a Tracyy host?" must not also answer "and the code is"."""
    _server, address = host
    reply = probe_host(address)
    assert reply.get("needs_code") is True
    assert "ABCD23" not in str(reply)


def test_probing_a_port_with_nothing_on_it_explains_itself() -> None:
    with pytest.raises(SyncError) as raised:
        probe_host(f"127.0.0.1:{_free_port()}")

    message = str(raised.value)
    assert "Firewall" in message or "chưa tạo phiên" in message


# -- joining -----------------------------------------------------------


def test_a_client_joins_and_the_host_sees_it(host, client) -> None:
    server, address = host
    client.start(address, "ABCD23", "laptop-2", "Hùng")

    assert _wait(lambda: client.snapshot()["state"] == "joined"), client.snapshot()

    names = _wait(lambda: sorted(p["display_name"] for p in server.snapshot()) or None)
    assert names == ["Chủ Phiên", "Hùng"]


def test_the_client_can_see_the_host(host, client) -> None:
    """The point of the feature. A list showing only yourself tells you that
    you joined, not who you joined."""
    _server, address = host
    client.start(address, "ABCD23", "laptop-2", "Hùng")

    host_peer = _wait(
        lambda: next(
            (p for p in client.snapshot()["peers"] if p.get("role") == "owner"), None
        )
    )
    assert host_peer is not None, client.snapshot()
    assert host_peer["display_name"] == "Chủ Phiên"


def test_the_host_cannot_be_demoted(host) -> None:
    server, _address = host
    assert not server.set_role("host-machine", "viewer")
    owner = next(p for p in server.snapshot() if p["machine_id"] == "host-machine")
    assert owner["role"] == "owner"


def test_the_client_learns_who_else_is_in(host, client) -> None:
    server, address = host
    server._join(
        {"protocol": PROTOCOL_VERSION, "machine_id": "laptop-3", "display_name": "Mai", "code": "ABCD23"}
    )
    client.start(address, "ABCD23", "laptop-2", "Hùng")

    names = _wait(
        lambda: sorted(peer["display_name"] for peer in client.snapshot()["peers"]) or None
    )
    assert names == ["Chủ Phiên", "Hùng", "Mai"]


def test_a_new_arrival_starts_as_a_viewer(host, client) -> None:
    """Never anything else: a machine that briefly believes it may edit shows
    enabled controls that then fail."""
    _server, address = host
    client.start(address, "ABCD23", "laptop-2", "Hùng")

    you = _wait(lambda: client.snapshot()["you"] or None)
    assert you["role"] == "viewer"


def test_the_wrong_code_is_refused_with_a_reason(host, client) -> None:
    _server, address = host
    client.start(address, "WRONG1", "laptop-2", "Hùng")

    assert _wait(lambda: client.snapshot()["state"] == "refused"), client.snapshot()
    assert "Mã phiên" in client.snapshot()["error"]


def test_a_refusal_is_not_retried_forever(host, client) -> None:
    """A wrong code will still be wrong in a second; retrying buries the reason."""
    _server, address = host
    client.start(address, "WRONG1", "laptop-2", "Hùng")

    assert _wait(lambda: client.snapshot()["state"] == "refused")
    assert _wait(lambda: not client.running, timeout=3.0), "client kept retrying"


def test_mismatched_versions_are_reported_as_versions(host) -> None:
    server, _address = host
    with pytest.raises(SyncError) as raised:
        server._join(
            {"protocol": PROTOCOL_VERSION + 99, "machine_id": "old", "display_name": "Cũ"}
        )
    assert "phiên bản" in str(raised.value)


# -- staying in, and leaving -------------------------------------------


def test_a_granted_role_reaches_the_client(host, client) -> None:
    server, address = host
    client.start(address, "ABCD23", "laptop-2", "Hùng")
    assert _wait(lambda: client.snapshot()["state"] == "joined")

    assert server.set_role("laptop-2", "editor")
    assert _wait(lambda: client.snapshot()["you"].get("role") == "editor"), client.snapshot()


def test_a_peer_that_stops_answering_is_dropped(host, client) -> None:
    server, address = host
    client.start(address, "ABCD23", "laptop-2", "Hùng")
    assert _wait(lambda: _joined(server)), server.snapshot()

    # Stop without saying goodbye: the case where a laptop is closed or the
    # network vanishes, which is the one the timeout exists for.
    client.stop(notify=False)
    for machine_id, peer in server._peers.items():
        if machine_id != "host-machine":
            peer["last_seen"] = 0.0

    remaining = _wait(
        lambda: [p["machine_id"] for p in server.snapshot()] == ["host-machine"],
        timeout=3.0,
    )
    assert remaining, server.snapshot()


def test_leaving_removes_the_peer_at_once(host, client) -> None:
    server, address = host
    client.start(address, "ABCD23", "laptop-2", "Hùng")
    assert _wait(lambda: _joined(server)), server.snapshot()

    client.stop(notify=True)
    gone = _wait(
        lambda: [p["machine_id"] for p in server.snapshot()] == ["host-machine"],
        timeout=3.0,
    )
    assert gone, server.snapshot()


def test_a_rejoin_keeps_the_role_it_was_granted(host, client) -> None:
    """Somebody's laptop sleeping must not cost them their permissions."""
    server, address = host
    client.start(address, "ABCD23", "laptop-2", "Hùng")
    assert _wait(lambda: client.snapshot()["state"] == "joined")
    server.set_role("laptop-2", "editor")

    client.stop(notify=False)
    client.start(address, "ABCD23", "laptop-2", "Hùng")

    assert _wait(lambda: client.snapshot()["you"].get("role") == "editor"), client.snapshot()


# -- the host's own guards ---------------------------------------------


def test_a_second_host_on_the_same_port_says_the_port_is_taken(host) -> None:
    _server, address = host
    _host_name, port = split_address(address)

    other = LiveServer()
    with pytest.raises(SyncError) as raised:
        other.start("127.0.0.1", port, "Khác", "ZZZZ99", "host-2")
    assert "Cổng" in str(raised.value)


def test_a_browser_gets_words_rather_than_a_404(host) -> None:
    """Somebody will paste the address into a browser to check it."""
    import urllib.request

    _server, address = host
    with urllib.request.urlopen(f"http://{address}/", timeout=4) as response:
        body = response.read().decode("utf-8")
    assert "Tracyy Live" in body
    assert "Sync" in body

"""Connection regressions, including a host in a separate spawned process."""

from __future__ import annotations

import multiprocessing
import threading
import time

import pytest

from tracyy.sync import client as client_module
from tracyy.sync.client import LiveClient, _post, probe_host
from tracyy.sync.protocol import (
    PATH_HEARTBEAT,
    PATH_JOIN,
    PATH_LEAVE,
    PROTOCOL_VERSION,
    SyncError,
    server_url,
    split_address,
)
from tracyy.sync.server import PEER_TIMEOUT, LiveServer


def wait_for(check, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if value := check():
            return value
        time.sleep(0.025)
    raise AssertionError("Timed out waiting for connection state")


@pytest.fixture
def pair():
    host, client = LiveServer(), LiveClient()
    host.start("127.0.0.1", 0, "Phiên A", "ABC234", "host", "Máy chính")
    try:
        yield host, client, f"127.0.0.1:{host.port}"
    finally:
        client.stop()
        wait_for(lambda: not client.running)
        host.stop()


def join(client, address):
    client.start(address, "ABC234", "client", "Máy khách")
    wait_for(lambda: client.snapshot()["state"] == "joined")


@pytest.mark.parametrize("address", ["[::1]", "[::1]:8090", "http://[::1]:8090/"])
def test_ipv6_keeps_brackets_in_http_url(address):
    assert split_address(address) == ("::1", 8090)
    assert server_url(address) == "http://[::1]:8090"


@pytest.mark.parametrize(
    "address",
    [
        "0.0.0.0:8090",
        "[::]:8090",
        "host:",
        ":8090",
        "http://user@host",
        "host name",
        "https://host",
    ],
)
def test_non_connectable_addresses_fail_before_start(address):
    with pytest.raises(SyncError):
        server_url(address)


def test_two_hosts_do_not_share_handler_owner(pair):
    first, _, address = pair
    second = LiveServer()
    try:
        second.start("127.0.0.1", 0, "Phiên B", "XYZ234", "other-host")
        assert probe_host(address)["session_name"] == "Phiên A"
        assert probe_host(f"127.0.0.1:{second.port}")["session_name"] == "Phiên B"
        # A failed bind must not redirect requests on the original server.
        failed = LiveServer()
        with pytest.raises(SyncError):
            failed.start("127.0.0.1", first.port, "Không mở được", "BAD", "bad-host")
        assert probe_host(address)["session_name"] == "Phiên A"
    finally:
        second.stop()


def test_lan_requests_ignore_system_proxy(pair, monkeypatch):
    _, _, address = pair
    monkeypatch.setattr(
        client_module.urllib.request, "getproxies", lambda: {"http": "http://127.0.0.1:1"}
    )
    monkeypatch.setattr(client_module.urllib.request, "proxy_bypass", lambda _host: False)
    assert probe_host(address)["ok"]


def test_host_identity_and_grants_cannot_be_claimed(pair):
    host, client, address = pair
    client.start(address, "ABC234", "host", "Giả máy chính")
    wait_for(lambda: client.snapshot()["state"] == "refused")
    assert host.snapshot()[0]["display_name"] == "Máy chính"
    assert host.snapshot()[0]["role"] == "owner"
    join(client, address)
    assert not host.set_role("client", "owner")
    assert not host.set_role("client", "invented-role")
    assert host.set_role("client", "editor")
    assert all("token" not in peer for peer in host.snapshot())
    for path in (PATH_HEARTBEAT, PATH_LEAVE):
        reply = _post(server_url(address), path, {"machine_id": "client", "role": "owner"})
        assert reply["ok"] is False
    assert len(host.snapshot()) == 2


def test_sleep_past_timeout_keeps_granted_role(pair):
    host, client, address = pair
    join(client, address)
    host.set_role("client", "editor")
    client.stop(notify=False)
    wait_for(lambda: not client.running)
    with host._lock:
        host._peers["client"]["last_seen"] = 0.0
    assert [peer["machine_id"] for peer in host.snapshot()] == ["host"]
    join(client, address)
    assert client.snapshot()["you"]["role"] == "editor"


def test_heartbeat_prunes_silent_peers_without_gui_tick(pair):
    host, client, address = pair
    join(client, address)
    host._join({"protocol": PROTOCOL_VERSION, "code": "ABC234", "machine_id": "silent"})
    with host._lock:
        host._peers["silent"]["last_seen"] = 0.0
    reply = _post(
        server_url(address),
        PATH_HEARTBEAT,
        {
            "machine_id": "client",
            "token": host._tokens["client"],
        },
    )
    assert {p["machine_id"] for p in reply["peers"]} == {"host", "client"}


def test_stopped_and_replaced_client_ignores_late_reply(pair, monkeypatch):
    host, client, address = pair
    second = LiveServer()
    entered, release = threading.Event(), threading.Event()
    real_post = client_module._post

    def delayed(base, path, payload, **kwargs):
        reply = real_post(base, path, payload, **kwargs)
        if base == server_url(address) and path == PATH_JOIN:
            entered.set()
            assert release.wait(6)
        return reply

    monkeypatch.setattr(client_module, "_post", delayed)
    try:
        second.start("127.0.0.1", 0, "Phiên B", "ABC234", "second-host")
        client.start(address, "ABC234", "client", "Máy khách")
        assert entered.wait(4)
        started = time.monotonic()
        client.stop()
        # Cancel a queued rejoin too: a third worker must still wait for the
        # first connection's in-flight request and farewell to retire.
        client.start(address, "ABC234", "client", "Máy khách")
        client.start(f"127.0.0.1:{second.port}", "ABC234", "client", "Máy khách")
        assert time.monotonic() - started < 0.5
        assert client.snapshot()["state"] == "connecting"
        release.set()
        wait_for(lambda: client.snapshot()["state"] == "joined")
        assert client.snapshot()["session_name"] == "Phiên B"
        assert {p["machine_id"] for p in client.snapshot()["peers"]} == {
            "client",
            "second-host",
        }
        assert [p["machine_id"] for p in host.snapshot()] == ["host"]
    finally:
        release.set()
        client.stop()
        wait_for(lambda: not client.running)
        second.stop()


@pytest.mark.parametrize("missing_local", [False, True])
def test_malformed_heartbeat_locks_instead_of_killing_worker(pair, monkeypatch, missing_local):
    _, client, address = pair
    join(client, address)
    real_post = client_module._post
    you = client.snapshot()["you"]

    def malformed(base, path, payload, **kwargs):
        if path == PATH_HEARTBEAT:
            return {"ok": True, "you": you if missing_local else [], "peers": []}
        return real_post(base, path, payload, **kwargs)

    monkeypatch.setattr(client_module, "_post", malformed)
    wait_for(lambda: client.snapshot()["state"] == "disconnected")
    assert client.running
    assert client.snapshot()["peers"] == []
    monkeypatch.setattr(client_module, "_post", real_post)
    wait_for(lambda: client.snapshot()["state"] == "joined")


def test_lost_join_reply_can_retry_without_losing_identity(pair, monkeypatch):
    host, client, address = pair
    real_post = client_module._post
    lost_reply = threading.Event()

    def lose_first_reply(base, path, payload, **kwargs):
        reply = real_post(base, path, payload, **kwargs)
        if path == PATH_JOIN and not lost_reply.is_set():
            assert host.set_role("client", "editor")
            lost_reply.set()
            raise SyncError("Test: connection lost after host accepted join")
        return reply

    monkeypatch.setattr(client_module, "_post", lose_first_reply)
    join(client, address)
    assert lost_reply.is_set()
    assert client.snapshot()["you"]["role"] == "editor"
    assert len(host.snapshot()) == 2


def _host_process(pipe):
    """Command-controlled lifetime; never exit just because boot took 10 s."""
    host = LiveServer()
    try:
        host.start("0.0.0.0", 0, "Hai máy • Việt Nam", "ABC234", "host", "Máy chính")
        port = host.port
        pipe.send(port)
        while pipe.poll(40):
            command = pipe.recv()
            if command == "peers":
                pipe.send(host.snapshot())
            elif command == "grant":
                pipe.send(host.set_role("client", "editor"))
            elif command == "down":
                host.stop()
                pipe.send(True)
            elif command == "up":
                host.start("0.0.0.0", port, "Phiên mới", "ABC234", "host", "Máy chính")
                pipe.send(True)
            elif command == "exit":
                break
    finally:
        host.stop()
        pipe.close()


def test_spawned_host_stays_connected_then_recovers_from_restart():
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_host_process, args=(child,))
    client = LiveClient()
    process.start()
    child.close()

    def command(value):
        parent.send(value)
        assert parent.poll(6), f"Host did not answer {value}"
        return parent.recv()

    try:
        assert parent.poll(10), "Host failed to start"
        port = parent.recv()
        join(client, f"127.0.0.1:{port}")
        assert {p["machine_id"] for p in command("peers")} == {"host", "client"}
        assert command("grant")
        wait_for(lambda: client.snapshot()["you"].get("role") == "editor")
        client.set_presence("t-khac", "Bài khác.wav", 42.5, "Biên tập viên")
        wait_for(
            lambda: next(
                (p for p in command("peers") if p.get("position_seconds") == 42.5), None
            )
        )
        deadline = time.monotonic() + PEER_TIMEOUT + 2
        while time.monotonic() < deadline:
            assert client.snapshot()["state"] == "joined", client.snapshot()
            assert len(command("peers")) == 2
            time.sleep(0.2)
        assert command("down")
        wait_for(lambda: client.snapshot()["state"] == "disconnected")
        assert client.snapshot()["peers"] == []
        assert command("up")
        wait_for(lambda: client.snapshot()["state"] == "joined")
        # A new host session never silently reuses the previous grant.
        assert client.snapshot()["you"]["role"] == "viewer"
        assert client.snapshot()["session_name"] == "Phiên mới"
        client.stop()
        wait_for(lambda: not client.running)
        assert [p["machine_id"] for p in command("peers")] == ["host"]
    finally:
        client.stop(notify=False)
        wait_for(lambda: not client.running)
        if process.is_alive():
            parent.send("exit")
        process.join(6)
        if process.is_alive():
            process.terminate()
            process.join(3)
        parent.close()
    assert process.exitcode == 0

"""LAN cue viewer: transport-level behaviour.

These are the things a room full of phones actually exercises — conditional
requests, compression, and the push feed — rather than the page markup.
"""

from __future__ import annotations

import gzip
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tracyy.server.cue_viewer import Server, StateReader

STATE = {
    "timecode": "01:02:03:04",
    "state": "PLAYING",
    "offset": "00:00:00:00",
    "elapsed": "00:01:02",
    "duration": "00:04:00",
    "track": "Opening Number",
    "session_id": "session-1",
    "manifest_revision": "rev-1",
    "cues": [{"id": 1, "time": 12.5, "label": "Lights", "type": "Lighting"}],
}


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def running_server(tmp_path: Path):
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(STATE), encoding="utf-8")

    reader = StateReader(state_path, poll_interval=0.02)
    reader.start()
    server = Server(
        ("127.0.0.1", _free_port()),
        reader,
        status_path=state_path.with_name(state_path.name + ".viewers"),
    )
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.daemon = True
    thread.start()

    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base, state_path, reader, server
    finally:
        server.shutdown()
        reader.stop()
        server.server_close()
        thread.join(timeout=5)


def _get(url: str, headers: dict[str, str] | None = None):
    request = urllib.request.Request(url, headers=headers or {})
    return urllib.request.urlopen(request, timeout=5)


def test_state_endpoint_omits_cues(running_server) -> None:
    base, _state_path, _reader, _server = running_server
    with _get(f"{base}/api/state") as response:
        payload = json.loads(response.read().decode("utf-8"))
    assert payload["timecode"] == "01:02:03:04"
    assert payload["track"] == "Opening Number"
    assert "cues" not in payload, "the state feed must stay small"


def test_manifest_endpoint_carries_cues(running_server) -> None:
    base, _state_path, _reader, _server = running_server
    with _get(f"{base}/api/manifest") as response:
        payload = json.loads(response.read().decode("utf-8"))
    assert payload["manifest_revision"] == "rev-1"
    assert len(payload["cues"]) == 1


def test_page_is_compressed(running_server) -> None:
    base, _state_path, _reader, _server = running_server
    with _get(base + "/", {"Accept-Encoding": "gzip"}) as response:
        assert response.headers.get("Content-Encoding") == "gzip"
        body = gzip.decompress(response.read())
    assert b"Tracyy Cue Viewer" in body


def test_page_is_sent_plain_when_gzip_is_not_offered(running_server) -> None:
    base, _state_path, _reader, _server = running_server
    with _get(base + "/", {"Accept-Encoding": "identity"}) as response:
        assert response.headers.get("Content-Encoding") is None
        assert b"Tracyy Cue Viewer" in response.read()


def test_unchanged_page_revalidates_as_304(running_server) -> None:
    base, _state_path, _reader, _server = running_server
    with _get(base + "/") as response:
        etag = response.headers.get("ETag")
    assert etag

    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _get(base + "/", {"If-None-Match": etag})
    assert excinfo.value.code == 304


def test_live_state_is_never_cached(running_server) -> None:
    base, _state_path, _reader, _server = running_server
    with _get(f"{base}/api/state") as response:
        assert response.headers.get("Cache-Control") == "no-store"
        assert response.headers.get("ETag") is None


def test_events_push_the_first_frames(running_server) -> None:
    base, _state_path, _reader, _server = running_server
    with _get(f"{base}/events") as response:
        assert response.headers.get("Content-Type", "").startswith("text/event-stream")
        seen = b""
        deadline = time.monotonic() + 5.0
        while b"event: state" not in seen and time.monotonic() < deadline:
            seen += response.fp.read1(512)
    assert b"event: manifest" in seen
    assert b"event: state" in seen
    assert b"01:02:03:04" in seen


def test_watcher_publishes_a_new_revision_on_change(running_server) -> None:
    _base, state_path, reader, _server = running_server
    revision = reader.snapshot()[0]

    updated = dict(STATE, timecode="09:09:09:09")
    state_path.write_text(json.dumps(updated), encoding="utf-8")

    assert reader.wait_for_change(revision, timeout=5.0)
    new_revision, state_frame, _manifest, _signature = reader.snapshot()
    assert new_revision != revision
    assert b"09:09:09:09" in state_frame


def test_half_written_state_keeps_the_last_good_snapshot(running_server) -> None:
    _base, state_path, reader, _server = running_server
    before = reader.read()["timecode"]

    state_path.write_text('{"timecode": "09:0', encoding="utf-8")
    time.sleep(0.15)

    assert reader.read()["timecode"] == before


def test_writes_are_refused(running_server) -> None:
    base, _state_path, _reader, _server = running_server
    request = urllib.request.Request(base + "/api/state", data=b"{}", method="POST")
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request, timeout=5)
    assert excinfo.value.code == 405
    assert json.loads(excinfo.value.read())["error"] == "READ_ONLY_VIEWER"


def _viewer_count(state_path: Path) -> int | None:
    """What the application would read out of the status file."""
    status = state_path.with_name(state_path.name + ".viewers")
    try:
        return int(json.loads(status.read_bytes())["viewers"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def test_viewer_count_starts_at_zero(running_server) -> None:
    _base, state_path, _reader, _server = running_server
    assert _viewer_count(state_path) == 0


def test_open_event_streams_are_counted(running_server) -> None:
    base, state_path, _reader, _server = running_server

    with _get(f"{base}/events") as first:
        first.fp.read1(64)
        with _get(f"{base}/events") as second:
            second.fp.read1(64)
            deadline = time.monotonic() + 5.0
            while _viewer_count(state_path) != 2 and time.monotonic() < deadline:
                time.sleep(0.02)
            assert _viewer_count(state_path) == 2


def test_the_count_survives_viewers_leaving_together(running_server) -> None:
    """Simultaneous departures must not leave the file reading too high.

    Counting under the lock and publishing outside it passes a one-at-a-time
    test and fails this one: two threads compute 1 and 0 correctly, then race
    to write, and the loser's stale 1 stands until somebody else connects.
    """
    _base, state_path, _reader, server = running_server

    for _ in range(6):
        server.viewer_joined()
    assert _viewer_count(state_path) == 6

    leaving = [threading.Thread(target=server.viewer_left) for _ in range(6)]
    for thread in leaving:
        thread.start()
    for thread in leaving:
        thread.join()

    assert _viewer_count(state_path) == 0


def test_the_status_file_is_never_seen_half_written(running_server) -> None:
    """A reader polling flat out must never catch a partial write."""
    _base, state_path, _reader, server = running_server

    stop = threading.Event()
    torn: list[object] = []

    def poll() -> None:
        while not stop.is_set():
            if _viewer_count(state_path) is None:
                torn.append(object())

    watcher = threading.Thread(target=poll)
    watcher.start()
    try:
        for _ in range(200):
            server.viewer_joined()
            server.viewer_left()
    finally:
        stop.set()
        watcher.join(timeout=5)

    assert torn == []

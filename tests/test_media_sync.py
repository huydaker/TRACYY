"""Music crossing between machines: identity, resume, and refusal.

The failures worth catching here are the quiet ones. A file that downloads
99% and plays anyway. A rename that lands a half file under the real name. A
client that asks for a path instead of a track and gets it.
"""

from __future__ import annotations

import socket
import time
from pathlib import Path

import pytest

from tracyy.sync.client import LiveClient
from tracyy.sync.media import (
    MediaDownloader,
    MediaEntry,
    MediaLibrary,
    MediaTarget,
    hash_file,
)
from tracyy.sync.server import LiveServer

SONG = b"ID3 pretend this is audio " * 5000  # ~130 KB, several chunks


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait(check, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.05)
    return None


@pytest.fixture
def song(tmp_path):
    path = tmp_path / "Bài mở màn.mp3"
    path.write_bytes(SONG)
    return path


@pytest.fixture
def library(song):
    shelf = MediaLibrary()
    shelf.publish({"t-1": str(song)})
    return shelf


# -- what the host publishes -------------------------------------------------


def test_the_manifest_describes_the_track_by_its_content(library, song) -> None:
    entries = library.manifest()
    assert len(entries) == 1
    entry = MediaEntry.from_wire(entries[0])
    assert entry is not None
    assert entry.track_id == "t-1"
    assert entry.size == len(SONG)
    assert entry.media_hash == hash_file(song)
    assert entry.suffix == ".mp3"


def test_a_track_whose_file_vanished_is_left_out(tmp_path) -> None:
    shelf = MediaLibrary()
    shelf.publish({"t-gone": str(tmp_path / "không có.mp3")})
    assert shelf.manifest() == []


def test_only_published_tracks_can_be_read(library) -> None:
    """A request names a track, never a path. There is nothing to walk out of."""
    assert library.read_chunk("t-1", 0, 16) == SONG[:16]
    assert library.read_chunk("../../etc/passwd", 0, 16) == b""
    assert library.read_chunk("t-nope", 0, 16) == b""


def test_a_bad_entry_from_the_wire_is_refused(tmp_path) -> None:
    good = MediaLibrary()
    good.publish({"t-1": str(tmp_path)})
    for payload in (
        {"track_id": "t", "size": 10},  # no hash
        {"track_id": "t", "size": 10, "media_hash": "abc"},  # not a sha256
        {"track_id": "", "size": 10, "media_hash": "a" * 64},
        {"track_id": "t", "size": 3 * 1024**3, "media_hash": "a" * 64},  # absurd
    ):
        assert MediaEntry.from_wire(payload) is None

    # A suffix is pasted onto a filename this machine writes, so it may be an
    # extension and nothing more.
    sneaky = MediaEntry.from_wire(
        {"track_id": "t", "size": 4, "media_hash": "a" * 64, "suffix": "/../../x"}
    )
    assert sneaky is not None and sneaky.suffix == ""


# -- what the receiving machine does with the bytes --------------------------


def _entry_for(library: MediaLibrary) -> MediaEntry:
    entry = MediaEntry.from_wire(library.manifest()[0])
    assert entry is not None
    return entry


def test_a_file_is_absent_or_complete_never_half(tmp_path, library) -> None:
    entry = _entry_for(library)
    target = MediaTarget(tmp_path / "media", entry)

    with target.open_for_append() as handle:
        handle.write(SONG[: len(SONG) // 2])

    # Half the bytes: the fragment exists, the real name does not.
    assert target.part_path.exists()
    assert not target.final_path.exists()
    assert target.finish() == ""

    with target.open_for_append() as handle:
        handle.write(SONG[len(SONG) // 2 :])
    assert target.finish() == str(target.final_path)
    assert target.final_path.read_bytes() == SONG
    assert not target.part_path.exists()


def test_bytes_that_are_not_the_track_are_thrown_away(tmp_path, library) -> None:
    entry = _entry_for(library)
    target = MediaTarget(tmp_path / "media", entry)
    with target.open_for_append() as handle:
        handle.write(b"x" * len(SONG))  # right length, wrong song

    assert target.finish() == ""
    assert not target.final_path.exists()
    # And the fragment goes, so the next attempt starts clean rather than
    # resuming something that can never pass.
    assert not target.part_path.exists()


def test_a_download_resumes_where_it_stopped(tmp_path, library) -> None:
    entry = _entry_for(library)
    target = MediaTarget(tmp_path / "media", entry)
    with target.open_for_append() as handle:
        handle.write(SONG[:1000])

    asked: list[int] = []

    def fetch(track_id: str, offset: int) -> bytes:
        asked.append(offset)
        return library.read_chunk(track_id, offset, 4096)

    downloader = MediaDownloader(tmp_path / "media", fetch, chunk=4096)
    downloader.request([entry])
    downloader.start()
    try:
        assert _wait(lambda: downloader.take_completed())
    finally:
        downloader.stop()

    assert asked[0] == 1000, "started again from zero instead of resuming"
    assert target.final_path.read_bytes() == SONG


def test_a_host_that_stops_answering_leaves_the_fragment(tmp_path, library) -> None:
    entry = _entry_for(library)

    def fetch(track_id: str, offset: int) -> bytes:
        return library.read_chunk(track_id, offset, 4096) if offset < 8192 else b""

    downloader = MediaDownloader(tmp_path / "media", fetch, chunk=4096)
    downloader.request([entry])
    downloader.start()
    try:
        assert _wait(lambda: downloader.snapshot()["failed"])
    finally:
        downloader.stop()

    target = MediaTarget(tmp_path / "media", entry)
    assert not target.final_path.exists()
    assert target.received == 8192, "the partial download was not kept"


# -- across a real socket ----------------------------------------------------


@pytest.fixture
def session(tmp_path, song):
    server = LiveServer()
    port = _free_port()
    server.start(
        "127.0.0.1",
        port,
        "Show Nhạc",
        "ABCD23",
        "host-machine",
        "Chủ Phiên",
        journal_path=tmp_path / "journal.ndjson",
    )
    server.publish_media({"t-1": str(song)})

    client = LiveClient()
    client.start(f"127.0.0.1:{port}", "ABCD23", "laptop-1", "Linh")
    try:
        assert _wait(lambda: client.snapshot()["state"] == "joined")
        yield server, client
    finally:
        client.stop(notify=False)
        server.stop()


def test_a_client_downloads_the_show_over_the_wire(tmp_path, session, song) -> None:
    _server, client = session

    entries = client.fetch_manifest()
    assert [entry.track_id for entry in entries] == ["t-1"]
    assert entries[0].media_hash == hash_file(song)

    downloader = MediaDownloader(
        tmp_path / "store",
        client.fetch_media_chunk,
        manifest_fetch=client.fetch_manifest,
    )
    downloader.refresh_manifest()
    downloader.start()
    try:
        landed = _wait(lambda: downloader.take_completed(), timeout=20.0)
    finally:
        downloader.stop()

    assert landed and "t-1" in landed
    assert Path(landed["t-1"]).read_bytes() == SONG


def test_a_machine_that_never_joined_gets_nothing(session) -> None:
    """Credentials are checked before a single byte of audio is read."""
    stranger = LiveClient()
    assert stranger.fetch_manifest() == []
    assert stranger.fetch_media_chunk("t-1", 0) == b""

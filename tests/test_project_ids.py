"""Cue and track identity, and the v3 project migration that introduces it.

Version 3 gave a cue an integer that only meant something inside its own
track, and gave a track no identifier at all — it was addressed by its path on
disk. Both are fine for one laptop and silently wrong for two, so version 4
replaces them. These tests exist because the migration rewrites every cue in
somebody's show, and a mistake there loses work rather than raising.
"""

from __future__ import annotations

import json
import os
import wave

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tracyy.core.ids import cue_key, new_cue_id, new_origin, new_track_id


@pytest.fixture(scope="module")
def window():
    from tracyy.ui.main_window import MainWindow, TracyyApplication
    from tracyy.ui.theme import apply_theme

    app = TracyyApplication.instance() or TracyyApplication([])
    apply_theme(app)
    main = MainWindow()

    # Loading a project selects the first track, which opens an audio device
    # and fails on a machine with none. The application reports that with a
    # modal box, and a modal box in an offscreen test does not time out — it
    # simply never returns. Recording the text keeps the failures visible
    # without stopping the run.
    main.reported_errors = []
    main.show_error = main.reported_errors.append

    # Not closed on teardown: closing the window runs the save-on-close path,
    # which asks a question and waits for an answer that never comes. The
    # process ends when the run does.
    yield main


@pytest.fixture(autouse=True)
def no_modal_dialogs(monkeypatch):
    """Nothing in a test may open a dialog and wait for a person.

    Patched everywhere the helper is bound, not only where it is defined:
    modules that imported it by name hold their own reference, and patching
    the definition alone leaves those calls still blocking.
    """
    from PySide6.QtWidgets import QMessageBox

    import tracyy.controllers.project as project_module
    import tracyy.core.ui_helpers as helpers
    import tracyy.ui.main_window as window_module

    def refuse(_parent, _title, _message, **_kwargs):
        return QMessageBox.StandardButton.No

    for module in (helpers, project_module, window_module):
        if hasattr(module, "silent_message_box"):
            monkeypatch.setattr(module, "silent_message_box", refuse)
    yield


def _audio(tmp_path, name: str):
    """A real, tiny WAV the decoder can actually open.

    A file with only a RIFF header is enough to stop the loader calling the
    track missing, and then loading it fails and the application raises a
    modal error dialog — which in an offscreen test simply hangs forever.
    """
    path = tmp_path / name
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(48000)
        handle.writeframes(b"\x00" * (4 * 4800))
    return path


def _load(window, path: str) -> None:
    """Load a project, and say why when it does not.

    ``load_project_path`` swallows every exception into a message box, so a
    bare ``assert ok`` reports nothing but False and leaves the reason in a
    dialog nobody sees.
    """
    window.reported_errors.clear()
    assert window.load_project_path(path), (
        f"project failed to load: {window.reported_errors}"
    )


def _write_project(tmp_path, payload: dict) -> str:
    target = tmp_path / "show.Tracyy"
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(target)


# -- the minting rules -------------------------------------------------


def test_ids_from_one_origin_are_unique() -> None:
    origin = new_origin()
    minted = [new_cue_id(origin) for _ in range(500)]
    assert len(set(minted)) == 500


def test_two_machines_never_collide() -> None:
    """The whole point: max(id) + 1 gave both machines the same number."""
    a = [new_cue_id(new_origin()) for _ in range(200)]
    b = [new_cue_id(new_origin()) for _ in range(200)]
    assert not set(a) & set(b)


def test_track_ids_are_not_cue_ids() -> None:
    origin = new_origin()
    assert new_track_id(origin) != new_cue_id(origin)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(7, "7"), ("7", "7"), (" 7 ", "7"), (7.0, "7"), (None, ""), (True, "")],
)
def test_ids_compare_equal_across_the_types_they_arrive_as(value, expected) -> None:
    """A v3 integer, a string off the wire and a value out of a table's item
    data must all name the same cue, or a cue looks like it vanished."""
    assert cue_key(value) == expected


# -- migrating a version 3 project -------------------------------------


def test_v3_cue_ids_become_unique_across_the_whole_document(window, tmp_path) -> None:
    """Both tracks used id 1 and 2. After the migration nothing is shared."""
    first = _audio(tmp_path, "one.wav")
    second = _audio(tmp_path, "two.wav")
    path = _write_project(
        tmp_path,
        {
            "format": "Tracyy Project",
            "version": 3,
            "project_fps": 30,
            "playlist": [
                {"path": str(first), "display_name": "One"},
                {"path": str(second), "display_name": "Two"},
            ],
            "cues": {
                str(first): [
                    {"id": 1, "seconds": 1.0, "name": "A", "type": "Choreo"},
                    {"id": 2, "seconds": 2.0, "name": "B", "type": "Choreo"},
                ],
                str(second): [
                    {"id": 1, "seconds": 3.0, "name": "C", "type": "Choreo"},
                    {"id": 2, "seconds": 4.0, "name": "D", "type": "Choreo"},
                ],
            },
        },
    )

    _load(window, path)

    every_id = [
        cue_key(cue["id"]) for cues in window.cues_by_track.values() for cue in cues
    ]
    assert len(every_id) == 4
    assert len(set(every_id)) == 4, "two tracks still share a cue id"
    assert all(identifier and not identifier.isdigit() for identifier in every_id)


def test_migration_keeps_every_cue_and_its_time(window, tmp_path) -> None:
    """The migration rewrites ids; it must not touch anything else."""
    track = _audio(tmp_path, "song.wav")
    path = _write_project(
        tmp_path,
        {
            "format": "Tracyy Project",
            "version": 3,
            "project_fps": 30,
            "playlist": [{"path": str(track), "display_name": "Song"}],
            "cues": {
                str(track): [
                    {"id": 1, "seconds": 12.5, "name": "Lights", "label": "L1", "type": "Lighting"},
                    {"id": 2, "seconds": 30.0, "name": "Pyro", "label": "", "type": "Pyro"},
                ]
            },
        },
    )

    _load(window, path)

    cues = window.cues_by_track[str(track)]
    assert [cue["seconds"] for cue in cues] == [12.5, 30.0]
    assert [cue["name"] for cue in cues] == ["Lights", "Pyro"]
    assert [cue["type"] for cue in cues] == ["Lighting", "Pyro"]


def test_a_migrated_track_gets_an_id(window, tmp_path) -> None:
    track = _audio(tmp_path, "song.wav")
    path = _write_project(
        tmp_path,
        {
            "format": "Tracyy Project",
            "version": 3,
            "project_fps": 30,
            "playlist": [{"path": str(track), "display_name": "Song"}],
            "cues": {str(track): [{"id": 1, "seconds": 1.0, "name": "A", "type": "Choreo"}]},
        },
    )

    _load(window, path)
    assert window.track_id_for_path(str(track))
    assert window.path_for_track_id(window.track_id_for_path(str(track))) == str(track)


# -- version 4 round trip ----------------------------------------------


def test_saving_writes_version_4_keyed_by_track_id(window, tmp_path) -> None:
    track = _audio(tmp_path, "song.wav")
    source = _write_project(
        tmp_path,
        {
            "format": "Tracyy Project",
            "version": 3,
            "project_fps": 30,
            "playlist": [{"path": str(track), "display_name": "Song"}],
            "cues": {str(track): [{"id": 1, "seconds": 5.0, "name": "A", "type": "Choreo"}]},
        },
    )
    _load(window, source)

    target = str(tmp_path / "saved.Tracyy")
    assert window.write_project_file(target)

    saved = json.loads((tmp_path / "saved.Tracyy").read_text(encoding="utf-8"))
    assert saved["version"] == 4

    track_id = window.track_id_for_path(str(track))
    assert track_id in saved["cues"], "cues are still keyed by path"
    assert str(track) not in saved["cues"]
    assert saved["playlist"][0]["track_id"] == track_id


def test_ids_survive_a_full_round_trip(window, tmp_path) -> None:
    """Save and load again: the same cue keeps the same id.

    If it did not, every reopen would look to the other machines in a session
    like every cue had been deleted and replaced.
    """
    track = _audio(tmp_path, "song.wav")
    source = _write_project(
        tmp_path,
        {
            "format": "Tracyy Project",
            "version": 3,
            "project_fps": 30,
            "playlist": [{"path": str(track), "display_name": "Song"}],
            "cues": {
                str(track): [
                    {"id": 1, "seconds": 5.0, "name": "A", "type": "Choreo"},
                    {"id": 2, "seconds": 9.0, "name": "B", "type": "Choreo"},
                ]
            },
        },
    )
    _load(window, source)
    before_cues = [cue_key(cue["id"]) for cue in window.cues_by_track[str(track)]]
    before_track = window.track_id_for_path(str(track))

    target = str(tmp_path / "saved.Tracyy")
    assert window.write_project_file(target)
    _load(window, target)

    assert [cue_key(cue["id"]) for cue in window.cues_by_track[str(track)]] == before_cues
    assert window.track_id_for_path(str(track)) == before_track


def test_cues_for_a_song_this_machine_lacks_are_not_dropped(window, tmp_path) -> None:
    """A v4 file can name a track that is not in this playlist.

    Discarding those cues would quietly delete somebody's work the first time
    a project was opened on a laptop missing one song.
    """
    track = _audio(tmp_path, "song.wav")
    source = _write_project(
        tmp_path,
        {
            "format": "Tracyy Project",
            "version": 4,
            "project_fps": 30,
            "playlist": [{"path": str(track), "track_id": "tabc-1", "display_name": "Song"}],
            "cues": {
                "tabc-1": [{"id": "m1-1", "seconds": 5.0, "name": "A", "type": "Choreo"}],
                "tzzz-9": [{"id": "m2-1", "seconds": 7.0, "name": "Orphan", "type": "Choreo"}],
            },
        },
    )
    _load(window, source)

    target = str(tmp_path / "saved.Tracyy")
    assert window.write_project_file(target)
    saved = json.loads((tmp_path / "saved.Tracyy").read_text(encoding="utf-8"))

    assert "tzzz-9" in saved["cues"], "cues for an absent song were lost"
    assert saved["cues"]["tzzz-9"][0]["name"] == "Orphan"

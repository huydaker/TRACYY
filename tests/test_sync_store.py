"""The per-show download cache: what it measures, and what it will delete.

The delete path gets the most attention here because it is the one that
removes files from somebody's disk, and the show id it is handed comes out of
a settings file a person can edit by hand.
"""

from __future__ import annotations

import pytest

from tracyy.sync import store as store_module
from tracyy.sync.store import ShowStore, human_bytes, store_for


@pytest.fixture(autouse=True)
def live_root(tmp_path, monkeypatch):
    root = tmp_path / "live"
    monkeypatch.setattr(store_module, "live_root", lambda: root)
    return root


# -- sizes -------------------------------------------------------------


@pytest.mark.parametrize(
    ("size", "shown"),
    [
        (0, "0 B"),
        (512, "512 B"),
        (2048, "2 KB"),
        (5 * 1024 * 1024, "5 MB"),
        (int(1.29 * 1024**3), "1.3 GB"),
    ],
)
def test_sizes_read_as_something_a_person_can_act_on(size: int, shown: str) -> None:
    assert human_bytes(size) == shown


def test_an_untouched_show_measures_zero() -> None:
    assert ShowStore("abc").size_bytes() == 0
    assert not ShowStore("abc").exists()


def test_size_counts_everything_underneath() -> None:
    store = ShowStore("abc")
    store.ensure()
    (store.media_dir / "song.wav").write_bytes(b"x" * 3000)
    (store.peaks_dir / "song.npy").write_bytes(b"y" * 1000)
    store.document_path.write_text("{}", encoding="utf-8")

    assert store.size_bytes() == 4002
    assert store.media_count() == 1


# -- deleting ----------------------------------------------------------


def test_delete_removes_the_show_and_nothing_else(live_root) -> None:
    keeper = ShowStore("keep")
    keeper.ensure()
    (keeper.media_dir / "song.wav").write_bytes(b"x" * 10)

    victim = ShowStore("drop")
    victim.ensure()
    (victim.media_dir / "song.wav").write_bytes(b"x" * 10)

    assert victim.delete()
    assert not victim.exists()
    assert keeper.exists()
    assert live_root.is_dir()


def test_deleting_nothing_is_not_a_success() -> None:
    """Returning True for a folder that was never there would report a
    deletion that did not happen, and the page would say it freed space."""
    assert not ShowStore("never-downloaded").delete()


@pytest.mark.parametrize("show_id", ["..", ".", "", "   ", "a/b", "a\\b", "C:", "../../etc"])
def test_ids_that_could_name_a_folder_elsewhere_are_refused(show_id: str) -> None:
    """The id arrives from a host and from an editable settings file.

    A traversal in it must not turn "clear cached music" into deleting
    somebody's home directory, and it is refused when the store is built
    rather than at the moment Delete is pressed.
    """
    assert store_for(show_id) is None


def test_a_normal_id_is_accepted() -> None:
    store = store_for("7f3a91c02b45")
    assert store is not None
    assert store.show_id == "7f3a91c02b45"


def test_delete_refuses_to_escape_the_live_root(live_root, tmp_path) -> None:
    """Belt and braces behind store_for: even handed a bad id directly, the
    store checks that what it is about to remove sits under the live root."""
    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / "project.Tracyy").write_text("{}", encoding="utf-8")

    escaping = ShowStore("../precious")
    assert not escaping.delete()
    assert (outside / "project.Tracyy").exists()


def test_delete_refuses_the_live_root_itself(live_root) -> None:
    live_root.mkdir(parents=True, exist_ok=True)
    assert not ShowStore(".").delete()
    assert live_root.is_dir()


# -- layout ------------------------------------------------------------


def test_downloads_never_land_in_the_project_folder() -> None:
    """A copy of the show's music inside the project would travel to whoever
    the project is handed to next."""
    from tracyy.core.paths import cache_root, data_root

    assert cache_root() != data_root()
    assert data_root() not in store_module.live_root().parents


def test_media_peaks_and_document_are_separable() -> None:
    store = ShowStore("abc")
    store.ensure()
    assert store.media_dir.parent == store.root
    assert store.peaks_dir.parent == store.root
    assert store.media_dir != store.peaks_dir


def test_the_journal_lives_beside_the_document_not_inside_it() -> None:
    """The journal is append-only history and document.json a future compact
    snapshot; two artefacts with two lifetimes must be two files, both inside
    the folder that delete() measures and removes as one thing."""
    store = ShowStore("abc")
    assert store.journal_path.parent == store.root
    assert store.journal_path != store.document_path
    assert store.journal_path.name == "journal.ndjson"

"""The presence layer on the waveform: what it draws, and what it refuses to.

Pixel assertions rather than method-call assertions. The thing that matters is
whether another machine's playhead ends up on screen at the right place, and a
test that only checks a list was stored would keep passing through a paint path
that never runs.
"""

from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("TRACYY_WAVEFORM_SOFTWARE", "1")
os.environ.setdefault("TRACYY_GPU_TIMELINE", "0")

import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QTableWidget, QTableWidgetItem

from tracyy.sync.model import PEER_COLOURS, colour_on_dark
from tracyy.widgets.cue_widgets import CUE_COLUMN_KEYS, CueTablePresenceOverlay
from tracyy.widgets.waveform import WaveformWidget

PINK = colour_on_dark(PEER_COLOURS[1])


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def wave(app):
    widget = WaveformWidget()
    widget.resize(600, 200)
    samples = (0.6 * np.ones(4000)).astype(np.float32)
    widget.set_track("Bài thử", samples)
    widget.set_progress(0.5, False)
    return widget


#: How near a rendered pixel has to be to the wanted colour to count as it.
#: A 1.1px line lands between two columns and antialiases, so the brightest
#: pixel of a drawn mark sits around 58 away; the background and the grey
#: waveform are never nearer than 120.
DRAWN = 75


def nearest(widget: WaveformWidget, fraction: float, wanted: str) -> int:
    """How close the rendered widget gets to ``wanted`` around ``fraction``.

    A band rather than one column, and the whole height including the dot on
    top: the mark is allowed to move a pixel as the widget resizes without
    turning this into a test about rounding.
    """
    image = widget.grab().toImage()
    rect = widget.wave_rect()
    centre = int(rect.left() + rect.width() * fraction)
    target = (int(wanted[1:3], 16), int(wanted[3:5], 16), int(wanted[5:7], 16))

    best = 255
    for x in range(max(0, centre - 3), min(image.width(), centre + 4)):
        for y in range(int(rect.top()) + 2, int(rect.bottom()) - 2):
            found = image.pixelColor(x, y).getRgb()[:3]
            distance = max(abs(a - b) for a, b in zip(found, target, strict=True))
            best = min(best, distance)
    return best


# -- the colour rule ---------------------------------------------------------


def test_colour_on_dark_lifts_without_changing_the_hue() -> None:
    lifted = colour_on_dark("#4C8DF6")
    assert lifted != "#4C8DF6"
    red, green, blue = (int(lifted[i : i + 2], 16) for i in (1, 3, 5))
    assert red > 0x4C and green > 0x8D and blue > 0xF6 - 1
    # Blue still dominates; a lift that flattened the hue would make every
    # peer the same pale grey and the whole colour scheme pointless.
    assert blue > green > red


def test_colour_on_dark_hands_back_nonsense_unchanged() -> None:
    for value in ("", "not a colour", "#12345", None):
        assert colour_on_dark(value) == (value or "")


# -- what reaches the widget -------------------------------------------------


def test_identical_presence_does_not_repaint(wave: WaveformWidget) -> None:
    """The network tick calls this whether or not anybody moved."""
    marks = [{"colour": PINK, "name": "Linh", "position": 0.3}]
    wave.set_peer_presence(marks)

    repaints = []
    wave.update = lambda *args: repaints.append(1)  # type: ignore[method-assign]
    wave.set_peer_presence([dict(marks[0])])
    assert repaints == []

    wave.set_peer_presence([{**marks[0], "position": 0.31}])
    assert repaints == [1]


def test_a_position_that_is_not_a_number_is_dropped(wave: WaveformWidget) -> None:
    wave.set_peer_presence(
        [
            {"colour": PINK, "name": "Hỏng", "position": float("nan")},
            {"colour": PINK, "name": "Sai", "position": "gần cuối"},
            {"colour": PINK, "name": "Được", "position": 0.4},
        ]
    )
    assert [name for _colour, name, _position in wave._peer_marks] == ["Được"]


def test_a_new_position_is_glided_to_not_jumped_to(wave: WaveformWidget) -> None:
    """Positions land once a beat; drawn raw, a playhead hops and reads as a fault."""
    wave.set_peer_presence([{"id": "m1", "colour": PINK, "name": "Linh", "position": 0.10}])
    assert wave._peer_marks[0][2] == pytest.approx(0.10)

    wave.set_peer_presence([{"id": "m1", "colour": PINK, "name": "Linh", "position": 0.115}])
    drawn = wave._peer_marks[0][2]
    assert drawn < 0.115, "jumped straight to the new position"
    assert drawn >= 0.10

    # And it does close the gap as time passes. Real time, because the whole
    # point of the blend is that it is measured in seconds rather than frames:
    # a busy machine drawing a show catches up at the same rate as an idle one.
    time.sleep(0.12)
    wave._advance_peer_motion()
    assert wave._peer_marks[0][2] > drawn


def test_a_seek_is_shown_at_once(wave: WaveformWidget) -> None:
    """Sliding across half a song would draw them passing through it."""
    wave.set_peer_presence([{"id": "m1", "colour": PINK, "name": "Linh", "position": 0.10}])
    wave.set_peer_presence([{"id": "m1", "colour": PINK, "name": "Linh", "position": 0.70}])
    assert wave._peer_marks[0][2] == pytest.approx(0.70)


def test_a_machine_that_leaves_takes_its_mark_with_it(wave: WaveformWidget) -> None:
    wave.set_peer_presence([{"id": "m1", "colour": PINK, "name": "Linh", "position": 0.3}])
    wave.set_peer_presence([])
    assert wave._peer_marks == ()
    assert not wave._peer_timer.isActive()


# -- what ends up on screen --------------------------------------------------


def test_a_peer_draws_a_playhead_at_its_position(wave: WaveformWidget) -> None:
    assert nearest(wave, 0.30, PINK) > DRAWN

    wave.set_peer_presence([{"colour": PINK, "name": "Linh", "position": 0.30}])
    assert nearest(wave, 0.30, PINK) <= DRAWN


def test_clearing_presence_takes_the_playhead_away(wave: WaveformWidget) -> None:
    wave.set_peer_presence([{"colour": PINK, "name": "Linh", "position": 0.30}])
    assert nearest(wave, 0.30, PINK) <= DRAWN

    wave.set_peer_presence([])
    assert nearest(wave, 0.30, PINK) > DRAWN


def test_a_peer_outside_the_view_is_not_drawn_at_the_edge(wave: WaveformWidget) -> None:
    """Zoomed in, somebody further along the song must not be pinned to the end.

    Drawing them at the boundary would be worse than not drawing them: it
    reads as a real position, and the person watching would seek to it.
    """
    wave.view_start, wave.view_span = 0.0, 0.5
    wave.set_peer_presence([{"colour": PINK, "name": "Linh", "position": 0.9}])

    for fraction in (0.0, 0.5, 0.98, 0.999):
        assert nearest(wave, fraction, PINK) > DRAWN


# -- the cue table ------------------------------------------------------------


CUE_IDS = ("4ce550-1", "4ce550-2", "4ce550-3")

#: A 1.5px solid outline lands nearly full strength on at least one pixel, so
#: this is tighter than the waveform's dashed 1.1px line needs.
OUTLINE = 45


@pytest.fixture
def table(app):
    """A cue table shaped like the real one: four columns, ids on every item."""
    widget = QTableWidget(len(CUE_IDS), len(CUE_COLUMN_KEYS))
    # The real table's colours, so a "nothing is drawn here" assertion is not
    # measured against Qt's default white — mid-grey antialiased text on white
    # sits closer to a pale peer colour than the peer colour does to the dark
    # background it is really drawn on.
    widget.setStyleSheet(
        "QTableWidget { background: #070A05; color: #E1E5DA; gridline-color: #243515; }"
        "QTableWidget::item { background: #0B1007; color: #E1E5DA; }"
    )
    widget.horizontalHeader().hide()
    widget.verticalHeader().hide()
    widget.verticalHeader().setDefaultSectionSize(40)
    widget.resize(520, 200)
    for row, cue_id in enumerate(CUE_IDS):
        for column, text in enumerate((f"00:00:0{row}:00", "Pyro", f"Cue {row:02}", "")):
            item = QTableWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, cue_id)
            widget.setItem(row, column, item)
    for column in range(len(CUE_COLUMN_KEYS)):
        widget.setColumnWidth(column, 130)
    return widget


def cell_holds(table: QTableWidget, row: int, column: int, wanted: str) -> bool:
    """Whether ``wanted`` is painted anywhere inside one cell."""
    image = table.grab().toImage()
    rect = table.visualItemRect(table.item(row, column))
    target = (int(wanted[1:3], 16), int(wanted[3:5], 16), int(wanted[5:7], 16))
    for x in range(rect.left(), rect.right()):
        for y in range(rect.top(), rect.bottom()):
            found = image.pixelColor(x, y).getRgb()[:3]
            if max(abs(a - b) for a, b in zip(found, target, strict=True)) <= OUTLINE:
                return True
    return False


def test_the_edited_cell_is_outlined(table: QTableWidget) -> None:
    overlay = CueTablePresenceOverlay(table)
    assert not cell_holds(table, 1, 3, PINK)

    overlay.set_marks([{"cue_id": CUE_IDS[1], "column": "label", "colour": PINK}])
    assert cell_holds(table, 1, 3, PINK)
    # Only that cell: the row beside it and the cue above it stay clean.
    assert not cell_holds(table, 1, 0, PINK)
    assert not cell_holds(table, 0, 3, PINK)


def test_the_outline_follows_the_cue_not_the_row(table: QTableWidget) -> None:
    """Every machine sorts and scrolls its own table.

    The same cue can sit on a different row here than it does there, so a row
    number on the wire would point at somebody else's cue.
    """
    overlay = CueTablePresenceOverlay(table)
    overlay.set_marks([{"cue_id": CUE_IDS[2], "column": "cue", "colour": PINK}])
    assert cell_holds(table, 2, 2, PINK)

    # That cue is now the first row, as a re-sort on this machine would leave it.
    for column in range(len(CUE_COLUMN_KEYS)):
        table.item(0, column).setData(Qt.ItemDataRole.UserRole, CUE_IDS[2])
        table.item(2, column).setData(Qt.ItemDataRole.UserRole, CUE_IDS[0])

    assert cell_holds(table, 0, 2, PINK)
    assert not cell_holds(table, 2, 2, PINK)


def test_a_cue_this_table_does_not_have_draws_nothing(table: QTableWidget) -> None:
    overlay = CueTablePresenceOverlay(table)
    overlay.set_marks([{"cue_id": "somebody-else-99", "column": "label", "colour": PINK}])
    for row in range(len(CUE_IDS)):
        for column in range(len(CUE_COLUMN_KEYS)):
            assert not cell_holds(table, row, column, PINK)


def test_an_unknown_column_draws_nothing(table: QTableWidget) -> None:
    overlay = CueTablePresenceOverlay(table)
    overlay.set_marks([{"cue_id": CUE_IDS[0], "column": "colour", "colour": PINK}])
    for column in range(len(CUE_COLUMN_KEYS)):
        assert not cell_holds(table, 0, column, PINK)


def test_the_overlay_never_takes_a_click(table: QTableWidget) -> None:
    overlay = CueTablePresenceOverlay(table)
    assert overlay.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    assert overlay.focusPolicy() == Qt.FocusPolicy.NoFocus


def test_identical_marks_do_not_repaint(table: QTableWidget) -> None:
    overlay = CueTablePresenceOverlay(table)
    mark = {"cue_id": CUE_IDS[0], "column": "time", "colour": PINK}
    overlay.set_marks([mark])

    repaints = []
    overlay.update = lambda *args: repaints.append(1)  # type: ignore[method-assign]
    overlay.set_marks([dict(mark)])
    assert repaints == []

    overlay.set_marks([{**mark, "column": "label"}])
    assert repaints == [1]

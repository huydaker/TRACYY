"""Write-if-changed helpers for Qt widgets.

Qt's setters are unconditional: ``QLabel.setText`` with the text the label
already shows still invalidates the widget, schedules a repaint and, on
editable widgets, re-emits change signals. V1's 66 Hz refresh called about
fifteen of them every tick, so most of the interface was repainted a million
times a show to display exactly the same characters.

Every helper here compares first and returns whether it actually wrote, so
callers can also skip the work that would have followed.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QSignalBlocker

__all__ = [
    "ValueCache",
    "set_checked",
    "set_enabled",
    "set_line_text",
    "set_property",
    "set_style_sheet",
    "set_text",
    "set_tooltip",
    "set_value",
    "set_visible",
]


def set_text(widget: Any, text: str) -> bool:
    """``widget.setText(text)`` only when the text differs."""
    if widget is None:
        return False
    text = str(text)
    try:
        if widget.text() == text:
            return False
    except (AttributeError, RuntimeError):
        return False
    widget.setText(text)
    return True


def set_line_text(widget: Any, text: str) -> bool:
    """Same as :func:`set_text`, with change signals suppressed.

    For ``QLineEdit``/``QPlainTextEdit`` fields the application writes into
    while the user may also be editing them.
    """
    if widget is None:
        return False
    text = str(text)
    try:
        if widget.text() == text:
            return False
    except (AttributeError, RuntimeError):
        return False
    with QSignalBlocker(widget):
        widget.setText(text)
    return True


def set_tooltip(widget: Any, text: str) -> bool:
    if widget is None:
        return False
    text = str(text)
    try:
        if widget.toolTip() == text:
            return False
        widget.setToolTip(text)
    except (AttributeError, RuntimeError):
        return False
    return True


def set_enabled(widget: Any, enabled: bool) -> bool:
    if widget is None:
        return False
    enabled = bool(enabled)
    try:
        if widget.isEnabled() == enabled:
            return False
        widget.setEnabled(enabled)
    except (AttributeError, RuntimeError):
        return False
    return True


def set_visible(widget: Any, visible: bool) -> bool:
    """Avoid the show/hide relayout when visibility is already correct.

    ``isVisible()`` is false for a widget whose parent is hidden, so the
    explicitly-hidden flag is what gets compared.
    """
    if widget is None:
        return False
    visible = bool(visible)
    try:
        if widget.isHidden() != visible:
            return False
        widget.setVisible(visible)
    except (AttributeError, RuntimeError):
        return False
    return True


def set_checked(widget: Any, checked: bool) -> bool:
    if widget is None:
        return False
    checked = bool(checked)
    try:
        if widget.isChecked() == checked:
            return False
        with QSignalBlocker(widget):
            widget.setChecked(checked)
    except (AttributeError, RuntimeError):
        return False
    return True


def set_value(widget: Any, value: int | float) -> bool:
    """Sliders and progress bars, without re-emitting ``valueChanged``."""
    if widget is None:
        return False
    try:
        if widget.value() == value:
            return False
        with QSignalBlocker(widget):
            widget.setValue(value)
    except (AttributeError, RuntimeError):
        return False
    return True


def set_style_sheet(widget: Any, style: str) -> bool:
    """Re-applying a style sheet forces a full style re-polish — the single
    most expensive no-op in the V1 refresh path. Compare before writing."""
    if widget is None:
        return False
    style = str(style)
    try:
        if widget.styleSheet() == style:
            return False
        widget.setStyleSheet(style)
    except (AttributeError, RuntimeError):
        return False
    return True


def set_property(widget: Any, name: str, value: Any, *, repolish: bool = True) -> bool:
    """Set a dynamic property used by QSS selectors, repolishing on change.

    Cheaper than swapping whole style sheets: the sheet is parsed once and
    only the affected widget is re-polished.
    """
    if widget is None:
        return False
    try:
        if widget.property(name) == value:
            return False
        widget.setProperty(name, value)
        if repolish:
            style = widget.style()
            style.unpolish(widget)
            style.polish(widget)
            widget.update()
    except (AttributeError, RuntimeError):
        return False
    return True


class ValueCache:
    """Remembers the last value written under a key.

    For updates whose *inputs* are cheap to compute but whose *effect* is
    expensive — rebuilding a JSON snapshot, re-sorting a table, repainting a
    waveform — guard the effect with this instead of a widget comparison.
    """

    __slots__ = ("_values",)

    def __init__(self) -> None:
        self._values: dict[str, Any] = {}

    def changed(self, key: str, value: Any) -> bool:
        """True when ``value`` differs from the last call for ``key``."""
        previous = self._values.get(key, _MISSING)
        if previous is not _MISSING and previous == value:
            return False
        self._values[key] = value
        return True

    def apply(self, key: str, value: Any, action: Callable[[Any], None]) -> bool:
        if not self.changed(key, value):
            return False
        action(value)
        return True

    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)

    def invalidate(self, key: str | None = None) -> None:
        if key is None:
            self._values.clear()
        else:
            self._values.pop(key, None)


class _Missing:
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<missing>"


_MISSING = _Missing()

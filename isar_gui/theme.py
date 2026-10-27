"""The GUI's one vocabulary of status dots and words.

A status is always drawn as ``dot_icon(tone)`` (the item's decoration) plus the word as text, never
as colour alone. The word is the scan-list read's ``Status.word``, unchanged; this module only decides
the tone of a word. ``TONE_COLOR`` is the only place a status colour is spelled.

The words come from the stored records: the fast pass's count states
(``isar/analyze/fast_pass.py``), the deep pass's states (``isar/session/deep_delivery.py``) and the
results writer's ``NOT_FITTED`` / ``NOT_RECORDED``. A word missing from the table is never renamed or
hidden: it shows as stored with the IDLE tone. There is no "running" tone, because no process reports
a live state.
"""
from __future__ import annotations

from enum import Enum

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap

# The GUI may import only isar.workspace_api (G16): the two writer words come via the scan-list read.
from isar.workspace_api.scan_list import (HELD, NOT_FITTED, NOT_READ, NOT_RECORDED, NOT_RUN,
                                          PARSED_OK)


class Tone(str, Enum):
    OK = "ok"
    WARN = "warn"
    ERR = "err"
    IDLE = "idle"


TONE_COLOR: dict[Tone, str] = {Tone.OK: "#2e7d4f", Tone.WARN: "#b7791f",
                               Tone.ERR: "#b42318", Tone.IDLE: "#9aa1ab"}   # the mockup's palette

PARSED, FAST, DEEP = "parsed", "fast", "deep"          # the three status columns

# Words the fitting code stores (fast: CountState; deep: deep-pass states). Spelled once, here.
_RESOLVED, _CLOSE_CALL, _UNRESOLVED, _SEARCH_LIMITED, _REFUSED = (
    "resolved", "close_call", "unresolved", "search_limited", "refused")

_FAST_TONES: dict[str, Tone] = {
    _RESOLVED: Tone.OK,
    _CLOSE_CALL: Tone.WARN, _UNRESOLVED: Tone.WARN, _SEARCH_LIMITED: Tone.WARN, HELD: Tone.WARN,
    NOT_FITTED: Tone.ERR,
    NOT_RECORDED: Tone.IDLE, NOT_RUN: Tone.IDLE,
}
_DEEP_TONES: dict[str, Tone] = {
    _RESOLVED: Tone.OK,
    _CLOSE_CALL: Tone.WARN, _SEARCH_LIMITED: Tone.WARN,
    _REFUSED: Tone.ERR,
    NOT_RUN: Tone.IDLE,
}
_SEVERITY = {Tone.ERR: 3, Tone.WARN: 2, Tone.IDLE: 1, Tone.OK: 0}   # worst first

_ICONS: dict[Tone, QIcon] = {}


def status_tone(column: str, word: str) -> Tone:
    """The tone of ``word`` in ``column`` (PARSED, FAST or DEEP). Unknown words are IDLE."""
    if column == PARSED:
        if word == PARSED_OK:
            return Tone.OK
        return Tone.ERR if word == NOT_READ else Tone.WARN   # any other word is a gap: "no Q"
    if column == FAST:
        return _FAST_TONES.get(word, Tone.IDLE)
    if column == DEEP:
        return _DEEP_TONES.get(word, Tone.IDLE)
    return Tone.IDLE


def summary_tone(tones) -> Tone:
    """The worst of ERR > WARN > IDLE > OK; an empty collection is IDLE."""
    worst = None
    for tone in tones:
        if worst is None or _SEVERITY[tone] > _SEVERITY[worst]:
            worst = tone
    return Tone.IDLE if worst is None else worst


def dot_icon(tone: Tone) -> QIcon:
    """A 10 px filled circle in the tone's colour, cached per tone."""
    icon = _ICONS.get(tone)
    if icon is None:
        pix = QPixmap(10, 10)
        pix.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(TONE_COLOR[tone]))
        painter.drawEllipse(QRectF(1, 1, 8, 8))
        painter.end()
        icon = _ICONS[tone] = QIcon(pix)
    return icon

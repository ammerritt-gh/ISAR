"""The GUI's one vocabulary of status dots and words.

A status is always drawn as ``dot_icon(tone)`` (the item's decoration) plus the word as text, never
as colour alone. The tone is decided from the scan-list read's ``Status.word`` (the fitting code's word); the
text shown is its plain word from :mod:`isar_gui.status_words`. This module only decides the tone of a word. ``TONE_COLOR`` is the only place a status colour is spelled.

The words come from the stored records: the fast pass's count states
(``isar/analyze/fast_pass.py``), the deep pass's states (``isar/session/deep_delivery.py``) and the
results writer's ``NOT_FITTED`` / ``NOT_RECORDED``. A word missing from the table is never renamed or
hidden: it shows as stored with the IDLE tone. The stored passes have no "running" tone, because a record
holds no live state; ``Tone.ACTIVE`` belongs to the analysis queue's live work alone (``QUEUE_STATES``)
and is never a settled status.
"""
from __future__ import annotations

from collections.abc import Mapping
from enum import Enum

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap

# The GUI may import only isar.workspace_api (G16): the two writer words come via the scan-list read.
from isar.workspace_api.scan_list import (HELD, NOT_FITTED, NOT_READ, NOT_RECORDED, NOT_RUN,
                                          PARSED_OK)


class Tone(str, Enum):
    OK = "ok"
    WARN = "warn"
    ERR = "err"
    IDLE = "idle"
    ACTIVE = "active"           # live queue work (running, saving); never a settled status (core law 4)


TONE_COLOR: dict[Tone, str] = {Tone.OK: "#2e7d4f", Tone.WARN: "#b7791f", Tone.ERR: "#b42318",
                               Tone.IDLE: "#9aa1ab", Tone.ACTIVE: "#2b6cb0"}   # the mockup's palette

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
_SEVERITY = {Tone.ERR: 4, Tone.WARN: 3, Tone.ACTIVE: 2, Tone.IDLE: 1, Tone.OK: 0}   # worst first

# The analysis queue's live words for the panel and the scan list's marks: status -> (word, tone, what it
# means, as the hover). "queued" is the scan list's word for an item the panel calls "waiting".
QUEUE_STATES: Mapping[str, tuple[str, Tone, str]] = {
    "waiting": ("waiting", Tone.IDLE, "Added; starts when cores free up, fast passes first"),
    "queued": ("queued", Tone.IDLE,
               "In the analysis queue for this pass; starts when cores free up, fast passes first"),
    "running": ("running", Tone.ACTIVE, "ISAR is fitting this analyzer file now"),
    "saving": ("saving", Tone.ACTIVE, "Finished; ISAR is writing the result into the experiment folder"),
    "done": ("done", Tone.OK, "Finished and saved in the experiment folder"),
    "done_warning": ("done with a warning", Tone.WARN,
                     "Finished and saved, but ISAR reports a limit on this result; the reason is shown beside it"),
    "held": ("held", Tone.WARN,
             "ISAR did not fit this file because something it needs is missing; the reason says what. "
             "Once that is fixed, add the file again"),
    "failed": ("failed", Tone.ERR,
               "The fit or the save went wrong; the reason says what. Add the file again to retry"),
    "stopped": ("stopped", Tone.IDLE, "Stopped by you before it finished; anything already saved stays"),
}

_ICONS: dict[Tone, QIcon] = {}
_STATE_ICONS: dict[bool, QIcon] = {}


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
    """The worst of ERR > WARN > ACTIVE > IDLE > OK; an empty collection is IDLE."""
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


def state_icon(good: bool) -> QIcon:
    """A check (Tone.OK's colour) or a cross (Tone.ERR's) on a 12 px pixmap, cached per state. The shape
    carries the meaning and the colour only repeats it (core law 7); no literal colours, no image files."""
    icon = _STATE_ICONS.get(good)
    if icon is None:
        pix = QPixmap(12, 12)
        pix.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(TONE_COLOR[Tone.OK if good else Tone.ERR]), 2.0)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        if good:
            painter.drawPolyline([QPointF(2.0, 6.5), QPointF(5.0, 9.5), QPointF(10.0, 2.5)])
        else:
            painter.drawLine(QPointF(2.5, 2.5), QPointF(9.5, 9.5))
            painter.drawLine(QPointF(9.5, 2.5), QPointF(2.5, 9.5))
        painter.end()
        icon = _STATE_ICONS[good] = QIcon(pix)
    return icon

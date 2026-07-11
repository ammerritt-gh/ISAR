"""GUI session persistence for isar_gui.

Stores the recent-workspace list, the serialized dock layout, and an open-ended per-view state map
in a single JSON document. Saving is atomic (write a temp file, then ``os.replace``) and loading is
corruption-tolerant: a missing or unreadable/garbage file yields a default :class:`Session` rather
than raising, so a bad file never blocks startup.

Lifted from ACIDD's ``acidd/session.py`` (atomic save + tolerant load; C:/Users/AMM/Documents/
Github/ACIDD/acidd/session.py), trimmed to what the ISAR shell needs and given a debounced autosave
timer for the Qt side.

File location (platformdirs-free): ``%APPDATA%/isar_gui/session.json`` on Windows via
``os.environ["APPDATA"]``; where ``APPDATA`` is unset (Linux/CI) it falls back to
``~/.isar_gui/session.json``. Chosen to avoid a platformdirs dependency while still landing in the
per-user config area on the target (Windows) platform.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

SESSION_VERSION = 1
_MAX_RECENT = 12


@dataclass
class Session:
    """Persisted GUI state.

    ``recent_workspaces`` is a most-recent-first list of workspace directory paths.
    ``dock_layout_b64`` is the base64 of ``QMainWindow.saveState()`` (opaque here).
    ``view_state`` is an open-ended per-view scratch map (later view tasks own its shape).
    """

    recent_workspaces: list[str] = field(default_factory=list)
    dock_layout_b64: str | None = None
    view_state: dict = field(default_factory=dict)
    version: int = SESSION_VERSION

    def to_dict(self) -> dict:
        return asdict(self)

    def note_workspace(self, path) -> None:
        """Record ``path`` as the most-recent workspace (dedup, capped at ``_MAX_RECENT``)."""
        p = str(Path(path))
        self.recent_workspaces = [p] + [q for q in self.recent_workspaces if q != p]
        del self.recent_workspaces[_MAX_RECENT:]

    @classmethod
    def from_dict(cls, data: dict | None) -> "Session":
        """Build defensively from persisted data; bad/missing fields fall back to defaults."""
        data = data if isinstance(data, dict) else {}
        recent = [str(p) for p in (data.get("recent_workspaces") or []) if isinstance(p, str)]
        layout = data.get("dock_layout_b64")
        if layout is not None and not isinstance(layout, str):
            layout = None
        vs = data.get("view_state")
        return cls(
            recent_workspaces=recent[:_MAX_RECENT],
            dock_layout_b64=layout,
            view_state=vs if isinstance(vs, dict) else {},
            version=int(data.get("version", SESSION_VERSION)) if str(data.get("version", SESSION_VERSION)).isdigit() else SESSION_VERSION,
        )


def session_path() -> Path:
    """Per-user session file: ``%APPDATA%/isar_gui/session.json`` (else ``~/.isar_gui/...``)."""
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / ".isar_gui"
    if appdata:
        base = base / "isar_gui"
    return base / "session.json"


def load_session(path: Path | None = None) -> Session:
    """Load the session, returning a default :class:`Session` if missing or unreadable."""
    path = Path(path) if path is not None else session_path()
    if not path.exists():
        return Session()
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return Session.from_dict(data)
    except Exception:  # noqa: BLE001 — corrupt/unreadable session must never block startup
        return Session()


def save_session(session: Session, path: Path | None = None) -> None:
    """Write the session atomically (temp file + ``os.replace``)."""
    path = Path(path) if path is not None else session_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(session.to_dict(), f, indent=2)
    os.replace(tmp, path)


class Autosaver:
    """Debounced session autosave for the Qt side (500ms single-shot, coalescing).

    Qt is imported lazily in ``__init__`` so the pure save/load API above stays Qt-free (its tests
    need no ``QApplication``). ``request()`` (re)arms the timer; the snapshot callback runs on the
    Qt thread when it fires, so the window can hand back its current :class:`Session` at save time.
    """

    def __init__(self, snapshot, path: Path | None = None, *, delay_ms: int = 500, parent=None):
        from PySide6.QtCore import QTimer  # lazy: keep module import Qt-free

        self._snapshot = snapshot
        self._path = path
        self._timer = QTimer(parent)
        self._timer.setSingleShot(True)
        self._timer.setInterval(delay_ms)
        self._timer.timeout.connect(self._flush)

    def request(self) -> None:
        """Schedule a save ``delay_ms`` from now; further requests within the window coalesce."""
        self._timer.start()

    def flush_now(self) -> None:
        """Save immediately (used on window close), cancelling any pending debounce."""
        self._timer.stop()
        self._flush()

    def _flush(self) -> None:
        save_session(self._snapshot(), self._path)

"""GUI session persistence for isar_gui.

Stores the recent-workspace list, the serialized dock layout, and an open-ended per-view state map
in a single JSON document. Saving is atomic (write a temp file, then ``os.replace``) and loading is
corruption-tolerant: a missing or unreadable/garbage file yields a default :class:`Session` rather
than raising, so a bad file never blocks startup.

Lifted from ACIDD's ``acidd/session.py`` (atomic save + tolerant load; C:/Users/AMM/Documents/
Github/Science/ACIDD/acidd/session.py), trimmed to what the ISAR shell needs and given a debounced autosave
timer for the Qt side.

File location (platformdirs-free): ``%APPDATA%/isar_gui/session.json`` on Windows via
``os.environ["APPDATA"]``; where ``APPDATA`` is unset (Linux/CI) it falls back to
``~/.isar_gui/session.json``. Chosen to avoid a platformdirs dependency while still landing in the
per-user config area on the target (Windows) platform.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from isar_gui.credentials import CredentialError, store_api_key, supported
from isar_gui.health import LLM_DEFAULT_ENDPOINT

SESSION_VERSION = 1
_MAX_RECENT = 12
log = logging.getLogger(__name__)


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
    # Transient only: a credential migration failed, so the on-disk legacy key must be retained
    # until the operator has a chance to repair it.  It is deliberately not part of the JSON schema.
    migration_warnings: list[str] = field(default_factory=list, repr=False)
    _retain_legacy_llm_key: bool = field(default=False, repr=False)

    def to_dict(self) -> dict:
        data = asdict(self)
        data.pop("migration_warnings", None)
        data.pop("_retain_legacy_llm_key", None)
        settings = data.get("view_state", {}).get("settings")
        # A successful migration (and every newly-created session) can never write a plaintext key
        # back to disk.  The one exception is an unsuccessful migration: removing it then would be
        # a silent loss of the operator's key, so retain it alongside an explicit warning instead.
        if isinstance(settings, dict) and not self._retain_legacy_llm_key:
            settings.pop("llm_api_key", None)
        return data

    def note_workspace(self, path) -> None:
        """Record ``path`` as the most-recent workspace (dedup, capped at ``_MAX_RECENT``).

        A path under the system temp directory is a transient scratch/test workspace and is refused:
        recording it would let a later no-arg launch auto-open a dead scratch directory instead of
        the empty-start surface (the class of bug where a pytest ``tmp_path`` bind polluted the real
        recents list). The skip is logged rather than silent.
        """
        p = str(Path(path))
        try:
            if Path(path).resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()):
                log.info("not recording transient workspace under temp dir as a recent: %s", p)
                return
        except OSError as exc:
            # An unresolvable path is not worth failing the record over; log and fall through to
            # store the literal path (a later launch will skip it if it is still unopenable).
            log.warning("could not resolve workspace path %s for temp-dir check: %s", p, exc)
        self.recent_workspaces = [p] + [q for q in self.recent_workspaces if q != p]
        del self.recent_workspaces[_MAX_RECENT:]

    def rename_workspace(self, old, new) -> None:
        """An experiment folder was renamed: follow it in the recents and the remembered Run dialog.

        ``old`` is replaced by ``new`` in place (its position in the recents is kept; an older entry
        already equal to ``new`` is dropped). ``view_state["run"][old]`` moves to ``new``. An ``old``
        that was never recorded (a transient one) is not added.
        """
        old_s, new_s = str(Path(old)), str(Path(new))
        recent: list[str] = []
        for q in self.recent_workspaces:
            q = new_s if str(Path(q)) == old_s else q
            if q not in recent:
                recent.append(q)
        self.recent_workspaces = recent
        runs = self.view_state.get("run")
        if isinstance(runs, dict) and old_s in runs:
            runs[new_s] = runs.pop(old_s)

    @classmethod
    def from_dict(cls, data: dict | None) -> "Session":
        """Build defensively from persisted data; bad/missing fields fall back to defaults."""
        data = data if isinstance(data, dict) else {}
        recent = [str(p) for p in (data.get("recent_workspaces") or []) if isinstance(p, str)]
        layout = data.get("dock_layout_b64")
        if layout is not None and not isinstance(layout, str):
            layout = None
        vs = data.get("view_state")
        session = cls(
            recent_workspaces=recent[:_MAX_RECENT],
            dock_layout_b64=layout,
            view_state=vs if isinstance(vs, dict) else {},
            version=int(data.get("version", SESSION_VERSION)) if str(data.get("version", SESSION_VERSION)).isdigit() else SESSION_VERSION,
        )
        settings = session.view_state.get("settings")
        if isinstance(settings, dict) and isinstance(settings.get("llm_api_key"), str):
            session._retain_legacy_llm_key = True
        return session


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
        session = Session.from_dict(data)
        migrate_legacy_llm_key(session, path)
        return session
    except Exception as exc:  # noqa: BLE001 — corrupt/unreadable session must never block startup
        log.error("could not load GUI session %s: %s", path, exc)
        return Session()


def save_session(session: Session, path: Path | None = None) -> None:
    """Write the session atomically (temp file + ``os.replace``)."""
    path = Path(path) if path is not None else session_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Per-pid tmp name so two GUI instances autosaving concurrently don't clobber each other's
    # temp file before the atomic replace (the final path is shared, but os.replace is atomic).
    tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(session.to_dict(), f, indent=2)
    os.replace(tmp, path)


def migrate_legacy_llm_key(session: Session, path: Path | None = None) -> bool:
    """Move a legacy plaintext session key into Credential Manager before scrubbing the JSON.

    The order is intentional: credential write first, then an atomic session rewrite.  Any failure
    restores the in-memory legacy field and leaves the existing JSON untouched, with a warning that
    MainWindow surfaces to the operator.  On non-Windows systems no secure persistence is available,
    so the plaintext is retained rather than silently discarded.
    """
    settings = session.view_state.get("settings")
    if not isinstance(settings, dict):
        return True
    api_key = settings.get("llm_api_key")
    if not isinstance(api_key, str) or not api_key:
        if "llm_api_key" in settings:
            settings.pop("llm_api_key", None)
            session._retain_legacy_llm_key = False
        return True
    endpoint = settings.get("llm_endpoint") or LLM_DEFAULT_ENDPOINT
    if not supported():
        _add_migration_warning(
            session,
            "An LLM API key remains in the GUI session because secure credential storage is only "
            "available on Windows. Remove it or configure the key through the environment.",
        )
        return False
    try:
        store_api_key(endpoint, api_key)
    except (CredentialError, ValueError) as exc:
        log.error("could not migrate legacy LLM API key to Credential Manager: %s", exc)
        _add_migration_warning(
            session,
            "Could not move the LLM API key into Windows Credential Manager; the old plaintext "
            "session value was retained. Fix the credential-store problem before closing ISAR.",
        )
        return False

    old_key = settings.pop("llm_api_key")
    session._retain_legacy_llm_key = False
    try:
        save_session(session, path)
    except Exception as exc:  # noqa: BLE001 — do not lose the source secret after a vault write
        settings["llm_api_key"] = old_key
        session._retain_legacy_llm_key = True
        log.error("credential stored but could not scrub legacy GUI session key: %s", exc)
        _add_migration_warning(
            session,
            "The LLM key was stored in Windows Credential Manager, but ISAR could not remove the "
            "old plaintext session value. Check the GUI session file before closing ISAR.",
        )
        return False
    log.info("migrated legacy GUI LLM API key into Windows Credential Manager")
    return True


def _add_migration_warning(session: Session, message: str) -> None:
    if message not in session.migration_warnings:
        session.migration_warnings.append(message)
    session._retain_legacy_llm_key = True


class Autosaver:
    """Debounced session autosave for the Qt side (500ms single-shot, coalescing).

    Qt is imported lazily in ``__init__`` so the pure save/load API above stays Qt-free (its tests
    need no ``QApplication``). ``request()`` (re)arms the timer; the snapshot callback runs on the
    Qt thread when it fires, so the window can hand back its current :class:`Session` at save time.
    """

    def __init__(self, snapshot, path: Path | None = None, *, delay_ms: int = 500, parent=None,
                 on_error=None):
        from PySide6.QtCore import QTimer  # lazy: keep module import Qt-free

        self._snapshot = snapshot
        self._path = path
        self._on_error = on_error
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
        # A snapshot/serialize/write failure must never propagate out of the Qt timer callback (it
        # would tear down the event loop). Report it via on_error (else stderr) and swallow it.
        try:
            save_session(self._snapshot(), self._path)
        except Exception as exc:  # noqa: BLE001 — autosave failure is non-fatal, but never silent
            msg = f"session autosave failed: {exc}"
            if self._on_error is not None:
                self._on_error(msg)
            else:
                print(msg, file=sys.stderr)

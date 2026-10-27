"""IntakeState -- the one Qt-side holder of the BL43 intake reads and writers.

Every intake read the GUI shows and every intake save it makes goes through one ``IntakeState``, so
one re-read after each save keeps every panel on the same snapshot ("what the user sees is what ISAR
uses"). The reads are the backend's own contracts, returned unchanged:

* on the GUI thread, from a fresh ``Workspace(root)`` per reload: ``fact_rows``, ``conditions``,
  ``summary`` (``import_summary``) and ``settings`` (``current_settings``), then ``changed``;
* on a worker thread: ``fit_inputs`` and ``attention`` (``needs_attention``), which parse the
  fit-inputs log and so cost about a third of a second each; then ``worker_changed``.

The worker follows :mod:`isar_gui.viewmodels.sandbox`: one thread at a time, the newest request
pending, results through a queue polled on the GUI thread, no Qt on the worker. Each reload takes a
generation number and a worker result from an older generation (or another root) is dropped, so a
read made before a save is never shown after it.

The writers are called in process, synchronously on the GUI thread, as the intake backend allows
(each is one locked append bounded at 2 s). Every save is refused while an export runs, and re-reads
after it returns, ok or not.
"""
from __future__ import annotations

import gc
import logging
import queue
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

from isar.intake import settings as intake_settings
from isar.intake import values as intake_values
from isar.intake.settings import SaveResult
from isar.workspace_api import Workspace, import_summary, needs_attention

log = logging.getLogger(__name__)

NO_EXPERIMENT = "No experiment open"
_POLL_MS = 50


@dataclass(frozen=True)
class SaveOutcome:
    ok: bool
    message: str                # on ok: "Saved: 100 K on 2 scan sets"; else the backend's exact reason
    result: SaveResult | None   # the backend's SaveResult, unchanged; None if refused before the call or it raised


def _n(count: int, one: str, many: str | None = None) -> str:
    return f"{count} {one if count == 1 else (many or one + 's')}"


def _number(value) -> str:
    return f"{value:g}" if isinstance(value, float) else str(value)


_live: list[threading.Thread] = []   # intake workers not yet seen ended (GUI thread only)
_gc_was_enabled = False


def _pause_collector(thread: threading.Thread) -> None:
    """Turn the cyclic collector off before a worker starts (GUI thread). A collection run on the worker
    could destroy unreachable Qt objects off the GUI thread, which deadlocked the GUI tests. Re-enabling it
    on the worker was not enough: the worker's next allocation then ran the whole backlog there (seen as
    workers parked at that point in hung xdist runs)."""
    global _gc_was_enabled
    _resume_collector()
    if not _live:
        _gc_was_enabled = gc.isenabled()
        gc.disable()
    _live.append(thread)


def _resume_collector() -> None:
    """Turn the collector back on once every worker thread has ended (GUI thread), so the backlog runs here."""
    global _gc_was_enabled
    _live[:] = [t for t in _live if t.is_alive()]
    if not _live and _gc_was_enabled:
        _gc_was_enabled = False
        gc.enable()


def _work(results: queue.SimpleQueue, root: Path, generation: int) -> None:
    """The worker thread's reads (NO Qt access, the collector off for its whole life); the answer goes back
    through ``results``."""
    try:
        # shortcut: the worker parses the fit-inputs log twice per reload (once in fit_inputs, once inside
        # needs_attention); ask the intake job for a needs_attention that takes the fit-inputs read if a
        # reload during a run is seen to lag.
        fit_inputs = tuple(Workspace(root).fit_inputs())
        attention = needs_attention(root)
        item = (generation, root, attention, fit_inputs, None)
    except Exception as exc:  # noqa: BLE001 - reported on the GUI thread as `error`
        log.exception("intake worker read of %s failed", root)
        item = (generation, root, None, None, f"worker: {type(exc).__name__}: {exc}")
    results.put(item)


class IntakeState(QObject):
    changed = Signal()                 # after the GUI-thread reads are swapped in (bind, reload, after a save)
    worker_changed = Signal()          # after the worker's reads (attention, fit_inputs) are swapped in together
    saved = Signal(str)                # SaveOutcome.message of every save, ok or not, for the Log

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._root: Path | None = None
        self._fact_rows = self._conditions = self._summary = self._settings = None
        self._attention = self._fit_inputs = None
        self._error: str | None = None
        self._busy: str | None = None
        self._generation = 0            # bumped by every reload; a worker result carries the one it served
        self._active = False            # exactly one worker thread at a time, until it is seen ended
        self._thread: threading.Thread | None = None
        self._pending: tuple[Path, int] | None = None   # the newest request not yet started
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._timer = QTimer(self)
        self._timer.setInterval(_POLL_MS)
        self._timer.timeout.connect(self._poll)

    # ---- binding and reading -----------------------------------------------------------
    def bind(self, root: Path | None) -> None:
        """The experiment bound (or ``None``: unbound, every read emptied); re-reads at once."""
        root = None if root is None else Path(root)
        if root != self._root:
            self._attention = self._fit_inputs = None     # another experiment's marks are never shown
        self._root = root
        self.reload()

    def reload(self) -> None:
        """Read the GUI-thread reads now and emit ``changed``; ask the worker for the other two."""
        self._generation += 1
        root = self._root
        self._error = None
        if root is None:
            self._fact_rows = self._conditions = self._summary = self._settings = None
            self._attention = self._fit_inputs = None
            self._pending = None
            self.changed.emit()
            return
        ws = self._read("workspace", lambda: Workspace(root))   # a fresh parse cache, never the window's
        self._fact_rows = None if ws is None else self._read("fact_rows", lambda: tuple(ws.fact_rows()))
        self._conditions = None if ws is None else self._read("conditions", lambda: tuple(ws.conditions()))
        self._summary = self._read("import_summary", lambda: import_summary(root))
        self._settings = self._read("current_settings", lambda: intake_settings.current_settings(root))
        self.changed.emit()
        self._pending = (root, self._generation)
        self._start_pending()
        self._timer.start()

    def _read(self, what: str, call: Callable):
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 - a read failure is a shown state, never a crash
            log.exception("intake read %s of %s failed", what, self._root)
            self._error = f"{what}: {type(exc).__name__}: {exc}"
            return None

    def _start_pending(self) -> None:
        if self._active or self._pending is None:
            return
        root, generation = self._pending
        self._pending = None
        self._active = True
        # The thread gets the queue, never `self`: a last reference dropped on the worker would destroy
        # this QObject (and its timer) off the GUI thread.
        self._thread = threading.Thread(target=_work, args=(self._queue, root, generation), daemon=True,
                                        name="isar-gui-intake")
        _pause_collector(self._thread)
        self._thread.start()

    # ---- GUI thread ----
    def _poll(self) -> None:
        latest = None
        ended = self._active and not self._thread.is_alive()   # read first: an ended worker's answer is queued
        try:
            while True:
                item = self._queue.get_nowait()
                if item[0] == self._generation and item[1] == self._root:
                    latest = item       # older generations are dropped; the pending request delivers instead
        except queue.Empty:
            pass
        if latest is not None:
            _gen, _root, self._attention, self._fit_inputs, error = latest
            if error is not None:
                self._error = error
            self.worker_changed.emit()
        if ended:
            self._active = False
            _resume_collector()
        self._start_pending()
        if not self._active and self._pending is None:
            self._timer.stop()

    @property
    def root(self) -> Path | None:
        return self._root

    @property
    def fact_rows(self):
        return self._fact_rows

    @property
    def conditions(self):
        return self._conditions

    @property
    def summary(self):
        return self._summary

    @property
    def settings(self):
        return self._settings

    @property
    def attention(self):
        return self._attention

    @property
    def fit_inputs(self):
        return self._fit_inputs

    @property
    def error(self) -> str | None:
        return self._error

    def busy_reason(self) -> str | None:
        return self._busy

    def set_busy_reason(self, reason: str | None) -> None:
        self._busy = reason or None

    # ---- the writers -------------------------------------------------------------------
    def _save(self, what: str, call: Callable[[Path], SaveResult],
              sentence: Callable[[SaveResult], str]) -> SaveOutcome:
        if self._busy is not None:
            outcome = SaveOutcome(False, self._busy, None)
        elif self._root is None:
            outcome = SaveOutcome(False, NO_EXPERIMENT, None)
        else:
            try:
                result = call(self._root)
            except Exception as exc:  # noqa: BLE001 - a programmer error, shown and logged, never swallowed
                log.exception("intake save (%s) raised", what)
                outcome = SaveOutcome(False, f"{type(exc).__name__}: {exc}", None)
            else:
                outcome = SaveOutcome(result.ok, sentence(result) if result.ok else str(result.reason), result)
            self.saved.emit(outcome.message)
            self.reload()               # ok or not: no panel keeps a belief the save may have changed
            return outcome
        self.saved.emit(outcome.message)
        return outcome

    def save_value(self, fact: str, targets: Sequence[str], value, reason: str | None) -> SaveOutcome:
        def sentence(r: SaveResult) -> str:
            shown = f"{_number(r.value)} K" if fact == "temperature" else f"slit {r.value} mm"
            return f"Saved: {shown} on {_n(len(r.targets), 'scan set')}"
        return self._save(f"{fact} value", lambda root: intake_values.save_value(
            root, fact, value, list(targets), reason=reason), sentence)

    def use_file_value(self, fact: str, targets: Sequence[str], reason: str | None) -> SaveOutcome:
        return self._save(f"{fact} file value", lambda root: intake_values.use_file_value(
            root, fact, list(targets), reason=reason),
            lambda r: f"Saved: the file's {fact} is used again on {_n(len(r.targets), 'scan set')}")

    def set_dead(self, targets: Sequence[str], dead: bool, reason: str | None) -> SaveOutcome:
        word = "dead" if dead else "in use"
        return self._save("dead analyzer", lambda root: intake_values.save_condition(
            root, "dead", list(targets), declared=dead, reason=reason),
            lambda r: f"Saved: {_n(len(r.targets), 'analyzer')} marked {word}")

    def set_counts_accepted(self, targets: Sequence[str], accepted: bool, reason: str | None) -> SaveOutcome:
        word = "accepted" if accepted else "no longer accepted"
        return self._save("counts accepted", lambda root: intake_values.save_condition(
            root, "counts_accepted", list(targets), declared=accepted, reason=reason),
            lambda r: f"Saved: counts {word} on {_n(len(r.targets), 'analyzer')}")

    def save_details(self, fields: Mapping[str, str]) -> SaveOutcome:
        return self._save("experiment details", lambda root: intake_settings.save_experiment_metadata(
            root, fields), lambda _r: "Saved: experiment details")

    def save_resolution_folder(self, folder: Path) -> SaveOutcome:
        return self._save("resolution folder", lambda root: intake_settings.save_resolution_folder(
            root, str(folder)), lambda r: f"Saved: resolution folder {r.value}")

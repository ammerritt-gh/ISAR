"""InterState -- the GUI thread's one holder of the inter-spectra reads and writers (C-gui-state).

Reads (the overview, one spectrum's history, one batch preview) run on one daemon worker thread, and their
answers come back through a queue polled on the GUI thread. An answer from before the last reload is dropped,
as IntakeState does. Each writer appends one delta to ``intake/inter_spectra.jsonl`` on the GUI thread, then
reloads. A refresh only reads (AGENTS.md hard rule 7).
"""
from __future__ import annotations

import logging
import queue
import threading
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

from isar.intake import inter_values
from isar.intake.settings import SaveResult
from isar.workspace_api import inter_spectra as reads
from isar.workspace_api.inter_spectra import BatchPreview, BatchRequest, FitEntry, InterOverview, SpectrumInter
from isar_gui import collector_guard
from isar_gui.viewmodels.intake import NO_EXPERIMENT, SaveOutcome

log = logging.getLogger(__name__)

_POLL_MS = 50
_PREVIEW_DEBOUNCE_MS = 150


def _work(results: queue.SimpleQueue, todo: deque) -> None:
    """The worker thread's reads, until none is queued (NO Qt access, the collector off for its whole life).
    It gets the queue and the job deque, never the view-model: a last reference dropped here would destroy
    that QObject off the GUI thread."""
    while True:
        try:
            generation, root, kind, key, call = todo.popleft()
        except IndexError:
            return
        try:
            value, error = call(root), None
        except Exception as exc:  # noqa: BLE001 - logged here, shown on the GUI thread as `error`
            log.exception("inter-spectra read %s %s of %s failed", kind, key, root)
            value, error = None, f"{kind}: {type(exc).__name__}: {exc}"
        results.put((generation, root, kind, key, value, error))


def _spectra(n: int) -> str:
    return f"{n} {'spectrum' if n == 1 else 'spectra'}"


class InterState(QObject):
    changed = Signal()                 # the overview (anchors, flags, locks, settings) swapped in
    history_ready = Signal(str)        # spectrum key whose history arrived
    preview_ready = Signal(object)     # BatchRequest whose preview arrived
    preview_failed = Signal(object)    # BatchRequest whose preview read raised: the box shows why beside Start
    saved = Signal(str)                # SaveOutcome.message of every write, ok or not (the window sends it to the Log)

    def __init__(self, intake, parent=None) -> None:
        super().__init__(parent)
        self._intake = intake           # only for busy_reason(): an export refuses a save
        self._root: Path | None = None
        self._overview: InterOverview | None = None
        self._histories: dict[str, tuple[FitEntry, ...]] = {}
        self._previews: dict[BatchRequest, BatchPreview] = {}
        self._preview_errors: dict[BatchRequest, str] = {}
        self._want: BatchRequest | None = None      # the newest preview asked for
        self._loading: set[tuple[str, object]] = set()   # (kind, key) queued or running
        self._error: str | None = None
        self._generation = 0            # bumped by every reload; an answer carries the one it was asked under
        self._active = False            # exactly one worker thread at a time, until it is seen ended
        self._thread: threading.Thread | None = None
        self._todo: deque = deque()     # queued reads, oldest first
        self._results: queue.SimpleQueue = queue.SimpleQueue()
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(_POLL_MS)
        self._poll_timer.timeout.connect(self._poll)
        self._debounce = QTimer(self)   # only the newest preview request is computed
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(_PREVIEW_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._fire_preview)

    # ---- binding and reading -----------------------------------------------------------
    def bind(self, root: Path | None) -> None:
        """The experiment bound (or ``None``: unbound, every read emptied); re-reads at once."""
        root = None if root is None else Path(root)
        if root != self._root:
            self._overview = None       # another experiment's anchors and locks are never shown
        self._root = root
        self.reload()

    def reload(self) -> None:
        """Forget the cached reads and ask the worker for the overview again; ``changed`` follows its answer."""
        self._generation += 1
        self._error = None
        self._histories.clear()
        self._previews.clear()
        self._preview_errors.clear()
        self._loading.clear()
        self._todo.clear()
        if self._root is None:
            self._overview = None
            self._want = None
            self._debounce.stop()
            self.changed.emit()
            return
        self._enqueue("overview", None, reads.inter_overview)
        if self._want is not None:
            self._debounce.start()

    def wait_idle(self) -> None:
        """Block until the worker has ended. Its reads hold files open in the experiment folder, and Windows
        refuses to rename a folder with an open handle in it."""
        if self._active and self._thread is not None:
            self._thread.join()

    @property
    def overview(self) -> InterOverview | None:
        return self._overview

    @property
    def revision(self) -> str:
        return self._overview.revision if self._overview else ""

    @property
    def anchors(self) -> tuple[str, ...]:
        return self._overview.anchors if self._overview else ()

    @property
    def settings(self) -> Mapping[str, object]:
        # Empty until the first read: G16 keeps isar_gui from importing the settings defaults itself.
        return self._overview.settings if self._overview else {}

    @property
    def error(self) -> str | None:
        return self._error

    def row(self, key: str) -> SpectrumInter | None:
        return self._overview.by_key.get(key) if self._overview else None

    def history(self, key: str) -> tuple[FitEntry, ...] | None:
        """The spectrum's fits, or None while they load (``history_ready`` follows the read)."""
        if key in self._histories:
            return self._histories[key]
        if self._root is not None and ("history", key) not in self._loading:
            self._loading.add(("history", key))
            self._enqueue("history", key, lambda root: reads.inter_history(root, key))
        return None

    def request_preview(self, req: BatchRequest) -> None:
        """Ask for a batch preview; each request restarts the debounce, so only the newest is computed."""
        self._want = req
        self._debounce.start()

    def preview_for(self, req: BatchRequest) -> BatchPreview | None:
        return self._previews.get(req)

    def preview_error_for(self, req: BatchRequest) -> str | None:
        """The text of a preview read that raised, kept until a reload; the box does not re-ask for that request."""
        return self._preview_errors.get(req)

    def _fire_preview(self) -> None:
        req = self._want
        if req is None or self._root is None:
            return
        if req in self._previews:
            self.preview_ready.emit(req)
        elif ("preview", req) not in self._loading:
            self._loading.add(("preview", req))
            self._enqueue("preview", req, lambda root: reads.batch_preview(root, req))

    # ---- the worker -------------------------------------------------------------------
    def _enqueue(self, kind: str, key, call: Callable[[Path], object]) -> None:
        self._todo.append((self._generation, self._root, kind, key, call))
        if not self._active:
            self._start_worker()

    def _start_worker(self) -> None:
        self._active = True
        # The thread gets the queue and the deque, never `self` (see _work).
        self._thread = threading.Thread(target=_work, args=(self._results, self._todo), daemon=True,
                                        name="isar-gui-inter")
        collector_guard.hold(self._thread)
        self._thread.start()
        self._poll_timer.start()

    # ---- GUI thread ----
    def _poll(self) -> None:
        ended = self._active and not self._thread.is_alive()   # read first: an ended worker's answers are queued
        try:
            while True:
                generation, root, kind, key, value, error = self._results.get_nowait()
                if generation == self._generation and root == self._root:
                    self._answer(kind, key, value, error)
        except queue.Empty:
            pass
        if ended:
            self._active = False
            collector_guard.release()
        if not self._active and self._todo:
            self._start_worker()
        if not self._active and not self._todo:
            self._poll_timer.stop()

    def _answer(self, kind: str, key, value, error: str | None) -> None:
        self._loading.discard((kind, key))
        if error is not None:
            self._error = error         # the last good value stays shown
            if kind == "preview":
                self._preview_errors[key] = error
                self.preview_failed.emit(key)
            return
        if kind == "overview":
            self._overview = value
            self.changed.emit()
        elif kind == "history":
            self._histories[key] = value
            self.history_ready.emit(key)
        elif key == self._want:         # the request is its own tag: an answer to one since passed over is dropped
            self._previews[key] = value
            self.preview_ready.emit(key)

    # ---- the writers -------------------------------------------------------------------
    def _save(self, call: Callable[[Path], SaveResult], sentence: Callable[[SaveResult], str]) -> SaveOutcome:
        busy = self._intake.busy_reason()
        if busy is not None or self._root is None:
            outcome = SaveOutcome(False, busy or NO_EXPERIMENT, None)
            self.saved.emit(outcome.message)
            return outcome
        try:
            result = call(self._root)
        except Exception as exc:  # noqa: BLE001 - shown and logged, never swallowed
            log.exception("inter-spectra save raised")
            outcome = SaveOutcome(False, f"{type(exc).__name__}: {exc}", None)
        else:
            outcome = SaveOutcome(result.ok, sentence(result) if result.ok else str(result.reason), result)
        self.saved.emit(outcome.message)
        self.reload()                   # ok or not: no view keeps a belief the save may have changed
        return outcome

    def set_anchors(self, keys: Sequence[str], anchored: bool, reason: str | None = None) -> SaveOutcome:
        keys = tuple(keys)
        word = "set as anchors" if anchored else "taken off the anchors"
        return self._save(lambda root: inter_values.set_anchors(root, keys, anchored=anchored, reason=reason),
                          lambda _r: f"Saved: {_spectra(len(keys))} {word}")

    def pick_fit(self, key: str, fit_id: str) -> SaveOutcome:
        return self._save(lambda root: inter_values.pick_fit(root, key, fit_id),
                          lambda _r: f"Saved: {key} now uses fit {fit_id}")

    def set_lock(self, key: str, locked: bool, fit_id: str | None = None) -> SaveOutcome:
        return self._save(lambda root: inter_values.set_lock(root, key, locked=locked, fit_id=fit_id),
                          lambda _r: f"Saved: {key} {'locked' if locked else 'unlocked'}")

    def save_settings(self, changes: Mapping[str, object | None]) -> SaveOutcome:
        delta = dict(changes)          # None: back to the default
        n = len(delta)
        return self._save(lambda root: inter_values.save_settings(root, delta),
                          lambda _r: f"Saved: {n} inter-spectra {'setting' if n == 1 else 'settings'}")

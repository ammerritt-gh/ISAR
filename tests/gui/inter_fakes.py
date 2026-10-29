"""A fake of the GUI's inter-spectra state (C-gui-state) for the view tests.

Reads are plain attributes a test sets. Writers record ``(name, args)`` in ``calls`` and return an ok
``SaveOutcome`` (``fail_next`` makes the next one refuse); a refresh never writes, so a test can spy on
``calls`` to show a view only read.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from isar.analyze.coupled.settings import InterSettings
from isar.workspace_api.inter_spectra import (BatchPreview, BatchRequest, FitEntry, InterOverview, SpectrumInter)
from isar_gui.viewmodels.intake import SaveOutcome


class FakeInterState(QObject):
    changed = Signal()
    history_ready = Signal(str)
    preview_ready = Signal(object)
    preview_failed = Signal(object)
    saved = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.overview: InterOverview | None = make_overview()
        self.histories: dict[str, tuple[FitEntry, ...]] = {}
        self.previews: dict[BatchRequest, BatchPreview] = {}
        self.preview_errors: dict[BatchRequest, str] = {}
        self.error: str | None = None
        self.calls: list[tuple[str, tuple]] = []
        self.fail_next: str | None = None          # a reason: the next writer refuses with it

    @property
    def revision(self) -> str:
        return self.overview.revision if self.overview else ""

    @property
    def anchors(self) -> tuple[str, ...]:
        return self.overview.anchors if self.overview else ()

    @property
    def settings(self):
        return self.overview.settings if self.overview else {}     # before the first overview read: nothing saved yet

    def row(self, key: str) -> SpectrumInter | None:
        return self.overview.by_key.get(key) if self.overview else None

    def history(self, key: str) -> tuple[FitEntry, ...] | None:
        return self.histories.get(key)

    def preview_for(self, req: BatchRequest) -> BatchPreview | None:
        return self.previews.get(req)

    def preview_error_for(self, req: BatchRequest) -> str | None:
        return self.preview_errors.get(req)

    def bind(self, root) -> None:
        self.calls.append(("bind", (root,)))

    def reload(self) -> None:
        self.calls.append(("reload", ()))

    def request_preview(self, req: BatchRequest) -> None:
        self.calls.append(("request_preview", (req,)))
        if req in self.previews:
            self.preview_ready.emit(req)

    def _write(self, name: str, *args) -> SaveOutcome:
        self.calls.append((name, args))
        if self.fail_next is not None:
            reason, self.fail_next = self.fail_next, None
            outcome = SaveOutcome(ok=False, message=reason, result=None)
        else:
            outcome = SaveOutcome(ok=True, message=f"Saved: {name}", result=None)
        self.saved.emit(outcome.message)
        return outcome

    def set_anchors(self, keys, anchored: bool, reason=None) -> SaveOutcome:
        return self._write("set_anchors", tuple(keys), anchored, reason)

    def pick_fit(self, key: str, fit_id: str) -> SaveOutcome:
        return self._write("pick_fit", key, fit_id)

    def set_lock(self, key: str, locked: bool, fit_id=None) -> SaveOutcome:
        return self._write("set_lock", key, locked, fit_id)

    def save_settings(self, changes) -> SaveOutcome:
        return self._write("save_settings", dict(changes))


def make_overview(*, by_key=(), anchors=(), batches=(), revision: str = "1") -> InterOverview:
    return InterOverview(revision=revision, by_key=dict(by_key), anchors=tuple(anchors),
                         settings=InterSettings().to_values(), batches=tuple(batches))


def make_entry(fit_id: str, label: str, **overrides) -> FitEntry:
    fields = dict(batch_id=None, batch_label=None, stage_label=None, changed=True, authoritative=False,
                  picked=False, locked=False, stale=False, stale_text=None, flag=None, why_changed=(),
                  conditional_errors=fit_id != "fast", errors_available=True)
    return FitEntry(fit_id=fit_id, label=label, **{**fields, **overrides})


def make_history(*refits: FitEntry) -> tuple[FitEntry, ...]:
    """The Fast solo entry first, then the refits; Fast solo is authoritative when no refit is."""
    fast = make_entry("fast", "Fast solo", conditional_errors=False,
                      authoritative=not any(r.authoritative for r in refits))
    return (fast, *refits)


def make_preview(**overrides) -> BatchPreview:
    return BatchPreview(**{**dict(can_start=True, reason=None, members=(), line="", notes=()), **overrides})

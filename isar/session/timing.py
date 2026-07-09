"""Subprocess-side stage timing (profiling instrumentation, mighty-starfish §3).

A tiny, dependency-free timer the ``isar run`` subprocess uses to record how long each stage of a
single invocation took (workspace load, ingest parse, reduce/QC, fit, report render, ...). The
result is serialized to ``report/run_timings.json`` and the driver reads it back as a plain file —
so this module lives in ``isar.session`` and deliberately does **not** import ``isar.drive`` (the
session/drive boundary stays one-directional).

Two implementations share one interface so callers never branch on ``None``:

- :class:`StageTimer` — real timing.
- :class:`NullTimer`  — no-op with the same shape, used when timing is disabled.
"""
from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class StageTimer:
    """Accumulates per-stage timing spans for a single ``isar run`` invocation.

    Construct with any top-level metadata (e.g. ``errors_tier``, ``M``, ``argv``); it is stored
    verbatim under the top level of :meth:`to_dict`. ``total_seconds`` is wall-clock elapsed from
    construction to the :meth:`to_dict` call (not the sum of stage spans), so it also captures time
    spent outside any timed stage.
    """

    def __init__(self, **meta: Any) -> None:
        self._t0 = time.perf_counter()
        self._meta = dict(meta)
        self._stages: list[dict[str, Any]] = []

    @contextmanager
    def stage(self, name: str, **meta: Any):
        """Time a span and append ``{"stage", "seconds", **meta}`` to the stage list on exit.

        Yields the ``meta`` dict so callers can mutate it inside the ``with`` block to attach
        results that ride into the recorded stage entry.
        """
        t0 = time.perf_counter()
        try:
            yield meta
        finally:
            entry = {"stage": name, "seconds": round(time.perf_counter() - t0, 4)}
            entry.update(meta)
            self._stages.append(entry)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = dict(self._meta)
        out["total_seconds"] = round(time.perf_counter() - self._t0, 4)
        out["stages"] = list(self._stages)
        return out

    def write(self, path: str | Path) -> None:
        """Best-effort JSON dump of :meth:`to_dict` to ``path`` (never raises)."""
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(self.to_dict(), ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass  # a timing-write failure must never fail the run


class NullTimer:
    """No-op timer with :class:`StageTimer`'s interface, for when timing is disabled."""

    def __init__(self, **meta: Any) -> None:
        pass

    @contextmanager
    def stage(self, name: str, **meta: Any):
        yield meta

    def to_dict(self) -> dict[str, Any]:
        return {}

    def write(self, path: str | Path) -> None:
        pass

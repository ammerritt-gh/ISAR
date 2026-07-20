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
import logging
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any


log = logging.getLogger(__name__)

_ERROR_METHOD_KEYS = ("bootstrap_seconds", "minos_seconds", "profile_seconds", "bayes_seconds")


def embedded_run_timings(events, *, fallback_path: str | Path | None = None) -> list[dict[str, Any]]:
    """Extract valid embedded ``isar run`` timing documents from session-log events.

    A standalone ``run_timings.json`` may be supplied as a fallback for an ordinary run. Malformed
    timing payloads are advisory and therefore skipped, but they are always logged.
    """
    embedded: list[dict[str, Any]] = []
    for index, event in enumerate(events):
        if not isinstance(event, dict) or event.get("kind") != "analyze":
            continue
        timing = (event.get("payload") or {}).get("timings")
        if timing is None:
            continue
        if isinstance(timing, dict):
            embedded.append(timing)
        else:
            log.warning("timing payload in analyze event %d is not an object; skipped", index)
    if embedded or fallback_path is None:
        return embedded
    path = Path(fallback_path)
    if not path.exists():
        return []
    try:
        timing = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(timing, dict):
            raise ValueError("top-level timing value is not an object")
        return [timing]
    except (OSError, ValueError, TypeError) as exc:
        log.warning("could not read timing data from %s: %s: %s", path, type(exc).__name__, exc)
        return []


def fit_timing_breakdown(timings) -> dict[str, Any] | None:
    """Return disjoint fit buckets from embedded run timing documents.

    ``fit_seconds`` is the outer per-unit wall measurement. Its known children are error attachment
    and two-stage fitting; their remainder is enumeration/IC/other optimization overhead. The four
    buckets therefore sum to total fit time without double counting. Per-method timings are retained
    separately because parallel work can overlap and is not a disjoint wall-time decomposition.
    """
    buckets = {
        "scan_error_attachment": 0.0,
        "master_error_attachment": 0.0,
        "two_stage_fitting": 0.0,
        "fit_remainder": 0.0,
    }
    methods = {key: 0.0 for key in _ERROR_METHOD_KEYS}
    n_units = 0
    for timing_index, timing in enumerate(timings):
        if not isinstance(timing, dict):
            log.warning("embedded timing document %d is not an object; skipped", timing_index)
            continue
        stages = timing.get("stages") or []
        if not isinstance(stages, list):
            log.warning("embedded timing document %d has non-list stages; skipped", timing_index)
            continue
        for stage_index, stage in enumerate(stages):
            if not isinstance(stage, dict):
                log.warning("timing stage %d:%d is not an object; skipped", timing_index, stage_index)
                continue
            for role, field in (("scan_error_attachment", "per_task"),
                                ("master_error_attachment", "per_master")):
                items = stage.get(field) or []
                if not isinstance(items, list):
                    log.warning("timing stage %d:%d has non-list %s; skipped",
                                timing_index, stage_index, field)
                    continue
                for item_index, item in enumerate(items):
                    if not isinstance(item, dict):
                        log.warning("timing %s item %d:%d:%d is not an object; skipped",
                                    field, timing_index, stage_index, item_index)
                        continue
                    try:
                        fit = float(item.get("fit_seconds", item.get("seconds")) or 0.0)
                        errors = float(item.get("errors_attach_seconds") or 0.0)
                        two_stage = float(item.get("two_stage_seconds") or 0.0)
                        item_methods = {
                            key: float(item.get(key) or 0.0) for key in _ERROR_METHOD_KEYS}
                    except (TypeError, ValueError) as exc:
                        log.warning("malformed timing %s item %d:%d:%d skipped: %s",
                                    field, timing_index, stage_index, item_index, exc)
                        continue
                    for key, value in item_methods.items():
                        methods[key] += value
                    buckets[role] += max(errors, 0.0)
                    buckets["two_stage_fitting"] += max(two_stage, 0.0)
                    buckets["fit_remainder"] += max(fit - errors - two_stage, 0.0)
                    n_units += 1
    if not n_units:
        return None
    total = sum(buckets.values())
    return {"buckets": buckets, "method_seconds": methods,
            "total_fit_seconds": total, "n_units": n_units}


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
        except Exception as exc:  # noqa: BLE001 — timing remains advisory
            log.warning("could not write timing data to %s: %s: %s",
                        path, type(exc).__name__, exc)


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

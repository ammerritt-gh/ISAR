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


def _read_timing_json(path: Path) -> dict[str, Any] | None:
    """Read one advisory timing/checkpoint document, warning and failing closed on damage."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("top-level value is not an object")
        return value
    except (OSError, ValueError, TypeError) as exc:
        log.warning("could not read timing data from %s: %s: %s",
                    path, type(exc).__name__, exc)
        return None


def certification_pass_breakdown(workspace: str | Path | None, events) -> list[dict[str, Any]]:
    """Return critical-path-aware metrics for durable certification revisions.

    Certification units run in parallel, so their summed ``fit_seconds`` is work, not campaign
    wall.  Each row therefore keeps both the activity wall span and the slowest unit (the closest
    persisted approximation to the pass critical path), along with selective-certification reuse.
    Older campaigns without durable manifests simply return an empty list.
    """
    if workspace is None:
        return []
    root = Path(workspace) / "report" / "certification"
    if not root.exists():
        return []
    summary_path = Path(workspace) / "report" / "campaign_summary.json"
    summary = (_read_timing_json(summary_path) or {}) if summary_path.exists() else {}
    campaign_path = (summary.get("objective") or {}).get("path") or []
    campaign_points = len(campaign_path) if isinstance(campaign_path, list) else 0

    activities: dict[str, dict[str, Any]] = {}
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            log.warning("session event %d is not an object while reading certification timings",
                        index)
            continue
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            log.warning("session event %d has non-object payload while reading certification timings",
                        index)
            continue
        activity_id = payload.get("activity_id")
        if not activity_id or payload.get("stage") != "final_certification":
            continue
        record = activities.setdefault(str(activity_id), {})
        if event.get("kind") == "activity_start":
            record["start_ts"] = event.get("ts")
        elif event.get("kind") == "activity_end":
            record["end_ts"] = event.get("ts")

    rows: list[dict[str, Any]] = []
    for revision in root.iterdir():
        if not revision.is_dir() or revision.name == "requests":
            continue
        manifest_path = revision / "manifest.json"
        if not manifest_path.exists():
            continue
        manifest = _read_timing_json(manifest_path)
        if manifest is None:
            continue
        runtime_path = revision / "runtime.json"
        progress_path = revision / "progress.json"
        runtime = (_read_timing_json(runtime_path) or {}) if runtime_path.exists() else {}
        progress = (_read_timing_json(progress_path) or {}) if progress_path.exists() else {}
        request = None
        request_fingerprint = manifest.get("request_fingerprint")
        if request_fingerprint:
            request_path = root / "requests" / f"{request_fingerprint}.json"
            if request_path.exists():
                request = _read_timing_json(request_path)
        request = request or {}

        worklist = manifest.get("worklist") or []
        if not isinstance(worklist, list):
            log.warning("certification manifest %s has non-list worklist; skipped", manifest_path)
            continue
        work_by_key = {str(item.get("unit_key")): item for item in worklist
                       if isinstance(item, dict) and item.get("unit_key")}
        units: list[dict[str, Any]] = []
        units_dir = revision / "units"
        if units_dir.exists():
            for unit_path in units_dir.glob("*.json"):
                unit = _read_timing_json(unit_path)
                if unit is None:
                    continue
                entry = unit.get("entry") or {}
                provenance = entry.get("provenance") or {}
                if not isinstance(entry, dict) or not isinstance(provenance, dict):
                    log.warning("certification unit %s has malformed entry/provenance; skipped",
                                unit_path)
                    continue
                try:
                    fit_seconds = float(provenance.get("fit_seconds") or 0.0)
                    methods = {key: float(provenance.get(key) or 0.0)
                               for key in _ERROR_METHOD_KEYS}
                except (TypeError, ValueError) as exc:
                    log.warning("certification unit %s has malformed timings; skipped: %s",
                                unit_path, exc)
                    continue
                work = work_by_key.get(str(unit.get("unit_key")), {})
                units.append({"unit_key": unit.get("unit_key"),
                              "master_id": entry.get("scan_id"),
                              "hkl": work.get("hkl"),
                              "fit_seconds": max(fit_seconds, 0.0),
                              "method_seconds": methods})

        objective_points = request.get("objective_path") or request.get("per_q") or []
        request_points = (len(objective_points)
                          if isinstance(objective_points, list) else len(worklist))
        requested = campaign_points or request_points
        selected = len(worklist)
        activity_id = str(runtime.get("activity_id") or progress.get("activity_id") or "")
        activity = activities.get(activity_id, {})
        start_ts, end_ts = activity.get("start_ts"), activity.get("end_ts")
        wall_seconds = None
        if isinstance(start_ts, (int, float)) and isinstance(end_ts, (int, float)):
            wall_seconds = max(float(end_ts) - float(start_ts), 0.0)
        slowest = max(units, key=lambda item: item["fit_seconds"], default=None)
        rows.append({
            "fingerprint": str(manifest.get("fingerprint") or revision.name),
            "activity_id": activity_id or None,
            "requested_points": requested,
            "selected_points": selected,
            "carried_points": max(requested - selected, 0),
            "newly_fitted": int(progress.get("newly_fitted", len(units)) or 0),
            "reused_checkpoint": int(progress.get("reused_checkpoint", 0) or 0),
            "reused_workspace": int(progress.get("reused_workspace", 0) or 0),
            "wall_seconds": wall_seconds,
            "unit_work_seconds": sum(item["fit_seconds"] for item in units),
            "critical_unit_seconds": slowest["fit_seconds"] if slowest else None,
            "slowest_hkl": slowest.get("hkl") if slowest else None,
            "slowest_master_id": slowest.get("master_id") if slowest else None,
            "critical_method_seconds": slowest.get("method_seconds") if slowest else {},
            "sort_ts": float(start_ts) if isinstance(start_ts, (int, float)) else float("inf"),
        })
    rows.sort(key=lambda row: (row["sort_ts"], row["fingerprint"]))
    for row in rows:
        row.pop("sort_ts", None)
    return rows


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
    timing = _read_timing_json(path)
    return [timing] if timing is not None else []


def fit_timing_breakdown(timings) -> dict[str, Any] | None:
    """Return disjoint fit buckets from embedded run timing documents.

    ``fit_seconds`` is the outer per-unit wall measurement. Its known children are error attachment
    and two-stage fitting; their remainder is enumeration/IC/other optimization overhead. The four
    buckets therefore sum to total fit time without double counting. Per-method timings are retained
    separately because parallel work can overlap and is not a disjoint wall-time decomposition.
    So are the count ladder's: ``count_ladder_seconds`` sums each scan unit's rung-1 (fits and their
    Laplace selection) and rung-2 (nested sampling) wall time, and ``per_spectrum_rungs`` lists them
    unit by unit (rung 2 sits inside the remainder bucket).
    """
    buckets = {
        "scan_error_attachment": 0.0,
        "master_error_attachment": 0.0,
        "two_stage_fitting": 0.0,
        "fit_remainder": 0.0,
    }
    methods = {key: 0.0 for key in _ERROR_METHOD_KEYS}
    rungs = {"rung1": 0.0, "rung2": 0.0}
    per_spectrum = []
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
                    if "rung1_seconds" in item or "rung2_seconds" in item:
                        try:
                            rung1 = float(item.get("rung1_seconds") or 0.0)
                            rung2 = float(item.get("rung2_seconds") or 0.0)
                        except (TypeError, ValueError) as exc:
                            log.warning("malformed rung timing in %s item %d:%d:%d skipped: %s",
                                        field, timing_index, stage_index, item_index, exc)
                        else:
                            rungs["rung1"] += rung1
                            rungs["rung2"] += rung2
                            per_spectrum.append({"scan_id": item.get("scan_id"),
                                                 "rung1_seconds": rung1, "rung2_seconds": rung2})
                    buckets[role] += max(errors, 0.0)
                    buckets["two_stage_fitting"] += max(two_stage, 0.0)
                    buckets["fit_remainder"] += max(fit - errors - two_stage, 0.0)
                    n_units += 1
    if not n_units:
        return None
    total = sum(buckets.values())
    return {"buckets": buckets, "method_seconds": methods,
            "total_fit_seconds": total, "n_units": n_units,
            "count_ladder_seconds": {k: round(v, 4) for k, v in rungs.items()},
            "per_spectrum_rungs": per_spectrum}


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

"""`isar profile --workspace DIR` — where an autonomous campaign spent its time (plan §6).

Reads the driver's ``session_log.jsonl`` and turns the ``"timing"`` events into a stage
breakdown, an embedded ``isar_run`` subprocess sub-stage breakdown (with the per-scan
interpreter/matplotlib startup overhead), and — with ``--iterations`` — a per-iteration
table. Old logs written before timing instrumentation degrade to approximate per-kind
gap estimates from consecutive event timestamps. Stdlib only, plain print.
"""
from __future__ import annotations

import sys
from pathlib import Path

from isar.drive.session_log import read_log
from isar.session.shadow import compare_workspaces
from isar.session.timing import (certification_pass_breakdown, embedded_run_timings,
                                 fit_timing_breakdown)


# --------------------------------------------------------------------------- helpers
def _aggregate(pairs):
    """Aggregate ``(name, seconds)`` pairs into ``{name: {count,total,mean,max}}``."""
    agg: dict[str, dict] = {}
    for name, sec in pairs:
        a = agg.setdefault(name, {"count": 0, "total": 0.0, "max": 0.0})
        a["count"] += 1
        a["total"] += sec
        if sec > a["max"]:
            a["max"] = sec
    for a in agg.values():
        a["mean"] = a["total"] / a["count"] if a["count"] else 0.0
    return agg


def _print_table(agg, wall, top, title, *, key_label="stage"):
    rows = sorted(agg.items(), key=lambda kv: kv[1]["total"], reverse=True)
    if top:
        rows = rows[:top]
    print(title)
    print(f"  {key_label:22s} {'count':>6s} {'total_s':>10s} "
          f"{'mean_s':>10s} {'max_s':>10s} {'%wall':>7s}")
    for name, a in rows:
        pct = (a["total"] / wall * 100.0) if wall > 0 else 0.0
        print(f"  {name:22s} {a['count']:6d} {a['total']:10.3f} "
              f"{a['mean']:10.3f} {a['max']:10.3f} {pct:6.1f}%")


def _fmt_hkl(hkl):
    if isinstance(hkl, (list, tuple)) and len(hkl) == 3:
        try:
            return "(" + " ".join(f"{float(x):.3g}" for x in hkl) + ")"
        except (TypeError, ValueError):
            pass
    return str(hkl)


# --------------------------------------------------------------------------- sections
def _print_subprocess_breakdown(events, log_path, stage_pairs, wall, top):
    """Aggregate embedded analysis timings and their driver-side process overhead."""
    embedded = embedded_run_timings(events, fallback_path=log_path.parent / "run_timings.json")
    if not embedded:
        return

    sub_pairs = [
        (st.get("stage", "?"), float(st.get("seconds", 0.0)))
        for t in embedded for st in (t.get("stages") or [])
    ]
    print()
    _print_table(_aggregate(sub_pairs), wall, top,
                 "  isar_run: subprocess sub-stage breakdown:")

    # Ordinary scan analysis is wrapped by ``isar_run``; final master-only analysis is wrapped by
    # ``certify``.  Both publish an embedded run timing.  Omitting the latter parent while subtracting
    # its child produced impossible negative overhead on campaigns such as OP5.
    parent_wall = sum(sec for name, sec in stage_pairs if name in {"isar_run", "certify"})
    embedded_total = sum(float(t.get("total_seconds", 0.0)) for t in embedded)
    overhead = parent_wall - embedded_total
    print()
    print(f"  subprocess overhead (interpreter + analysis process startup): "
          f"{overhead:.3f}s over {len(embedded)} embedded run(s)")


_ERROR_METHOD_SECONDS_KEYS = (
    "bootstrap_seconds", "minos_seconds", "profile_seconds", "bayes_seconds")


def _print_error_method_breakdown(events, log_path, wall, top):
    """Aggregate per-method error-attach timings (bootstrap/MINOS/profile/bayes) from the
    embedded ``run_timings.json`` ``per_task``/``per_master`` entries. Silently skipped when the
    embedded timings predate this instrumentation (no entries carry the keys)."""
    embedded = embedded_run_timings(events, fallback_path=log_path.parent / "run_timings.json")
    if not embedded:
        return

    items = []
    for t in embedded:
        for st in (t.get("stages") or []):
            items.extend(st.get("per_task") or [])
            items.extend(st.get("per_master") or [])

    has_keys = any(k in item for item in items for k in _ERROR_METHOD_SECONDS_KEYS)
    if not has_keys:
        return

    pairs = []
    for item in items:
        for k in _ERROR_METHOD_SECONDS_KEYS:
            sec = item.get(k)
            if sec is not None:
                pairs.append((k, float(sec)))

    if not pairs:
        return

    print()
    _print_table(_aggregate(pairs), wall, top, "  error-method breakdown:", key_label="method")
    print()
    print("  Note: HESSE cost is inside the MIGRAD fit (two_stage_seconds); the comparable "
          "fast-tier number is mean fit_seconds on fast-tier iterations.")


def _print_disjoint_fit_breakdown(events, log_path, wall):
    embedded = embedded_run_timings(events, fallback_path=log_path.parent / "run_timings.json")
    breakdown = fit_timing_breakdown(embedded)
    if breakdown is None:
        return
    total = breakdown["total_fit_seconds"]
    labels = {
        "scan_error_attachment": "scan error attachment",
        "master_error_attachment": "master error attachment",
        "two_stage_fitting": "two-stage fitting",
        "fit_remainder": "enumeration/IC/optimization remainder",
    }
    print()
    print("  disjoint fit-time decomposition:")
    print(f"  {'bucket':36s} {'total_s':>10s} {'%wall':>7s} {'%fit':>7s}")
    for key, seconds in breakdown["buckets"].items():
        wall_pct = seconds / wall * 100.0 if wall > 0 else 0.0
        fit_pct = seconds / total * 100.0 if total > 0 else 0.0
        print(f"  {labels[key]:36s} {seconds:10.3f} {wall_pct:6.1f}% {fit_pct:6.1f}%")


def _print_certification_passes(workspace, events):
    passes = certification_pass_breakdown(workspace, events)
    if not passes:
        return
    print()
    print("Certification passes (parallel-unit critical path):")
    print(f"  {'pass':>4s} {'requested':>9s} {'carried':>7s} {'fitted':>7s} "
          f"{'checkpoint':>10s} {'wall_s':>9s} {'critical_s':>10s} {'slowest_q':>16s}")
    for index, item in enumerate(passes, 1):
        wall = item.get("wall_seconds")
        critical = item.get("critical_unit_seconds")
        print(f"  {index:4d} {item['requested_points']:9d} {item['carried_points']:7d} "
              f"{item['newly_fitted']:7d} {item['reused_checkpoint']:10d} "
              f"{wall if wall is not None else 0.0:9.3f} "
              f"{critical if critical is not None else 0.0:10.3f} "
              f"{_fmt_hkl(item.get('slowest_hkl')):>16s}")
        methods = item.get("critical_method_seconds") or {}
        method_text = ", ".join(
            f"{key.removesuffix('_seconds')}={float(seconds):.3f}s"
            for key, seconds in methods.items() if float(seconds) > 0.0)
        if method_text:
            print(f"       slowest unit: {item.get('slowest_master_id') or '?'}; {method_text}")
    print("  Note: fitted-unit seconds overlap across workers; pass wall and the slowest unit expose "
          "the elapsed-time critical path.")


def _print_iterations(events):
    iters: dict[int, dict] = {}
    for e in events:
        pay = e.get("payload") or {}
        it = pay.get("iter")
        if it is None:
            continue
        d = iters.setdefault(it, {"total": None, "stages": {}, "prop": None,
                                  "tier": None, "exc": 0})
        kind = e.get("kind")
        if kind == "timing":
            stage = pay.get("stage")
            sec = float(pay.get("seconds", 0.0))
            if stage == "iteration":
                d["total"] = sec
            else:
                d["stages"][stage] = d["stages"].get(stage, 0.0) + sec
        elif kind == "propose":
            d["prop"] = (pay.get("kind", "?"), pay.get("hkl"))
        elif kind == "analyze":
            scan_tier = pay.get("scan_errors_tier")
            master_tier = pay.get("master_errors_tier")
            if scan_tier or master_tier:
                d["tier"] = f"{scan_tier or '?'}/{master_tier or '?'}"
            elif pay.get("errors_tier"):
                d["tier"] = pay.get("errors_tier")
        elif kind == "exception":
            d["exc"] += 1

    if not iters:
        print()
        print("Per-iteration breakdown: (no iteration-indexed events in this log)")
        return

    print()
    print("Per-iteration breakdown:")
    print(f"  {'iter':>4s} {'total_s':>9s} {'dominant':18s} "
          f"{'proposal':28s} {'scan/master tier':16s} {'exc':>3s}")
    for it in sorted(iters):
        d = iters[it]
        total_s = f"{d['total']:9.3f}" if d["total"] is not None else f"{'-':>9s}"
        dom = max(d["stages"], key=d["stages"].get) if d["stages"] else "-"
        if d["prop"]:
            k, hkl = d["prop"]
            prop = f"{k} {_fmt_hkl(hkl)}"
        else:
            prop = "-"
        print(f"  {it:4d} {total_s} {dom:18s} {prop:28s} "
              f"{(d['tier'] or '-'):16s} {d['exc']:3d}")


def _print_gap_estimates(events, wall):
    print("No timing events in this log (older campaign, or timing instrumentation disabled).")
    print("Approximate per-kind wall time from gaps between consecutive events")
    print("(each gap is attributed to the LATER event's kind — a rough proxy, not measured):")
    print()
    pairs = []
    prev = None
    for e in sorted(events, key=lambda e: e.get("ts", 0.0)):
        ts = e.get("ts")
        if ts is None:
            continue
        if prev is not None:
            pairs.append((e.get("kind", "?"), ts - prev))
        prev = ts
    _print_table(_aggregate(pairs), wall, None,
                 "Per-kind gap estimate (approximate):", key_label="kind")


# --------------------------------------------------------------------------- entry point
def run(args) -> int:
    if getattr(args, "log", None):
        log_path = Path(args.log)
    else:
        log_path = Path(args.workspace) / "report" / "session_log.jsonl"

    if not log_path.exists():
        print(f"error: no session log at {log_path}", file=sys.stderr)
        print("       pass --log PATH, or point --workspace at a driven campaign "
              "(the log lives at <workspace>/report/session_log.jsonl).", file=sys.stderr)
        return 2

    events = read_log(log_path)
    if not events:
        print(f"error: session log at {log_path} is empty", file=sys.stderr)
        return 2

    ts = [e["ts"] for e in events if "ts" in e]
    wall = (max(ts) - min(ts)) if len(ts) >= 2 else 0.0

    print(f"Session log: {log_path}")
    print(f"Events: {len(events)}   campaign wall: {wall:.1f}s ({wall / 60.0:.1f} min)")
    print()

    timing = [e for e in events if e.get("kind") == "timing"]
    if not timing:
        _print_gap_estimates(events, wall)
        return 0

    stage_pairs = [
        ((e.get("payload") or {}).get("stage", "?"),
         float((e.get("payload") or {}).get("seconds", 0.0)))
        for e in timing
    ]
    # The "iteration" span wraps every child stage, so it double-counts against %wall and
    # would dominate the table as a bogus peer row. Exclude it from the per-stage breakdown
    # and report it once as the wrapping total instead.
    iter_pairs = [(n, s) for n, s in stage_pairs if n == "iteration"]
    child_pairs = [(n, s) for n, s in stage_pairs if n != "iteration"]
    if iter_pairs:
        iter_total = sum(s for _, s in iter_pairs)
        print(f"Iterations: {len(iter_pairs)}   total iteration time: {iter_total:.3f}s "
              f"(wraps the per-stage rows below)")
    _print_table(_aggregate(child_pairs), wall, args.top,
                 "Stage breakdown (driver-side timing events):")

    _print_subprocess_breakdown(events, log_path, stage_pairs, wall, args.top)

    _print_disjoint_fit_breakdown(events, log_path, wall)

    _print_error_method_breakdown(events, log_path, wall, args.top)

    _print_certification_passes(args.workspace, events)

    if getattr(args, "iterations", False):
        _print_iterations(events)

    compare_workspace = getattr(args, "compare_workspace", None)
    if compare_workspace:
        comparison = compare_workspaces(args.workspace, compare_workspace)
        print()
        print(f"Shadow comparison: {args.workspace} vs {compare_workspace}")
        if comparison.identical:
            print("  identical scientific decision stream, prior admissions, and certified results")
        else:
            print("  DIFFERENT: " + ", ".join(comparison.differences))
            return 1

    return 0

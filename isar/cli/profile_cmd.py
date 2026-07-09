"""`isar profile --workspace DIR` — where an autonomous campaign spent its time (plan §6).

Reads the driver's ``session_log.jsonl`` and turns the ``"timing"`` events into a stage
breakdown, an embedded ``isar_run`` subprocess sub-stage breakdown (with the per-scan
interpreter/matplotlib startup overhead), and — with ``--iterations`` — a per-iteration
table. Old logs written before timing instrumentation degrade to approximate per-kind
gap estimates from consecutive event timestamps. Stdlib only, plain print.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from isar.drive.session_log import read_log


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
    """Aggregate the embedded ``isar_run`` sub-stage timings and the per-scan overhead."""
    embedded = []
    for e in events:
        if e.get("kind") == "analyze":
            t = (e.get("payload") or {}).get("timings")
            if isinstance(t, dict):
                embedded.append(t)
    if not embedded:
        # Fallback: the last run's standalone run_timings.json next to the log.
        rt_path = log_path.parent / "run_timings.json"
        if rt_path.exists():
            try:
                embedded = [json.loads(rt_path.read_text(encoding="utf-8"))]
            except (ValueError, OSError):
                embedded = []
    if not embedded:
        return

    sub_pairs = [
        (st.get("stage", "?"), float(st.get("seconds", 0.0)))
        for t in embedded for st in (t.get("stages") or [])
    ]
    print()
    _print_table(_aggregate(sub_pairs), wall, top,
                 "  isar_run: subprocess sub-stage breakdown:")

    isar_run_wall = sum(sec for name, sec in stage_pairs if name == "isar_run")
    embedded_total = sum(float(t.get("total_seconds", 0.0)) for t in embedded)
    overhead = isar_run_wall - embedded_total
    print()
    print(f"  subprocess overhead (interpreter + matplotlib startup, paid per scan): "
          f"{overhead:.3f}s over {len(embedded)} embedded run(s)")


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
            if pay.get("errors_tier"):
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
          f"{'proposal':28s} {'tier':6s} {'exc':>3s}")
    for it in sorted(iters):
        d = iters[it]
        total_s = f"{d['total']:9.3f}" if d["total"] is not None else f"{'-':>9s}"
        dom = max(d["stages"], key=d["stages"].get) if d["stages"] else "-"
        if d["prop"]:
            k, hkl = d["prop"]
            prop = f"{k} {_fmt_hkl(hkl)}"
        else:
            prop = "-"
        print(f"  {it:4d} {total_s} {dom:18s} {prop:28s} {(d['tier'] or '-'):6s} {d['exc']:3d}")


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
    _print_table(_aggregate(stage_pairs), wall, args.top,
                 "Stage breakdown (driver-side timing events):")

    _print_subprocess_breakdown(events, log_path, stage_pairs, wall, args.top)

    if getattr(args, "iterations", False):
        _print_iterations(events)

    return 0

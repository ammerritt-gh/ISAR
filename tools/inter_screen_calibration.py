"""Calibrate the candidate search's screens against full prices on ring 1 of an anchored batch (S1U5).

    python tools/inter_screen_calibration.py <workspace COPY> --anchors KEY KEY --direction q --workers 16 --out DIR

Sets ``ISAR_INTER_SCREENS=shadow`` and runs the batch's real stage-1 search of ring 1 through its pool: full prices
drive every decision, and each screen is computed alongside and recorded as a shadow decision (C-screen-record, the
search record's version 2), so an early wrong screen cannot hide its own consequences. The run stops at the end of
ring 1's stage 1 and writes ``calibration.json`` and ``calibration.md`` under --out: per split basin the full price
minus its screen (the underrating; positive is the dangerous sign), its distribution and how many exceed half and all
of the allowance; per count the full total minus the face-screen total; every shadow decision, the would-promote
count (a cost proxy) and each divergence; per spectrum the first divergence and which choices would differ.

Scope: it calibrates the screens on this anchored ring's frozen priors. It does not establish the same error for
every future simultaneous batch. The allowance criterion below is the plan's reading of "with room to spare":
every observed underrating of a split basin and of every count's total is at most half the allowance, and no shadow
decision diverges. It is not an operator number. This run prices every candidate twice, so it is not a timing run.

Run on a scratch COPY of the experiment only (the batch records are written into the workspace). Deliberate
measurement, never part of the routine suite. --timeout-s kills the run and writes TIMEOUT.txt.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
from pathlib import Path

if __package__ in (None, ""):                      # run as a script from anywhere in the repo
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.path.insert(0, str(Path(__file__).resolve().parent))

SCOPE = ("Scope: this calibrates the screens on this anchored ring's frozen priors. It does not establish the same "
         "error for every future simultaneous batch.")
CRITERION = ("Allowance criterion (the plan's reading of 'with room to spare', not an operator number): every "
             "observed underrating of a split basin and of every count's total is at most half the allowance, and "
             "no shadow decision diverges.")


def _lse(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    if not xs:
        return None
    m = max(xs)
    return m + math.log(sum(math.exp(x - m) for x in xs))


def _dist(xs: list[float], allowance: float) -> dict:
    """Count, median, 90th percentile and largest of ``xs``, and how many exceed half and all of the allowance."""
    if not xs:
        return {"n": 0, "median": None, "p90": None, "max": None, "over_half": 0, "over_all": 0}
    s = sorted(xs)
    p90 = s[min(len(s) - 1, math.ceil(0.9 * len(s)) - 1)]
    return {"n": len(s), "median": statistics.median(s), "p90": p90, "max": s[-1],
            "over_half": sum(x > allowance / 2 for x in s), "over_all": sum(x > allowance for x in s)}


def build_report(items: dict, allowance: float) -> dict:
    """``items``: unit -> {"search": Assignment.search, "candidates": Assignment.candidates}, every search a version-2
    record taken in mode ``shadow``. Returns the report as plain data (``render`` writes it as text)."""
    basin_under, count_under, cand_under = [], [], []
    spectra, unreadable = {}, []
    for unit, it in items.items():
        s = it["search"]
        if not s or s.get("version") != 2 or not s.get("shadow"):
            unreadable.append(unit)
            continue
        sh = s["shadow"]
        basins = [{"count": c, "labels": lab, "full": f, "screen": sc,
                   "underrating": None if f is None else f - sc} for c, lab, f, sc in sh["basins"]]
        basin_under += [b["underrating"] for b in basins if b["underrating"] is not None]
        cands = [c for c in it["candidates"] if c.get("ln_z") is not None]
        cand_under += [c["ln_z"] - c["ln_z_screen"] for c in cands if c.get("ln_z_screen") is not None]
        counts = {}
        for k, v in s["counts"].items():
            mine = [c for c in cands if str(c["count"]) == k]
            face = _lse([c["ln_z_screen"] if c.get("ln_z_screen") is not None else c["ln_z"] for c in mine])
            full = v.get("ln_total")
            counts[k] = {"full_total": full, "face_screen_total": face,
                         "underrating": None if full is None or face is None else full - face}
            if counts[k]["underrating"] is not None:
                count_under.append(counts[k]["underrating"])
        decs = sh["decisions"]
        definite = [d for d in decs if d["screen"] is not None]
        fd = sh["first_divergence"]
        spectra[unit] = {
            "basins": basins, "counts": counts, "decisions": decs,
            "would_promote": sum(d["screen"] is None for d in decs), "divergences": definite,
            "first_divergence": None if fd is None else {"index": fd, **decs[fd]},
            "kinds_that_differ": sorted({d["kind"] for d in definite}),
            "final_choice_differs": any(d["kind"] == "final" and d["screen"] is False for d in decs),
            "final_would_promote": any(d["kind"] == "final" and d["screen"] is None for d in decs),
            "screens": s["screens"], "candidates": len(it["candidates"])}
    agg_b, agg_c = _dist(basin_under, allowance), _dist(count_under, allowance)
    divergences = sum(len(x["divergences"]) for x in spectra.values())
    met = bool(spectra) and not unreadable and divergences == 0 and all(
        d["max"] is None or d["max"] <= allowance / 2 for d in (agg_b, agg_c))
    return {"allowance_nats": allowance, "scope": SCOPE, "criterion": CRITERION, "criterion_met": met,
            "spectra": spectra, "unreadable": unreadable, "split_basin_underrating": agg_b,
            "count_total_underrating": agg_c, "candidate_underrating": _dist(cand_under, allowance),
            "divergences": divergences,
            "would_promote": sum(x["would_promote"] for x in spectra.values())}


def _f(x, spec=".3f") -> str:
    return "-" if x is None else format(x, spec)


def render(rep: dict) -> str:
    a = rep["allowance_nats"]
    L = ["# Screen calibration, ring 1 (shadow run)", "", rep["scope"], "", rep["criterion"], "",
         f"allowance {a} nats (half: {a / 2}); criterion met: **{rep['criterion_met']}**; shadow decisions that "
         f"diverge: {rep['divergences']}; decisions where the screen could not decide (would promote): "
         f"{rep['would_promote']}", ""]
    if rep["unreadable"]:
        L += [f"No shadow record for: {', '.join(rep['unreadable'])}", ""]
    L += ["## Underrating (full minus screen; positive is dangerous)", "",
          "| what | n | median | p90 | largest | over half the allowance | over the allowance |", "|---|---|---|---|---|---|---|"]
    for name, k in (("split basin", "split_basin_underrating"), ("count total", "count_total_underrating"),
                    ("candidate (all its basins)", "candidate_underrating")):
        d = rep[k]
        L.append(f"| {name} | {d['n']} | {_f(d['median'])} | {_f(d['p90'])} | {_f(d['max'])} | {d['over_half']} | "
                 f"{d['over_all']} |")
    for unit, x in rep["spectra"].items():
        L += ["", f"## {unit}", "",
              f"candidates {x['candidates']}; screens priced {x['screens']['screened']}; would promote "
              f"{x['would_promote']}; divergences {len(x['divergences'])}; first divergence: "
              f"{json.dumps(x['first_divergence'])}; kinds that differ: {x['kinds_that_differ'] or 'none'}; final "
              f"choice differs: {x['final_choice_differs']}; final would promote: {x['final_would_promote']}", "",
              "| count | full total | face-screen total | underrating |", "|---|---|---|---|"]
        for k, c in x["counts"].items():
            L.append(f"| {k} | {_f(c['full_total'])} | {_f(c['face_screen_total'])} | {_f(c['underrating'])} |")
        if x["basins"]:
            L += ["", "| count | labels | full term | screen term | underrating |", "|---|---|---|---|---|"]
            L += [f"| {b['count']} | {b['labels']} | {_f(b['full'])} | {_f(b['screen'])} | {_f(b['underrating'])} |"
                  for b in x["basins"]]
        if x["decisions"]:
            L += ["", "| # | kind | at | full | screen |", "|---|---|---|---|---|"]
            L += [f"| {i} | {d['kind']} | {d['at']} | {d['full']} | {d['screen']} |"
                  for i, d in enumerate(x["decisions"])]
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Calibrate the search's screens on ring 1 of an anchored batch (S1U5).")
    p.add_argument("workspace", help="a scratch COPY of the experiment (batch records are written into it)")
    p.add_argument("--anchors", nargs="+", required=True, metavar="KEY", help="full spectrum keys")
    p.add_argument("--direction", required=True, choices=("q", "T", "qT"))
    p.add_argument("--workers", type=int, default=16)
    p.add_argument("--out", required=True, help="folder for calibration.json and calibration.md")
    p.add_argument("--timeout-s", type=float, default=10800.0, help="wall-clock guard: kills the run and reports")
    args = p.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    from inter_ring1 import arm_timeout, ring1
    timer = arm_timeout(args.timeout_s, out, "inter_screen_calibration")
    os.environ["ISAR_INTER_SCREENS"] = "shadow"          # read by search.Search; pool children inherit it
    from isar.analyze.coupled.search import SCREEN_ALLOWANCE_NATS
    t0 = time.perf_counter()
    root = Path(args.workspace).resolve()
    print(f"running ring 1 in shadow mode on {root}", flush=True)
    got = ring1(root, args.anchors, args.direction, args.workers, search=True)
    print(got["line"], f"ring-1 spectra: {len(got['snaps'])}; searched: {len(got['A'])}; "
          f"errors: {len(got['errors'])}", flush=True)
    items = {u: {"search": a.search, "candidates": a.candidates} for u, a in got["A"].items()}
    rep = build_report(items, SCREEN_ALLOWANCE_NATS)
    rep["errors"] = {u: f"{type(e).__name__}: {e}" for u, e in got["errors"].items()}
    rep["wall_s"] = time.perf_counter() - t0
    (out / "calibration.json").write_text(json.dumps(rep, indent=1, default=str) + "\n", encoding="utf-8",
                                          newline="\n")
    text = render(rep)
    (out / "calibration.md").write_text(text, encoding="utf-8", newline="\n")
    print(text[:3000])
    print(f"wrote {out / 'calibration.md'} and {out / 'calibration.json'} ({rep['wall_s']:.0f} s)")
    timer.cancel()
    return 0 if items and not got["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())

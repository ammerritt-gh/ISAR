"""The fast pass's acceptance table (coordinator decision 9): a helper, never collected by pytest.

Twelve rows on the production Voigt path: the clean three-peak truth under ten Poisson draws, and
the same truth with a fourth, planted narrow peak at 17.5 meV (width 0.05 and 0) on the first draw.
Each row is one ``run_fast_pass`` at production defaults (mode ceiling 6) in its own process, so
``DELTA_TWIN_RULE`` can be set per row and a runaway draw is killed. ``judge`` is the criterion;
``tests/analyze/test_fast_pass_acceptance.py`` runs the table and prints it.

Run one row by hand: ``python tests/analyze/fast_pass_table.py <seed> <narrow_gamma|none> <1|0> <out.json>``.
"""
from __future__ import annotations

import json
import math
import os
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
SEEDS = (642029, 1, 11, 12, 13, 14, 15, 16, 17, 18)
NARROW_GAMMAS = (0.05, 0.0)
NARROW_ENERGY = 17.5
NARROW_GRADE = 100.0            # noiseless deletion grade of the planted narrow peak
TRUE_PEAKS = ((5.0, 0.35, 650.0), (9.0, 0.40, 400.0), (14.0, 0.45, 300.0))
ELASTIC, BACKGROUND, TEMPERATURE, FWHM = 90.0, 4.0, 302.0, 1.5
MATCH_MEV = 0.75                # 0.5 FWHM, the measured table's tolerance
NARROW_MATCH_MEV = 1.5          # 1 FWHM
NARROW_MARGIN_NATS = 10.0
PASS_AIM, WALL_AIM_S, KILL_S, POOL = 9, 60.0, 600.0, 6


def make_context(counts=None):
    from isar.analyze.context import ScanContext
    x = np.linspace(-16.0, 20.0, 184)
    ctx = ScanContext(x=x, counts=np.ones(x.size) if counts is None else counts, mask=np.ones(x.size, bool),
                      T=TEMPERATURE, center=0.0, gauss_fwhm=FWHM, lorentz_fwhm=0.0, instrument="S30",
                      energy_status="absolute")
    return ctx


def _expected(modes):
    """Expected counts of elastic + the modes (omega0, Gamma, h) + background, and the unit context."""
    from isar.analyze import catalogue
    ctx = make_context()
    label = "elastic+stokes{}+bg".format(len(modes) if len(modes) > 1 else "")
    hyp = next(h for h in catalogue.enumerate_count_structures(ctx, len(modes)) if h.label == label)
    theta = [v for m in modes for v in m] + [ELASTIC, BACKGROUND]
    return hyp.build().evaluate(ctx.x, theta, ctx), hyp, ctx


def narrow_height(gamma):
    """The planted peak's height at which its noiseless deletion grade is NARROW_GRADE (bisection)."""
    def grade(h):
        mean = _expected([*TRUE_PEAKS, (NARROW_ENERGY, gamma, h)])[0]
        reduced = _expected([*TRUE_PEAKS, (NARROW_ENERGY, gamma, 0.0)])[0]
        with np.errstate(divide="ignore", invalid="ignore"):
            terms = np.where(mean > 0, mean * np.log(mean / reduced) - (mean - reduced), 0.0)
        return 2.0 * float(np.sum(terms))
    lo, hi = 0.0, 1.0
    while grade(hi) < NARROW_GRADE:
        hi *= 2.0
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        lo, hi = (mid, hi) if grade(mid) < NARROW_GRADE else (lo, mid)
    return 0.5 * (lo + hi)


def unpriced_counts(models):
    """Admitted counts with no finite priced basin, independent of the contention policy."""
    counts = {m.n_peaks for m in models}
    priced = {m.n_peaks for m in models if m.evidence_status in ("ok", "ok_boundary")
              and m.ln_z is not None and math.isfinite(m.ln_z)}
    return sorted(counts - priced)


def run_one(seed, narrow_gamma, twin_rule, out_json):
    """One row: the fast pass at production defaults on one draw; the row is written as JSON."""
    from isar.analyze import fast_pass
    from isar.analyze.fast_pass import FastPassSettings
    fast_pass.DELTA_TWIN_RULE = bool(twin_rule)
    modes = list(TRUE_PEAKS)
    height = None
    if narrow_gamma is not None:
        height = narrow_height(narrow_gamma)
        modes.append((NARROW_ENERGY, narrow_gamma, height))
    lam, _hyp, ctx = _expected(modes)
    counts = np.random.default_rng(seed).poisson(lam).astype(float)
    ctx = make_context(counts)
    started = time.perf_counter()
    result = fast_pass.run_fast_pass(ctx, settings=FastPassSettings(mode_ceiling=6))
    wall = time.perf_counter() - started
    priced = [m for m in result.models if m.evidence_status in ("ok", "ok_boundary") and m.ln_z is not None]
    best = {}
    for m in priced:
        best[m.n_peaks] = max(best.get(m.n_peaks, -math.inf), m.ln_z)
    peak = lambda p: [p.omega0, p.Gamma, p.deletion_impact]
    row = {
        "seed": seed, "narrow_gamma": narrow_gamma, "twin_rule": bool(twin_rule), "narrow_height": height,
        "state": result.state, "preferred": result.preferred_count,
        "search_limit_reason": result.search_limit_reason,
        "probabilities": {str(k): v for k, v in sorted(result.count_probabilities.items())},
        "confident": [peak(p) for p in result.peaks if p.confident],
        "tentative": [peak(p) for p in result.peaks if not p.confident],
        "delta_modes": list(result.selected.delta_modes) if result.selected else [],
        "boundary_methods": sorted({m.boundary_method or "none" for m in priced}),
        "selected_boundary": result.selected.boundary_method if result.selected else None,
        "ln_z_best": {str(k): v for k, v in sorted(best.items())},
        "counts_considered": sorted({m.n_peaks for m in result.models}),
        "unpriced_counts": unpriced_counts(result.models),
        "n_fits": result.n_fits, "timing": {k: v for k, v in result.timing.items() if k != "families"},
        "wall": wall,
    }
    Path(out_json).write_text(json.dumps(row, indent=1), encoding="utf-8", newline="\n")
    return row


def _spawn(job, workdir):
    seed, narrow_gamma, twin_rule = job
    name = "row-{}-{}-{}".format(seed, "none" if narrow_gamma is None else narrow_gamma, int(twin_rule))
    out = Path(workdir) / (name + ".json")
    env = dict(os.environ, OMP_NUM_THREADS="1", MKL_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1",
               NUMEXPR_NUM_THREADS="1", PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(ROOT))
    cmd = [sys.executable, str(Path(__file__).resolve()), str(seed),
           "none" if narrow_gamma is None else repr(narrow_gamma), str(int(twin_rule)), str(out)]
    started = time.perf_counter()
    try:
        done = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
                              timeout=KILL_S)
        log, code, killed = done.stdout + done.stderr, done.returncode, False
    except subprocess.TimeoutExpired as exc:
        log, code, killed = "killed after {:.0f} s: {}".format(KILL_S, exc), None, True
    wall = time.perf_counter() - started
    Path(workdir, name + ".log").write_text(log, encoding="utf-8", newline="\n")
    if out.exists() and code == 0:
        return json.loads(out.read_text(encoding="utf-8"))
    return {"seed": seed, "narrow_gamma": narrow_gamma, "twin_rule": twin_rule, "state": "killed" if killed else "error",
            "error": log[-400:], "wall": wall, "preferred": None, "probabilities": {}, "confident": [],
            "tentative": [], "delta_modes": [], "boundary_methods": [], "ln_z_best": {}}


def table_jobs(twin_rule):
    return [(s, None, twin_rule) for s in SEEDS] + [(SEEDS[0], g, twin_rule) for g in NARROW_GAMMAS]


def run_table(jobs, workdir):
    """The rows for ``jobs`` (seed, narrow_gamma, twin_rule), in order, from at most POOL processes."""
    workers = max(1, min(POOL, (os.cpu_count() or 2) - 2, len(jobs)))
    with ThreadPoolExecutor(workers) as pool:
        return list(pool.map(lambda job: _spawn(job, workdir), jobs))


def matched(confident, truth, tolerance):
    """True when each truth energy has its own confident peak within ``tolerance``, and nothing else is confident."""
    if len(confident) != len(truth):
        return False
    from scipy.optimize import linear_sum_assignment
    dist = np.abs(np.array([p[0] for p in confident])[:, None] - np.array(truth)[None, :])
    r, c = linear_sum_assignment(dist)
    return bool((dist[r, c] <= tolerance).all())


def judge(row):
    """(passed, why-not) for one row, by decision 9's criterion."""
    if row["state"] in ("killed", "error"):
        return False, row["state"]
    if row["narrow_gamma"] is None:
        if not matched(row["confident"], [t[0] for t in TRUE_PEAKS], MATCH_MEV):
            return False, "confident peaks do not match the three true peaks"
        p3 = row["probabilities"].get("3", 0.0)
        if (row["state"] == "resolved" and row["preferred"] == 3) or (row["state"] == "close_call" and p3 >= 0.05):
            return True, ""
        return False, "state {} preferred {} P(3) {:.3f}".format(row["state"], row["preferred"], p3)
    if not any(abs(p[0] - NARROW_ENERGY) <= NARROW_MATCH_MEV for p in row["confident"]):
        return False, "no confident peak within 1 FWHM of {}".format(NARROW_ENERGY)
    if row["state"] == "unresolved":
        return False, "unresolved"
    z = row["ln_z_best"]
    if "4" not in z or "3" not in z or z["4"] - z["3"] <= NARROW_MARGIN_NATS:
        return False, "ln_z(4) - ln_z(3) not above {} (best models {})".format(NARROW_MARGIN_NATS, z)
    return True, ""


def format_table(rows, title):
    lines = [title, "{:>7} {:>6} {:>11} {:>3} {:<42} {:<28} {:<10} {:<8} {:>6}  {}".format(
        "seed", "narrow", "state", "pref", "P(count)", "confident (omega0/Gamma/impact)", "delta", "boundary",
        "wall", "judge")]
    for r in rows:
        ok, why = judge(r)
        prob = " ".join("{}:{:.2f}".format(k, v) for k, v in r["probabilities"].items() if v >= 0.005)
        conf = " ".join("{:.2f}/{:.2f}/{:.0f}".format(*p) for p in r["confident"])
        lines.append("{:>7} {:>6} {:>11} {:>3} {:<42} {:<28} {:<10} {:<8} {:>6.1f}  {}".format(
            r["seed"], "-" if r["narrow_gamma"] is None else r["narrow_gamma"], r["state"],
            "-" if r["preferred"] is None else r["preferred"], prob, conf, ",".join(map(str, r["delta_modes"])) or "-",
            "/".join(r["boundary_methods"]) or "-", r["wall"], "pass" if ok else "FAIL: " + why))
        if r["tentative"]:
            lines.append("{:>7}   tentative: {}".format("", " ".join("{:.2f}/{:.2f}/{:.0f}".format(*p) for p in r["tentative"])))
        if "unpriced_counts" in r:
            lines.append("{:>7}   wholly unpriced counts: {}".format(
                "", ", ".join(map(str, r["unpriced_counts"])) or "none"))
    return "\n".join(lines)


def summary(rows):
    """(draws passed of the ten, rows unresolved, narrow cases passed, median wall, max wall)."""
    draws = [r for r in rows if r["narrow_gamma"] is None]
    narrow = [r for r in rows if r["narrow_gamma"] is not None]
    walls = [r["wall"] for r in rows]
    return (sum(judge(r)[0] for r in draws), sum(r["state"] == "unresolved" for r in rows),
            sum(judge(r)[0] for r in narrow), statistics.median(walls), max(walls))


def coverage_summary(rows):
    """Count-instance frequency; failed/killed rows carry no evidence-coverage observation."""
    measured = [r for r in rows if "unpriced_counts" in r]
    by_count = {}
    for row in measured:
        for k in row["unpriced_counts"]:
            by_count[k] = by_count.get(k, 0) + 1
    return {
        "rows_measured": len(measured),
        "rows_with_unpriced_count": sum(bool(r["unpriced_counts"]) for r in measured),
        "unpriced_count_instances": sum(by_count.values()),
        "count_instances": sum(len(r["counts_considered"]) for r in measured),
        "unpriced_by_count": dict(sorted(by_count.items())),
    }


if __name__ == "__main__":
    gamma = None if sys.argv[2] == "none" else float(sys.argv[2])
    run_one(int(sys.argv[1]), gamma, int(sys.argv[3]), sys.argv[4])

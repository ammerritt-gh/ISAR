#!/usr/bin/env python
"""Benchmark the four within-model error paradigms on archived delivered scans — one seeded doc-script.

Before the Bayesian posterior path (§5.4) is wired into campaigns we need an honest cost/interval
comparison against the frequentist rungs it would join: how much wall-time does each method spend per
scan, and how wide is the ω₀ interval each one reports? This script re-fits every archived delivered
scan from the reference workspace and times, independently, with :func:`time.perf_counter`:

  1. HESSE       — the MIGRAD+HESSE fit itself (best converged structure); the ω₀ half-width is the
                   fit's own ``omega0_err_hesse`` (Hessian σ, free with the fit).
  2. bootstrap   — :func:`parametric_bootstrap` (M=200); half-width = (hi−lo)/2 of the ω₀ interval.
  3. MINOS/prof  — :func:`minos_intervals` + :func:`omega0_profile_interval`; half-width from the
                   exact 1-D ω₀ profile crossings.
  4. Bayesian    — :func:`bayesian_posterior` (dynesty nested sampling) for each ``--nlive`` (default
                   250 and 400 — the production default vs the step-7-validated setting). Per run we
                   record seconds, the credible half-width, the effective sample size, the §5.4
                   convergence gate, and its fail_reason.

This is a triage tool: where the methods disagree — a HESSE bar far from the bootstrap, a Bayesian
run that fails its gate, a 250-vs-400 credible-width gap — the disagreement is the finding and is
printed as-is, not smoothed.

Trust model = reproducibility: a fixed module-level ``SEED`` (matching ``make_figures.py``) drives
every stochastic method, so a deterministic rerun produces a byte-identical CSV. Run in the tavi-dev
env from the repo root:

    micromamba run -n tavi-dev python docs/stats/bench_error_methods.py \
        --workspace C:/Users/AMM/Documents/ISAR_baselines/mvp_zone_test16
    micromamba run -n tavi-dev python docs/stats/bench_error_methods.py --limit 2        # smoke test
    micromamba run -n tavi-dev python docs/stats/bench_error_methods.py --nlive 250,400,800

Output:
  * docs/stats/figures/bench_error_methods.csv  — one row per (scan × method[/nlive]).
  * a markdown summary table to stdout (seconds + half-widths per method, cross-method ratios,
    and a medians footer).

dynesty runs take ~30–60 s per scan per nlive; use ``--limit N`` to bench a subset. The workspace
(``--workspace`` / env ``ISAR_REF_WORKSPACE``) is required — if it is missing/unreadable the script
stops rather than fabricating data.
"""
from __future__ import annotations

import argparse
import csv
import os
import statistics
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# --------------------------------------------------------------------------- constants
SEED = 20260710                 # same convention as docs/stats/make_figures.py
_BAYES_SEED = SEED + 1_000_003  # distinct namespace for the nested sampler's rstate
_BOOT_M = 200
_DEFAULT_NLIVE = (250, 400)
FIGDIR = Path(__file__).resolve().parent / "figures"
CSV_PATH = FIGDIR / "bench_error_methods.csv"

CSV_COLUMNS = ["scan_id", "in_window_counts", "method", "nlive", "seconds",
               "omega0_halfwidth", "ess", "converged", "fail_reason"]


# --------------------------------------------------------------------------- workspace io
# Copied (small, deliberate duplication — see module docstring) from make_figures.py so this
# doc-script stays self-contained; do NOT refactor make_figures.py to share these.
def _load_card(ws: Path):
    from isar.schema.card import Card
    return Card.load(ws / "card.policy.toml", ws / "card.state.json")


def _load_results(ws: Path) -> dict:
    rows = {}
    with (ws / "report" / "results.csv").open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            rows[int(row["scan"])] = row
    return rows


def _workspace_ctx(ws: Path, card, scan_number: int, row: dict):
    """Standalone ScanContext for one archived scan, built from the archived calibration columns
    (zero / gauss_fwhm / lorentz_fwhm / prior_omega) — archival-faithful, no calibration replumb."""
    from isar.parsers import tavi
    from isar.analyze.context import ScanContext
    rec = tavi.parse_file(ws / "data" / f"j-{scan_number:04d}.json", card)[0]
    det = row["detector"]
    prior = float(row["prior_omega"]) if row.get("prior_omega") else None
    ctx = ScanContext.from_scan(rec, det, card, center=float(row["zero"] or 0.0),
                                gauss_fwhm=float(row["gauss_fwhm"]),
                                lorentz_fwhm=float(row["lorentz_fwhm"]), prior_omega=prior)
    return ctx, rec


def _refit_best(ctx):
    """Unconstrained standalone re-fit → best converged structure (AIC-lite: cost + 2k).

    Mirrors make_figures.py's `_refit_bootstrap` selection but stops at the fit — the timed error
    methods each consume this (fit_result, hypothesis) pair. Returns (None, None) if nothing
    converged."""
    from isar.analyze.structures import enumerate_structures
    from isar.analyze import loop
    hyps = enumerate_structures(ctx)
    pairs = [(loop.fit_hypothesis(h, ctx), h) for h in hyps]
    conv = [(r, h) for r, h in pairs if r.optimizer_converged]
    if not conv:
        return None, None
    return min(conv, key=lambda rh: rh[0].cost + 2.0 * rh[0].k)


# --------------------------------------------------------------------------- helpers
def _hw(lo, hi):
    """Half-width (hi−lo)/2, or nan if either bound is missing/non-finite/disordered."""
    if lo is None or hi is None:
        return float("nan")
    lo, hi = float(lo), float(hi)
    if not (np.isfinite(lo) and np.isfinite(hi)) or hi < lo:
        return float("nan")
    return (hi - lo) / 2.0


def _ratio(a, b):
    a, b = float(a), float(b)
    if not (np.isfinite(a) and np.isfinite(b)) or b == 0.0:
        return float("nan")
    return a / b


def _fnum(x, nd=6):
    """CSV cell for a float: rounded, or '' for None/non-finite (empty ≠ a fabricated 0)."""
    if x is None:
        return ""
    x = float(x)
    return "" if not np.isfinite(x) else repr(round(x, nd))


# --------------------------------------------------------------------------- per-scan bench
def bench_scan(ws: Path, card, scan: int, row: dict, nlives) -> list[dict]:
    """Time all four paradigms on one archived scan. Returns a list of per-method record dicts
    (CSV_COLUMNS keys). Bayesian contributes one record per nlive."""
    from isar.analyze.errors import parametric_bootstrap
    from isar.analyze.errors.profile import minos_intervals, omega0_profile_interval
    from isar.analyze.errors.bayes import bayesian_posterior

    ctx, _rec = _workspace_ctx(ws, card, scan, row)
    counts = int(round(float(np.sum(ctx.counts_used))))
    recs: list[dict] = []

    def rec(method, seconds, hw, *, nlive="", ess="", converged, fail_reason=""):
        d = {"scan_id": scan, "in_window_counts": counts, "method": method,
             "nlive": nlive, "seconds": seconds, "omega0_halfwidth": hw,
             "ess": ess, "converged": converged, "fail_reason": fail_reason}
        recs.append(d)
        return d

    # 1) HESSE — the fit itself; ω₀ σ comes free with MIGRAD+HESSE.
    t0 = time.perf_counter()
    fit, hyp = _refit_best(ctx)
    t_fit = time.perf_counter() - t0
    if fit is None:
        rec("hesse", t_fit, float("nan"), converged=False, fail_reason="no structure converged")
        # Nothing to bench downstream without a converged fit.
        for m in ("bootstrap", "minos_profile"):
            rec(m, float("nan"), float("nan"), converged=False, fail_reason="no fit")
        for nl in nlives:
            rec("bayes", float("nan"), float("nan"), nlive=nl, ess="", converged=False,
                fail_reason="no fit")
        return recs
    rec("hesse", t_fit, float(fit.omega0_err_hesse), converged=True)

    # 2) parametric bootstrap (authoritative frequentist interval).
    t0 = time.perf_counter()
    try:
        boot = parametric_bootstrap(hyp, ctx, fit, M=_BOOT_M, seed=SEED)
        t_boot = time.perf_counter() - t0
        blo, _bm, bhi = boot.omega0_interval
        rec("bootstrap", t_boot, _hw(blo, bhi), converged=boot.n_converged > 0,
            fail_reason="" if boot.n_converged > 0 else "no bootstrap replicate converged")
    except Exception as exc:                                   # pragma: no cover - defensive
        rec("bootstrap", time.perf_counter() - t0, float("nan"), converged=False,
            fail_reason=f"bootstrap raised: {exc}")

    # 3) MINOS + exact ω₀ profile (timed together; profile crossings give the half-width).
    t0 = time.perf_counter()
    try:
        _minos = minos_intervals(hyp, ctx, fit)
        pl, ph = omega0_profile_interval(hyp, ctx, fit)
        t_prof = time.perf_counter() - t0
        hw = _hw(pl, ph)
        rec("minos_profile", t_prof, hw, converged=bool(np.isfinite(hw)),
            fail_reason="" if np.isfinite(hw) else "profile has no clean 1-sigma crossing")
    except Exception as exc:                                   # pragma: no cover - defensive
        rec("minos_profile", time.perf_counter() - t0, float("nan"), converged=False,
            fail_reason=f"profile raised: {exc}")

    # 4) Bayesian posterior (dynesty) — one run per nlive. Same seed across nlive so the only
    #    difference is the live-point budget (the honest 250-vs-400 comparison).
    for nl in nlives:
        t0 = time.perf_counter()
        b = bayesian_posterior(hyp, ctx, fit, prior=None, seed=_BAYES_SEED, n_live=int(nl),
                               dlogz=0.5, max_iter=30_000)
        t_b = time.perf_counter() - t0
        blo, _bm, bhi = b.omega0_interval
        rec("bayes", t_b, _hw(blo, bhi), nlive=int(nl), ess=float(b.ess),
            converged=bool(b.converged), fail_reason=b.fail_reason or "")
    return recs


# --------------------------------------------------------------------------- output
def write_csv(records: list[dict]) -> Path:
    FIGDIR.mkdir(parents=True, exist_ok=True)
    with CSV_PATH.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_COLUMNS)
        for r in records:
            w.writerow([
                r["scan_id"], r["in_window_counts"], r["method"], r["nlive"],
                _fnum(r["seconds"], 4), _fnum(r["omega0_halfwidth"], 6),
                _fnum(r["ess"], 1) if r["ess"] != "" else "",
                "1" if r["converged"] else "0", r["fail_reason"],
            ])
    return CSV_PATH


def _pivot(records: list[dict], nlives) -> list[dict]:
    """Group per-scan records into one summary row keyed by scan_id, in method column order."""
    by_scan: dict[int, dict] = {}
    for r in records:
        s = by_scan.setdefault(r["scan_id"], {"scan_id": r["scan_id"],
                                              "counts": r["in_window_counts"]})
        if r["method"] == "bayes":
            s[f"bayes{r['nlive']}"] = r
        else:
            s[r["method"]] = r
    return [by_scan[k] for k in sorted(by_scan)]


def print_summary(records: list[dict], nlives) -> None:
    rows = _pivot(records, nlives)
    bkeys = [f"bayes{nl}" for nl in nlives]

    def sec(row, key):
        return row.get(key, {}).get("seconds", float("nan"))

    def hw(row, key):
        return row.get(key, {}).get("omega0_halfwidth", float("nan"))

    # Header: seconds per method, then half-widths, then the requested cross-method ratios.
    sec_cols = ["hesse", "bootstrap", "minos_profile"] + bkeys
    hdr = (["scan", "cts"]
           + [f"t_{k}" for k in sec_cols]
           + [f"hw_{k}" for k in ["hesse", "bootstrap", "minos_profile"] + bkeys]
           + ["boot/b%d" % nl for nl in nlives]
           + ["hesse/boot"])
    print("\n### Error-method benchmark — per scan "
          f"(seed {SEED}, bootstrap M={_BOOT_M}, nlive {','.join(str(n) for n in nlives)})\n")
    print("| " + " | ".join(hdr) + " |")
    print("|" + "|".join(["---"] * len(hdr)) + "|")

    agg: dict[str, list] = {k: [] for k in [f"t_{c}" for c in sec_cols]}
    ratio_agg: dict[str, list] = {}
    for row in rows:
        cells = [str(row["scan_id"]), str(row["counts"])]
        for k in sec_cols:
            v = sec(row, k)
            cells.append(f"{v:.2f}" if np.isfinite(v) else "-")
            if np.isfinite(v):
                agg[f"t_{k}"].append(v)
        for k in ["hesse", "bootstrap", "minos_profile"] + bkeys:
            v = hw(row, k)
            cells.append(f"{v:.4f}" if np.isfinite(v) else "-")
        for nl in nlives:
            r = _ratio(hw(row, "bootstrap"), hw(row, f"bayes{nl}"))
            cells.append(f"{r:.2f}" if np.isfinite(r) else "-")
            ratio_agg.setdefault(f"boot/b{nl}", []).append(r)
        rhb = _ratio(hw(row, "hesse"), hw(row, "bootstrap"))
        cells.append(f"{rhb:.2f}" if np.isfinite(rhb) else "-")
        ratio_agg.setdefault("hesse/boot", []).append(rhb)
        print("| " + " | ".join(cells) + " |")

    # Footer: medians (times + ratios). Half-width columns get a blank spacer.
    def med(vals):
        v = [x for x in vals if np.isfinite(x)]
        return statistics.median(v) if v else float("nan")

    foot = ["**median**", ""]
    for k in sec_cols:
        m = med(agg[f"t_{k}"])
        foot.append(f"{m:.2f}" if np.isfinite(m) else "-")
    foot += [""] * len(["hesse", "bootstrap", "minos_profile"] + bkeys)
    for nl in nlives:
        m = med(ratio_agg.get(f"boot/b{nl}", []))
        foot.append(f"{m:.2f}" if np.isfinite(m) else "-")
    m = med(ratio_agg.get("hesse/boot", []))
    foot.append(f"{m:.2f}" if np.isfinite(m) else "-")
    print("| " + " | ".join(foot) + " |")

    # Bayesian gate/ESS notes — disagreements printed as-is.
    print("\n**Bayesian run detail** (converged gate + ESS):\n")
    print("| scan | nlive | seconds | halfwidth | ess | converged | fail_reason |")
    print("|---|---|---|---|---|---|---|")
    for r in records:
        if r["method"] != "bayes":
            continue
        ess = r["ess"]
        print(f"| {r['scan_id']} | {r['nlive']} | "
              f"{r['seconds']:.2f} | "
              f"{r['omega0_halfwidth']:.4f} | "
              f"{ess:.0f} | {'yes' if r['converged'] else 'NO'} | {r['fail_reason']} |")


# --------------------------------------------------------------------------- cli
def _require_ws(ws: str | None) -> Path:
    if not ws:
        raise SystemExit("this benchmark needs --workspace (or env ISAR_REF_WORKSPACE): the archived "
                         "reference workspace mvp_zone_test16")
    p = Path(ws)
    if not (p / "report" / "results.csv").exists():
        raise SystemExit(f"workspace {p} has no report/results.csv — cannot benchmark (refusing to "
                         "fabricate data)")
    return p


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Benchmark wall-time + interval widths of the four "
                                             "within-model error methods on archived scans.")
    ap.add_argument("--workspace",
                    default=os.environ.get("ISAR_REF_WORKSPACE",
                                           "C:/Users/AMM/Documents/ISAR_baselines/mvp_zone_test16"),
                    help="archived reference workspace (default env ISAR_REF_WORKSPACE, then "
                         "the mvp_zone_test16 baseline)")
    ap.add_argument("--nlive", default="250,400",
                    help="comma list of dynesty live-point budgets to bench (default 250,400)")
    ap.add_argument("--limit", type=int, default=None,
                    help="bench only the first N scans (default all); use --limit 2 to smoke test")
    args = ap.parse_args(argv)

    ws = _require_ws(args.workspace)
    nlives = [int(x) for x in str(args.nlive).split(",") if x.strip()]
    card = _load_card(ws)
    rows = _load_results(ws)
    scans = sorted(rows)
    if args.limit is not None:
        scans = scans[:args.limit]

    print(f"benchmarking {len(scans)} scan(s) from {ws}")
    all_records: list[dict] = []
    for scan in scans:
        t0 = time.perf_counter()
        try:
            recs = bench_scan(ws, card, scan, rows[scan], nlives)
        except ValueError as exc:
            # Unfittable archived scan (e.g. anti-Stokes-only window: structure_ic has no
            # Stokes-side candidate grid). Record + skip — the bench compares error methods
            # on fittable scans; it is not a fit-robustness harness.
            print(f"  scan {scan}: SKIPPED ({exc})")
            all_records.append({"scan_id": scan, "in_window_counts": "", "method": "hesse",
                                "nlive": "", "seconds": float("nan"),
                                "omega0_halfwidth": float("nan"), "ess": "",
                                "converged": False, "fail_reason": f"unfittable: {exc}"})
            continue
        all_records.extend(recs)
        print(f"  scan {scan}: {time.perf_counter() - t0:.1f}s")

    path = write_csv(all_records)
    print_summary(all_records, nlives)
    print(f"\nCSV -> {path}")


if __name__ == "__main__":
    main()

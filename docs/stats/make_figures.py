#!/usr/bin/env python
"""Generate the STATS_PRIMER figure gallery — one deterministic, seeded script.

Every figure in ``docs/STATS_PRIMER.md`` is produced here so a skeptical reader can regenerate
the evidence and check the numbers, not the prose. The trust model is reproducibility: fixed
``SEED``, per-figure RNGs, and a small JSON dump of the plotted numbers next to every figure so
determinism is checkable without hashing PNG bytes.

Six figures (seven output images):
  1. three_questions.svg   — static pipeline map (no data)
  2. cost_bias.png         — the estimator-straddle identity + measured amplitude bias
  3. coverage_table.png    \\  measured 68% coverage of the interval ladder
     coverage_curve.png    /   (+ coverage_summary.json)
  4. wall_figure.png       — the Γ-at-bound degeneracy (archived scans, standalone re-fit)
  5. akaike_example.png    — model selection by Akaike weights (archived scan 507)
  6. branch_posterior.png  — the earned Bayesian piece (archived branch entropy)

Figures 4–6 need the archived reference workspace (``--workspace`` / env ``ISAR_REF_WORKSPACE``);
figures 1–3 are synthetic/static and need no workspace. Run from the repo root:

    python docs/stats/make_figures.py --workspace C:/Users/AMM/Documents/ISAR_baselines/mvp_zone_test16
    python docs/stats/make_figures.py --only three_questions            # no workspace
    python docs/stats/make_figures.py --only cost_bias --workers 8
    python docs/stats/make_figures.py --only coverage --fast --workers 8

``--fast`` runs the coverage ensemble at N=40/level, M=50 and writes ``coverage_summary.fast.json``
(never overwriting the committed full-run ``coverage_summary.json``). ``--workers`` spreads the
synthetic ensembles over a process pool; results are worker-count-invariant (each scan is an
independent, deterministically-seeded task). ``--outdir`` redirects every output (images, JSON
dumps and caption sidecars) to another directory, which is how the regeneration test regenerates
into a scratch directory instead of over the committed gallery.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt          # noqa: E402
import numpy as np                       # noqa: E402
from matplotlib.patches import FancyBboxPatch  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from isar.report import captions          # noqa: E402
from isar.analyze import estimators as _est  # noqa: E402

# --------------------------------------------------------------------------- constants
SEED = 20260710
# Default output directory. `main()` rebinds this module global when --outdir is given: every
# writer below (_ensure_figdir/_save_png/_save_svg/_dump_json) and the --replot-from-dump reader
# resolve paths against it, so a single rebind redirects the whole script. Threading a parameter
# through every figure function instead would touch a dozen signatures for no behavioural gain.
FIGDIR = Path(__file__).resolve().parent / "figures"
_DEFAULT_FIGDIR = FIGDIR

# Per-figure RNG/seed offsets (SEED + offset) so figures never share a stream.
_OFF_COSTBIAS_TOY = 2      # panel (a): the single Poisson draw
_OFF_COSTBIAS_ENS = 1000   # panel (b): amplitude-bias ensemble seeds
_OFF_COVERAGE = 2000       # coverage ensemble scan seeds
_OFF_COVERAGE_BOOT = 5000  # coverage bootstrap seeds (distinct namespace)
_OFF_GAMMA_SKEW = 9000     # skewed-Γ ensemble scan seeds (distinct namespace)
_OFF_GAMMA_SKEW_BOOT = 9500

# Skewed-regime Γ cell (coordinator spec): small Γ near the resolution/bound where the Γ posterior
# is visibly right-skewed but the fits are NOT majority at-bound. Chosen from an N=20 probe over
# {0.10, 0.15, 0.20}: Γ=0.10 has n_at_bound≈0.25 (<0.30) and the strongest bootstrap-interval
# asymmetry (~1.19) — the smallest value that stays sub-majority-at-bound.
_GAMMA_SKEW_TRUE = 0.10
_GAMMA_SKEW_LEVEL = 15

# cost_bias panel (b) + coverage share one fixed synthetic dispersion point (mid-zone).
_TOY_H = 2.5
_COSTBIAS_LEVELS = (5, 15, 30, 100)
_COSTBIAS_N = 500
_COVERAGE_LEVELS = (15, 30, 100)
_COVERAGE_N_FULL = 200
_COVERAGE_N_FAST = 40
_COVERAGE_M_FULL = 100
_COVERAGE_M_FAST = 50

_ESTIMATOR_ORDER = ("baker_cousins", "neyman", "pearson")
_ESTIMATOR_LABEL = {
    "baker_cousins": "Baker–Cousins (arithmetic mean)",
    "neyman": "Neyman χ² (harmonic mean)",
    "pearson": "mod-Pearson χ² (RMS)",
}
_ESTIMATOR_COLOR = {"baker_cousins": "C0", "neyman": "C1", "pearson": "C2"}

# Archived scans (see results.csv / session.json in the reference workspace).
# Healthy panel: the archived interval_reliable=True scans (505/507) were fit *branch-conditionally*
# in the campaign (Γ conditioned), so a standalone unconstrained re-fit of 505 collapses like the
# degenerate case. The healthy panel therefore uses a scan with a genuine interior Γ minimum whose
# standalone bootstrap does NOT collapse — 529 (Γ≈0.02) first, then broader fallbacks.
_WALL_HEALTHY_CANDIDATES = (529, 533, 507)
_WALL_DEGEN_CANDIDATES = (518, 540, 542, 544, 501)   # interval_reliable=False; expect collapse
_AKAIKE_SCAN = 507
_BRANCH_CERTAIN = 505
_BRANCH_AMBIGUOUS = 477


# --------------------------------------------------------------------------- io helpers
def _ensure_figdir() -> None:
    FIGDIR.mkdir(parents=True, exist_ok=True)


def _save_png(fig, name: str, fig_id: str, *, draw: bool = True) -> Path:
    return captions.save_captioned(fig, FIGDIR / f"{name}.png", fig_id, dpi=110, draw=draw)


def _save_svg(
    fig,
    name: str,
    fig_id: str,
    *,
    lifecycle: str | None = None,
    provenance: str | None = None,
) -> Path:
    """SVG path: the caption helper is PNG-centric, so save the vector figure directly and
    write the sidecar via the public path-suffix-agnostic API (no helper change)."""
    path = FIGDIR / f"{name}.svg"
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    cap = captions.caption_for(fig_id)
    if cap is not None:
        captions.write_sidecar(path, cap, lifecycle=lifecycle, provenance=provenance)
    return path


def _dump_json(name: str, obj) -> Path:
    path = FIGDIR / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _r(x, nd=6):
    """Round for a stable JSON dump (numbers, not PNG bytes, are the determinism contract)."""
    if x is None:
        return None
    x = float(x)
    return None if not np.isfinite(x) else round(x, nd)


# =========================================================================== FIGURE 1
def figure_three_questions() -> Path:
    """Static pipeline map: MINIMIZE → SELECT → CERTIFY, with the Bayesian rung drawn dashed
    (Implemented but diagnostics-only — it never certifies). No data dependency."""
    fig, ax = plt.subplots(figsize=(9.5, 5.0))
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 10)
    ax.axis("off")

    cols = [
        ("MINIMIZE", "C0", 1.6,
         "which parameters?", "Baker–Cousins −2lnL\n(not a χ²)",
         "cost, cost_per_dof"),
        ("SELECT", "C1", 5.0,
         "which model?", "Akaike weights\n+ nested LRT",
         "best_model, n_modes,\nmodes.csv"),
        ("CERTIFY", "C2", 8.4,
         "what uncertainty?", "the interval ladder\n(below)",
         "interval_lo/hi,\ninterval_reliable"),
    ]
    for title, color, xc, q, method, columns in cols:
        ax.add_patch(FancyBboxPatch((xc - 1.35, 6.4), 2.7, 3.0,
                                    boxstyle="round,pad=0.08", ec=color, fc="none", lw=2.0))
        ax.text(xc, 9.05, title, ha="center", va="center", fontsize=12, color=color, weight="bold")
        ax.text(xc, 8.4, q, ha="center", va="center", fontsize=9, style="italic", color="0.3")
        ax.text(xc, 7.6, method, ha="center", va="center", fontsize=8.5, color="0.15")
        ax.text(xc, 6.75, columns, ha="center", va="center", fontsize=7.5, color="0.45",
                family="monospace")
    for x0, x1 in ((3.0, 3.6), (6.35, 6.95)):
        ax.annotate("", xy=(x1, 7.9), xytext=(x0, 7.9),
                    arrowprops=dict(arrowstyle="-|>", color="0.5", lw=1.4))

    # The four-rung ladder under CERTIFY.
    # `noncertifying` = drawn dashed. The Bayesian rung is Implemented (--errors bayes) but its
    # interval is diagnostics-only; bootstrap remains the certifying rung (ruling S5).
    rungs = [("Hessian (HESSE)", "C2", False), ("profile (MINOS)", "C2", False),
             ("parametric bootstrap", "C2", False), ("Bayesian credible", "0.55", True)]
    y0, dy, xc = 5.2, 1.0, 8.4
    for i, (name, color, noncertifying) in enumerate(rungs):
        y = y0 - i * dy
        style = "round,pad=0.05"
        if noncertifying:
            ax.add_patch(FancyBboxPatch((xc - 1.7, y - 0.32), 3.4, 0.64, boxstyle=style,
                                        ec=color, fc="none", lw=1.4, ls="--"))
            ax.text(xc, y, name + "   [diagnostics-only]", ha="center", va="center", fontsize=8,
                    color=color, style="italic")
        else:
            ax.add_patch(FancyBboxPatch((xc - 1.7, y - 0.32), 3.4, 0.64, boxstyle=style,
                                        ec=color, fc="none", lw=1.4))
            ax.text(xc, y, name, ha="center", va="center", fontsize=8, color="0.15")
    ax.annotate("more expensive,\nmore honest", xy=(xc - 2.15, y0 - 3 * dy),
                xytext=(xc - 2.15, y0), ha="center", va="center", fontsize=7.5, color="0.4",
                arrowprops=dict(arrowstyle="-|>", color="0.6", lw=1.2, ls="--"))

    ax.text(0.35, 3.2, "cross-rung agreement is the trust signal:\n"
            "  hesse_trusted — HESSE band matches the bootstrap\n"
            "  interval_reliable — the fit had an interior minimum",
            ha="left", va="center", fontsize=8, color="0.35", family="monospace")
    ax.set_title("Every ISAR results row answers one of three questions", fontsize=12, pad=8)

    _dump_json("three_questions", {
        "columns": [c[0] for c in cols],
        "ladder": [r[0] for r in rungs],
        "noncertifying_rung": "Bayesian credible",
        # Deprecated compatibility alias; keep equal to noncertifying_rung for existing readers.
        "planned_rung": "Bayesian credible",
        "seed": SEED,
    })
    return _save_svg(
        fig,
        "three_questions",
        "stats/three_questions",
        lifecycle="Current",
        provenance="generated by `python docs/stats/make_figures.py --only three_questions`",
    )


# =========================================================================== FIGURE 2
def _costbias_panel_a(ax) -> dict:
    """Closed-form estimator straddle on one seeded constant-rate Poisson draw."""
    rng = np.random.default_rng(SEED + _OFF_COSTBIAS_TOY)
    mu_true = 4.0
    y = rng.poisson(mu_true, 10).astype(float)
    mus = np.linspace(max(0.5, float(y.min()) - 1.0), float(y.max()) + 1.5, 400)
    mins = {}
    for est in _ESTIMATOR_ORDER:
        cost = _est.get_estimator(est).cost
        curve = np.array([cost(y, np.full_like(y, m)) for m in mus])
        curve = curve - float(curve.min())
        ax.plot(mus, curve, "-", color=_ESTIMATOR_COLOR[est], lw=1.8,
                label=_ESTIMATOR_LABEL[est])
        m_hat = _est.constant_rate_estimate(y, est)
        mins[est] = float(m_hat)
        ymark = float(np.interp(m_hat, mus, curve))
        ax.plot([m_hat, m_hat], [0, ymark], "--", color=_ESTIMATOR_COLOR[est], lw=1.0)
        ax.plot([m_hat], [ymark], "o", color=_ESTIMATOR_COLOR[est], ms=5)
    ax.axvline(mu_true, color="0.4", ls=":", lw=1.0, label=f"truth μ={mu_true:g}")
    # Minimizer values as one legible block (the drop-lines + o-markers carry the colour mapping);
    # the three values (3.96/4.60/4.96) are too close to label at the axis without overlapping.
    block = ("minimizers:\n"
             f"harmonic (Neyman) = {mins['neyman']:.2f}\n"
             f"arithmetic (BC) = {mins['baker_cousins']:.2f}\n"
             f"RMS (Pearson) = {mins['pearson']:.2f}")
    ax.text(0.02, 0.98, block, transform=ax.transAxes, va="top", ha="left", fontsize=6.8,
            color="0.15", family="monospace",
            bbox=dict(boxstyle="round", fc="white", ec="0.7", alpha=0.9))
    ax.set_ylim(bottom=-0.3)
    ax.set_xlabel("constant rate μ (counts)")
    ax.set_ylabel("cost − min(cost)")
    ax.set_title("(a) the χ²'s straddle the likelihood (n=10, μ=4)", fontsize=9.5)
    ax.legend(fontsize=6.8, loc="upper right")
    return {"mu_true": mu_true, "counts": [float(v) for v in y],
            "minimizers": {k: _r(v) for k, v in mins.items()}}


def _costbias_worker(task):
    """One synthetic scan fit under all three estimators → per-estimator A_fit/A_true."""
    level, i, H = task
    from isar.synth import config as cfg_mod, generate as gen, fit as fit_mod
    cfg = cfg_mod.default_run_config()
    gt = gen.make_ground_truth(cfg, H, float(level), seed=SEED + _OFF_COSTBIAS_ENS
                               + level * 100000 + i)
    x, counts, gt = gen.generate_scan(gt)
    out = {}
    for est in ("baker_cousins", "neyman", "pearson"):
        res = fit_mod.fit(x, counts, gauss_fwhm=gt.gauss_fwhm, lorentz_fwhm=gt.lorentz_fwhm,
                          T=gt.T, center=gt.center, estimator=est)
        ok = res.success and gt.phonon_amplitude > 0 and np.isfinite(res.phonon_amplitude)
        out[est] = float(res.phonon_amplitude / gt.phonon_amplitude) if ok else float("nan")
    return level, out


def _costbias_panel_b(ax, workers: int) -> dict:
    """Measured amplitude bias vs counts (N=500/level), each scan fit under all three estimators."""
    tasks = [(level, i, _TOY_H) for level in _COSTBIAS_LEVELS for i in range(_COSTBIAS_N)]
    ratios = {level: {est: [] for est in _ESTIMATOR_ORDER} for level in _COSTBIAS_LEVELS}
    for level, out in _map_tasks(_costbias_worker, tasks, workers):
        for est in _ESTIMATOR_ORDER:
            v = out[est]
            if np.isfinite(v):
                ratios[level][est].append(v)

    stats: dict = {}
    for j, est in enumerate(_ESTIMATOR_ORDER):
        xs, ys, es = [], [], []
        for level in _COSTBIAS_LEVELS:
            arr = np.array(ratios[level][est], dtype=float)
            mean = float(np.mean(arr)) if arr.size else float("nan")
            se = float(np.std(arr, ddof=1) / np.sqrt(arr.size)) if arr.size > 1 else float("nan")
            xs.append(level * (1.0 + 0.05 * (j - 1)))   # deterministic ±5% jitter, no RNG
            ys.append(mean)
            es.append(se)
            stats.setdefault(str(level), {})[est] = {
                "mean_ratio": _r(mean), "se": _r(se), "n": int(arr.size),
                "bias_pct": _r((mean - 1.0) * 100.0, 3)}
        ax.errorbar(xs, ys, yerr=es, fmt="osD"[j] + "-", color=_ESTIMATOR_COLOR[est],
                    ecolor="k", capsize=3, ms=5, lw=1.4, label=_ESTIMATOR_LABEL[est])
    ax.axhline(1.0, color="0.6", ls="-", lw=0.8)
    ax.set_xscale("log")
    ax.set_xticks(_COSTBIAS_LEVELS)
    ax.set_xticklabels([str(c) for c in _COSTBIAS_LEVELS])
    ax.set_xlabel("phonon peak counts")
    ax.set_ylabel("⟨A_fit / A_true⟩")
    ax.set_title(f"(b) amplitude bias vs counts (N={_COSTBIAS_N}/level)", fontsize=9.5)
    ax.legend(fontsize=6.8, loc="best")
    low = _COSTBIAS_LEVELS[0]
    note = "  ".join(
        f"{_ESTIMATOR_LABEL[est].split()[0]}: {stats[str(low)][est]['bias_pct']:+.1f}%"
        for est in _ESTIMATOR_ORDER)
    ax.annotate(f"at {low} counts:  {note}", xy=(0.5, 0.02), xycoords="axes fraction",
                ha="center", va="bottom", fontsize=6.8, color="0.3")
    return stats


def figure_cost_bias(workers: int) -> Path:
    fig, (axa, axb) = plt.subplots(1, 2, figsize=(11.0, 4.6))
    dump_a = _costbias_panel_a(axa)
    dump_b = _costbias_panel_b(axb, workers)
    _dump_json("cost_bias", {"panel_a": dump_a, "panel_b": dump_b,
                             "N_per_level": _COSTBIAS_N, "H": _TOY_H, "seed": SEED})
    return _save_png(fig, "cost_bias", "stats/cost_bias", draw=False)


# =========================================================================== FIGURE 3
def _synth_record(level, i, M, gamma_true, seed, boot_seed) -> dict:
    """One synthetic scan → a rich per-scan record (fitted values, truths, per-method intervals,
    gamma_at_bound). Coverage/diagnostics are tallied downstream so the raw records stay
    post-hoc-analyzable (the anomaly-diagnosis contract). No new fits are added for Γ: its Hessian
    band is the fit's own ``gamma ± gamma_err`` and its bootstrap band is ``boot.gamma_interval``.
    ``gamma_true=None`` uses the config default (0.5); the skewed cell passes a small Γ."""
    from isar.synth import config as cfg_mod, generate as gen
    from isar.analyze.context import ScanContext
    from isar.analyze.phonon import enumerate_phonon_structures as enumerate_structures
    from isar.analyze import loop
    from isar.analyze.errors import bootstrap as bs, profile as prof
    cfg = cfg_mod.default_run_config()
    gt = gen.make_ground_truth(cfg, _TOY_H, float(level), seed=seed, gamma=gamma_true)
    x, counts, gt = gen.generate_scan(gt)
    ctx = ScanContext(x=x, counts=counts, mask=np.ones(counts.size, dtype=bool), T=gt.T,
                      center=gt.center, gauss_fwhm=gt.gauss_fwhm, lorentz_fwhm=gt.lorentz_fwhm,
                      instrument="S30", energy_status="absolute")
    rec = {"level": level, "seed": int(seed), "converged": False,
           "omega0_true": float(gt.omega0), "gamma_true": float(gt.gamma)}
    hyp = {h.label: h for h in enumerate_structures(ctx)}.get("elastic+stokes+bg")
    if hyp is None:
        return rec
    res = loop.fit_hypothesis(hyp, ctx)
    if not res.optimizer_converged:
        return rec

    def _iv(lo, hi):
        return [float(lo), float(hi)] if (np.isfinite(lo) and np.isfinite(hi)) else None

    o_sig, g_sig = res.omega0_err_hesse, res.gamma_err
    pl, ph = prof.omega0_profile_interval(hyp, ctx, res)
    boot = bs.parametric_bootstrap(hyp, ctx, res, M=M, seed=boot_seed, workers=1)
    blo, _bm, bhi = boot.omega0_interval
    bglo, bgm, bghi = boot.gamma_interval
    rec.update({
        "converged": True,
        "omega0_hat": float(res.omega0), "omega0_err_hesse": float(o_sig),
        "gamma_hat": float(res.gamma), "gamma_err": float(g_sig),
        "gamma_at_bound": bool(res.gamma_at_bound),
        "o0_hess": _iv(res.omega0 - o_sig, res.omega0 + o_sig) if o_sig > 0 else None,
        "o0_prof": _iv(pl, ph),
        "o0_boot": _iv(blo, bhi),
        "g_hess": _iv(res.gamma - g_sig, res.gamma + g_sig) if g_sig > 0 else None,
        "g_boot": _iv(bglo, bghi),
        "g_boot_med": float(bgm) if np.isfinite(bgm) else None,  # for the Γ-interval asymmetry
    })
    return rec


def _coverage_worker(task):
    """Main-ensemble scan (Γ_true = config default 0.5)."""
    level, i, M = task
    return _synth_record(level, i, M, None, SEED + _OFF_COVERAGE + level * 100000 + i,
                         SEED + _OFF_COVERAGE_BOOT + level * 100000 + i)


def _gamma_skew_worker(task):
    """Skewed-regime scan: small Γ near the resolution/bound, distinct seed namespace."""
    level, i, M = task
    return _synth_record(level, i, M, _GAMMA_SKEW_TRUE, SEED + _OFF_GAMMA_SKEW + i,
                         SEED + _OFF_GAMMA_SKEW_BOOT + i)


def _cover(iv, true) -> bool | None:
    if iv is None:
        return None
    return bool(iv[0] <= true <= iv[1])


def _cell(flags: list) -> dict:
    n = len(flags)
    cov = float(np.mean(flags)) if n else float("nan")
    se = float(np.sqrt(cov * (1.0 - cov) / n)) if n else float("nan")
    return {"coverage": _r(cov, 4), "se": _r(se, 4), "n": n, "n_covered": int(sum(flags))}


def _diag(vals: dict) -> dict:
    """Bias / pull / median-σ diagnostics for one (level, param). ``vals`` carries parallel arrays
    of (hat, true, sigma_hesse, sigma_by_method)."""
    hat = np.asarray(vals["hat"], float)
    tru = np.asarray(vals["true"], float)
    sig = np.asarray(vals["sig"], float)
    out = {"n": int(hat.size)}
    if hat.size:
        resid = hat - tru
        out["bias_mean"] = _r(float(np.mean(resid)))
        out["bias_se"] = _r(float(np.std(resid, ddof=1) / np.sqrt(hat.size)) if hat.size > 1
                            else float("nan"))
        good = sig > 0
        if good.any():
            pull = resid[good] / sig[good]
            out["pull_mean"] = _r(float(np.mean(pull)))
            out["pull_std"] = _r(float(np.std(pull, ddof=1)) if pull.size > 1 else float("nan"))
        out["median_sigma"] = {m: _r(float(np.median(a))) for m, a in vals["sigma_by_method"].items()
                               if len(a)}
    return out


# Per-scan HESSE/bootstrap interval-WIDTH ratio — the single-scan deliverable. Even when ENSEMBLE
# coverage matches, one scan's HESSE bar can be far from its bootstrap bar; the ladder's per-scan
# agreement (hesse_trusted) is what the experimenter actually leans on. Band is log-symmetric
# (1/0.8 = 1.25): a HESSE bar within ±25% of the bootstrap is "in agreement".
_RATIO_BAND = (0.8, 1.25)


def _hb_ratio(rec, hess_key, boot_key):
    """width(HESSE) / width(bootstrap) for one scan, or None if either interval is missing/degenerate."""
    h, b = rec.get(hess_key), rec.get(boot_key)
    if h is None or b is None:
        return None
    wh, wb = float(h[1]) - float(h[0]), float(b[1]) - float(b[0])
    if not (wb > 0 and np.isfinite(wh) and np.isfinite(wb)):
        return None
    return wh / wb


def _level_ratios(records, hess_key, boot_key, level, *, exclude_at_bound=False):
    out = []
    for r in records:
        if r.get("level") != level or not r.get("converged"):
            continue
        if exclude_at_bound and r.get("gamma_at_bound"):
            continue
        v = _hb_ratio(r, hess_key, boot_key)
        if v is not None:
            out.append(v)
    return out


def _ratio_stats(ratios) -> dict:
    a = np.asarray([r for r in ratios if r is not None and np.isfinite(r)], dtype=float)
    if a.size == 0:
        return {"n": 0, "band": list(_RATIO_BAND)}
    lo, hi = _RATIO_BAND
    q25, q75 = float(np.percentile(a, 25)), float(np.percentile(a, 75))
    return {"n": int(a.size), "median": _r(float(np.median(a)), 4),
            "q25": _r(q25, 4), "q75": _r(q75, 4), "iqr": _r(q75 - q25, 4),
            "frac_outside": _r(float(np.mean((a < lo) | (a > hi))), 4), "band": list(_RATIO_BAND)}


def _dump_main(records) -> list:
    """The per-scan dump for the main ensemble (converged scans; scalars rounded, intervals kept)."""
    out = []
    for r in records:
        if not r.get("converged"):
            continue
        out.append({k: (_r(v, 6) if isinstance(v, float) else v)
                    for k, v in r.items() if k not in ("o0_hess", "o0_prof", "o0_boot",
                                                        "g_hess", "g_boot")}
                   | {k: r.get(k) for k in ("o0_hess", "o0_prof", "o0_boot", "g_hess", "g_boot")})
    return out


def _dump_skew(records) -> list:
    out = []
    for r in records:
        if not r.get("converged"):
            continue
        out.append({k: (_r(v, 6) if isinstance(v, float) else v)
                    for k, v in r.items() if not isinstance(r.get(k), list)}
                   | {k: r.get(k) for k in ("g_hess", "g_boot")})
    return out


def _coverage_fit(workers: int, fast: bool) -> tuple[list, dict]:
    N = _COVERAGE_N_FAST if fast else _COVERAGE_N_FULL
    M = _COVERAGE_M_FAST if fast else _COVERAGE_M_FULL
    tasks = [(level, i, M) for level in _COVERAGE_LEVELS for i in range(N)]
    records = list(_map_tasks(_coverage_worker, tasks, workers))
    meta = {"N_per_level": N, "M": M, "levels": list(_COVERAGE_LEVELS), "H": _TOY_H, "seed": SEED,
            "fast": fast, "nominal": 0.68, "gamma_true": None}
    return records, meta


def _gamma_skew_fit(workers: int, fast: bool) -> list:
    N = _COVERAGE_N_FAST if fast else _COVERAGE_N_FULL
    M = _COVERAGE_M_FAST if fast else _COVERAGE_M_FULL
    tasks = [(_GAMMA_SKEW_LEVEL, i, M) for i in range(N)]
    return list(_map_tasks(_gamma_skew_worker, tasks, workers))


def _summarize(records: list, meta: dict) -> dict:
    """Build the coverage summary (cells + diagnostics, incl. per-scan width-ratio stats) from a list
    of per-scan records. Works identically on freshly-fitted records and on records reloaded from the
    per-scan dump — the shared path that lets ``--replot-from-dump`` skip all fitting."""
    meta = dict(meta)
    levels = meta["levels"]
    summary = {"meta": meta, "cells": {}, "diagnostics": {}}
    for level in levels:
        recs = [r for r in records if r.get("level") == level and r.get("converged")]
        o_flags = {m: [] for m in ("hessian", "profile", "bootstrap")}
        g_flags = {m: [] for m in ("hessian", "bootstrap")}
        g_flags_incl = {m: [] for m in ("hessian", "bootstrap")}
        n_at_bound = 0
        o_diag = {"hat": [], "true": [], "sig": [],
                  "sigma_by_method": {"hessian": [], "profile": [], "bootstrap": []}}
        g_diag = {"hat": [], "true": [], "sig": [], "sigma_by_method": {"hessian": [], "bootstrap": []}}
        for r in recs:
            ot, gt_ = r["omega0_true"], r["gamma_true"]
            meta["gamma_true"] = _r(gt_)
            o_flags["hessian"].append(_cover(r["o0_hess"], ot))
            o_flags["profile"].append(_cover(r["o0_prof"], ot))
            o_flags["bootstrap"].append(_cover(r["o0_boot"], ot))
            o_diag["hat"].append(r["omega0_hat"]); o_diag["true"].append(ot)
            o_diag["sig"].append(r["omega0_err_hesse"])
            for m, iv in (("hessian", r["o0_hess"]), ("profile", r["o0_prof"]),
                          ("bootstrap", r["o0_boot"])):
                if iv is not None:
                    o_diag["sigma_by_method"][m].append((iv[1] - iv[0]) / 2.0)
            at_bound = r["gamma_at_bound"]
            n_at_bound += int(at_bound)
            g_flags_incl["hessian"].append(_cover(r["g_hess"], gt_))
            g_flags_incl["bootstrap"].append(_cover(r["g_boot"], gt_))
            if not at_bound:
                g_flags["hessian"].append(_cover(r["g_hess"], gt_))
                g_flags["bootstrap"].append(_cover(r["g_boot"], gt_))
                g_diag["hat"].append(r["gamma_hat"]); g_diag["true"].append(gt_)
                g_diag["sig"].append(r["gamma_err"])
                for m, iv in (("hessian", r["g_hess"]), ("bootstrap", r["g_boot"])):
                    if iv is not None:
                        g_diag["sigma_by_method"][m].append((iv[1] - iv[0]) / 2.0)

        clean = lambda lst: [f for f in lst if f is not None]  # noqa: E731
        summary["cells"][str(level)] = {
            "omega0": {m: _cell(clean(o_flags[m])) for m in o_flags},
            "gamma": {m: _cell(clean(g_flags[m])) for m in g_flags},
            "gamma_incl_bound": {m: _cell(clean(g_flags_incl[m])) for m in g_flags},
            "n_at_bound": n_at_bound, "n_converged": len(recs)}
        o_dg, g_dg = _diag(o_diag), _diag(g_diag)
        o_dg["width_ratio"] = _ratio_stats(_level_ratios(recs, "o0_hess", "o0_boot", level))
        g_dg["width_ratio"] = _ratio_stats(
            _level_ratios(recs, "g_hess", "g_boot", level, exclude_at_bound=True))
        summary["diagnostics"][str(level)] = {"omega0": o_dg, "gamma": g_dg}
    return summary


def _summarize_skew(records: list) -> dict | None:
    """The ``gamma_skewed`` block from skew per-scan records (Γ coverage incl/excl at-bound,
    n_at_bound, pull/width diagnostics + width-ratio, and the median bootstrap Γ-interval asymmetry
    (hi−med)/(med−lo))."""
    recs = [r for r in records if r.get("converged")]
    if not recs:
        return None
    g_excl = {m: [] for m in ("hessian", "bootstrap")}
    g_incl = {m: [] for m in ("hessian", "bootstrap")}
    diag = {"hat": [], "true": [], "sig": [], "sigma_by_method": {"hessian": [], "bootstrap": []}}
    asyms, n_at_bound = [], 0
    for r in recs:
        gt_, at = r["gamma_true"], r["gamma_at_bound"]
        n_at_bound += int(at)
        g_incl["hessian"].append(_cover(r["g_hess"], gt_))
        g_incl["bootstrap"].append(_cover(r["g_boot"], gt_))
        if r["g_boot"] is not None and r.get("g_boot_med") is not None:
            lo, hi = r["g_boot"]; med = r["g_boot_med"]
            if (med - lo) > 1e-9:
                asyms.append((hi - med) / (med - lo))
        if not at:
            g_excl["hessian"].append(_cover(r["g_hess"], gt_))
            g_excl["bootstrap"].append(_cover(r["g_boot"], gt_))
            diag["hat"].append(r["gamma_hat"]); diag["true"].append(gt_)
            diag["sig"].append(r["gamma_err"])
            for m, iv in (("hessian", r["g_hess"]), ("bootstrap", r["g_boot"])):
                if iv is not None:
                    diag["sigma_by_method"][m].append((iv[1] - iv[0]) / 2.0)
    clean = lambda lst: [f for f in lst if f is not None]  # noqa: E731
    dg = _diag(diag)
    dg["width_ratio"] = _ratio_stats(
        _level_ratios(recs, "g_hess", "g_boot", _GAMMA_SKEW_LEVEL, exclude_at_bound=True))
    return {"level": _GAMMA_SKEW_LEVEL, "gamma_true": _r(recs[0]["gamma_true"]),
            "N": len(recs), "M": None, "n_converged": len(recs), "n_at_bound": n_at_bound,
            "gamma": {m: _cell(clean(g_excl[m])) for m in g_excl},
            "gamma_incl_bound": {m: _cell(clean(g_incl[m])) for m in g_incl},
            "diagnostics": dg,
            "bootstrap_asymmetry_median": _r(float(np.median(asyms)) if asyms else float("nan"))}


def _load_coverage_dumps(fast: bool) -> tuple[list, list, dict]:
    """Reload per-scan records + meta from the committed dump — no fitting. Powers --replot-from-dump."""
    suffix = ".fast" if fast else ""
    path = FIGDIR / f"coverage_perscan{suffix}.json"
    if not path.exists():
        raise SystemExit(f"--replot-from-dump: no dump at {path} — run the coverage ensemble first")
    d = json.loads(path.read_text(encoding="utf-8"))
    return d.get("scans", []), d.get("gamma_skewed_scans", []), d.get("meta", {})


_SHADE_HDR = "Observed 68%% coverage — N=%d scans/cell, seed %d\n(Γ rows exclude at-bound fits)"


def _shade(cov, se):
    if cov is None or not se:
        return "white"
    dist = (0.68 - cov) / se if se > 0 else 0.0        # under-coverage is the finding → no green
    return "#f2d0d0" if dist > 2 else "#f5e6c8" if dist > 1 else "white"


def _coverage_table_figure(summary: dict) -> Path:
    fig, ax = plt.subplots(figsize=(7.2, 3.6))
    ax.axis("off")
    levels = summary["meta"]["levels"]
    m = summary["meta"]["M"]
    rows = [("ω₀ · Hessian", "omega0", "hessian"), ("ω₀ · Profile", "omega0", "profile"),
            ("ω₀ · Bootstrap (M=%d)" % m, "omega0", "bootstrap"),
            ("Γ · Hessian", "gamma", "hessian"), ("Γ · Bootstrap (M=%d)" % m, "gamma", "bootstrap")]
    cell_text, cell_colors = [], []
    for _lab, param, key in rows:
        rt, rc = [], []
        for level in levels:
            c = summary["cells"][str(level)][param][key]
            cov, se = c["coverage"], c["se"]
            rt.append(f"{cov:.2f} ± {se:.2f}" if cov is not None else "n/a")
            rc.append(_shade(cov, se))
        cell_text.append(rt); cell_colors.append(rc)
    tbl = ax.table(cellText=cell_text, cellColours=cell_colors, rowLabels=[r[0] for r in rows],
                   colLabels=[f"{c} cts" for c in levels], loc="center", cellLoc="center")
    tbl.auto_set_font_size(False); tbl.set_fontsize(9.5); tbl.scale(1.0, 1.55)
    # visual gap between the two row groups: thicken the ω₀/Γ boundary.
    for col in range(-1, len(levels)):
        tbl[(3, col)].visible_edges = "TBLR"
        tbl[(3, col)].set_edgecolor("0.4")
    nb = "; ".join(f"{c}cts n_at_bound={summary['cells'][str(c)]['n_at_bound']}" for c in levels)
    ax.set_title(_SHADE_HDR % (summary["meta"]["N_per_level"], SEED), fontsize=9.0, pad=12)
    ax.text(0.5, -0.08, nb, transform=ax.transAxes, ha="center", fontsize=6.8, color="0.4")
    sk = summary.get("gamma_skewed")
    if sk is not None:
        gh, gb = sk["gamma"]["hessian"], sk["gamma"]["bootstrap"]
        line = (f"Skewed cell — Γ={sk['gamma_true']}, {sk['level']} cts "
                f"(n_at_bound={sk['n_at_bound']}/{sk['n_converged']}, "
                f"boot asym {sk['bootstrap_asymmetry_median']:.2f}):\n"
                f"Γ·Hessian {gh['coverage']:.2f}±{gh['se']:.2f}   "
                f"Γ·Bootstrap {gb['coverage']:.2f}±{gb['se']:.2f}")
        ax.text(0.5, -0.15, line, transform=ax.transAxes, ha="center", va="top",
                fontsize=6.5, color="0.25", linespacing=1.4)
    return _save_png(fig, "coverage_table", "stats/coverage_table", draw=False)


def _coverage_panel(ax, summary, param, styles, *, title):
    levels = summary["meta"]["levels"]
    for j, (key, (mk, col, lab)) in enumerate(styles.items()):
        xs = [level * (1.0 + 0.04 * (j - 1)) for level in levels]
        ys = [summary["cells"][str(level)][param][key]["coverage"] for level in levels]
        es = [summary["cells"][str(level)][param][key]["se"] for level in levels]
        ax.errorbar(xs, ys, yerr=es, fmt=mk + "-", color=col, ecolor="k", capsize=3,
                    ms=6, lw=1.5, label=lab)
    ax.axhline(0.68, ls="--", color="0.4", lw=1.0, label="nominal 0.68")
    se0 = summary["cells"][str(levels[0])][param]["bootstrap"]["se"] or 0.033
    ax.axhspan(0.68 - se0, 0.68 + se0, color="0.85", alpha=0.5,
               label=f"±1 binomial SE (N={summary['meta']['N_per_level']})")
    ax.set_xscale("log"); ax.set_xticks(levels); ax.set_xticklabels([str(c) for c in levels])
    ax.set_ylim(0.4, 0.9)
    ax.set_xlabel("phonon peak counts")
    ax.set_title(title, fontsize=9.5)
    ax.legend(fontsize=7.0, loc="lower left")


def _coverage_curve_figure(summary: dict) -> Path:
    fig, (axo, axg) = plt.subplots(1, 2, figsize=(11.0, 4.6), sharey=True)
    m = summary["meta"]["M"]
    _coverage_panel(axo, summary, "omega0",
                    {"hessian": ("o", "C0", "Hessian"), "profile": ("s", "C1", "Profile"),
                     "bootstrap": ("D", "C2", f"Bootstrap (M={m})")},
                    title="(a) ω₀ coverage")
    axo.set_ylabel("observed 68% coverage")
    # ω₀: HESSE tracks profile (location param). If all rungs dip together at high counts, say what
    # the per-scan diagnostics show it IS — a mild width under-estimation (pull std > 1), NOT a
    # central-value bias (pull mean ≈ 0). Data-driven so the figure never asserts the wrong cause.
    levels = summary["meta"]["levels"]
    hi = summary["cells"][str(levels[-1])]["omega0"]["bootstrap"]
    dg = summary["diagnostics"][str(levels[-1])]["omega0"]
    hi_o, hi_se = hi["coverage"], hi["se"] or 0.0
    pull_std, pull_mean = dg.get("pull_std"), dg.get("pull_mean")
    if hi_o is not None and hi_o < 0.68 - hi_se and pull_std is not None:
        # Attribute the deficit to whichever dominates in THIS dataset: a central shift (|pull mean|)
        # or a width under-estimate (|pull std − 1|). Keeps the fast (noisy) and full figures each
        # internally honest — the full run shows the width story, a small fast sample may not.
        cause = ("a width effect: σ under-states the scatter"
                 if abs(pull_std - 1.0) >= abs(pull_mean) else "a central-value shift")
        axo.annotate(f"all rungs dip together here\n(pull mean ≈ {pull_mean:+.2f}, "
                     f"std ≈ {pull_std:.2f})\n→ {cause}",
                     xy=(levels[-1], hi_o), xytext=(levels[-1] * 0.42, 0.85), ha="center",
                     fontsize=6.3, color="0.3",
                     arrowprops=dict(arrowstyle="-|>", color="0.55", lw=1.0))
    _coverage_panel(axg, summary, "gamma",
                    {"hessian": ("o", "C0", "Hessian"),
                     "bootstrap": ("D", "C2", f"Bootstrap (M={m})")},
                    title="(b) Γ coverage (at-bound fits excluded)")
    # Skewed-Γ cell: same count level, small Γ near the resolution/bound. Drawn as distinct star
    # markers offset just left of the main 15-cts cell, colour-matched to method.
    sk = summary.get("gamma_skewed")
    if sk is not None:
        xs = sk["level"] * 0.80
        gh, gb = sk["gamma"]["hessian"], sk["gamma"]["bootstrap"]
        axg.errorbar([xs * 0.97], [gh["coverage"]], yerr=[gh["se"]], fmt="*", color="C0",
                     ecolor="0.3", ms=13, mec="k", mew=0.5, capsize=3,
                     label=f"Γ={sk['gamma_true']} Hessian (skewed)")
        axg.errorbar([xs * 1.03], [gb["coverage"]], yerr=[gb["se"]], fmt="*", color="C2",
                     ecolor="0.3", ms=13, mec="k", mew=0.5, capsize=3,
                     label=f"Γ={sk['gamma_true']} Bootstrap (skewed)")
        axg.annotate(f"Γ near resolution floor\n(Γ_true={sk['gamma_true']}, {sk['level']} cts;\n"
                     f"boot asym {sk['bootstrap_asymmetry_median']:.2f})",
                     xy=(xs, min(gh["coverage"], gb["coverage"])), xytext=(sk["level"] * 1.3, 0.44),
                     ha="center", fontsize=6.3, color="0.3",
                     arrowprops=dict(arrowstyle="-|>", color="0.55", lw=1.0))
        axg.legend(fontsize=6.3, loc="lower left")
    fig.suptitle("Interval-ladder coverage vs counts — measured on known truth", fontsize=10.5)
    return _save_png(fig, "coverage_curve", "stats/coverage_curve", draw=False)


def _boxplot_ratios(ax, series, labels, fracs, title, color):
    lo, hi = _RATIO_BAND
    data = [np.asarray(s, dtype=float) for s in series]
    pos = list(range(1, len(data) + 1))
    ax.axhspan(lo, hi, color="0.85", alpha=0.7, zorder=0, label=f"agreement band [{lo}, {hi}]")
    ax.axhline(1.0, color="0.4", ls="--", lw=1.0, zorder=1)
    bp = ax.boxplot(data, positions=pos, widths=0.6, showfliers=True, patch_artist=True,
                    flierprops=dict(marker=".", ms=3, mfc="0.5", mec="0.5"),
                    medianprops=dict(color="k", lw=1.4))
    for patch in bp["boxes"]:
        patch.set_facecolor(color); patch.set_alpha(0.35)
    ax.set_yscale("log")
    ax.set_yticks([0.5, 0.8, 1.0, 1.25, 2.0])
    ax.set_yticklabels(["0.5", "0.8", "1.0", "1.25", "2.0"])
    ax.yaxis.set_minor_formatter(plt.NullFormatter())
    ax.set_ylim(0.35, 3.0)
    ax.set_xticks(pos); ax.set_xticklabels(labels)
    for p, s, fr in zip(pos, data, fracs):
        txt = (f"{fr:.0%} out\nn={s.size}" if fr is not None else f"n={s.size}")
        ax.text(p, 2.75, txt, ha="center", va="top", fontsize=6.2, color="0.25")
    ax.set_title(title, fontsize=9.5)


def _coverage_scatter_figure(records_main, records_skew, summary) -> Path:
    """Per-scan HESSE/bootstrap interval-WIDTH ratio by count level — the single-scan view. Even where
    ENSEMBLE coverage matches, an individual scan's HESSE bar can be far from its bootstrap bar; the
    fraction outside the [0.8, 1.25] agreement band is what the per-scan hesse_trusted flag guards."""
    levels = summary["meta"]["levels"]
    fig, (axo, axg) = plt.subplots(1, 2, figsize=(11.0, 4.6), sharey=True)

    o_series = [_level_ratios(records_main, "o0_hess", "o0_boot", L) for L in levels]
    o_frac = [summary["diagnostics"][str(L)]["omega0"]["width_ratio"].get("frac_outside")
              for L in levels]
    _boxplot_ratios(axo, o_series, [str(L) for L in levels], o_frac,
                    "(a) ω₀   width(HESSE) / width(bootstrap)", "C0")
    axo.set_ylabel("per-scan HESSE / bootstrap width")
    axo.set_xlabel("phonon peak counts")
    axo.legend(fontsize=6.5, loc="upper left")

    g_series = [_level_ratios(records_main, "g_hess", "g_boot", L, exclude_at_bound=True)
                for L in levels]
    g_frac = [summary["diagnostics"][str(L)]["gamma"]["width_ratio"].get("frac_outside")
              for L in levels]
    labels = [str(L) for L in levels]
    sk = summary.get("gamma_skewed")
    if records_skew and sk is not None:
        g_series.append(_level_ratios(records_skew, "g_hess", "g_boot", sk["level"],
                                      exclude_at_bound=True))
        g_frac.append(sk["diagnostics"].get("width_ratio", {}).get("frac_outside"))
        labels.append(f"Γ={sk['gamma_true']}\n@{sk['level']}")
    _boxplot_ratios(axg, g_series, labels, g_frac,
                    "(b) Γ   width(HESSE) / width(bootstrap)", "C2")
    axg.set_xlabel("phonon peak counts (+ skewed cell)")
    fig.suptitle("Per-scan interval-width ratio — one scan is not the ensemble", fontsize=10.5)
    return _save_png(fig, "coverage_scatter", "stats/coverage_scatter", draw=False)


def figure_coverage(workers: int, fast: bool, *, from_dump: bool = False) -> list[Path]:
    """Live: fit the ensembles, dump records, build all three figures. ``from_dump``: reload the
    per-scan records and rebuild figures + summary WITHOUT re-fitting (fast figure iteration)."""
    if from_dump:
        rec_main, rec_skew, meta = _load_coverage_dumps(fast)
    else:
        rec_main, meta = _coverage_fit(workers, fast)
        rec_skew = _gamma_skew_fit(workers, fast)
    summary = _summarize(rec_main, meta)
    skew = _summarize_skew(rec_skew)
    if skew is not None:
        skew["M"] = meta.get("M")
        summary["gamma_skewed"] = skew
    suffix = ".fast" if fast else ""
    _dump_json("coverage_summary" + suffix, summary)
    if not from_dump:                                   # never clobber the dump we replot FROM
        _dump_json("coverage_perscan" + suffix,
                   {"meta": meta, "scans": _dump_main(rec_main),
                    "gamma_skewed_scans": _dump_skew(rec_skew)})
    return [_coverage_table_figure(summary), _coverage_curve_figure(summary),
            _coverage_scatter_figure(rec_main, rec_skew, summary)]


# =========================================================================== workspace figs
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


def _refit_bootstrap(ctx, *, M=200, seed=SEED):
    """Unconstrained standalone re-fit → best converged structure → bootstrap ω₀ samples."""
    from isar.analyze.phonon import enumerate_phonon_structures as enumerate_structures
    from isar.analyze import loop, errors
    hyps = enumerate_structures(ctx)
    pairs = [(loop.fit_hypothesis(h, ctx), h) for h in hyps]
    conv = [(r, h) for r, h in pairs if r.optimizer_converged]
    if not conv:
        raise RuntimeError("no structure converged on standalone re-fit")
    best_r, best_h = min(conv, key=lambda rh: rh[0].cost + 2.0 * rh[0].k)
    errors.attach(best_r, best_h, ctx, M=M, seed=seed)
    samples = np.asarray(best_r.within_model["omega0_samples"], dtype=float)
    return best_r, samples


def _hist_width(samples: np.ndarray, level: float = 0.68) -> tuple:
    s = samples[np.isfinite(samples)]
    if s.size == 0:
        return float("nan"), float("nan"), float("nan")
    lo_q, hi_q = 100 * (1 - level) / 2, 100 * (1 + level) / 2
    lo, hi = float(np.percentile(s, lo_q)), float(np.percentile(s, hi_q))
    return lo, hi, hi - lo


def figure_wall(ws: Path) -> Path:
    card = _load_card(ws)
    rows = _load_results(ws)

    # (b) healthy: the first candidate with a genuine interior Γ minimum (sane, non-collapsed width).
    healthy_scan, res_h, samp_h = None, None, None
    for cand in _WALL_HEALTHY_CANDIDATES:
        ctx_h, _ = _workspace_ctx(ws, card, cand, rows[cand])
        r, s = _refit_bootstrap(ctx_h, M=200, seed=SEED)
        _lo, _hi, w = _hist_width(s)
        if np.isfinite(w) and 0.01 < w < 1.0:
            healthy_scan, res_h, samp_h = cand, r, s
            lo_h, hi_h, w_h = _lo, _hi, w
            break
    if healthy_scan is None:
        raise AssertionError("no candidate healthy scan gave a sane (0.01–1.0 meV) standalone width")

    # (c) degenerate: the first reliable=False candidate whose standalone bootstrap collapses.
    degen_scan, res_d, samp_d = None, None, None
    for cand in _WALL_DEGEN_CANDIDATES:
        ctx_d, _ = _workspace_ctx(ws, card, cand, rows[cand])
        r, s = _refit_bootstrap(ctx_d, M=200, seed=SEED)
        _lo, _hi, w = _hist_width(s)
        if np.isfinite(w) and w < 0.01:
            degen_scan, res_d, samp_d = cand, r, s
            lo_d, hi_d, w_d = _lo, _hi, w
            break
    if degen_scan is None:
        raise AssertionError("no candidate degenerate scan collapsed standalone (width < 0.01)")

    fig, (axa, axb, axc) = plt.subplots(1, 3, figsize=(12.5, 4.2))

    # (a) schematic.
    g = np.linspace(0.0, 3.0, 200)
    axa.plot(g, 0.5 * (g - 1.4) ** 2 + 0.4, "--", color="0.5", lw=1.6,
             label="interior minimum → curvature\ndefined, HESSE meaningful")
    gm = np.linspace(0.0, 3.0, 200)
    axa.plot(gm, 1.6 + 1.3 * np.exp(-1.1 * gm), "-", color="C3", lw=2.0,
             label="minimum pinned at the wall →\nno parabola; bootstrap piles on")
    axa.axvspan(-0.3, 0.0, color="0.7", hatch="///", alpha=0.4)
    axa.axvline(0.0, color="k", lw=1.2)
    axa.set_xlim(-0.3, 3.0)
    axa.set_xticks([0.0])
    axa.set_xticklabels(["Γ_min"])
    axa.set_yticks([])
    axa.set_xlabel("Γ from bound")
    axa.set_ylabel("−2 ln L")
    axa.set_title("(a) the Γ wall", fontsize=9.5)
    axa.legend(fontsize=6.3, loc="upper right")

    # (b) healthy histogram.
    axb.hist(samp_h[np.isfinite(samp_h)], bins=25, color="C0", alpha=0.7)
    axb.axvspan(lo_h, hi_h, color="C0", alpha=0.18)
    for v in (lo_h, hi_h):
        axb.axvline(v, color="C0", lw=1.2)
    axb.set_title(f"(b) scan {healthy_scan} — interior minimum", fontsize=9.5)
    axb.set_xlabel("bootstrap ω₀ (meV)")
    axb.set_ylabel("draws")
    axb.annotate(f"68%: [{lo_h:.3f}, {hi_h:.3f}]\ninterval_reliable=True",
                 xy=(0.5, 0.86), xycoords="axes fraction", ha="center", fontsize=7.5, color="C0")
    _xb0, _xb1 = axb.get_xlim()
    span_b = float(_xb1 - _xb0)                # match the WIDTH of (b), centered on (c)'s own data

    # (c) degenerate collapse, x-span width forced equal to (b) so the spike reads as a spike.
    axc.hist(samp_d[np.isfinite(samp_d)], bins=25, color="C3", alpha=0.7)
    center_d = float(np.median(samp_d[np.isfinite(samp_d)]))
    axc.set_xlim(center_d - span_b / 2, center_d + span_b / 2)
    axc.set_title(f"(c) scan {degen_scan} — Γ at bound", fontsize=9.5)
    axc.set_xlabel("bootstrap ω₀ (meV)")
    axc.annotate(f"[{lo_d:.4f}, {hi_d:.4f}]\nwidth {w_d:.4f} meV\ninterval_reliable=False",
                 xy=(center_d, 0.0), xytext=(0.5, 0.7), textcoords="axes fraction",
                 ha="center", fontsize=7.5, color="C3",
                 arrowprops=dict(arrowstyle="-|>", color="C3", lw=1.0))
    axc.text(0.5, 0.4, "a naive reader would\nquote σ ≈ %.4f meV" % (w_d / 2.0),
             transform=axc.transAxes, ha="center", fontsize=6.8, color="0.4")

    _dump_json("wall", {
        "healthy": {"scan": healthy_scan, "omega0": _r(res_h.omega0),
                    "interval_lo": _r(lo_h), "interval_hi": _r(hi_h), "width": _r(w_h),
                    "gamma": _r(res_h.gamma), "cost_per_dof": _r(res_h.cost_per_dof),
                    "n_samples": int(np.isfinite(samp_h).sum())},
        "degenerate": {"scan": degen_scan, "omega0": _r(res_d.omega0),
                       "interval_lo": _r(lo_d, 6), "interval_hi": _r(hi_d, 6), "width": _r(w_d, 6),
                       "gamma": _r(res_d.gamma), "cost_per_dof": _r(res_d.cost_per_dof),
                       "n_samples": int(np.isfinite(samp_d).sum())},
        "M": 200, "seed": SEED})
    print(f"  wall: healthy scan {healthy_scan} width {w_h:.4f} meV; "
          f"degenerate scan {degen_scan} width {w_d:.4f} meV")
    return _save_png(fig, "wall_figure", "stats/wall", draw=False)


def figure_akaike(ws: Path) -> Path:
    from isar.analyze import loop, select
    card = _load_card(ws)
    rows = _load_results(ws)
    ctx, _ = _workspace_ctx(ws, card, _AKAIKE_SCAN, rows[_AKAIKE_SCAN])

    # Force the two-stage enumeration to also fit the 2-mode set (archived best_model=stokes2+bg).
    triples = loop._enumerate_and_fit(ctx, peaks=[(0.0,), (0.0,)])
    by_label = {r.label: (r, h) for r, h, _ in triples}
    if "stokes2+bg" not in by_label:            # gate/IC fallback: fit the 2-mode set explicitly
        from isar.analyze.phonon import enumerate_phonon_multimode as enumerate_multimode
        from isar.analyze.phonon import greedy_multimode_ic
        best_one = loop._best_converged([r for r, _, _ in triples])
        for h in enumerate_multimode(ctx):
            x0 = greedy_multimode_ic(h.build(), ctx, best_one, peaks=[(0.0,), (0.0,)])
            r = loop.fit_hypothesis(h, ctx, x0=x0)
            by_label[r.label] = (r, h)
    r1, h1 = by_label["stokes+bg"]
    r2, h2 = by_label["stokes2+bg"]
    scores = {s.label: s for s in select.score_models([r1, r2])}

    x, counts = ctx.x_used, ctx.counts_used
    err = np.sqrt(np.maximum(counts, 1.0))
    xf = np.linspace(float(x.min()), float(x.max()), 500)

    def theta(res, hyp):
        comp = hyp.build()
        return comp, np.array([res.params[n] for n in comp.param_names()], dtype=float)

    c1, t1 = theta(r1, h1)
    c2, t2 = theta(r2, h2)

    fig, (ax0, ax1) = plt.subplots(2, 1, sharex=True, height_ratios=[3, 1.2], figsize=(7.5, 6.2))
    ax0.errorbar(x, counts, yerr=err, fmt="o", ms=4, color="k", zorder=5, label="data ±√N")
    ax0.plot(xf, c1.evaluate(xf, t1, ctx), "--", color="C0", lw=1.8,
             label=f"stokes+bg (w={scores['stokes+bg'].weight:.3f})")
    ax0.plot(xf, c2.evaluate(xf, t2, ctx), "-", color="C1", lw=2.0,
             label=f"stokes2+bg (w={scores['stokes2+bg'].weight:.3f})")
    for lab, vals in c2.evaluate_components(xf, t2, ctx):
        ax0.plot(xf, vals, ":", lw=1.0, alpha=0.8, color="0.5")
    # The two-mode fit's mode farther from the one-mode fit's: the extra mode the selection weighs.
    sec = max((m["omega0"] for m in (r2.modes or [])), key=lambda w: abs(w - r1.omega0),
              default=None)
    if sec:
        ax0.annotate(f"second mode {float(sec):.2f} meV", xy=(float(sec), float(counts.max()) * 0.5),
                     xytext=(float(sec) + 1.5, float(counts.max()) * 0.8), fontsize=7.5, color="C1",
                     arrowprops=dict(arrowstyle="-|>", color="C1", lw=1.0))
    tbl = ["model        k   −2lnL     AIC   ΔAIC  weight"]
    for lab in ("stokes+bg", "stokes2+bg"):
        s = scores[lab]
        tbl.append(f"{lab:<12s} {s.k:>2d} {s.cost:>8.1f} {s.aic:>7.1f} {s.delta_aic:>5.1f} "
                   f"{s.weight:>6.3f}")
    ax0.text(0.02, 0.97, "\n".join(tbl), transform=ax0.transAxes, va="top", ha="left",
             fontsize=6.8, family="monospace",
             bbox=dict(boxstyle="round", fc="white", ec="0.7", alpha=0.9))
    ax0.set_ylabel("counts")
    ax0.set_title(f"scan {_AKAIKE_SCAN}: does the second mode earn its parameters?", fontsize=9.5)
    ax0.legend(fontsize=7.5, loc="upper right")

    ax1.axhline(0, color="0.6", lw=0.8)
    for s in (1.0, -1.0):
        ax1.axhline(s, color="0.85", lw=0.6)
    ax1.plot(x, (counts - c1.evaluate(x, t1, ctx)) / err, "s-", ms=3, color="C0",
             label="stokes+bg")
    ax1.plot(x, (counts - c2.evaluate(x, t2, ctx)) / err, "o-", ms=3, color="C1",
             label="stokes2+bg")
    ax1.set_ylabel("pull")
    ax1.set_xlabel("energy transfer (meV)")
    ax1.legend(fontsize=7, loc="upper right")

    _dump_json("akaike_example", {
        "scan": _AKAIKE_SCAN,
        "models": {lab: {"k": scores[lab].k, "cost": _r(scores[lab].cost, 3),
                         "aic": _r(scores[lab].aic, 3), "delta_aic": _r(scores[lab].delta_aic, 3),
                         "weight": _r(scores[lab].weight, 5)}
                   for lab in ("stokes+bg", "stokes2+bg")},
        "second_mode_omega0": _r(float(sec)) if sec else None, "seed": SEED})
    return _save_png(fig, "akaike_example", "stats/akaike_example", draw=False)


def figure_branch(ws: Path) -> Path:
    """Render the archived branch-assignment posterior (weights from session.json, entropy from
    results.csv). Archived numbers only — the caption says so."""
    session = json.loads((ws / "session.json").read_text(encoding="utf-8"))
    rows = _load_results(ws)
    by_num = {}
    for ent in session["scans"].values():
        n = ent.get("scan_number")
        if n in (_BRANCH_CERTAIN, _BRANCH_AMBIGUOUS):
            det = next(iter(ent["detectors"].values()))
            by_num[n] = {"branch_id": det.get("branch_id"),
                         "branch_weight": float(det.get("branch_weight")),
                         "entropy": float(rows[n]["assignment_entropy"])}

    fig, axes = plt.subplots(1, 2, figsize=(9.0, 4.2), sharey=True)
    panels = [(_BRANCH_CERTAIN, "certain"), (_BRANCH_AMBIGUOUS, "ambiguous")]
    dump = {}
    for ax, (num, tag) in zip(axes, panels):
        info = by_num[num]
        assigned = info["branch_id"] or "acoustic"
        other = "optic" if assigned == "acoustic" else "acoustic"
        w = info["branch_weight"]
        names = [assigned, other]
        weights = [w, 1.0 - w]
        colors = ["C0", "0.6"]
        ax.bar(names, weights, color=colors)
        for xi, wi in enumerate(weights):
            ax.text(xi, wi + 0.02, f"{wi:.3f}", ha="center", fontsize=8)
        ax.axhline(0.5, ls=":", color="0.5", lw=1.0)
        ax.set_ylim(0, 1.05)
        ax.set_title(f"scan {num} — H = {info['entropy']:.3f} ({tag})", fontsize=9.5)
        ax.set_ylabel("branch posterior weight")
        dump[str(num)] = {"assigned": assigned, "weight": _r(w, 5),
                          "entropy": _r(info["entropy"], 4)}
    axes[1].annotate("H_max = ln 2 ≈ 0.693\n(a coin flip)", xy=(0.5, 0.92),
                     xycoords="axes fraction", ha="center", fontsize=7.5, color="0.35")
    _dump_json("branch_posterior", {"scans": dump, "h_max": _r(np.log(2), 4), "seed": SEED})
    return _save_png(fig, "branch_posterior", "stats/branch_posterior", draw=False)


# --------------------------------------------------------------------------- infra
def _load_card(ws: Path):
    from isar.schema.card import Card
    return Card.load(ws / "card.policy.toml", ws / "card.state.json")


def _map_tasks(worker, tasks, workers: int):
    """Deterministic (submission-order) map over a process pool, or serial when workers<=1.
    Order-preserving so the aggregate is worker-count-invariant."""
    if workers <= 1:
        for t in tasks:
            yield worker(t)
    else:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            for res in ex.map(worker, tasks):
                yield res


def _require_ws(ws: str | None) -> Path:
    if not ws:
        raise SystemExit("this figure needs --workspace (or env ISAR_REF_WORKSPACE): the archived "
                         "reference workspace mvp_zone_test16")
    p = Path(ws)
    if not (p / "report" / "results.csv").exists():
        raise SystemExit(f"workspace {p} has no report/results.csv")
    return p


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Generate the STATS_PRIMER figure gallery.")
    ap.add_argument("--workspace", default=os.environ.get("ISAR_REF_WORKSPACE"),
                    help="archived reference workspace (default env ISAR_REF_WORKSPACE); "
                         "required only for wall/akaike/branch")
    ap.add_argument("--only", action="append",
                    choices=["three_questions", "cost_bias", "coverage", "wall", "akaike", "branch"],
                    help="generate only these figures (repeatable); default: all")
    ap.add_argument("--fast", action="store_true",
                    help="coverage: N=40/level, M=50 → coverage_summary.fast.json")
    ap.add_argument("--workers", type=int, default=1, help="process-pool size for the ensembles")
    ap.add_argument("--replot-from-dump", action="store_true",
                    help="coverage: rebuild figures + summary from the committed "
                         "coverage_perscan[.fast].json WITHOUT re-fitting (fast figure iteration)")
    ap.add_argument("--outdir", type=Path, default=_DEFAULT_FIGDIR,
                    help="write every figure, JSON dump and caption sidecar here "
                         f"(default: {_DEFAULT_FIGDIR}); also where --replot-from-dump reads from")
    args = ap.parse_args(argv)

    global FIGDIR
    FIGDIR = Path(args.outdir).resolve()

    which = args.only or ["three_questions", "cost_bias", "coverage", "wall", "akaike", "branch"]
    _ensure_figdir()

    if "three_questions" in which:
        print(f"  three_questions.svg -> {figure_three_questions()}")
    if "cost_bias" in which:
        print(f"  cost_bias.png -> {figure_cost_bias(args.workers)}")
    if "coverage" in which:
        for p in figure_coverage(args.workers, args.fast, from_dump=args.replot_from_dump):
            print(f"  {p.name} -> {p}")
    if any(w in which for w in ("wall", "akaike", "branch")):
        ws = _require_ws(args.workspace)
        if "wall" in which:
            print(f"  wall_figure.png -> {figure_wall(ws)}")
        if "akaike" in which:
            print(f"  akaike_example.png -> {figure_akaike(ws)}")
        if "branch" in which:
            print(f"  branch_posterior.png -> {figure_branch(ws)}")


if __name__ == "__main__":
    main()

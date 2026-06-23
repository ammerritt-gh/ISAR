"""Monte-Carlo validation gate: generate → fit → score recovery against known truth.

For each (H, target) data-cell, draw M Poisson realizations (seeded), fit each under the
requested estimators (paired on identical draws), and accumulate bias / pull / success.
Baker–Cousins is the gated production estimator; Neyman/Pearson surfaces are produced too.

Censoring is handled honestly (the easy place to get a validation gate subtly wrong):
  - failed fits are excluded but `success_rate` is always reported and gated on;
  - Γ-PULL uses only successful non-`gamma_at_floor` fits (floored σ_Γ is invalid);
  - Γ-BIAS uses ALL successful fits incl. floored (excluding would truncate the low tail
    and manufacture a fake positive bias) — `gamma_floored_fraction` is reported alongside.
"""
from __future__ import annotations

import argparse
import csv
import dataclasses
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import TwoSlopeNorm

from . import config, dispersion, fit, generate, noise

GATED_ESTIMATOR = "baker_cousins"


# ============================================================================
# Cell enumeration & seeding (canonical, scheduling-independent)
# ============================================================================
def measurable_datacells(cfg: config.RunConfig) -> list[tuple[float, float]]:
    """Canonical (sorted) list of measurable (H, target) data-cells (endpoints dropped)."""
    cells = []
    for H in cfg.H_values:
        if dispersion.classify_cell(H, cfg.gamma, cfg.eps_omega_d) == "unmeasurable":
            continue
        for t in cfg.target_counts_levels:
            cells.append((float(H), float(t)))
    return sorted(cells)


def datacell_ordinal_map(cfg: config.RunConfig) -> dict[tuple[float, float], int]:
    return {cell: i for i, cell in enumerate(measurable_datacells(cfg))}


def realization_seed(base_seed: int, ordinal: int, M: int, i: int) -> int:
    """Seed depends only on (data-cell ordinal, realization i) — NOT estimator/backend."""
    return int(base_seed + ordinal * M + i)


# ============================================================================
# Result containers
# ============================================================================
@dataclass
class DataCellResult:
    H: float
    target: float
    scores: dict           # estimator -> CellScore
    samples: dict | None = None  # estimator -> {field: ndarray} (per-realization, optional)


@dataclass
class ScoreRun:
    cfg: config.RunConfig
    scores: list           # flat list[CellScore]
    results: list | None = None  # list[DataCellResult] (carries per-realization samples)

    def for_estimator(self, estimator: str) -> list:
        return [c for c in self.scores if c.estimator == estimator]


@dataclass
class CellReport:
    """Per-cell documentation (no pass/fail) — reliability + calibration."""
    H: float
    target: float
    classification: str
    well_counted: object         # bool | None
    reliable_rate: float
    success_rate: float
    failure_rate: float          # 1 − reliable_rate (non-convergence + untrustworthy)
    gamma_floored_fraction: float
    omega0_bias_mean: float
    gamma_bias_mean: float
    omega0_pull_mean: float
    omega0_pull_std: float
    gamma_pull_mean: float
    gamma_pull_std: float


@dataclass
class RunReport:
    estimator: str
    cells: list = field(default_factory=list)
    threshold: dict = field(default_factory=dict)


# ============================================================================
# Per-cell statistics (the censoring core)
# ============================================================================
def _std(a: np.ndarray) -> float:
    return float(np.std(a, ddof=1)) if a.size > 1 else float("nan")


# A converged fit is 'untrustworthy' if its reported σ is below this fraction of the
# cell's typical σ — the degenerate-Γ→0 corner where HESSE collapses σ catastrophically.
# Validated empirically: this cleanly separates the pathologies (σ ~100× too small) from
# legitimate fits (σ clustered near the median); covariance condition number does not.
TRUST_SIGMA_FACTOR = 0.2


def _is_trustworthy(r, med_so, med_sg) -> bool:
    ok_o = r.omega0_err >= TRUST_SIGMA_FACTOR * med_so
    ok_g = r.gamma_at_floor or (r.gamma_err >= TRUST_SIGMA_FACTOR * med_sg)
    return bool(ok_o and ok_g)


def summarize_cell(H, target, estimator, classification, fit_results,
                   gt_omega0, gt_gamma) -> config.CellScore:
    """Reduce M FitResults for one cell to a CellScore.

    Failures are documented, not hidden: non-converged fits and 'untrustworthy' fits
    (σ a gross low outlier → the degenerate Γ→0 corner) both count toward (1 −
    reliable_rate). Recovery/calibration stats are over the reliable set. Within that:
    Γ-pull excludes gamma_at_floor (invalid σ_Γ); Γ-bias keeps them (excluding truncates
    the low tail).
    """
    n_total = len(fit_results)
    converged = [r for r in fit_results if r.success]
    n_success = len(converged)
    cs = config.CellScore(
        H=float(H), target_peak_counts=float(target), estimator=estimator,
        classification=classification, n_total=n_total, n_success=n_success,
        success_rate=(n_success / n_total if n_total else float("nan")),
    )
    if n_success == 0:
        cs.reliable_rate = 0.0 if n_total else float("nan")
        return cs

    # Typical σ scale from the non-floored converged fits (floored σ_Γ is legitimately small).
    ref = [r for r in converged if not r.gamma_at_floor] or converged
    med_so = float(np.median([r.omega0_err for r in ref]))
    med_sg = float(np.median([r.gamma_err for r in ref]))

    reliable = [r for r in converged if _is_trustworthy(r, med_so, med_sg)]
    cs.n_reliable = len(reliable)
    cs.n_untrustworthy = n_success - len(reliable)
    cs.reliable_rate = len(reliable) / n_total
    if not reliable:
        return cs

    o = np.array([r.omega0 for r in reliable], dtype=float)
    oe = np.array([r.omega0_err for r in reliable], dtype=float)
    g_all = np.array([r.gamma for r in reliable], dtype=float)
    n_r = len(reliable)

    cs.median_omega0_err = float(np.median(oe))
    cs.omega0_bias_mean = float(np.mean(o) - gt_omega0)
    cs.omega0_bias_std = _std(o)
    opull = (o - gt_omega0) / oe
    cs.omega0_pull_mean = float(np.mean(opull))
    cs.omega0_pull_std = _std(opull)
    cs.omega0_recovered_mean = float(np.mean(o))
    cs.omega0_recovered_sem = cs.omega0_bias_std / np.sqrt(n_r) if n_r > 1 else float("nan")

    # Γ-bias over ALL reliable fits (including floored).
    cs.gamma_bias_mean = float(np.mean(g_all) - gt_gamma)
    cs.gamma_bias_std = _std(g_all)
    cs.gamma_recovered_mean = float(np.mean(g_all))
    cs.gamma_recovered_sem = cs.gamma_bias_std / np.sqrt(n_r) if n_r > 1 else float("nan")

    # Γ-pull over reliable AND NOT gamma_at_floor (floored σ_Γ is invalid).
    nf = [r for r in reliable if not r.gamma_at_floor]
    cs.n_gamma_used = len(nf)
    cs.gamma_floored_fraction = float((n_r - len(nf)) / n_r)
    if nf:
        gg = np.array([r.gamma for r in nf], dtype=float)
        ge = np.array([r.gamma_err for r in nf], dtype=float)
        gpull = (gg - gt_gamma) / ge
        cs.gamma_pull_mean = float(np.mean(gpull))
        cs.gamma_pull_std = _std(gpull)
    return cs


# ============================================================================
# Scoring one data-cell (all estimators paired on shared draws)
# ============================================================================
def _fit_cell(cfg, H, target, ordinal, estimators, *, backend="iminuit", minos=False,
              keep_samples=False) -> DataCellResult:
    classification = dispersion.classify_cell(H, cfg.gamma, cfg.eps_omega_d)
    gt0 = generate.make_ground_truth(cfg, H, target, seed=0)  # truth values (seed-independent)
    results = {e: [] for e in estimators}
    for i in range(cfg.M):
        seed = realization_seed(cfg.base_seed, ordinal, cfg.M, i)
        gt = generate.make_ground_truth(cfg, H, target, seed=seed)
        counts = noise.draw_counts(gt)  # one draw, shared across estimators (paired)
        for e in estimators:
            results[e].append(fit.fit_truth(gt, counts, estimator=e, backend=backend, minos=minos))

    scores = {e: summarize_cell(H, target, e, classification, results[e], gt0.omega0, gt0.gamma)
              for e in estimators}
    samples = None
    if keep_samples:
        samples = {e: {
            "omega0": np.array([r.omega0 for r in results[e]]),
            "omega0_err": np.array([r.omega0_err for r in results[e]]),
            "gamma": np.array([r.gamma for r in results[e]]),
            "gamma_err": np.array([r.gamma_err for r in results[e]]),
            "success": np.array([r.success for r in results[e]]),
            "gamma_at_floor": np.array([r.gamma_at_floor for r in results[e]]),
            "phonon_amplitude": np.array([r.phonon_amplitude for r in results[e]]),
            "phonon_amplitude_err": np.array([r.phonon_amplitude_err for r in results[e]]),
        } for e in estimators}
    return DataCellResult(float(H), float(target), scores, samples)


def _score_one_datacell(args) -> DataCellResult:
    """Top-level, picklable worker for ProcessPoolExecutor (Windows spawn)."""
    cfg, H, target, ordinal, estimators, backend, minos, keep_samples = args
    return _fit_cell(cfg, H, target, ordinal, estimators,
                     backend=backend, minos=minos, keep_samples=keep_samples)


def score_cell(cfg, H, target, *, estimator=GATED_ESTIMATOR, datacell_ordinal=None,
               minos=False, backend="iminuit") -> config.CellScore:
    """Score one (H, target) cell under a single estimator (used for tests/diagnostics)."""
    if datacell_ordinal is None:
        datacell_ordinal = datacell_ordinal_map(cfg)[(float(H), float(target))]
    dcr = _fit_cell(cfg, H, target, datacell_ordinal, (estimator,),
                    backend=backend, minos=minos)
    return dcr.scores[estimator]


def run_scorer(cfg, *, n_workers=None, estimators=None, backend="iminuit",
               minos=False, keep_samples=True, write_csv=True, make_figures=True) -> ScoreRun:
    """Run the full (H, target) surface; parallel over data-cells (or serial if n_workers=1)."""
    estimators = tuple(estimators) if estimators is not None else tuple(cfg.estimators)
    omap = datacell_ordinal_map(cfg)
    cells = measurable_datacells(cfg)
    tasks = [(cfg, H, t, omap[(H, t)], estimators, backend, minos, keep_samples)
             for (H, t) in cells]

    if n_workers is None:
        n_workers = max(1, (os.cpu_count() or 2) - 2)

    if n_workers == 1:
        results = [_score_one_datacell(t) for t in tasks]
    else:
        with ProcessPoolExecutor(max_workers=n_workers) as ex:
            results = list(ex.map(_score_one_datacell, tasks))

    results.sort(key=lambda d: (d.H, d.target))
    scores = [dcr.scores[e] for dcr in results for e in estimators]
    run = ScoreRun(cfg=cfg, scores=scores, results=results)

    if write_csv:
        config.ensure_out_dirs()
        write_summary(run, config.SUMMARY_CSV)
    if make_figures:
        config.ensure_out_dirs()
        for e in estimators:
            plot_surfaces(run, cfg, estimator=e,
                          out_path=config.FIGURE_DIR / f"score_surfaces_{e}.png")
        plot_capstone(run, cfg, out_path=config.FIGURE_DIR / "score_capstone_dispersion.png")
        plot_pull_histograms(run, cfg, out_path=config.FIGURE_DIR / "score_pull_histograms.png")
        plot_threshold(run, cfg, out_path=config.FIGURE_DIR / "score_threshold.png")
    return run


# ============================================================================
# Documentation (no pass/fail): reliability, calibration, and the data threshold
# ============================================================================
def data_threshold(run: ScoreRun, cfg, *, estimator=GATED_ESTIMATOR,
                   reliable_floor=0.99, pull_width_max=1.10) -> dict:
    """Find the minimum counts for reliable mid-zone fits.

    Aggregates over MIDZONE cells per count level and returns the smallest target where
    every mid-zone cell is reliable (reliable_rate ≥ floor) with calibrated pulls
    (ω₀ and Γ pull-width ≤ max). The deliverable ISAR triage needs: 'reliable above N cts'.
    """
    mid = [c for c in run.scores
           if c.estimator == estimator and c.classification == "midzone"]
    by_count = {}
    for t in sorted({c.target_peak_counts for c in mid}):
        cells = [c for c in mid if c.target_peak_counts == t]
        rr = [c.reliable_rate for c in cells if np.isfinite(c.reliable_rate)]
        gw = [c.gamma_pull_std for c in cells if np.isfinite(c.gamma_pull_std)]
        ow = [c.omega0_pull_std for c in cells if np.isfinite(c.omega0_pull_std)]
        by_count[t] = {
            "n_cells": len(cells),
            "min_reliable_rate": min(rr) if rr else float("nan"),
            "mean_reliable_rate": float(np.mean(rr)) if rr else float("nan"),
            "mean_gamma_pull_width": float(np.mean(gw)) if gw else float("nan"),
            "max_gamma_pull_width": max(gw) if gw else float("nan"),
            "mean_omega0_pull_width": float(np.mean(ow)) if ow else float("nan"),
            "max_omega0_pull_width": max(ow) if ow else float("nan"),
            # 'reliable' gates on the FAILURE RATE + the MEAN calibration (robust to
            # single-cell pull-width sampling noise, which is ±1/√(2M) per cell).
            "ok": bool(rr and min(rr) >= reliable_floor
                       and gw and float(np.mean(gw)) <= pull_width_max
                       and ow and float(np.mean(ow)) <= pull_width_max),
        }
    reliable_counts = [t for t, d in by_count.items() if d["ok"]]
    return {
        "by_count": by_count,
        "min_reliable_counts": min(reliable_counts) if reliable_counts else None,
        "reliable_floor": reliable_floor,
        "pull_width_max": pull_width_max,
    }


def characterize_run(run: ScoreRun, cfg, *, estimator=GATED_ESTIMATOR,
                     well_counted_sigma=0.1) -> RunReport:
    """Document per-cell reliability + calibration (no pass/fail) and the data threshold."""
    cells = []
    for cs in run.scores:
        if cs.estimator != estimator:
            continue
        wc = (bool(cs.median_omega0_err < well_counted_sigma)
              if np.isfinite(cs.median_omega0_err) else None)
        fr = 1.0 - cs.reliable_rate if np.isfinite(cs.reliable_rate) else float("nan")
        cells.append(CellReport(
            cs.H, cs.target_peak_counts, cs.classification, wc,
            cs.reliable_rate, cs.success_rate, fr, cs.gamma_floored_fraction,
            cs.omega0_bias_mean, cs.gamma_bias_mean,
            cs.omega0_pull_mean, cs.omega0_pull_std,
            cs.gamma_pull_mean, cs.gamma_pull_std))
    return RunReport(estimator=estimator, cells=cells,
                     threshold=data_threshold(run, cfg, estimator=estimator))


# ============================================================================
# CSV I/O
# ============================================================================
_INT_FIELDS = {"n_total", "n_success", "n_gamma_used"}
_STR_FIELDS = {"estimator", "classification"}


def write_summary(run: ScoreRun, path) -> Path:
    path = Path(path)
    fields = [f.name for f in dataclasses.fields(config.CellScore)]
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for cs in run.scores:
            w.writerow(cs.to_dict())
    return path


def _cast(name: str, value: str):
    if name in _STR_FIELDS:
        return value
    if name in _INT_FIELDS:
        return int(value)
    return float(value)


def read_summary(path) -> list:
    out = []
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            out.append(config.CellScore(**{k: _cast(k, v) for k, v in row.items()}))
    return out


# ============================================================================
# Figures
# ============================================================================
def _surface_matrix(run, estimator, metric, Hs, targets):
    lut = {(c.H, c.target_peak_counts): c for c in run.scores if c.estimator == estimator}
    M = np.full((len(targets), len(Hs)), np.nan)
    for j, H in enumerate(Hs):
        for k, t in enumerate(targets):
            c = lut.get((H, t))
            if c is not None:
                M[k, j] = getattr(c, metric)
    return M


def plot_surfaces(run: ScoreRun, cfg, *, estimator=GATED_ESTIMATOR, out_path=None) -> Path:
    if out_path is None:
        config.ensure_out_dirs()
        out_path = config.FIGURE_DIR / f"score_surfaces_{estimator}.png"
    out_path = Path(out_path)

    cells = [c for c in run.scores if c.estimator == estimator]
    Hs = sorted({c.H for c in cells})
    targets = sorted({c.target_peak_counts for c in cells})
    panels = [
        ("reliable_rate", "reliable rate", "viridis", None),
        ("omega0_bias_mean", "ω₀ bias (meV)", "RdBu_r", 0.0),
        ("gamma_bias_mean", "Γ bias (meV)", "RdBu_r", 0.0),
        ("omega0_pull_std", "ω₀ pull width", "RdBu_r", 1.0),
        ("gamma_pull_std", "Γ pull width", "RdBu_r", 1.0),
    ]
    fig, axes = plt.subplots(1, len(panels), figsize=(4.0 * len(panels), 3.8), squeeze=False)
    for ax, (metric, title, cmap, center) in zip(axes.ravel(), panels):
        M = _surface_matrix(run, estimator, metric, Hs, targets)
        norm = None
        if center is not None and np.any(np.isfinite(M)):
            lo, hi = np.nanmin(M), np.nanmax(M)
            span = max(abs(lo - center), abs(hi - center), 1e-6)
            norm = TwoSlopeNorm(vmin=center - span, vcenter=center, vmax=center + span)
        im = ax.imshow(M, aspect="auto", origin="lower", cmap=cmap, norm=norm,
                       extent=[min(Hs), max(Hs), -0.5, len(targets) - 0.5])
        ax.set_yticks(range(len(targets)))
        ax.set_yticklabels([f"{int(t)}" for t in targets])
        ax.set_xlabel("H (r.l.u.)")
        ax.set_ylabel("target peak counts")
        ax.set_title(f"{title}\n[{estimator}]", fontsize=9)
        fig.colorbar(im, ax=ax, fraction=0.046)
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    return out_path


def plot_threshold(run: ScoreRun, cfg, *, estimator=GATED_ESTIMATOR, out_path=None) -> Path:
    """Failure-rate-vs-counts: the data-threshold map (mid-zone reliable rate per count)."""
    if out_path is None:
        config.ensure_out_dirs()
        out_path = config.FIGURE_DIR / "score_threshold.png"
    out_path = Path(out_path)
    thr = data_threshold(run, cfg, estimator=estimator)
    counts = sorted(thr["by_count"])
    mean_rr = [thr["by_count"][t]["mean_reliable_rate"] for t in counts]
    min_rr = [thr["by_count"][t]["min_reliable_rate"] for t in counts]
    g_pw = [thr["by_count"][t]["max_gamma_pull_width"] for t in counts]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4.2))
    ax1.plot(counts, mean_rr, "o-", label="mean over mid-zone")
    ax1.plot(counts, min_rr, "s--", color="C3", label="worst mid-zone cell")
    ax1.axhline(thr["reliable_floor"], color="gray", ls=":", lw=1,
                label=f"floor {thr['reliable_floor']:.2f}")
    mc = thr["min_reliable_counts"]
    if mc is not None:
        ax1.axvline(mc, color="C2", lw=1.2, label=f"threshold = {int(mc)} cts")
    ax1.set_xlabel("target peak counts")
    ax1.set_ylabel("reliable rate")
    ax1.set_title(f"Data threshold — mid-zone reliability [{estimator}]")
    ax1.set_xscale("log")
    ax1.legend(fontsize=8)

    ax2.plot(counts, g_pw, "o-", color="C1", label="worst Γ pull-width")
    ax2.axhline(thr["pull_width_max"], color="gray", ls=":", lw=1,
                label=f"max {thr['pull_width_max']:.2f}")
    ax2.axhline(1.0, color="k", ls="-", lw=0.6, alpha=0.4)
    ax2.set_xlabel("target peak counts")
    ax2.set_ylabel("Γ pull-width (worst cell)")
    ax2.set_title("Γ calibration vs counts")
    ax2.set_xscale("log")
    ax2.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    return out_path


def plot_capstone(run: ScoreRun, cfg, *, estimator=GATED_ESTIMATOR, target=100.0,
                  out_path=None) -> Path:
    if out_path is None:
        config.ensure_out_dirs()
        out_path = config.FIGURE_DIR / "score_capstone_dispersion.png"
    out_path = Path(out_path)

    cells = sorted([c for c in run.scores
                    if c.estimator == estimator and c.target_peak_counts == target],
                   key=lambda c: c.H)
    fig, ax = plt.subplots(figsize=(8, 5))
    Hgrid = np.linspace(2.0, 3.0, 400)
    ax.plot(Hgrid, dispersion.omega0_of_H(Hgrid), "-", color="C7", lw=1.2,
            label="true 6·|sin(π(H−2))|")
    for c in cells:
        mk = "s" if c.classification == "boundary" else "o"
        col = "C1" if c.classification == "boundary" else "C0"
        # empirical scatter (true spread) — must straddle the sine
        ax.errorbar(c.H, c.omega0_recovered_mean, yerr=c.omega0_bias_std, fmt=mk, color=col,
                    ms=5, capsize=2, lw=1.2, zorder=3)
        # reported σ band (claimed uncertainty) — mismatch = miscalibration
        if np.isfinite(c.median_omega0_err):
            ax.errorbar(c.H, c.omega0_recovered_mean, yerr=c.median_omega0_err, fmt="none",
                        ecolor=col, alpha=0.35, elinewidth=4, capsize=0, zorder=2)
    ax.plot([], [], "o", color="C0", label="midzone: recovered ω₀ ± empirical std")
    ax.plot([], [], "s", color="C1", label="boundary (ω₀ error skewed)")
    ax.plot([], [], "-", color="C0", alpha=0.35, lw=4, label="reported σ_ω₀ (thick band)")
    ax.set_xlabel("H (r.l.u.)")
    ax.set_ylabel("ω₀ (meV)")
    ax.set_title(f"Capstone — recovered dispersion vs truth "
                 f"(peak {target:.0f} cts, [{estimator}])")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    return out_path


def plot_pull_histograms(run: ScoreRun, cfg, *, estimator=GATED_ESTIMATOR,
                         out_path=None) -> Path | None:
    if run.results is None:
        return None
    if out_path is None:
        config.ensure_out_dirs()
        out_path = config.FIGURE_DIR / "score_pull_histograms.png"
    out_path = Path(out_path)

    # Designate a few cells: lowest, mid, highest count at a midzone H.
    midz = [d for d in run.results
            if dispersion.classify_cell(d.H, cfg.gamma, cfg.eps_omega_d) == "midzone"
            and d.samples and estimator in d.samples]
    if not midz:
        return None
    Hmid = sorted({d.H for d in midz})[len(set(d.H for d in midz)) // 2]
    chosen = sorted([d for d in midz if d.H == Hmid], key=lambda d: d.target)
    targets = [chosen[0], chosen[len(chosen) // 2], chosen[-1]]

    fig, axes = plt.subplots(2, len(targets), figsize=(4.0 * len(targets), 6), squeeze=False)
    xx = np.linspace(-4, 4, 200)
    normal = np.exp(-xx**2 / 2) / np.sqrt(2 * np.pi)
    for col, d in enumerate(targets):
        s = d.samples[estimator]
        ok = s["success"]
        gt = generate.make_ground_truth(cfg, d.H, d.target, seed=0)
        opull = (s["omega0"][ok] - gt.omega0) / s["omega0_err"][ok]
        nf = ok & (~s["gamma_at_floor"])
        gpull = (s["gamma"][nf] - gt.gamma) / s["gamma_err"][nf]
        for row, (pull, lab) in enumerate([(opull, "ω₀"), (gpull, "Γ")]):
            ax = axes[row, col]
            if pull.size:
                ax.hist(pull, bins=25, density=True, color=f"C{row}", alpha=0.7)
            ax.plot(xx, normal, "k--", lw=1)
            w = float(np.std(pull, ddof=1)) if pull.size > 1 else float("nan")
            ax.set_title(f"{lab} pull  H={d.H:.2f} c={int(d.target)}  width={w:.2f}", fontsize=8)
            ax.set_xlabel("pull")
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    return out_path


def plot_minos_hesse(cfg, worst_cells, *, K=100, backend="iminuit", out_path=None) -> Path:
    """Re-fit K paired draws of the worst cells with MINOS; compare Γ-pull HESSE vs MINOS."""
    if out_path is None:
        config.ensure_out_dirs()
        out_path = config.FIGURE_DIR / "score_minos_hesse.png"
    out_path = Path(out_path)
    omap = datacell_ordinal_map(cfg)

    fig, axes = plt.subplots(1, len(worst_cells), figsize=(5.0 * len(worst_cells), 4), squeeze=False)
    for ax, (H, target) in zip(axes.ravel(), worst_cells):
        ordinal = omap[(float(H), float(target))]
        gt0 = generate.make_ground_truth(cfg, H, target, seed=0)
        hesse_pull, minos_pull = [], []
        for i in range(K):
            seed = realization_seed(cfg.base_seed, ordinal, cfg.M, i)
            gt = generate.make_ground_truth(cfg, H, target, seed=seed)
            counts = noise.draw_counts(gt)
            r = fit.fit_truth(gt, counts, estimator=GATED_ESTIMATOR, backend=backend, minos=True)
            if not r.success or r.gamma_at_floor:
                continue
            hesse_pull.append((r.gamma - gt0.gamma) / r.gamma_err)
            side = abs(r.gamma_err_lo) if r.gamma > gt0.gamma else abs(r.gamma_err_hi)
            if np.isfinite(side) and side > 0:
                minos_pull.append((r.gamma - gt0.gamma) / side)
        hp, mp = np.array(hesse_pull), np.array(minos_pull)
        wh = float(np.std(hp, ddof=1)) if hp.size > 1 else float("nan")
        wm = float(np.std(mp, ddof=1)) if mp.size > 1 else float("nan")
        if hp.size:
            ax.hist(hp, bins=20, density=True, alpha=0.5, label=f"HESSE (w={wh:.2f})")
        if mp.size:
            ax.hist(mp, bins=20, density=True, alpha=0.5, label=f"MINOS (w={wm:.2f})")
        xx = np.linspace(-4, 4, 200)
        ax.plot(xx, np.exp(-xx**2 / 2) / np.sqrt(2 * np.pi), "k--", lw=1)
        ratio = wh / wm if (np.isfinite(wh) and np.isfinite(wm) and wm) else float("nan")
        ax.set_title(f"Γ pull  H={H:.2f} c={int(target)}\nwidth HESSE/MINOS = {ratio:.2f}",
                     fontsize=9)
        ax.set_xlabel("Γ pull")
        ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    return out_path


# ============================================================================
# Entry point
# ============================================================================
def _print_report(rep: RunReport) -> None:
    thr = rep.threshold
    mc = thr.get("min_reliable_counts")
    print("=" * 72)
    print(f"DOCUMENTATION [{rep.estimator}] - failure rate & data threshold (no pass/fail)")
    print(f"  data threshold (min counts for reliable mid-zone fit): "
          f"{mc if mc is not None else 'NOT reached in swept range'}")
    print(f"  criterion: min reliable_rate >= {thr['reliable_floor']:.2f} and "
          f"mean pull-width <= {thr['pull_width_max']:.2f} over mid-zone cells")
    print(f"  {'counts':>7} {'minRel':>7} {'meanGpw':>8} {'maxGpw':>7} {'meanO0pw':>9} {'reliable?':>9}")
    for t, d in thr.get("by_count", {}).items():
        print(f"  {int(t):7d} {d['min_reliable_rate']:7.3f} {d['mean_gamma_pull_width']:8.2f} "
              f"{d['max_gamma_pull_width']:7.2f} {d['mean_omega0_pull_width']:9.2f} {str(d['ok']):>9}")
    wc = [c for c in rep.cells if c.classification == "midzone" and c.well_counted]
    if wc:
        ob = np.nanmean([abs(c.omega0_bias_mean) for c in wc])
        gb = np.nanmean([abs(c.gamma_bias_mean) for c in wc])
        o0pw = np.nanmean([c.omega0_pull_std for c in wc])
        gpw = np.nanmean([c.gamma_pull_std for c in wc])
        print(f"  well-counted mid-zone ({len(wc)} cells): mean |bias| omega0={ob:.3f} "
              f"gamma={gb:.3f} meV; pull-width omega0={o0pw:.2f} gamma={gpw:.2f}")
    print("=" * 72)


def main() -> None:
    p = argparse.ArgumentParser(description="ISAR synthetic-core validation (document-only)")
    p.add_argument("--dev", action="store_true", help="tiny dev config (fast smoke)")
    p.add_argument("--M", type=int, default=None, help="override Monte-Carlo M per cell")
    p.add_argument("--workers", type=int, default=None, help="process pool size (1 = serial)")
    args = p.parse_args()

    cfg = config.default_run_config(dev_mode=args.dev)
    if args.M is not None:
        cfg = dataclasses.replace(cfg, M=args.M)
    config.ensure_out_dirs()
    n_workers = args.workers if args.workers is not None else (1 if args.dev else None)
    print(f"[score] M={cfg.M}  estimators={cfg.estimators}  workers={n_workers}")
    run = run_scorer(cfg, n_workers=n_workers, estimators=cfg.estimators)
    rep = characterize_run(run, cfg)
    print(f"[score] wrote {config.SUMMARY_CSV}  ({len(run.scores)} cell-scores)")
    print(f"[score] figures in {config.FIGURE_DIR}")
    _print_report(rep)


if __name__ == "__main__":
    main()

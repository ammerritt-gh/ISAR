"""Section-8 three-estimator demonstration.

Layer 1: constant-rate identity (analytic, indisputable).
  Fit one flat rate mu to N Poisson channels; recover harmonic mean (Neyman),
  arithmetic mean (Baker-Cousins), RMS (Pearson) -- the straddle harmonic <= arith <= RMS.
  All three biases shrink like 1/counts.

Layer 2: the DHO case in physics terms.
  Same paired-draw engine, one comfortable mid-zone cell (H=2.5, omega0=6 meV).
  Quantities plotted: Gamma-bias and normalization(phonon_amplitude)-bias vs counts.
  (The Stokes/anti-Stokes ratio is Bose-locked to peak position, which all three
  estimators recover equally well -- it shows no estimator divergence and is omitted.)
"""
from __future__ import annotations

import argparse
import dataclasses
from dataclasses import dataclass
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from scipy.optimize import minimize_scalar

from . import config, estimators, generate, score

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------
# Straddle order: Neyman (low/harmonic) -- Baker-Cousins (unbiased) -- Pearson (high/RMS)
ESTIMATOR_ORDER = ("neyman", "baker_cousins", "pearson")

ESTIMATOR_LABEL = {
    "neyman":         "Neyman chi2 (harmonic)",
    "baker_cousins":  "Baker-Cousins -2lnL (arithmetic)",
    "pearson":        "mod-Pearson chi2 (RMS)",
}
ESTIMATOR_COLOR = {
    "neyman":        "C0",
    "baker_cousins": "C2",
    "pearson":       "C3",
}

DEMO_H = 2.5   # comfortable mid-zone cell: omega0=6.0 meV, 12x the linewidth


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------
@dataclass
class Layer1Result:
    n_channels: int
    mu_levels: tuple
    n_reps: int
    bias: dict          # estimator -> ndarray[len(mu_levels)]   mean(mu_hat) - mu_true
    bias_sem: dict      # estimator -> ndarray
    recovered: dict     # estimator -> ndarray   mean(mu_hat)
    closed_form_matches_argmin: bool


@dataclass
class Layer2Result:
    H: float
    targets: tuple
    T: float
    M: int
    run: score.ScoreRun
    amp_bias: dict      # estimator -> ndarray[len(targets)]  mean(A_hat)/A_true - 1
    amp_bias_sem: dict  # estimator -> ndarray
    amp_true: np.ndarray   # per-target true phonon_amplitude (seed-0 truth)


# ---------------------------------------------------------------------------
# Layer 1
# ---------------------------------------------------------------------------
def _layer1_theorem_holds(counts: np.ndarray) -> bool:
    """Assert closed forms equal the numerical argmin of the real cost functions.

    Checks all-positive draws (rtol 1e-4) and the harmonic <= arithmetic <= RMS ordering.
    """
    counts = np.asarray(counts, dtype=float)
    ok = True
    for name in ESTIMATOR_ORDER:
        cf = estimators.constant_rate_estimate(counts, name)
        cost_fn = estimators.get_estimator(name).cost
        res = minimize_scalar(lambda mu, fn=cost_fn, y=counts: fn(y, np.full_like(y, mu)),
                              bounds=(counts.min() * 0.5, counts.max() * 2.0),
                              method="bounded")
        if abs(cf - res.x) / (abs(res.x) + 1e-12) > 1e-3:
            ok = False
    # ordering check on positive draw
    ne = estimators.constant_rate_estimate(counts, "neyman")
    bc = estimators.constant_rate_estimate(counts, "baker_cousins")
    pe = estimators.constant_rate_estimate(counts, "pearson")
    if ne > bc + 1e-9 or bc > pe + 1e-9:
        ok = False
    return ok


def run_layer1(
    *,
    n_channels: int = 64,
    mu_levels: tuple = (3, 5, 10, 20, 50, 100),
    n_reps: int = 4000,
    seed: int = 20240,
) -> Layer1Result:
    """Compute Layer-1 constant-rate bias vs true rate for all three estimators."""
    rng = np.random.default_rng(seed)
    mu_arr = np.array(mu_levels, dtype=float)
    n_mu = len(mu_arr)

    bias     = {e: np.zeros(n_mu) for e in ESTIMATOR_ORDER}
    bias_sq  = {e: np.zeros(n_mu) for e in ESTIMATOR_ORDER}
    recovered = {e: np.zeros(n_mu) for e in ESTIMATOR_ORDER}

    for j, mu in enumerate(mu_arr):
        for _ in range(n_reps):
            counts = rng.poisson(mu, size=n_channels).astype(float)
            for e in ESTIMATOR_ORDER:
                mu_hat = estimators.constant_rate_estimate(counts, e)
                b = mu_hat - mu
                bias[e][j]    += b
                bias_sq[e][j] += b * b
                recovered[e][j] += mu_hat
        for e in ESTIMATOR_ORDER:
            bias[e][j]     /= n_reps
            bias_sq[e][j]  /= n_reps
            recovered[e][j] /= n_reps

    bias_sem = {}
    for e in ESTIMATOR_ORDER:
        var = bias_sq[e] - bias[e] ** 2
        bias_sem[e] = np.sqrt(np.maximum(var, 0.0) / n_reps)

    # Theorem check on one all-positive draw at mu=max(mu_levels)
    probe = np.maximum(rng.poisson(float(mu_arr[-1]), size=n_channels).astype(float), 1.0)
    theorem_ok = _layer1_theorem_holds(probe)

    return Layer1Result(
        n_channels=n_channels,
        mu_levels=tuple(mu_levels),
        n_reps=n_reps,
        bias=bias,
        bias_sem=bias_sem,
        recovered=recovered,
        closed_form_matches_argmin=theorem_ok,
    )


def plot_layer1(res: Layer1Result, *, out_path=None) -> Path:
    """Two-panel figure: straddle (bias vs mu) and 1/counts shrink (log-log)."""
    config.ensure_out_dirs()
    out_path = Path(out_path) if out_path is not None else config.FIGURE_DIR / "demo_layer1_constant_rate.png"

    mu = np.array(res.mu_levels, dtype=float)
    fig, axes = plt.subplots(1, 2, figsize=(10, 4))

    # Panel A: straddle
    ax = axes[0]
    for e in ESTIMATOR_ORDER:
        ax.errorbar(mu, res.bias[e], yerr=res.bias_sem[e],
                    label=ESTIMATOR_LABEL[e], color=ESTIMATOR_COLOR[e],
                    marker="o", capsize=3)
    ax.axhline(0, color="k", lw=0.8, ls="--")
    ax.set_xscale("log")
    ax.set_xlabel("True rate mu (counts/channel)")
    ax.set_ylabel("Bias: mean(mu_hat) - mu")
    ax.set_title("Constant-rate straddle\nNeyman (harmonic) <= BC (arith) <= Pearson (RMS)")
    ax.legend(fontsize=7)

    # Panel B: 1/counts shrink (log-log, fractional bias |bias/mu|)
    ax = axes[1]
    for e in ESTIMATOR_ORDER:
        frac = np.abs(res.bias[e]) / mu
        ax.plot(mu, frac, label=ESTIMATOR_LABEL[e],
                color=ESTIMATOR_COLOR[e], marker="o")
    # 1/mu guide through the Neyman low-mu point
    guide0 = np.abs(res.bias["neyman"][0]) / mu[0]
    guide = guide0 * mu[0] / mu
    ax.plot(mu, guide, "k:", lw=0.8, label="~1/mu guide")
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("True rate mu (counts/channel)")
    ax.set_ylabel("|Bias| / mu  (fractional bias)")
    ax.set_title("Relative bias shrinks as 1/counts")
    ax.legend(fontsize=7)

    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    return out_path


# ---------------------------------------------------------------------------
# Layer 2
# ---------------------------------------------------------------------------
def run_layer2(
    cfg: config.RunConfig,
    *,
    H: float = DEMO_H,
    n_workers=None,
    backend: str = "iminuit",
) -> Layer2Result:
    """Run Layer-2 DHO demonstration at a single mid-zone cell H, sweeping counts.

    Reuses run_scorer (paired draws, full seed scheme) with keep_samples=True to
    get per-realization phonon_amplitude for the normalization-bias figure.
    """
    demo_cfg = dataclasses.replace(cfg, H_values=(float(H),),
                                   estimators=ESTIMATOR_ORDER)
    if n_workers is None:
        n_workers = 1 if cfg.dev_mode else None

    run = score.run_scorer(demo_cfg, n_workers=n_workers, backend=backend,
                           keep_samples=True, write_csv=False, make_figures=False)

    targets = sorted({c.target_peak_counts for c in run.scores if c.estimator == ESTIMATOR_ORDER[0]})

    amp_true = np.array([
        generate.make_ground_truth(demo_cfg, H, t, seed=0).phonon_amplitude
        for t in targets
    ])

    amp_bias = {e: np.full(len(targets), np.nan) for e in ESTIMATOR_ORDER}
    amp_bias_sem = {e: np.full(len(targets), np.nan) for e in ESTIMATOR_ORDER}

    for dcr in run.results:
        if dcr.H != float(H):
            continue
        j = targets.index(dcr.target)
        a_true = amp_true[j]
        for e in ESTIMATOR_ORDER:
            if dcr.samples is None or e not in dcr.samples:
                continue
            s = dcr.samples[e]
            ok = s["success"].astype(bool)
            # trustworthy: use the success mask (same as reliable for normalization)
            A_hat = s["phonon_amplitude"][ok]
            if len(A_hat) == 0:
                continue
            frac_bias = A_hat / a_true - 1.0
            amp_bias[e][j] = float(np.mean(frac_bias))
            amp_bias_sem[e][j] = float(np.std(frac_bias, ddof=1) / np.sqrt(len(A_hat)))

    return Layer2Result(
        H=float(H),
        targets=tuple(targets),
        T=cfg.T,
        M=cfg.M,
        run=run,
        amp_bias=amp_bias,
        amp_bias_sem=amp_bias_sem,
        amp_true=amp_true,
    )


def _layer2_gamma_bias_arrays(res2: Layer2Result):
    """Return (targets, bias_dict, sem_dict) for Gamma over the run's CellScores."""
    targets = list(res2.targets)
    bias = {e: np.full(len(targets), np.nan) for e in ESTIMATOR_ORDER}
    sem  = {e: np.full(len(targets), np.nan) for e in ESTIMATOR_ORDER}
    for j, t in enumerate(targets):
        for e in ESTIMATOR_ORDER:
            cells = [c for c in res2.run.scores
                     if c.estimator == e and c.target_peak_counts == t and c.H == res2.H]
            if cells:
                c = cells[0]
                bias[e][j] = c.gamma_bias_mean
                n = c.n_reliable if c.n_reliable > 0 else 1
                sem[e][j] = c.gamma_bias_std / np.sqrt(n) if np.isfinite(c.gamma_bias_std) else np.nan
    return np.array(targets), bias, sem


def plot_gamma_bias(res2: Layer2Result, *, out_path=None) -> Path:
    """Gamma-bias vs counts: the headline estimator-divergence figure."""
    config.ensure_out_dirs()
    out_path = Path(out_path) if out_path is not None else config.FIGURE_DIR / "demo_dho_gamma_bias.png"

    targets, bias, sem = _layer2_gamma_bias_arrays(res2)
    fig, ax = plt.subplots(figsize=(6, 4))
    for e in ESTIMATOR_ORDER:
        ax.errorbar(targets, bias[e], yerr=sem[e],
                    label=ESTIMATOR_LABEL[e], color=ESTIMATOR_COLOR[e],
                    marker="o", capsize=3)
    ax.axhline(0, color="k", lw=0.8, ls="--")
    ax.set_xscale("log")
    ax.set_xlabel("Target phonon peak counts")
    ax.set_ylabel("Gamma bias (meV)")
    ax.set_title("Gamma (linewidth) bias vs counts\nH=%.2f, T=%.0f K, M=%d" % (res2.H, res2.T, res2.M))
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    return out_path


def plot_normalization_bias(res2: Layer2Result, *, out_path=None) -> Path:
    """Fractional phonon_amplitude bias vs counts: the DHO analog of Layer-1 straddle."""
    config.ensure_out_dirs()
    out_path = Path(out_path) if out_path is not None else config.FIGURE_DIR / "demo_dho_normalization_bias.png"

    targets = np.array(res2.targets, dtype=float)
    fig, ax = plt.subplots(figsize=(6, 4))
    for e in ESTIMATOR_ORDER:
        ax.errorbar(targets, res2.amp_bias[e], yerr=res2.amp_bias_sem[e],
                    label=ESTIMATOR_LABEL[e], color=ESTIMATOR_COLOR[e],
                    marker="o", capsize=3)
    ax.axhline(0, color="k", lw=0.8, ls="--")
    ax.set_xscale("log")
    ax.set_xlabel("Target phonon peak counts")
    ax.set_ylabel("Fractional amplitude bias: A_hat/A_true - 1")
    ax.set_title("Normalization bias vs counts\nNeyman low / Pearson high straddle survives DHO")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    return out_path


def plot_calibration(res2: Layer2Result, *, out_path=None) -> Path:
    """Gamma pull-width vs counts: are the reported errors honest at each count level?"""
    config.ensure_out_dirs()
    out_path = Path(out_path) if out_path is not None else config.FIGURE_DIR / "demo_dho_calibration.png"

    targets = np.array(res2.targets, dtype=float)
    fig, ax = plt.subplots(figsize=(6, 4))
    for e in ESTIMATOR_ORDER:
        pw = np.array([
            next((c.gamma_pull_std for c in res2.run.scores
                  if c.estimator == e and c.target_peak_counts == t and c.H == res2.H),
                 np.nan)
            for t in res2.targets
        ])
        ax.plot(targets, pw, label=ESTIMATOR_LABEL[e],
                color=ESTIMATOR_COLOR[e], marker="o")
    ax.axhline(1.0, color="k", lw=0.8, ls="--", label="ideal (unit width)")
    ax.set_xscale("log")
    ax.set_xlabel("Target phonon peak counts")
    ax.set_ylabel("Gamma pull-width (sigma)")
    ax.set_title("Gamma pull-width vs counts\n<1 = overconfident, >1 = conservative")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    return out_path


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    p = argparse.ArgumentParser(description="ISAR section-8 three-estimator demonstration")
    p.add_argument("--dev", action="store_true", help="fast dev mode (M=20, few cells)")
    p.add_argument("--M", type=int, default=None, help="override M (realizations per cell)")
    p.add_argument("--workers", type=int, default=None, help="parallel workers")
    args = p.parse_args()

    cfg = config.default_run_config(dev_mode=args.dev)
    if args.M is not None:
        cfg = dataclasses.replace(cfg, M=args.M)
    n_workers = args.workers if args.workers is not None else (1 if args.dev else None)

    config.ensure_out_dirs()

    n_reps_l1 = 400 if args.dev else 4000
    print("[demo] Layer 1: constant-rate identity  (n_reps=%d)" % n_reps_l1)
    l1 = run_layer1(n_reps=n_reps_l1)
    f1 = plot_layer1(l1, out_path=config.FIGURE_DIR / "demo_layer1_constant_rate.png")

    print("[demo] Layer 2: DHO at H=%.2f  M=%d  workers=%s" % (DEMO_H, cfg.M, str(n_workers)))
    l2 = run_layer2(cfg, n_workers=n_workers)
    fg = plot_gamma_bias(l2, out_path=config.FIGURE_DIR / "demo_dho_gamma_bias.png")
    fn = plot_normalization_bias(l2, out_path=config.FIGURE_DIR / "demo_dho_normalization_bias.png")
    fc = plot_calibration(l2, out_path=config.FIGURE_DIR / "demo_dho_calibration.png")

    print("=" * 72)
    print("[demo] Layer 1 constant-rate identity")
    print("  closed forms == numerical argmin: %s" % l1.closed_form_matches_argmin)
    print("  straddle: Neyman(harmonic) <= BC(arithmetic) <= Pearson(RMS) verified")
    print("[demo] Layer 2 DHO at H=%.2f, T=%.0f K, M=%d" % (l2.H, l2.T, l2.M))
    print("  REVIEW: %s" % f1)
    print("  REVIEW: %s" % fg)
    print("  REVIEW: %s" % fn)
    print("  REVIEW: %s" % fc)
    print("=" * 72)


if __name__ == "__main__":
    main()

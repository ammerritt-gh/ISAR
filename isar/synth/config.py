"""Configuration objects and core data structures for the synthetic harness.

Truth lives in (ω₀, Γ) — the conventional dispersion quantities. The damped peak
position Ω_d = √(ω₀²−Γ²) exists only in fit space (`fit.py`), never in the manifest.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import dispersion

# ----------------------------------------------------------------------------
# Parameter ordering (fixes the optimizer vector layout)
# ----------------------------------------------------------------------------
# Free fit parameters, in optimizer-vector order.
FIT_PARAM_ORDER = ("omega_d", "gamma", "phonon_amplitude", "elastic_amplitude", "BG")

# Positional argument order of convolute_voigt_dho after (x_target,), for reference.
MODEL_PARAM_ORDER = (
    "phonon_energy", "center", "gauss_fwhm", "lorentz_fwhm", "damping",
    "phonon_amplitude", "elastic_amplitude", "BG", "T",
)

# ----------------------------------------------------------------------------
# Output locations (out/ at the repo root, sibling of the isar/ package)
# ----------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = _REPO_ROOT / "out"
MANIFEST_DIR = OUT_DIR / "manifests"
FIGURE_DIR = OUT_DIR / "figures"
SUMMARY_CSV = OUT_DIR / "summary.csv"


def ensure_out_dirs() -> None:
    """Create the output directory tree if it does not exist."""
    for d in (OUT_DIR, MANIFEST_DIR, FIGURE_DIR):
        d.mkdir(parents=True, exist_ok=True)


# ----------------------------------------------------------------------------
# Ground-truth manifest (one per synthetic scan)
# ----------------------------------------------------------------------------
@dataclass
class GroundTruth:
    """Exact parameters and RNG seed that produced one synthetic scan."""

    H: float
    omega0: float
    gamma: float
    phonon_amplitude: float
    elastic_amplitude: float
    BG: float
    gauss_fwhm: float
    lorentz_fwhm: float
    T: float
    x_min: float
    x_max: float
    x_step: float
    target_peak_counts: float
    seed: int
    center: float = 0.0
    counts_scale: float | None = None
    peak: str = "all"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "GroundTruth":
        return cls(**d)


# ----------------------------------------------------------------------------
# Run configuration (the validation surface and its axes)
# ----------------------------------------------------------------------------
@dataclass
class RunConfig:
    # Happy-path physics (settled: T = 100 K, Γ = 0.5 meV).
    gamma: float = 0.5
    T: float = 100.0
    gauss_fwhm: float = 0.961
    lorentz_fwhm: float = 0.596

    # Relative spectrum shape (both scale with counting time, so the ratios are
    # sample/facility properties, fixed across count levels). Tunable:
    #   elastic ratio — "very low" (energy-zero risk) up to a few × the phonon;
    #     rarely orders of magnitude (a very strong elastic line also drives up BG
    #     via second-order effects, not modelled here — raise bg_ratio alongside it).
    #   bg ratio — IXS is near-zero; INS facility backgrounds are larger (~0.1–0.5).
    elastic_to_phonon_height_ratio: float = 3.0
    bg_to_phonon_height_ratio: float = 0.02

    # Energy grid: ±10 meV at 0.25 meV → 81 channels (the DATA grid; plots render
    # the model on a finer grid for smoothness).
    x_min: float = -10.0
    x_max: float = 10.0
    x_step: float = 0.25

    # Sweep axes. Target counts = phonon peak above background:
    #   ~100 is "good"; 20–30 is the routine floor; below ~20 fits get very hard.
    #   The low end probes where recovery fails; 300 anchors the well-counted regime.
    H_values: tuple = field(default_factory=lambda: tuple(dispersion.h_values().tolist()))
    target_counts_levels: tuple = (15, 20, 30, 50, 100, 300)
    estimators: tuple = ("baker_cousins", "neyman", "pearson")

    # Monte Carlo.
    M: int = 1000
    base_seed: int = 12345

    # Numerical floors.
    eps_omega_d: float = 1e-3
    gamma_floor: float = 1e-3

    dev_mode: bool = False


def default_run_config(dev_mode: bool = False) -> RunConfig:
    """Full validation config, or a small fast config for iteration."""
    if dev_mode:
        return RunConfig(
            H_values=(2.05, 2.25, 2.5, 2.75, 2.95),
            target_counts_levels=(30, 100),
            M=20,
            dev_mode=True,
        )
    return RunConfig()


# ----------------------------------------------------------------------------
# Fit result (one fit of one scan under one estimator)
# ----------------------------------------------------------------------------
_NAN = math.nan


@dataclass
class FitResult:
    estimator: str
    success: bool = False
    covariance_ok: bool = False
    gamma_at_floor: bool = False
    message: str = ""
    n_eval: int = 0
    seed: int = -1

    # Fit-space parameters + symmetric errors.
    omega_d: float = _NAN
    gamma: float = _NAN
    phonon_amplitude: float = _NAN
    elastic_amplitude: float = _NAN
    BG: float = _NAN
    omega_d_err: float = _NAN
    gamma_err: float = _NAN
    phonon_amplitude_err: float = _NAN
    elastic_amplitude_err: float = _NAN
    BG_err: float = _NAN

    # Asymmetric (profile / MINOS) errors where available.
    omega_d_err_lo: float = _NAN
    omega_d_err_hi: float = _NAN
    gamma_err_lo: float = _NAN
    gamma_err_hi: float = _NAN

    # Derived dispersion quantity ω₀ = √(Ω_d² + Γ²).
    omega0: float = _NAN
    omega0_err: float = _NAN
    omega0_err_lo: float = _NAN
    omega0_err_hi: float = _NAN

    # Goodness of fit.
    cost: float = _NAN
    ndof: int = 0
    cost_per_dof: float = _NAN

    def to_dict(self) -> dict:
        return asdict(self)


# ----------------------------------------------------------------------------
# Per-cell score (one (H, counts, estimator) Monte-Carlo summary)
# ----------------------------------------------------------------------------
@dataclass
class CellScore:
    H: float
    target_peak_counts: float
    estimator: str
    classification: str = ""          # 'midzone' | 'boundary' | 'unmeasurable'
    n_total: int = 0
    n_success: int = 0                # converged (FitResult.success)
    success_rate: float = _NAN        # convergence rate
    # Reliability: a converged fit is 'untrustworthy' if it reports a σ that is a gross
    # low outlier vs its cell peers (the degenerate-Γ→0 corner, σ catastrophically small).
    # These are counted as FAILURES (documented), not silently dropped. Stats below are
    # computed over the reliable set; (1 − reliable_rate) is the documented failure rate.
    n_reliable: int = 0
    n_untrustworthy: int = 0
    reliable_rate: float = _NAN
    median_omega0_err: float = _NAN   # over reliable fits (for the 'well-counted' label)

    omega0_bias_mean: float = _NAN
    omega0_bias_std: float = _NAN
    gamma_bias_mean: float = _NAN     # over ALL successful fits (incl. gamma_at_floor)
    gamma_bias_std: float = _NAN

    omega0_pull_mean: float = _NAN    # over successful fits
    omega0_pull_std: float = _NAN
    gamma_pull_mean: float = _NAN     # over successful AND NOT gamma_at_floor
    gamma_pull_std: float = _NAN

    # Censoring bookkeeping (Γ-pull uses the non-floored set; Γ-bias uses all successful).
    n_gamma_used: int = 0             # successful & not gamma_at_floor (the Γ-pull set)
    gamma_floored_fraction: float = _NAN

    omega0_recovered_mean: float = _NAN
    omega0_recovered_sem: float = _NAN
    gamma_recovered_mean: float = _NAN
    gamma_recovered_sem: float = _NAN

    def to_dict(self) -> dict:
        return asdict(self)

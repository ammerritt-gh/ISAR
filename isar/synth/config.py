"""Configuration objects and core data structures for the synthetic harness.

Truth lives in (ω₀, Γ) — the conventional dispersion quantities. The damped peak
position Ω_d = √(ω₀²−Γ²) exists only in fit space (`fit.py`), never in the manifest.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import dispersion


def _reject_unknown_keys(d: dict, allowed: set[str], cls_name: str) -> None:
    """Raise if ``d`` carries a key ``from_dict`` on ``cls_name`` does not know about.

    A silently-dropped extra key means a manifest round-trip can lose data without
    telling anyone; from_dict must fail loudly instead.
    """
    unexpected = set(d) - allowed
    if unexpected:
        raise ValueError(f"{cls_name}.from_dict: unexpected key(s) {sorted(unexpected)!r}")

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
# World-level ground truth (SCENE world suite)
# ----------------------------------------------------------------------------
# A `GroundTruth` describes one scan of one branch. A SCENE world is a whole Q-path
# carrying several components at once, so its truth is a level up: what exists on the
# path, what each thing IS, and the seed that turned that into counts. The manifest is
# both the generator input and the scoring key — there is deliberately no second copy of
# the truth for the generator to drift away from.
SCENE_TRUTH_SCHEMA = "isar.scene_world_truth/1"

# Component kinds. 'dho' and 'elastic' are the production model's own shapes; 'unknown'
# is the deliberate model mismatch (see noise.two_horn_profile) and must never be
# fittable by the production DHO.
COMPONENT_KINDS = ("dho", "elastic", "unknown")


@dataclass(frozen=True)
class WorldConfig:
    """The observation plan for one world: Q-path, energy window, resolution, seed.

    The *intrinsic* physics of a world lives in 0–10 meV; the scan window runs to −10 meV
    so the anti-Stokes side is actually measured (that side is the whole point of the
    warm/cold pair).
    """

    name: str
    seed: int
    H_start: float = dispersion.H_START
    H_stop: float = dispersion.H_STOP
    H_step: float = dispersion.H_STEP
    x_min: float = -10.0
    x_max: float = 10.0
    x_step: float = 0.25
    gauss_fwhm: float = 0.961
    lorentz_fwhm: float = 0.596

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "WorldConfig":
        _reject_unknown_keys(d, {"name", "seed", "H_start", "H_stop", "H_step",
                                  "x_min", "x_max", "x_step", "gauss_fwhm", "lorentz_fwhm"},
                             "WorldConfig")
        return cls(name=str(d["name"]), seed=int(d["seed"]),
                   **{k: float(d[k]) for k in
                      ("H_start", "H_stop", "H_step", "x_min", "x_max", "x_step",
                       "gauss_fwhm", "lorentz_fwhm")})


@dataclass(frozen=True)
class ComponentTruth:
    """One thing that is really there, and what it really is.

    ``identity`` is the scoring key a scene hypothesis has to recover ('acoustic',
    'optic', 'bragg', 'level', 'unknown', …). It is never consulted by the generator:
    what a component *does* is set by ``kind`` + ``branch``, so a world cannot cheat by
    labelling a gapped branch 'acoustic'.

    ``peak_counts`` is the component maximum in counts where its Q-envelope is 1.
    ``q_centers`` empty means the component is present at every H; otherwise the
    component is localized in Q by a Gaussian of FWHM ``q_fwhm`` about each centre
    (a Bragg-like elastic line lives at the zone centres, an off-lattice one does not).
    ``width``/``asymmetry`` are the 'unknown' lineshape's horn separation (meV) and horn
    imbalance in [−1, 1]; they are ignored by the other kinds.
    """

    kind: str
    identity: str
    peak_counts: float
    branch: "dispersion.BranchSpec | None" = None
    gamma: float = 0.5
    q_centers: tuple[float, ...] = ()
    q_fwhm: float = 0.08
    width: float = 0.0
    asymmetry: float = 0.0

    def __post_init__(self):
        if self.kind not in COMPONENT_KINDS:
            raise ValueError(f"unknown component kind {self.kind!r}; expected {COMPONENT_KINDS}")
        if self.kind in ("dho", "unknown") and self.branch is None:
            raise ValueError(f"component kind {self.kind!r} needs a branch")
        if self.kind == "elastic" and self.branch is not None:
            raise ValueError("elastic components must not declare a branch")
        if self.q_centers and self.q_fwhm <= 0:
            raise ValueError(
                f"q_fwhm must be > 0 for a Q-localized component; got {self.q_fwhm!r}")
        if self.kind == "unknown":
            # width is the horn separation: 0 collapses the mismatch world into a
            # single-horn DHO lookalike, defeating the world's whole purpose.
            if self.width <= 0:
                raise ValueError(
                    f"unknown components need a positive horn separation; got width={self.width!r}")
            if not -1.0 <= self.asymmetry <= 1.0:
                raise ValueError(
                    f"unknown component asymmetry must be in [-1, 1]; got {self.asymmetry!r}")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["branch"] = None if self.branch is None else self.branch.to_dict()
        d["q_centers"] = list(self.q_centers)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ComponentTruth":
        _reject_unknown_keys(d, {"kind", "identity", "peak_counts", "branch", "gamma",
                                  "q_centers", "q_fwhm", "width", "asymmetry"},
                             "ComponentTruth")
        branch = d.get("branch")
        return cls(
            kind=str(d["kind"]), identity=str(d["identity"]),
            peak_counts=float(d["peak_counts"]),
            branch=None if branch is None else dispersion.BranchSpec.from_dict(branch),
            gamma=float(d["gamma"]),
            q_centers=tuple(float(q) for q in d.get("q_centers", ())),
            q_fwhm=float(d["q_fwhm"]), width=float(d["width"]),
            asymmetry=float(d["asymmetry"]),
        )


@dataclass(frozen=True)
class WorldTruth:
    """Everything true about one seeded world — the calibration-scoring key."""

    config: WorldConfig
    T: float
    background_counts: float
    components: tuple[ComponentTruth, ...] = ()
    description: str = ""
    schema: str = SCENE_TRUTH_SCHEMA

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def seed(self) -> int:
        return self.config.seed

    def to_dict(self) -> dict:
        return {
            "schema": self.schema,
            "config": self.config.to_dict(),
            "T": self.T,
            "background_counts": self.background_counts,
            "components": [c.to_dict() for c in self.components],
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "WorldTruth":
        _reject_unknown_keys(d, {"schema", "config", "T", "background_counts",
                                  "components", "description"}, "WorldTruth")
        schema = str(d.get("schema", SCENE_TRUTH_SCHEMA))
        if schema != SCENE_TRUTH_SCHEMA:
            raise ValueError(f"unsupported world-truth schema {schema!r}")
        return cls(
            config=WorldConfig.from_dict(d["config"]),
            T=float(d["T"]), background_counts=float(d["background_counts"]),
            components=tuple(ComponentTruth.from_dict(c) for c in d.get("components", ())),
            description=str(d.get("description", "")), schema=schema,
        )


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

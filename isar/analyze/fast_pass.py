"""Fast-pass engine: the boundary the peak-count fitter is built against (design sec. 2.2-2.3).

Qt-free (ISAR hard rule 6); stdlib imports only. Both entry points are stubs until S1U4 fills them.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal

@dataclass(frozen=True)
class FastPassSettings:
    mode_ceiling: int = 6                    # pinned known limit (operator); card [fitting] max_modes
    safety_budget_s: float = 300.0           # per-spectrum runaway stop (design agent default)
    contending_margin: float = 10.0          # nats, = count_ladder.CONTENDING_MARGIN (existing)
    close_call_p: float = 0.80               # preferred count below this -> close call (operator)
    seed_stop_gain: float = 6.0              # PROVISIONAL (S1U7): 2*dlnL a seed peak must add
    confident_anchor: tuple[tuple[float, float], ...] = ((0.0, 50.0),)
                                             # PROVISIONAL (S1U7): (spectrum max count, deletion-
                                             # impact anchor) pairs, interpolated log-linearly in the
                                             # count, clamped at the ends; one pair = a constant
    flag_residual_threshold: float = 0.0     # PROVISIONAL (S1U7): 0.0 = uncalibrated -> that flag kind off
    overdamped_correlation: float = 0.9      # PROVISIONAL (S1U7): |corr(omega0, Gamma)| above -> overdamped warning
    near_elastic_fwhm: float = 2.0           # resolution FWHMs from zero -> near elastic (design)
    material_p: float = 0.05                 # an alternative carrying at least this is "material" (agent default)
    twin_height_frac: float = 1e-3           # height below frac x height ceiling -> zero-height twin screen
    twin_coincide_fwhm: float = 0.5          # |d omega0| below this x FWHM -> coincident-pair screen
    elastic_twin_tolerance: float = 0.05     # max|unit column - elastic column| -> elastic twin
    twin_cost_tolerance: float = 2.0         # deviance: a screened twin is one only if the lower model refits within this
    split_separations_fwhm: tuple[float, ...] = (0.25, 1.0)
    n_restarts: int = 3                      # perturbed restarts per seeded start
    basin_merge_fwhm: float = 0.3            # one basin needs every mode's |d omega0| < this x FWHM, and:
    basin_gamma_frac: float = 0.3            # (new) every mode's |d Gamma| <= this x max(FWHM, larger Gamma)
    basin_height_frac: float = 0.3           # (new) every mode's |d h| <= this x larger h
    trf_max_nfev: int = 160                  # per TRF call (prototype value; hitting it = that start failed)
    posterior_maxiter: int = 1000            # (new) L-BFGS-B iterations for the posterior mode (Defect D)
    audit_rounds: int = 1
    prior: "PriorSettings | None" = None     # C-prior; None -> recipe v5

Regime = Literal["near_elastic", "underdamped", "overdamped"]
ParamStatus = Literal["constrained", "model_dependent", "boundary_limited", "undetermined"]
EvidenceStatus = Literal["ok", "ok_boundary", "degenerate_twin", "no_evidence", "not_fitted"]
CountState = Literal["resolved", "close_call", "unresolved", "search_limited"]

@dataclass(frozen=True)
class PeakResult:
    index: int                       # 0-based, ascending omega0 within the selected model
    omega0: float                    # meV, undamped energy E0 (record key omega0)
    Gamma: float                     # meV; GAMMA_FLOOR for a delta mode
    delta: bool                      # (new) resolution limited: Gamma is a fixed model coordinate,
                                     #   not a measured zero width
    height: float                    # counts/point at reference exposure (C-dho's maximum)
    area_stokes: float               # counts*meV, E > 0 side of the convolved profile
    area_both: float                 # counts*meV, both sides
    sigma_omega0: float | None       # posterior-mode covariance errors; None if unavailable
    sigma_Gamma: float | None        #   (always None for a delta mode)
    sigma_height: float | None
    sigma_area: float | None         # Stokes area, delta method through (omega0, Gamma, h)
    sigma_area_both: float | None
    area_status: Literal["usable", "decomposition_conditional", "unavailable"]
    corr_omega0_Gamma: float | None  # None for a delta mode or a singular covariance
    regime: Regime                   # physical regime, recorded apart from status (operator)
    status: dict[str, ParamStatus]   # keys "omega0", "Gamma", "height", "area"; a delta mode's
                                     #   Gamma is "boundary_limited"
    confident: bool
    deletion_impact: float           # visibility-grade units (design 2.6), others held fixed
    manual_fit: bool                 # near elastic or overdamped -> flagged for manual fitting

@dataclass(frozen=True)
class ModelResult:
    model_id: str                    # (new) unique identity: label + "#" + basin index, e.g.
                                     #   "elastic+stokes3+bg_delta2#0"
    label: str                       # catalogue label; a delta twin carries loop's "_delta<i>-<j>" suffix
    n_peaks: int
    elastic: bool
    background: bool
    delta_modes: tuple[int, ...]     # (new) 0-based indices (ascending omega0) of modes held at GAMMA_FLOOR
    ln_model_prior: float            # (new) ln of this model's prior weight within its count (see below)
    evidence_status: EvidenceStatus
    ln_z: float | None               # box-rule Laplace (C-prior) at the posterior mode; None unless ok*
    ln_like: float | None            # Poisson log-likelihood at the optimum, saturated constant included
    ln_prior: float | None           # parameter log prior at the posterior mode
    cost: float | None               # Baker-Cousins deviance at the optimum; ln_like = const - cost/2
    twin_of: str | None              # model_id of the TERMINAL model this one resolves to
    n_basins: int                    # distinct solutions summed into this model's evidence (1 per id)
    reason: str                      # "" or why no evidence / which limit made it a twin
    params: dict[str, float]         # FULL vector at the TRF optimum (where cost and ln_like are),
                                     #   CompositeModel names: phonon.omega0, phonon.Gamma, phonon.h,
                                     #   phonon2.*, elastic.h, bg.BG (fixed coordinates included)
    map_params: dict[str, float] | None   # (new) the same vector at the posterior mode, where ln_prior,
                                     #   ln_z, cov and pinned are; PeakResult values come from it
    free: tuple[str, ...]            # (new) names of the free coordinates, in `params` order
    cov: "np.ndarray | None"         # (new shape) full len(params) x len(params), params order; fixed
                                     #   coordinates' rows and columns are zero; None if unavailable
    probability: float               # posterior of this model (0 for twins and unpriced models)
    k_free: int                      # = len(free)
    pinned: tuple[str, ...]          # free coordinates at a bound at the posterior mode
    boundary_method: Literal["", "box"]
    posterior_iterations: int        # (new) optimiser iterations used; = posterior_maxiter when it hit the limit
    hypothesis: object               # (new) the catalogue Hypothesis (delta twin: with its derived context)
    fit: object | None               # (new) loop.StructureFitResult, no refit: at map_params for a priced
                                     #   model, at the TRF optimum (cov None) for an unpriced one or a twin

@dataclass(frozen=True)
class SpectrumFlag:
    kind: Literal["structured_residual", "ambiguous_decomposition"]
    e_lo: float; e_hi: float         # meV, interval shown to the user
    statistic: float | None          # residual-scan statistic or evidence gap (nats); None for a
                                     #   covariance-only ambiguity
    detail: str                      # "possible hidden component near X meV; a neighbour may be absorbing it"

@dataclass(frozen=True)
class FastPassResult:
    state: CountState
    preferred_count: int | None      # count of the displayed fit; None only when no count has evidence
    count_probabilities: dict[int, float]   # over counts with evidence; sum 1; conditional when unresolved
    probabilities_conditional: bool
    close_call_alternatives: tuple[int, ...]
    numerically_marginal: bool
    search_limit_reason: Literal["", "ceiling", "safety_budget"]
    models: tuple[ModelResult, ...]  # every model tried, ascending (n_peaks, elastic, background, delta_modes)
    selected: ModelResult | None
    peaks: tuple[PeakResult, ...]    # the selected model's, ascending omega0
    elastic_height: float | None
    background: float | None
    grouped_intensity: tuple[tuple[float, float, float, float | None], ...]   # (e_lo, e_hi, counts*meV, sigma)
    flags: tuple[SpectrumFlag, ...]
    flag_statistics: dict            # raw statistics behind every threshold: reclassify() and
                                     #   reclassify_record() input (Meaning: reclassify_record)
    prior_provenance: dict           # SpectrumPrior.provenance()
    arena_fingerprint: str           # loop.data_likelihood_fingerprint(ctx)
    ln_like_saturated: float         # (new) the arena's saturated Poisson constant,
                                     #   sum(xlogy(y, y) - y - gammaln(y + 1)): ln_like = ln_like_saturated
                                     #   - cost/2; S2 compares ln_z - ln_like_saturated with its -cost/2 scale
    settings: FastPassSettings
    timing: dict                     # "seed", "ladder", "audits", "twins", "evidence", "classify", "total" (s);
                                     # "families": {family: {"fits": int, "seconds": float}}; "seed_count": int
    n_fits: int                      # TRF calls made
    deep_pass_candidate: bool        # state is unresolved, close_call or search_limited

def run_fast_pass(ctx, *, settings: FastPassSettings | None = None, peaks=(), structures=None) -> FastPassResult:
    raise NotImplementedError("fast pass: S1U4")
def reclassify(result: FastPassResult, settings: FastPassSettings) -> FastPassResult:
    raise NotImplementedError("fast pass: S1U4")
def reclassify_record(record: dict, settings: FastPassSettings) -> dict:  # (new) on a C-fastpass-record
    raise NotImplementedError("fast pass: S1U4")

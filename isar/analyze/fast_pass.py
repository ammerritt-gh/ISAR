"""Fast-pass engine: one spectrum's peak count, parameters and evidence (design sec. 2.2-2.3).

A cheap pseudo-Voigt seed gives starting points only; the full stage then fits every count from 0
to the mode ceiling in every elastic/background variant (bounded TRF on signed Poisson-deviance
residuals), audits the contending counts, resolves degenerate and delta-mode twins recursively,
and prices each distinct solution by the box-rule Laplace evidence at its posterior mode
(:func:`isar.analyze.prior.laplace_log_evidence`). Every count carries equal prior weight, split
equally over its V(k) variants and its 2^k delta/width masks. No nested sampling, no bootstrap and
no second pass run here.

On BL43LXU every starting variant is elastic + central QE (:class:`~isar.analyze.components.CentralQE`)
+ k phonons, with and without a flat background (V(k) = 2). The models without the elastic line or
without the QE are built only on demand, when a twin screen names them; each starting model and
the reduced models built from it are one group, which prices exactly one label (:func:`_superseded`).

Qt-free (ISAR hard rule 6). Units: meV; heights in counts per point at ``ctx.exposure_reference``;
areas in counts x meV; evidence in nats; ``time.perf_counter`` seconds.
"""
from __future__ import annotations

import copy
import itertools
import logging
import math
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from typing import Literal

import numpy as np
from scipy.optimize import least_squares, linear_sum_assignment, minimize
from scipy.special import gammaln, logsumexp, xlogy

from isar.analyze import backends, catalogue, loop, prior
from isar.analyze.components import (QE_WEIGHT_CONVENTION, CentralQE, Elastic, PhononDHO, QuasiElastic,
                                     central_qe_columns, phonon_unit_columns)
from isar.analyze.estimators import FMIN
from isar.analyze.lineshapes import KB, voigt_area_for_height
from isar.analyze.phonon import GAMMA_FLOOR, _peak_omega0, central_qe_admissible
from isar.analyze.progress import ProgressFn, emitter

log = logging.getLogger(__name__)

# Algorithm settings: agent defaults, uncalibrated; changing one changes cost, not a connection.
SEED_STEP_FWHM = 0.4          # seed candidate grid step (x FWHM)
SEED_GAMMA = 0.1              # meV, a seed candidate's width
SEED_EXCLUDE_FWHM = 0.6       # no seed candidate this close (x FWHM) to a held seed peak
MIN_ADD_ENERGY = 0.5          # meV, lowest energy a seed or ladder addition is placed at
LADDER_GAMMA = 0.3            # meV, a ladder addition's width
LADDER_EXCLUDE_FWHM = 0.4     # a ladder addition keeps this far (x FWHM) from the parent's modes
LADDER_SPLIT_FWHM = 0.125     # a ladder split puts its parts this far (x FWHM) either side
LADDER_SPLIT_HEIGHTS = (0.6, 0.4)
HINT_EXCLUDE_FWHM = 0.5       # a card peak hint this close (x FWHM) to a seed peak adds nothing
HINT_GAMMA_FWHM = 0.3         # a hint's starting width (x FWHM)
RESTART_SPREAD = (0.3, 0.7, 0.3)   # restart jitter: omega0 sd (x FWHM), ln Gamma sd, h +- fraction
BROAD_STEP_MEV = 3.0
BROAD_WIDTHS_MEV = (1.5, 4.0)  # the lone width cap is 4 meV: a wider start would only be clipped to it
WIDTH_STEP_FLOOR_MEV = 0.2    # Laplace's step floor for a width (meV): the 1e-2 x 20 meV it had before recipe /7
BROAD_HEIGHT_FRAC = 0.04     # a broad addition's height (x height ceiling)
MOVE_FWHM = 0.5
WIDTH_FACTORS = (1 / 3, 3)
SHOULDER_FWHM = 1.5
SHOULDER_FRACTION = 0.2
SPLIT_FRACTIONS = (0.75, 0.25)     # an audit split's lower part carries this share; both orientations
NEAR_ZERO_SIGMAS = 4.5
NEAR_ZERO_MAX_MODES = 3
COINCIDENT_DELTA_MEV = 1e-6   # two delta modes closer than this are one mode, merged first
QE_START_WIDTHS_FWHM = (1.0, 1.5, 3.0)   # central QE starting widths (x FWHM), inside its support; starts only
CACHE_SIZE = 4096             # unit columns kept, keyed by (omega0, Gamma, peak selector)
# The red-first switch of the delta-mode twin rule: a module flag the acceptance worker may set;
# never a setting, never read from the environment.
DELTA_TWIN_RULE = True

# Increment for result-changing repairs; saved fit signatures must not carry older answers.
IMPLEMENTATION_VERSION = 3

STAGES = ("seed", "ladder", "audits", "twins", "evidence", "classify")
_PRICED = ("ok", "ok_boundary")

@dataclass(frozen=True)
class FastPassSettings:
    mode_ceiling: int = 6                    # pinned known limit (operator); card [fitting] max_modes
    contending_margin: float = 10.0          # nats: a count within this of the best evidence is contending
    close_call_p: float = 0.80               # preferred count below this -> close call (operator)
    seed_stop_gain: float = 6.0              # PROVISIONAL, NOT calibrated (refit grid skipped, run-once ruling); operator's call: 2*dlnL a seed peak must add
    confident_anchor: tuple[tuple[float, float], ...] = ((0.0, 50.0),)
                                             # PROVISIONAL; operator's call 2026-10-26 ("50 is fine"): (spectrum max count,
                                             # deletion-impact anchor) pairs, interpolated log-linearly in the
                                             # count, clamped at the ends; one pair = a constant
    flag_residual_threshold: float = 10.05   # PROVISIONAL, calibrated 2026-10-26 on development seed 20261023, 63 clean spectra; operator's call: residual-scan statistic at/above -> flag (0.0 = that flag kind off)
    overdamped_correlation: float = 0.8      # PROVISIONAL, calibrated 2026-10-26 on development seed 20261023, 150 spectra; operator's call: |corr(omega0, Gamma)| above -> overdamped warning
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
    at_width_cap: bool = False       # (recipe /7) Gamma sits at its mode's ceiling: the mode "wants to be
                                     #   broader than a lone spectrum may claim"; absent in older records

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
    qe: bool = False                 # the model carries a CentralQE (BL43LXU)
    on_demand: bool = False          # a reduced variant the twin rule built (never a starting candidate)
    removed: tuple[str, ...] = ()    # prefixes it lacks against its starting variant ("elastic", "qe")
    superseded: bool = False         # fitted, never priced: another label of its group was priced
    parent: str = ""                 # the group key: its starting model's label (its own for a starting
                                     #   model; the delta suffix included)

@dataclass(frozen=True)
class QEResult:
    """The selected model's central QE (BL43LXU), at its posterior mode."""
    height: float                 # qe.h at the posterior mode (counts per point, reference exposure)
    width: float                  # intrinsic FWHM, meV
    pinned: bool                  # width from the card, not fitted
    weight: float                 # counts x meV: CentralQE.spectral_weight at the posterior mode
    window: tuple[float, float]   # meV, the energy window the weight integrates over
    convention: str               # components.QE_WEIGHT_CONVENTION
    sigma_height: float | None    # posterior-mode covariance sd; None when unavailable
    sigma_width: float | None     # None when pinned or unavailable

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
    search_limit_reason: Literal["", "ceiling"]      # records before 2026-10-27 may say "safety_budget"
    models: tuple[ModelResult, ...]  # every model tried, ascending (n_peaks, elastic, background, qe,
                                     #   delta_modes)
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
    qe: QEResult | None = None       # the selected model's central QE; None when it has none


def poisson_residuals(y, mu) -> np.ndarray:
    """Signed Baker-Cousins deviance residuals: their sum of squares is
    ``estimators.baker_cousins_cost`` (the mean clipped at its FMIN). No floor on the counts (D3)."""
    mu = np.maximum(np.asarray(mu, dtype=float), FMIN)
    out = np.sqrt(2.0 * mu)
    pos = y > 0
    yp, mp = y[pos], mu[pos]
    ratio = (mp - yp) / yp
    term = ratio - np.log1p(ratio)
    small = np.abs(ratio) < 1e-4            # the series is exact where the difference cancels
    r = ratio[small]
    term[small] = r * r * (0.5 - r / 3 + r * r / 4 - r * r * r / 5)
    out[pos] = np.sign(mp - yp) * np.sqrt(2.0 * yp * np.maximum(term, 0.0))
    return out


def _gradient_of(fg):
    """The gradient alone of a joint evaluation ``fg(z) = (f, df/dz)``, NaN where f is not finite, so a Hessian
    stencil that leaves the support reads "Hessian not finite" as the value's own stencil does."""
    def grad(z):
        v, g = fg(z)
        return g if math.isfinite(v) else np.full(np.size(z), math.nan)
    return grad


def _ln_model_prior(n_variants: int, k: int) -> float:
    """ln of one (variant, delta mask) combination's weight within count k: 1/(V(k) 2^k)."""
    return -math.log(n_variants) - k * math.log(2.0)


@dataclass(eq=False)
class _Variant:
    """One catalogue structure in one elastic/background variant (no delta mask)."""
    k: int
    hyp: object
    elastic: bool
    background: bool
    peaks: tuple                      # each mode's PhononDHO peak selector, from the catalogue
    qe: bool = False                  # carries a CentralQE
    on_demand: bool = False           # a reduced variant built by the twin rule
    start: "_Variant | None" = None   # the starting variant an on-demand one was reduced from
    removed: tuple = ()               # the prefixes it lacks against ``start``

    @property
    def label(self) -> str:
        return str(self.hyp.label)

    @property
    def origin(self) -> "_Variant":
        """The starting variant: itself, or the one an on-demand variant was built from."""
        return self.start or self


@dataclass(eq=False)
class _Model:
    """One model identity: a variant and a delta mask (0-based positions in ascending omega0)."""
    variant: _Variant
    delta: tuple
    hyp: object                       # the variant's hypothesis, or its delta twin's
    cctx: object                      # loop.candidate_context(hyp, ctx)
    names: list
    lo: np.ndarray
    hi: np.ndarray
    fixed_pos: list
    fixed_vals: list
    free: np.ndarray
    soft: list                        # (position, mu, sigma) of the soft-constrained coordinates
    modes: list                       # (i_omega0, i_Gamma, i_h) per mode, component order
    el: int | None
    bg: int | None
    omit: frozenset                   # positions with no recipe factor: pinned or soft-constrained
    soft_priors: list                 # (position, ResolvedPrior) truncated Gaussian of each soft coordinate
    zmap: "_ZMap | None" = None       # the optimiser coordinate of the widths (built by _Engine.zmap)
    groups: list = field(default_factory=list)   # energy positions of each group of exchangeable modes
    basins: list = field(default_factory=list)
    qe_h: int | None = None           # position of qe.h (None: no central QE)
    qe_w: int | None = None           # position of qe.width (None: no QE, or its width pinned)
    qe_pin: float | None = None       # the card-pinned QE width, meV

    def qe_width(self, theta) -> float:
        return self.qe_pin if self.qe_w is None else float(theta[self.qe_w])

    @property
    def parent(self) -> str:
        """The group key: the starting variant's label with this model's delta suffix."""
        v = self.variant
        return v.origin.label + str(self.hyp.label)[len(v.label):]


@dataclass(frozen=True)
class _Post:
    """The posterior optimiser's outcome, the fields of scipy's OptimizeResult ``price`` reads; ``x`` is the
    stopping point on the model's own scale, not yet clipped to the box."""
    x: np.ndarray
    nit: int
    status: int
    success: bool
    message: str


@dataclass(eq=False)
class _Basin:
    """One distinct solution of one model: its TRF optimum, twin link and evidence."""
    model: _Model
    index: int
    theta: np.ndarray | None          # TRF optimum, full vector (None: not fitted)
    cost: float | None
    ln_like: float | None
    ln_model_prior: float
    family: str
    nfev: int = 0
    message: str = ""
    status: str = "pending"
    reason: str = ""
    twin: "_Basin | None" = None
    limit: str = ""
    theta_map: np.ndarray | None = None
    ln_z: float | None = None
    ln_prior: float | None = None
    cov_free: np.ndarray | None = None
    pinned: tuple = ()
    boundary_method: str = ""
    posterior_iterations: int = 0
    objective: object = None          # the posterior objective on the free coordinates
    spread: float | None = None

    @property
    def count(self) -> int:
        return self.model.variant.k

    @property
    def k_free(self) -> int:
        return int(self.model.free.size)

    @property
    def model_id(self) -> str:
        return f"{self.model.hyp.label}#{self.index}"

    @property
    def label(self) -> str:
        return str(self.model.hyp.label)

    @property
    def parent(self) -> str:
        return self.model.parent

    def terminal(self) -> "_Basin":
        b = self
        while b.status == "degenerate_twin" and b.twin is not None:
            b = b.twin
        return b


def _merge_pair(a, b, delta: bool):
    """One mode from two: summed height, height-weighted omega0 and Gamma (GAMMA_FLOOR if delta)."""
    w = a[2] + b[2]
    fa = a[2] / w if w > 0 else 0.5
    e = fa * a[0] + (1.0 - fa) * b[0]
    g = GAMMA_FLOOR if delta else fa * a[1] + (1.0 - fa) * b[1]
    return (e, g, w, delta)


def _merge_chain(modes, distance: float) -> list:
    """The prototype's all-pairs merge: ascending omega0, each mode merged into the previous merged
    one when closer than ``distance``; a merged mode is delta only when both parts are."""
    out = []
    for m in sorted(modes, key=lambda m: m[0]):
        if out and abs(m[0] - out[-1][0]) < distance:
            out[-1] = _merge_pair(out[-1], m, out[-1][3] and m[3])
        else:
            out.append(tuple(m))
    return out


@dataclass
class _Tally:
    count_ln_z: dict
    probabilities: dict
    preferred: int | None
    selected: object | None
    terms: dict                       # id(entry) -> ln_model_prior + ln_z
    total: float | None
    priced_contenders: list
    unpriced_contenders: list
    dismissed_counts: dict             # count -> best-found-fit dismissal and its comparison


def _superseded(entries) -> dict:
    """One result per group (decision 6): ``{id(entry): kept label}`` for every entry of a group's
    other labels. ``entries`` carry ``parent`` (the group key), ``label``, ``status`` and ``ln_z``.
    Of a group's labels with a priced basin, the one with the largest ln Z (its basins summed) is
    kept; every entry of every other label of that group is superseded. A group with no priced label
    supersedes nothing."""
    groups = {}
    for e in entries:
        groups.setdefault(e.parent, {}).setdefault(e.label, []).append(e)
    out = {}
    for labels in groups.values():
        priced = {}
        for lab, es in labels.items():
            z = [e.ln_z for e in es if e.status in _PRICED and e.ln_z is not None]
            if z:
                priced[lab] = float(logsumexp(z))
        if not priced:
            continue
        kept = max(priced, key=lambda lab: (priced[lab], lab))
        out.update({id(e): kept for lab, es in labels.items() if lab != kept for e in es})
    return out


def _tally(entries, contending_margin: float, superseded=()) -> _Tally:
    """Step 9: count evidence, probabilities, the selected model and the contending counts.

    ``entries`` carry ``count``, ``status``, ``ln_z``, ``ln_model_prior``, ``ln_like`` and
    ``k_free``. Z_k sums exp(ln_model_prior + ln_z) over the count's priced entries; a count with
    none has no evidence. A wholly unpriced count contends unless its best found non-twin
    ln L + ln model prior falls below the leading count's ln Z minus the margin (operator Q1,
    2026-10-28). This is a local-fit heuristic, not a guaranteed evidence bound. Both ln L and
    ln Z include the same saturated-likelihood constant. Entirely unfitted counts contend;
    twins alone do not add a competing count. Any priced basin still supplies its count's value.
    ``superseded`` (ids, :func:`_superseded`) are skipped entirely: never priced, never live."""
    terms, by_count = {}, {}
    for e in entries:
        if id(e) in superseded:
            continue
        by_count.setdefault(e.count, []).append(e)
        if e.status in _PRICED and e.ln_z is not None:
            terms[id(e)] = e.ln_model_prior + e.ln_z
    lnz = {}
    for k, es in sorted(by_count.items()):
        t = [terms[id(e)] for e in es if id(e) in terms]
        if t:
            lnz[k] = float(logsumexp(t))
    probs, preferred, selected, total = {}, None, None, None
    if lnz:
        total = float(logsumexp(list(lnz.values())))
        probs = {k: float(np.exp(v - total)) for k, v in lnz.items()}
        preferred = min(probs, key=lambda k: (-probs[k], k))
        selected = min((e for e in by_count[preferred] if id(e) in terms),
                       key=lambda e: (-terms[id(e)], e.k_free))
    top = max(lnz.values()) if lnz else None
    priced = sorted(k for k, v in lnz.items() if v >= top - contending_margin)
    unpriced, dismissed = [], {}
    for k, es in sorted(by_count.items()):
        if k in lnz:                  # a priced basin gives the count its evidence
            continue
        live = [e for e in es if e.status not in ("degenerate_twin", "not_fitted")
                and e.ln_like is not None]
        if not live:
            if any(e.status != "degenerate_twin" for e in es):
                unpriced.append(k)
            continue
        best = max(live, key=lambda e: e.ln_like + e.ln_model_prior)
        best_weight = best.ln_like + best.ln_model_prior
        if top is not None and best_weight < top - contending_margin:
            dismissed[k] = {"reason": "dismissed on its best found fit",
                            "best_found_ln_weight": float(best_weight),
                            "leading_count_ln_z": float(top),
                            "contending_margin": float(contending_margin)}
        else:
            unpriced.append(k)
    return _Tally(lnz, probs, preferred, selected, terms, total, priced, unpriced, dismissed)


def _numerically_marginal(count_ln_z: dict, preferred, spread: float, close_call_p: float) -> bool:
    """Whether shifting every count's ln Z by the evidence's numerical spread, in the worst
    direction, changes the preferred count or moves its probability across ``close_call_p``."""
    if preferred is None or not spread > 0 or len(count_ln_z) < 2:
        return False

    def shifted(sign):
        z = {k: v + (sign * spread if k == preferred else -sign * spread)
             for k, v in count_ln_z.items()}
        total = logsumexp(list(z.values()))
        best = max(z, key=lambda k: (z[k], -k))
        return float(np.exp(z[preferred] - total)), best

    p_low, best_low = shifted(-1.0)
    p_high, _ = shifted(+1.0)
    return best_low != preferred or (p_low < close_call_p) != (p_high < close_call_p)

# ---- classification: the post-search thresholds, on plain quantities ----------------------------------
# One function serves the engine, reclassify (a FastPassResult) and reclassify_record (a saved
# C-fastpass-record), so the three cannot drift. It reads only what flag_statistics, the peaks, the
# count probabilities and the settings hold: never a covariance, a model or the data.
RECLASSIFIABLE = ("confident_anchor", "flag_residual_threshold", "overdamped_correlation",
                  "close_call_p", "material_p")
_MISSING = object()


def _anchor_at(pairs, max_count: float) -> float:
    """The deletion-impact anchor at the spectrum's max count: log-linear between the
    (count, anchor) pairs, clamped at the ends; one pair is a constant."""
    pts = sorted((float(c), float(a)) for c, a in pairs)
    if not pts:
        raise ValueError("fast pass: confident_anchor needs at least one (count, anchor) pair")
    if len(pts) == 1:
        return pts[0][1]
    return float(np.interp(math.log(max(float(max_count), 1.0)),
                           [math.log(max(c, 1.0)) for c, _ in pts], [a for _, a in pts]))


def _matched(selected, other, fwhm: float) -> np.ndarray:
    """Which ``selected`` energies get a one-to-one match within one FWHM in ``other``; the
    assignment maximises the number of matches before it minimises distance."""
    matched = np.zeros(len(selected), bool)
    if len(selected) and len(other):
        d = np.abs(np.subtract.outer(np.asarray(selected, float), np.asarray(other, float))) / fwhm
        cost = np.where(d <= 1.0, d, len(selected) + len(other) + 1.0)
        rows, cols = linear_sum_assignment(cost)
        matched[rows[d[rows, cols] <= 1.0]] = True
    return matched


def _groups(n: int, edges) -> list:
    """Connected groups (size >= 2) of the peaks 0..n-1 joined by ``edges``, each ascending."""
    groups = [{i} for i in range(n)]
    for a, b in edges:
        ga, gb = next(g for g in groups if a in g), next(g for g in groups if b in g)
        if ga is not gb:
            ga.update(gb)
            groups.remove(gb)
    return [sorted(g) for g in groups if len(g) > 1]


def _classify(s: FastPassSettings, peaks: list, fs: dict, probabilities: dict, preferred,
              search_limit_reason: str) -> dict:
    """Step 10's thresholds. ``peaks``: PeakResult-shaped dicts (ascending omega0) whose ``status``
    and ``area_status`` carry the covariance-only part; ``fs``: flag_statistics, JSON-shaped
    (``count_ln_z`` keys may be strings); ``probabilities``: count -> P. Returns the recomputed
    state, close-call alternatives, numerical marginality, flags, grouped intensity and the
    per-peak ``confident``, ``regime``, ``manual_fit``, ``status`` and ``area_status``."""
    fwhm, n = float(fs["fwhm"]), len(peaks)
    fp, e0 = fs["peaks"], [float(p["omega0"]) for p in fs["peaks"]]
    probs = {int(k): float(v) for k, v in probabilities.items()}
    height_pairs = [(int(i), int(j), float(c)) for i, j, c in fs["height_correlations"]]
    corr_edges = [(i, j) for i, j, c in height_pairs if abs(c) > s.overdamped_correlation]
    conditional = {i for e in corr_edges for i in e}

    # -- flags: (lo, hi, statistic, kind)
    found = []
    if s.flag_residual_threshold > 0:
        merged = []
        for lo, hi, stat in fs["residual_scan"]:
            if stat < s.flag_residual_threshold:
                continue
            if merged and lo <= merged[-1][1]:
                merged[-1][1], merged[-1][2] = max(merged[-1][1], hi), max(merged[-1][2], stat)
            else:
                merged.append([lo, hi, stat])
        found += [(lo, hi, stat, "structured_residual") for lo, hi, stat in merged]
    ambiguous = []                       # (lo, hi, evidence gap | None)
    for g in _groups(n, corr_edges):     # correlated heights: the assignment of intensity is ambiguous
        ambiguous.append((max(0.0, e0[g[0]] - fwhm / 2), e0[g[-1]] + fwhm / 2, None))
    material = [a for a in fs["alternatives"]
                if a["evidence_gap"] is not None and a["probability"] >= s.material_p]
    for a in material:                   # a forced broad or shoulder fit about as good, elsewhere
        if a["family"] in ("broad", "shoulder"):
            for c in a["omega0"]:        # within one FWHM of a selected peak is a split, not a new place
                if all(abs(c - e) > fwhm for e in e0):
                    ambiguous.append((max(0.0, c - fwhm / 2), c + fwhm / 2, float(a["evidence_gap"])))
    for lo, hi in sorted({(lo, hi) for lo, hi, _ in ambiguous}):
        gaps = [g for a, b, g in ambiguous if (a, b) == (lo, hi) and g is not None]
        found.append((lo, hi, min(gaps) if gaps else None, "ambiguous_decomposition"))
    flags = [{"kind": kind, "e_lo": float(lo), "e_hi": float(hi),
              "statistic": None if stat is None else float(stat),
              "detail": ("possible hidden component near %.1f meV; a neighbour may be absorbing it (%s)"
                         % ((lo + hi) / 2, kind.replace("_", " ")))}
             for lo, hi, stat, kind in found]

    # -- peaks
    anchor = _anchor_at(s.confident_anchor, fs["max_count"])
    credible = [a for a in fs["alternatives"]
                if a["evidence_gap"] is None or a["probability"] >= s.material_p]
    alive = np.ones(n, bool)
    for a in credible:                   # every credible alternative must hold a matching peak
        alive &= _matched(e0, a["omega0"], fwhm)
    updates = []
    for i, (p, f) in enumerate(zip(peaks, fp)):
        corr = f["corr_omega0_gamma"]
        if f["omega0"] <= s.near_elastic_fwhm * fwhm:
            regime = "near_elastic"
        elif f["Gamma"] >= f["omega0"] or (corr is not None and abs(corr) >= s.overdamped_correlation):
            regime = "overdamped"
        else:
            regime = "underdamped"
        status = dict(p["status"])
        for key in ("height", "area"):
            if status[key] in ("constrained", "model_dependent"):
                status[key] = "model_dependent" if i in conditional else "constrained"
        area_status = p["area_status"]
        if area_status in ("usable", "decomposition_conditional"):
            area_status = "decomposition_conditional" if i in conditional else "usable"
        named = any(lo - fwhm <= f["omega0"] <= hi + fwhm for lo, hi, _, _ in found)
        updates.append({
            "confident": bool(p["height"] > 0 and f["deletion_impact"] >= anchor
                              and alive[i] and not named),
            "regime": regime, "manual_fit": regime != "underdamped",
            "status": status, "area_status": area_status})

    # -- grouped intensity: peaks within one FWHM of a neighbour, or a correlated pair; a peak whose
    #    height trades with the elastic line's joins it (node n), the group then running from zero
    near_edges = [(i, j) for i in range(n) for j in range(i + 1, n) if abs(e0[i] - e0[j]) <= fwhm]
    area_cov, el_cov = fs.get("area_covariance"), fs.get("elastic_area_covariance")
    el_corr = fs.get("elastic_height_correlations") or []     # absent from older records: no group
    el_edges = [(i, n) for i, c in enumerate(el_corr)
                if c is not None and abs(c) > s.overdamped_correlation
                and fs.get("elastic_area") is not None and el_cov is not None]
    grouped = []
    for g in _groups(n + 1, near_edges + corr_edges + el_edges):
        with_el = n in g
        g = [i for i in g if i < n]
        sigma = None
        if area_cov is not None:
            var = sum(area_cov[i][j] for i in g for j in g)
            if with_el:                  # Var(A_el + sum A_i) = Var(A_el) + 2 sum Cov(A_el, A_i) + ...
                var += el_cov[n] + 2.0 * sum(el_cov[i] for i in g)
            sigma = float(math.sqrt(max(var, 0.0)))
        grouped.append((0.0 if with_el else max(0.0, e0[g[0]] - fwhm / 2), e0[g[-1]] + fwhm / 2,
                        float(sum(peaks[i]["area_stokes"] for i in g))
                        + (float(fs["elastic_area"]) if with_el else 0.0), sigma))

    # -- the count state
    if preferred is None or fs["unpriced_contenders"]:
        state = "unresolved"
    elif search_limit_reason:
        state = "search_limited"
    elif probs[preferred] < s.close_call_p:
        state = "close_call"
    else:
        state = "resolved"
    count_ln_z = {int(k): float(v) for k, v in fs["count_ln_z"].items()}
    return {
        "state": state, "probabilities_conditional": state == "unresolved",
        "deep_pass_candidate": state != "resolved", "anchor": anchor,
        "close_call_alternatives": tuple(k for k in sorted(probs)
                                         if k != preferred and probs[k] >= s.material_p),
        "numerically_marginal": _numerically_marginal(count_ln_z, preferred,
                                                      fs["evidence_spread_nats"], s.close_call_p),
        "flags": flags, "grouped_intensity": grouped, "peaks": updates}


def _plain(v):
    """Tuples as lists, recursively: a settings dict compares equal across a JSON round trip."""
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    return v


def _settings_dict(settings: FastPassSettings) -> dict:
    return _plain(asdict(settings))


def _refuse_changed_search(old: dict, new: dict) -> None:
    """ValueError naming the first settings field outside RECLASSIFIABLE that differs."""
    for name, value in new.items():
        if name not in RECLASSIFIABLE and old.get(name, _MISSING) != value:
            raise ValueError(f"fast pass: {name} changed since the search; only "
                             f"{', '.join(RECLASSIFIABLE)} can be reclassified (rerun the fast pass)")


class _ZMap:
    """The optimiser coordinate of one model's widths (recipe /7). Each mapped mode's Gamma is fitted as
    ``t`` in [0, 1] on ``[g_lo, ceiling(omega0)]``; ``theta`` stays in meV everywhere else. The z-space
    log density is ``ln_prior(theta) + ln_jac(theta)``, so the objective is ``F_theta - ln_jac``.
    Mapped: every free, non-delta Gamma (default-prior and soft-constrained alike). ``active`` False:
    the identity (legacy_v4). Kink at omega0 = 4: right derivative."""

    def __init__(self, engine, model):
        self.free = model.free
        self.active = engine._conditional_widths()
        self.lo, self.hi = model.lo[self.free].astype(float), model.hi[self.free].astype(float)
        free_set = {int(i): n for n, i in enumerate(self.free)}
        self.mapped = []            # (mode m, pos_a, pos_g, lifted, g_lo)
        if self.active:
            for m, (a, g, _c) in enumerate(model.modes):
                if g in free_set and m not in model.delta:
                    self.mapped.append((m, a, g, bool(engine.lifted_of(model, m)), float(model.lo[g])))
                    self.lo[free_set[g]], self.hi[free_set[g]] = 0.0, 1.0
        self._k = {m: k for k, (m, *_rest) in enumerate(self.mapped)}
        self.gmap = prior.GammaMap([(a, g, lo, lf) for _m, a, g, lf, lo in self.mapped], engine.prior_s)

    def mapped_mode(self, m: int) -> bool:
        return m in self._k

    def ceiling(self, theta, m) -> float:
        return self.gmap.ceiling(theta, self._k[m])

    def clip(self, theta) -> np.ndarray:
        """``theta`` with each mapped Gamma pulled under its mode's ceiling (a start never outside it)."""
        for k, (_m, _a, g, _lf, _lo) in enumerate(self.mapped):
            if np.isfinite(theta[g]):
                theta[g] = min(theta[g], self.gmap.ceiling(theta, k))
        return theta

    def to_z(self, theta) -> np.ndarray:
        return self.gmap.to_z(theta)[self.free] if self.mapped else np.asarray(theta, float)[self.free]

    def to_theta(self, z, template) -> np.ndarray:
        t = np.array(template, dtype=float)
        t[self.free] = z
        return self.gmap.to_theta(t) if self.mapped else t

    def ln_jac(self, theta) -> float:
        return self.gmap.ln_jac(theta) if self.mapped else 0.0

    def grad_to_z(self, theta, dF_dtheta) -> np.ndarray:
        """Gradient over z (free order) of ``F_z = F_theta - ln_jac`` from ``dF_dtheta`` (full vector)."""
        dF = np.asarray(dF_dtheta, dtype=float)
        if not self.mapped:
            return dF[self.free].copy()
        return (self.gmap.jac(theta).T @ dF - self.gmap.d_ln_jac(theta))[self.free]

    def cov_to_theta(self, theta, cov_z) -> np.ndarray:
        if not self.mapped:
            return np.asarray(cov_z, dtype=float)
        J = self.gmap.jac(theta)[np.ix_(self.free, self.free)]
        return J @ np.asarray(cov_z, dtype=float) @ J.T

    def step_floor(self, theta) -> np.ndarray:
        """Laplace's absolute step floor per free coordinate (NaN: its default). A mapped width's is the
        meV scale it had before the coordinate changed, ``WIDTH_STEP_FLOOR_MEV``, in ``t`` units."""
        out = np.full(self.free.size, np.nan)
        where = {int(i): n for n, i in enumerate(self.free)}
        for k, (_m, _a, g, _lf, g_lo) in enumerate(self.mapped):
            out[where[g]] = WIDTH_STEP_FLOOR_MEV / (self.gmap.ceiling(theta, k) - g_lo)
        return out

    def at_cap(self, theta, m) -> bool:
        """The mode's Gamma sits at its ceiling (t >= 1 - 1e-6, the pin tolerance)."""
        if m not in self._k:
            return False
        _m, _a, g, _lf, g_lo = self.mapped[self._k[m]]
        return prior.t_from_gamma(float(theta[g]), g_lo, self.ceiling(theta, m)) >= 1.0 - 1e-6


class _Engine:
    """One spectrum's fast pass. ``run`` searches; ``conclude`` tallies and reports, from any state
    (so a stopped search still reports what it priced)."""

    def __init__(self, ctx, settings: FastPassSettings, peaks=(), structures=None,
                 progress: ProgressFn | None = None):
        self.ctx, self.s = ctx, settings
        self.emit = emitter(progress, "fast")
        self.prior_s = settings.prior or prior.PriorSettings()
        x = np.asarray(ctx.x_used, dtype=float)
        y = np.asarray(ctx.counts_used, dtype=float)
        if x.size == 0 or not np.all(np.isfinite(y)) or np.any(y < 0):
            raise ValueError("fast pass: the kept points need finite, non-negative counts")
        self.sp = prior.resolve_prior_for(ctx, self.prior_s)   # ValueError: no counts / energy
        self.fwhm = prior.resolution_fwhm(ctx)                  # ValueError: no resolution
        self.x, self.y = x, y
        self.E = x - float(ctx.center)
        expo = ctx.exposure_for(x)
        self.expo = np.ones_like(x) if expo is None else np.asarray(expo, dtype=float)
        self.el_col = np.asarray(Elastic().unit_columns(x, {}, ctx)["h"], dtype=float)
        self.sat = float(np.sum(xlogy(y, y) - y - gammaln(y + 1.0)))
        T = getattr(ctx, "T", None)
        self.kT = KB * float(T) if T is not None and np.isfinite(T) and T > 0 else None
        self.g_lo, self.g_hi = prior.mode_bounds(["phonon.Gamma"], ctx, self.prior_s)[0]
        self.hints = [w for w in (_peak_omega0(p) for p in (peaks or ())) if w is not None and w > 0]
        self.variants, self._by_flags = {}, {}
        for n in range(settings.mode_ceiling + 1):
            found = []
            for s in catalogue.enumerate_count_structures(ctx, n):
                if any(c is QuasiElastic or isinstance(c, QuasiElastic) for c in s.components):
                    continue                     # design sec. 5: IN8's quasi-elastic stays out (R13)
                for hyp in (s, loop.without_background(s)):
                    if hyp is None or (structures is not None and hyp.label not in structures
                                       and s.label not in structures):
                        continue
                    found.append(self._new_variant(n, hyp))
            self.variants[n] = found
            for v in found:
                self._by_flags.setdefault((n, v.elastic, v.background, v.qe), v)
        self._reduced = {}                       # on-demand variants, never in self.variants
        starting = [v for vs in self.variants.values() for v in vs]
        self.seed_elastic = any(v.elastic for v in starting)
        self.seed_qe = any(v.qe for v in starting)
        self.qe_start = None                     # meV, chosen before the seed (_choose_qe_start)
        self.qe_start_widths = []                # the free-width starting widths it chose from
        self.models = {}
        self.cache = OrderedDict()
        self.timing = {name: 0.0 for name in STAGES}
        self.families = {}
        self._stack = []
        self.n_fits = 0
        self.posterior_jac = None                # the posterior's finite-difference jac when it has no gradient (scipy's default)
        self.seed_count = None
        self.reached = set()
        self.rng = np.random.default_rng(0)
        self.start = time.perf_counter()

    # ---- bookkeeping ---------------------------------------------------------------------------
    @contextmanager
    def _stage(self, name):
        """Exclusive stage time: a nested stage's time is taken out of the stage around it."""
        t0 = time.perf_counter()
        self._stack.append(0.0)
        try:
            yield
        finally:
            elapsed = time.perf_counter() - t0
            self.timing[name] += elapsed - self._stack.pop()
            if self._stack:
                self._stack[-1] += elapsed

    @staticmethod
    def _new_variant(k, hyp, **kw):
        comps = hyp.build().components
        return _Variant(k, hyp, elastic=any(c.prefix == "elastic" for c in comps),
                        background=any(c.prefix == "bg" for c in comps),
                        peaks=tuple(c.peak for c in comps if isinstance(c, PhononDHO)),
                        qe=any(isinstance(c, CentralQE) for c in comps), **kw)

    def variant(self, k, elastic, background, qe=False):
        """The variant at count ``k`` with those flags, or None (``qe`` defaults to the off-BL43LXU
        value; every engine call passes the parent's explicitly). A starting variant when there is
        one; else (only where the starting variants carry the QE) the reduced variant built from
        the starting variant of the same count and background, without the elastic line and/or the
        QE, built once and cached outside ``self.variants``."""
        key = (k, bool(elastic), bool(background), bool(qe))
        if key in self._by_flags:
            return self._by_flags[key]
        if not self.seed_qe:
            return None
        if key not in self._reduced:
            self._reduced[key] = self._reduce(*key)
        return self._reduced[key]

    def _reduce(self, k, elastic, background, qe):
        start = next((v for v in self.variants.get(k, ()) if v.background == background and v.qe), None)
        if start is None or (elastic and not start.elastic):
            return None
        removed = tuple(p for p, drop in (("elastic", start.elastic and not elastic), ("qe", not qe))
                        if drop)
        hyp = start.hyp
        for prefix in removed:
            hyp = loop.without_component(hyp, prefix)
            if hyp is None:
                return None
        return self._new_variant(k, hyp, on_demand=True, start=start, removed=removed)

    def basins(self):
        return [b for m in list(self.models.values()) if m is not None for b in m.basins]

    # ---- the width ceiling (recipe /7) -------------------------------------------------------------
    def lifted_of(self, model, m: int) -> bool:
        """Whether mode ``m`` of ``model`` has the lifted width ceiling (a coupled engine's hook)."""
        return False

    def zmap(self, model) -> _ZMap:
        if model.zmap is None:
            model.zmap = _ZMap(self, model)
        return model.zmap

    def _conditional_widths(self) -> bool:
        # legacy_v4's widths are flat on (GAMMA_FLOOR, GAMMA_MAX_V4), not conditional on omega0
        return not self.prior_s.legacy_v4

    def _ln_width(self, model, theta, m: int) -> float:
        """The width factor of mode ``m``: conditional on its energy's ceiling (recipe /7)."""
        a, g, _c = model.modes[m]
        if not self._conditional_widths():
            return self.sp.ln_width(theta[g])
        return self.sp.ln_width_at(theta[g], theta[a], lifted=self.lifted_of(model, m))

    def _ln_soft(self, model, theta) -> float:
        """The soft-constrained coordinates' truncated Gaussians; a soft width is cut at its mode's ceiling
        (the same measure as the default width prior)."""
        gamma_of = {g: m for m, (_a, g, _c) in enumerate(model.modes)}
        lp = 0.0
        for i, p in model.soft_priors:
            m = gamma_of.get(i)
            if m is not None and self._conditional_widths() and m not in model.delta:
                lp += p.logpdf_given(float(theta[i]), self.zmap(model).ceiling(theta, m))
            else:
                lp += p.logpdf(float(theta[i]))
        return lp

    # ---- the model mean --------------------------------------------------------------------------
    def units(self, modes, peaks):
        """(unit column, UnitPeak) per (omega0, Gamma) mode; the uncached ones from ONE batched
        convolution, so a finite difference that moves one shape costs one convolution."""
        keys = [(float(e), float(g), p) for (e, g), p in zip(modes, peaks)]
        got, missing = {}, []
        for key in keys:
            if key in self.cache:
                self.cache.move_to_end(key)
                got[key] = self.cache[key]
            elif key not in missing:
                missing.append(key)
        if missing:
            cols, ups = phonon_unit_columns(self.x, self.ctx, [k[:2] for k in missing],
                                            [k[2] for k in missing])
            for key, col, up in zip(missing, cols, ups):
                got[key] = self.cache[key] = (col, up)
            while len(self.cache) > CACHE_SIZE:
                self.cache.popitem(last=False)
        return [got[k] for k in keys]

    def qe_column(self, width) -> np.ndarray:
        """The central QE's unit column at intrinsic FWHM ``width`` (exposure-free), from the same LRU."""
        key = ("qe", float(width))
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        col = self.cache[key] = central_qe_columns(self.x, self.ctx, [float(width)])[0][0]
        while len(self.cache) > CACHE_SIZE:
            self.cache.popitem(last=False)
        return col

    def mean(self, model, theta):
        """``exposure x (sum h_i col_i + elastic.h x elastic column + qe.h x QE column + bg.BG)``."""
        total = np.zeros_like(self.y)
        if model.modes:
            units = self.units([(theta[a], theta[b]) for a, b, _ in model.modes], model.variant.peaks)
            for (_, _, c), (col, _up) in zip(model.modes, units):
                total += theta[c] * col
        if model.el is not None:
            total += theta[model.el] * self.el_col
        if model.qe_h is not None:
            total += theta[model.qe_h] * self.qe_column(model.qe_width(theta))
        if model.bg is not None:
            total += theta[model.bg]
        return total * self.expo

    def cost(self, model, theta) -> float:
        r = poisson_residuals(self.y, self.mean(model, theta))
        return float(r @ r)

    # ---- model identities ------------------------------------------------------------------------
    def model(self, variant, delta) -> _Model | None:
        """The model of ``variant`` with delta mask ``delta``; None when the scan's own constraints
        clash with the mask (no such model)."""
        key = (variant.label, tuple(delta))
        if key not in self.models:
            self.models[key] = self._build_model(variant, tuple(delta))
        return self.models[key]

    def _build_model(self, variant, delta):
        hyp = variant.hyp
        if delta:
            hyp = loop.delta_twin_hypothesis(hyp, [loop._mode_prefix(i + 1) for i in delta], self.ctx)
            if hyp is None:
                return None
        cctx = loop.candidate_context(hyp, self.ctx)
        composite = hyp.build()
        names = composite.param_names()
        bounds = prior.mode_bounds(names, cctx, self.prior_s)
        lo, hi = np.array(bounds, dtype=float).T
        constraints = getattr(cctx, "constraints", None)
        fixed_pos, fixed_vals = loop._hard_fixed(constraints, names)
        soft = [(names.index(n), float(v[0]), float(v[1]))
                for n, v in (getattr(constraints, "soft", None) or {}).items()
                if n in names and names.index(n) not in fixed_pos and float(v[1]) > 0]
        modes = [(i, i + 1, i + 2) for i in composite.phonon_indices()]
        soft_priors = []
        if soft:
            # A soft constraint replaces its own coordinate's recipe factor (one prior per
            # coordinate); every other coordinate keeps the configured recipe (ln_prior).
            got = catalogue.resolve_priors(composite, cctx, bounds,
                                           exclude=[names[i] for i in fixed_pos]).by_name()
            soft_priors = [(i, got[names[i]]) for i, _mu, _sd in soft]
        omit = frozenset(fixed_pos) | {modes[i][1] for i in delta} | {i for i, _mu, _sd in soft}
        # Modes are exchangeable (one ordered-energy factor) only when their non-default priors
        # agree, as catalogue.PriorBundle._energy_groups has it: a soft prior or a pin on one mode's
        # width or height singles it out; a delta mode's own pinned width does not.
        soft_by_pos = {i: (p.kind, p.mu, p.sigma, p.lo, p.hi) for i, p in soft_priors}
        by_key = {}
        for m, (a, g, c) in enumerate(modes):
            if a in omit:
                continue
            key = tuple((tag, soft_by_pos.get(j, "pinned")) for tag, j in (("Gamma", g), ("h", c))
                        if j in soft_by_pos or (j in fixed_pos and not (j == g and m in delta)))
            by_key.setdefault(key, []).append(a)
        qe = next((c for c in composite.components if isinstance(c, CentralQE)), None)
        return _Model(variant=variant, delta=delta, hyp=hyp, cctx=cctx, names=names, lo=lo, hi=hi,
                      fixed_pos=list(fixed_pos), fixed_vals=list(fixed_vals),
                      free=np.array([i for i in range(len(names)) if i not in set(fixed_pos)], int),
                      soft=soft, modes=modes,
                      el=names.index("elastic.h") if "elastic.h" in names else None,
                      bg=names.index("bg.BG") if "bg.BG" in names else None,
                      omit=omit, soft_priors=soft_priors, groups=list(by_key.values()),
                      qe_h=names.index("qe.h") if qe is not None else None,
                      qe_w=names.index("qe.width") if "qe.width" in names else None,
                      qe_pin=None if qe is None else qe.width)

    def modes_of(self, model, theta) -> list:
        """[(omega0, Gamma, h, delta)] per mode, ascending omega0 for a canonical point."""
        return [(float(theta[a]), float(theta[b]), float(theta[c]), i in model.delta)
                for i, (a, b, c) in enumerate(model.modes)]

    def linear_of(self, model, theta) -> dict:
        def at(pos):
            return None if pos is None else float(theta[pos])
        return {"elastic": at(model.el), "bg": at(model.bg), "qe_h": at(model.qe_h),
                "qe_width": at(model.qe_w)}

    # ---- one fit ---------------------------------------------------------------------------------
    def _start(self, model, modes, linear):
        """The start vector: the modes' values, the given elastic/background/QE values, the pins; a
        free QE width not given starts at ``qe_start``; any height left unset (``None``) solved by
        Poisson NNLS with the others held."""
        theta = np.full(len(model.names), np.nan)
        for (a, b, c), (e, g, h, _d) in zip(model.modes, modes):
            theta[a], theta[b], theta[c] = e, g, (np.nan if h is None else h)
        for key, pos in (("elastic", model.el), ("bg", model.bg), ("qe_h", model.qe_h),
                         ("qe_width", model.qe_w)):
            if pos is not None and linear and linear.get(key) is not None:
                theta[pos] = linear[key]
        if model.qe_w is not None and not np.isfinite(theta[model.qe_w]):
            theta[model.qe_w] = self.qe_start
        ok = np.isfinite(theta)
        theta[ok] = np.clip(theta[ok], model.lo[ok], model.hi[ok])
        theta[model.fixed_pos] = model.fixed_vals
        self.zmap(model).clip(theta)        # a width starts under its mode's ceiling
        missing = [int(i) for i in model.free if not np.isfinite(theta[i])]
        if missing:
            theta[missing] = 0.0
            offset = self.mean(model, theta)
            phi = np.column_stack([self._linear_column(model, theta, i) for i in missing])
            theta[missing] = backends.poisson_nnls(phi, self.y, np.zeros(len(missing)),
                                                   model.lo[missing], model.hi[missing],
                                                   offset=offset)
        return theta

    def _linear_column(self, model, theta, i):
        if i == model.el:
            return self.el_col * self.expo
        if i == model.bg:
            return self.expo
        if i == model.qe_h:
            return self.qe_column(model.qe_width(theta)) * self.expo
        j = next(j for j, (_a, _b, c) in enumerate(model.modes) if c == i)
        a, b, _c = model.modes[j]
        return self.units([(theta[a], theta[b])], [model.variant.peaks[j]])[0][0] * self.expo

    def _least_squares(self, residual, z0, lo, hi, family):
        """One bounded TRF call (prototype tolerances); None when it fails or hits max_nfev."""
        t0 = time.perf_counter()
        self.n_fits += 1
        try:
            res = least_squares(residual, z0, bounds=(lo, hi), method="trf", x_scale="jac",
                                max_nfev=self.s.trf_max_nfev, ftol=2e-7, xtol=2e-7, gtol=2e-6)
        except (ValueError, np.linalg.LinAlgError) as exc:
            log.info("fast pass: a %s start failed: %s", family, exc)
            return None
        finally:
            fam = self.families.setdefault(family, {"fits": 0, "seconds": 0.0})
            fam["fits"] += 1
            fam["seconds"] += time.perf_counter() - t0
        if res.status <= 0 or not res.success:
            log.debug("fast pass: a %s start stopped: %s", family, res.message)
            return None
        return res

    def _trf(self, model, theta0, family):
        """TRF in the z coordinate (widths as ``t`` in [0, 1]); ``res.x`` is z, ``zmap(model).to_theta``
        maps it back."""
        zm = self.zmap(model)
        lo, hi = zm.lo, zm.hi

        def residual(z):
            t = zm.to_theta(z, theta0)
            r = poisson_residuals(self.y, self.mean(model, t))
            if model.soft:
                r = np.concatenate([r, [(t[i] - mu) / sd for i, mu, sd in model.soft]])
            return np.nan_to_num(r, nan=1e100, posinf=1e100, neginf=-1e100)

        return self._least_squares(residual, np.clip(zm.to_z(theta0), lo, hi), lo, hi, family)

    def fit(self, variant, modes, family, linear=None):
        """Fit ``modes`` ([(omega0, Gamma, h | None, delta)]) in ``variant`` by TRF, canonicalise,
        and place the optimum in its model's basins (a new or improved basin is resolved).
        Returns (basin, this fit's cost), or None when there is no such model or the start failed
        (family counts record attempts; ``conclude`` retains models with no converged start)."""
        if variant is None:
            return None
        modes = sorted(modes, key=lambda m: m[0])
        model = self.model(variant, tuple(i for i, m in enumerate(modes) if m[3]))
        if model is None:
            return None
        theta0 = self._start(model, modes, linear)
        res = self._trf(model, theta0, family)
        if res is None:
            return None
        theta = self.zmap(model).to_theta(res.x, theta0)
        model, theta = self._canonical(model, theta)
        if model is None:
            return None
        cost = self.cost(model, theta)
        if not math.isfinite(cost):
            return None
        return self._place(model, theta, cost, family, int(res.nfev), str(res.message)), cost

    def _canonical(self, model, theta):
        """Modes sorted by omega0; the delta mask permutes with its modes (a new identity)."""
        order = sorted(range(len(model.modes)), key=lambda i: theta[model.modes[i][0]])
        if order == list(range(len(order))):
            return model, theta
        out = theta.copy()
        for dst, src in enumerate(order):
            for a, b in zip(model.modes[dst], model.modes[src]):
                out[a] = theta[b]
        delta = tuple(sorted(dst for dst, src in enumerate(order) if src in model.delta))
        return self.model(model.variant, delta), out

    def same_basin(self, model, a, b) -> bool:
        """One basin needs, mode by mode, every omega0, Gamma and height close (C-fastpass)."""
        s, w = self.s, self.fwhm
        for i, j, c in model.modes:
            if abs(a[i] - b[i]) >= s.basin_merge_fwhm * w:
                return False
            if abs(a[j] - b[j]) > s.basin_gamma_frac * max(w, a[j], b[j]):
                return False
            if abs(a[c] - b[c]) > s.basin_height_frac * max(a[c], b[c]):
                return False
        if model.qe_w is not None and abs(a[model.qe_w] - b[model.qe_w]) > \
                s.basin_gamma_frac * max(w, a[model.qe_w], b[model.qe_w]):
            return False
        h = model.qe_h
        return h is None or abs(a[h] - b[h]) <= s.basin_height_frac * max(a[h], b[h])

    def _place(self, model, theta, cost, family, nfev, message):
        for b in model.basins:
            if b.theta is not None and self.same_basin(model, b.theta, theta):
                if cost < b.cost - loop.DEGENERATE_REFIT_GAIN:
                    b.theta, b.cost, b.ln_like = theta, cost, self.sat - cost / 2
                    b.family, b.nfev, b.message = family, nfev, message
                    b.status, b.reason, b.twin, b.limit = "pending", "", None, ""
                    b.theta_map = b.ln_z = b.ln_prior = b.cov_free = b.objective = None
                    b.pinned, b.boundary_method, b.posterior_iterations = (), "", 0
                    self.resolve(b, b.theta)
                return b
        b = _Basin(model, len(model.basins), theta, cost, self.sat - cost / 2,
                   _ln_model_prior(len(self.variants[model.variant.k]), model.variant.k),
                   family, nfev, message)
        model.basins.append(b)
        self.resolve(b, theta)
        return b

    # ---- stage 1: the seed -------------------------------------------------------------------------
    def _pv(self, e0, gamma):
        """The seed's pseudo-Voigt column at unit height, its anti-Stokes partner tied by the Bose
        factor (none at unknown T, C-dho's convention); no convolution."""
        width = self.fwhm + 2.0 * gamma

        def side(c):
            u = (self.E - c) / width
            return 0.5 * np.exp(-4.0 * math.log(2.0) * u * u) + 0.5 / (1.0 + 4.0 * u * u)

        w = math.exp(-e0 / self.kT) if self.kT else 0.0
        return side(e0) + w * side(-e0)

    def _choose_qe_start(self):
        """``qe_start``: the pinned width, else of the QE_START_WIDTHS_FWHM the one whose Poisson NNLS
        of [elastic, QE column, flat] costs least. Starting points only; the prior is unchanged."""
        pins = [c.width for vs in self.variants.values() for v in vs for c in v.hyp.components
                if isinstance(c, CentralQE) and c.width is not None]
        if pins:
            self.qe_start = pins[0]
            return
        lo, hi = self.sp.bounds("qe_width")
        widths = list(dict.fromkeys(float(np.clip(f * self.fwhm, lo, hi)) for f in QE_START_WIDTHS_FWHM))
        self.qe_start_widths = widths
        costs = [self._seed_nnls(self._seed_linear_cols(w), 0)[1] for w in widths]
        self.qe_start = widths[int(np.argmin(costs))]

    def _seed_linear_cols(self, qe_width=None):
        qe = [self.qe_column(self.qe_start if qe_width is None else qe_width)] if self.seed_qe else []
        return ([self.el_col] if self.seed_elastic else []) + qe + [np.ones_like(self.y)]

    def _seed_nnls(self, cols, n_peaks):
        sp = self.sp
        phi = np.column_stack(cols) * self.expo[:, None]
        hi = np.array([sp.height_hi] * (n_peaks + self.seed_elastic + self.seed_qe)
                      + [sp.background_hi])
        a = backends.poisson_nnls(phi, self.y, np.zeros(phi.shape[1]), np.zeros(phi.shape[1]), hi)
        r = poisson_residuals(self.y, phi @ a)
        return a, float(r @ r)

    def _seed_fit(self, peaks, linear):
        """Joint TRF of the PV seed model, every centre free; (peaks ascending, cost) or None."""
        sp, k, el, qe = self.sp, len(peaks), self.seed_elastic, self.seed_qe
        z0 = np.array([v for p in peaks for v in p] + list(linear), dtype=float)
        lo = np.array([sp.e_lo, self.g_lo, 0.0] * k + [0.0] * (el + qe + 1))
        hi = np.array([sp.e_hi, self.g_hi, sp.height_hi] * k + [sp.height_hi] * (el + qe)
                      + [sp.background_hi])
        qe_col = self.qe_column(self.qe_start) if qe else None

        def mean(z):
            mu = np.zeros_like(self.y)
            for i in range(k):
                mu += z[3 * i + 2] * self._pv(z[3 * i], z[3 * i + 1])
            if el:
                mu += z[3 * k] * self.el_col
            if qe:
                mu += z[3 * k + el] * qe_col
            return (mu + z[-1]) * self.expo

        def residual(z):
            return np.nan_to_num(poisson_residuals(self.y, mean(z)), nan=1e100, posinf=1e100,
                                 neginf=-1e100)

        res = self._least_squares(residual, np.clip(z0, lo, hi), lo, hi, "seed")
        if res is None:
            return None
        r = poisson_residuals(self.y, mean(res.x))
        return sorted((res.x[3 * i], res.x[3 * i + 1], res.x[3 * i + 2]) for i in range(k)), float(r @ r)

    def seed(self) -> list:
        """Stage 1: PV peaks added one at a time at the grid energy that most improves the Poisson
        fit (NNLS heights), all refitted jointly after each insertion, until a peak adds less than
        ``seed_stop_gain`` or the ceiling. Starting points only: [(omega0, Gamma, h)]."""
        sp, w = self.sp, self.fwhm
        if self.seed_qe:
            self._choose_qe_start()
        base = self._seed_linear_cols()
        a, _ = self._seed_nnls(base, 0)
        prev = self._seed_fit([], a)
        held = []
        if prev is not None:
            for k in range(1, self.s.mode_ceiling + 1):
                best = None
                for e in np.arange(max(MIN_ADD_ENERGY, sp.e_lo), sp.e_hi, SEED_STEP_FWHM * w):
                    if any(abs(e - p[0]) < SEED_EXCLUDE_FWHM * w for p in held):
                        continue
                    trial = [(p[0], p[1]) for p in held] + [(float(e), SEED_GAMMA)]
                    a, score = self._seed_nnls([self._pv(*p) for p in trial] + base, k)
                    if best is None or score < best[0]:
                        best = (score, trial, a)
                if best is None:
                    break
                _, trial, a = best
                cand = self._seed_fit([(e, g, h) for (e, g), h in zip(trial, a[:k])], a[k:])
                if cand is None or prev[1] - cand[1] < self.s.seed_stop_gain:
                    break
                held, prev = cand[0], cand
        self.seed_count = len(held)
        return held

    # ---- stage 2: the ladder -------------------------------------------------------------------------
    def best_basin(self, k, *, live=False):
        """The lowest-cost fitted basin at count ``k`` (``live``: twins excluded), or None."""
        found = [b for b in self.basins() if b.count == k and b.theta is not None
                 and not (live and b.status in ("degenerate_twin", "not_fitted"))]
        return min(found, key=lambda b: b.cost, default=None)

    def _perturbed(self, top):
        sp, (de, dg, dh) = self.sp, RESTART_SPREAD
        out = []
        for e, g, h in top:
            out.append((float(np.clip(e + self.rng.normal(0.0, de * self.fwhm), sp.e_lo, sp.e_hi)),
                        float(np.clip(g * math.exp(self.rng.normal(0.0, dg)), self.g_lo, self.g_hi)),
                        float(np.clip(h * self.rng.uniform(1.0 - dh, 1.0 + dh), 0.0, sp.height_hi)),
                        False))
        return out

    def _additions(self, parent):
        """Ladder starts above the seed's count: the parent plus one mode at its largest positive
        residual, and every parent mode split in two (family "split")."""
        w, sp = self.fwhm, self.sp
        model, t = parent.model, parent.theta
        held = self.modes_of(model, t)
        r = (self.y - self.mean(model, t)) / self.expo
        pick = next((i for i in np.argsort(-r) if MIN_ADD_ENERGY < self.E[i] <= sp.e_hi
                     and all(abs(self.E[i] - m[0]) > LADDER_EXCLUDE_FWHM * w for m in held)), None)
        e_new, h_new = ((0.8 * sp.e_hi, 1.0) if pick is None
                        else (float(self.E[pick]), max(float(r[pick]), 1.0)))
        starts = [("ladder", held + [(e_new, LADDER_GAMMA, h_new, False)])]
        lo_f, hi_f = LADDER_SPLIT_HEIGHTS
        for j, (e, g, h, d) in enumerate(held):
            g2 = SEED_GAMMA if d else g
            parts = [(e - LADDER_SPLIT_FWHM * w, g2, h * lo_f, False),
                     (e + LADDER_SPLIT_FWHM * w, g2, h * hi_f, False)]
            starts.append(("split", held[:j] + parts + held[j + 1:]))
        return starts

    def _hint_start(self, seed, k):
        """Card peak hints (not within 0.5 FWHM of a seed peak) first, then the seed peaks, k of
        them; None when no hint survives or there are fewer than k modes."""
        w = self.fwhm
        hints = [h for h in self.hints if all(abs(h - p[0]) >= HINT_EXCLUDE_FWHM * w for p in seed)]
        modes = ([(h, HINT_GAMMA_FWHM * w, None, False) for h in hints]
                 + [(e, g, h, False) for e, g, h in seed])[:k]
        return modes if hints and len(modes) == k else None

    def ladder(self, seed):
        """Stage 2: every count 0..ceiling, each start fitted in every variant at that count."""
        for k in range(self.s.mode_ceiling + 1):
            if not self.variants.get(k):
                continue
            self.reached.add(k)
            self.emit("ladder", step=k, of=self.s.mode_ceiling)
            starts = []
            if k <= len(seed):
                top = sorted(seed, key=lambda p: -p[2])[:k]
                starts.append(("ladder", [(e, g, h, False) for e, g, h in top]))
                if k:
                    starts += [("restart", self._perturbed(top)) for _ in range(self.s.n_restarts)]
            else:
                parent = self.best_basin(k - 1)
                if parent is not None:
                    starts += self._additions(parent)
            if k >= 1 and self.hints:
                hinted = self._hint_start(seed, k)
                if hinted is not None:
                    starts.append(("hint", hinted))
            starts = [(family, modes, None) for family, modes in starts]
            if k == 0:                       # the other QE starting widths (none when pinned)
                starts += [("qe_width", [], {"qe_width": w}) for w in self.qe_start_widths
                           if w != self.qe_start]
            for v in self.variants[k]:
                for family, modes, linear in starts:
                    self.fit(v, modes, family, linear)

    # ---- twins ------------------------------------------------------------------------------------
    def resolve(self, b, point):
        """Step 5: whether ``b`` at ``point`` (a TRF optimum or a posterior mode) is a degenerate or
        delta-mode twin of a model with fewer free coordinates. The first screen that hits acts:
        its twin is refitted by TRF from ``point`` and, except for a railed width and a QE at its
        narrowest width, must reach
        ``point``'s cost within ``twin_cost_tolerance``; a twin that fails or does not exist leaves
        ``b`` a model of its own. Recursion runs through the twin's own placement."""
        with self._stage("twins"):
            self._resolve(b, point)

    def _resolve(self, b, point):
        s, model, sp = self.s, b.model, self.sp
        v, k, names = model.variant, model.variant.k, model.names
        free = set(model.free.tolist())
        cost_p = b.cost if point is b.theta else self.cost(model, point)
        modes = self.modes_of(model, point)
        lin = self.linear_of(model, point)

        def twin(kind, limit, variant, new_modes, linear, *, verify=True, reason=None):
            out = self.fit(variant, new_modes, "twin", linear)
            if out is None:
                return False
            tb, tcost = out
            if verify and tcost > cost_p + s.twin_cost_tolerance:
                return False
            b.status, b.twin, b.limit = "degenerate_twin", tb, limit
            b.reason = f"{kind}: {reason or limit}"
            return True

        def without(j):
            return modes[:j] + modes[j + 1:]

        for j, (_a, _g, c) in enumerate(model.modes):                  # 1 amplitude
            if c in free and point[c] <= s.twin_height_frac * sp.height_hi:
                twin("zero height", names[c], self.variant(k - 1, v.elastic, v.background, v.qe),
                     without(j), lin)
                return
        for j, (a, _g, c) in enumerate(model.modes):                   # 2 energy
            lo = model.lo[a]
            railed = a in free and point[a] <= lo + loop.DEGENERATE_LIMIT_TOL * max(abs(lo), 1.0)
            if railed or self._mimics_elastic(model, point, j):
                target = self.variant(k - 1, True, v.background, v.qe)
                if target is not None:
                    merged = dict(lin, elastic=(lin["elastic"] or 0.0) + float(point[c]))
                    twin("elastic limit", names[a], target, without(j), merged)
                return
        if model.el is not None and model.el in free \
                and point[model.el] <= s.twin_height_frac * sp.height_hi \
                and not central_qe_admissible(self.ctx):                       # 3 elastic
            # never on BL43LXU: the elastic line stays in every model, and a zero QE falls to 3b
            twin("zero elastic", names[model.el], self.variant(k, False, v.background, v.qe),
                 modes, lin)
            return
        if model.qe_h is not None and model.qe_h in free \
                and point[model.qe_h] <= s.twin_height_frac * sp.height_hi:     # 3b QE
            twin("zero qe", names[model.qe_h], self.variant(k, v.elastic, v.background, False),
                 modes, lin)
            return
        if v.elastic and model.qe_w is not None and model.qe_w in free:                                # 3c QE width
            lo = model.lo[model.qe_w]
            if point[model.qe_w] <= lo + loop.DEGENERATE_LIMIT_TOL * max(abs(lo), 1.0):
                twin("qe railed", names[model.qe_w], self.variant(k, v.elastic, v.background, False),
                     modes, lin, verify=False)
                return
        if self._coincident(model, modes, lin, twin):                     # 4 coincident
            return
        if DELTA_TWIN_RULE:                                                # 5 width
            for j, (_a, g, _c) in enumerate(model.modes):
                if g in free and point[g] <= GAMMA_FLOOR + loop.DEGENERATE_LIMIT_TOL * max(GAMMA_FLOOR, 1.0):
                    e, _gv, h, _d = modes[j]
                    railed = modes[:j] + [(e, GAMMA_FLOOR, h, True)] + modes[j + 1:]
                    twin("width railed", names[g], v, railed, lin, verify=False)
                    return
        if model.bg is not None and model.bg in free \
                and point[model.bg] <= s.twin_height_frac * sp.background_hi:   # 6 background
            twin("zero background", names[model.bg], self.variant(k, v.elastic, False, v.qe),
                 modes, lin)

    def _mimics_elastic(self, model, point, j) -> bool:
        """A mode whose unit column is the elastic column (both of maximum 1) within tolerance."""
        a, g, _c = model.modes[j]
        col = self.units([(point[a], point[g])], [model.variant.peaks[j]])[0][0]
        return float(np.max(np.abs(col - self.el_col))) < self.s.elastic_twin_tolerance

    def _coincident(self, model, modes, lin, twin) -> bool:
        """Screen 4: pairs closer than ``twin_coincide_fwhm`` x FWHM, merged ONE AT A TIME into the
        count below (exactly coincident delta pairs first, then ascending separation); when none
        refits within tolerance, the all-pairs chained merge is one more candidate. True when the
        screen hit (whether or not a twin was accepted)."""
        v, k, names = model.variant, model.variant.k, model.names
        distance = self.s.twin_coincide_fwhm * self.fwhm
        pairs = [(i, j) for i in range(k) for j in range(i + 1, k)
                 if abs(modes[i][0] - modes[j][0]) < distance]
        if not pairs:
            return False

        def order(p):
            i, j = p
            d = abs(modes[i][0] - modes[j][0])
            return (not (modes[i][3] and modes[j][3] and d < COINCIDENT_DELTA_MEV), d)

        target = self.variant(k - 1, v.elastic, v.background, v.qe)
        for i, j in sorted(pairs, key=order):
            merged = _merge_pair(modes[i], modes[j], modes[i][3] and modes[j][3])
            rest = [m for n, m in enumerate(modes) if n not in (i, j)]
            pair = f"{names[model.modes[i][0]]}, {names[model.modes[j][0]]}"
            if twin("coincident", names[model.modes[j][0]], target, rest + [merged], lin,
                    reason=pair):
                return True
        chained = _merge_chain(modes, distance)
        if len(pairs) > 1 and len(chained) < k:
            twin("coincident", names[model.modes[pairs[0][1]][0]],
                 self.variant(len(chained), v.elastic, v.background, v.qe), chained, lin,
                 reason="all-pairs merge")
        return True

    # ---- evidence ----------------------------------------------------------------------------------
    def ln_prior(self, model, theta) -> float:
        """The configured recipe's log prior (C-prior) at a full vector, one factor per coordinate: a
        pinned coordinate (a delta mode's width included) carries none and a soft-constrained one
        its truncated Gaussian instead; one ordered-energy factor per group of exchangeable modes
        (``model.groups``; a group of one is the single penalised energy factor g(E))."""
        sp, omit = self.sp, model.omit
        lp = sum(sp.ln_energies([theta[a] for a in grp]) for grp in model.groups)
        for m, (a, g, c) in enumerate(model.modes):
            if g not in omit:
                lp += self._ln_width(model, theta, m)
            if c not in omit:
                lp += sp.ln_height(theta[c])
        if model.el is not None and model.el not in omit:
            lp += sp.ln_height(theta[model.el])
        if model.bg is not None and model.bg not in omit:
            lp += sp.ln_background(theta[model.bg])
        if model.qe_h is not None and model.qe_h not in omit:
            lp += sp.ln_height(theta[model.qe_h])
        if model.qe_w is not None and model.qe_w not in omit:
            lp += sp.ln_qe_width(theta[model.qe_w])
        return lp + self._ln_soft(model, theta)

    def objective(self, model, theta):
        """f(z) = cost/2 - sat - ln prior - ln_jac on the free coordinates in the z coordinate (the
        widths as ``t`` in [0, 1]), +inf outside the box; with ``theta`` supplying the fixed
        coordinates. Returns (f, lo, hi), the box in z."""
        zm = self.zmap(model)
        lo, hi = zm.lo, zm.hi
        tol = 1e-10 * np.maximum(np.abs(lo) + np.abs(hi), 1.0)
        template = np.array(theta, dtype=float)

        def f(z):
            z = np.asarray(z, dtype=float)
            if np.any(z < lo - tol) or np.any(z > hi + tol):
                return math.inf
            t = zm.to_theta(np.clip(z, lo, hi), template)   # a bound transform can round a few ulps outside
            lp = self.ln_prior(model, t)
            if not math.isfinite(lp):
                return math.inf
            return 0.5 * self.cost(model, t) - self.sat - lp - zm.ln_jac(t)

        return f, lo, hi

    def objective_and_gradient(self, model, theta):
        """``(fg, lo, hi)`` with ``fg(z) = (f(z), df/dz)`` from one joint evaluation of ``objective``'s f, or None:
        the fast pass has none, so ``price`` and ``laplace`` keep their finite-difference calls exactly (a
        subclass, the coupled engine, supplies one)."""
        return None

    def laplace(self, b, f, z, lo, hi, rel_step=1e-4):
        fg = self.objective_and_gradient(b.model, b.theta)
        zm = self.zmap(b.model)
        floor = zm.step_floor(zm.to_theta(z, b.theta)) if zm.mapped else None
        if fg is None:
            return prior.laplace_log_evidence(f, z, lo, hi, rel_step=rel_step, step_floor=floor)
        return prior.laplace_log_evidence(f, z, lo, hi, rel_step=rel_step, grad=_gradient_of(fg[0]),
                                          step_floor=floor)

    def price_all(self):
        """Step 6 for every pending basin, until none is left (pricing can add basins)."""
        self.emit("evidence")
        with self._stage("evidence"):
            while True:
                pending = [b for b in self.basins() if b.status == "pending"]
                if not pending:
                    return
                for b in pending:
                    if b.status == "pending":
                        self.price(b)

    def _posterior(self, model, theta) -> _Post:
        """The posterior optimiser (L-BFGS-B on the free coordinates scaled to order one) from ``theta``."""
        f, lo, hi = self.objective(model, theta)
        fg = self.objective_and_gradient(model, theta)
        z0 = self.zmap(model).to_z(theta)
        scale = np.maximum(np.abs(z0), 1.0)
        options = {"maxiter": self.s.posterior_maxiter, "ftol": 1e-11, "gtol": 1e-5,
                   "maxfun": 20 * (z0.size + 1) * self.s.posterior_maxiter}
        if fg is None:
            res = minimize(lambda u: f(u * scale), z0 / scale, method="L-BFGS-B", jac=self.posterior_jac,
                           bounds=list(zip(lo / scale, hi / scale)), options=options)
        else:
            def fun(u):
                v, g = fg[0](u * scale)
                return v, g * scale
            res = minimize(fun, z0 / scale, method="L-BFGS-B", jac=True,
                           bounds=list(zip(lo / scale, hi / scale)), options=options)
        return _Post(res.x * scale, int(res.nit), int(res.status), bool(res.success), str(res.message))

    def price(self, b):
        """The posterior mode by L-BFGS-B from the TRF optimum, the twin screens there, then the
        box-rule Laplace evidence; a hit iteration limit is ``no_evidence``, never a failed model."""
        s, model = self.s, b.model
        f, lo, hi = self.objective(model, b.theta)
        res = self._posterior(model, b.theta)
        b.posterior_iterations = res.nit
        zm = self.zmap(model)
        z = np.clip(res.x, lo, hi)
        point = zm.to_theta(z, b.theta)
        if res.status == 1:
            b.status = "no_evidence"
            b.reason = f"posterior optimiser iteration limit ({s.posterior_maxiter})"
            if DELTA_TWIN_RULE:
                self._delta_proposal(model, point)
            return
        if not res.success:
            b.status, b.reason = "no_evidence", f"posterior optimiser: {res.message}"
            return
        self.resolve(b, point)
        if b.status == "degenerate_twin":
            return
        lap = self.laplace(b, f, z, lo, hi)
        if lap.status == "no_evidence":
            b.status, b.reason = "no_evidence", lap.reason
            return
        b.status, b.ln_z, b.theta_map = lap.status, float(lap.ln_z), point
        b.ln_prior = float(self.ln_prior(model, point))
        # the Laplace covariance is in z; the stored one is in meV (delta method at the mode)
        b.cov_free = None if lap.cov is None else zm.cov_to_theta(point, lap.cov)
        b.boundary_method, b.objective = lap.boundary_method, f
        span = hi - lo
        b.pinned = tuple(model.names[i] for i, zi, l, h, w in zip(model.free, z, lo, hi, span)
                         if zi - l <= 1e-6 * w or h - zi <= 1e-6 * w)
        self._near_zero(b)

    def _delta_proposal(self, model, point):
        """An unconverged posterior iterate that rails a free width only proposes that delta
        twin, as one more start (decision 2); the iterate never replaces the model."""
        free = set(model.free.tolist())
        for j, (_a, g, _c) in enumerate(model.modes):
            if g in free and point[g] <= GAMMA_FLOOR + loop.DEGENERATE_LIMIT_TOL * max(GAMMA_FLOOR, 1.0):
                modes = self.modes_of(model, point)
                e, _gv, h, _d = modes[j]
                modes[j] = (e, GAMMA_FLOOR, h, True)
                self.fit(model.variant, modes, "delta_proposal", self.linear_of(model, point))
                return

    def _near_zero(self, b):
        """Step 6a: every finite width within NEAR_ZERO_SIGMAS sd of zero gets its delta masks
        fitted and priced although it never rails (the mask prior is unchanged)."""
        model, t = b.model, b.theta_map
        free = model.free.tolist()
        near = []
        for j, (_a, g, _c) in enumerate(model.modes):
            if j in model.delta or g not in free:
                continue
            var = float(b.cov_free[free.index(g), free.index(g)]) if b.cov_free is not None else math.nan
            sd = math.sqrt(var) if math.isfinite(var) and var > 0 else None
            if t[g] < (NEAR_ZERO_SIGMAS * sd if sd is not None else self.fwhm / 2):
                near.append(j)
        # shortcut: masks over a 4th near-zero mode stay unpriced; upgrade if a spectrum shows four.
        # shortcut: masks of well-resolved widths (Gamma >= 4.5 sd) are not fitted: their evidence is
        #   below e^-10 of the parent's (a Gaussian width 4.5 sd from zero); S2U2's comparison checks it.
        near = sorted(sorted(near, key=lambda j: t[model.modes[j][1]])[:NEAR_ZERO_MAX_MODES])
        modes, lin = self.modes_of(model, t), self.linear_of(model, t)
        for r in range(1, len(near) + 1):
            for subset in itertools.combinations(near, r):
                mask = tuple(sorted(set(model.delta) | set(subset)))
                target = self.model(model.variant, mask)
                if target is None or target.basins:
                    continue
                start = [(e, GAMMA_FLOOR, h, True) if j in subset else (e, g, h, d)
                         for j, (e, g, h, d) in enumerate(modes)]
                self.fit(model.variant, start, "near_zero", lin)

    # ---- audits ------------------------------------------------------------------------------------
    def audit(self):
        """Step 7: from the best non-twin basin of every contending count, deletions and merges
        down, moved centres and changed widths, splits both ways, shoulders and broad additions up."""
        basins = self.basins()
        tally = _tally(basins, self.s.contending_margin, _superseded(basins))
        for k in sorted(set(tally.priced_contenders) | set(tally.unpriced_contenders)):
            parent = max((b for b in self.basins() if b.count == k and b.theta is not None
                          and b.status not in ("degenerate_twin", "not_fitted")),
                         key=lambda b: b.ln_like, default=None)
            if parent is None:
                continue
            v = parent.model.variant.origin       # an on-demand parent's starts fit its starting variant
            for family, n, modes, linear in self._audit_starts(parent):
                self.fit(self._by_flags.get((n, v.elastic, v.background, v.qe)), modes, family, linear)

    def _audit_starts(self, parent):
        s, sp, w = self.s, self.sp, self.fwhm
        model = parent.model
        modes = self.modes_of(model, parent.theta)
        k = len(modes)
        out = []
        if k >= 1:
            for j in range(k):
                out.append(("delete", k - 1, modes[:j] + modes[j + 1:]))
            for j in range(k - 1):
                merged = _merge_pair(modes[j], modes[j + 1], False)
                out.append(("merge", k - 1, modes[:j] + [merged] + modes[j + 2:]))
            for j, (e, g, h, d) in enumerate(modes):
                for sign in (-1.0, 1.0):
                    out.append(("move", k, modes[:j] + [(e + sign * MOVE_FWHM * w, g, h, d)]
                                + modes[j + 1:]))
                if not d:
                    for factor in WIDTH_FACTORS:
                        out.append(("width", k, modes[:j] + [(e, max(g * factor, self.g_lo), h, d)]
                                    + modes[j + 1:]))
        if k < s.mode_ceiling:
            for j, (e, g, h, d) in enumerate(modes):
                g2 = SEED_GAMMA if d else g
                for sep in s.split_separations_fwhm:
                    for low in SPLIT_FRACTIONS:
                        parts = [(e - sep * w / 2, g2, h * low, False),
                                 (e + sep * w / 2, g2, h * (1.0 - low), False)]
                        out.append(("split", k + 1, modes[:j] + parts + modes[j + 1:]))
                for sign in (-1.0, 1.0):
                    out.append(("shoulder", k + 1,
                                modes + [(e + sign * SHOULDER_FWHM * w, g2, SHOULDER_FRACTION * h, False)]))
            for e in np.arange(1.0, sp.e_hi, BROAD_STEP_MEV):
                for width in BROAD_WIDTHS_MEV:
                    out.append(("broad", k + 1, modes + [(float(e), width,
                                                          BROAD_HEIGHT_FRAC * sp.height_hi, False)]))
        out = [(family, n, m, None) for family, n, m in out]
        if model.qe_w is not None:           # the QE width moved both ways, the heights re-solved
            i = model.qe_w
            clipped = {float(np.clip(parent.theta[i] * f, model.lo[i], model.hi[i])) for f in WIDTH_FACTORS}
            out += [("qe_width", k, modes, {"qe_width": w}) for w in sorted(clipped)]   # a clip may repeat
        return out

    def run(self):
        """Seed, ladder, pricing and the audit rounds. No clock stops the search."""
        self.emit("seed")
        with self._stage("seed"):
            seed = self.seed()
        with self._stage("ladder"):
            self.ladder(seed)
        self.price_all()
        for _ in range(self.s.audit_rounds):
            self.emit("audits")
            with self._stage("audits"):
                self.audit()
            self.price_all()

    # ---- adapter out ---------------------------------------------------------------------------------
    def _full_cov(self, b):
        if b.status not in _PRICED or b.cov_free is None:
            return None
        n, free = len(b.model.names), b.model.free
        cov = np.zeros((n, n))
        cov[np.ix_(free, free)] = b.cov_free
        return cov

    def _structure_fit(self, b, point, cov):
        """The loop.StructureFitResult of ``b`` at ``point``, built from the engine's own point and
        covariance (no refit), so the verdict and the error layers attach to it."""
        model, ctx = b.model, self.ctx
        names = model.names
        prob = loop.build_problem(model.hyp, model.cctx, x0=dict(zip(names, point)))
        prob.zmap = None            # this Problem only maps the at-limit flags; the engine fitted in its own z
        zm = self.zmap(model)
        hi = np.array(model.hi, dtype=float)
        for m, (_a, g, _c) in enumerate(model.modes):          # a width's upper edge is its mode's ceiling
            if zm.mapped_mode(m):
                hi[g] = zm.ceiling(point, m)
        prob.bounds = [(float(l), float(h)) for l, h in zip(model.lo, hi)]
        free = model.free
        br = backends.BackendResult(
            x=np.asarray(point, dtype=float), success=True, message=b.message, n_eval=b.nfev,
            covariance=cov, covariance_ok=cov is not None,
            at_limit=backends._at_limit_map(point[free], [prob.bounds[i] for i in free],
                                            [loop._sanitize(names[i]) for i in free]),
            merrors=None)
        fit = loop.result_from_backend(
            str(model.hyp.label), prob.composite, prob.costv, prob.n_used, prob.k, br,
            prior_omega=getattr(ctx, "prior_omega", None),
            acoustic_omega=getattr(ctx, "acoustic_omega", None), penalty=prob.penalty,
            hard_fixed=prob.is_hard_fixed, T=getattr(ctx, "T", None), ctx=model.cctx)
        fit.identity = getattr(model.hyp, "identity", None)
        fit.variant = loop.DELTA_VARIANT if model.delta else None
        if b.status == "degenerate_twin":
            fit.resolved_to = str(b.terminal().model.hyp.label)
            fit.resolved_limit = b.limit
        loop._attach_fit_semantics(fit, prob, model.cctx)
        if fit.fit_semantics is not None:      # the prior this engine used, not the recipe default
            fit.fit_semantics["prior_recipe"] = self.sp.provenance()
        return fit

    def _model_result(self, b, probability, kept=None):
        """``kept``: the label priced for ``b``'s group when ``b`` is superseded, else None."""
        model = b.model
        names, free = model.names, model.free
        priced = b.status in _PRICED
        cov = self._full_cov(b)
        fitted = b.theta is not None
        point = b.theta_map if priced else b.theta
        terminal = b.terminal() if b.status == "degenerate_twin" else None
        reason = b.reason
        if kept is not None:
            note = f"superseded: {kept} priced for its group"
            reason = f"{reason}; {note}" if reason else note
        v = model.variant
        return ModelResult(
            model_id=b.model_id, label=str(model.hyp.label), n_peaks=model.variant.k,
            elastic=model.variant.elastic, background=model.variant.background,
            delta_modes=tuple(model.delta), ln_model_prior=b.ln_model_prior,
            evidence_status=b.status, ln_z=b.ln_z if priced else None, ln_like=b.ln_like,
            ln_prior=b.ln_prior if priced else None, cost=b.cost,
            twin_of=None if terminal is None else terminal.model_id,
            n_basins=1 if priced else 0, reason=reason,
            params=({n: float(v) for n, v in zip(names, b.theta)} if fitted else {}),
            map_params=({n: float(v) for n, v in zip(names, b.theta_map)} if priced else None),
            free=tuple(names[i] for i in free), cov=cov, probability=float(probability),
            k_free=int(free.size), pinned=tuple(b.pinned) if priced else (),
            boundary_method=b.boundary_method if priced else "",
            posterior_iterations=b.posterior_iterations, hypothesis=model.hyp,
            fit=self._structure_fit(b, point, cov) if fitted else None,
            qe=v.qe, on_demand=v.on_demand, removed=tuple(v.removed), superseded=kept is not None,
            parent=model.parent)

    def _qe_result(self, b):
        """The selected basin's central QE at its posterior mode, or None (no QE, or nothing priced)."""
        if b is None or b.theta_map is None or b.model.qe_h is None:
            return None
        model, t = b.model, b.theta_map
        height, width = float(t[model.qe_h]), model.qe_width(t)
        cov = self._full_cov(b)

        def sd(i):
            v = None if cov is None or i is None else float(cov[i, i])
            return math.sqrt(v) if v is not None and v > 0 else None

        used = np.asarray(self.ctx.x_used, dtype=float) - float(self.ctx.center)
        return QEResult(height=height, width=width, pinned=model.qe_w is None,
                        weight=CentralQE().spectral_weight({"h": height, "width": width}, self.ctx),
                        window=(float(used.min()), float(used.max())),
                        convention=QE_WEIGHT_CONVENTION, sigma_height=sd(model.qe_h),
                        sigma_width=sd(model.qe_w))

    # ---- the selected model's peaks: values only; thresholds are applied by _classify ---------------------
    def _area_per_h(self, peak, e, g):
        _col, up = self.units([(e, g)], [peak])[0]
        return up.area_stokes / up.height, up.area_both / up.height

    def _elastic_area_per_h(self):
        """The elastic line's area (counts*meV) per unit height: the resolution profile has unit
        area, ``Elastic`` scales it to unit height (same convention as the phonon areas, no exposure)."""
        curve = getattr(self.ctx, "resolution_curve", None)
        if curve is not None:
            return 1.0 / float(np.max(curve.values))
        return voigt_area_for_height(1.0, self.ctx.gauss_fwhm, self.ctx.lorentz_fwhm)

    def _peaks(self, b):
        """(PeakResult values, the plain statistics ``_classify`` reads) of the selected basin ``b``.
        ``confident``, ``regime``, ``manual_fit`` and the correlation-dependent statuses are
        placeholders here: ``_classify`` sets them, in the engine and in reclassify alike."""
        extras = {"peaks": [], "height_correlations": [], "area_covariance": None,
                  "elastic_area": None, "elastic_height_correlations": [],
                  "elastic_area_covariance": None,
                  "residual_scan": [], "residual_scan_max": 0.0, "saturated_deletion_peaks": []}
        if b is None or b.theta_map is None:
            return (), extras
        model, t = b.model, b.theta_map
        cov = self._full_cov(b)
        mean = self.mean(model, t)
        sd = None if cov is None else np.sqrt(np.clip(np.diag(cov), 0.0, None))
        free = model.free.tolist()
        zm = self.zmap(model)
        out, grads = [], []

        def upper(i, j):     # the mode's own upper edge of coordinate j (a width's is its ceiling)
            return zm.ceiling(t, i) if j == model.modes[i][1] and zm.mapped_mode(i) else model.hi[j]

        for i, (a, g, c) in enumerate(model.modes):
            delta = i in model.delta
            e, gam, h = float(t[a]), float(t[g]), float(t[c])
            peak = model.variant.peaks[i]
            col = self.units([(e, gam)], [peak])[0][0]
            per_s, per_b = self._area_per_h(peak, e, gam)

            def sigma(j):
                return float(sd[j]) if sd is not None and sd[j] > 0 else None

            corr = None
            if cov is not None and not delta and sd[a] > 0 and sd[g] > 0:
                corr = float(cov[a, g] / (sd[a] * sd[g]))
            sig_area = sig_area_both = None
            grad_s, grad_b = np.zeros(len(t)), np.zeros(len(t))
            if cov is not None:
                grad_s[c], grad_b[c] = per_s, per_b
                for j, val in ((a, e), (g, gam)):
                    if j not in free:
                        continue
                    step = 1e-4 * max(1.0, abs(val))
                    step = -step if val + step > upper(i, j) else step   # inward at the bound
                    moved = (e + step, gam) if j == a else (e, gam + step)
                    ds, db = self._area_per_h(peak, *moved)
                    grad_s[j], grad_b[j] = h * (ds - per_s) / step, h * (db - per_b) / step
                sig_area = float(math.sqrt(max(grad_s @ cov @ grad_s, 0.0)))
                sig_area_both = float(math.sqrt(max(grad_b @ cov @ grad_b, 0.0)))
            grads.append(grad_s)
            reduced = mean - self.expo * h * col
            with np.errstate(divide="ignore", invalid="ignore"):
                terms = np.where(mean > 0, mean * np.log(mean / reduced) - (mean - reduced), 0.0)
            impact = float(2.0 * np.sum(terms))
            if not math.isfinite(impact):          # a zero reduced mean under counts: infinite grade
                impact = float(np.finfo(float).max)
                extras["saturated_deletion_peaks"].append(i)

            def status(j, key):
                if cov is None:
                    return "undetermined"
                if key == "Gamma" and delta:
                    return "boundary_limited"
                if model.names[j] in b.pinned:
                    return "boundary_limited"
                if sd[j] >= (upper(i, j) - model.lo[j]) / math.sqrt(12.0):
                    return "undetermined"
                return "constrained"

            out.append(PeakResult(
                index=i, omega0=e, Gamma=gam, delta=delta, height=h,
                area_stokes=h * per_s, area_both=h * per_b,
                sigma_omega0=sigma(a), sigma_Gamma=None if delta else sigma(g),
                sigma_height=sigma(c), sigma_area=sig_area, sigma_area_both=sig_area_both,
                area_status="unavailable" if cov is None else "usable",
                corr_omega0_Gamma=corr, regime="underdamped",
                status={"omega0": status(a, "omega0"), "Gamma": status(g, "Gamma"),
                        "height": status(c, "height"), "area": status(c, "area")},
                confident=False, deletion_impact=impact, manual_fit=False,
                at_width_cap=zm.at_cap(t, i)))
            extras["peaks"].append({"omega0": e, "Gamma": gam, "deletion_impact": impact,
                                    "corr_omega0_gamma": corr})
        if cov is not None and grads:                  # count 0 has no peak to correlate
            hc = [m[2] for m in model.modes]
            extras["height_correlations"] = [
                [i, j, float(cov[hc[i], hc[j]] / (sd[hc[i]] * sd[hc[j]]))]
                for i in range(len(hc)) for j in range(i + 1, len(hc))
                if sd[hc[i]] > 0 and sd[hc[j]] > 0]
            G = np.array(grads)
            extras["area_covariance"] = (G @ cov @ G.T).tolist()
            el = model.el
            if el is not None and sd[el] > 0:     # the elastic line's area trades with a soft mode's
                a_el = self._elastic_area_per_h()
                extras["elastic_height_correlations"] = [
                    float(cov[hc[i], el] / (sd[hc[i]] * sd[el])) if sd[hc[i]] > 0 else None
                    for i in range(len(hc))]
                extras["elastic_area_covariance"] = (
                    [float(v) for v in a_el * (G @ cov[:, el])] + [float(a_el ** 2 * cov[el, el])])
        if model.el is not None:
            extras["elastic_area"] = float(t[model.el]) * self._elastic_area_per_h()
        extras["residual_scan"] = self._residual_scan(mean)
        extras["residual_scan_max"] = max((w[2] for w in extras["residual_scan"]), default=0.0)
        return tuple(out), extras

    def _residual_scan(self, mean):
        """Every one-FWHM window of the signed deviance, both energy signs: [e_lo, e_hi,
        (sum r)^2 / N] for the window centred on each kept point; ``_classify`` thresholds and
        merges them."""
        order = np.argsort(self.E)
        e, r = self.E[order], poisson_residuals(self.y, mean)[order]
        csum = np.concatenate(([0.0], np.cumsum(r)))
        half = 0.5 * self.fwhm + 1e-9
        lo, hi = np.searchsorted(e, e - half, "left"), np.searchsorted(e, e + half, "right")
        stat = (csum[hi] - csum[lo]) ** 2 / (hi - lo)
        return [[float(e[a]), float(e[b - 1]), float(v)] for a, b, v in zip(lo, hi, stat)]

    # ---- tally and result ------------------------------------------------------------------------------
    def conclude(self) -> FastPassResult:
        """Steps 9-11 from whatever the search reached: tally, states, peaks, ModelResults."""
        s = self.s
        self.emit("classify")
        with self._stage("classify"):
            for b in self.basins():
                if b.status == "pending":
                    b.status = "no_evidence"
                    b.reason = "not priced"
            # A successful sibling at the same count must not hide an unfitted variant.
            # Admit each reached base variant and retain already-built delta models, without
            # enumerating masks the fast search never considered. The deep pass needs no fit.
            for k in sorted(self.reached):
                for v in self.variants[k]:
                    self.model(v, ())
            for m in self.models.values():
                if m is not None and not m.basins:
                    k = m.variant.k
                    m.basins.append(_Basin(m, 0, None, None, None,
                                           _ln_model_prior(len(self.variants[k]), k), "",
                                           status="not_fitted",
                                           reason=f"no start converged for model {m.hyp.label}"))
            basins = sorted(self.basins(), key=lambda b: (b.count, b.model.variant.elastic,
                                                          b.model.variant.background,
                                                          b.model.variant.qe,
                                                          b.model.delta, b.index))
            superseded = _superseded(basins)
            tally = _tally(basins, s.contending_margin, superseded)
            prob = {id(b): (math.exp(tally.terms[id(b)] - tally.total) if id(b) in tally.terms else 0.0)
                    for b in basins}
            spreads = []
            for b in basins:
                if prob[id(b)] >= s.material_p and b.objective is not None:
                    zm = self.zmap(b.model)
                    lap = self.laplace(b, b.objective, zm.to_z(b.theta_map), zm.lo, zm.hi,
                                       rel_step=3e-4)
                    if lap.status == "no_evidence":
                        log.info("fast pass: %s has no evidence at the wider step (%s)",
                                 b.model_id, lap.reason)
                    else:
                        b.spread = abs(b.ln_z - float(lap.ln_z))
                        spreads.append(b.spread)
            spread = max(spreads, default=0.0)
            selected = tally.selected
            preferred = tally.preferred
            reason = "ceiling" if preferred is not None and preferred == s.mode_ceiling else ""
            results = [self._model_result(b, prob[id(b)], superseded.get(id(b))) for b in basins]
            by_id = {id(b): r for b, r in zip(basins, results)}
            peaks, extras = self._peaks(selected)
            t_map = None if selected is None else selected.theta_map
            sel_model = None if selected is None else selected.model
            # every other priced non-twin basin, and the best basin of each unpriced contender
            # (evidence_gap None): what the survival test and the ambiguity flag compare against
            alternatives = []
            for b in basins:
                if b is not selected and id(b) in tally.terms:
                    alternatives.append(self._alternative(b, b.theta_map, prob[id(b)],
                                                          tally.terms[id(selected)] - tally.terms[id(b)]))
            for k in tally.unpriced_contenders:
                best = max((b for b in basins if b.count == k and b.theta is not None
                            and id(b) not in superseded
                            and b.status not in ("degenerate_twin", "not_fitted")),
                           key=lambda b: b.ln_like, default=None)
                if best is not None:
                    alternatives.append(self._alternative(best, best.theta, 0.0, None))
            flag_statistics = {
                "max_count": float(self.sp.max_count), "fwhm": float(self.fwhm),
                "count_ln_z": {int(k): float(v) for k, v in tally.count_ln_z.items()},
                "unpriced_contenders": [int(k) for k in tally.unpriced_contenders],
                "dismissed_counts": tally.dismissed_counts,
                "evidence_spread_nats": float(spread), "alternatives": alternatives, **extras}
            c = _classify(s, [asdict(p) for p in peaks], flag_statistics, tally.probabilities,
                          preferred, reason)
            flag_statistics["anchor"] = c["anchor"]
            peaks = tuple(replace(p, **u) for p, u in zip(peaks, c["peaks"]))
        timing = dict(self.timing)
        timing["total"] = time.perf_counter() - self.start
        timing["families"] = {k: dict(v) for k, v in self.families.items()}
        timing["seed_count"] = self.seed_count
        return FastPassResult(
            preferred_count=preferred,
            count_probabilities={int(k): float(v) for k, v in sorted(tally.probabilities.items())},
            search_limit_reason=reason, models=tuple(results),
            selected=None if selected is None else by_id[id(selected)], peaks=peaks,
            elastic_height=(None if sel_model is None or sel_model.el is None
                            else float(t_map[sel_model.el])),
            background=(None if sel_model is None or sel_model.bg is None
                        else float(t_map[sel_model.bg])),
            flag_statistics=flag_statistics, prior_provenance=self.sp.provenance(),
            arena_fingerprint=loop.data_likelihood_fingerprint(self.ctx),
            ln_like_saturated=self.sat, settings=s, timing=timing, n_fits=self.n_fits,
            qe=self._qe_result(selected), **_apply(c))

    def _alternative(self, b, theta, probability, gap):
        omega0 = sorted(float(theta[a]) for a, _g, _c in b.model.modes)
        return {"model_id": b.model_id, "family": b.family, "probability": float(probability),
                "evidence_gap": None if gap is None else float(gap), "omega0": omega0}


def run_fast_pass(ctx, *, settings: FastPassSettings | None = None, peaks=(), structures=None,
                  progress: ProgressFn | None = None) -> FastPassResult:
    """The fast pass on one spectrum (C-fastpass). ``peaks``: card peak hints, extra starts only;
    ``structures``: a whitelist of catalogue labels (None: every count structure); ``progress``: a
    C-progress callback (never changes the result). Raises ``ValueError`` only for an unusable
    context (no finite counts, no positive energy, no resolution)."""
    engine = _Engine(ctx, settings or FastPassSettings(), peaks=peaks, structures=structures,
                     progress=progress)
    engine.run()
    return engine.conclude()


def _apply(c: dict) -> dict:
    """The recomputed fields of ``_classify`` in FastPassResult form."""
    return dict(state=c["state"], probabilities_conditional=c["probabilities_conditional"],
                close_call_alternatives=c["close_call_alternatives"],
                numerically_marginal=c["numerically_marginal"],
                deep_pass_candidate=c["deep_pass_candidate"],
                flags=tuple(SpectrumFlag(**f) for f in c["flags"]),
                grouped_intensity=tuple(c["grouped_intensity"]))


def reclassify(result: FastPassResult, settings: FastPassSettings) -> FastPassResult:
    """Re-apply the post-search thresholds (RECLASSIFIABLE) with no refit and no mutation.
    ValueError naming the field when anything else in ``settings`` differs from the search's."""
    _refuse_changed_search(_settings_dict(result.settings), _settings_dict(settings))
    c = _classify(settings, [asdict(p) for p in result.peaks], result.flag_statistics,
                  result.count_probabilities, result.preferred_count, result.search_limit_reason)
    peaks = tuple(replace(p, **u) for p, u in zip(result.peaks, c["peaks"]))
    return replace(result, peaks=peaks, settings=settings,
                   flag_statistics={**result.flag_statistics, "anchor": c["anchor"]}, **_apply(c))


def reclassify_record(record: dict, settings: FastPassSettings) -> dict:  # (new) on a C-fastpass-record
    """:func:`reclassify` on a saved record dict (C-fastpass-record): a new dict, the input untouched,
    with ``state``, ``close_call_alternatives``, ``deep_pass_candidate``, ``flags`` and each peak's
    ``confident``, ``regime``, ``manual_fit`` and ``status`` recomputed from ``flag_statistics``."""
    if "settings" not in record:
        raise ValueError("fast pass: the record carries no settings to compare")
    _refuse_changed_search(_plain(record["settings"]), _settings_dict(settings))
    out = copy.deepcopy(record)
    c = _classify(settings, out["peaks"], out["flag_statistics"], out["count_probabilities"],
                  out["preferred_count"], out["search_limit_reason"])
    for peak, u in zip(out["peaks"], c["peaks"]):
        peak.update(u)
    out["flag_statistics"]["anchor"] = c["anchor"]
    out.update(state=c["state"], probabilities_conditional=c["probabilities_conditional"],
               close_call_alternatives=list(c["close_call_alternatives"]),
               numerically_marginal=c["numerically_marginal"],
               deep_pass_candidate=c["deep_pass_candidate"], flags=c["flags"],
               grouped_intensity=[dict(zip(("e_lo", "e_hi", "intensity", "sigma"), g))
                                  for g in c["grouped_intensity"]],
               settings=_settings_dict(settings))
    return out

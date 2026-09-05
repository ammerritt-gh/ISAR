"""Expected-information-gain scan planner over a hypothesis map and an intensity map.

The planner answers one question: *given what the branch beliefs say now, which (Q, energy window,
exposure) buys the most information about the declared targets per neutron?*  There is no survey,
no opening book and no placement rule -- a wide window happens only because a belief is wide, and a
scan is worth nothing at a grid point whose target energy is already known to the requested bar.

Everything here is pure, deterministic and I/O-free: no RNG, no file access, no printing.  The
module is drive-side, so it must never import :mod:`isar.session` (see the ``classify_eig``
docstring); ``NEW_LEVEL`` and ``ASSIGNMENT_ENTROPY_STOP_NATS`` are copied from the session layer for
that reason.

Two different questions are asked of the same fitted peaks, and they normalise differently:

* **which branch is this peak?** -- :func:`assign`, a distribution over the modelled branches plus a
  flat "new" escape, computed independently per peak.  It decides the *belief anchor*: whose
  dispersion the peak is evidence for, and how strongly.
* **which peak, if any, IS the target here?** -- the target-energy posterior, one mutually exclusive
  mixture over the point's peaks plus "none of them", summing to one.  It decides the *prize*.

The prize is stated the way the scientist states it: the target's energy known to +-tau at one
sigma, on a peak that has been identified as the target.  Width is the half-width of the 68.27%
highest-density interval of that posterior, so a sharp bimodal posterior -- two rival peaks, 50/50 --
fails the bar however narrow each mode is; identity is ``sum_j w_j``, the mass that the target is one
of the fitted peaks at all.  The deficit at a point is
``max(log(hw / tau), 0) + max(log(CONFIRMED_PROBABILITY / sum_j w_j), 0)`` and the prize is met when
every deficit is zero.

Names the loop reads.  ``RankedDesign.neutrons`` is the requested dose, ``n_points * ncount``;
``RankedDesign.price`` is that plus ``PlannerInput.scan_overhead_price``, the declared exchange value
of one scan's fixed cost, and ``score`` is expected completions per unit price.  ``shaping_nats`` is
the log-deficit a design removes and orders only the designs that complete nothing.
``Decision.ranked`` holds EVERY design in rank order, chosen first; the caller decides how many to
log.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Mapping, Sequence

import numpy as np
from numpy.polynomial.hermite import hermgauss
from scipy.special import erf, ndtr

from isar.drive.acquisition.classify_eig import belief_predict

__all__ = [
    "Candidate", "ExecutedScan", "IntensityScale", "PlannerInput", "PointState", "RankedDesign",
    "Decision", "plan_next", "assign", "identity_entropy", "anchor_admitted",
    "merge_candidate", "upsert_anchor",
    "fisher_sigma_meV", "branch_counts", "expected_counts", "detection_significance",
    "DETECT_FLOOR", "hdi_halfwidth",
    "target_posterior", "point_presence", "banked_exposure", "pooled_delivery", "pooled_significance",
    "miss_factor", "FALSE_IDENTIFICATION", "PRESENCE_PRIOR_ODDS",
    "joint_assign", "_pad_candidates",
    "default_intensity_scale", "NEW", "NEW_LEVEL", "ASSIGNMENT_ENTROPY_STOP_NATS",
    "CONFIRMED_PROBABILITY",
]

NEW = "new"
#: Flat "something new / spurious" likelihood floor, per meV.  Copied from
#: ``isar.session.branch_assignment.NEW_LEVEL`` -- the drive may not import the session package.
NEW_LEVEL = 0.01
#: A fitted peak becomes a belief anchor only when its identification is confident, by the same
#: two-part gate the session applies in ``branch_tracking._frontier_states`` before a mode enters the
#: dispersion prior: ``identity_entropy <= assignment_entropy_stop_nats`` AND the branch's own
#: probability at or above ``CONFIRMED_PROBABILITY``.  The session deliberately measures the entropy
#: of *which branch* (renormalised over the branches) and guards the "something new" mass with the
#: probability floor instead; folding ``new`` into the entropy as well double-counts it, and with a
#: 1% flat floor that alone costs ~0.05 nats, enough that no cold-start scan could ever anchor
#: anything and the campaign would never bootstrap.
ASSIGNMENT_ENTROPY_STOP_NATS = 0.2
CONFIRMED_PROBABILITY = 0.85
KB_MEV_PER_K = 0.08617
FWHM_TO_SIGMA = 1.0 / 2.3548
HDI_MASS = 0.6827

SCENARIO_ORDER = 8
WINDOW_K_FACTORS = (1.5, 2.5, 4.0)
HULL_K = 2.5
#: Two peaks at one grid point closer than this many combined sigmas are the same physical peak
#: re-measured, and combine by inverse variance rather than standing as rival mixture components.
MERGE_SIGMA_FACTOR = 3.0
#: Fisher eigenvalues below this fraction of the largest are null-space directions (the data
#: carry no information along them); a parameter with more than _FISHER_NULL_WEIGHT of its
#: weight on such a direction is unidentifiable and therefore unmeasured by that scan.
_FISHER_RCOND = 1e-10
_FISHER_NULL_WEIGHT = 1e-6
#: Relative tolerance for "the same score" when breaking ranking ties.
TIE_REL = 1e-12
#: Expected counts a null scan's window must have held for it to be evidence against a branch
#: energy inside that window.
#: Expected significance a branch's peak must reach for a scan to MEASURE it -- and, equally, for
#: that scan's silence to be evidence the branch is not there.  A mirror of
#: ``isar.analyze.verdict.DETECT_FLOOR``, whose ``detection_power`` computes ``A / sqrt(A + B)``
#: with ``A`` the peak strength in counts on the fitted background ``B`` per point and calls the
#: peak detectable at 3 sigma.  Copied rather than imported because the drive may not import the
#: analyze package (the same rule as ``NEW_LEVEL``), so the two MUST NOT DRIFT: the planner's whole
#: job is to buy scans the real fitter will deliver, and its previous cruder gate -- 5 integrated
#: counts anywhere in the window -- said the counts sufficed where the fitter then abstained,
#: 20 scans out of 26 at one live grid point returning "underpowered / abstain" with no candidate
#: while the planner re-bought the identical design.
DETECT_FLOOR = 3.0
#: The declared probability that the fitter misses a peak it should have detected.  A null scan is
#: evidence, not proof, so a contradicted scenario is down-weighted by this factor rather than
#: killed: this is a model parameter of the fitter's reliability, not a placement rule.  Two nulls
#: over the same band multiply, so the band's weight falls; posterior mass flows back to it once the
#: rest of the domain has returned nulls too, which is what brings the planner back at higher
#: exposure instead of leaving the band excluded for ever.
NULL_MISS_PROBABILITY = 0.1
#: The declared rate at which a fitted peak is assigned to a branch that is not there.  The mirror
#: of :data:`NULL_MISS_PROBABILITY`: one says how often a real peak is missed, this says how often
#: an absent one is claimed.  A model parameter of the fitter's reliability, not a placement rule --
#: and it is what lets a detection *restore* presence a run of misses took away.
FALSE_IDENTIFICATION = 0.05
#: Prior odds that a DECLARED branch is present at a grid point before anything is looked for.
#: Not 1: the hypothesis map declaring a branch is itself the statement that the branch exists along
#: the path, so before looking, absence is the unlikely case; what the class prior does not assert
#: is WHERE, which is the scenarios' business.  Measured at 1 (an even prior), an unexamined branch
#: sits at presence 0.5 -- a standing halving of its claim on its own peak that no measurement can
#: lift, because a branch outside every window scanned so far never accrues evidence either way --
#: and `--target both --tolerance 0.05` looped on it, 13 takes of one design at h=2.15.
PRESENCE_PRIOR_ODDS = 9.0
#: The prize is binary per point -- known to +-tau and identified, or not -- so utility counts
#: COMPLETIONS and nothing else.  The log-deficit removed is carried beside it as
#: ``RankedDesign.shaping_nats`` and used only to order designs that complete nothing: a genuine
#: tie-breaker now, not a weighted term that could ever be traded against a completion.  A weighted
#: term let a half-count scan leaving sigma just over the bar score ~99% of the value of the
#: full-count scan that would finish the point, at half the price, so the point never got finished.
#: How far a real fit's sigma sits above the Cramer-Rao bound at the counts we run.  The bound is
#: optimistic at low counts; this is a declared model of the fitter's reliability, like
#: :data:`NULL_MISS_PROBABILITY`, and it applies to the PLANNER's expectations only -- the world
#: delivers the bare bound, so the planner has to earn its margin rather than assume it.
FIT_SIGMA_INFLATION = 1.5   # live 2026-09-03: published calibrated halfwidths 0.06-0.09 meV against
                            # a Cramér-Rao bound of ~0.02-0.03 at 5e6 counts/point (inflation-calibrated
                            # intervals, not the raw HESSE sigma); 1.25 bought scans that could not finish
#: Floor on the identified mass in :func:`_deficit`, so an unmeasured point (no peaks at all, mass
#: exactly zero) scores a large finite deficit rather than an infinity that would poison every sum.
IDENTIFIED_FLOOR = 1e-6
#: Anchor weights are rounded to this many decimals before designs are grouped for the cross-point
#: term (A4 caching).  A belief is insensitive to a 0.005 change in one anchor weight, and grouping
#: is what keeps the per-design cross term inside the time budget.
ANCHOR_WEIGHT_DECIMALS = 2

#: The intensity map's class prior, before any scan: counts per ``PRIOR_NCOUNT_REFERENCE`` neutrons.
PRIOR_NCOUNT_REFERENCE = 1.0e7
PRIOR_PEAK_COUNTS_MEV = 1.0    # PRE-BOSE Stokes area per 1e7: the live TAVI runs of 2026-09-03 measured
                                # 0.65 (a lobe of ~13 counts*meV at 0.9 meV is that times n+1 = 19.7);
                                # 15 was the Bose-weighted lobe and made the rival 20x too bright
PRIOR_ELASTIC_COUNTS_MEV = 30.0
PRIOR_BACKGROUND_PER_POINT = 0.3

_x8, _w8 = hermgauss(SCENARIO_ORDER)
_GH8_X = np.asarray(_x8, dtype=float)
_GH8_W = np.asarray(_w8, dtype=float) / float(np.sum(_w8))
_SQRT2 = math.sqrt(2.0)
_SQRT2PI = math.sqrt(2.0 * math.pi)


# --------------------------------------------------------------------------- contracts

@dataclass(frozen=True)
class Candidate:
    """One fitted peak at a grid point (a ``results.csv`` branch candidate)."""

    energy_meV: float
    sigma_meV: float
    probabilities: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutedScan:
    """One scan already executed at a grid point: where it looked and for how long.

    Negative evidence is per branch: a band the pooled master has looked into deeply and found
    empty is evidence that branch is not there, exactly as a delivered peak is evidence that it is.

    A scan carries only what it asked of the instrument -- where it looked and how long.  Whether a
    branch was DELIVERED is deliberately not on it: ISAR pools every scan at a q into a master and
    fits the master, so both halves of the evidence read the point's pooled candidates, never one
    scan's own fit.  What each scan contributes here is its share of the exposure over a given
    energy, which is what decides whether the master could have seen the branch at all.
    """

    e_lo: float
    e_hi: float
    ncount: int


@dataclass(frozen=True)
class IntensityScale:
    """The intensity map's count scale, per one neutron of ``ncount``."""

    peak_counts_meV: Mapping[str, float]   # branch -> integrated Stokes-lobe counts*meV, pre-Bose
    elastic_counts_meV: float
    background_per_point: float


@dataclass(frozen=True)
class PlannerInput:
    path: tuple[tuple[float, float, float], ...]
    u: tuple[float, ...]
    targets: tuple[str, ...]
    beliefs: Mapping[str, Mapping]
    measured: Mapping[int, Sequence[Candidate]]
    energy_domain_meV: tuple[float, float]
    fwhm_meV: tuple[float, ...]
    temperature_K: float
    scale: IntensityScale
    tolerance_meV: float
    base_ncount: int = 10_000_000
    #: Exposure rungs as multiples of base_ncount. The top rungs exist because a dim peak far from
    #: the fold (pre-Bose ~0.6 counts*meV per 1e7 on TAVI, n+1 ~ 1.4 at 6 meV) needs ~1e8
    #: neutrons per point to reach the requested energy bar at all; the instrument cap still clamps.
    ncount_ladder: tuple[float, ...] = (0.5, 1.0, 2.0, 4.0, 8.0, 16.0)
    #: The declared exchange value of one scan's fixed cost (the instrument's move and the fit's
    #: analysis time) in neutron-equivalents; time is the honest currency and this is its declared
    #: rate, a price, not a placement rule. Without it sixteen 5M scans and one 8e7 scan price
    #: identically and the planner grinds a dim point in tiny repeats. The loop may pass the
    #: instrument's own figure.
    scan_overhead_price: float = 5.0e7
    max_points: int = 220
    step_fraction: float = 0.25
    scans: Mapping[int, Sequence[ExecutedScan]] = field(default_factory=dict)
    #: Grid indices whose master ABSTAINED as ``underpowered``: the fitter said "I could not see it
    #: at this exposure", not "there is nothing here". Its counts still bank, but its silence is
    #: not negative evidence -- read as a null it pushed the branch's mass out of the very window
    #: it sits in and bought an 8 meV scan to find it again (eig-2-test, h = 2.30, 2026-09-03).
    abstained: Mapping[int, bool] = field(default_factory=dict)


@dataclass(frozen=True)
class PointState:
    grid_index: int
    hkl: tuple[float, float, float]
    u: float
    exempt: bool
    halfwidth_meV: float          # 68.27% HDI half-width of the target-energy posterior
    identified_mass: float        # sum_j w_j: the mass that the target IS one of the fitted peaks
    deficit_nats: float
    within_bar: bool
    n_candidates: int


@dataclass(frozen=True)
class RankedDesign:
    grid_index: int
    hkl: tuple[float, float, float]
    e_lo: float
    e_hi: float
    e_step: float
    n_points: int
    ncount: int
    utility_nats: float           # expected completions: the primary objective
    shaping_nats: float           # log-deficit removed; orders designs that complete nothing
    neutrons: float               # the requested dose, n_points * ncount
    price: float                  # neutrons + the scan's fixed price
    score: float                  # utility_nats / price, the primary ranking
    expected_sigma_fit_meV: Mapping[str, float]
    expected_assignment: Mapping[str, float]
    e_pred_meV: Mapping[str, tuple[float, float]]


@dataclass(frozen=True)
class Decision:
    stop: bool
    stop_reason: str
    chosen: RankedDesign | None
    #: EVERY design considered, in rank order with the chosen one first -- typically a few hundred
    #: at one grid point and a few thousand across a path. The caller decides how many to log.
    ranked: tuple[RankedDesign, ...]
    per_point: tuple[PointState, ...]
    #: Set when the prize is unmet but no design is expected to complete a point: the cheapest
    #: design is taken as a probe and the campaign feed says so, instead of a silent zero.
    note: str = ""


def default_intensity_scale(branches: Sequence[str]) -> IntensityScale:
    """The class-prior intensity map: what is believed before a single scan has been fitted."""
    return IntensityScale(
        peak_counts_meV={str(b): PRIOR_PEAK_COUNTS_MEV / PRIOR_NCOUNT_REFERENCE for b in branches},
        elastic_counts_meV=PRIOR_ELASTIC_COUNTS_MEV / PRIOR_NCOUNT_REFERENCE,
        background_per_point=PRIOR_BACKGROUND_PER_POINT / PRIOR_NCOUNT_REFERENCE)


# --------------------------------------------------------------------------- small numerics

def _norm_pdf(x, mean, sigma):
    sigma = np.maximum(sigma, 1e-12)
    z = (x - mean) / sigma
    return np.exp(-0.5 * z * z) / (sigma * _SQRT2PI)


def _bose(omega, temperature_K: float, floor: float):
    """Bose occupation; the omega -> 0 divergence is capped at ``floor`` (a quarter resolution)."""
    return 1.0 / np.expm1(np.maximum(omega, floor) / (KB_MEV_PER_K * float(temperature_K)))


def hdi_halfwidth(energies, sigmas, weights, w_none, width: float, *, iterations: int = 32):
    """Half-width of the 68.27% HDI of ``sum_j w_j N(e_j, s_j) + w_none U[0, width]``.

    The interval is found by bisecting the density *level* rather than scanning a grid: at level c
    each Gaussian component contributes the interval where it, plus the flat floor, exceeds c, and
    that interval's mass and width are both closed forms.  A 2000-point grid scan is the textbook
    recipe (and is what the tests check this against), but it costs a full density evaluation for
    every design and scenario, which does not fit the sub-3-second decision budget.  The two agree
    wherever the components are separated -- which they are, since same-peak candidates are combined
    by :func:`merge_candidate` -- and where they overlap the level-set form double-counts the shared
    tails and so reports a *wider* interval, the conservative direction for a bar.

    The half-width reported is half the *extent* of the interval set -- from the bottom of its
    lowest interval to the top of its highest -- not half the measure.  That is what makes a bimodal
    posterior fail: "the energy is either 4.0 or 6.0, each to +-0.02" is not the energy known to
    +-0.05, and only the extent says so.  For a single mode the two agree.
    """
    e = np.asarray(energies, dtype=float)
    s = np.maximum(np.asarray(sigmas, dtype=float), 1e-12)
    w = np.clip(np.asarray(weights, dtype=float), 0.0, None)
    floor = np.asarray(w_none, dtype=float) / float(width)
    if e.shape[-1] == 0:
        # No components: the posterior is the flat floor alone, whose 68.27% HDI is any
        # interval holding that mass -- an extent of HDI_MASS * width, consistent with the
        # flat-dominated limit of the bisection below.
        return np.full(e.shape[:-1], 0.5 * HDI_MASS * float(width))
    peak = w / (s * _SQRT2PI)
    # Bisect the level in LOG space of its excess over the flat floor.  A flat-dominated
    # posterior (a negligible peak on a uniform) needs a level a few hundred decades above
    # the floor before the set holds 68% of the mass; a linear bisection cannot reach it in
    # any number of iterations and reported an interval ~4x too narrow -- a phantom
    # information gain worth ~3 nats to any scan that measured nothing useful.
    log_hi = np.log(np.maximum(np.max(peak, axis=-1), 1e-300))
    log_lo = np.full(log_hi.shape, math.log(1e-300))

    def measure(above):
        """(mass, measure, extent) of the super-level set at level ``floor + above``.

        ``above`` -- the level's excess over the flat floor -- is passed on its own and never
        added to the floor: the excess a flat-dominated set needs is hundreds of decades below
        the floor and would be absorbed by the addition, stalling the bisection.
        """
        above = np.maximum(above[..., None], 1e-300)
        r = s * np.sqrt(2.0 * np.log(np.minimum(np.maximum(peak / above, 1.0), 1e300)))
        size = np.minimum(2.0 * np.sum(r, axis=-1), float(width))
        mass = np.sum(w * erf(r / (s * _SQRT2)), axis=-1) + floor * size
        live = r > 0.0
        top = np.max(np.where(live, e + r, -np.inf), axis=-1)
        bottom = np.min(np.where(live, e - r, np.inf), axis=-1)
        extent = np.where(live.any(axis=-1), np.minimum(top - bottom, float(width)), float(width))
        return mass, size, np.maximum(extent, size)

    for _ in range(iterations):
        mid = 0.5 * (log_lo + log_hi)
        mass, _size, _extent = measure(np.exp(mid))
        too_much = mass > HDI_MASS
        log_lo = np.where(too_much, mid, log_lo)
        log_hi = np.where(too_much, log_hi, mid)
    _mass, _size, extent = measure(np.exp(0.5 * (log_lo + log_hi)))
    return 0.5 * extent


def target_posterior(energies, sigmas, valid, mus, sigs, target_index: int, absent=None,
                     presence=None):
    """Weights of the mutually exclusive "which peak, if any, IS the target" mixture.

    Returns ``(w_j, w_none)``, summing to one: the joint peak <-> branch matching gives the target
    branch at most one peak, so its marginal over the peaks plus "the target is not among them" is
    already a proper distribution.  Scoring the target against its own belief alone -- one peak
    against one flat ``NEW_LEVEL`` rival -- would call the acoustic peak "the optic, known to
    +-0.02 meV" wherever the optic belief is still broad, because a flat 0.01/meV cannot outweigh a
    Gaussian whose own peak density is only 0.15/meV.  The rival branch's claim on that peak is the
    evidence that settles it, and that lives in the matching.
    """
    e = np.asarray(energies, dtype=float)
    s = np.asarray(sigmas, dtype=float)
    like = np.stack([np.where(valid, _norm_pdf(e, np.asarray(mu)[..., None],
                                               np.sqrt(np.asarray(sig)[..., None] ** 2 + s ** 2)),
                              0.0) for mu, sig in zip(mus, sigs)], axis=-2)
    if presence is not None:
        like = like * np.asarray(presence, dtype=float)[..., :, None]
    marg, _p_new = _joint_marginals(like, valid, absent)
    w = marg[..., target_index]
    return w, np.clip(1.0 - w.sum(axis=-1), 0.0, 1.0)


# --------------------------------------------------------------------------- assignment

def _assign_weights(e, sigma, mus, sigs):
    """P(branch | peak) for every branch, vectorised.  Returns ``(..., J, B)``."""
    total = np.full(np.shape(e), NEW_LEVEL, dtype=float)
    per = []
    for mu, sg in zip(mus, sigs):
        d = _norm_pdf(e, mu, np.sqrt(sg * sg + np.asarray(sigma, dtype=float) ** 2))
        per.append(d)
        total = total + d
    return np.stack(per, axis=-1) / total[..., None]


def assign(candidate_energy: float, candidate_sigma: float, u: float,
           beliefs: Mapping[str, Mapping], branches: Sequence[str], *,
           domain_hi: float | None = None) -> dict[str, float]:
    """P(branch | peak) for a peak at ``candidate_energy`` +- ``candidate_sigma``, reduced-q ``u``.

    Equal branch priors and a flat ``NEW_LEVEL`` escape.  Exported because the world (the simulator,
    and the real evidence update) must label peaks by exactly the rule the planner scored them with.
    """
    names = tuple(str(b) for b in branches)
    preds = {b: belief_predict(dict(beliefs[b]), float(u), domain_hi=domain_hi) for b in names}
    weights = _assign_weights(np.array([float(candidate_energy)]),
                              np.array([float(candidate_sigma)]),
                              [preds[b][0] for b in names], [preds[b][1] for b in names])[0]
    out = {b: float(weights[i]) for i, b in enumerate(names)}
    out[NEW] = float(max(1.0 - sum(out.values()), 0.0))
    return out


def _injective_maps(n_branch: int, n_peak: int):
    """Every partial injective map branch -> peak index (``-1`` = that branch is not in view)."""
    out: list[tuple[int, ...]] = []

    def walk(b: int, used: frozenset, cur: list[int]) -> None:
        if b == n_branch:
            out.append(tuple(cur))
            return
        walk(b + 1, used, cur + [-1])
        for j in range(n_peak):
            if j not in used:
                walk(b + 1, used | {j}, cur + [j])

    walk(0, frozenset(), [])
    return out


def _absent_weight(mu, sig, window):
    """P(a branch is NOT in view of this window), from its own belief.

    A branch that should have appeared in the window and did not is evidence: it is why a scan that
    sees one peak near the zone centre can still say decisively *which* branch that peak is.
    """
    if window is None:
        return np.ones_like(np.asarray(mu, dtype=float))
    lo, hi = float(window[0]), float(window[1])
    sig = np.maximum(np.asarray(sig, dtype=float), 1e-9)
    inside = ndtr((hi - mu) / sig) - ndtr((lo - mu) / sig)
    return np.clip(1.0 - inside, 1e-9, 1.0)


def miss_factor(delivered: float) -> float:
    """How much a look that would have detected a branch and did not report it leaves of its claim.

    ``p + (1 - p) * NULL_MISS_PROBABILITY`` with ``p`` the probability the look DID deliver that
    branch: 1.0 when it surely did, :data:`NULL_MISS_PROBABILITY` when it surely did not, and in
    between when the identification was unsure.  The look is the pooled master's fit, so ``p`` is
    :func:`pooled_delivery`; it read one scan's ``seen`` while the backward question was still
    asked per scan.

    This weights a scenario's LOCATION -- where a branch would be if it is there -- and is a
    different question from whether it is there at all, which is :func:`point_presence`.
    """
    p = float(delivered)
    return p + (1.0 - p) * NULL_MISS_PROBABILITY


def banked_exposure(scans: Sequence[ExecutedScan], energy: float) -> float:
    """Exposure already collected at this grid point that covers ``energy``.

    ISAR pools every scan at a (q, condition) into a master and fits the MASTER; a single scan's
    own fittability is not the evidence.  So the forward question -- can a design deliver a fitted
    peak -- is asked of the pool: what is banked here already, plus what this design would add.
    Only scans whose window covers the energy count, since a scan that never looked there
    contributes nothing to that peak in the master.
    """
    return float(sum(int(scan.ncount) for scan in scans
                     if float(scan.e_lo) <= float(energy) <= float(scan.e_hi)))


def pooled_delivery(candidates: Sequence["Candidate"], branch: str, *,
                    near: float | None = None, within: float = math.inf) -> float:
    """P(the MASTER delivered ``branch``'s peak) at one grid point, read off its candidates.

    ISAR pools every scan at a (q, condition) into a master and fits the MASTER; the point's
    candidates ARE that fit.  Each candidate carries ``P(branch | peak)`` from the same joint
    identification the prize reads, so the master's delivery probability for a branch is the best
    claim any of its peaks has on it -- exactly the rule the loop already used to build one scan's
    ``seen``, asked of the pool instead of a scan.

    ``near`` restricts that to peaks within ``within`` of one energy, which is what the scenario
    weights need: "did the master deliver this branch AT 6 meV" is a different question from "did
    it deliver this branch at all", and answering the first with the second is how a distant,
    part-claimed peak protected a band the master had looked into and found empty.
    """
    return max((float(c.probabilities.get(branch, 0.0)) for c in candidates
                if near is None or abs(float(c.energy_meV) - float(near)) <= float(within)),
               default=0.0)


def pooled_significance(energies: Sequence[float], index: int, scans: Sequence[ExecutedScan], *,
                        step: float, fwhm_meV: float, temperature_K: float,
                        scale: IntensityScale, branches: Sequence[str]) -> float:
    """Detection significance of ``branches[index]`` on the exposure BANKED at its energy.

    The same test as :func:`detection_significance`, asked of the pool: every scan whose window
    covers the energy contributes its counts, because they all land in one master.  The window's
    own grid is taken from the deepest covering scan -- the peak lies inside all of them, so the
    choice only shifts which grid point samples its crest by less than one step.  No covering scan
    means the master never looked there, which is 0.0: no evidence, not weak evidence.
    """
    e = float(energies[index])
    covering = [s for s in scans if float(s.e_lo) <= e <= float(s.e_hi)]
    if not covering:
        return 0.0
    pooled = float(sum(int(s.ncount) for s in covering))
    ref = max(covering, key=lambda s: (int(s.ncount), float(s.e_hi) - float(s.e_lo)))
    n_points = max(int(math.floor((float(ref.e_hi) - float(ref.e_lo)) / step)) + 1, 1)
    return float(detection_significance(
        energies, e_lo=float(ref.e_lo), e_step=step, n_points=n_points, fwhm_meV=fwhm_meV,
        temperature_K=temperature_K, scale=scale, branches=branches, ncount=pooled)[index])


def point_presence(branches: Sequence[str], energies: Sequence[float],
                   scans: Sequence[ExecutedScan], *, step: float, fwhm_meV: float,
                   temperature_K: float, scale: IntensityScale,
                   candidates: Sequence["Candidate"] = ()):
    """P(each branch is present at this grid point at all), from the master pooled here.

    Reweighting the scenarios was only half of "a null is evidence": it moves where a branch would
    be if it is there, and never whether it is there.  So a rival that is broad, unanchored and just
    under the detection floor kept a full claim on someone else's peak no matter how often it was
    looked for and not found -- and the target's identity could never resolve, whatever was measured.

    One question, asked once.  The exposure banked at the branch's predicted energy either clears
    the fitter's own floor (:data:`DETECT_FLOOR`) or it does not.  Below it the master could not
    have seen the branch, so there is no evidence either way and presence stays at
    :data:`PRESENCE_PRIOR_ODDS`.  At or above it the master WOULD have seen it, so the odds move
    once by

        LR = [p(1 - NULL_MISS_PROBABILITY) + (1 - p) NULL_MISS_PROBABILITY]
             / [p FALSE_IDENTIFICATION + (1 - p)(1 - FALSE_IDENTIFICATION)]

    with ``p`` = :func:`pooled_delivery`, the probability the MASTER delivered that branch.

    **Why the product over scans was the wrong shape.**  It multiplied one such ratio per executed
    scan, which counted a single pooled observation many times: the master is fitted once, so ten
    scans into one pool are ten shares of one look, not ten looks.  It was wrong in both
    directions.  Forwards it over-claimed -- ten scans that each surely delivered the branch drove
    presence to certainty a single fit cannot support.  Backwards it under-claimed, and that was
    the fault that stalled a campaign: each scan was tested for detectability ALONE, so at
    h = 2.75 a master holding 4.5e7 counts over the band where the optic was believed to be, and
    showing nothing, said nothing at all -- no single 5e6 scan could have seen the optic by itself
    (solo significance 1.27 against a pooled 4.03), so presence never decayed, the belief never
    moved, and the planner topped up a pool looking in the wrong place for ever.  Pooled, the
    silence of that master is evidence exactly as its signal would have been.

    Being a function of the pool's current state rather than an accumulation, presence also moves
    in BOTH directions for free: a point whose master starts delivering its own branch again
    recovers, which the earlier monotone decay could never do.
    """
    log_odds = np.full(len(branches), math.log(PRESENCE_PRIOR_ODDS))
    for bi, b in enumerate(branches):
        significance = pooled_significance(energies, bi, scans, step=step, fwhm_meV=fwhm_meV,
                                           temperature_K=temperature_K, scale=scale,
                                           branches=branches)
        if significance < DETECT_FLOOR:
            continue
        p = pooled_delivery(candidates, b)
        present = p * (1.0 - NULL_MISS_PROBABILITY) + (1.0 - p) * NULL_MISS_PROBABILITY
        absent = p * FALSE_IDENTIFICATION + (1.0 - p) * (1.0 - FALSE_IDENTIFICATION)
        log_odds[bi] += math.log(max(present, 1e-300) / max(absent, 1e-300))
    odds = np.exp(np.clip(log_odds, -700.0, 700.0))
    return odds / (1.0 + odds)


def _joint_marginals(like, present, absent=None):
    """Per-peak branch marginals from the JOINT peak <-> branch identification.

    ``like`` is ``(..., B, J)`` with ``like[b, j] = N(e_j; mu_b, sqrt(sig_b^2 + s_j^2))``; ``present``
    is ``(..., J)``.  Each hypothesis is a one-to-one matching of branches to the peaks in view (a
    branch may be out of view, a peak may be spurious at ``NEW_LEVEL``); its weight is the product of
    the matched likelihoods.  Returns ``(marginals (..., J, B), p_new (..., J))``.

    This is what makes "reject the optic phonon" possible.  Asking of each peak separately which
    branch it is cannot use the fact that the *other* peak in the same scan is obviously the rival,
    so with a broad rival prior no single-peak scan is ever confident enough to anchor a belief and a
    cold campaign never bootstraps.  Matching both peaks at once is decisive, which is exactly why a
    scan that catches both branches is worth more than one that catches either alone.
    """
    like = np.asarray(like, dtype=float)
    n_branch, n_peak = like.shape[-2], like.shape[-1]
    batch = like.shape[:-2]
    total = np.zeros(batch)
    num = np.zeros(batch + (n_peak, n_branch))
    num_new = np.zeros(batch + (n_peak,))
    for mapping in _injective_maps(n_branch, n_peak):
        claimed = {j for j in mapping if j >= 0}
        weight = np.ones(batch)
        for b, j in enumerate(mapping):
            if j >= 0:
                weight = weight * np.where(present[..., j], like[..., b, j], 0.0)
            elif absent is not None:
                weight = weight * absent[..., b]
        for j in range(n_peak):
            if j not in claimed:
                weight = weight * np.where(present[..., j], NEW_LEVEL, 1.0)
        total = total + weight
        for b, j in enumerate(mapping):
            if j >= 0:
                num[..., j, b] += weight
        for j in range(n_peak):
            if j not in claimed:
                num_new[..., j] += weight
    total = np.maximum(total, 1e-300)[..., None]
    return num / total[..., None], num_new / total


def joint_assign(peaks: Sequence[tuple[float, float]], u: float,
                 beliefs: Mapping[str, Mapping], branches: Sequence[str], *,
                 window: tuple[float, float] | None = None,
                 domain_hi: float | None = None,
                 presence=None) -> list[dict[str, float]]:
    """Per-peak branch probabilities from the joint identification of one scan's peaks.

    ``peaks`` is ``[(energy, sigma), ...]`` -- the peaks one scan delivered; ``window`` is the scan's
    energy range, which prices the branches that did *not* appear.  Exported so the world labels a
    scan's peaks by exactly the rule the planner priced the design with.
    """
    names = tuple(str(b) for b in branches)
    preds = [belief_predict(dict(beliefs[b]), float(u), domain_hi=domain_hi) for b in names]
    if not peaks:
        return []
    e = np.array([float(p[0]) for p in peaks])
    sg = np.array([max(float(p[1]), 1e-9) for p in peaks])
    like = np.stack([_norm_pdf(e, mu, np.sqrt(sig * sig + sg * sg)) for mu, sig in preds])
    if presence is not None:
        like = like * np.asarray(presence, dtype=float)[:, None]
    absent = np.array([[_absent_weight(np.array(mu), np.array(sig), window) for mu, sig in preds]])
    marg, p_new = _joint_marginals(like[None, ...], np.ones((1, len(peaks)), dtype=bool),
                                   absent.reshape(1, len(names)))
    return [{**{b: float(marg[0, j, k]) for k, b in enumerate(names)}, NEW: float(p_new[0, j])}
            for j in range(len(peaks))]


def identity_entropy(probabilities: Mapping[str, float]) -> float:
    """Entropy (nats) of *which branch* a peak is, conditional on it being a known branch."""
    known = np.array([max(float(v), 0.0) for k, v in probabilities.items() if k != NEW])
    if known.sum() <= 0.0:
        return math.inf
    known = known / known.sum()
    known = known[known > 0.0]
    return float(-np.sum(known * np.log(known)))


def anchor_admitted(probabilities: Mapping[str, float], branch: str) -> bool:
    """The session's two-part gate: confident identity AND enough probability on this branch."""
    return (float(probabilities.get(branch, 0.0)) >= CONFIRMED_PROBABILITY
            and identity_entropy(probabilities) <= ASSIGNMENT_ENTROPY_STOP_NATS)


def _distribution_entropy(full):
    """Shannon entropy (nats) over the last axis of an already-normalised distribution."""
    full = np.clip(np.asarray(full, dtype=float), 0.0, None)
    terms = np.where(full > 0.0, full * np.log(np.maximum(full, 1e-300)), 0.0)
    return -np.sum(terms, axis=-1)


def upsert_anchor(row: Mapping, u: float, omega0: float, weight: float) -> dict:
    """Return ``row`` with the anchor at reduced-q ``u`` set (replaced, not appended).

    A grid point contributes *one* anchor per branch, exactly as the session's dispersion prior holds
    one row per (q, branch).  Appending instead would let a redundant re-scan of a resolved point
    double the belief precision there and so look informative -- the planner would buy neutrons for
    a measurement it already has.
    """
    anchors = [dict(a) for a in (row.get("anchors") or []) if abs(float(a["u"]) - float(u)) >= 1e-9]
    anchors.append({"u": float(u), "omega0": float(omega0), "weight": float(weight)})
    out = dict(row)
    out["anchors"] = anchors
    out["n_anchors"] = len(anchors)
    return out


def merge_candidate(existing: Sequence[Candidate], new: Candidate) -> tuple[Candidate, ...]:
    """Fold one newly fitted peak into a grid point's candidate list.

    A peak within :data:`MERGE_SIGMA_FACTOR` combined sigmas of an existing one is the *same*
    physical peak measured again, so the two combine by inverse variance (what a pooled master fit
    does).  Anything further away is a rival component and is appended.  Without this, two scans of
    one point would stand as two rival mixture components and the point could never pass the bar.
    """
    rows = list(existing)
    best, best_dist = -1, math.inf
    for k, c in enumerate(rows):
        dist = abs(float(new.energy_meV) - float(c.energy_meV))
        threshold = MERGE_SIGMA_FACTOR * math.hypot(max(float(new.sigma_meV), 1e-9),
                                                    max(float(c.sigma_meV), 1e-9))
        if dist <= threshold and dist < best_dist:
            best, best_dist = k, dist
    if best < 0:
        return tuple(rows + [new])
    old = rows[best]
    w_old = 1.0 / max(float(old.sigma_meV), 1e-9) ** 2
    w_new = 1.0 / max(float(new.sigma_meV), 1e-9) ** 2
    energy = (float(old.energy_meV) * w_old + float(new.energy_meV) * w_new) / (w_old + w_new)
    rows[best] = Candidate(energy, 1.0 / math.sqrt(w_old + w_new), dict(new.probabilities))
    return tuple(rows)


# --------------------------------------------------------------------------- forward model

def _rates(om, sres: float, grid, peak, elastic: float, background: float, temperature_K: float):
    """Per-neutron rate, its parameter derivatives, and per-branch window counts.

    ``om`` is ``(S, B)`` branch energies, ``grid`` the ``(N,)`` energy points.  Returns
    ``(rate (S,N), deriv (S, 2B+2, N), branch_counts (S, B), peak_height (S, B))`` with parameter
    order ``(omega_b..., P_b..., elastic, background)``.  ``dn/domega`` is deliberately ignored.

    ``peak_height`` is the tallest per-point contribution that branch makes anywhere on the grid --
    ``area / (sigma_res * sqrt(2 pi))`` at the lobe centre, but read off the grid so a lobe that
    falls outside the window, or is clipped by its edge, reports the height actually on offer.
    """
    om = np.asarray(om, dtype=float)
    s, b = om.shape
    nb = _bose(om, temperature_K, sres / 4.0)[..., None]                 # (S, B, 1)
    g0 = _norm_pdf(grid, 0.0, sres)                                      # (N,)
    gp = _norm_pdf(grid[None, None, :], om[..., None], sres)             # (S, B, N)
    gm = _norm_pdf(grid[None, None, :], -om[..., None], sres)
    amp = np.asarray(peak, dtype=float)[None, :, None]                   # (1, B, 1)
    shape = (nb + 1.0) * gp + nb * gm                                    # per unit amplitude
    rate = background + elastic * g0[None, :] + np.sum(amp * shape, axis=1)
    d_om = amp * ((nb + 1.0) * gp * (grid[None, None, :] - om[..., None])
                  - nb * gm * (grid[None, None, :] + om[..., None])) / (sres * sres)
    deriv = np.concatenate([d_om, shape,
                            np.broadcast_to(g0[None, None, :], (s, 1, len(grid))),
                            np.ones((s, 1, len(grid)))], axis=1)
    return rate, deriv, np.sum(amp * shape, axis=2), np.max(amp * shape, axis=2)


def _sigma_fit(rate, deriv, ncount, n_branches: int, resolution_sigma: float):
    """Cramer-Rao sigma on each branch energy.  ``ncount`` may be scalar or an ``(E,)`` ladder.

    The Fisher information is exactly linear in ``ncount`` (rate and derivative both scale with it
    and the likelihood divides by the rate once), so the unit-exposure information is computed once
    and scaled.

    A scan **measures** a branch only when it pins that peak's position better than the resolution
    width -- ``sigma <= resolution_sigma``; anything looser is reported as ``inf``, meaning "not
    measured by this scan".  This is a model statement about what a fit can resolve, mirroring the
    session's own notion of a resolvable peak (``branch_assignment.RESOLVABLE_FWHM_FACTOR``), not a
    placement rule.  It matters at the zone centre: an acoustic branch at omega ~ 0 still returns a
    finite Cramer-Rao sigma of about a meV, but that number comes from the (n+1) versus n asymmetry
    of the two lobes, not from a resolved peak position, and taking it as a measurement puts a junk
    anchor at u = 0 that the belief never recovers from.
    """
    scaled = deriv / np.maximum(rate, 1e-300)[:, None, :]
    unit = np.einsum("spn,sqn->spq", scaled, deriv)                      # (S, P, P)
    counts = np.atleast_1d(np.asarray(ncount, dtype=float))
    # Invert through the eigendecomposition so an UNIDENTIFIABLE parameter is reported as
    # unmeasured rather than as measured to +-0.000.  Two peaks at the same energy, or a peak at
    # omega = 0 whose (2n+1)-weighted lobes coincide with the elastic line, make the Fisher matrix
    # singular; a ridge or a pseudo-inverse then returns a tiny variance for exactly the parameter
    # the data cannot see, and the planner would buy that scan for a prize it cannot deliver.
    # A parameter with weight on a null-space direction (eigenvalue below RCOND x the largest)
    # has infinite variance; the others take the usual sum over the identifiable directions.
    # Judge identifiability on the CORRELATION form of the Fisher matrix: the parameters have
    # wildly different units (a peak position against a per-neutron amplitude), so the raw
    # eigenvalues only report scale, while a correlation eigenvalue near zero reports genuine
    # collinearity.  A parameter with no sensitivity at all (zero diagonal: a peak outside the
    # window) is unidentifiable outright.
    diag = np.diagonal(unit, axis1=-2, axis2=-1)                          # (S, P)
    insensitive = diag <= 1e-300
    d = np.sqrt(np.where(insensitive, 1.0, diag))
    corr = unit / (d[:, :, None] * d[:, None, :])
    corr = np.where(insensitive[:, :, None] | insensitive[:, None, :], 0.0, corr)
    corr[:, np.arange(corr.shape[-1]), np.arange(corr.shape[-1])] = 1.0
    lam, vec = np.linalg.eigh(corr)                                      # (S, P), (S, P, P)
    null = lam < _FISHER_RCOND * np.max(lam, axis=-1, keepdims=True)     # (S, P)
    weight = vec[:, :n_branches, :] ** 2                                 # (S, B, P) V_jk^2
    unidentifiable = (insensitive[:, :n_branches]
                      | np.any(null[:, None, :] & (weight > _FISHER_NULL_WEIGHT), axis=-1))
    inv_lam = np.where(null, 0.0, 1.0 / np.maximum(lam, 1e-300))
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        # An insensitive parameter has d ~ 0 here; the ``unidentifiable`` mask below discards those
        # entries, so their overflow is cosmetic.
        unit_var = np.einsum("sbp,sp->sb", weight, inv_lam) / d[:, :n_branches] ** 2  # ncount = 1
    var = unit_var[None, :, :] / counts[:, None, None]                    # (E, S, B)
    with np.errstate(all="ignore"):
        sigma = np.sqrt(np.where(var > 0.0, var, np.nan))
    measured = np.isfinite(sigma) & (sigma <= resolution_sigma) & ~unidentifiable[None, :, :]
    return np.where(measured, sigma, np.inf)


def fisher_sigma_meV(energies: Sequence[float], *, e_lo: float, e_step: float, n_points: int,
                     fwhm_meV: float, temperature_K: float, scale: IntensityScale,
                     branches: Sequence[str], ncount: float) -> tuple[float, ...]:
    """Per-branch Cramer-Rao position sigma for one window at one set of true branch energies."""
    grid = float(e_lo) + float(e_step) * np.arange(int(n_points), dtype=float)
    sres = float(fwhm_meV) * FWHM_TO_SIGMA
    peak = [float(scale.peak_counts_meV[b]) for b in branches]
    rate, deriv, _c, _h = _rates(np.array([[float(e) for e in energies]]), sres, grid, peak,
                            float(scale.elastic_counts_meV), float(scale.background_per_point),
                            temperature_K)
    return tuple(float(v) for v in _sigma_fit(rate, deriv, float(ncount), len(peak), sres)[0, 0])


def expected_counts(energies: Sequence[float], *, e_lo: float, e_step: float, n_points: int,
                    fwhm_meV: float, temperature_K: float, scale: IntensityScale,
                    branches: Sequence[str], ncount: float):
    """Expected counts AT EACH POINT of one window: the Poisson mean a real scan would draw from.

    The same forward model the planner scores with, exposed so the simulator's world can draw counts
    from it rather than being handed the answer.  Returns an ``(n_points,)`` array.
    """
    grid = float(e_lo) + float(e_step) * np.arange(int(n_points), dtype=float)
    sres = float(fwhm_meV) * FWHM_TO_SIGMA
    peak = [float(scale.peak_counts_meV[b]) for b in branches]
    rate, _deriv, _c, _h = _rates(np.array([[float(e) for e in energies]]), sres, grid, peak,
                                   float(scale.elastic_counts_meV),
                                   float(scale.background_per_point), temperature_K)
    return np.asarray(rate[0], dtype=float) * float(ncount)


def detection_significance(energies: Sequence[float], *, e_lo: float, e_step: float,
                           n_points: int, fwhm_meV: float, temperature_K: float,
                           scale: IntensityScale, branches: Sequence[str], ncount: float):
    """Expected detection significance of each branch's peak in one window, the fitter's own test.

    ``A / sqrt(A + B)`` with ``A`` the branch's expected peak HEIGHT in counts at its tallest point
    on the grid and ``B`` the flat background per point -- exactly
    ``isar.analyze.verdict.detection_power``, which takes ``B`` from the fitted ``bg.BG`` and so
    ignores the elastic line sitting under a low-energy peak.  Mirrored, not re-derived: a peak the
    fitter would call underpowered is one the planner must not buy.
    """
    grid = float(e_lo) + float(e_step) * np.arange(int(n_points), dtype=float)
    sres = float(fwhm_meV) * FWHM_TO_SIGMA
    peak = [float(scale.peak_counts_meV[b]) for b in branches]
    _rate, _deriv, _counts, height = _rates(
        np.array([[float(e) for e in energies]]), sres, grid, peak,
        float(scale.elastic_counts_meV), float(scale.background_per_point), temperature_K)
    return tuple(float(v) for v in _significance(height[0] * float(ncount),
                                                 float(scale.background_per_point) * float(ncount)))


def _significance(peak_counts, background_counts):
    """``A / sqrt(A + B)``, zero where there is nothing to see."""
    total = np.asarray(peak_counts) + np.asarray(background_counts)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(total > 0.0, np.asarray(peak_counts) / np.sqrt(np.maximum(total, 1e-300)),
                        0.0)


def branch_counts(energies: Sequence[float], *, e_lo: float, e_step: float, n_points: int,
                  fwhm_meV: float, temperature_K: float, scale: IntensityScale,
                  branches: Sequence[str], ncount: float) -> tuple[float, ...]:
    """Expected counts each branch contributes inside one window (both lobes, Bose applied)."""
    grid = float(e_lo) + float(e_step) * np.arange(int(n_points), dtype=float)
    sres = float(fwhm_meV) * FWHM_TO_SIGMA
    peak = [float(scale.peak_counts_meV[b]) for b in branches]
    _r, _d, counts, _h = _rates(np.array([[float(e) for e in energies]]), sres, grid, peak,
                          float(scale.elastic_counts_meV), float(scale.background_per_point),
                          temperature_K)
    return tuple(float(v) * float(ncount) for v in counts[0])


# --------------------------------------------------------------------------- windows

def _window_list(mus, sigs, sres: float, fwhm: float, lo_d: float, hi_d: float,
                 step_fraction: float, max_points: int, extra=()):
    """Every window the beliefs generate at one grid point, hull windows included.

    ``extra`` carries additional ``(centre, spread)`` pairs -- the clusters of surviving scenario
    mass -- which get the same window families as a branch's own moments.  With a unimodal scenario
    set they land on the moment windows and dedup away.
    """
    step = float(step_fraction) * float(fwhm)
    if step <= 0.0:
        raise ValueError("step_fraction * fwhm_meV must be positive")
    raw: list[tuple[float, float]] = []
    for mu, sg in list(zip(mus, sigs)) + list(extra):
        for k in WINDOW_K_FACTORS:
            hw = k * math.sqrt(sg * sg + sres * sres) + sres
            raw.append((mu - hw, mu + hw))
            raw.append((-(mu + hw), mu + hw))
    hulls = [HULL_K * math.sqrt(sg * sg + sres * sres) + sres for sg in sigs]
    hull_lo = min(mu - hw for mu, hw in zip(mus, hulls))
    hull_hi = max(mu + hw for mu, hw in zip(mus, hulls))
    raw.extend([(hull_lo, hull_hi), (-hull_hi, hull_hi)])

    windows: list[tuple[float, float, float, int]] = []
    seen: set[tuple[float, float, float]] = set()
    for lo, hi in raw:
        lo, hi = max(lo, lo_d), min(hi, hi_d)
        if hi <= lo:
            continue
        n = int(math.floor((hi - lo) / step)) + 1
        if n < 3 or n > max_points:
            continue
        key = (round(lo, 3), round(hi, 3), round(step, 3))
        if key in seen:
            continue
        seen.add(key)
        windows.append((lo, hi, step, n))
    return windows


# --------------------------------------------------------------------------- the planner

def _validate(inp: PlannerInput) -> None:
    n = len(inp.path)
    if n == 0:
        raise ValueError("path must contain at least one grid point")
    if len(inp.u) != n or len(inp.fwhm_meV) != n:
        raise ValueError("path, u and fwhm_meV must have the same length")
    if not inp.targets:
        raise ValueError("at least one target branch is required")
    if not inp.beliefs:
        raise ValueError("beliefs must contain every modelled branch")
    missing = sorted(set(inp.targets) - set(inp.beliefs))
    if missing:
        raise ValueError(f"target branch(es) without a belief: {', '.join(missing)}")
    lo, hi = (float(v) for v in inp.energy_domain_meV)
    if not (math.isfinite(lo) and math.isfinite(hi)) or hi <= max(lo, 0.0):
        raise ValueError("energy_domain_meV must be finite with a positive energy range")
    if float(inp.tolerance_meV) <= 0.0:
        raise ValueError("tolerance_meV must be positive")
    if int(inp.base_ncount) <= 0 or not inp.ncount_ladder:
        raise ValueError("base_ncount and ncount_ladder must be positive and non-empty")
    if any(float(f) <= 0.0 for f in inp.ncount_ladder):
        raise ValueError("every ncount ladder factor must be positive")
    if any(not math.isfinite(float(f)) or float(f) <= 0.0 for f in inp.fwhm_meV):
        raise ValueError("fwhm_meV entries must be finite and positive")
    if int(inp.max_points) < 3:
        raise ValueError("max_points must be at least three")
    if float(inp.scale.background_per_point) <= 0.0:
        raise ValueError("background_per_point must be positive (the rate must never vanish)")
    for b in inp.beliefs:
        if str(b) not in inp.scale.peak_counts_meV:
            raise ValueError(f"intensity scale has no peak amplitude for branch {b!r}")
    for source, name in ((inp.measured, "measured"), (inp.scans, "scans")):
        for index in source:
            if not 0 <= int(index) < n:
                raise ValueError(f"{name} grid index {index} is off the path")


def _is_gapless(row: Mapping) -> bool:
    prior = dict(row.get("parameter_prior") or {})
    zc = dict(prior.get("zc_energy") or {})
    return float(zc.get("mean", zc.get("mode", 0.0))) == 0.0


class _Predictor:
    """``belief_predict`` with a cache keyed by the row's anchor content and u (A4)."""

    def __init__(self, rows: Mapping[str, Mapping], domain_hi: float):
        self._rows = rows
        self._domain_hi = float(domain_hi)
        self._cache: dict[tuple, tuple[float, float]] = {}

    def __call__(self, branch: str, u: float, row: Mapping | None = None) -> tuple[float, float]:
        row = self._rows[branch] if row is None else row
        key = (branch, round(float(u), 9), self._domain_hi,
               tuple((round(float(a["u"]), 9), round(float(a["omega0"]), 9),
                      round(float(a.get("weight", 1.0)), 6))
                     for a in (row.get("anchors") or [])))
        hit = self._cache.get(key)
        if hit is None:
            mu, sigma = belief_predict(dict(row), float(u), domain_hi=self._domain_hi)
            hit = (float(mu), max(float(sigma), 1e-9))
            self._cache[key] = hit
        return hit


def _pad_candidates(lists):
    """Pad ragged candidate lists into ``(M, Jmax)`` energy/sigma/valid arrays."""
    j_max = max((len(c) for c in lists), default=0)
    e = np.zeros((len(lists), max(j_max, 1)))
    s = np.ones_like(e)
    valid = np.zeros(e.shape, dtype=bool)
    for m, cands in enumerate(lists):
        for j, c in enumerate(cands):
            e[m, j] = float(c.energy_meV)
            s[m, j] = max(float(c.sigma_meV), 1e-9)
            valid[m, j] = True
    return e, s, valid


def _deficit(hw, identified, tau: float):
    """How far a point is from complete, on both fronts, in nats.

    A point is complete only when the target's energy is known to +-tau AND the target has been
    identified: the mass that it is one of the fitted peaks, ``sum_j w_j = 1 - w_none``, is at least
    :data:`CONFIRMED_PROBABILITY`.  A narrow posterior on a peak that might not be the target at all
    is not the answer the scientist asked for, and the same 0.85 the anchor gate uses means the same
    thing here -- identified.  Either front alone can hold the deficit above zero, and the shaping
    term still orders progress on either.
    """
    width_term = np.maximum(np.log(np.maximum(hw, 1e-300) / tau), 0.0)
    identity_term = np.maximum(
        np.log(CONFIRMED_PROBABILITY / np.maximum(identified, IDENTIFIED_FLOOR)), 0.0)
    return width_term + identity_term


def _deficits_batch(padded, mus, sigs, target_index, width: float, tau: float, exempt,
                    presence=None):
    """HDI half-width, identified mass and deficit of a padded batch, in one vectorised pass.

    ``mus`` / ``sigs`` are per-branch sequences of per-point belief predictions.
    """
    e, s, valid = padded
    w, w_none = target_posterior(e, s, valid, [np.asarray(m) for m in mus],
                                 [np.asarray(g) for g in sigs], target_index, presence=presence)
    any_peak = valid.any(axis=-1)
    hw = np.where(any_peak, hdi_halfwidth(e, s, w, w_none, width), 0.5 * width)
    identified = np.where(any_peak, w.sum(axis=-1), 0.0)
    deficit = _deficit(hw, identified, tau)
    return hw, identified, np.where(np.asarray(exempt, dtype=bool), 0.0, deficit)


def plan_next(inp: PlannerInput) -> Decision:
    """Rank every window/exposure the beliefs generate; return the best information per neutron."""
    _validate(inp)
    branches = tuple(sorted(str(b) for b in inp.beliefs))
    rows = {b: dict(inp.beliefs[b]) for b in branches}
    targets = tuple(str(t) for t in inp.targets)
    lo_d, hi_d = float(inp.energy_domain_meV[0]), float(inp.energy_domain_meV[1])
    width = hi_d - max(lo_d, 0.0)
    tau = float(inp.tolerance_meV)
    predict = _Predictor(rows, hi_d)
    measured = {int(k): tuple(v) for k, v in inp.measured.items() if v}
    executed = {int(k): tuple(v) for k, v in (inp.scans or {}).items() if v}
    n_path = len(inp.path)

    exempt = [[_is_gapless(rows[t]) and abs(float(inp.u[m])) < 1e-9 for t in targets]
              for m in range(n_path)]
    padded_all = _pad_candidates([measured.get(m, ()) for m in range(n_path)])
    preds_all = [[predict(b, inp.u[m]) for m in range(n_path)] for b in branches]
    presence_all = np.array([
        point_presence(branches, [preds_all[b][m][0] for b in range(len(branches))],
                       (() if inp.abstained.get(m) else executed.get(m, ())),
                       step=float(inp.step_fraction) * float(inp.fwhm_meV[m]),
                       fwhm_meV=float(inp.fwhm_meV[m]), temperature_K=float(inp.temperature_K),
                       scale=inp.scale, candidates=measured.get(m, ()))
        for m in range(n_path)])                                   # (M, B)
    halfwidth, identified, deficit_now = [], [], []
    for k, t in enumerate(targets):
        hw, ident, d = _deficits_batch(padded_all, [[q[0] for q in row] for row in preds_all],
                                       [[q[1] for q in row] for row in preds_all],
                                       branches.index(t), width, tau,
                                       [exempt[m][k] for m in range(n_path)],
                                       presence=presence_all)
        halfwidth.append(hw)
        identified.append(ident)
        deficit_now.append(d)
    halfwidth = np.array(halfwidth).T
    identified = np.array(identified).T
    deficit_now = np.array(deficit_now).T

    per_point = tuple(
        PointState(grid_index=m, hkl=tuple(float(c) for c in inp.path[m]), u=float(inp.u[m]),
                   exempt=all(exempt[m]),
                   halfwidth_meV=float(halfwidth[m].max()),
                   identified_mass=float(identified[m].min()),
                   deficit_nats=float(deficit_now[m].sum()),
                   within_bar=bool(deficit_now[m].max() <= 0.0),
                   n_candidates=len(measured.get(m, ())))
        for m in range(n_path))

    if float(deficit_now.sum()) <= 0.0:
        return Decision(stop=True, stop_reason="goal_met", chosen=None, ranked=(),
                        per_point=per_point)

    ladder = tuple(max(int(round(int(inp.base_ncount) * float(f))), 1) for f in inp.ncount_ladder)
    designs: list[RankedDesign] = []
    for i in range(n_path):
        # ponytail: a within-bar point is excluded as a scan site rather than losing on score.
        # Its own-point utility is zero by the prize definition, but its cross-point utility is not
        # -- an anchor there still sharpens the target belief where the map is still ambiguous, and
        # measured with the exclusion removed it wins outright (optic campaign, u=0.05, scan 53).
        # The ceiling is that a genuinely cheap disambiguating re-scan is unreachable; lift it by
        # pricing the anchor's cross-point gain against a scan that does not re-measure a solved
        # point (a bridging q, or the same anchor bought at a neighbouring unsolved point).
        if deficit_now[i].max() <= 0.0:
            continue
        designs.extend(_designs_at(i, inp, branches, rows, targets, predict, exempt, deficit_now,
                                   measured, executed, width, ladder, presence_all))
    if not designs:
        return Decision(stop=True, stop_reason="no_admissible_window", chosen=None, ranked=(),
                        per_point=per_point)

    # Lexicographic: expected completions per unit price first.  Only among designs that tie on it
    # -- and among all of them when nothing completes anything -- does the shaping deficit order the
    # field, then the lower price, then the content hash.
    def order(d: RankedDesign):
        return (-d.shaping_nats / d.price, d.price, _design_hash(d))

    designs.sort(key=lambda d: (-d.score,) + order(d))
    best = designs[0].score
    cut = best * (1.0 - TIE_REL) if best > 0.0 else math.inf
    tied = [d for d in designs if d.score >= cut] if best > 0.0 else designs
    chosen = min(tied, key=order)
    ranked = [chosen] + [d for d in designs if d is not chosen]
    note = ""
    if chosen.utility_nats <= 0.0:
        note = ("no design is expected to complete a point from here; taking the cheapest "
                "probe (check the intensity map scale and the beliefs)")
    return Decision(stop=False, stop_reason="", chosen=chosen, ranked=tuple(ranked),
                    per_point=per_point, note=note)


def _design_hash(d: RankedDesign) -> str:
    """A5: ties break on a content hash, so no physical property is silently preferred."""
    canonical = (f"{d.hkl[0]:.6f},{d.hkl[1]:.6f},{d.hkl[2]:.6f}|{d.e_lo:.6f}|{d.e_hi:.6f}|"
                 f"{d.e_step:.6f}|{d.ncount:d}")
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def _scenarios(i, inp, branches, predict, executed, candidates=()):
    """Joint scenario energies and weights, down-weighted by what the master here did NOT find (A6).

    This is the energy-resolved half of :func:`point_presence`, and it asks the same pooled
    question: a branch's node is argued against when the exposure BANKED at that energy would have
    detected the branch there and the master's fit does not report it.  How MUCH it is argued
    against is soft on two axes -- the fit can miss a peak that is there
    (:data:`NULL_MISS_PROBABILITY`), and the master may be unsure it delivered that branch at all
    (:func:`pooled_delivery`).  With ``p`` that probability the node's weight is multiplied by
    ``p + (1 - p) * NULL_MISS_PROBABILITY``, once: 1.0 when the branch was surely seen here, 0.1
    when it surely was not, 0.55 on a coin flip.

    Asked once per node, not once per scan, and of the pool.  The product over scans was wrong in
    both directions.  Judging each scan's reach alone let a band the master had looked into deeply
    keep its full weight whenever no single scan could have seen the branch by itself -- the
    ordinary case for a pool built out of cheap top-ups, and the reason the optic campaign could
    top up a pool looking in the wrong place for ever.  And where a scan did clear the floor alone,
    multiplying per scan counted one pooled fit as many independent observations.  The accumulation
    is still done in log-weights, which cost nothing and keep a long pool finite.
    """
    step = float(inp.step_fraction) * float(inp.fwhm_meV[i])
    # An underpowered abstain is not a null: the fitter could not see, it did not see nothing.
    scans = () if inp.abstained.get(i) else executed.get(i, ())
    cands = tuple(candidates or ())
    # Two peaks closer than two resolution widths are one peak, which is the same separation
    # _scenario_clusters splits on -- so it is the radius within which a candidate answers for a
    # scenario node.
    near_radius = 2.0 * float(inp.fwhm_meV[i]) * FWHM_TO_SIGMA
    mu_all = [predict(b, inp.u[i])[0] for b in branches]
    nodes, weights = [], []
    reweighted = [False] * len(branches)
    for bi, b in enumerate(branches):
        mu, sig = predict(b, inp.u[i])
        # Clipped to the authorized band at BOTH ends: the belief's moments are already truncated
        # to it, but the Gauss-Hermite nodes spread around them were not, so a wide belief put
        # scenario mass -- and the windows built from it -- outside the band the program may scan.
        x = np.clip(mu + _SQRT2 * sig * _GH8_X, 0.0, float(inp.energy_domain_meV[1]))
        log_w = np.log(_GH8_W)
        if scans:
            # Both halves of the one question, node by node: could the master here have seen this
            # branch at that energy, and does its fit report it there.
            seeable = np.array([
                pooled_significance([e if k == bi else mu_all[k] for k in range(len(branches))],
                                    bi, scans, step=step, fwhm_meV=float(inp.fwhm_meV[i]),
                                    temperature_K=float(inp.temperature_K), scale=inp.scale,
                                    branches=branches) for e in x]) >= DETECT_FLOOR
            factor = np.array([miss_factor(pooled_delivery(cands, b, near=e, within=near_radius))
                               for e in x])
            contradicted = seeable & (factor < 1.0)
            log_w = np.where(contradicted, log_w + np.log(np.maximum(factor, 1e-300)), log_w)
            reweighted[bi] = bool(contradicted.any())
        w = np.exp(log_w - log_w.max())       # shift before exponentiating, never underflow to 0
        nodes.append(x)
        weights.append(w / w.sum())
    mesh = np.meshgrid(*nodes, indexing="ij")
    wmesh = np.meshgrid(*weights, indexing="ij")
    om = np.stack([m.ravel() for m in mesh], axis=-1)
    wt = np.prod(np.stack([w.ravel() for w in wmesh], axis=-1), axis=-1)
    return om, wt / wt.sum(), bool(scans), list(zip(nodes, weights, reweighted))


def _scenario_clusters(nodes, weights, sres: float):
    """Weighted (mean, spread) of each separated lump of one branch's scenario mass.

    Negative evidence moves scenario WEIGHT without moving the belief's moments, so a branch whose
    middle has been contradicted keeps a mean at the band centre while its surviving mass sits at
    the ends -- and windows built from the moments look exactly where the evidence says the branch
    is not.  Nodes are split wherever a gap exceeds two resolution widths, which is the smallest
    separation a scan could resolve into distinct peaks anyway; a lump holding less weight than one
    fitter miss (:data:`NULL_MISS_PROBABILITY`) is not worth a window of its own.
    """
    order = np.argsort(nodes, kind="stable")
    x, w = np.asarray(nodes)[order], np.asarray(weights)[order]
    cuts = np.flatnonzero(np.diff(x) > 2.0 * sres) + 1
    out = []
    for part_x, part_w in zip(np.split(x, cuts), np.split(w, cuts)):
        mass = float(part_w.sum())
        if mass <= NULL_MISS_PROBABILITY:
            continue
        mean = float(np.dot(part_w, part_x) / mass)
        spread = math.sqrt(max(float(np.dot(part_w, part_x ** 2) / mass) - mean * mean, 0.0))
        out.append((mean, spread))
    # One surviving lump is already what the moments describe, so it would only re-derive the
    # windows the branch already has.  Two or more is the case the moments cannot express: a mean
    # sitting in the contradicted gap between them.
    return out if len(out) > 1 else []


def _admitted_anchor(energies, weights, sres: float):
    """``(energy, mass)`` of the anchor a design would actually leave, given it admits one.

    ``weights`` are the scenario weights already multiplied by this design's admission indicator, so
    the mean is over the outcomes in which the scan measures AND admits the peak -- never the
    marginal mean over all scenarios.  After negative evidence splits a branch's set the marginal
    mean can sit in the contradicted gap, an energy no outcome of the scan produces, and crediting a
    window with an anchor outside itself pays a design for evidence it cannot deliver.  When the
    admitted outcomes themselves split into lumps more than two resolution widths apart, the heavier
    lump is the anchor and its own mass is what the gain is scaled by.
    """
    order = np.argsort(np.asarray(energies), kind="stable")
    x, w = np.asarray(energies)[order], np.asarray(weights)[order]
    live = w > 0.0
    x, w = x[live], w[live]
    if x.size == 0:
        return 0.0, 0.0
    cuts = np.flatnonzero(np.diff(x) > 2.0 * sres) + 1
    best_mean, best_mass = 0.0, -1.0
    for part_x, part_w in zip(np.split(x, cuts), np.split(w, cuts)):
        mass = float(part_w.sum())
        if mass > best_mass:
            best_mass, best_mean = mass, float(np.dot(part_w, part_x) / mass)
    return best_mean, max(best_mass, 0.0)


def _designs_at(i, inp, branches, rows, targets, predict, exempt, deficit_now, measured, executed,
                width, ladder, presence_all):
    """Every ranked design at one grid point, with its preposterior utility."""
    n_branch = len(branches)
    lo_d, hi_d = float(inp.energy_domain_meV[0]), float(inp.energy_domain_meV[1])
    tau = float(inp.tolerance_meV)
    fwhm = float(inp.fwhm_meV[i])
    sres = fwhm * FWHM_TO_SIGMA
    peak = [float(inp.scale.peak_counts_meV[b]) for b in branches]
    elastic = float(inp.scale.elastic_counts_meV)
    background = float(inp.scale.background_per_point)

    belief = [predict(b, inp.u[i]) for b in branches]
    om, weight_s, truncated, marginals = _scenarios(i, inp, branches, predict, executed,
                                                    measured.get(i, ()))
    n_scen = om.shape[0]
    extra: list[tuple[float, float]] = []
    if truncated:
        mus = np.einsum("s,sb->b", weight_s, om)
        sigs = np.sqrt(np.maximum(np.einsum("s,sb->b", weight_s, om ** 2) - mus ** 2, 1e-6))
        # Candidate windows follow the surviving scenario mass, not only its moments -- but only
        # for a branch some scan actually argued against.  An untouched branch's nodes are a
        # quadrature grid, not structure, and splitting it on absolute gaps would invent windows.
        for nodes, node_w, was_reweighted in marginals:
            if was_reweighted:
                extra.extend(_scenario_clusters(nodes, node_w, sres))
    else:
        mus = np.array([p[0] for p in belief])
        sigs = np.array([p[1] for p in belief])
    windows = _window_list(mus, sigs, sres, fwhm, lo_d, hi_d, float(inp.step_fraction),
                           int(inp.max_points), extra=extra)
    if not windows:
        return []

    # --- Fisher sigma for every window and exposure ----------------------------------------
    # Banked exposure per SCENARIO energy and branch: what the master at this q already holds at
    # the energy this outcome puts the peak at. Asked at the branch mean it credited a deep old
    # window over an excluded gap to a surviving lump that window never covered, and a small top-up
    # there scored as fit-delivering with only its own counts at that energy (PR #37 review). The
    # Fisher sigma itself stays per design -- precision accumulates through merge_candidate pooling
    # the delivered peaks, and counting it here too would double it.
    executed_here = executed.get(i, ())
    banked = np.array([[banked_exposure(executed_here, float(om[s, b])) for b in range(n_branch)]
                       for s in range(n_scen)], dtype=float)                 # (S, B)
    n_win, n_exp = len(windows), len(ladder)
    sigma_fit = np.empty((n_win, n_exp, n_scen, n_branch))
    for wi, (lo, _hi, step, npts) in enumerate(windows):
        grid = lo + step * np.arange(npts, dtype=float)
        rate, deriv, _counts, height = _rates(om, sres, grid, peak, elastic, background,
                                              float(inp.temperature_K))
        sigma_fit[wi] = _sigma_fit(rate, deriv, np.array(ladder, dtype=float), n_branch,
                                   sres) * FIT_SIGMA_INFLATION
        # A fit also needs a peak the fitter can SEE, not only a favourable Fisher matrix, and
        # "can see" is the fitter's own published test rather than one invented here: expected
        # significance A/sqrt(A+B) against DETECT_FLOOR. Without it the planner buys a cheap probe
        # of a dim band whose Cramer-Rao sigma looks fine, the fitter abstains, the point's state
        # never changes, and the identical design wins again.
        #
        # The test is on the POOLED exposure -- what is banked at this q plus what this design
        # would add -- because the fit that matters is the master's, not this scan's. Height and
        # background both scale with exposure, so this is the same arithmetic at a larger ncount,
        # not a second model. A point with nothing banked is unchanged; a point already part-way
        # there can be finished by a cheap top-up instead of a fresh full-significance scan.
        exposure = np.array(ladder, dtype=float)[:, None, None] + banked[None, :, :]
        significance = _significance(height[None, :, :] * exposure, background * exposure)
        sigma_fit[wi] = np.where(significance >= DETECT_FLOOR, sigma_fit[wi], np.inf)
    sigma_fit = sigma_fit.reshape(n_win * n_exp, n_scen, n_branch)
    fit_measured = np.isfinite(sigma_fit)
    n_design = n_win * n_exp
    safe_sigma = np.where(fit_measured, sigma_fit, 1.0)

    # --- the anchor each design would leave: A3 admission, A4 per design --------------------
    om_d = np.broadcast_to(om[None, :, :], (n_design, n_scen, n_branch))
    like = np.stack([_norm_pdf(om_d, belief[b][0], np.sqrt(belief[b][1] ** 2 + safe_sigma ** 2))
                     for b in range(n_branch)], axis=-2)                  # (D, S, B, J=B)
    absent = np.repeat(np.array([[_absent_weight(np.array(belief[b][0]), np.array(belief[b][1]),
                                                 (lo, lo + step * (npts - 1)))
                                  for b in range(n_branch)]
                                 for (lo, _hi, step, npts) in windows]), n_exp, axis=0)
    marg, p_new = _joint_marginals(like, fit_measured, absent[:, None, :])  # (D,S,J,B), (D,S,J)
    own = np.diagonal(marg, axis1=-2, axis2=-1)                           # (D, S, B): P(peak b -> b)
    known = marg / np.maximum(marg.sum(axis=-1, keepdims=True), 1e-300)
    entropy = _distribution_entropy(known)                                # (D, S, J=B)
    admitted = (fit_measured & (own >= CONFIRMED_PROBABILITY)
                & (entropy <= ASSIGNMENT_ENTROPY_STOP_NATS))
    admit_mass = np.einsum("s,dsb->db", weight_s, admitted.astype(float))
    # The anchor's weight is conditional on admission too: it is what the belief would be told in
    # the outcomes that tell it anything, not an average over outcomes that stay silent.
    own_admitted = np.einsum("s,dsb->db", weight_s, np.where(admitted, own, 0.0))
    anchor_w = np.where(admit_mass > 0.0, own_admitted / np.maximum(admit_mass, 1e-300), 0.0)
    anchor_e = np.zeros((n_design, n_branch))
    anchor_mass = np.zeros((n_design, n_branch))
    for d in range(n_design):
        for b in range(n_branch):
            anchor_e[d, b], anchor_mass[d, b] = _admitted_anchor(
                om[:, b], weight_s * admitted[d, :, b], sres)
    present = anchor_mass > 0.0

    groups: dict[tuple, list[int]] = {}
    for d in range(n_design):
        key = tuple((bool(present[d, b]), round(float(anchor_e[d, b]), ANCHOR_WEIGHT_DECIMALS),
                     round(float(anchor_w[d, b]), ANCHOR_WEIGHT_DECIMALS))
                    for b in range(n_branch))
        groups.setdefault(key, []).append(d)

    # --- own-point components: existing candidates merged with this scan's new peaks --------
    existing = measured.get(i, ())
    n_ex = len(existing)
    comp_e = np.empty((n_design, n_scen, n_ex + n_branch))
    comp_s = np.empty_like(comp_e)
    valid = np.ones(comp_e.shape, dtype=bool)
    if n_ex:
        comp_e[..., :n_ex] = np.array([float(c.energy_meV) for c in existing])
        comp_s[..., :n_ex] = np.maximum(np.array([float(c.sigma_meV) for c in existing]), 1e-9)
    used = np.zeros((n_design, n_scen, n_ex), dtype=bool)
    for b in range(n_branch):
        om_b = om_d[..., b]
        s_b = safe_sigma[..., b]
        if n_ex:
            dist = np.abs(om_b[..., None] - comp_e[..., :n_ex])
            thr = MERGE_SIGMA_FACTOR * np.sqrt(s_b[..., None] ** 2 + comp_s[..., :n_ex] ** 2)
            masked = np.where((dist <= thr) & fit_measured[..., b, None] & ~used, dist, np.inf)
            k = np.argmin(masked, axis=-1)[..., None]
            hit = np.isfinite(np.take_along_axis(masked, k, -1))
            e_old = np.take_along_axis(comp_e[..., :n_ex], k, -1)
            s_old = np.maximum(np.take_along_axis(comp_s[..., :n_ex], k, -1), 1e-6)
            w_old = 1.0 / s_old ** 2
            w_new = 1.0 / np.maximum(s_b[..., None], 1e-6) ** 2
            comp_e[..., n_ex + b] = np.where(
                hit[..., 0], ((e_old * w_old + om_b[..., None] * w_new) / (w_old + w_new))[..., 0],
                om_b)
            comp_s[..., n_ex + b] = np.where(hit[..., 0],
                                             (1.0 / np.sqrt(w_old + w_new))[..., 0], s_b)
            slot = valid[..., :n_ex]
            np.put_along_axis(slot, k, np.take_along_axis(slot, k, -1) & ~hit, -1)
            np.put_along_axis(used, k, np.take_along_axis(used, k, -1) | hit, -1)
        else:
            comp_e[..., n_ex + b] = om_b
            comp_s[..., n_ex + b] = s_b
        valid[..., n_ex + b] = fit_measured[..., b]

    # --- price every group: cross-point deficit removed, plus the own-point preposterior ----
    utility = np.zeros(n_design)
    shaping = np.zeros(n_design)
    others = [(m, c) for m, c in sorted(measured.items()) if m != i]
    padded_others = _pad_candidates([c for _m, c in others]) if others else None
    for key, members in groups.items():
        idx = np.array(members)
        updated = dict(rows)
        anchored = [b for b in range(n_branch) if key[b][0]]
        for b in anchored:
            updated[branches[b]] = upsert_anchor(rows[branches[b]], float(inp.u[i]),
                                                 key[b][1], key[b][2])
        # The cross-point gain arrives only in the outcomes that actually leave the anchor, so it
        # is scaled by that expected fraction -- independently per anchored branch -- rather than
        # credited in full on a Boolean.
        reach = (np.prod(np.stack([anchor_mass[idx, b] for b in anchored]), axis=0)
                 if anchored else np.zeros(len(members)))
        gain = np.zeros(len(members))
        shaped = np.zeros(len(members))
        for k, t in enumerate(targets):
            if anchored and others:
                preds_m = [[predict(b, inp.u[m], updated[b]) for m, _c in others]
                           for b in branches]
                _hw, _ident, after = _deficits_batch(
                    padded_others, [[q[0] for q in row] for row in preds_m],
                    [[q[1] for q in row] for row in preds_m], branches.index(t), width, tau,
                    [exempt[m][k] for m, _c in others],
                    presence=presence_all[[m for m, _c in others]])
                now_deficit = np.array([deficit_now[m][k] for m, _c in others])
                gain += reach * float(np.count_nonzero(after <= 0.0)
                                      - np.count_nonzero(now_deficit <= 0.0))
                shaped += reach * float(now_deficit.sum() - after.sum())
            upd = [predict(b, inp.u[i], updated[b]) for b in branches]
            # The anchored belief speaks only in the outcomes that admit the anchor. The group key
            # collapses every admitting outcome into one anchor, which is fine for the cross-point
            # gain (scaled by ``reach``), but the own-point posterior below is evaluated per
            # outcome, and an outcome that admits nothing leaves the belief as it was. Crediting the
            # anchored belief to those outcomes bought self-identification most of a design's
            # outcomes would not earn (PR #37 review).
            sel_mu = [np.where(admitted[idx][:, :, b], upd[b][0], belief[b][0])
                      for b in range(n_branch)]
            sel_sig = [np.where(admitted[idx][:, :, b], upd[b][1], belief[b][1])
                       for b in range(n_branch)]
            # No window-absence evidence here, deliberately: the prize is judged on the point's
            # accumulated candidates, which carry no single window, so a preposterior that used it
            # would predict completions the prize rule can never confirm -- and the planner would
            # re-buy the same scan for ever.  The design is scored by the rule that will judge it.
            # ponytail: the presence used here is the one the point already has, not the one this
            # design would leave -- a scan that looks and does not find lowers a rival's presence
            # further, so the preposterior UNDER-states its own gain. Safe direction (it can never
            # promise a completion the prize will refuse); lift it by folding the design's own
            # expected non-detection into presence per scenario if the campaigns get slow.
            w, w_none = target_posterior(comp_e[idx], comp_s[idx], valid[idx], sel_mu, sel_sig,
                                         branches.index(t), presence=presence_all[i])
            hw = hdi_halfwidth(comp_e[idx], comp_s[idx], w, w_none, width)
            after = (np.zeros_like(hw) if exempt[i][k]
                     else _deficit(hw, w.sum(axis=-1), tau))
            gain += (np.einsum("s,ds->d", weight_s, (after <= 0.0).astype(float))
                     - float(deficit_now[i][k] <= 0.0))
            shaped += np.einsum("s,ds->d", weight_s, deficit_now[i][k] - after)
        utility[idx] = gain
        shaping[idx] = shaped
    utility = np.maximum(utility, 0.0)
    shaping = np.maximum(shaping, 0.0)

    sigma_mass = np.einsum("s,dsb->db", weight_s, fit_measured.astype(float))
    sigma_sum = np.einsum("s,dsb->db", weight_s, np.where(fit_measured, sigma_fit, 0.0))
    assign_sum = np.einsum("s,dsb->db", weight_s, np.where(fit_measured, own, 0.0))
    e_pred = {b: (float(belief[k][0]), float(belief[k][1])) for k, b in enumerate(branches)}

    out: list[RankedDesign] = []
    for wi, (lo, _hi, step, npts) in enumerate(windows):
        for ei, ncount in enumerate(ladder):
            d = wi * n_exp + ei
            dose = float(npts) * float(ncount)
            price = dose + float(inp.scan_overhead_price)
            out.append(RankedDesign(
                grid_index=i, hkl=tuple(float(c) for c in inp.path[i]),
                e_lo=float(lo), e_hi=float(lo + step * (npts - 1)), e_step=float(step),
                n_points=int(npts), ncount=int(ncount),
                utility_nats=float(utility[d]), shaping_nats=float(shaping[d]),
                neutrons=dose, price=price,
                score=float(utility[d] / price),
                expected_sigma_fit_meV={
                    b: (float(sigma_sum[d, k] / sigma_mass[d, k]) if sigma_mass[d, k] > 0.0
                        else math.inf) for k, b in enumerate(branches)},
                expected_assignment={
                    t: (float(assign_sum[d, branches.index(t)] / sigma_mass[d, branches.index(t)])
                        if sigma_mass[d, branches.index(t)] > 0.0 else 0.0) for t in targets},
                e_pred_meV=e_pred))
    return out

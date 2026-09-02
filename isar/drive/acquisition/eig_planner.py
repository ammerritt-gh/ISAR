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
sigma.  It is measured as the half-width of the 68.27% highest-density interval of that posterior,
so a sharp bimodal posterior -- two rival peaks, 50/50 -- fails the bar however narrow each mode is.
The deficit at a point is ``max(log(hw / tau), 0)`` and the prize is met when every deficit is zero.
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
    "fisher_sigma_meV", "branch_counts", "hdi_halfwidth", "target_posterior",
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
NULL_DETECTION_COUNTS = 5.0
#: The declared probability that the fitter misses a peak it should have detected.  A null scan is
#: evidence, not proof, so a contradicted scenario is down-weighted by this factor rather than
#: killed: this is a model parameter of the fitter's reliability, not a placement rule.  Two nulls
#: over the same band multiply, so the band's weight falls; posterior mass flows back to it once the
#: rest of the domain has returned nulls too, which is what brings the planner back at higher
#: exposure instead of leaving the band excluded for ever.
NULL_MISS_PROBABILITY = 0.1
#: The prize is binary per point -- known to +-tau, or not -- so utility counts COMPLETIONS.  This
#: is the weight on the log-deficit term kept beside it: it orders the designs that cannot finish a
#: point by how much they learn, and is small enough that it can never outweigh a completion.
#: Without it a half-count scan leaving sigma just over the bar scored ~99% of the value of the
#: full-count scan that would actually finish the point, at half the cost, so the point never got
#: finished.
SHAPING = 0.02
#: How far a real fit's sigma sits above the Cramer-Rao bound at the counts we run.  The bound is
#: optimistic at low counts; this is a declared model of the fitter's reliability, like
#: :data:`NULL_MISS_PROBABILITY`, and it applies to the PLANNER's expectations only -- the world
#: delivers the bare bound, so the planner has to earn its margin rather than assume it.
FIT_SIGMA_INFLATION = 3.0   # live 2026-09-03: published calibrated halfwidths 0.06-0.09 meV against
                            # a Cramér-Rao bound of ~0.02-0.03 at 5e6 counts/point (inflation-calibrated
                            # intervals, not the raw HESSE sigma); 1.25 bought scans that could not finish
#: Anchor weights are rounded to this many decimals before designs are grouped for the cross-point
#: term (A4 caching).  A belief is insensitive to a 0.005 change in one anchor weight, and grouping
#: is what keeps the per-design cross term inside the time budget.
ANCHOR_WEIGHT_DECIMALS = 2

#: The intensity map's class prior, before any scan: counts per ``PRIOR_NCOUNT_REFERENCE`` neutrons.
PRIOR_NCOUNT_REFERENCE = 1.0e7
PRIOR_PEAK_COUNTS_MEV = 15.0
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
    """One scan already executed at a grid point, and which branches' peaks it delivered.

    Negative evidence is per branch, not per scan: a scan that fitted the acoustic but no optic is
    evidence against the optic lying inside its window at its exposure, exactly as a scan that
    fitted nothing is evidence against every branch there.  ``seen`` names the branches whose peak
    the fit delivered; every other modelled branch is contradicted wherever that window would have
    detected it.
    """

    e_lo: float
    e_hi: float
    ncount: int
    seen: frozenset[str] = frozenset()


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
    ncount_ladder: tuple[float, ...] = (0.5, 1.0, 2.0, 4.0)
    max_points: int = 220
    step_fraction: float = 0.25
    scans: Mapping[int, Sequence[ExecutedScan]] = field(default_factory=dict)


@dataclass(frozen=True)
class PointState:
    grid_index: int
    hkl: tuple[float, float, float]
    u: float
    exempt: bool
    halfwidth_meV: float          # 68.27% HDI half-width of the target-energy posterior
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
    utility_nats: float
    cost_neutrons: float
    score: float
    expected_sigma_fit_meV: Mapping[str, float]
    expected_assignment: Mapping[str, float]
    e_pred_meV: Mapping[str, tuple[float, float]]


@dataclass(frozen=True)
class Decision:
    stop: bool
    stop_reason: str
    chosen: RankedDesign | None
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


def target_posterior(energies, sigmas, valid, mus, sigs, target_index: int, absent=None):
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
                 domain_hi: float | None = None) -> list[dict[str, float]]:
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
    ``(rate (S,N), deriv (S, 2B+2, N), branch_counts (S, B))`` with parameter order
    ``(omega_b..., P_b..., elastic, background)``.  ``dn/domega`` is deliberately ignored.
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
    return rate, deriv, np.sum(amp * shape, axis=2)


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
    rate, deriv, _ = _rates(np.array([[float(e) for e in energies]]), sres, grid, peak,
                            float(scale.elastic_counts_meV), float(scale.background_per_point),
                            temperature_K)
    return tuple(float(v) for v in _sigma_fit(rate, deriv, float(ncount), len(peak), sres)[0, 0])


def branch_counts(energies: Sequence[float], *, e_lo: float, e_step: float, n_points: int,
                  fwhm_meV: float, temperature_K: float, scale: IntensityScale,
                  branches: Sequence[str], ncount: float) -> tuple[float, ...]:
    """Expected counts each branch contributes inside one window (both lobes, Bose applied)."""
    grid = float(e_lo) + float(e_step) * np.arange(int(n_points), dtype=float)
    sres = float(fwhm_meV) * FWHM_TO_SIGMA
    peak = [float(scale.peak_counts_meV[b]) for b in branches]
    _, _, counts = _rates(np.array([[float(e) for e in energies]]), sres, grid, peak,
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


def _deficits_batch(padded, mus, sigs, target_index, width: float, tau: float, exempt):
    """HDI half-width and deficit of every point in a padded batch, in one vectorised pass.

    ``mus`` / ``sigs`` are per-branch sequences of per-point belief predictions.
    """
    e, s, valid = padded
    w, w_none = target_posterior(e, s, valid, [np.asarray(m) for m in mus],
                                 [np.asarray(g) for g in sigs], target_index)
    hw = np.where(valid.any(axis=-1), hdi_halfwidth(e, s, w, w_none, width), 0.5 * width)
    deficit = np.maximum(np.log(np.maximum(hw, 1e-300) / tau), 0.0)
    return hw, np.where(np.asarray(exempt, dtype=bool), 0.0, deficit)


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
    halfwidth, deficit_now = [], []
    for k, t in enumerate(targets):
        hw, d = _deficits_batch(padded_all, [[q[0] for q in row] for row in preds_all],
                                [[q[1] for q in row] for row in preds_all], branches.index(t),
                                width, tau, [exempt[m][k] for m in range(n_path)])
        halfwidth.append(hw)
        deficit_now.append(d)
    halfwidth = np.array(halfwidth).T
    deficit_now = np.array(deficit_now).T

    per_point = tuple(
        PointState(grid_index=m, hkl=tuple(float(c) for c in inp.path[m]), u=float(inp.u[m]),
                   exempt=all(exempt[m]),
                   halfwidth_meV=float(halfwidth[m].max()),
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
                                   measured, executed, width, ladder))
    if not designs:
        return Decision(stop=True, stop_reason="no_admissible_window", chosen=None, ranked=(),
                        per_point=per_point)

    designs.sort(key=lambda d: (-d.score, d.cost_neutrons, _design_hash(d)))
    best = designs[0].score
    cut = best * (1.0 - TIE_REL) if best > 0.0 else best - TIE_REL
    tied = [d for d in designs if d.score >= cut]
    chosen = min(tied, key=lambda d: (d.cost_neutrons, _design_hash(d)))
    ranked = [chosen] + [d for d in designs if d is not chosen][:9]
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


def _scenarios(i, inp, branches, predict, executed):
    """Joint scenario energies and weights, down-weighted by what the scans here did NOT find (A6).

    A branch's node is contradicted by an executed scan when it lies inside that scan's window, the
    scan would have detected it there, and the scan did not deliver that branch's peak.  A scan that
    found the acoustic and not the optic therefore argues against the optic in its window just as a
    scan that found nothing argues against both.
    """
    step = float(inp.step_fraction) * float(inp.fwhm_meV[i])
    scans = executed.get(i, ())
    mu_all = [predict(b, inp.u[i])[0] for b in branches]
    nodes, weights = [], []
    reweighted = [False] * len(branches)
    for bi, b in enumerate(branches):
        mu, sig = predict(b, inp.u[i])
        x = np.maximum(mu + _SQRT2 * sig * _GH8_X, 0.0)
        w = _GH8_W.copy()
        for scan in scans:
            if b in scan.seen:
                continue                      # this scan delivered that peak: no evidence against
            inside = (x >= float(scan.e_lo)) & (x <= float(scan.e_hi))
            if not inside.any():
                continue
            n_pts = max(int(math.floor((float(scan.e_hi) - float(scan.e_lo)) / step)) + 1, 1)
            detectable = np.array([
                branch_counts([e if k == bi else mu_all[k] for k in range(len(branches))],
                              e_lo=float(scan.e_lo), e_step=step, n_points=n_pts,
                              fwhm_meV=float(inp.fwhm_meV[i]),
                              temperature_K=float(inp.temperature_K), scale=inp.scale,
                              branches=branches, ncount=int(scan.ncount))[bi] for e in x])
            contradicted = inside & (detectable >= NULL_DETECTION_COUNTS)
            w = np.where(contradicted, w * NULL_MISS_PROBABILITY, w)
            reweighted[bi] |= bool(contradicted.any())
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


def _designs_at(i, inp, branches, rows, targets, predict, exempt, deficit_now, measured, executed,
                width, ladder):
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
    om, weight_s, truncated, marginals = _scenarios(i, inp, branches, predict, executed)
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
    n_win, n_exp = len(windows), len(ladder)
    sigma_fit = np.empty((n_win, n_exp, n_scen, n_branch))
    for wi, (lo, _hi, step, npts) in enumerate(windows):
        grid = lo + step * np.arange(npts, dtype=float)
        rate, deriv, _ = _rates(om, sres, grid, peak, elastic, background, float(inp.temperature_K))
        sigma_fit[wi] = _sigma_fit(rate, deriv, np.array(ladder, dtype=float), n_branch,
                                   sres) * FIT_SIGMA_INFLATION
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
    admit_frac = np.einsum("s,dsb->db", weight_s, admitted.astype(float))
    anchor_w = np.einsum("s,dsb->db", weight_s, np.where(fit_measured, own, 0.0))
    present = (admit_frac > 0.5) & (anchor_w > 0.0)

    rounded = np.round(anchor_w, ANCHOR_WEIGHT_DECIMALS)
    groups: dict[tuple, list[int]] = {}
    for d in range(n_design):
        key = tuple((bool(present[d, b]), float(rounded[d, b])) for b in range(n_branch))
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
    others = [(m, c) for m, c in sorted(measured.items()) if m != i]
    padded_others = _pad_candidates([c for _m, c in others]) if others else None
    for key, members in groups.items():
        idx = np.array(members)
        updated = dict(rows)
        changed = False
        for b in range(n_branch):
            if key[b][0]:
                updated[branches[b]] = upsert_anchor(rows[branches[b]], float(inp.u[i]),
                                                     float(mus[b]), key[b][1])
                changed = True
        gain = np.zeros(len(members))
        for k, t in enumerate(targets):
            if changed and others:
                preds_m = [[predict(b, inp.u[m], updated[b]) for m, _c in others]
                           for b in branches]
                _hw, after = _deficits_batch(padded_others,
                                             [[q[0] for q in row] for row in preds_m],
                                             [[q[1] for q in row] for row in preds_m],
                                             branches.index(t), width, tau,
                                             [exempt[m][k] for m, _c in others])
                now_deficit = np.array([deficit_now[m][k] for m, _c in others])
                # Completions at the other points, plus the shaped remainder.
                gain += float(np.count_nonzero(after <= 0.0) - np.count_nonzero(now_deficit <= 0.0)
                              + SHAPING * float(now_deficit.sum() - after.sum()))
            upd = [predict(b, inp.u[i], updated[b]) for b in branches]
            w, w_none = target_posterior(comp_e[idx], comp_s[idx], valid[idx],
                                         [q[0] for q in upd], [q[1] for q in upd],
                                         branches.index(t), absent[idx][:, None, :])
            hw = hdi_halfwidth(comp_e[idx], comp_s[idx], w, w_none, width)
            after = (np.zeros_like(hw) if exempt[i][k]
                     else np.maximum(np.log(np.maximum(hw, 1e-300) / tau), 0.0))
            gain += (np.einsum("s,ds->d", weight_s, (after <= 0.0).astype(float))
                     - float(deficit_now[i][k] <= 0.0)
                     + SHAPING * np.einsum("s,ds->d", weight_s, deficit_now[i][k] - after))
        utility[idx] = gain
    utility = np.maximum(utility, 0.0)

    sigma_mass = np.einsum("s,dsb->db", weight_s, fit_measured.astype(float))
    sigma_sum = np.einsum("s,dsb->db", weight_s, np.where(fit_measured, sigma_fit, 0.0))
    assign_sum = np.einsum("s,dsb->db", weight_s, np.where(fit_measured, own, 0.0))
    e_pred = {b: (float(belief[k][0]), float(belief[k][1])) for k, b in enumerate(branches)}

    out: list[RankedDesign] = []
    for wi, (lo, _hi, step, npts) in enumerate(windows):
        for ei, ncount in enumerate(ladder):
            d = wi * n_exp + ei
            cost = float(npts) * float(ncount)
            out.append(RankedDesign(
                grid_index=i, hkl=tuple(float(c) for c in inp.path[i]),
                e_lo=float(lo), e_hi=float(lo + step * (npts - 1)), e_step=float(step),
                n_points=int(npts), ncount=int(ncount),
                utility_nats=float(utility[d]), cost_neutrons=cost,
                score=float(utility[d] / cost),
                expected_sigma_fit_meV={
                    b: (float(sigma_sum[d, k] / sigma_mass[d, k]) if sigma_mass[d, k] > 0.0
                        else math.inf) for k, b in enumerate(branches)},
                expected_assignment={
                    t: (float(assign_sum[d, branches.index(t)] / sigma_mass[d, branches.index(t)])
                        if sigma_mass[d, branches.index(t)] > 0.0 else 0.0) for t in targets},
                e_pred_meV=e_pred))
    return out

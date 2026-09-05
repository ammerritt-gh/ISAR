"""Branch beliefs: a per-branch dispersion ω_b(u) with an honest σ (steering;
docs/MEASUREMENT_STEERING.md §2, confidence ladder).

Replaces the hard branch-identity gates (the ``_is_clean_anchor`` provenance gate, the ``prior_for``
global polyfit, the ``primary_phonon_index`` nearest-prior rule) with a belief the session prior,
the cross-q evidence update and the near-Γ constrained recipe all read. Physics enters as *priors
in the likelihood*, never as switches:

- **zone-centre gaplessness** — the acoustic branch has ω₀→0 at the Γ-point, so the acoustic belief
  carries a soft zone anchor ``(u=0, ω₀=0)`` whose influence *decays* with reduced-q distance. "Near
  the zone centre the low-E feature is probably acoustic" is therefore emergent (and a real dispersing
  anchor outvotes it); it is not a coded rule. One zone anchor + one real anchor already pin the whole
  low-q dispersion line (intercept 0, slope from the real point).
- **branch continuity in q** — each branch is a weighted local-linear dispersion ω_b(u) with an honest
  σ (interpolation shrinks, extrapolation grows), the ``drive/acquisition/model.py`` pattern.
- **branch persistence** — a mild occupancy prior ∝ a branch's anchor count, plus a fixed new/spurious
  escape so a feature never has to be forced onto a known branch.

This module no longer decides which feature at a q IS the target. Per-q feature selection retired
with the ``target_selector`` values the EIG planner replaced: the planner maps a DECLARED branch,
and the authoritative label is assigned once every pooled master has updated, over the whole
objective path in both directions (``evidence_update._apply_tracked_branch``).

Session-side (imports analyze/schema). The drive never imports this module; it reads the persisted
``branch_belief`` / ``branch_id`` from ``card.state.json`` / ``results.csv``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from isar.probability import (
    continuity_adjust, lower_truncated_normal_moments, truncated_normal_moments)

from isar.analyze.truth import _fold
from isar.session.session_prior import is_unconstrained_prior_point

# --- tunables (module-level so tests / a future card can override) ---------------------------
SIGMA_SLACK = 0.3          # meV — generous per-feature noise floor + nominal anchor σ (§6)
ZONE_ANCHOR_WEIGHT = 1.0   # weight of the acoustic (u=0, ω₀=0) zone anchor
ZONE_SIGMA = 0.2           # meV — how tightly ω₀→0 is believed at the Γ-point (pins the intercept)
ACOUSTIC_MAX_SLOPE = 6.0   # meV / rlu — cold-start dispersion-uncertainty scale (acoustic group-velocity
#                            scale). Tight enough that a mid-branch scan discriminates acoustic from optic
#                            (so the belief BOOTSTRAPS by seeding from the clearest low-q points and grows
#                            outward), broad enough to still admit a real steep low-q acoustic.
LOCALITY_BW = 0.35         # rlu — Gaussian locality bandwidth (fixed, so 2 anchors stay well-weighted)
WINDOW_K = 6               # nearest anchors entering each local fit
NEW_LEVEL = 0.01           # flat likelihood floor for the new/spurious hypothesis (per meV).
#                            Canonical definition; the EIG planner mirrors it (it may not
#                            import the session package) and the two must not drift.
RESOLVABLE_FWHM_FACTOR = 1.0   # ω₀ ≳ this × gauss_fwhm to be a position-separable peak
ACOUSTIC = "acoustic"
NEW = "new"
BRANCH_ASSIGNMENT_SCORER_REVISION = "isar.branch_assignment/1"


def probability_semantics(probabilities: dict | None) -> dict:
    """Producer-owned declarations for local branch-assignment outputs."""
    from isar.schema.statistics import authority_scopes

    labels = sorted(str(label) for label in (probabilities or {}))
    state_space = {"kind": "branch_assignment", "labels": labels}
    common = {
        "source_measure_semantics": "normalized_assignment_weight",
        "scorer_revision": BRANCH_ASSIGNMENT_SCORER_REVISION,
        "calibration_reference": None,
        "conditioning": "observed_features_and_branch_beliefs",
        "state_space": state_space,
        "inference_paradigm": "normalized_score_distribution",
        "authority_scopes": authority_scopes(
            single_fit_delivery="active",
            user_diagnostic="active",
        ),
    }
    return {
        "branch_posterior": {**common, "quantity_kind": "distribution"},
        "branch_weight": {**common, "quantity_kind": "marginal_weight"},
        "assignment_entropy": {**common, "quantity_kind": "entropy"},
    }


def reduced_q(q, period: float) -> float:
    """Reduced-zone |q| magnitude: ``sqrt(Σ fold(q_i)²)`` with each component folded into ``period``
    (the coordinate the acoustic branch is gapless in — 0 at a zone centre). Reuses the engine/truth
    fold so the session and the scorer agree on where the zone centres are."""
    return float(math.sqrt(sum(_fold(float(c), period) ** 2 for c in q)))


@dataclass(frozen=True)
class Feature:
    """One observed candidate feature at a q — a fitted DHO mode or a model-free descriptor peak."""
    energy: float          # ω₀ (meV)
    sigma_e: float         # 1σ position uncertainty (meV), floored
    intensity: float       # amplitude / integrated counts (for provenance; not used in continuity)
    significance: float    # detection σ (descriptor) or +inf (a converged fit mode)
    source: str            # "mode" | "descriptor"


def _peak_sigma(width: float, floor: float) -> float:
    """Position σ from a peak FWHM (÷2.355), floored."""
    s = float(width) / 2.3548 if width and width > 0 else 0.0
    return max(s, floor)


def features_from(modes, descriptor, *, gauss_fwhm: float) -> list[Feature]:
    """Merge fitted DHO ``modes`` (each ``{omega0, omega0_err, gamma, amplitude}``) and model-free
    ``descriptor`` peaks into a de-duplicated feature list in PHONON energy ``|ω|``. A fit mode and
    a descriptor peak within ½·resolution are the same physical feature; the fit mode wins (it
    carries an ω₀ error). A negative-energy (anti-Stokes) descriptor peak is the detailed-balance
    mirror of the same phonon, so it is folded to ``|E|`` and merged with its Stokes partner rather
    than entering as a rival feature — otherwise the mirror splits the branch-assignment weight and
    the dominance triage abstains on every scan whose anti-Stokes wing is visible."""
    floor = max(0.1 * float(gauss_fwhm), 1e-3)
    merge_tol = 0.5 * float(gauss_fwhm)
    feats: list[Feature] = []
    for m in modes or []:
        w = m.get("omega0")
        if w is None or not np.isfinite(w):
            continue
        err = m.get("omega0_err")
        se = float(err) if (err is not None and np.isfinite(err) and err > 0) else floor
        feats.append(Feature(abs(float(w)), max(se, floor), float(m.get("amplitude", 0.0) or 0.0),
                             float("inf"), "mode"))
    if descriptor is not None:
        for p in getattr(descriptor, "peaks", ()) or ():
            e = abs(float(p.energy))
            if any(abs(e - f.energy) <= merge_tol for f in feats if f.source == "mode"):
                continue    # already carried by a fit mode (or its anti-Stokes mirror)
            partner = next((i for i, f in enumerate(feats)
                            if f.source == "descriptor" and abs(e - f.energy) <= merge_tol), None)
            if partner is not None:    # Stokes/anti-Stokes wings of one phonon: pool the evidence
                f = feats[partner]
                inten = f.intensity + float(getattr(p, "integrated_intensity", 0.0) or 0.0)
                feats[partner] = Feature(f.energy, f.sigma_e, inten,
                                         max(f.significance,
                                             float(getattr(p, "significance", 0.0) or 0.0)),
                                         "descriptor")
                continue
            feats.append(Feature(e, _peak_sigma(getattr(p, "width", 0.0), floor),
                                 float(getattr(p, "integrated_intensity", 0.0) or 0.0),
                                 float(getattr(p, "significance", 0.0) or 0.0), "descriptor"))
    feats.sort(key=lambda f: f.energy)
    return feats


@dataclass
class BranchBelief:
    """A branch's weighted local-linear dispersion ω_b(u) over accumulated (reduced-q, ω₀, weight)
    anchors. The acoustic branch additionally carries the soft zone anchor ``(0, 0)``."""
    branch_id: str
    us: np.ndarray
    ys: np.ndarray
    ws: np.ndarray
    zone_anchor: bool = False
    window_k: int = WINDOW_K
    parameter_prior: dict | None = None
    period: float | None = None      # zone period; without it the curvature clause is skipped

    @property
    def n_anchors(self) -> int:
        return int(self.us.size)

    def _anchors(self):
        """``(us, ys, precision)`` — real anchors carry precision ``weight/σ²`` (σ = SIGMA_SLACK); the
        acoustic zone anchor ``(0, 0)`` carries ``ZONE_ANCHOR_WEIGHT/ZONE_SIGMA²`` (a firm but not
        overriding pin on the intercept)."""
        us, ys = self.us, self.ys
        prec = np.maximum(self.ws, 0.0) / (SIGMA_SLACK ** 2)
        if self.zone_anchor:
            us = np.concatenate([[0.0], us])
            ys = np.concatenate([[0.0], ys])
            prec = np.concatenate([[ZONE_ANCHOR_WEIGHT / (ZONE_SIGMA ** 2)], prec])
        return us, ys, prec

    def predict(self, u: float, *, domain_hi: float | None = None) -> tuple[float, float]:
        """``(mu, sigma)`` of ω₀ at reduced-q ``u``. Weighted local-linear (precision × fixed-bandwidth
        Gaussian locality), so σ shrinks where anchors are dense/precise and grows on extrapolation.
        With only the zone anchor (acoustic cold start) ``mu≈0`` and ``sigma`` grows as
        ``ACOUSTIC_MAX_SLOPE·u`` — tight enough near Γ to reject a 6-meV feature, honest far out."""
        if self.parameter_prior:
            return self._predict_with_parameter_prior(float(u), include_prior=True,
                                                     domain_hi=domain_hi)
        u = float(u)
        us, ys, prec = self._anchors()
        n = us.size
        if n == 0:
            return 0.0, 1e3
        if n == 1:
            return float(ys[0]), ACOUSTIC_MAX_SLOPE * abs(u - float(us[0])) + SIGMA_SLACK
        if not self.zone_anchor and n < 3:
            # An unanchored (no zone pin) branch with two nearby noisy anchors must not assert a
            # slope: the 2-point line through banked optic anchors can plunge into the acoustic band
            # and claim its features. Flat, honest fallback: precision-weighted level, σ growing at
            # the group-velocity scale with distance to the nearest anchor.
            mu = float(np.average(ys, weights=np.maximum(prec, 1e-9)))
            return mu, ACOUSTIC_MAX_SLOPE * float(np.min(np.abs(us - u))) + SIGMA_SLACK
        d = np.abs(us - u)
        k = min(n, self.window_k)
        idx = np.argsort(d, kind="stable")[:k]
        wu, wy = us[idx], ys[idx]
        w = prec[idx] * np.exp(-0.5 * (d[idx] / LOCALITY_BW) ** 2)   # precision × Gaussian locality
        w = np.maximum(w, 1e-9)
        if len(set(np.round(wu, 9))) < 2:
            return float(np.average(wy, weights=w)), ACOUSTIC_MAX_SLOPE * abs(u - float(wu[0])) + SIGMA_SLACK
        uref = float(np.average(wu, weights=w))
        X = np.column_stack([np.ones_like(wu), wu - uref])
        XtWX = X.T @ (w[:, None] * X)
        try:
            cov = np.linalg.inv(XtWX)
        except np.linalg.LinAlgError:
            return float(np.average(wy, weights=w)), max(float(np.sqrt(1.0 / np.sum(w))), SIGMA_SLACK)
        beta = cov @ (X.T @ (w * wy))
        xq = np.array([1.0, u - uref])
        mu = float(xq @ beta)
        var = float(xq @ cov @ xq)
        # honest extrapolation growth past the anchored range
        umin, umax = float(us.min()), float(us.max())
        span = max(umax - umin, 1e-9)
        frac = (umin - u) / span if u < umin else (u - umax) / span if u > umax else 0.0
        return mu, max(math.sqrt(max(var, 0.0)), SIGMA_SLACK) * (1.0 + max(frac, 0.0))

    def predict_data_only(self, u: float) -> tuple[float, float]:
        """Prediction from measured anchors only; never lets a prior confirm itself."""
        if not self.parameter_prior:
            return self.predict(u)
        return self._predict_with_parameter_prior(float(u), include_prior=False)

    def _predict_with_parameter_prior(self, u: float, *, include_prior: bool,
                                      domain_hi: float | None = None) -> tuple[float, float]:
        """Local linear Gaussian update followed by marginal truncation moments.

        The class prior's third clause -- bounded by the energy domain -- is this truncation:
        an energy outside the authorized band is not a hypothesis the program can hold, so with
        ``domain_hi`` given the predictive is truncated to ``[0, domain_hi]`` at BOTH ends.  A cold
        branch far from the fold then predicts "somewhere in the band" (mean about hi/2, sigma
        about hi/sqrt(12)) instead of a one-sided runaway to tens of meV outside it.
        """
        if self.us.size:
            d = np.abs(self.us - u)
            idx = np.argsort(d, kind="stable")[:min(self.us.size, self.window_k)]
            wu, wy = self.us[idx], self.ys[idx]
            weights = (np.maximum(self.ws[idx], 0.0) / (SIGMA_SLACK ** 2)
                       * np.exp(-0.5 * (d[idx] / LOCALITY_BW) ** 2))
            X = np.column_stack([np.ones_like(wu), wu])
            precision = X.T @ (weights[:, None] * X)
            rhs = X.T @ (weights * wy)
        else:
            precision = np.zeros((2, 2), dtype=float)
            rhs = np.zeros(2, dtype=float)
        if include_prior:
            prior = self.parameter_prior or {}
            confidence = max(float(prior.get("confidence", 0.0)), 0.0)
            gap = prior.get("zc_energy") or {}
            slope = prior.get("slope_zc") or {}
            means = np.array([float(gap.get("mean", gap.get("mode", 0.0))),
                              float(slope.get("mean", 0.0))])
            sigmas = np.array([max(float(gap.get("sigma", 1e3)), 1e-9),
                               max(float(slope.get("sigma", 1e3)), 1e-9)])
            # The (gap, slope) prior describes the line AT THE FOLD, so it is admitted into a
            # LOCAL fit only as far as that fit is still near the fold -- through the same locality
            # kernel the anchors use. At full precision everywhere the zone-centre pin tilts the
            # local line, and near the zone boundary a saturating branch is predicted about a meV
            # high, confidently enough that the rival branch's peak reads as its own.
            prior_precision = (confidence / np.square(sigmas)
                               * math.exp(-0.5 * (float(u) / LOCALITY_BW) ** 2))
            precision += np.diag(prior_precision)
            rhs += prior_precision * means
        if not np.any(precision):
            return 0.0, 1e3
        try:
            cov = np.linalg.pinv(precision, hermitian=True)
            beta = cov @ rhs
        except np.linalg.LinAlgError:
            return 0.0, 1e3
        xq = np.array([1.0, u])
        mu = float(xq @ beta)
        sigma = max(math.sqrt(max(float(xq @ cov @ xq), 0.0)), SIGMA_SLACK)
        if self.us.size:
            # Continuity governs the predictive beyond the anchors: the mean reverts to the level at the
            # edge of the anchored range instead of carrying the fitted slope onward, and the sigma
            # reverts with it and widens at the declared group velocity with the distance to the nearest
            # measurement. Inside the anchored range this leaves the local fit unchanged.
            edge = min(max(float(u), float(self.us.min())), float(self.us.max()))
            mu, sigma = continuity_adjust(
                mu, sigma, float(np.array([1.0, edge]) @ beta), max(math.sqrt(max(float(np.array([1.0, edge]) @ cov @ np.array([1.0, edge])),
                                    0.0)), SIGMA_SLACK),
                abs(float(u) - edge), float(np.min(np.abs(self.us - float(u)))))
        if not include_prior:
            return mu, sigma
        if domain_hi is None:
            return lower_truncated_normal_moments(mu, sigma)
        return truncated_normal_moments(mu, sigma, 0.0, float(domain_hi))


def build_beliefs(points, *, sample, t_bin, period: float,
                  t_round: float = 5.0, hypotheses: list[dict] | None = None) -> dict[str, BranchBelief]:
    """Build ``{branch_id: BranchBelief}`` from ``state.dispersion_prior`` points for one (sample,
    T-bin). Each point carries ``q``, ``omega0``, ``branch_id`` (default ``"acoustic"``) and a soft
    assignment ``weight`` (default 1.0). The acoustic belief always exists (zone anchor), even with
    no real anchors, so a cold-start low-q feature is still testable against it."""
    by: dict[str, list] = {}
    for p in points:
        if p.get("sample") != sample:
            continue
        tb = p.get("t_bin", p.get("T"))
        if tb is None or abs(float(tb) - float(t_bin)) > (0.5 * t_round if t_round else 1e-9):
            continue
        w = p.get("omega0")
        if w is None or not np.isfinite(float(w)):
            continue
        if not is_unconstrained_prior_point(p):
            continue                       # contract 4: a constrained/demoted fit never seeds a branch
        bid = p.get("branch_id") or ACOUSTIC
        by.setdefault(bid, []).append(
            (reduced_q(p["q"], period), float(w), float(p.get("weight", 1.0))))
    resolved = {str(h.get("name")): h for h in (hypotheses or []) if isinstance(h, dict)}
    beliefs: dict[str, BranchBelief] = {}
    for bid, rows in by.items():
        us = np.array([r[0] for r in rows], dtype=float)
        ys = np.array([r[1] for r in rows], dtype=float)
        ws = np.array([r[2] for r in rows], dtype=float)
        prior = resolved.get(bid)
        beliefs[bid] = BranchBelief(bid, us, ys, ws,
                                    zone_anchor=(bid == ACOUSTIC and prior is None),
                                    parameter_prior=prior, period=float(period))
    for bid, prior in resolved.items():
        if bid not in beliefs and bid != "null":
            empty = np.array([], dtype=float)
            beliefs[bid] = BranchBelief(bid, empty, empty, empty, parameter_prior=prior,
                                        period=float(period))
    if ACOUSTIC not in beliefs:
        empty = np.array([], dtype=float)
        prior = resolved.get(ACOUSTIC)
        beliefs[ACOUSTIC] = BranchBelief(ACOUSTIC, empty, empty, empty,
                                         zone_anchor=prior is None, parameter_prior=prior,
                                         period=float(period))
    return beliefs

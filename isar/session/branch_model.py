"""The cross-q branch model (docs/MEASUREMENT_STEERING.md §7, steering Phase 2).

Shares more than ω₀ across a dispersion branch: Γ and the (portable, exposure-normalised) intensity
A, so a fit near the zone centre — where the acoustic peak merges with the elastic line and Γ/A go
unidentifiable — can be softly constrained to the branch and recover ω₀ (the LOW-q rescue).

The anchor intensity is the **intrinsic** amplitude per exposure (H4: the lineshape's A behind
the fitted height carries no thermal occupation — the model applies it pointwise), so the acoustic
law below is exact. The fit parameter is the convolved height ``phonon.h``, so the constraints this
module emits convert the branch's A to a height with the caller's ``height_per_intrinsic`` (the
:class:`~isar.analyze.lineshapes.UnitPeak` height at the predicted ω₀ and Γ).

**Coordinate = ω₀, not q** (owner decision): Γ and A are modelled as functions of ω₀ itself, because
the acoustic intensity law is literally A ∝ 1/ω (intrinsic — the measured kT/ω² classical form is
this law times the occupation the model now owns). The dispersion prior already predicts ω₀(q), so a
target q's constraint is ``predict(branch, ω₀_pred)``. This is path-free (works in batch and drive)
and avoids the fragile "dominant-varying-Q" heuristic. An explicit ``branch_id`` keeps anchors from a
different branch (or across a mode-crossing) out; the MVP builds/uses only ``"acoustic"``.

Built from the accumulated ``dispersion_prior`` points (which now carry Γ and a portable
:class:`~isar.schema.steering.IntensityMeasure`), gated to **clean anchors**: resolved+fold (already,
by ``session_prior.add_point``), well-separated from the elastic line, and carrying Γ and intensity.
A is kept **soft-only with a loose relative σ** (contract: it absorbs monitor / focusing-asymmetry
drift; never hard-fixed).
"""
from __future__ import annotations

from dataclasses import dataclass
import logging

import numpy as np

from isar.analyze.prior import width_ceiling
from isar.schema.steering import RETIRED_AMPLITUDE_BASES, ParamConstraints
from isar.session.session_prior import is_unconstrained_prior_point

log = logging.getLogger(__name__)

BRANCH_ID = "acoustic"          # MVP: a single acoustic branch
MIN_ANCHORS = 3                 # too few to fit a trend
SEP_FWHM = 1.5                  # ω₀ must clear the elastic line by this × resolution to be an anchor
GAMMA_FLOOR = 1e-3
GAMMA_SIGMA_FLOOR = 0.05        # meV — never claim Γ tighter than this from the branch
A_REL_SIGMA_FLOOR = 0.3         # loose relative σ on intensity (absorbs systematics; A stays soft)
_EDGE = 1e-9


@dataclass(frozen=True)
class BranchPrediction:
    omega0: float                # the query ω₀ (branch does not refine it — that is the prior's job)
    gamma: float
    gamma_sigma: float
    amplitude_norm: float        # predicted exposure-normalised intensity (IntensityMeasure.value units)
    amplitude_rel_sigma: float
    in_anchor_range: bool        # False ⇒ extrapolating ⇒ σ inflated (do not hard-fix off-range, Phase 3)
    n_anchors: int
    branch_id: str = BRANCH_ID

    def soft_constraints(self, exposure, height_per_intrinsic: float) -> ParamConstraints:
        """Soft :class:`ParamConstraints` for a low-q fit: Γ pinned to ``gamma ± gamma_sigma`` and the
        height ``phonon.h`` to the branch intensity scaled to *this scan's* exposure and converted
        to a height (``amplitude_norm × exposure × height_per_intrinsic``) with a loose relative σ.
        Never hard (the intensity absorbs systematics)."""
        soft: dict = {}
        if self.gamma_sigma > 0:
            soft["phonon.Gamma"] = (float(self.gamma), float(self.gamma_sigma))
        if exposure and float(exposure) > 0 and self.amplitude_norm > 0:
            raw_h = float(self.amplitude_norm) * float(exposure) * float(height_per_intrinsic)
            soft["phonon.h"] = (raw_h, max(self.amplitude_rel_sigma, A_REL_SIGMA_FLOOR) * raw_h)
        return ParamConstraints(
            soft=soft,
            origins={name: "prior_predictive" for name in soft},
        )

    def hard_constraints(self, exposure, height_per_intrinsic: float) -> ParamConstraints:
        """The near-Γ **constrained** recipe (Phase A; the old ``examples/old_fitters`` ``Constrained``
        model): hard-fix BOTH Γ and the height ``phonon.h`` to the branch's 1/ω law at the predicted
        ω₀ (``amplitude_norm × exposure × height_per_intrinsic``), reducing the near-zero acoustic to
        ω₀-only. With the height fixed, ω₀ is pinned by the blob's *position* only — 0.34 FWHM from
        the origin, Stokes/anti-Stokes merged. The Bose-weighted brightness mechanism of the old
        intrinsic-amplitude coordinate (A fixed, the height following ω₀) is lost: accepted (T-1),
        with no functional tie; ``TODO.md`` DEFECT-10 records it. Used only in the near-Γ
        unresolvable regime, in-anchor (so the intensity is trustworthy); away from Γ the ordinary
        soft rescue applies (the height stays soft, absorbing systematics)."""
        hard: dict = {}
        if self.gamma > 0:
            hard["phonon.Gamma"] = float(self.gamma)
        if exposure and float(exposure) > 0 and self.amplitude_norm > 0:
            hard["phonon.h"] = (float(self.amplitude_norm) * float(exposure)
                                * float(height_per_intrinsic))
        return ParamConstraints(
            hard=hard,
            origins={name: "prior_predictive" for name in hard},
        )


def _is_clean_anchor(p: dict, sample, t_bin) -> bool:
    """A dispersion-prior point usable as a branch anchor: same sample+T bin, carries Γ and a portable
    intensity, and is well-separated from the elastic line (ω₀ ≳ 2Γ and ≳ SEP·resolution)."""
    if p.get("sample") != sample:
        return False
    if not is_unconstrained_prior_point(p):
        return False                       # contract 4: a constrained/demoted fit never seeds a branch
    tb = p.get("t_bin", p.get("T"))
    if tb is None or abs(float(tb) - float(t_bin)) > 1e-9:
        return False
    im = p.get("intensity") or {}
    w, g, a = p.get("omega0"), p.get("gamma"), im.get("value")
    if w is None or g is None or a is None:
        return False
    w, g, a = float(w), float(g), float(a)
    if g <= 0 or a <= 0:
        return False
    fwhm = float(im.get("gauss_fwhm", 0.0))
    return w >= max(2.0 * g, SEP_FWHM * fwhm)


def _linfit(xv, yv, wv=None) -> tuple[float, float, float]:
    """(slope, intercept, residual σ) of a weighted ``y ~ x``; falls back to (0, weighted-mean, std)
    when x is degenerate. ``wv`` (assignment weights, Phase A) default to 1 — a low-weight anchor
    (a soft branch assignment) contributes proportionally less to the trend."""
    xv = np.asarray(xv, dtype=float)
    yv = np.asarray(yv, dtype=float)
    wv = np.ones_like(xv) if wv is None else np.clip(np.asarray(wv, dtype=float), 0.0, None)
    if xv.size < 2 or len(set(np.round(xv, 9))) < 2 or wv.sum() <= 0:
        m = float(np.average(yv, weights=wv)) if wv.sum() > 0 else float(np.mean(yv))
        return 0.0, m, float(np.std(yv))
    slope, intercept = np.polyfit(xv, yv, 1, w=np.sqrt(wv))
    resid = yv - (slope * xv + intercept)
    return float(slope), float(intercept), float(np.std(resid))


def build_branch(points, *, sample, t_bin, branch_id: str = BRANCH_ID) -> dict | None:
    """Fit a branch from the clean anchors in ``points`` (``state.dispersion_prior``) **assigned to this
    branch_id**, or None if too sparse. Γ(ω₀) is a weighted local linear trend; log A(ω₀) is linear in
    log ω₀ (the 1/ω acoustic law is the slope≈−1 fit itself). Anchors are branch-filtered (Phase A: an
    optic-assigned anchor can no longer seed the acoustic Γ/A branch) and weighted by their soft
    assignment weight. Returns a plain JSON-able dict (persists in ``CardState.branch_models``)."""
    # H4/H1 clean break: a point persisted under a retired basis holds the MEASURED amplitude
    # A·(n+1). Nothing in the stored number distinguishes it from an intrinsic one and the
    # occupation is not recoverable after the fact, so mixing the two would fit log A(ω₀) across
    # incomparable anchors and emit intensity constraints wrong by (n+1). Excluded, never
    # reinterpreted — and said out loud, since a resumed campaign otherwise loses anchors silently.
    usable, retired = [], 0
    for p in points:
        if ((p.get("intensity") or {}).get("basis")) in RETIRED_AMPLITUDE_BASES:
            retired += 1
            continue
        usable.append(p)
    if retired:
        log.warning(
            "branch %r: excluded %d anchor(s) carrying a retired intensity basis (pre-H4 measured "
            "amplitude, not comparable with the intrinsic A this model fits); re-fit those scans "
            "to restore them", branch_id, retired)
    anchors = [p for p in usable if _is_clean_anchor(p, sample, t_bin)
               and (p.get("branch_id") or BRANCH_ID) == branch_id]
    if len(anchors) < MIN_ANCHORS:
        return None
    w = np.array([float(p["omega0"]) for p in anchors])
    g = np.array([float(p["gamma"]) for p in anchors])
    a = np.array([float(p["intensity"]["value"]) for p in anchors])
    wt = np.array([float(p.get("weight", 1.0)) for p in anchors])
    gs, gi, gsig = _linfit(w, g, wt)
    ls, li, lsig = _linfit(np.log(w), np.log(np.clip(a, 1e-30, None)), wt)
    return {"branch_id": branch_id, "sample": sample, "t_bin": float(t_bin),
            "w_min": float(w.min()), "w_max": float(w.max()), "n_anchors": len(anchors),
            "gamma": [gs, gi, gsig], "logA": [ls, li, lsig]}


def predict(branch: dict, omega0: float) -> BranchPrediction:
    """Predict (Γ, A) at ``omega0`` from a built branch, inflating σ when extrapolating past the
    anchor ω₀ range (honest weakening where the branch is guessing)."""
    w = float(omega0)
    gs, gi, gsig = branch["gamma"]
    ls, li, lsig = branch["logA"]
    w_min, w_max = float(branch["w_min"]), float(branch["w_max"])
    span = max(w_max - w_min, 1e-6)
    in_range = (w_min - _EDGE) <= w <= (w_max + _EDGE)
    dist = 0.0 if in_range else (w_min - w if w < w_min else w - w_max)
    infl = 1.0 + dist / span                               # σ grows linearly outside the anchor range

    # anchors fitted under an older recipe may be wider than a lone fit may now be (recipe /7)
    gamma = max(min(gs * w + gi, width_ceiling(w)), GAMMA_FLOOR)
    amp = float(np.exp(ls * np.log(max(w, 1e-9)) + li))
    gamma_sigma = max(GAMMA_SIGMA_FLOOR, gsig) * infl
    a_rel = max(A_REL_SIGMA_FLOOR, float(np.exp(lsig) - 1.0)) * infl
    return BranchPrediction(omega0=w, gamma=gamma, gamma_sigma=gamma_sigma, amplitude_norm=amp,
                            amplitude_rel_sigma=a_rel, in_anchor_range=in_range,
                            n_anchors=int(branch["n_anchors"]), branch_id=str(branch["branch_id"]))


def branch_for(state, *, sample, T, omega0, t_round: float = 5.0,
               branch_id: str = BRANCH_ID) -> BranchPrediction | None:
    """Build the branch for (sample, T-bin) from ``state.dispersion_prior`` and predict at ``omega0``,
    or None if it can't be built. Rebuilt on demand (like ``session_prior.prior_for``) so it always
    reflects the accumulated anchors."""
    t_bin = round(float(T) / t_round) * t_round if t_round else float(T)
    branch = build_branch(state.dispersion_prior, sample=sample, t_bin=t_bin, branch_id=branch_id)
    if branch is None:
        return None
    return predict(branch, omega0)

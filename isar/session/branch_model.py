"""The cross-q branch model (docs/MEASUREMENT_STEERING.md §7, steering Phase 2).

Shares more than ω₀ across a dispersion branch: Γ and the (portable, exposure-normalised) intensity
A, so a fit near the zone centre — where the acoustic peak merges with the elastic line and Γ/A go
unidentifiable — can be softly constrained to the branch and recover ω₀ (the LOW-q rescue).

**Coordinate = ω₀, not q** (owner decision): Γ and A are modelled as functions of ω₀ itself, because
the acoustic intensity law is literally A ∝ 1/ω. The dispersion prior already predicts ω₀(q), so a
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

import numpy as np

from isar.schema.steering import ParamConstraints

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

    def soft_constraints(self, exposure) -> ParamConstraints:
        """Soft :class:`ParamConstraints` for a low-q fit: Γ pinned to ``gamma ± gamma_sigma`` and the
        raw amplitude to the branch intensity scaled to *this scan's* exposure
        (``amplitude_norm × exposure``) with a loose relative σ. Never hard (A absorbs systematics)."""
        soft: dict = {}
        if self.gamma_sigma > 0:
            soft["phonon.Gamma"] = (float(self.gamma), float(self.gamma_sigma))
        if exposure and float(exposure) > 0 and self.amplitude_norm > 0:
            raw_a = float(self.amplitude_norm) * float(exposure)
            soft["phonon.A"] = (raw_a, max(self.amplitude_rel_sigma, A_REL_SIGMA_FLOOR) * raw_a)
        return ParamConstraints(soft=soft)


def _is_clean_anchor(p: dict, sample, t_bin) -> bool:
    """A dispersion-prior point usable as a branch anchor: same sample+T bin, carries Γ and a portable
    intensity, and is well-separated from the elastic line (ω₀ ≳ 2Γ and ≳ SEP·resolution)."""
    if p.get("sample") != sample:
        return False
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


def _linfit(xv, yv) -> tuple[float, float, float]:
    """(slope, intercept, residual σ) of ``y ~ x``; falls back to (0, mean, std) when x is degenerate."""
    xv = np.asarray(xv, dtype=float)
    yv = np.asarray(yv, dtype=float)
    if xv.size < 2 or len(set(np.round(xv, 9))) < 2:
        return 0.0, float(np.mean(yv)), float(np.std(yv))
    slope, intercept = np.polyfit(xv, yv, 1)
    resid = yv - (slope * xv + intercept)
    return float(slope), float(intercept), float(np.std(resid))


def build_branch(points, *, sample, t_bin, branch_id: str = BRANCH_ID) -> dict | None:
    """Fit a branch from the clean anchors in ``points`` (``state.dispersion_prior``), or None if too
    sparse. Γ(ω₀) is a local linear trend; log A(ω₀) is linear in log ω₀ (the 1/ω acoustic law is the
    slope≈−1 fit itself). Returns a plain JSON-able dict (persists in ``CardState.branch_models``)."""
    anchors = [p for p in points if _is_clean_anchor(p, sample, t_bin)]
    if len(anchors) < MIN_ANCHORS:
        return None
    w = np.array([float(p["omega0"]) for p in anchors])
    g = np.array([float(p["gamma"]) for p in anchors])
    a = np.array([float(p["intensity"]["value"]) for p in anchors])
    gs, gi, gsig = _linfit(w, g)
    ls, li, lsig = _linfit(np.log(w), np.log(np.clip(a, 1e-30, None)))
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

    gamma = max(gs * w + gi, GAMMA_FLOOR)
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

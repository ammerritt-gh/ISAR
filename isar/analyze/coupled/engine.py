"""Coupled (inter-spectra) refit core: one spectrum's refit with its neighbours' values frozen.

Expert hand-over for job j1c46f8949fcf, unit S1U2 (planning draft, NOT yet a validated component).
Home: isar/analyze/coupled/engine.py. Specification: the inter-spectra plan's S1U2 algorithm
specification (sections 1-4); this file implements sections 1, 2, 3 ("labelled coordinates") and 4 for one block.

Conventions: energies in meV, absolute from zero energy transfer (the fast pass's omega0).
Penalties P and entry prices B are in deviance units (2 x nats), like D = Baker-Cousins cost.
A density's ln is in nats. Every setting value here is UNCALIBRATED (spec-draft section 6 table).

Seam: CoupledEngine subclasses fast_pass._Engine and overrides only ln_prior, _canonical and _trf;
fast_pass.py itself is untouched (check_base_unchanged).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from scipy.integrate import quad

from isar.analyze import fast_pass, prior

EPS_KNEE = 0.05          # meV, smooth zero-zone knee (uncalibrated numerical setting)


# ---- one physical factor ------------------------------------------------------------------------
@dataclass(frozen=True)
class Factor:
    """One coupling term, held exactly once: residual u = sum_j a_j E_j over its members.

    kind: "dT" (pair), "dq" (anchored q change pair, zero zone w), "curv" (collinear q triplet).
    s: frozen tolerance (meV) including the neighbour-uncertainty broadening (spec 1, frozen per stage).
    C: cap in deviance units. ``d`` > 0 only for the literal outside-division T arm (comparison only).
    """
    factor_id: str
    branch_id: str
    members: tuple            # spectrum keys
    a: tuple                  # coefficients, one per member
    s: float
    C: float
    kind: str
    w: float = 0.0
    d: float = 0.0
    family: str = "welsch"    # "welsch" | "quartic" (comparison family)


def _knee(v: float) -> float:
    """max(0, v) rounded C2 over [0, EPS_KNEE] (spec 1, smooth zero-zone knee)."""
    if v <= 0.0:
        return 0.0
    if v >= EPS_KNEE:
        return v
    x = v / EPS_KNEE
    return EPS_KNEE * x ** 3 * (6.0 - 8.0 * x + 3.0 * x * x)


def potential(f: Factor, u: float) -> float:
    """Welsch P = C (1 - exp(-z^2/2)), z = knee(|u| - w) / s; outside-division arm when f.d > 0.
    family "quartic": the bounded P = C z^4 / (1 + z^4), written in 1/z beyond z = 1 so it never overflows."""
    t = _knee(abs(u) - f.w) if f.w > 0 else abs(u)
    if f.family == "quartic":
        z = t / f.s
        p = f.C / (1.0 + (1.0 / z) ** 4) if z > 1.0 else f.C * z ** 4 / (1.0 + z ** 4)
    else:
        z2 = (t / f.s) ** 2
        p = -f.C * math.expm1(-0.5 * z2)
    return p / f.d if f.d > 0 else p


def mismatch(f: Factor, u: float) -> float:
    """Unlevelled small-z cost U = C z^2 / 2 (flag statistic, spec 6); /d on the literal arm.
    family "quartic": U = C z^4."""
    t = _knee(abs(u) - f.w) if f.w > 0 else abs(u)
    z2 = (t / f.s) ** 2
    U = f.C * z2 * z2 if f.family == "quartic" else 0.5 * f.C * z2
    return U / f.d if f.d > 0 else U


# ---- energy densities ---------------------------------------------------------------------------
@dataclass
class MatchedDensity:
    """p(E) = 1_W g(E) exp(-V(E)/2) / Z on W = [lo, hi]: ONE normaliser of the product of every
    factor incident on this local mode, neighbours frozen at the snapshot. g is the recipe's
    near-zero ramp (EnergyPrior.penalty), kept per Opus point 1 (pending Astra turn 4)."""
    ep: prior.EnergyPrior
    me: str                                   # this spectrum's key
    factors: list                             # [Factor] containing ``me``
    frozen: dict                              # neighbour key -> frozen energy (meV)
    keep_ramp: bool = True
    ln_z: float = field(init=False)
    status: str = field(init=False, default="ok")

    def _u(self, f: Factor, e: float) -> float:
        return sum(a * (e if k == self.me else self.frozen[k]) for k, a in zip(f.members, f.a))

    def V(self, e: float) -> float:
        return sum(potential(f, self._u(f, e)) for f in self.factors)

    def _ln_unnorm(self, e: float) -> float:
        return -(self.ep.penalty(e) if self.keep_ramp else 0.0) - 0.5 * self.V(e)

    def __post_init__(self):
        lo, hi = float(self.ep.lo), float(self.ep.hi)
        pts = []
        for f in self.factors:                # split at each factor's predicted centre and knees
            ai = next(a for k, a in zip(f.members, f.a) if k == self.me)
            c = -sum(a * self.frozen[k] for k, a in zip(f.members, f.a) if k != self.me) / ai
            pts += [c, c - f.w, c + f.w, c - f.w - EPS_KNEE, c + f.w + EPS_KNEE]
        pts += [self.ep.start, self.ep.end]
        pts = sorted({p for p in pts if lo < p < hi})
        grid = np.linspace(lo, hi, 401)
        shift = max(self._ln_unnorm(float(e)) for e in grid)
        total, err = 0.0, 0.0
        for a, b in zip([lo] + pts, pts + [hi]):
            v, e = quad(lambda x: math.exp(self._ln_unnorm(x) - shift), a, b,
                        epsabs=0.0, epsrel=1e-9, limit=200)
            total, err = total + v, err + e
        if not (math.isfinite(total) and total > 0 and err <= 1e-7 * total):
            self.status, self.ln_z = "no_evidence", math.nan   # never a silent uniform fallback
            return
        self.ln_z = shift + math.log(total)

    def ln_pdf(self, e: float) -> float:
        if not (self.ep.lo <= e <= self.ep.hi) or self.status != "ok":
            return -math.inf
        return self._ln_unnorm(e) - self.ln_z


@dataclass(frozen=True)
class Entry:
    """Unmatched-branch entry (spec 2): B(E) = B_mid H(x), x = d_edge / band, H the C2 smoothstep;
    the joint weight q_U p_U(E) = g(E) exp(-(B_edge + B(E))/2) / Z_g, Z_g today's solo normaliser,
    so B_edge = B_mid = 0 is exactly the solo energy density (the zero-price limit)."""
    ep: prior.EnergyPrior
    band: float                               # meV: min(0.2 ell, max(FWHM, 0.5))
    b_edge: float = 2.0                       # deviance units, uncalibrated
    b_mid: float = 12.0                       # deviance units, uncalibrated
    physical_lo: bool = True                  # False: a cap/exclusion edge, no edge relief
    physical_hi: bool = True

    def B(self, e: float) -> float:
        d = [e - self.ep.lo] if self.physical_lo else []
        d += [self.ep.hi - e] if self.physical_hi else []
        if not d:
            return self.b_mid
        x = min(max(min(d) / self.band, 0.0), 1.0)
        return self.b_mid * x ** 3 * (10.0 - 15.0 * x + 6.0 * x * x)

    def ln_weight(self, e: float) -> float:
        """ln(q_U p_U(E)): the discrete entry mass and the energy density together (nats)."""
        lp = self.ep.log_pdf(e)
        return lp - 0.5 * (self.b_edge + self.B(e)) if math.isfinite(lp) else -math.inf


# ---- the engine seam ----------------------------------------------------------------------------
class CoupledEngine(fast_pass._Engine):
    """One spectrum's coupled refit inside a FIXED composition (labelled coordinates, spec 3).

    ``slots``: one entry per mode position of the model, in the model's mode order:
    a MatchedDensity (matched branch) or an Entry (unmatched). ``_Engine.fit`` sorts the START modes
    by omega0 before building the model, so slots must be listed in ascending start energy; after
    that the labels stay with their coordinates. Coordinates keep their labels: no
    re-sorting (``_canonical`` is the identity) and no k!. ``n_exchangeable`` = sizes of groups of
    unmatched modes with identical complete priors; ln(u!) of each is added once (one
    representative basin evaluated). Continuous stage: ``continuous=True`` makes TRF minimise
    J_i = D_i + sum_f P_f + sum_unmatched B + 2 penalty (ramp), each term once.
    """

    def __init__(self, ctx, settings, slots, *, n_exchangeable=(), continuous=False, **kw):
        super().__init__(ctx, settings, **kw)
        self.slots = list(slots)
        self.ln_exch = sum(math.lgamma(n + 1) for n in n_exchangeable)
        self.continuous = continuous

    def ln_prior(self, model, theta) -> float:
        sp, omit = self.sp, model.omit
        if len(model.modes) != len(self.slots):
            raise ValueError("coupled refit: the model's mode count differs from the composition")
        lp = self.ln_exch
        for (a, _g, _c), slot in zip(model.modes, self.slots):
            if a in omit:
                continue                       # a pinned energy carries no free-coordinate density
            lp += slot.ln_pdf(float(theta[a])) if isinstance(slot, MatchedDensity) \
                else slot.ln_weight(float(theta[a]))
        for a, g, c in model.modes:            # non-energy factors exactly as _Engine.ln_prior
            if g not in omit:
                lp += sp.ln_width(theta[g])
            if c not in omit:
                lp += sp.ln_height(theta[c])
        if model.el is not None and model.el not in omit:
            lp += sp.ln_height(theta[model.el])
        if model.bg is not None and model.bg not in omit:
            lp += sp.ln_background(theta[model.bg])
        return lp + sum(p.logpdf(float(theta[i])) for i, p in model.soft_priors)

    def _canonical(self, model, theta):
        return model, theta                    # labelled coordinates: never re-sort (spec 3)

    def penalty_terms(self, model, theta) -> list:
        """[(term id, deviance, sign)] of this block's J_i beyond D_i, each term once; the sign
        (of the factor residual u) makes the TRF residual sign(u) sqrt(P), smooth through u = 0."""
        out = []
        for m, ((a, _g, _c), slot) in enumerate(zip(model.modes, self.slots)):
            e = float(theta[a])
            if isinstance(slot, MatchedDensity):
                out += [(f.factor_id, potential(f, slot._u(f, e)), slot._u(f, e))
                        for f in slot.factors]
                ramp = slot.ep.penalty(e) if slot.keep_ramp else 0.0
            else:
                out.append((f"entry:{m}", slot.B(e), 1.0))
                ramp = slot.ep.penalty(e)
            out.append((f"ramp:{m}", 2.0 * ramp, 1.0))
        return out

    def J(self, model, theta) -> float:
        return self.cost(model, theta) + sum(p for _, p, _s in self.penalty_terms(model, theta))

    def _trf(self, model, theta0, family):
        if not self.continuous:
            return super()._trf(model, theta0, family)
        free = model.free
        lo, hi = model.lo[free], model.hi[free]

        def residual(z):
            t = theta0.copy()
            t[free] = z
            r = fast_pass.poisson_residuals(self.y, self.mean(model, t))
            extra = [math.copysign(math.sqrt(max(p, 0.0)), sg)
                     for _, p, sg in self.penalty_terms(model, t)]
            r = np.concatenate([r, extra])
            if model.soft:
                r = np.concatenate([r, [(t[i] - mu) / sd for i, mu, sd in model.soft]])
            return np.nan_to_num(r, nan=1e100, posinf=1e100, neginf=-1e100)

        return self._least_squares(residual, np.clip(theta0[free], lo, hi), lo, hi, family)

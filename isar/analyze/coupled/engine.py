"""Coupled (inter-spectra) refit core: one spectrum's refit with its neighbours' values frozen.

Expert hand-over for job j1c46f8949fcf, unit S1U2 (planning draft, NOT yet a validated component).
Home: isar/analyze/coupled/engine.py. Specification: the inter-spectra plan's S1U2 algorithm
specification (sections 1-4); this file implements sections 1, 2, 3 ("labelled coordinates") and 4 for one block.

Conventions: energies in meV, absolute from zero energy transfer (the fast pass's omega0).
Penalties P and entry prices B are in deviance units (2 x nats), like D = Baker-Cousins cost.
A density's ln is in nats. Every setting value here is UNCALIBRATED (spec-draft section 6 table).

Seam: CoupledEngine subclasses fast_pass._Engine and overrides ln_prior, _canonical, _trf and _resolve, plus the
hidden-mode pieces (S1U1): a twin-screen guard (_resolve, which keeps a matched slot from being merged away and
otherwise runs the fast pass's screens, the railed-QE rule among them), and a price that integrates over the
intensity split of an overlapping pair (price, split_evidence) and reports an undetermined fit as such.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from functools import cached_property, lru_cache

import numpy as np
from scipy.integrate import quad
from scipy.optimize import minimize
from scipy.special import logsumexp

from isar.analyze import fast_pass, prior
from isar.analyze.coupled.linking import _GL_W, _GL_X, KB_MEV_PER_K, stokes_area_edges

# The split integral of a mode hidden under another (S1U1); algorithm settings, not operator settings.
SPLIT_NODE_SETS = (6, 12, 24, 48)   # Gauss-Legendre node counts over the split f, tried in order
SPLIT_TOL_NATS = 0.1                # required agreement between two successive node sets
SPLIT_BAIL_NATS = 1.0               # sets further apart than this: the grid cannot resolve the integrand
SPLIT_RIDGE = 0.1                   # nats of curvature on the pair's free widths (a flat width is singular)
SPLIT_FD_REL = 1e-2                 # rel_step of the conditional Hessian (the default 1e-4 is below FD noise)
SPLIT_OUTSIDE = 1e7                 # finite stand-in for +inf inside L-BFGS-B only
SPLIT_ESCAPES = 4                   # saddle-escape tries per node
COLUMN_FD_REL = 1e-5                # step of a column difference: COLUMN_FD_REL x max(|value|, FWHM) meV
# prior.laplace_log_evidence's reasons for a fit whose parameters stay undetermined (pinned by a test)
UNDETERMINED_REASONS = ("Hessian not positive definite", "box covariance numerically singular")


# ---- one physical factor ------------------------------------------------------------------------
@dataclass(frozen=True)
class Factor:
    """One coupling term, held exactly once: residual u = sum_j a_j E_j over its members.

    kind: "dT" (pair), "dq" (anchored q change pair), "curv" (collinear q triplet).
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
    d: float = 0.0
    family: str = "welsch"    # "welsch" | "quartic" (comparison family)


def potential(f: Factor, u: float) -> float:
    """Welsch P = C (1 - exp(-z^2/2)), z = |u| / s; outside-division arm when f.d > 0.
    family "quartic": the bounded P = C z^4 / (1 + z^4), written in 1/z beyond z = 1 so it never overflows."""
    t = abs(u)
    if f.family == "quartic":
        z = t / f.s
        p = f.C / (1.0 + (1.0 / z) ** 4) if z > 1.0 else f.C * z ** 4 / (1.0 + z ** 4)
    else:
        z2 = (t / f.s) ** 2
        p = -f.C * math.expm1(-0.5 * z2)
    return p / f.d if f.d > 0 else p


def d_potential(f: Factor, u: float) -> float:
    """dP/du of ``potential``: Welsch C (u / s^2) exp(-u^2/(2 s^2)); quartic 4 C z^3 / (1 + z^4)^2 x sign(u) / s
    (in w = 1/z beyond z = 1: w^5 / (1 + w^4)^2); / d on the literal arm."""
    if f.family == "quartic":
        z = abs(u) / f.s
        if z > 1.0:
            w = 1.0 / z
            dz = 4.0 * f.C * w ** 5 / (1.0 + w ** 4) ** 2
        else:
            dz = 4.0 * f.C * z ** 3 / (1.0 + z ** 4) ** 2
        dp = math.copysign(dz / f.s, u)
    else:
        dp = f.C * (u / f.s ** 2) * math.exp(-0.5 * (u / f.s) ** 2)
    return dp / f.d if f.d > 0 else dp


def mismatch(f: Factor, u: float) -> float:
    """Unlevelled small-z cost U = C z^2 / 2 (flag statistic, spec 6); /d on the literal arm.
    family "quartic": U = C z^4."""
    z2 = (abs(u) / f.s) ** 2
    U = f.C * z2 * z2 if f.family == "quartic" else 0.5 * f.C * z2
    return U / f.d if f.d > 0 else U


# ---- energy densities ---------------------------------------------------------------------------
_GL = np.polynomial.legendre.leggauss(256)   # the width normaliser's rule (nodes, weights) on [-1, 1]: 1e-8 at tau_w 0.1, 1e-14 at the default 0.3
GF_SPAN = 8.0                                 # the G_f table covers ln X in [-GF_SPAN tau_A, GF_SPAN tau_A]
GF_NODES = 8001                               # cubic Hermite in ln X: under 1e-10 relative for tau_A in [0.2, 2] (2001: 1e-8)


def _welsch(C: float, u: float) -> tuple[float, float]:
    """(P(u), P'(u)) of the Welsch potential at cap C; u = -inf (zero intensity) sits at the cap, P' = 0."""
    if not math.isfinite(u):
        return C, 0.0
    return -C * math.expm1(-0.5 * u * u), C * u * math.exp(-0.5 * u * u)


@lru_cache(maxsize=64)
def _gf_table(tau: float, C: float):
    """(v0, step, G_f at the nodes, dG_f/dv at the nodes) on v = ln X: each interval by 8-point Gauss-Legendre of
    e^v exp(-P(v/tau)/2), cumulated from the exact lower tail e^{-C/2} X."""
    v = np.linspace(-GF_SPAN * tau, GF_SPAN * tau, GF_NODES)
    step = float(v[1] - v[0])
    x, w = np.polynomial.legendre.leggauss(8)
    mid = v[:-1, None] + 0.5 * step * (x + 1.0)
    f = np.exp(mid + 0.5 * C * np.expm1(-0.5 * (mid / tau) ** 2))
    base = math.exp(-0.5 * C + float(v[0]))
    G = np.concatenate([[base], base + np.cumsum(0.5 * step * (f @ w))])
    D = np.exp(v + 0.5 * C * np.expm1(-0.5 * (v / tau) ** 2))
    return float(v[0]), step, G, D


def intensity_gf(tau: float, C: float, X: float) -> tuple[float, float]:
    """(G_f(X), G_f'(X)), G_f(X) = int_0^X exp(-P(ln x / tau)/2) dx with P the Welsch potential at cap C: cubic
    Hermite in ln X on the table, the exact linear tails e^{-C/2} X below it and G_f(X0) + e^{-C/2} (X - X0) above.
    G_f' is exact."""
    if not X > 0:
        return 0.0, math.exp(-0.5 * C)
    v = math.log(X)
    d = math.exp(-0.5 * _welsch(C, v / tau)[0])
    v0, step, G, D = _gf_table(tau, C)
    if v <= v0:
        return math.exp(-0.5 * C) * X, d
    i = int((v - v0) / step)
    if i >= GF_NODES - 1:
        return float(G[-1]) + math.exp(-0.5 * C) * (X - math.exp(v0 + (GF_NODES - 1) * step)), d
    s = (v - (v0 + i * step)) / step
    s2, s3 = s * s, s * s * s
    g = ((2 * s3 - 3 * s2 + 1) * G[i] + (s3 - 2 * s2 + s) * step * D[i]
         + (-2 * s3 + 3 * s2) * G[i + 1] + (s3 - s2) * step * D[i + 1])
    return float(g), d


_STOKES_X, _STOKES_W = np.array(_GL_X), np.array(_GL_W)


@lru_cache(maxsize=4096)
def _stokes_area_and_partials(omega0: float, gamma: float, T: float | None) -> tuple[float, float, float]:
    """(U, dU/domega0, dU/dGamma), U = ``linking.unit_stokes_area`` by the same quadrature (its edges, nodes, weights
    and tail substitution) in one numpy pass. The partials integrate the integrand's own partials on the same nodes:
    U runs over E in (0, inf), so no limit moves with omega0 or Gamma and the edges only place nodes."""
    edges, upper = stokes_area_edges(omega0, gamma, T)
    lo, hi = np.array(edges[:-1]), np.array(edges[1:])
    half = 0.5 * (hi - lo)
    u = np.concatenate([0.25 + 0.25 * _STOKES_X, 0.75 + 0.25 * _STOKES_X])      # the tail E = upper/u on (0, 1)
    E = np.concatenate([((0.5 * (lo + hi))[:, None] + half[:, None] * _STOKES_X).ravel(), upper / u])
    w = np.concatenate([(half[:, None] * _STOKES_W).ravel(), np.tile(0.25 * _STOKES_W, 2) * upper / (u * u)])
    if T is None or not math.isfinite(T) or T <= 0:
        y = E                                                                    # every node has E > 0
    else:
        y = E / -np.expm1(-E / (KB_MEV_PER_K * T))
    q = E * E - omega0 * omega0
    D = q * q + 4.0 * gamma * gamma * E * E
    s = (omega0 / math.pi * 4.0 * gamma) * y / D
    U = float(w @ s)
    if not (math.isfinite(U) and U > 0):
        raise ValueError(f"no finite Stokes area for omega0={omega0!r}, Gamma={gamma!r}, T={T!r}")
    dU_de = float(w @ (s * (1.0 / omega0 + 4.0 * omega0 * q / D)))
    dU_dG = float(w @ (s * (1.0 / gamma - 8.0 * gamma * E * E / D)))
    return U, dU_de, dU_dG


@dataclass(frozen=True)
class SlotCoupling:
    """What a matched slot's width and height densities need from the link prediction (S4U3, S4U5).
    w_star: the neighbours' predicted ``matching_width`` (meV, resolution included); fwhm: this spectrum's
    resolution R; tau_w: ``width_tolerance_ln``; C: ``penalty_cap`` (deviance); knee: the width prior's knee k;
    g_lo: the width's lower bound. a_star: the predicted intrinsic intensity in this spectrum's stored scale
    (``Prediction.A``; <= 0: no height coupling, today's term); tau_a: ``intensity_tolerance_ln``; T: this
    spectrum's temperature (K; None: the T -> 0 response), for ``unit_stokes_area``."""
    w_star: float
    fwhm: float
    tau_w: float
    C: float
    knee: float
    g_lo: float
    a_star: float = 0.0
    tau_a: float = 0.0
    T: float | None = None


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
    coupling: SlotCoupling | None = None      # the width density centred on the neighbours' prediction (None: today's terms)
    _lnf: dict = field(init=False, default_factory=dict, repr=False)   # ceiling -> ln F(c), the width normaliser

    def _u(self, f: Factor, e: float) -> float:
        return sum(a * (e if k == self.me else self.frozen[k]) for k, a in zip(f.members, f.a))

    def V(self, e: float) -> float:
        return sum(potential(f, self._u(f, e)) for f in self.factors)

    def _ln_unnorm(self, e: float) -> float:
        return -(self.ep.penalty(e) if self.keep_ramp else 0.0) - 0.5 * self.V(e)

    def __post_init__(self):
        cp = self.coupling
        if cp is not None and not (all(math.isfinite(x) for x in (cp.w_star, cp.fwhm, cp.tau_w, cp.C, cp.knee, cp.g_lo))
                                   and cp.w_star > 0 and cp.fwhm > 0 and cp.tau_w > 0 and cp.C >= 0
                                   and math.isfinite(self.ln_f(max(4.0, 2.0 * cp.g_lo)))
                                   and (not cp.a_star > 0 or self._area_ok())):
            self.status, self.ln_z = "no_evidence", math.nan   # never a silent flat fallback
            return
        lo, hi = float(self.ep.lo), float(self.ep.hi)
        pts = []
        for f in self.factors:                # split at each factor's predicted centre
            ai = next(a for k, a in zip(f.members, f.a) if k == self.me)
            pts += [-sum(a * self.frozen[k] for k, a in zip(f.members, f.a) if k != self.me) / ai]
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

    def d_ln_pdf(self, e: float) -> float:
        """d ln_pdf / d e inside the support: -ramp'(e) - (1/2) sum_f a_me P_f'(u_f)."""
        dv = sum(sum(a for k, a in zip(f.members, f.a) if k == self.me) * d_potential(f, self._u(f, e))
                 for f in self.factors)
        return -(self.ep.d_penalty(e) if self.keep_ramp else 0.0) - 0.5 * dv

    # ---- the width density (S4U3) ------------------------------------------------------------------------------
    # p_w(G | c) = g(u) / ((G + k) F(c)) on [g_lo, c], u = ln(matching_width(G, R) / w*) / tau_w, g = exp(-P(u)/2),
    # P the Welsch potential at cap C, F(c) = int g(u(G)) dG / (G + k): proper, and the base density when C = 0.
    def _g(self, G):
        """(u, ln g) at the widths ``G`` (array or float)."""
        cp = self.coupling
        u = np.log(np.hypot(2.0 * G, cp.fwhm) / cp.w_star) / cp.tau_w
        return u, 0.5 * cp.C * np.expm1(-0.5 * u * u)

    def ln_f(self, c: float) -> float:
        """ln F(c), a 256-node Gauss-Legendre rule in v = ln(G + k), memoised per ceiling."""
        v = self._lnf.get(c)
        if v is None:
            cp = self.coupling
            a, b = math.log(cp.g_lo + cp.knee), math.log(c + cp.knee)
            x, w = _GL
            _u, lg = self._g(np.exp(0.5 * (b - a) * x + 0.5 * (a + b)) - cp.knee)
            v = math.log(0.5 * (b - a) * float(w @ np.exp(lg)))
            if len(self._lnf) > 4096:
                self._lnf.clear()
            self._lnf[c] = v
        return v

    @property
    def has_width(self) -> bool:
        return self.coupling is not None and self.status == "ok"

    def ln_width(self, G: float, c: float) -> float:
        """ln p_w(G | c); -inf outside [g_lo, c]."""
        cp = self.coupling
        if not cp.g_lo <= G <= c:
            return -math.inf
        return float(self._g(G)[1]) - math.log(G + cp.knee) - self.ln_f(c)

    def d_ln_width(self, G: float, c: float) -> float:
        """d ln p_w / dG inside the support."""
        cp = self.coupling
        u = float(self._g(G)[0])
        du = 4.0 * G / ((4.0 * G * G + cp.fwhm ** 2) * cp.tau_w)
        return -0.5 * cp.C * u * math.exp(-0.5 * u * u) * du - 1.0 / (G + cp.knee)

    def d_ln_width_dc(self, G: float, c: float) -> float:
        """d ln p_w / dc at fixed G: only the normaliser moves with the ceiling."""
        cp = self.coupling
        return -math.exp(float(self._g(c)[1]) - math.log(c + cp.knee) - self.ln_f(c))

    # ---- the height density (S4U5) ------------------------------------------------------------------------------
    # The prior is on the intrinsic intensity m = h b, b = (area per unit height) / unit_stokes_area: Welsch from zero
    # about a*, u = ln(h b / a*) / tau_a. Induced on the height h in [0, H]:
    # ln p_h = -P(u)/2 + ln b - ln a* - ln G_f(X), X = H b / a* (+ ln b: the Jacobian of m = h b).
    def _area_ok(self) -> bool:
        cp = self.coupling
        if not (math.isfinite(cp.a_star) and math.isfinite(cp.tau_a) and cp.tau_a > 0
                and (cp.T is None or math.isfinite(cp.T))):
            return False
        G = _gf_table(float(cp.tau_a), float(cp.C))[2]
        return bool(np.all(np.isfinite(G)) and G[0] > 0)

    @property
    def has_area(self) -> bool:
        return self.has_width and self.coupling.a_star > 0

    def _ua(self, h: float, b: float) -> float:
        cp = self.coupling
        return math.log(h * b / cp.a_star) / cp.tau_a if h > 0 else -math.inf

    def ln_height(self, h: float, b: float, H: float) -> float:
        """ln p_h(h | b); -inf outside [0, H]."""
        cp = self.coupling
        if not 0.0 <= h <= H:
            return -math.inf
        P = _welsch(cp.C, self._ua(h, b))[0]
        G = intensity_gf(cp.tau_a, cp.C, H * b / cp.a_star)[0]
        return -0.5 * P + math.log(b) - math.log(cp.a_star) - math.log(G)

    def d_ln_height_dh(self, h: float, b: float, H: float) -> float:
        """d ln p_h / dh inside the support (0 at h = 0: the Welsch factor beats 1/h)."""
        if not h > 0:
            return 0.0
        cp = self.coupling
        return -_welsch(cp.C, self._ua(h, b))[1] / (2.0 * h * cp.tau_a)

    def d_ln_height_db(self, h: float, b: float, H: float) -> float:
        """d ln p_h / db at fixed h: the potential's, the Jacobian's and the normaliser's."""
        cp = self.coupling
        G, dG = intensity_gf(cp.tau_a, cp.C, H * b / cp.a_star)
        return -_welsch(cp.C, self._ua(h, b))[1] / (2.0 * b * cp.tau_a) + 1.0 / b - H * dG / (cp.a_star * G)

    @cached_property
    def centre(self) -> float:
        """Where the density peaks (argmax on a 401-point grid): the energy of a mode too weak to have one."""
        grid = np.linspace(float(self.ep.lo), float(self.ep.hi), 401)
        return float(grid[int(np.argmax([self.ln_pdf(float(e)) for e in grid]))])


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

    def dB(self, e: float) -> float:
        """dB/dE: the smoothstep's derivative 30 x^2 (1 - x)^2 B_mid / band, signed by the nearer edge; 0 outside
        the band and where no edge is physical."""
        d = [(e - self.ep.lo, 1.0)] if self.physical_lo else []
        d += [(self.ep.hi - e, -1.0)] if self.physical_hi else []
        if not d:
            return 0.0
        dist, sign = min(d, key=lambda p: p[0])
        x = dist / self.band
        if not 0.0 < x < 1.0:
            return 0.0
        return sign * self.b_mid * 30.0 * x * x * (1.0 - x) ** 2 / self.band

    def ln_weight(self, e: float) -> float:
        """ln(q_U p_U(E)): the discrete entry mass and the energy density together (nats)."""
        lp = self.ep.log_pdf(e)
        return lp - 0.5 * (self.b_edge + self.B(e)) if math.isfinite(lp) else -math.inf

    def d_ln_weight(self, e: float) -> float:
        """d ln_weight / d E inside the support."""
        return self.ep.d_log_pdf(e) - 0.5 * self.dB(e)


# ---- the split integral -------------------------------------------------------------------------------
@dataclass(frozen=True)
class SplitResult:
    ln_z: float | None
    kind: str          # "ok" | "undetermined" | "not_converged" | "no_evidence" (a node failed for another reason)
    reason: str
    sets: tuple        # ((n, ln I), ...) the node sets used
    nodes: int         # conditional solves done
    theta: object = None   # kind == "ok" only: the representative point, full model coordinates


def split_set(n: int, f_joint: float):
    """One Gauss-Legendre set of n nodes on (0, 1): (f, weights summing to 1, (up chain, down chain)), each chain
    the node indices in the order ``integrate_split`` reads them, outward from ``f_joint``."""
    x, w = np.polynomial.legendre.leggauss(n)
    f, w = (x + 1.0) / 2.0, w / 2.0
    order = np.argsort(f)
    return f, w, ([i for i in order if f[i] >= f_joint], [i for i in order[::-1] if f[i] < f_joint])


def integrate_split(node, f_joint, *, sets=SPLIT_NODE_SETS, tol=SPLIT_TOL_NATS, bail=SPLIT_BAIL_NATS) -> SplitResult:
    """ln of the integral over f in (0, 1) of exp(node(f, near)); node returns ln Z_f, or the reason its f failed:
    a reason starting with UNDETERMINED_REASONS makes the whole integral undetermined (never interpolated across),
    any other reason no_evidence with that reason.
    Gauss-Legendre node sets are tried in order; two successive sets within ``tol`` agree. Sets further apart
    than ``bail`` cannot resolve the integrand (a narrow peak in f: the data pin the split): not converged,
    without trying the larger set. Nodes are solved outward from ``f_joint``, ``near`` the previous node."""
    done, nodes, d = [], 0, math.nan
    for n in sets:
        f, w, chains = split_set(n, f_joint)
        lz = np.empty(n)
        for chain in chains:
            near = None
            for i in chain:
                v = node(float(f[i]), near)
                nodes += 1
                if isinstance(v, str):
                    kind = "undetermined" if v.startswith(UNDETERMINED_REASONS) else "no_evidence"
                    return SplitResult(None, kind, f"{v}: split node f={f[i]:.4f}", tuple(done), nodes)
                lz[i], near = v, float(f[i])
        done.append((n, float(logsumexp(lz + np.log(w)))))
        if len(done) >= 2:
            d = abs(done[-1][1] - done[-2][1])
            if d <= tol:
                return SplitResult(done[-1][1], "ok", "", tuple(done), nodes)
            if d > bail:
                return SplitResult(None, "not_converged", f"node sets differ by {d:.2f} nats", tuple(done), nodes)
    return SplitResult(None, "not_converged", f"{done[-1][0]} nodes still differ by {d:.3f} nats",
                       tuple(done), nodes)


def split_representative(res: SplitResult, problem, solved, lnz_at) -> SplitResult:
    """An ``ok`` integral with its representative point: a solved node (not the TRF start) within the agreement
    tolerance of the best, the nearest to the start's split. ``solved``/``lnz_at``: the nodes read, f -> solution
    and f -> conditional ln Z."""
    if res.kind != "ok":
        return res
    f_joint, theta_of = problem[2], problem[4]
    top = max(lnz_at.values())
    f_star = min((f for f, z in lnz_at.items() if z >= top - SPLIT_TOL_NATS), key=lambda f: abs(f - f_joint))
    return replace(res, theta=theta_of(solved[f_star], f_star))


def _polish(g, lo, hi, x, gv=None):
    """L-BFGS-B of g, scaled by max(|x|, 1); +inf is replaced by a finite stand-in because the first projected
    step can land on the box corner, where scipy then reports convergence at the start. Returns (g(x), x).
    ``gv`` None (finite differences): two passes, the second from the first's result (one fails at the first tie).
    ``gv(x) = (g, dg/dx)``: one pass with that gradient, and a retry from its result, the scaling recomputed, only
    when the pass reports failure or ends on a non-finite value. The finite-difference single pass stops short at a
    weak/strong tie (up to 1e-2 nat, on ftol with a noisy gradient); the gradient pass alone reaches the two-pass
    value there (test_coupled_gradient.py, check 4). A projected-gradient trigger was dropped: L-BFGS-B stops on ftol
    near 1e-4, so it retried at 34 of 36 nodes of the hidden case for the same ln Z to 1e-4."""
    x = np.clip(x, lo, hi)
    opts = {"maxiter": 500, "ftol": 1e-12, "gtol": 1e-6}
    if gv is None:
        for _ in range(2):
            sc = np.maximum(np.abs(x), 1.0)
            r = minimize(lambda u: (lambda v: v if math.isfinite(v) else SPLIT_OUTSIDE)(g(u * sc)), x / sc,
                         method="L-BFGS-B", bounds=list(zip(lo / sc, hi / sc)), options=opts)
            x = np.clip(r.x * sc, lo, hi)
        return g(x), x
    for _ in range(2):
        sc = np.maximum(np.abs(x), 1.0)

        def fun(u):
            v, d = gv(u * sc)
            return (v, d * sc) if math.isfinite(v) else (SPLIT_OUTSIDE, np.zeros(u.size))
        lo_s, hi_s = lo / sc, hi / sc
        r = minimize(fun, x / sc, jac=True, method="L-BFGS-B", bounds=list(zip(lo_s, hi_s)), options=opts)
        x = np.clip(r.x * sc, lo, hi)
        v = g(x)
        if r.success and math.isfinite(v):
            break
    return v, x


def _d_logpdf(p, v: float) -> float:
    """d logpdf / d v of a soft coordinate's ``catalogue.ResolvedPrior`` inside [lo, hi], kind by kind as its
    ``logpdf`` (kept here: the coupled objective is its only differentiating caller)."""
    if p.kind == "gaussian":
        return -(v - p.mu) / p.sigma ** 2
    if p.kind == "lognormal":                    # scipy's lognorm(s=sigma, scale=mu)
        return -1.0 / v - math.log(v / p.mu) / (p.sigma ** 2 * v)
    if p.kind == "softlog":
        return -1.0 / (v + p.mu)
    if p.kind == "energy_ramp":
        return p.energy.d_log_pdf(v)
    if p.kind == "qe_width":
        return p.qe_width.d_log_pdf(v)
    if p.kind == "flat":
        return 0.0
    raise ValueError(f"no derivative for prior kind {p.kind!r}")


# ---- the engine seam ----------------------------------------------------------------------------------
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
    ``gradients`` (default on): the posterior optimiser, each split node's optimiser and every Laplace curvature they
    take run on one joint evaluation of the objective and its gradient (``objective_and_gradient``); off, the base
    engine's finite-difference calls exactly.
    ``lifted`` (one bool per slot, default all False): a lifted slot's width ceiling is ``settings.width_cap`` (the
    old 20 meV) instead of the lone ``min(4, omega0)``; the caller knows which modes a settled link vouches for.
    """

    def __init__(self, ctx, settings, slots, *, n_exchangeable=(), continuous=False, gradients=True,
                 lifted=(), **kw):
        super().__init__(ctx, settings, **kw)
        self.lifted = tuple(bool(x) for x in lifted)
        if self.lifted and len(self.lifted) != len(slots):
            raise ValueError(f"coupled refit: {len(self.lifted)} lifted flags for {len(slots)} slots")
        self._s0 = self.s                      # the guard swaps self.s temporarily; the pair rule reads the original
        self.gradients = gradients             # False: the finite-difference pricing (no joint objective and gradient)
        self.fd_posterior = False              # True: the posterior alone on finite differences (refit's ABNORMAL retry)
        self._split_done = {}                  # model -> {basin index: [pair-canonical points of its split solutions]}
        self.n_split_nodes = 0
        self.slots = list(slots)
        self.ln_exch = sum(math.lgamma(n + 1) for n in n_exchangeable)
        self.continuous = continuous
        if self.seed_qe:                       # the central QE's start width (BL43LXU), as the fast pass's seed picks it
            self._choose_qe_start()

    def lifted_of(self, model, m: int) -> bool:
        """Whether slot ``m`` (the model's mode position) has the lifted width ceiling."""
        return m < len(self.lifted) and self.lifted[m]

    @property
    def n_split_basins(self) -> int:              # one ledger entry per split basin: each counted once
        return sum(len(d) for d in self._split_done.values())

    # ---- a mode hidden under another (S1U1) ---------------------------------------------------------------
    def hidden_pairs(self, model, point) -> list:
        """Pairs (i, j) of modes within ``twin_coincide_fwhm x fwhm`` of each other with at least one slot
        matched and both heights free with equal bounds: the pairs the twin screens must not merge and the
        split integral prices. A matched mode too weak to have an energy sits at its density's centre."""
        if len(model.modes) != len(self.slots):
            return []
        s0, free = self._s0, set(int(x) for x in model.free)
        thr, dist = s0.twin_height_frac * self.sp.height_hi, s0.twin_coincide_fwhm * self.fwhm
        pos = [sl.centre if isinstance(sl, MatchedDensity) and c in free and point[c] <= thr else point[a]
               for (a, _g, c), sl in zip(model.modes, self.slots)]
        return [(i, j) for i in range(len(pos)) for j in range(i + 1, len(pos))
                if abs(pos[i] - pos[j]) < dist
                and (isinstance(self.slots[i], MatchedDensity) or isinstance(self.slots[j], MatchedDensity))
                and model.modes[i][2] in free and model.modes[j][2] in free
                and model.hi[model.modes[i][2]] == model.hi[model.modes[j][2]]]

    def _resolve(self, b, point):
        """The twin screens, except that a matched slot is never merged away for being close to another mode
        (screen 4) or for having no height (screen 1): its neighbours support it. Repeated starts reaching one
        solution still merge (``_place``'s own basin test). fast_pass.py stays untouched: the screens are
        switched off through the settings, restored in ``finally``."""
        model = b.model
        if len(model.modes) != len(self.slots):
            return super()._resolve(b, point)
        free, thr = set(int(x) for x in model.free), self._s0.twin_height_frac * self.sp.height_hi
        zero = any(isinstance(sl, MatchedDensity) and c in free and point[c] <= thr
                   for (_a, _g, c), sl in zip(model.modes, self.slots))
        coin = bool(self.hidden_pairs(model, point))
        if not (zero or coin):
            return super()._resolve(b, point)
        keep = self.s
        # shortcut: the screens are off for every mode of this basin, upgrade if a basin shows both a hidden pair and
        #   a vanished mode; the zero swap also disables screens 3, 3b and 6 (zero elastic, QE, background) for it
        self.s = replace(keep, twin_height_frac=-1.0 if zero else keep.twin_height_frac,
                         twin_coincide_fwhm=0.0 if zero or coin else keep.twin_coincide_fwhm)
        try:
            super()._resolve(b, point)
        finally:
            self.s = keep

    def price(self, b):
        """The base price, except that a basin with one hidden pair is priced by the split integral
        (a coupled posterior optimiser stops ABNORMAL on the flat valley), and a fit whose parameters stay
        undetermined is ``undetermined``, never given a made-up finite evidence."""
        model = b.model
        self._split_done.get(model, {}).pop(b.index, None)   # a reprice (an improving refit) must not meet its own solution
        pairs = self.hidden_pairs(model, b.theta)
        if not pairs:
            super().price(b)
            self._relabel(b)
            return
        if len(pairs) > 1:
            # shortcut: one overlapping pair per basin, upgrade if a real spectrum shows a stack of three
            b.status, b.reason = "undetermined", "more than one overlapping pair: the split integral covers one"
            return
        pair = pairs[0]
        merged = ("merged_hidden", "the same physical solution as an earlier split basin of this model")
        key = self._pair_point(model, b.theta, pair)
        if self._seen_split(model, key):
            b.status, b.reason = merged
            return
        res = self.split_evidence(b, self._anchor(model, b.theta), pair)
        if res.kind == "not_converged":          # the data pin the split: what Laplace is good at
            super().price(b)
            self._relabel(b)
            b.reason = "; ".join(x for x in (b.reason, f"split integral not converged: {res.reason}") if x)
            return
        if res.kind in ("undetermined", "no_evidence"):
            b.status, b.reason = res.kind, res.reason
            return
        rep = res.theta
        rkey = self._pair_point(model, rep, pair)
        if self._seen_split(model, rkey):
            b.status, b.reason = merged
            return
        b.status, b.ln_z, b.theta_map = "ok", res.ln_z, rep
        b.ln_prior = float(self.ln_prior(model, rep))
        b.cov_free, b.boundary_method, b.objective, b.pinned = None, "split", None, ()
        b.hidden_pair = pair                     # read by refit._basin_record
        self._split_done.setdefault(model, {})[b.index] = [key, rkey]

    def _relabel(self, b):
        if b.status == "no_evidence" and b.reason.startswith(UNDETERMINED_REASONS):
            b.status = "undetermined"

    def _anchor(self, model, theta):
        """theta with each vanished matched mode's energy at its density's centre (its own is arbitrary)."""
        t, free = np.array(theta, dtype=float), set(int(x) for x in model.free)
        thr = self._s0.twin_height_frac * self.sp.height_hi
        for (a, _g, c), sl in zip(model.modes, self.slots):
            if isinstance(sl, MatchedDensity) and c in free and t[c] <= thr:
                t[a] = sl.centre
        return t

    def _pair_point(self, model, theta, pair):
        """The pair's identity without its split: both modes at the height-weighted mean energy and width and
        H/2 each, every other mode, the QE and the elastic as they are."""
        (ai, gi, ci), (aj, gj, cj) = model.modes[pair[0]], model.modes[pair[1]]
        hi_, hj_ = theta[ci], theta[cj]
        H = hi_ + hj_
        wi, wj = (hi_ / H, hj_ / H) if H > 1e-12 else (0.5, 0.5)
        t = np.array(theta, dtype=float)
        t[ai] = t[aj] = wi * theta[ai] + wj * theta[aj]
        t[gi] = t[gj] = wi * theta[gi] + wj * theta[gj]
        t[ci] = t[cj] = H / 2.0
        return t

    def _seen_split(self, model, point) -> bool:
        """Whether a split basin of this model already holds this physical solution. ``same_basin`` ignores
        the elastic and background amplitudes, so they are compared here by the same relative tolerance."""
        frac = self.s.basin_height_frac
        amps = [i for i in (model.el, model.bg) if i is not None]
        return any(self.same_basin(model, q, point)
                   and all(abs(q[i] - point[i]) <= frac * max(q[i], point[i]) for i in amps)
                   for pts in self._split_done.get(model, {}).values() for q in pts)

    def split_problem(self, model, theta, pair):
        """The node problem of ``split_evidence``: (objective, xi_joint, f_joint, lo, theta_of). ``objective(f)``
        is (g, gv, h2): the node objective g(xi), its joint value and gradient gv(xi) (None with gradients off) and
        the coordinates' upper bounds at that f. g = cost/2 - sat - ln_prior - Jacobians + ridge, in nats.
        Coordinates: H = h_i + h_j at h_i's place; each free pair width as s in [0, 1] on its mode's own range
        [g_lo, c(omega0)] in the generalised log coordinate u (u = ln(Gamma + knee) for the soft-log width prior,
        which it makes exactly flat, u = Gamma for the flat one): u(Gamma) = u(g_lo) + s R, R = u(c) - u(g_lo),
        Jacobian ln(dGamma/du) + ln R; every other mapped width as the z map's t; the rest as theta.
        The gradient by the chain rule: H f dF/dh_i + (1 - f) dF/dh_j - 1/H; a pair width dF/dGamma (dGamma/du) R,
        less R for the soft-log Jacobian, on its lower bound too (the inward derivative), and its mode's free energy
        dF/dGamma (dGamma/du) s R' - (soft-log s R') - R'/R, R' = u'(c) c'(omega0); every pair width the ridge's
        own term on top; the other mapped widths ``_ZMap.grad_to_z``'s chain; every other coordinate its dF/dtheta."""
        (_ai, _gi, ci), (_aj, _gj, cj) = model.modes[pair[0]], model.modes[pair[1]]
        free = [int(x) for x in model.free]
        coords = [x for x in free if x != cj]                    # ci carries H = h_i + h_j
        pos = {x: n for n, x in enumerate(coords)}
        st = self.sp.settings
        softlog = st.width_kind == "softlog" and not st.legacy_v4
        knee = st.width_knee
        zm = self.zmap(model)
        pw = [(model.modes[m][1], m, model.modes[m][0]) for m in pair if model.modes[m][1] in free]  # (Gamma, slot, omega0)
        widths = [g for g, _m, _a in pw]
        other = prior.GammaMap([(a, g, g_lo, lf) for _m, a, g, lf, g_lo in zm.mapped if g not in widths],
                               self.prior_s)
        zbox = {x: (lo_, hi_) for x, lo_, hi_ in zip(free, zm.lo, zm.hi)}
        lo = np.array([zbox[x][0] for x in coords], dtype=float)
        hi = np.array([zbox[x][1] for x in coords], dtype=float)
        for g in widths:
            lo[pos[g]], hi[pos[g]] = 0.0, 1.0
        mid, span = 0.5 * (lo + hi), np.maximum(hi - lo, 1e-9)
        hp, h_hi = pos[ci], float(model.hi[ci])
        wn = [pos[g] for g in widths]

        def u(G):
            return math.log(G + knee) if softlog else G

        def ceiling(t, g, m, a):
            """(c, R, R') of a pair width at t: its ceiling, the u range and dR/d omega0."""
            if zm.mapped_mode(m):
                c = zm.ceiling(t, m)
                dc = prior.d_width_ceiling(float(t[a]), lifted=self.lifted_of(model, m), settings=self.prior_s)
            else:                                                # legacy_v4: the static box
                c, dc = float(model.hi[g]), 0.0
            return c, u(c) - u(float(model.lo[g])), (dc / (c + knee) if softlog else dc)

        def theta_of(xi, f):
            t = np.array(theta, dtype=float)
            for x, n in pos.items():
                t[x] = xi[n]
            t[ci], t[cj] = f * xi[hp], (1.0 - f) * xi[hp]
            if other.pairs:
                t = other.to_theta(t)                            # energies assigned first: the ceilings read them
            for g, m, a in pw:
                c, R, _dR = ceiling(t, g, m, a)
                v = u(float(model.lo[g])) + xi[pos[g]] * R
                # the clip only absorbs rounding: exp(ln(lo + knee)) - knee can land a few ulps below lo
                t[g] = min(max(math.exp(v) - knee if softlog else v, float(model.lo[g])), c)
            return t

        def jacobian(xi, t):
            jac = math.log(max(xi[hp], 1e-300)) + (other.ln_jac(t) if other.pairs else 0.0)
            for g, m, a in pw:
                _c, R, _dR = ceiling(t, g, m, a)
                jac += math.log(R) + (u(float(model.lo[g])) + xi[pos[g]] * R if softlog else 0.0)
            return jac

        def objective(f):
            h2 = hi.copy()
            h2[hp] = h_hi / max(f, 1.0 - f)                      # both heights stay inside the height prior

            def g(xi):
                xi = np.asarray(xi, dtype=float)
                if np.any(xi < lo - 1e-10) or np.any(xi > h2 + 1e-10):
                    return math.inf
                xi = np.clip(xi, lo, h2)
                t = theta_of(xi, f)
                lp = self.ln_prior(model, t)
                if not math.isfinite(lp):
                    return math.inf
                ridge = sum(0.5 * SPLIT_RIDGE * ((xi[n] - mid[n]) / span[n]) ** 2 for n in wn)
                return 0.5 * self.cost(model, t) - self.sat - lp - jacobian(xi, t) + ridge

            def gv(xi):
                xi = np.asarray(xi, dtype=float)
                if np.any(xi < lo - 1e-10) or np.any(xi > h2 + 1e-10):
                    return math.inf, np.zeros(xi.size)
                xi = np.clip(xi, lo, h2)
                t = theta_of(xi, f)
                v, dF = self._value_and_grad(model, t)
                if dF is None:
                    return math.inf, np.zeros(xi.size)
                ridge = sum(0.5 * SPLIT_RIDGE * ((xi[n] - mid[n]) / span[n]) ** 2 for n in wn)
                full = other.jac(t).T @ dF - other.d_ln_jac(t) if other.pairs else dF
                out = full[coords]
                out[hp] = f * dF[ci] + (1.0 - f) * dF[cj] - (1.0 / xi[hp] if xi[hp] > 1e-300 else 0.0)
                for gw, m, a in pw:
                    n = pos[gw]
                    _c, R, dR = ceiling(t, gw, m, a)
                    s = xi[n]
                    w = math.exp(u(float(model.lo[gw])) + s * R) if softlog else 1.0   # dGamma/du, unclipped
                    out[n] = dF[gw] * w * R - (R if softlog else 0.0) + SPLIT_RIDGE * (s - mid[n]) / span[n] ** 2
                    if a in pos:                                 # a fixed energy has no coordinate
                        out[pos[a]] += dF[gw] * w * s * dR - (s * dR if softlog else 0.0) - dR / R
                return v - jacobian(xi, t) + ridge, out
            return g, (gv if self.gradients else None), h2

        t0 = np.array(theta, dtype=float)
        z0 = dict(zip(free, zm.to_z(t0)))
        x0 = []
        for x in coords:
            if x == ci:
                x0.append(t0[ci] + t0[cj])
            elif x in widths:
                g, m, a = next(p for p in pw if p[0] == x)
                _c, R, _dR = ceiling(t0, g, m, a)
                x0.append((u(max(float(t0[x]), float(model.lo[x]))) - u(float(model.lo[x]))) / R)
            else:
                x0.append(z0[x])
        xi_joint = np.clip(np.array(x0, dtype=float), lo, hi)
        f_joint = t0[ci] / (t0[ci] + t0[cj]) if t0[ci] + t0[cj] > 1e-12 else 0.5
        return objective, xi_joint, f_joint, lo, theta_of

    def split_evidence(self, b, theta, pair, *, sets=SPLIT_NODE_SETS) -> SplitResult:
        """ln of the integral of exp(-g) over the basin, integrating the intensity split f of ``pair``
        (h_i = f H, h_j = (1 - f) H, Jacobian H; the height prior as it is) with every other coordinate at its
        conditional Laplace at each f, Gauss-Legendre in f. g = cost/2 - sat - ln_prior - Jacobians, in nats,
        the normalisation of ``prior.laplace_log_evidence`` so split and Laplace basins sum in one logsumexp.
        The pair's free widths enter as s in [0, 1] linear in u = ln(Gamma + knee) on their mode's own range
        [g_lo, c(omega0)] when their prior is 1/(Gamma + knee) (flat in s), with a small ridge so a likelihood-blind
        width is not numerically singular (``split_problem``).
        With gradients on, each node's optimiser, Laplace curvature and saddle escape run on g's joint gradient."""
        problem = self.split_problem(b.model, theta, pair)
        xi_joint, f_joint = problem[1], problem[2]
        solved, lnz_at = {}, {}                                  # f -> its solution; f -> its conditional ln Z

        def node(f, near):
            # continuation from the previous node (a chain start: the nearest node solved so far), else the joint start
            key = near if near is not None else min(solved, key=lambda k: abs(k - f), default=None)
            out = self._split_node(problem, f, ([solved[key]] if key is not None else []) + [xi_joint])
            if out[0] != "ok":
                return out[1]                                # integrate_split maps the reason to undetermined or no_evidence
            solved[f], lnz_at[f] = out[2], out[1]
            return out[1]

        def counted(f, near):
            self.n_split_nodes += 1
            return node(f, near)

        return split_representative(integrate_split(counted, f_joint, sets=sets), problem, solved, lnz_at)

    def _split_node(self, problem, f, starts):
        """One node of the split integral at f: polish, Laplace and saddle escapes from each start in turn.
        ("ok", ln Z_f, solution) at the first start that gives an evidence, else ("fail", the last reason)."""
        objective, _xi_joint, _f_joint, lo, _theta_of = problem
        g, gv, h2 = objective(f)
        grad = None if gv is None else fast_pass._gradient_of(gv)
        why = "non-finite objective at every start"
        for start in starts:
            v, x = _polish(g, lo, h2, start, gv)
            for _ in range(SPLIT_ESCAPES):
                if not math.isfinite(v):
                    break
                lap = prior.laplace_log_evidence(g, x, lo, h2, rel_step=SPLIT_FD_REL, grad=grad)
                if lap.status != "no_evidence":
                    return "ok", float(lap.ln_z), x
                why = lap.reason
                # a saddle escape: follow the negative direction of the Hessian both ways (needed at weak/strong ties)
                steps = np.minimum(SPLIT_FD_REL * np.maximum(np.abs(x), 1e-2 * (h2 - lo)), (h2 - lo) / 4.0)
                xc = np.clip(x, lo + 2 * steps, h2 - 2 * steps)
                ev, vec = np.linalg.eigh(prior.hessian_fd(g, xc, steps) if grad is None
                                         else prior.hessian_from_grad(grad, xc, steps))
                if not np.all(np.isfinite(ev)):
                    why = "non-finite Hessian (finite differences)"
                    break
                if ev[0] >= 0:
                    break
                n = int(np.argmax(np.abs(vec[:, 0])))
                d = 0.1 * (h2[n] - lo[n]) / abs(vec[n, 0])
                v, x = min((_polish(g, lo, h2, x + s * d * vec[:, 0], gv) for s in (1.0, -1.0)),
                           key=lambda c: c[0])
        return "fail", why

    def _coupled_width(self, model, theta, m: int):
        """(slot, ceiling) when mode ``m``'s width term is its slot's coupled density, else None (today's term)."""
        slot = self.slots[m]
        if not (isinstance(slot, MatchedDensity) and slot.has_width and self._conditional_widths()):
            return None
        c = prior.width_ceiling(float(theta[model.modes[m][0]]), lifted=self.lifted_of(model, m), settings=self.prior_s)
        return (slot, c) if c > slot.coupling.g_lo else None

    def _ln_width(self, model, theta, m: int) -> float:
        cw = self._coupled_width(model, theta, m)
        if cw is None:
            return super()._ln_width(model, theta, m)
        return cw[0].ln_width(float(theta[model.modes[m][1]]), cw[1])

    # ---- the height coupling (S4U5) ------------------------------------------------------------------------------
    def _coupled_height(self, model, m: int):
        """The slot when mode ``m``'s height term is its slot's coupled density, else None (today's term)."""
        slot = self.slots[m]
        return slot if isinstance(slot, MatchedDensity) and slot.has_area else None

    def _intensity_per_height(self, model, theta, m: int, grad: bool = False):
        """b = a / U of mode ``m``: a = up.area_stokes / up.height (the fast pass's area per unit height, no exposure),
        U = ``unit_stokes_area(omega0, Gamma, T)``, so h b is the intrinsic intensity ``matching_intensity`` gives
        for the stored area h a. ``grad``: (b, {coordinate: db/dcoordinate}) for the mode's energy and width, da from
        the same column steps ``_value_and_grad`` takes (cache hits there), dU from ``_stokes_area_and_partials``."""
        a_, g, _c = model.modes[m]
        T = self.slots[m].coupling.T
        peak = model.variant.peaks[m]
        e, G = float(theta[a_]), float(theta[g])
        up = self.units([(e, G)], [peak])[0][1]
        U, dU_de, dU_dG = _stokes_area_and_partials(e, G, T)
        b = up.area_stokes / up.height / U
        if not grad:
            return b
        db = {}
        for pos in (a_, g):
            if pos in model.omit:
                continue
            u, d = self._column_steps(model, theta, pos, m)
            ups = self.units([(u, G), (d, G)] if pos == a_ else [(e, u), (e, d)], [peak, peak])
            da = (ups[0][1].area_stokes / ups[0][1].height - ups[1][1].area_stokes / ups[1][1].height) / (u - d)
            db[pos] = (da - b * (dU_de if pos == a_ else dU_dG)) / U
        return b, db

    def _column_steps(self, model, t, pos: int, m: int):
        """(up, down) of a mode coordinate's column difference: +-COLUMN_FD_REL x max(|value|, FWHM), one-sided inward
        within a step of a bound (a width's upper bound is its mode's ceiling)."""
        v = float(t[pos])
        h = COLUMN_FD_REL * max(abs(v), self.fwhm)
        up, dn = v + h, v - h
        zm = self.zmap(model)
        top = zm.ceiling(t, m) if pos == model.modes[m][1] and zm.mapped_mode(m) else model.hi[pos]
        if up > top:
            up = v
        elif dn < model.lo[pos]:
            dn = v
        return up, dn

    def _ln_height(self, model, theta, m: int) -> float:
        h = float(theta[model.modes[m][2]])
        slot = self._coupled_height(model, m)
        if slot is None:
            return self.sp.ln_height(h)
        return slot.ln_height(h, self._intensity_per_height(model, theta, m), self.sp.height_hi)

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
        for m, (a, g, c) in enumerate(model.modes):   # non-energy factors exactly as _Engine.ln_prior
            if g not in omit:
                lp += self._ln_width(model, theta, m)
            if c not in omit:
                lp += self._ln_height(model, theta, m)
        if model.el is not None and model.el not in omit:
            lp += sp.ln_height(theta[model.el])
        if model.bg is not None and model.bg not in omit:
            lp += sp.ln_background(theta[model.bg])
        if model.qe_h is not None and model.qe_h not in omit:      # the central QE: per-spectrum, never coupled
            lp += sp.ln_height(theta[model.qe_h])
        if model.qe_w is not None and model.qe_w not in omit:
            lp += sp.ln_qe_width(theta[model.qe_w])
        return lp + self._ln_soft(model, theta)

    def d_ln_prior(self, model, theta) -> np.ndarray:
        """d ln_prior / d theta over every coordinate, term by term as ``ln_prior`` (inside the support). The
        coupling enters once, through each matched density's V; the heights' and background's densities and the
        exchangeability count are constant, except a coupled height's, which moves with its mode's energy and width
        through b (``_intensity_per_height``). A width's density depends on its mode's energy through the ceiling
        (recipe /7), so it has an energy partial too; a soft width cut at the ceiling, its normaliser's."""
        sp, omit = self.sp, model.omit
        cond = self._conditional_widths()
        d = np.zeros(len(theta))
        for (a, _g, _c), slot in zip(model.modes, self.slots):
            if a not in omit:
                d[a] += slot.d_ln_pdf(float(theta[a])) if isinstance(slot, MatchedDensity) \
                    else slot.d_ln_weight(float(theta[a]))
        for m, (a, g, _c) in enumerate(model.modes):
            if g in omit:
                continue
            if cond:
                cw = self._coupled_width(model, theta, m)
                if cw is None:
                    dg, da = sp.d_ln_width_at(float(theta[g]), float(theta[a]), lifted=self.lifted_of(model, m))
                else:                              # the ceiling moves with omega0 only below the 4 meV kink
                    dg = cw[0].d_ln_width(float(theta[g]), cw[1])
                    da = cw[0].d_ln_width_dc(float(theta[g]), cw[1]) * prior.d_width_ceiling(
                        float(theta[a]), lifted=self.lifted_of(model, m), settings=self.prior_s)
                d[g] += dg
                d[a] += da
            else:
                d[g] += sp.d_ln_width(float(theta[g]))
        for m, (_a, _g, c) in enumerate(model.modes):
            slot = None if c in omit else self._coupled_height(model, m)
            if slot is None:
                continue
            b, db = self._intensity_per_height(model, theta, m, grad=True)
            h = float(theta[c])
            d[c] += slot.d_ln_height_dh(h, b, sp.height_hi)
            dlb = slot.d_ln_height_db(h, b, sp.height_hi)
            for pos, v in db.items():
                d[pos] += dlb * v
        if model.qe_w is not None and model.qe_w not in omit:
            d[model.qe_w] += sp.d_ln_qe_width(float(theta[model.qe_w]))
        gamma_of = {g: m for m, (_a, g, _c) in enumerate(model.modes)}
        for i, p in model.soft_priors:
            d[i] += _d_logpdf(p, float(theta[i]))
            m = gamma_of.get(i)
            if m is not None and cond and m not in model.delta:
                # the cut normaliser: d ln Z(c)/dc = pdf(c)/Z(c) where the ceiling cuts the soft prior
                a = model.modes[m][0]
                c = self.zmap(model).ceiling(theta, m)
                if c < p.hi:
                    d[a] -= math.exp(p.logpdf_given(c, c)) * prior.d_width_ceiling(
                        float(theta[a]), lifted=self.lifted_of(model, m), settings=self.prior_s)
        return d

    def objective_and_gradient(self, model, theta):
        """``objective``'s f with its gradient from one joint evaluation (``_value_and_grad``): (fg, lo, hi) with
        fg(z) = (f, df/dz), (inf, 0) outside the box, in the z coordinate (the widths as ``t`` in [0, 1]):
        f = F(theta(z)) - ln_jac, its gradient by the chain rule (``_ZMap.grad_to_z``); None with gradients off (the
        finite-difference pricing) or ``fd_posterior`` set."""
        if not self.gradients or self.fd_posterior:
            return None
        zm = self.zmap(model)
        lo, hi = zm.lo, zm.hi
        tol = 1e-10 * np.maximum(np.abs(lo) + np.abs(hi), 1.0)
        template = np.array(theta, dtype=float)

        def fg(z):
            z = np.asarray(z, dtype=float)
            if np.any(z < lo - tol) or np.any(z > hi + tol):
                return math.inf, np.zeros(z.size)
            t = zm.to_theta(np.clip(z, lo, hi), template)
            v, d = self._value_and_grad(model, t)
            return (math.inf, np.zeros(z.size)) if d is None else (v - zm.ln_jac(t), zm.grad_to_z(t, d))

        return fg, lo, hi

    def _value_and_grad(self, model, t):
        """(F, dF/dtheta) at the full vector t, F = cost/2 - sat - ln_prior exactly as ``objective`` computes it
        (the same mean, summed in the same order); (inf, None) where the prior has no support.
        dF/dmu = 1 - y/mu (0 where mu is clipped at FMIN); a height's, the elastic's, the QE's and the background's
        derivative from their columns; a free energy or width from the difference of its mode's complete
        height-normalised convolved column (``units``, peak-height normalisation included) at +-COLUMN_FD_REL x
        max(|value|, FWHM), one-sided inward within a step of a bound, every column in ONE ``units`` call; a free QE
        width the same through ``qe_column``; minus ``d_ln_prior``."""
        free = set(int(i) for i in model.free)
        peaks = model.variant.peaks

        def shifted(pos):                            # the QE width's step: its upper edge is its box's
            v = float(t[pos])
            h = COLUMN_FD_REL * max(abs(v), self.fwhm)
            up, dn = v + h, v - h
            if up > model.hi[pos]:
                up = v
            elif dn < model.lo[pos]:
                dn = v
            return up, dn

        keys = [(t[a], t[g]) for a, g, _c in model.modes]
        kp = list(peaks)
        diffs = []                                   # (position, mode, index of the up column, of the down one, step)
        for m, (a, g, _c) in enumerate(model.modes):
            for pos in (a, g):
                if pos not in free:
                    continue
                up, dn = self._column_steps(model, t, pos, m)   # a width's upper edge: its ceiling (a lifted one: 20)
                for v in (up, dn):
                    keys.append((v, t[g]) if pos == a else (t[a], v))
                    kp.append(peaks[m])
                diffs.append((pos, m, len(keys) - 2, len(keys) - 1, up - dn))
        units = self.units(keys, kp) if keys else []
        lp = self.ln_prior(model, t)                 # after the columns: a coupled height's b reads them from the cache
        if not math.isfinite(lp):
            return math.inf, None
        k = len(model.modes)
        total = np.zeros_like(self.y)
        for (_, _, c), (col, _up) in zip(model.modes, units[:k]):
            total += t[c] * col
        if model.el is not None:
            total += t[model.el] * self.el_col
        qcol = None
        if model.qe_h is not None:
            qcol = self.qe_column(model.qe_width(t))
            total += t[model.qe_h] * qcol
        if model.bg is not None:
            total += t[model.bg]
        mu = total * self.expo
        r = fast_pass.poisson_residuals(self.y, mu)
        F = 0.5 * float(r @ r) - self.sat - lp
        clipped = mu <= fast_pass.FMIN
        w = np.where(clipped, 0.0, 1.0 - self.y / np.where(clipped, 1.0, mu)) * self.expo   # dF/dmu x exposure
        d = np.zeros(len(t))
        for (_a, _g, c), (col, _up) in zip(model.modes, units[:k]):
            d[c] = w @ col
        for pos, m, iu, idn, step in diffs:
            d[pos] = t[model.modes[m][2]] * (w @ (units[iu][0] - units[idn][0])) / step
        if model.el is not None:
            d[model.el] = w @ self.el_col
        if model.qe_h is not None:
            d[model.qe_h] = w @ qcol
            if model.qe_w is not None and model.qe_w in free:
                up, dn = shifted(model.qe_w)
                d[model.qe_w] = t[model.qe_h] * (w @ (self.qe_column(up) - self.qe_column(dn))) / (up - dn)
        if model.bg is not None:
            d[model.bg] = float(np.sum(w))
        return F, d - self.d_ln_prior(model, t)

    def _canonical(self, model, theta):
        return model, theta                    # labelled coordinates: never re-sort (spec 3)

    def penalty_terms(self, model, theta) -> list:
        """[(term id, deviance, sign)] of this block's J_i beyond D_i, each term once; the sign
        (of the factor residual u) makes the TRF residual sign(u) sqrt(P), smooth through u = 0."""
        out = []
        for m, ((a, g, c), slot) in enumerate(zip(model.modes, self.slots)):
            e = float(theta[a])
            if isinstance(slot, MatchedDensity):
                out += [(f.factor_id, potential(f, slot._u(f, e)), slot._u(f, e))
                        for f in slot.factors]
                cw = None if g in model.omit else self._coupled_width(model, theta, m)
                if cw is not None:       # the full width density, normaliser included: V_w = -2 ln p_w - 2 B_w >= 0
                    out.append((f"width:{m}", -2.0 * cw[0].ln_width(float(theta[g]), cw[1])
                                - 2.0 * self._width_floor(model, m, cw[0]), 1.0))
                if c not in model.omit and self._coupled_height(model, m) is not None:
                    # the full height density, normaliser included: V_h = -2 ln p_h - 2 B_h >= 0, B_h = ln H - C/2
                    H = self.sp.height_hi
                    out.append((f"area:{m}", -2.0 * self._ln_height(model, theta, m)
                                - 2.0 * (math.log(H) - 0.5 * slot.coupling.C), 1.0))
                ramp = slot.ep.penalty(e) if slot.keep_ramp else 0.0
            else:
                out.append((f"entry:{m}", slot.B(e), 1.0))
                ramp = slot.ep.penalty(e)
            out.append((f"ramp:{m}", 2.0 * ramp, 1.0))
        return out

    def _width_floor(self, model, m: int, slot) -> float:
        """B_w, an analytic lower bound of -ln p_w over the slot's box, so the width term of J is never negative and
        the TRF residual sqrt(V_w) needs no clamp: P >= 0, G >= g_lo, g >= exp(-C/2) and F(c) >= exp(-C/2) L(c) with
        L(c) = ln((c + k)/(g_lo + k)) nondecreasing in c, taken at the ceiling of the omega0 box's lower edge."""
        cp = slot.coupling
        cmin = prior.width_ceiling(float(model.lo[model.modes[m][0]]), lifted=self.lifted_of(model, m),
                                   settings=self.prior_s)
        L = max(math.log((cmin + cp.knee) / (cp.g_lo + cp.knee)), 1e-12)
        return math.log(cp.g_lo + cp.knee) + math.log(L) - 0.5 * cp.C

    def J(self, model, theta) -> float:
        # the soft-constraint squares are the residuals _trf applies, so acceptance and covariance see its objective
        soft = sum(((theta[i] - mu) / sd) ** 2 for i, mu, sd in model.soft)
        return self.cost(model, theta) + soft + sum(p for _, p, _s in self.penalty_terms(model, theta))

    def _trf(self, model, theta0, family):
        if not self.continuous:
            return super()._trf(model, theta0, family)
        zm = self.zmap(model)                  # z coordinate, as the base _trf: res.x is z
        lo, hi = zm.lo, zm.hi

        def residual(z):
            t = zm.to_theta(z, theta0)
            r = fast_pass.poisson_residuals(self.y, self.mean(model, t))
            extra = [math.copysign(math.sqrt(max(p, 0.0)), sg)
                     for _, p, sg in self.penalty_terms(model, t)]
            r = np.concatenate([r, extra])
            if model.soft:
                r = np.concatenate([r, [(t[i] - mu) / sd for i, mu, sd in model.soft]])
            return np.nan_to_num(r, nan=1e100, posinf=1e100, neginf=-1e100)

        return self._least_squares(residual, np.clip(zm.to_z(theta0), lo, hi), lo, hi, family)

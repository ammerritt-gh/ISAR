"""Coupled (inter-spectra) refit core: one spectrum's refit with its neighbours' values frozen.

Expert hand-over for job j1c46f8949fcf, unit S1U2 (planning draft, NOT yet a validated component).
Home: isar/analyze/coupled/engine.py. Specification: the inter-spectra plan's S1U2 algorithm
specification (sections 1-4); this file implements sections 1, 2, 3 ("labelled coordinates") and 4 for one block.

Conventions: energies in meV, absolute from zero energy transfer (the fast pass's omega0).
Penalties P and entry prices B are in deviance units (2 x nats), like D = Baker-Cousins cost.
A density's ln is in nats. Every setting value here is UNCALIBRATED (spec-draft section 6 table).

Seam: CoupledEngine subclasses fast_pass._Engine and overrides ln_prior, _canonical and _trf, plus the
hidden-mode pieces (S1U1): a twin-screen guard (_resolve), and a price that integrates over the intensity
split of an overlapping pair (price, split_evidence) and reports an undetermined fit as such;
fast_pass.py itself is untouched (check_base_unchanged).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from functools import cached_property

import numpy as np
from scipy.integrate import quad
from scipy.optimize import minimize
from scipy.special import logsumexp

from isar.analyze import fast_pass, prior

# The split integral of a mode hidden under another (S1U1); algorithm settings, not operator settings.
SPLIT_NODE_SETS = (6, 12, 24, 48)   # Gauss-Legendre node counts over the split f, tried in order
SPLIT_TOL_NATS = 0.1                # required agreement between two successive node sets
SPLIT_BAIL_NATS = 1.0               # sets further apart than this: the grid cannot resolve the integrand
SPLIT_RIDGE = 0.1                   # nats of curvature on the pair's free widths (a flat width is singular)
SPLIT_FD_REL = 1e-2                 # rel_step of the conditional Hessian (the default 1e-4 is below FD noise)
SPLIT_OUTSIDE = 1e7                 # finite stand-in for +inf inside L-BFGS-B only
SPLIT_ESCAPES = 4                   # saddle-escape tries per node
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


def mismatch(f: Factor, u: float) -> float:
    """Unlevelled small-z cost U = C z^2 / 2 (flag statistic, spec 6); /d on the literal arm.
    family "quartic": U = C z^4."""
    z2 = (abs(u) / f.s) ** 2
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

    def ln_weight(self, e: float) -> float:
        """ln(q_U p_U(E)): the discrete entry mass and the energy density together (nats)."""
        lp = self.ep.log_pdf(e)
        return lp - 0.5 * (self.b_edge + self.B(e)) if math.isfinite(lp) else -math.inf


# ---- the split integral -------------------------------------------------------------------------------
@dataclass(frozen=True)
class SplitResult:
    ln_z: float | None
    kind: str          # "ok" | "undetermined" | "not_converged" | "no_evidence" (a node failed for another reason)
    reason: str
    sets: tuple        # ((n, ln I), ...) the node sets used
    nodes: int         # conditional solves done
    theta: object = None   # kind == "ok" only: the representative point, full model coordinates


def integrate_split(node, f_joint, *, sets=SPLIT_NODE_SETS, tol=SPLIT_TOL_NATS, bail=SPLIT_BAIL_NATS) -> SplitResult:
    """ln of the integral over f in (0, 1) of exp(node(f, near)); node returns ln Z_f, or the reason its f failed:
    a reason starting with UNDETERMINED_REASONS makes the whole integral undetermined (never interpolated across),
    any other reason no_evidence with that reason.
    Gauss-Legendre node sets are tried in order; two successive sets within ``tol`` agree. Sets further apart
    than ``bail`` cannot resolve the integrand (a narrow peak in f: the data pin the split): not converged,
    without trying the larger set. Nodes are solved outward from ``f_joint``, ``near`` the previous node."""
    done, nodes, d = [], 0, math.nan
    for n in sets:
        x, w = np.polynomial.legendre.leggauss(n)
        f, w = (x + 1.0) / 2.0, w / 2.0                         # on (0, 1); the weights sum to 1
        order = np.argsort(f)
        lz = np.empty(n)
        for chain in ([i for i in order if f[i] >= f_joint], [i for i in order[::-1] if f[i] < f_joint]):
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


def _polish(g, lo, hi, x):
    """Two L-BFGS-B passes of g (one fails at the first tie), scaled by max(|x|, 1); +inf is replaced by a
    finite stand-in because the first projected step can land on the box corner, where scipy then reports
    convergence at the start. Returns (g(x), x)."""
    x = np.clip(x, lo, hi)
    for _ in range(2):
        sc = np.maximum(np.abs(x), 1.0)
        r = minimize(lambda u: (lambda v: v if math.isfinite(v) else SPLIT_OUTSIDE)(g(u * sc)), x / sc,
                     method="L-BFGS-B", bounds=list(zip(lo / sc, hi / sc)),
                     options={"maxiter": 500, "ftol": 1e-12, "gtol": 1e-6})
        x = np.clip(r.x * sc, lo, hi)
    return g(x), x


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
    """

    def __init__(self, ctx, settings, slots, *, n_exchangeable=(), continuous=False, **kw):
        super().__init__(ctx, settings, **kw)
        self._s0 = self.s                      # the guard swaps self.s temporarily; the pair rule reads the original
        self._split_done = {}                  # model -> {basin index: [pair-canonical points of its split solutions]}
        self.n_split_nodes = 0
        self.slots = list(slots)
        self.ln_exch = sum(math.lgamma(n + 1) for n in n_exchangeable)
        self.continuous = continuous
        if self.seed_qe:                       # the central QE's start width (BL43LXU), as the fast pass's seed picks it
            self._choose_qe_start()

    @property
    def n_split_basins(self) -> int:                # one ledger entry per split basin: each counted once
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

    def split_evidence(self, b, theta, pair, *, sets=SPLIT_NODE_SETS) -> SplitResult:
        """ln of the integral of exp(-g) over the basin, integrating the intensity split f of ``pair``
        (h_i = f H, h_j = (1 - f) H, Jacobian H; the height prior as it is) with every other coordinate at its
        conditional Laplace at each f, Gauss-Legendre in f. g = cost/2 - sat - ln_prior - Jacobians, in nats,
        the normalisation of ``prior.laplace_log_evidence`` so split and Laplace basins sum in one logsumexp.
        The pair's free widths enter as u = ln(Gamma + knee) when their prior is 1/(Gamma + knee) (flat in u,
        Jacobian +u), with a small ridge so a likelihood-blind width is not numerically singular."""
        model = b.model
        (ai, gi, ci), (aj, gj, cj) = model.modes[pair[0]], model.modes[pair[1]]
        free = [int(x) for x in model.free]
        coords = [x for x in free if x != cj]                    # ci carries H = h_i + h_j
        pos = {x: n for n, x in enumerate(coords)}
        st = self.sp.settings
        softlog = st.width_kind == "softlog" and not st.legacy_v4
        knee = st.width_knee
        widths = [g for g in (gi, gj) if g in free]
        lo, hi = model.lo[coords].astype(float), model.hi[coords].astype(float)
        if softlog:
            for g in widths:
                lo[pos[g]], hi[pos[g]] = math.log(model.lo[g] + knee), math.log(model.hi[g] + knee)
        mid, span = 0.5 * (lo + hi), np.maximum(hi - lo, 1e-9)
        hp, h_hi = pos[ci], float(model.hi[ci])
        wn = [pos[g] for g in widths]

        def theta_of(xi, f):
            t = np.array(theta, dtype=float)
            for x, n in pos.items():
                t[x] = xi[n]
            t[ci], t[cj] = f * xi[hp], (1.0 - f) * xi[hp]
            if softlog:
                for g in widths:
                    t[g] = max(math.exp(xi[pos[g]]) - knee, float(model.lo[g]))
            return t

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
                jac = math.log(max(xi[hp], 1e-300)) + (sum(xi[n] for n in wn) if softlog else 0.0)
                return 0.5 * self.cost(model, t) - self.sat - lp - jac + ridge
            return g, h2

        t0 = np.array(theta, dtype=float)
        xi_joint = np.clip(np.array([t0[ci] + t0[cj] if x == ci
                                     else math.log(max(t0[x], 0.0) + knee) if softlog and x in widths else t0[x]
                                     for x in coords]), lo, hi)
        f_joint = t0[ci] / (t0[ci] + t0[cj]) if t0[ci] + t0[cj] > 1e-12 else 0.5
        solved, lnz_at = {}, {}                                  # f -> its solution; f -> its conditional ln Z

        def node(f, near):
            g, h2 = objective(f)
            # continuation from the previous node (a chain start: the nearest node solved so far), else the joint start
            key = near if near is not None else min(solved, key=lambda k: abs(k - f), default=None)
            why = "non-finite objective at every start"
            for start in ([solved[key]] if key is not None else []) + [xi_joint]:
                v, x = _polish(g, lo, h2, start)
                for _ in range(SPLIT_ESCAPES):
                    if not math.isfinite(v):
                        break
                    lap = prior.laplace_log_evidence(g, x, lo, h2, rel_step=SPLIT_FD_REL)
                    if lap.status != "no_evidence":
                        solved[f], lnz_at[f] = x, float(lap.ln_z)
                        return float(lap.ln_z)
                    why = lap.reason
                    # a saddle escape: follow the negative direction of the Hessian both ways (needed at weak/strong ties)
                    steps = np.minimum(SPLIT_FD_REL * np.maximum(np.abs(x), 1e-2 * (h2 - lo)), (h2 - lo) / 4.0)
                    ev, vec = np.linalg.eigh(prior.hessian_fd(g, np.clip(x, lo + 2 * steps, h2 - 2 * steps), steps))
                    if not np.all(np.isfinite(ev)):
                        why = "non-finite Hessian (finite differences)"
                        break
                    if ev[0] >= 0:
                        break
                    n = int(np.argmax(np.abs(vec[:, 0])))
                    d = 0.1 * (h2[n] - lo[n]) / abs(vec[n, 0])
                    v, x = min((_polish(g, lo, h2, x + s * d * vec[:, 0]) for s in (1.0, -1.0)),
                               key=lambda c: c[0])
            return why                                       # integrate_split maps the reason to undetermined or no_evidence

        def counted(f, near):
            self.n_split_nodes += 1
            return node(f, near)

        res = integrate_split(counted, f_joint, sets=sets)
        if res.kind != "ok":
            return res
        # a solved node (not the TRF start) within the agreement tolerance of the best, the nearest to the start's split
        top = max(lnz_at.values())
        f_star = min((f for f, z in lnz_at.items() if z >= top - SPLIT_TOL_NATS), key=lambda f: abs(f - f_joint))
        return replace(res, theta=theta_of(solved[f_star], f_star))

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
        if model.qe_h is not None and model.qe_h not in omit:      # the central QE: per-spectrum, never coupled
            lp += sp.ln_height(theta[model.qe_h])
        if model.qe_w is not None and model.qe_w not in omit:
            lp += sp.ln_qe_width(theta[model.qe_w])
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
        # the soft-constraint squares are the residuals _trf applies, so acceptance and covariance see its objective
        soft = sum(((theta[i] - mu) / sd) ** 2 for i, mu, sd in model.soft)
        return self.cost(model, theta) + soft + sum(p for _, p, _s in self.penalty_terms(model, theta))

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

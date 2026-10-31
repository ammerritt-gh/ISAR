"""The coupled refit (C-refit): one spectrum's two-stage refit against a frozen neighbour snapshot.

Stage 1 (``open_search``, ``price``; ``assign`` drives the search inline, ``search.drive``) compares counts
and branch associations by evidence under one normalised neighbour density per matched mode (the
hand-over ``engine``), with the unmatched entry price kept inside the odds and the loss price of each neighbour branch a candidate
does not name (the entry price's mirror, charged once per spectrum outside the normalised densities). Stage 2 (``refit(stage=
"continuous")``) refines the continuous parameters under the one shared objective J with the stage's
frozen factor definitions, then reports the acceptance status (spec 5), conditional error bars, a
release fit, the flag score F = R + max U, every flag reason, one sentence why and a drawable overlay.
The decision rules are ``rules`` (installed verbatim); thresholds always come from ``InterSettings``. The
decision is count first, then the labelling within the count, then the model (the width mask and the
variant), and a model change alone is a discrete change. The coupled pass has no mode ceiling: every engine
built here raises the fast-pass settings' ceiling to the count it fits.

``Snapshot.context`` is the spectrum's own prepared fast-pass context: a ``ScanContext``, or an object
carrying it as ``ctx`` with optional ``settings`` (its ``FastPassSettings``: mode ceiling and prior
recipe) and ``structures`` (the catalogue whitelist). The fast pass is never rerun here:
``engine.CoupledEngine`` subclasses its engine and inherits its twin screens. Counts stay raw Poisson (Baker-Cousins, sigma 0 at zero
counts, no floor) and the anti-Stokes amplitude stays derived by detailed balance: both live in the base
engine's model, which this module never changes. No I/O, no pool, no Qt.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Iterable, Mapping
from dataclasses import asdict, astuple, replace
from typing import TYPE_CHECKING, Literal

import numpy as np
from scipy.integrate import quad
from scipy.special import logsumexp

from isar.analyze import fast_pass, loop, prior
from isar.analyze.components import (QE_WEIGHT_CONVENTION, CentralQE, ConstantBackground, Elastic, PhononDHO,
                                     QuasiElastic)
from isar.analyze.coupled import engine as cc
from isar.analyze.coupled import rules
from isar.analyze.coupled.identity import _carried_ids, new_branch_id
from isar.analyze.coupled.settings import InterSettings
from isar.analyze.coupled.types import (SEVERITY_OF, Assignment, Dormant, Held, HeldMode, Priced, PricingJob,
                                        RefitResult, Snapshot)
from isar.analyze.model_manifest import MANIFEST_SCHEMA

if TYPE_CHECKING:
    from isar.analyze.coupled.engine import Factor
    from isar.analyze.coupled.search import Search
    from isar.session.inter_spectra.graph import Link

# Numerical defaults of the specification (uncalibrated; spec 5): an "unchanged" refit moves the predicted
# raw mean by at most MEAN_REL_TOL (denominator max(1, mu_old)) and every free width, height and
# background by at most PARAM_REL_TOL (scale max(|old|, 1)).
MEAN_REL_TOL = 1e-4
PARAM_REL_TOL = 1e-3
# The fast pass's record shape (C-fastpass-record): a refit's ``fit`` keeps it so every reader of a solo
# record reads a refit too; its ``source`` says which it is.
RECORD_SCHEMA = "isar.fast-pass/1"
# Flag codes that never set ``flagged`` (S1U2 blueprint, "Flags and severities"): the four notes, and the
# neighbour-supported split, a repair the review list must not be flooded with (design, Flags).
NOTE_CODES = ("neighbour_precision_unavailable", "incomplete_comparison", "release_fit_failed",
              "errors_unavailable", "neighbour_supported_split")
_PRICED = ("ok", "ok_boundary")
_FACTOR_KIND = {"T-change": "dT", "q-change": "dq", "curvature": "curv"}


# ---- the labelled engine ------------------------------------------------------------------------------
class _Labelled(cc.CoupledEngine):
    """``CoupledEngine`` whose ``fit`` keeps a start's modes in slot order (labelled coordinates, spec 3).

    ``_Engine.fit`` sorts a start by energy before building its model; a neighbour-placed start or a
    twin refit of a crossed basin would then hand one branch's density to another mode."""

    def fit(self, variant, modes, family, linear=None):
        if variant is None:
            return None
        model = self.model(variant, tuple(i for i, m in enumerate(modes) if m[3]))
        if model is None:
            return None
        theta0 = self._start(model, modes, linear)
        res = self._trf(model, theta0, family)
        if res is None:
            return None
        theta = theta0.copy()
        theta[model.free] = res.x
        cost = self.cost(model, theta)
        if not math.isfinite(cost):
            return None
        return self._place(model, theta, cost, family, int(res.nfev), str(res.message)), cost

    def price(self, b):
        """The coupled price, except that a posterior optimiser that stops ABNORMAL is retried once with a 3-point
        gradient (the 2-point one cannot finish on some resolution-limited models). The reason is reset first, so a
        stale 'ABNORMAL' never survives on a priced basin; a basin that still cannot converge stays no_evidence.
        The retry's posterior is the finite-difference one whatever ``gradients`` says: ``objective_and_gradient``
        returns None for that call, so the base engine passes no ``jac`` of its own (a split integral keeps its
        gradient, so a not-converged split reruns to the same nodes)."""
        super().price(b)
        if b.status != "no_evidence" or "ABNORMAL" not in b.reason:
            return
        b.status, b.reason = "pending", ""
        orig = fast_pass.minimize
        # shortcut: a module-level swap of fast_pass.minimize, because fast_pass.py gains no gradient option here;
        # upgrade by passing the option through _Engine.price if the fast pass ever wants it too (that would change
        # fast-pass results beyond change 8, so it is not done here). Safe: pool workers are separate processes
        # and price is single threaded.
        fast_pass.minimize = lambda *a, **k: orig(*a, jac="3-point", **k)
        self.fd_posterior = True
        try:
            super().price(b)
        finally:
            fast_pass.minimize, self.fd_posterior = orig, False

    def _ln_exch(self, model) -> float:
        """ln of the k! of each group of exchangeable unmatched modes (spec 3): the base engine's groups
        (equal priors, pinned energies out), split by the delta mask; a matched slot is never one."""
        pos = {a: m for m, (a, _g, _c) in enumerate(model.modes)}
        total = 0.0
        for grp in model.groups:
            masks = [pos[a] in model.delta for a in grp if isinstance(self.slots[pos[a]], cc.Entry)]
            total += sum(math.lgamma(masks.count(v) + 1) for v in (False, True))
        return total

    def ln_prior(self, model, theta) -> float:
        return super().ln_prior(model, theta) + self._ln_exch(model)

    def seed_hidden(self, k, starts):
        """A constructed hidden-mode start (D6): the overlapping pair of the neighbour-placed start replaced by
        one merged mode, fitted at count k-1 in each variant, then expanded back into the pair at an even-ish
        split. The TRF drifts a very weak mode away in about one draw in twelve, so the hidden candidate would
        otherwise depend on luck; one cheap TRF per variant covers it. The seed is placed, not fitted: the split
        integral prices it. A ``None`` height of the start is filled per variant by the engine's own NNLS start."""
        start = starts[-1][1]
        matched = [isinstance(sl, cc.MatchedDensity) for sl in self.slots]
        pos = [sl.centre if m else start[n][0] for n, (sl, m) in enumerate(zip(self.slots, matched))]
        reach = self._s0.twin_coincide_fwhm * self.fwhm
        pairs = [(i, j) for i in range(k) for j in range(i + 1, k)
                 if abs(pos[i] - pos[j]) < reach and (matched[i] or matched[j])]
        if not pairs:
            return
        i, j = min(pairs, key=lambda p: abs(pos[p[0]] - pos[p[1]]))
        for v in self.variants.get(k, ()):
            low = self.variant(k - 1, v.elastic, v.background, v.qe)
            if low is None:
                continue
            placed = self._heights(v, start)
            if placed is None:
                continue
            h = placed[i][2] + placed[j][2]
            big = i if placed[i][2] >= placed[j][2] else j
            rest = [m for n, m in enumerate(placed) if n not in (i, j)]
            got = self.fit(low, rest + [(placed[big][0], placed[big][1], h, False)], "hidden_low")
            if got is None:
                continue
            lb = got[0]
            lmodes, lin = self.modes_of(lb.model, lb.theta), self.linear_of(lb.model, lb.theta)
            # the merged mode is the fitted mode nearest its start: the TRF can swap the labels, so not the last one
            m = min(range(len(lmodes)), key=lambda n: abs(lmodes[n][0] - placed[big][0]))
            e, g, hm, _d = lmodes[m]
            f0 = min(0.8, max(0.2, placed[i][2] / h)) if h > 0 else 0.5
            others = iter(lmodes[:m] + lmodes[m + 1:])
            out = [(e, g, f0 * hm, False) if n == i else (e, g, (1.0 - f0) * hm, False) if n == j
                   else next(others) for n in range(k)]
            model = self.model(v, ())
            if model is None:
                continue
            theta = self._start(model, out, lin)
            cost = self.cost(model, theta)
            if math.isfinite(cost):
                self._place(model, theta, cost, "hidden", 0, "seed")

    def _heights(self, v, modes):
        """``modes`` (slot order) with each ``None`` height solved by the engine's own start in variant v (Poisson
        NNLS, energies and widths held, the modes in energy order), or as given when every height is set."""
        if all(m[2] is not None for m in modes):
            return list(modes)
        model = self.model(v, ())
        if model is None:
            return None
        order = sorted(range(len(modes)), key=lambda n: modes[n][0])
        theta = self._start(model, [modes[n] for n in order], None)
        out = list(modes)
        for pos, n in enumerate(order):
            if out[n][2] is None:
                out[n] = (out[n][0], out[n][1], float(theta[model.modes[pos][2]]), out[n][3])
        return out


def _parts(context):
    """(ScanContext, FastPassSettings, structure whitelist) of a ``Snapshot.context``."""
    ctx = getattr(context, "ctx", context)
    return (ctx, getattr(context, "settings", None) or fast_pass.FastPassSettings(),
            getattr(context, "structures", None))


def _prefix(i: int) -> str:
    return loop._mode_prefix(i + 1)


def _finite(v):
    """JSON-safe: numpy scalars as Python ones, a non-finite number as None."""
    if isinstance(v, dict):
        return {str(k): _finite(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_finite(x) for x in v]
    if isinstance(v, (bool, np.bool_)):
        return bool(v)
    if isinstance(v, (int, np.integer)):
        return int(v)
    if isinstance(v, (float, np.floating)):
        return float(v) if math.isfinite(v) else None
    return v


# ---- held fits ------------------------------------------------------------------------------------------
def _selected_model(record: dict) -> dict | None:
    sel = record.get("selected_model_id")
    return next((m for m in record.get("models") or () if m.get("model_id") == sel), None)


def held_from_fit(record: dict, *, unit: str, fit: str, inputs: str | None, context, T: float | None,
                  branch_ids: tuple[str, ...] | None = None, dormant: tuple[Dormant, ...] = ()) -> Held:
    """A stored fit (fast-pass record shape) as a neighbour or incoming state reads it.

    The guard of each energy (spec 1): for the fast solo its own covariance sigma; for a refit the
    release sigma stored with it, or its maximum with the conditional sigma. ``None`` (source
    "unavailable") without a release sigma, since a conditional sigma alone is precision borrowed from
    the neighbours: a factor that uses it falls back to ``ell/sqrt(12)`` and notes
    ``neighbour_precision_unavailable``. Branch ids: ``branch_ids``, else those stored on the peaks, else
    ``fast:<unit>#<i>`` for the fast solo (``<fit>#<i>`` for a refit). A mode is pinned only by an
    explicit user energy pin (its energy is not a free coordinate of the stored model)."""
    ctx, fs, _structures = _parts(context)
    sp = prior.resolve_prior_for(ctx, fs.prior or prior.PriorSettings())
    peaks = sorted(record.get("peaks") or (), key=lambda p: float(p["omega0"]))
    sel = _selected_model(record)
    free = set(sel.get("free") or ()) if sel is not None else None
    if branch_ids is not None and len(branch_ids) != len(peaks):
        raise ValueError(f"{unit}: {len(branch_ids)} branch ids for {len(peaks)} modes")
    modes = []
    for i, p in enumerate(peaks):
        if branch_ids is not None:
            branch = branch_ids[i]
        elif p.get("branch"):
            branch = str(p["branch"])
        else:
            branch = new_branch_id(f"fast:{unit}" if fit == "fast" else fit, i)
        cond, rel = p.get("sigma_omega0"), p.get("sigma_release")
        if fit == "fast":
            guard, source = cond, "solo"
        elif cond is not None and rel is not None:
            guard, source = max(cond, rel), "max"
        else:
            guard, source = rel, "release"
        if guard is None or not math.isfinite(guard) or guard <= 0:
            guard, source = None, "unavailable"
        status = p.get("mode_status") or ("determined_by_own_data" if guard is not None else "poorly_determined")
        pinned = free is not None and f"{_prefix(i)}.omega0" not in free
        modes.append(HeldMode(branch=branch, omega0=float(p["omega0"]), gamma=float(p["Gamma"]),
                              height=float(p["height"]), guard=None if guard is None else float(guard),
                              guard_source=source, status=status, regime=str(p.get("regime") or "underdamped"),
                              pinned=pinned))
    return Held(unit=unit, fit=fit, inputs=inputs, window=(float(sp.e_lo), float(sp.e_hi)), modes=tuple(modes),
                dormant=tuple(dormant), T=T)


# ---- the frozen problem of one snapshot -----------------------------------------------------------------
class _Spec:
    """Everything stage 1 derives from a snapshot: the support, the entry price, the assignable branch pool, one
    factor definition per (term, branch), the loss price's expected branches and the content key of a price."""

    def __init__(self, snap: Snapshot):
        self.snap = snap
        self.ctx, self.fs, self.structures = _parts(snap.context)
        self.sp = prior.resolve_prior_for(self.ctx, self.fs.prior or prior.PriorSettings())
        self.ep = self.sp.energy
        self.lo, self.hi = float(self.sp.e_lo), float(self.sp.e_hi)
        self.ell = self.hi - self.lo
        self.fwhm = prior.resolution_fwhm(self.ctx)
        self.band = min(0.2 * self.ell, max(self.fwhm, 0.5))
        # Convention recorded (spec 2): the support edges are the observation edges; an upper prior cap that
        # truncates the observed window is not one (no edge relief there, distance to the physical edge).
        self.physical_hi = self.hi >= float(np.max(self.ctx.x_used))
        self.entry = cc.Entry(self.ep, self.band, b_edge=snap.settings.birth_edge, b_mid=snap.settings.birth_mid,
                              physical_hi=self.physical_hi)
        self.own = snap.own
        self.n0 = len(self.own.modes)
        self.held_all = {h.unit: h for ti in snap.terms for h in ti.held}
        self.notes: list[dict] = []
        self.factors, self.frozen = self._build_factors()
        dormant = {d.branch for h in self.held_all.values() for d in h.dormant}
        dormant |= {d.branch for d in self.own.dormant}
        self.expected = self._expected()
        # the assignable pool (D9): every branch with a coupling term, every branch a neighbour predicts in the
        # window and every dormant one, one name per canonical branch, spelled by its first raw id with a factor
        self.names: dict[str, str] = {}
        for b in sorted(self.factors):
            self.names.setdefault(self.canon(b), b)
        for c in sorted({self.canon(b) for b in dormant} | set(self.expected)):
            self.names.setdefault(c, c)
        self.pool = tuple(sorted(self.names.values()))
        # every alias's factors go under the pool name, so a slot naming the branch carries every neighbour's term (D9)
        grouped: dict[str, list] = {}
        for b, fs_ in self.factors.items():
            grouped.setdefault(self.names[self.canon(b)], []).extend(fs_)
        self.factors = grouped
        self.pred = {b: [self._centre(f) for f in fs_] for b, fs_ in self.factors.items()}
        dormant_c = {self.canon(b) for b in dormant}
        self.returning = frozenset(n for c, n in self.names.items() if c in dormant_c and n not in self.factors)
        inc, used = [], set()
        for md in self.own.modes:                  # a second mode naming one canonical branch is new
            b = self.names.get(self.canon(md.branch))
            inc.append(b if b not in used else None)
            used.add(b)
        self.incoming = tuple(inc)

    # -- the loss price: the mirror of the entry price (a branch that disappears pays what one that appears pays)
    def canon(self, b: str) -> str:
        return self.snap.aliases.get(b, b)

    def _expected(self) -> dict[str, tuple[float, float]]:
        """canonical branch -> (E_exp, L): every branch a neighbour carries (dormant ones never), with E_exp the
        mean over the neighbours of their own prediction when it lies in the window (the centres of the factors
        it belongs to, else its own energy; one vote per neighbour, never per factor) and L the entry price
        ``b_edge + B(E_exp)`` of the same Entry, so births and deaths mirror. A branch no neighbour predicts
        inside the window is not eligible. The branches without a coupling term are eligible too, and assignable
        (D9): a candidate naming one does not pay for it."""
        votes: dict[str, list[float]] = {}
        for u, h in self.held_all.items():
            own: dict[str, list[float]] = {}
            for m in h.modes:
                own.setdefault(self.canon(m.branch), []).append(m.omega0)
            for B, energies in own.items():
                centres = [self._centre(f) for b_, fs_ in self.factors.items() if self.canon(b_) == B
                           for f in fs_ if u in f.members]
                p = float(np.mean(centres if centres else energies))
                if self.lo <= p <= self.hi:
                    votes.setdefault(B, []).append(p)
        out = {}
        for B, ps in votes.items():
            e = float(np.mean(ps))
            out[B] = (e, self.entry.b_edge + self.entry.B(e))
        return out

    def loss_terms(self, slots) -> tuple[tuple[str, float, float], ...]:
        """The eligible branches a candidate does not name, ((branch, E_exp, L), ...) sorted by branch; an
        unmatched slot (None) names nothing. Depends on the snapshot and the slot multiset only."""
        named = {self.canon(s) for s in slots if s is not None}
        return tuple((b, e, L) for b, (e, L) in sorted(self.expected.items()) if b not in named)

    # -- factors (spec 1)
    def _guard(self, held: Held, branch: str) -> float:
        md = next((m for m in held.modes if self.canon(m.branch) == self.canon(branch)), None)
        if md is None:
            return 0.0                    # a branch this end does not carry yet: no recorded guard
        if md.guard is not None:
            return md.guard
        g = (held.window[1] - held.window[0]) / math.sqrt(12.0)
        self.notes.append({"code": "neighbour_precision_unavailable", "ids": [held.unit],
                           "values": {"branch": branch, "fallback_meV": g}})
        return g

    def _build_factors(self):
        snap, s, me = self.snap, self.snap.settings, self.snap.unit
        anchored = snap.batch_kind == "anchored"
        factors: dict[str, list] = {}
        frozen: dict[str, dict[str, float]] = {}
        for ti in snap.terms:
            term, held = ti.term, {h.unit: h for h in ti.held}
            members = tuple(term.members)
            if me not in members or set(members) - {me} != set(held):
                raise ValueError(f"{me}: term {term.kind} {members} does not hold this spectrum and its neighbours")
            common = set.intersection(*({self.canon(m.branch) for m in h.modes} for h in held.values()))
            for c in sorted(common):              # aliases are one branch: each energy is read by canonical name
                b = next(m.branch for u in members if u in held for m in held[u].modes if self.canon(m.branch) == c)
                energy = {u: next(m.omega0 for m in h.modes if self.canon(m.branch) == c) for u, h in held.items()}
                guards = {u: self._guard(h, b) for u, h in held.items()}
                # anchored: the held end's guard only; simultaneous: both ends (symmetric, spec 1)
                guards[me] = 0.0 if anchored else self._guard(self.own, b)
                d, family = 0.0, s.shape_family
                if term.kind == "T-change":
                    a = (1.0, -1.0)
                    base, d = s.t_base(term.spacings[0])
                    spread = sum(guards[u] ** 2 for u in members)
                elif term.kind == "q-change":
                    a, family = (1.0, -1.0), "welsch"
                    base = s.q_tolerance(term.metric, term.spacings[0], f"{me}: q-change {members}")
                    spread = sum(guards[u] ** 2 for u in members)
                elif term.kind == "curvature":
                    t = float(term.t_frac)
                    a, base = (-(1.0 - t), 1.0, -t), s.q_curvature_scale_meV
                    left, _centre, right = members      # the centre's prediction, designated once
                    spread = ((1.0 - t) * guards[left] + t * guards[right]) ** 2
                else:
                    raise ValueError(f"unknown term kind {term.kind!r}")
                # named by the canonical branch: every endpoint of the link builds the same id (D9)
                f = cc.Factor(f"{term.kind}:{'|'.join(members)}:{c}", c, members, a,
                              s=s.widened(base, spread, d), C=s.penalty_cap, kind=_FACTOR_KIND[term.kind],
                              d=d, family=family)
                # keyed by a neighbour's spelling: names() seeds the pool name from it
                factors.setdefault(b, []).append(f)
                frozen.setdefault(c, {}).update(energy)
        return factors, frozen

    def _centre(self, f) -> float:
        me = self.snap.unit
        ai = next(a for k, a in zip(f.members, f.a) if k == me)
        return -sum(a * self.frozen[f.branch_id][k] for k, a in zip(f.members, f.a) if k != me) / ai

    def u(self, f, e: float) -> float:
        return sum(a * (e if k == self.snap.unit else self.frozen[f.branch_id][k]) for k, a in zip(f.members, f.a))

    def d_edge(self, e: float) -> float:
        return min(e - self.lo, self.hi - e) if self.physical_hi else e - self.lo

    def inside(self, b: str) -> bool:
        return any(self.lo <= c <= self.hi for c in self.pred.get(b, ()))

    def cost(self, e: float, b: str | None) -> float:
        """Proposal cost (deviance) of a mode at ``e`` taking branch ``b`` (None: staying unmatched)."""
        if b is None:
            return self.entry.b_edge + self.entry.B(e)
        return sum(cc.potential(f, self.u(f, e)) for f in self.factors.get(b, ()))

    def _record_modes(self, k: int):
        """The modes of the incoming record's best saved model at count k, ascending, or None."""
        models = [m for m in self.snap.own_record.get("models") or () if m.get("n_peaks") == k
                  and (m.get("map_params") or m.get("params"))]
        if not models:
            return None
        best = max(models, key=lambda m: (m.get("probability") or 0.0, m.get("ln_z") or -math.inf))
        p = best.get("map_params") or best.get("params")
        try:
            return sorted((float(p[f"{_prefix(i)}.omega0"]), float(p[f"{_prefix(i)}.Gamma"]),
                           float(p[f"{_prefix(i)}.h"])) for i in range(k))
        except KeyError:
            return None

    def content_key(self) -> str:
        """Everything a price depends on apart from the job (D21; a job key hashes it with the count, slots and
        starts): the spectrum, its fit and inputs, every held fit, mode value and guard of its terms, the aliases
        and the full settings. Never the snapshot id, so an identical snapshot in a later round reuses the price;
        any moved value re-prices."""
        return hashlib.sha256(json.dumps(self._payload(), sort_keys=True, default=repr).encode("utf-8")).hexdigest()

    def _payload(self) -> dict:
        snap = self.snap

        def held(h):
            return [h.unit, h.fit, h.inputs, list(h.window), h.T,
                    [[m.branch, m.omega0, m.gamma, m.height, m.guard, m.pinned] for m in h.modes],
                    [[d.branch, d.last_seen_fit, d.last_seen_unit] for d in h.dormant]]
        # every field of each term (a new Term field is hashed without another edit) and the merged aliases:
        # the loss price names branches canonically, so a price cached before a merge must not be reused after it
        return {"unit": snap.unit, "own": held(snap.own), "batch_kind": snap.batch_kind,
                "settings": snap.settings.to_values(), "aliases": sorted(snap.aliases.items()),
                "terms": [[astuple(ti.term), [held(h) for h in ti.held]] for ti in snap.terms]}

    def origins(self, slots) -> tuple[str, ...]:
        return tuple("new_branch" if b is None else "returning" if b in self.returning else "matched"
                     for b in slots)

    def incident(self, slots) -> tuple:
        return tuple(f for b in slots if b is not None for f in self.factors.get(b, ()))

    @property
    def inc_delta(self) -> tuple:
        """The incoming fit's width mask (slot positions, ascending), as ``_incoming_state`` reads it; () when its
        selected model has another count. Pricing does not depend on it, so ``content_key`` does not hash it."""
        sel = _selected_model(self.snap.own_record)
        return () if sel is None or sel.get("n_peaks") != self.n0 else tuple(sorted(sel.get("delta_modes") or ()))

    @property
    def inc_variant(self) -> tuple | None:
        """The incoming fit's (elastic, background, qe) as bools (D16); None when its selected model has another
        count, so the variant is not decided."""
        sel = _selected_model(self.snap.own_record)
        if sel is None or sel.get("n_peaks") != self.n0:
            return None
        return bool(sel.get("elastic")), bool(sel.get("background")), bool(sel.get("qe", False))

    def skipped(self) -> Assignment:
        """The incoming fit as an Assignment with no search (a test helper: production always searches)."""
        return Assignment(
            unit=self.snap.unit, snapshot_id=self.snap.stage_id, count=self.n0, slots=self.incoming,
            origins=self.origins(self.incoming), factors=self.incident(self.incoming), discrete="skipped",
            triggers=(), candidates=(),
            coverage={"counts_priced": [], "counts_unresolved": {}, "stopped": {}, "full_search": False,
                      "enumeration_truncated": False, "discrete_skipped": "no_proposed_change"},
            search_limited=True, changed=False, reasons=())

    # -- the slot densities of one composition
    def held_value(self, unit: str, branch: str) -> float:
        h = self.held_all.get(unit)
        md = None if h is None else next((m for m in h.modes if self.canon(m.branch) == self.canon(branch)), None)
        if md is None:
            raise KeyError(f"{unit} carries no branch {branch} in this snapshot")
        return md.omega0

    def slot_objects(self, slots, factors=None):
        """One MatchedDensity per matched slot (``factors``: the frozen definitions to use, by branch;
        default this snapshot's), one Entry per unmatched slot; neighbour values from this snapshot."""
        out = []
        for b in slots:
            if b is None:
                out.append(self.entry)
                continue
            fs_ = list(self.factors.get(b, []) if factors is None else factors.get(b, []))
            frozen = {k: self.held_value(k, b) for f in fs_ for k in f.members if k != self.snap.unit}
            out.append(cc.MatchedDensity(self.ep, self.snap.unit, fs_, frozen))
        return out


# ---- stage 1 ---------------------------------------------------------------------------------------------
def stage_factors(snapshot: Snapshot) -> dict[str, Factor]:
    """Every factor definition of the snapshot by id (spec 1), from its held values and frozen guards. The
    runner rebuilds a stage's factors from its snapshots under the reconciled branch ids, so both endpoints of
    one link carry one definition."""
    return {f.factor_id: f for fs in _Spec(snapshot).factors.values() for f in fs}


def _free_width_at_floor(b) -> bool:
    """A free width pegged at the floor is a resolution-limited mode, priced as its own delta model with its own
    weight, never as a free width. A split basin's hidden pair is exempt (its weak-mode width is likelihood-blind:
    not read); any other free width at the floor still counts."""
    hidden = set(b.hidden_pair) if getattr(b, "boundary_method", "") == "split" else set()
    free = {int(i) for i in b.model.free}
    return any(g in free and b.theta_map[g] <= fast_pass.GAMMA_FLOOR
               + loop.DEGENERATE_LIMIT_TOL * max(fast_pass.GAMMA_FLOOR, 1.0)
               for i, (_a, g, _c) in enumerate(b.model.modes) if i not in hidden)


def _failure_reason(basins, *, floored: bool) -> str:
    """Why no basin of a candidate is priced: the terminal failure of the twin chain, not its head."""
    for b in basins:
        t = b.terminal()
        if t.status == "no_evidence":
            return f"{t.model_id}: {t.reason}"
    if floored:
        return "free width at the floor and its delta alternative did not fit"
    return next((b.reason for b in basins if b.reason), "no start converged")


def _ceiling(fs, k: int):
    """The fast-pass settings with a mode ceiling of at least k: the coupled pass has no ceiling of its own."""
    return fs if fs.mode_ceiling >= k else replace(fs, mode_ceiling=k)


def price(snapshot: Snapshot, job: PricingJob, *, gradients: bool = True) -> Priced:
    """One labelling at one count from the job's starts: every start in every admissible variant (the base
    engine's TRF, twin resolution and delta alternatives, plus the constructed hidden start of ``seed_hidden``),
    each distinct basin priced by Laplace under the coupled prior, or for a basin with a hidden pair by the
    integral over the split (engine.CoupledEngine.price); ln Z sums the basins' masses with their variant/delta
    weights, then adds the labelling's composition weight (``rules.ln_w_label``) and, for a pinned unmatched
    energy, its entry charge outside the integral (spec 2). Every basin term also carries the candidate's loss
    price (``_Spec.loss_terms``), once. The engine's ceiling is raised to the job's count. ``gradients``: the
    posterior and split-node optimisers and their Laplace curvature run on the joint objective and gradient
    (``CoupledEngine.objective_and_gradient``); False prices with finite differences, as before that existed."""
    t0 = time.perf_counter()
    k, slots = job.count, tuple(job.slots)
    if len(slots) != k:
        raise ValueError(f"job {job.key}: {len(slots)} slots for count {k}")
    if not job.starts:
        raise ValueError(f"job {job.key}: no starts (a job is built by the search, with its starts)")
    if job.depth not in ("full", "screen"):
        raise ValueError(f"job {job.key}: depth {job.depth!r} is neither 'full' nor 'screen'")
    spec = _Spec(snapshot)
    loss = spec.loss_terms(slots)           # recorded on every return, even a failed one
    objs = spec.slot_objects(slots)
    bad = [b for b, o in zip(slots, objs) if isinstance(o, cc.MatchedDensity) and o.status != "ok"]
    if bad:
        return Priced(job.key, k, slots, None, "no_evidence", None,
                      f"the neighbour density of {bad[0]} has no verified normaliser", 0, time.perf_counter() - t0,
                      loss=loss, depth=job.depth)
    u = sum(b is None for b in slots)
    eng = _Labelled(spec.ctx, _ceiling(spec.fs, k), objs, structures=spec.structures, screen=job.depth == "screen",
                    gradients=gradients)
    if not eng.variants.get(k):
        return Priced(job.key, k, slots, None, "no_structure", None,
                      f"the card's structures hold no model with {k} modes", 0, time.perf_counter() - t0,
                      loss=loss, depth=job.depth)
    for v in eng.variants[k]:
        for st in job.starts:
            eng.fit(v, [(e, g, h, False) for e, g, h in st.modes], st.family)
    # S1's constructed hidden start, from the neighbours' energies when the job carries them
    hint = next((st for st in job.starts if st.family == "neighbour"), job.starts[0])
    eng.seed_hidden(k, [(hint.family, [(e, g, h, False) for e, g, h in hint.modes])])
    while True:
        pending = [b for b in eng.basins() if b.count == k and b.status == "pending"]
        if not pending:
            break
        for b in pending:
            if b.status == "pending":
                eng.price(b)
    basins = [b for b in eng.basins() if b.count == k]
    scored = [b for b in basins if b.status in _PRICED and b.ln_z is not None]
    priced = [b for b in scored if not _free_width_at_floor(b)]
    if not priced:
        twin = bool(basins) and all(b.status == "degenerate_twin" for b in basins)
        und = next((b for b in basins if b.status == "undetermined"), None)
        reason = und.reason if und is not None else _failure_reason(basins, floored=bool(scored))
        status = "degenerate_twin" if twin else "undetermined" if und is not None else "no_evidence"
        return Priced(job.key, k, slots, None, status, None, reason, eng.n_fits, time.perf_counter() - t0,
                      loss=loss, depth=job.depth)

    def charge(b):
        fixed = set(b.model.fixed_pos)
        return sum(rules.pinned_entry_charge(spec.entry, float(b.theta_map[a]))
                   for (a, _g, _c), sl in zip(b.model.modes, slots) if sl is None and a in fixed)
    # the loss price is one constant of the job, inside every basin term: logsumexp carries it exactly once, outside
    # every normalised density, and before any later comparison of this candidate with another count
    loss_nats = -0.5 * sum(L for _b, _e, L in loss)
    terms = [b.ln_model_prior + b.ln_z + charge(b) + loss_nats for b in priced]
    best = priced[int(np.argmax(terms))]
    ln_z = float(logsumexp(terms)) + rules.ln_w_label(k, k - u, len(spec.pool))
    v = best.model.variant
    mode = (v.elastic, v.background, v.qe, tuple(best.model.delta),
            tuple(float(v) for v in best.theta_map))
    records = tuple(_basin_record(b, slots, t) for b, t in zip(priced, terms))
    # a split basin's term with its evidence cut to the first node set (its screen); None where there is no split
    screen_terms = tuple(None if getattr(b, "split_sets", None) is None else t - b.ln_z + b.split_sets[0][1]
                         for b, t in zip(priced, terms))
    peak = _residual_peak(eng, best, [m[1] for m in records[int(np.argmax(terms))][2]])
    return Priced(job.key, k, slots, ln_z, best.status, mode, None, eng.n_fits, time.perf_counter() - t0,
                  records, loss, peak, depth=job.depth, screen_terms=screen_terms)


def _residual_peak(eng, b, energies) -> tuple[float, float]:
    """(omega0, height) of a step's residual add from basin b by the fast pass's ladder rule (``_additions``): the
    largest positive standardised residual above MIN_ADD_ENERGY, at most the window top, at least
    LADDER_EXCLUDE_FWHM x FWHM from every recorded mode energy; (0.8 x top, 1.0) when there is none."""
    r = (eng.y - eng.mean(b.model, b.theta_map)) / eng.expo
    top, reach = eng.sp.e_hi, fast_pass.LADDER_EXCLUDE_FWHM * eng.fwhm
    pick = next((i for i in np.argsort(-r) if fast_pass.MIN_ADD_ENERGY < eng.E[i] <= top
                 and all(abs(eng.E[i] - e) > reach for e in energies)), None)
    return (0.8 * top, 1.0) if pick is None else (float(eng.E[pick]), max(float(r[pick]), 1.0))


def _basin_record(b, slots, term: float) -> tuple:
    """(term, model label, ((slot, omega0, Gamma, h, delta, sigma_omega0) per mode in slot order), (QE height,
    width), mode, slot_mode) of one priced basin at its posterior mode (``mode`` as ``Priced.mode`` but in energy
    order, ``slot_mode`` the same tuple in slot order, its mask by slot position); the search's ledger files it
    under the labelling of its fitted energies. ``sigma_omega0`` is the Laplace covariance's energy sd (None when
    the energy is not free or there is no covariance). A split basin (a mode hidden under
    another, priced by the integral over the intensity split) is labelled ``|split`` and records its pair at the
    pair's mean energy and width with H/2 each: the split is integrated, not fitted, so the two slot orders of
    one hidden basin are one record. (The representative's weak-mode width is likelihood-blind: not read; it is a
    placed value, never a fitted one, and its sigma is None.)"""
    m, th = b.model, b.theta_map
    free = [int(i) for i in m.free]

    def sigma(a):
        if b.cov_free is None or a not in free:
            return None
        var = float(b.cov_free[free.index(a), free.index(a)])
        return math.sqrt(var) if var > 0 else None
    modes = [(sl, float(th[a]), float(th[g]), float(th[c]), i in m.delta, sigma(a))
             for i, ((a, g, c), sl) in enumerate(zip(m.modes, slots))]
    split = getattr(b, "boundary_method", "") == "split"
    if split:
        i, j = b.hidden_pair
        e, g = (modes[i][1] + modes[j][1]) / 2.0, (modes[i][2] + modes[j][2]) / 2.0
        h = (modes[i][3] + modes[j][3]) / 2.0
        for n in (i, j):
            modes[n] = (modes[n][0], e, g, h, modes[n][4], None)
    modes = tuple(modes)
    qe = tuple(None if i is None else float(th[i]) for i in (m.qe_h, m.qe_w))
    # the basin's model and parameters in energy order (the order the ledger files it under): a delta mask and
    # each mode's values follow their mode when a swapped slot order is rewritten to the sorted one
    perm = sorted(range(len(modes)), key=lambda i: (modes[i][1], str(modes[i][0])))
    theta = th.copy()
    for j, i in enumerate(perm):
        for new, old in zip(m.modes[j], m.modes[i]):
            theta[new] = th[old]
    v = m.variant
    mode = (v.elastic, v.background, v.qe, tuple(j for j, i in enumerate(perm) if i in m.delta),
            tuple(float(x) for x in theta))
    slot_mode = (v.elastic, v.background, v.qe, tuple(sorted(m.delta)), tuple(float(x) for x in th))
    return float(term), m.variant.label + ("|split" if split else ""), modes, qe, mode, slot_mode


def _same_basin(spec: _Spec, x: tuple, y: tuple) -> bool:
    """Two basin records (canonical order) are one physical basin by the base engine's own closeness rule."""
    s, w = spec.fs, spec.fwhm
    if x[1] != y[1] or [m[0] for m in x[2]] != [m[0] for m in y[2]]:
        return False
    for (_b, e1, g1, h1, d1), (_c, e2, g2, h2, d2) in ((m[:5], n[:5]) for m, n in zip(x[2], y[2])):
        if d1 != d2 or abs(e1 - e2) >= s.basin_merge_fwhm * w or abs(g1 - g2) > s.basin_gamma_frac * max(w, g1, g2) \
                or abs(h1 - h2) > s.basin_height_frac * max(h1, h2):
            return False
    (qh1, qw1), (qh2, qw2) = x[3], y[3]
    if qw1 is not None and qw2 is not None and abs(qw1 - qw2) > s.basin_gamma_frac * max(w, qw1, qw2):
        return False
    return qh1 is None or qh2 is None or abs(qh1 - qh2) <= s.basin_height_frac * max(qh1, qh2)


def _loss_record(spec: _Spec, slots) -> dict:
    """What a candidate paid for the branches it does not name (already inside its ln_z)."""
    loss = spec.loss_terms(slots)
    return {"loss": [[b, e, L] for b, e, L in loss], "loss_nats": -0.5 * sum(L for _b, _e, L in loss)}


def assign(snapshot: Snapshot, *, cached: Mapping[str, Priced] = {}) -> tuple[Assignment, tuple[Priced, ...]]:
    """The search driven inline (``search.drive``), each job priced here unless ``cached`` holds its key; returns the
    decision and every price the search was given, in the order asked."""
    from isar.analyze.coupled.search import drive
    return drive(open_search(snapshot), lambda j: cached.get(j.key) or price(snapshot, j))


def open_search(snapshot: Snapshot) -> Search:
    """The core's search entry point: the runner calls it by name, and the import is lazy (search imports types only)."""
    from isar.analyze.coupled.search import Search as _Search
    return _Search(snapshot)


# ---- stage 2 ---------------------------------------------------------------------------------------------
def _incoming_state(eng, spec: _Spec):
    """(model, theta) of the incoming fit's selected model at its stored point, or None."""
    sel = _selected_model(spec.snap.own_record)
    if sel is None or sel.get("n_peaks") != spec.n0:
        return None
    variant = eng.variant(spec.n0, sel.get("elastic"), sel.get("background"), sel.get("qe", False))
    model = None if variant is None else eng.model(variant, tuple(sel.get("delta_modes") or ()))
    p = sel.get("map_params") or sel.get("params") or {}
    if model is None or any(p.get(n) is None for n in model.names):
        return None
    return model, np.array([float(p[n]) for n in model.names])


def _with(theta, free, z):
    t = theta.copy()
    t[free] = z
    return t


def _variant_key(v) -> tuple:
    return (v.elastic, v.background, v.qe)


def _materially_moved(eng, model, theta, old) -> bool:
    if old is None or old[0] is not model:
        return True
    mu_old, mu = eng.mean(model, old[1]), eng.mean(model, theta)
    if np.max(np.abs(mu - mu_old) / np.maximum(1.0, mu_old), initial=0.0) > MEAN_REL_TOL:
        return True
    energies = {a for a, _g, _c in model.modes} | {model.qe_w}   # the QE width is a flat nuisance: the mean test covers it
    return any(abs(theta[i] - old[1][i]) / max(abs(old[1][i]), 1.0) > PARAM_REL_TOL
               for i in model.free if i not in energies)


def _cov(f, model, theta):
    """Free-coordinate covariance of ``f`` (= objective/2) at theta by the fast pass's Hessian method; None
    when not positive definite. The Laplace log evidence of this objective is discarded (spec 6)."""
    free = model.free
    if free.size == 0:
        return None
    return prior.laplace_log_evidence(lambda z: f(_with(theta, free, z)), theta[free], model.lo[free],
                                      model.hi[free]).cov


def _sigmas(model, cov):
    """{position: sd} of the free coordinates, and corr(omega0, Gamma) per mode."""
    if cov is None:
        return {}, {}
    sd = {int(i): math.sqrt(v) for i, v in zip(model.free, np.diag(cov)) if v > 0}
    pos = {int(i): j for j, i in enumerate(model.free)}
    corr = {m: float(cov[pos[a], pos[g]] / (sd[a] * sd[g]))
            for m, (a, g, _c) in enumerate(model.modes) if a in sd and g in sd}
    return sd, corr


def _regime(fs, fwhm, e, g, corr) -> str:
    """The fast pass's Regime rule (``_classify``) on one mode's values."""
    if e <= fs.near_elastic_fwhm * fwhm:
        return "near_elastic"
    if g >= e or (corr is not None and abs(corr) >= fs.overdamped_correlation):
        return "overdamped"
    return "underdamped"


def _manifest(model, theta) -> dict | None:
    """``isar.model_manifest/2`` of the refit, the shape predictive_artifact.model_manifest_of writes."""
    kinds = {PhononDHO: "phonon_dho", Elastic: "elastic", QuasiElastic: "quasielastic",
             ConstantBackground: "constant_background", CentralQE: "central_qe"}
    components = []
    for c in model.hyp.build().components:
        kind = kinds.get(type(c))
        if kind is None:
            return None
        item = {"type": kind, "prefix": str(c.prefix)}
        if isinstance(c, PhononDHO):
            item["peak"] = str(c.peak)
        if isinstance(c, CentralQE) and c.width is not None:
            item["width"] = float(c.width)         # a pinned width is part of the model
        components.append(item)
    return {"schema": MANIFEST_SCHEMA, "components": components,
            "parameters": {n: float(v) for n, v in zip(model.names, theta)}}


def _reason(code, text, ids=(), **values) -> dict:
    return {"code": code, "severity": SEVERITY_OF[code], "text": text, "ids": list(ids), "values": _finite(values)}


def _q_u(spec: _Spec) -> float:
    """q_U = exp(-B_edge/2) Z_U / Z_g (spec 2): the unmatched entry mass, a pure number."""
    edges = sorted({spec.lo, spec.hi} | {p for p in (spec.lo + spec.band, spec.hi - spec.band, spec.ep.start,
                                                      spec.ep.end) if spec.lo < p < spec.hi})
    z = sum(quad(lambda e: math.exp(spec.ep.log_pdf(e) - 0.5 * spec.entry.B(e)), a, b, limit=200)[0]
            for a, b in zip(edges, edges[1:]))
    return math.exp(-0.5 * spec.entry.b_edge) * z


def _failed(snapshot, a, reason, why, timing, priced, hits, notes) -> RefitResult:
    return RefitResult(
        unit=snapshot.unit, snapshot_id=snapshot.snapshot_id, status="failed", reason=reason, assignment=a,
        fit=None, modes=(), D=None, J=None, P={}, U={}, unary=0.0, energies={}, R=None, R_signed=None, F=None,
        delta_fast={"count": None, "omega0": {}}, flag={"score": None, "flagged": False, "reasons": notes},
        why_changed=why, overlay=None, overlay_unavailable="the refit failed: no fitted curve",
        covariance=None, errors="unavailable", normalisers={}, starts=0, timing=timing, priced=tuple(priced),
        cache_hits=hits)


def refit(snapshot: Snapshot, *, stage: Literal["continuous", "both"], assignment: Assignment | None = None,
          cached: Mapping[str, Priced] = {}, workers: int = 1) -> RefitResult:
    """One spectrum's refit. ``stage="both"``: ``assign`` then the continuous stage on one snapshot.
    ``stage="continuous"``: ``assignment`` must be this spectrum's, decided on ``snapshot.stage_id``; the
    neighbour values come from the snapshot and every factor definition from ``assignment.factors``;
    counts are never reopened. ``workers`` is the caller's budget: the core opens no pool."""
    timing = {"assign_s": 0.0, "likelihood_s": 0.0, "continuous_s": 0.0, "uncertainty_s": 0.0, "release_s": 0.0}
    priced: tuple[Priced, ...] = ()
    hits = 0
    if stage == "both":
        t0 = time.perf_counter()
        assignment, priced = assign(snapshot, cached=cached)
        hits = sum(p.key in cached for p in priced)
        timing["assign_s"] = time.perf_counter() - t0
        timing["likelihood_s"] = sum(p.seconds for p in priced if p.key not in cached)
    elif stage != "continuous":
        raise ValueError(f"unknown refit stage {stage!r}")
    if assignment is None:
        raise ValueError("the continuous stage needs the stage's Assignment")
    if assignment.unit != snapshot.unit or assignment.snapshot_id != snapshot.stage_id:
        raise ValueError(f"the assignment of {assignment.unit} on {assignment.snapshot_id} does not govern "
                         f"{snapshot.unit} on {snapshot.stage_id}")
    return _continuous(snapshot, assignment, priced, hits, timing)


def _continuous(snapshot, a, priced, hits, timing) -> RefitResult:
    t0 = time.perf_counter()
    spec = _Spec(snapshot)
    k, slots = a.count, tuple(a.slots)
    by_branch: dict[str, list] = {}
    dropped = []
    for f in a.factors:
        try:
            for m in f.members:
                if m != snapshot.unit:
                    spec.held_value(m, f.branch_id)
        except KeyError as exc:
            dropped.append(str(exc))
            continue
        by_branch.setdefault(spec.names[spec.canon(f.branch_id)], []).append(f)
    notes = [_reason(n["code"], "a neighbour's precision is unavailable; its guard is the window width / sqrt(12)",
                     n["ids"], **n["values"]) for n in spec.notes]
    objs = spec.slot_objects(slots, by_branch)
    eng = _Labelled(spec.ctx, _ceiling(spec.fs, k), objs, continuous=True, structures=spec.structures)
    old = _incoming_state(eng, spec) if k == spec.n0 else None

    # starts: the incoming state, the priced posterior mode (the decision's, ``a.selected_model``) and one
    # neighbour-placed start, all on the selected delta mask (the decision's, else the incoming fit's): a delta or a
    # free width is chosen by the evidence comparison, never by the continuous objective, which a free width always
    # lowers. The runner passes no prices, so the Assignment alone carries the choice.
    starts = [] if old is None else [old]
    selected = tuple(old[0].delta) if old is not None else ()
    base = [(m.omega0, m.gamma, m.height) for m in spec.own.modes] if k == spec.n0 else spec._record_modes(k)
    sel_variant = None
    if a.selected_model is not None:
        el, bg, qe, delta, theta = a.selected_model
        variant = eng.variant(k, el, bg, qe)
        model = None if variant is None else eng.model(variant, delta)
        if model is not None:
            selected, sel_variant = tuple(delta), variant
            theta = np.array(theta, dtype=float)
            starts = [s for s in starts if tuple(s[0].delta) == selected
                      and _variant_key(s[0].variant) == _variant_key(variant)] + [(model, theta)]
            base = [m[:3] for m in eng.modes_of(model, theta)]
    variants = [old[0].variant] if old is not None else list(eng.variants.get(k, ()))
    if sel_variant is not None:
        variants = [sel_variant]         # the decision's variant only: a QE it priced away is not restored
    if base is not None and len(base) == k:
        # matched slots at their branch's expected energy, the others where the mode is
        placed = [(spec.expected[spec.canon(b)][0] if b is not None and spec.canon(b) in spec.expected else e, g, h,
                   False) for (e, g, h), b in zip(base, slots)]
        for v in variants:
            model = eng.model(v, selected)
            if model is not None:
                starts.append((model, eng._start(model, placed, None)))
    best = None
    for model, theta0 in starts:
        res = eng._trf(model, theta0, "coupled")
        if res is None:
            continue
        theta = _with(theta0, model.free, res.x)
        J = eng.J(model, theta)
        if math.isfinite(J) and (best is None or J < best[2]):
            best = (model, theta, J)
    timing["continuous_s"] = time.perf_counter() - t0
    if best is None:
        return _failed(snapshot, a, "no_finite_candidate",
                       "The refit failed: no start reached a finite fit, so the previous fit stays.",
                       timing, priced, hits, notes)

    model, theta, J_new = best
    # a width mask is part of the topology: positions are slot positions, and at the incoming count the slot order
    # is the incoming's ascending order, so the comparison is positional and valid for relabelled slots too.
    # So is a variant (elastic, background, QE) change: the incoming's J is then not comparable
    variant_changed = old is not None and _variant_key(model.variant) != _variant_key(old[0].variant)
    topology_changed = (k != spec.n0 or slots != spec.incoming
                        or tuple(sorted(model.delta)) != spec.inc_delta or variant_changed)
    J_old = eng.J(*old) if old is not None and not topology_changed else None
    fixed_ok = all(abs(theta[p] - v) <= 1e-12 * max(1.0, abs(v)) for p, v in zip(model.fixed_pos, model.fixed_vals))
    in_window = all(spec.lo <= theta[e] <= spec.hi for e, _g, _c in model.modes)
    status, reason = rules.acceptance(
        any_finite=True, evidence_missing_for_change=topology_changed and "incomplete_comparison" in a.reasons,
        hard_violation=not (fixed_ok and in_window), topology_changed=topology_changed, evidence_won=a.changed,
        J_old=J_old, J_new=J_new, materially_moved=_materially_moved(eng, model, theta, old))
    if status == "failed":
        return _failed(snapshot, a, reason, f"The refit failed ({reason}); the previous fit stays.",
                       timing, priced, hits, notes)
    if status == "unchanged":
        model, theta = old                   # the incoming numerical state, never a slightly moved trial

    # objective breakdown at the solution
    D, J = eng.cost(model, theta), eng.J(model, theta)
    P, U = {}, {}
    unary = sum(((theta[i] - mu) / sd) ** 2 for i, mu, sd in model.soft)   # J counts the soft squares once
    for tid, p, _sign in eng.penalty_terms(model, theta):
        if tid.startswith(("entry:", "ramp:")):
            unary += p
        else:
            P[tid] = p
    for (ai, _g, _c), obj in zip(model.modes, objs):
        if isinstance(obj, cc.MatchedDensity):
            for f in obj.factors:
                U[f.factor_id] = cc.mismatch(f, obj._u(f, float(theta[ai])))

    # conditional covariance from f_cont = J_i / 2 (spec 4, 6)
    t1 = time.perf_counter()
    cov = _cov(lambda t: 0.5 * eng.J(model, t), model, theta)
    sd, corr = _sigmas(model, cov)
    timing["uncertainty_s"] = time.perf_counter() - t1

    # release fit: the same count with no inter-spectra potential and no entry price, own ramp kept (spec 6)
    t2 = time.perf_counter()
    ramp = cc.MatchedDensity(spec.ep, snapshot.unit, [], {})
    rel = _Labelled(spec.ctx, _ceiling(spec.fs, k), [ramp] * k, continuous=True, structures=spec.structures)
    rel_model = rel.model(model.variant, tuple(model.delta))
    rel_best = None
    for theta0 in [theta] + ([old[1]] if old is not None and old[0] is model else []):
        res = rel._trf(rel_model, theta0, "release")
        if res is None:
            continue
        t = _with(theta0, rel_model.free, res.x)
        Jr = rel.J(rel_model, t)
        if math.isfinite(Jr) and (rel_best is None or Jr < rel_best[1]):
            rel_best = (t, Jr)
    R = R_signed = None
    rel_sd, rel_railed = {}, set()
    if rel_best is not None:
        t_rel = rel_best[0]
        R_signed = D - rel.cost(rel_model, t_rel)
        R = max(0.0, R_signed)
        rel_sd, _corr = _sigmas(rel_model, _cov(lambda t: 0.5 * rel.J(rel_model, t), rel_model, t_rel))
        span = rel_model.hi - rel_model.lo
        rel_railed = {int(i) for i in rel_model.free
                      if min(t_rel[i] - rel_model.lo[i], rel_model.hi[i] - t_rel[i]) <= 1e-6 * span[i]}
    timing["release_s"] = time.perf_counter() - t2
    F = rules.flag_score(R, U)

    # modes, labelled by slot (slot order)
    # an unmatched mode continues the incoming mode nearest in energy that no slot names (identity's own
    # pairing); only a mode with no partner is a birth and gets a new id
    carried = _carried_ids(a, spec.own, [float(theta[ai]) for ai, _g, _c in model.modes])
    ids = [b if b is not None else carried.get(m) or new_branch_id(snapshot.fit_id, m) for m, b in enumerate(slots)]
    modes, energies = [], {}
    # of two modes closer than the twin distance the weaker one (a tie: the later slot) is sized by the
    # neighbours, not by this spectrum: a hidden mode reads held by neighbours whatever the release fit says
    near = spec.fs.twin_coincide_fwhm * spec.fwhm
    weaker = set()
    for i in range(k):
        for j in range(i + 1, k):
            if abs(theta[model.modes[i][0]] - theta[model.modes[j][0]]) < near:
                weaker.add(i if theta[model.modes[i][2]] < theta[model.modes[j][2]] else j)
    for m, (ai, gi, ci) in enumerate(model.modes):
        e, g, h = float(theta[ai]), float(theta[gi]), float(theta[ci])
        cond, relsig = sd.get(ai), rel_sd.get(ai)
        # the guard handed to later neighbours: never the conditional sigma alone, which the neighbours lent
        found = [x for x in (cond, relsig) if x is not None] if relsig is not None else []
        source = "max" if len(found) == 2 else "release" if found else "unavailable"
        # the fast pass's own parameter diagnostics on the release fit: a usable covariance, an energy sd
        # below the flat-prior width (else "undetermined") and off its bounds (else "boundary_limited");
        # corr(omega0, Gamma) is the regime rule, not identifiability, so it does not enter here
        release_ok = (relsig is not None and relsig < spec.ell / math.sqrt(12.0)
                      and ai not in rel_railed)
        # only a slot with a neighbour factor can be held by neighbours; an unmatched one is just poorly pinned
        mstatus = ("held_by_neighbours" if m in weaker and by_branch.get(slots[m])
                   else "poorly_determined" if cond is None else "determined_by_own_data" if release_ok
                   else "held_by_neighbours" if by_branch.get(slots[m]) else "poorly_determined")
        modes.append(HeldMode(branch=ids[m], omega0=e, gamma=g, height=h, guard=max(found) if found else None,
                              guard_source=source, status=mstatus,
                              regime=_regime(spec.fs, spec.fwhm, e, g, corr.get(m)), pinned=ai in model.fixed_pos))
        energies[ids[m]] = e
    fast = snapshot.fast or spec.own                 # what the changes are measured from (amendment 16, item 4)
    fast_by = _fast_modes(fast, ids, [md.omega0 for md in modes], exact_only=snapshot.fast is None)
    delta_fast = {"count": k - len(fast.modes), "omega0": {b: energies[b] - fast_by[b].omega0 for b in ids if b in fast_by}}

    flag = _flags(snapshot, spec, a, status, slots, ids, model, theta, modes, fast_by, F, R, cov, notes, dropped)
    why = _why(status, reason, spec.n0, k, slots, spec.incoming, J_old, J, variant_changed)
    fit, overlay, cov_out = _record(eng, spec, a, model, theta, cov, sd, rel_sd, modes, ids, D, status)
    normalisers = {f"ln_z:{b}": (float(o.ln_z), "ln meV") for b, o in zip(slots, objs)
                   if isinstance(o, cc.MatchedDensity)}
    if None in slots:
        q = _q_u(spec)
        normalisers.update({f"q_U:{m}": (q, "1") for m, b in enumerate(slots) if b is None})
    drawn = status in ("accepted", "unchanged")
    return RefitResult(
        unit=snapshot.unit, snapshot_id=snapshot.snapshot_id, status=status, reason=reason, assignment=a,
        fit=fit, modes=tuple(modes), D=D, J=J, P=P, U=U, unary=unary, energies=energies, R=R, R_signed=R_signed,
        F=F, delta_fast=delta_fast, flag=flag, why_changed=why, overlay=overlay if drawn else None,
        overlay_unavailable=None if drawn else f"a {status} refit is not drawn",
        covariance=cov_out, errors="conditional" if cov is not None else "unavailable", normalisers=normalisers,
        starts=eng.n_fits + rel.n_fits, timing=timing, priced=tuple(priced), cache_hits=hits)


def _fast_modes(fast: Held, ids, energy, *, exact_only: bool) -> dict[str, HeldMode]:
    """Result branch id -> the fast solo's mode it descends from, one-to-one only: the same id, else (the
    solo carries its own ``fast:<unit>#i`` ids) the same index when the count is unchanged. After a split
    or merge the modes without a partner have no fast comparison."""
    by_id = {md.branch: md for md in fast.modes}
    out = {b: by_id[b] for b in ids if b in by_id}
    if exact_only or len(ids) != len(fast.modes):
        return out
    used = {md.branch for md in out.values()}
    order = sorted(range(len(ids)), key=lambda m: energy[m])    # the solo's modes are in energy order
    for m, md in zip(order, fast.modes):
        if ids[m] not in out and md.branch not in used:
            out[ids[m]] = md
    return out


def _flags(snapshot, spec, a, status, slots, ids, model, theta, modes, fast_by, F, R, cov, notes, dropped) -> dict:
    """The flag (N2): F against the threshold, and every own reason of the design's list (they flag
    whatever the score); the four note codes and a neighbour-supported split never set ``flagged``."""
    s = snapshot.settings
    k = len(slots)
    reasons = list(notes)
    if F is not None and F > s.flag_threshold:
        reasons.append(_reason("score_over_threshold", f"flag score {F:.1f} is over the threshold {s.flag_threshold:g}",
                               [snapshot.unit], F=F, threshold=s.flag_threshold))
    if status == "accepted" and a.changed and k != spec.n0:
        if k > spec.n0 and sum(b is None for b in slots) <= sum(b is None for b in spec.incoming):
            reasons.append(_reason("neighbour_supported_split",
                                   f"one mode resolved into components of the neighbours' branches ({spec.n0} to {k})",
                                   [b for b in slots if b], count_from=spec.n0, count_to=k))
        else:
            reasons.append(_reason("count_changed_by_neighbours",
                                   f"the neighbours' evidence changed the count from {spec.n0} to {k}",
                                   [snapshot.unit], count_from=spec.n0, count_to=k))
    final = slots if status in ("accepted", "unchanged") else spec.incoming
    if status in ("accepted", "unchanged"):
        for m, b in enumerate(slots):
            e = float(theta[model.modes[m][0]])
            if b is None and spec.d_edge(e) >= spec.band:
                reasons.append(_reason("unmatched_branch_mid_window",
                                       f"a mode at {e:.2f} meV matches no neighbour branch, away from the window edges",
                                       [ids[m]], omega0=e, edge_band=spec.band))
    # one branch under two merged ids (an alias) is seen when either names a final slot
    canon = snapshot.aliases
    seen = {canon.get(b, b) for b in final if b is not None}
    for b in spec.pool:
        cs = [c for c in spec.pred.get(b, ()) if spec.lo <= c <= spec.hi and spec.d_edge(c) >= spec.band]
        if cs and canon.get(b, b) not in seen:
            reasons.append(_reason("branch_lost_mid_window",
                                   f"neighbour branch {b} is predicted at {cs[0]:.2f} meV but not seen here",
                                   [b], predicted=cs))
    for m, b in enumerate(slots):
        if b is None or b not in fast_by:
            continue
        g_old, g_new = fast_by[b].gamma, modes[m].gamma
        ratio = g_new / g_old if g_old > 0 else math.inf
        if not 1.0 / s.linewidth_ratio <= ratio <= s.linewidth_ratio:
            reasons.append(_reason("linewidth_change", f"the width of {b} changed by a factor {ratio:.2f}",
                                   [b], gamma_from=g_old, gamma_to=g_new))
        if fast_by[b].regime == "underdamped" and modes[m].regime in ("overdamped", "near_elastic"):
            reasons.append(_reason("overdamped_entry", f"{b} moved from underdamped to {modes[m].regime}",
                                   [b], regime=modes[m].regime))
    if "incomplete_comparison" in a.reasons:
        reasons.append(_reason("incomplete_comparison", "the incoming fit had no priced evidence to compare against",
                               [snapshot.unit]))
    for text in dropped:
        reasons.append(_reason("incomplete_comparison", f"a frozen factor was dropped: {text}", [snapshot.unit]))
    if R is None:
        reasons.append(_reason("release_fit_failed", "the fit without neighbours failed; no flag score",
                               [snapshot.unit]))
    if cov is None:
        reasons.append(_reason("errors_unavailable", "error bars unavailable for this fit", [snapshot.unit]))
    flagged = (F is not None and F > s.flag_threshold) or any(r["code"] not in NOTE_CODES for r in reasons)
    return {"score": F, "flagged": bool(flagged), "reasons": reasons}


def _why(status, reason, n0, k, slots, incoming, J_old, J, variant_changed=False) -> str:
    if status == "accepted" and k != n0:
        return f"The neighbours' evidence changed the number of modes from {n0} to {k}."
    if status == "accepted" and slots != incoming:
        return "The neighbours' evidence changed which branch a mode belongs to."
    if status == "accepted" and reason == "evidence" and variant_changed:
        return ("The evidence changed which components the model carries (the quasi-elastic line, the elastic line "
                "or the background).")
    if status == "accepted" and reason == "evidence":
        return "The evidence changed which modes are resolution limited."
    if status == "accepted":
        gain = "" if J_old is None else f" by {J_old - J:.3g}"
        return f"Fitting with the neighbours lowered the shared objective{gain}, so the modes moved."
    if status == "unchanged":
        return "The neighbours left this fit unchanged."
    words = {"no_supported_improvement": "the neighbours' evidence did not support the change",
             "material_change_without_gain": "it moved the fit without lowering the shared objective",
             "hard_constraint": "it broke a fixed value or left the energy window"}
    return f"The refit was not kept: {words.get(reason, reason)}."


def _qe_of(eng, model, theta, full):
    """The refit's central QE as the fast pass's record carries it (None without one)."""
    if model.qe_h is None:
        return None
    height, width = float(theta[model.qe_h]), model.qe_width(theta)

    def sd(i):
        v = None if full is None or i is None else float(full[i, i])
        return math.sqrt(v) if v is not None and v > 0 else None
    used = np.asarray(eng.ctx.x_used, dtype=float) - float(eng.ctx.center)
    return asdict(fast_pass.QEResult(
        height=height, width=width, pinned=model.qe_w is None,
        weight=CentralQE().spectral_weight({"h": height, "width": width}, eng.ctx),
        window=(float(used.min()), float(used.max())), convention=QE_WEIGHT_CONVENTION,
        sigma_height=sd(model.qe_h), sigma_width=sd(model.qe_w)))


def _record(eng, spec, a, model, theta, cov, sd, rel_sd, modes, ids, D, status):
    """The refit as a fast-pass-shaped record and its drawable overlay (N3), modes canonicalised to
    ascending energy for output only (branch ids, masks and covariance permuted with them, spec 3); and
    the full covariance in that order."""
    order = sorted(range(len(model.modes)), key=lambda m: theta[model.modes[m][0]])
    idx = list(range(len(model.names)))
    for dst, src in enumerate(order):
        for p, q in zip(model.modes[dst], model.modes[src]):
            idx[p] = q
    delta = tuple(sorted(dst for dst, src in enumerate(order) if src in model.delta))
    cmodel = eng.model(model.variant, delta) or model
    ctheta = theta[idx]
    full = None
    if cov is not None:
        full = np.zeros((len(model.names), len(model.names)))
        full[np.ix_(model.free, model.free)] = cov
        full = full[np.ix_(idx, idx)]
    peaks = []
    for dst, src in enumerate(order):
        ai, gi, ci = model.modes[src]
        md = modes[src]
        peaks.append({"index": dst, "omega0": md.omega0, "Gamma": md.gamma, "delta": src in model.delta,
                      "height": md.height, "sigma_omega0": sd.get(ai), "sigma_Gamma": sd.get(gi),
                      "sigma_height": sd.get(ci), "sigma_release": rel_sd.get(ai), "regime": md.regime,
                      "mode_status": md.status, "branch": ids[src], "confident": md.status != "poorly_determined"})
    cp = (a.coverage.get("decision") or {}).get("count_p") or spec.snap.own_record.get("count_probabilities") or {}
    p_k = cp.get(str(a.count))
    label = str(cmodel.hyp.label)
    params = {n: float(v) for n, v in zip(cmodel.names, ctheta)}
    fit = _finite({
        "schema": RECORD_SCHEMA, "source": "inter_refit", "status": status,
        "state": "close_call" if p_k is not None and p_k < spec.snap.settings.confidence else "resolved",
        "energy_support_meV": [spec.lo, spec.hi],
        "preferred_count": a.count, "count_probabilities": dict(cp),
        "probabilities_conditional": a.discrete == "priced", "close_call_alternatives": [],
        "numerically_marginal": False, "search_limit_reason": "", "deep_pass_candidate": False,
        "selected_model_id": f"{label}#0", "selected_label": label,
        "models": [{"model_id": f"{label}#0", "label": label, "n_peaks": a.count,
                    "elastic": cmodel.variant.elastic, "background": cmodel.variant.background,
                    "qe": cmodel.variant.qe, "on_demand": cmodel.variant.on_demand,
                    "removed": list(cmodel.variant.removed),
                    "delta_modes": list(delta), "free": [cmodel.names[i] for i in cmodel.free],
                    "cost": D, "evidence_status": "conditional", "probability": p_k,
                    "params": params, "map_params": params}],
        "peaks": peaks,
        "elastic_height": None if cmodel.el is None else float(ctheta[cmodel.el]),
        "background": None if cmodel.bg is None else float(ctheta[cmodel.bg]),
        "qe": _qe_of(eng, cmodel, ctheta, full),
        "grouped_intensity": [], "flags": [], "flag_statistics": {},
        "errors": "conditional" if cov is not None else "unavailable"})
    basin = fast_pass._Basin(cmodel, 0, ctheta, D, eng.sat - D / 2, 0.0, "coupled", status="ok")
    sf = eng._structure_fit(basin, ctheta, full)
    err = getattr(sf, "omega0_err_hesse", None)
    interval = None
    if sf.omega0 is not None and err is not None and math.isfinite(err) and math.isfinite(sf.omega0):
        interval = [float(sf.omega0) - float(err), float(sf.omega0) + float(err)]
    overlay = _finite({"fast_pass": fit, "modes": [dict(m) for m in sf.modes or ()],
                       "model_manifest": _manifest(cmodel, ctheta), "omega0": sf.omega0, "best_label": label,
                       "n_modes": a.count, "interval": interval})
    return fit, overlay, full


# ---- the shared objective and seams ------------------------------------------------------------------------
def total_objective(factors: Iterable[Factor], energies: Mapping[tuple[str, str], float],
                    data: Mapping[str, float], unary: Mapping[str, float]) -> tuple[float, dict[str, float]]:
    """Spec 4: sum D_i + each physical factor once (by id; two definitions under one id raise) + unary."""
    return rules.total_objective(factors, energies, data, unary, cc.potential)


def _ell(r) -> float:
    """The energy-window width of a refit's spectrum, from the support its record carries (0 if none: a
    failed refit has no modes, so no guard is ever asked of it)."""
    lo, hi = (r.fit or {}).get("energy_support_meV") or (0.0, 0.0)
    return float(hi) - float(lo)


def seam_report(a: RefitResult, b: RefitResult, link: Link, settings: InterSettings) -> dict:
    """Read-only mismatch of two ring-mates across one link (spec 0): for each branch both carry, the
    change factor's P and unlevelled U with both ends' guards (pair convention); nothing is added to either
    fit. ``over`` is True when any U passes ``flag_threshold`` (the seam_disagreement flag)."""
    s = settings
    if link.kind == "T":
        base, d = s.t_base(link.step)
        kind, family = "dT", s.shape_family
    else:
        base = s.q_tolerance(link.metric, link.step, f"seam {link.a}|{link.b}")
        d, kind, family = 0.0, "dq", "welsch"
    first, second = (a, b) if a.unit == link.a else (b, a)
    ga = {m.branch: m for m in first.modes}
    gb = {m.branch: m for m in second.modes}
    pairs = []
    for br in sorted(set(ga) & set(gb)):
        # an unavailable guard widens by the spec's fallback ell/sqrt(12) (that spectrum's window), never 0
        sa, sb = (m.guard if m.guard is not None else _ell(r) / math.sqrt(12.0)
                  for m, r in ((ga[br], first), (gb[br], second)))
        f = cc.Factor(f"seam:{link.a}|{link.b}:{br}", br, (link.a, link.b), (1.0, -1.0),
                      s=s.widened(base, sa * sa + sb * sb, d), C=s.penalty_cap, kind=kind, d=d, family=family)
        u = ga[br].omega0 - gb[br].omega0
        pairs.append({"branch": br, "dE": u, "P": cc.potential(f, u), "U": cc.mismatch(f, u),
                      "guard_unavailable": ga[br].guard is None or gb[br].guard is None})
    max_u = max((p["U"] for p in pairs), default=0.0)
    return {"link": [link.a, link.b], "kind": link.kind, "pairs": pairs, "max_U": max_u,
            "threshold": s.flag_threshold, "over": max_u > s.flag_threshold,
            "only_one_side": sorted(set(ga) ^ set(gb))}

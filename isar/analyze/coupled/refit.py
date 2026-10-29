"""The coupled refit (C-refit): one spectrum's two-stage refit against a frozen neighbour snapshot.

Stage 1 (``assign_jobs``, ``price``, ``assign_decide``; ``assign`` runs the three inline) compares counts
and branch associations by evidence under one normalised neighbour density per matched mode (the
hand-over ``engine``), with the unmatched entry price kept inside the odds. Stage 2 (``refit(stage=
"continuous")``) refines the continuous parameters under the one shared objective J with the stage's
frozen factor definitions, then reports the acceptance status (spec 5), conditional error bars, a
release fit, the flag score F = R + max U, every flag reason, one sentence why and a drawable overlay.
The decision rules are ``rules`` (installed verbatim); thresholds always come from ``InterSettings``.

``Snapshot.context`` is the spectrum's own prepared fast-pass context: a ``ScanContext``, or an object
carrying it as ``ctx`` with optional ``settings`` (its ``FastPassSettings``: mode ceiling and prior
recipe) and ``structures`` (the catalogue whitelist). The fast pass itself is never edited or rerun:
``engine.CoupledEngine`` subclasses its engine. Counts stay raw Poisson (Baker-Cousins, sigma 0 at zero
counts, no floor) and the anti-Stokes amplitude stays derived by detailed balance: both live in the base
engine's model, which this module never changes. No I/O, no pool, no Qt.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Literal

import numpy as np
from scipy.integrate import quad
from scipy.special import logsumexp

from isar.analyze import fast_pass, loop, prior
from isar.analyze.components import ConstantBackground, Elastic, PhononDHO, QuasiElastic
from isar.analyze.coupled import engine as cc
from isar.analyze.coupled import rules
from isar.analyze.coupled.identity import ModeObs, _carried_ids, match_link, new_branch_id
from isar.analyze.coupled.settings import InterSettings
from isar.analyze.coupled.types import (SEVERITY_OF, Assignment, Dormant, Held, HeldMode, Priced, PricingJob,
                                        RefitResult, Snapshot)
from isar.analyze.model_manifest import MANIFEST_SCHEMA

if TYPE_CHECKING:
    from isar.analyze.coupled.engine import Factor
    from isar.session.inter_spectra.graph import Link

# Numerical defaults of the specification (uncalibrated; spec 5): an "unchanged" refit moves the predicted
# raw mean by at most MEAN_REL_TOL (denominator max(1, mu_old)) and every free width, height and
# background by at most PARAM_REL_TOL (scale max(|old|, 1)).
MEAN_REL_TOL = 1e-4
PARAM_REL_TOL = 1e-3
# The fast pass's record shape (C-fastpass-record): a refit's ``fit`` keeps it so every reader of a solo
# record reads a refit too; its ``source`` says which it is.
RECORD_SCHEMA = "isar.fast-pass/1"
# Flag codes shown as notes: they never set ``flagged`` (S1U2 blueprint, "Flags and severities").
NOTE_CODES = ("neighbour_precision_unavailable", "incomplete_comparison", "release_fit_failed",
              "errors_unavailable")
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
    maximum of the release sigma stored with it and its conditional sigma. ``None`` (source
    "unavailable") when neither exists: a factor that uses it falls back to ``ell/sqrt(12)`` and notes
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
            guard, source = (rel, "release") if rel is not None else (cond, "conditional")
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
    """Everything stage 1 derives from a snapshot: the support, the entry price, the branch pool, one
    factor definition per (term, branch), the trigger filter, the candidate counts and the jobs."""

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
        self.ceiling = int(self.fs.mode_ceiling)
        self.own = snap.own
        self.n0 = len(self.own.modes)
        self.held_all = {h.unit: h for ti in snap.terms for h in ti.held}
        self.notes: list[dict] = []
        self.factors, self.frozen = self._build_factors()
        pool = set(self.factors)
        dormant = {d.branch for h in self.held_all.values() for d in h.dormant}
        dormant |= {d.branch for d in self.own.dormant}
        self.returning = frozenset(dormant - pool)
        self.pool = tuple(sorted(pool | dormant))
        self.pred = {b: [self._centre(f) for f in fs_] for b, fs_ in self.factors.items()}
        self.incoming = tuple(md.branch if md.branch in self.pool else None for md in self.own.modes)
        self.triggers, self.counts, self.missing_history = self._trigger_filter()

    # -- factors (spec 1)
    def _guard(self, held: Held, branch: str) -> float:
        md = next((m for m in held.modes if m.branch == branch), None)
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
            common = set.intersection(*({m.branch for m in h.modes} for h in held.values()))
            for b in sorted(common):
                energy = {u: next(m.omega0 for m in h.modes if m.branch == b) for u, h in held.items()}
                guards = {u: self._guard(h, b) for u, h in held.items()}
                # anchored: the held end's guard only; simultaneous: both ends (symmetric, spec 1)
                guards[me] = 0.0 if anchored else self._guard(self.own, b)
                w = d = 0.0
                if term.kind == "T-change":
                    a = (1.0, -1.0)
                    step = abs(float(term.spacings[0])) / s.reference_T_K
                    if s.t_spacing_form == "divide":
                        base, d = s.t_tolerance_meV, step
                    else:
                        base = s.t_tolerance_meV * step ** s.step_exponent
                    spread = sum(guards[u] ** 2 for u in members)
                elif term.kind == "q-change":
                    a, base, w = (1.0, -1.0), s.q_change_scale_meV, s.q_tolerance_meV
                    spread = sum(guards[u] ** 2 for u in members)
                elif term.kind == "curvature":
                    t = float(term.t_frac)
                    a, base = (-(1.0 - t), 1.0, -t), s.q_curvature_scale_meV
                    left, _centre, right = members      # the centre's prediction, designated once
                    spread = ((1.0 - t) * guards[left] + t * guards[right]) ** 2
                else:
                    raise ValueError(f"unknown term kind {term.kind!r}")
                f = cc.Factor(f"{term.kind}:{'|'.join(members)}:{b}", b, members, a,
                              s=math.sqrt(base * base + spread), C=s.penalty_cap, kind=_FACTOR_KIND[term.kind],
                              w=w, d=d, family=s.shape_family)
                factors.setdefault(b, []).append(f)
                frozen.setdefault(b, {}).update(energy)
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

    # -- the trigger filter and the counts it proposes (spec 3)
    def _record_probs(self):
        probs = self.snap.own_record.get("count_probabilities")
        if not probs:
            return None
        return {int(k): float(v) for k, v in probs.items() if v is not None}

    def _association_gap(self) -> float:
        """Cheapest other association's cost minus the incoming one's (deviance): negative when another is
        cheaper, within ``2 * close_call_nats`` when it is a close call (match_link's orders carry the
        exchanged ones), -inf when the incoming identity is invalid (ambiguous)."""
        s = self.snap.settings
        local = self._obs([(m.omega0, m.gamma, m.height) for m in self.own.modes])
        out = match_link(local, self.pool, lambda m, b: self.cost(local[m].omega0, b), incoming=self.incoming,
                         keep=s.composition_limit, close=s.close_call_nats)
        inc = next((p for p in out["proposals"] if p["source"] == "incoming"), None)
        if inc is None:
            return -math.inf
        others = [o["cost"] for p in out["proposals"] for o in p["orders"] if tuple(o["slots"]) != self.incoming]
        return min(others, default=math.inf) - inc["cost"]

    def _trigger_filter(self):
        s, trig, counts = self.snap.settings, [], {self.n0}
        missing = [b for b in self.pool if b in self.factors and self.inside(b) and b not in self.incoming]
        if missing:
            trig.append("a:missing_branch")
            counts |= {self.n0 + len(missing), len([b for b in self.pool if self.inside(b)])}
        unsupported = [m for m, b in enumerate(self.incoming) if b is None]
        b_fired = False
        if any(self.d_edge(self.own.modes[m].omega0) >= self.band for m in unsupported):
            trig.append("b:unmatched_mid_window")
            b_fired = True
        gap = self._association_gap() if self.pool and self.n0 else math.inf
        if gap < 0:
            trig.append("b:association_differs")
            b_fired = True
        elif gap <= 2.0 * s.close_call_nats:          # deviance window of identity.match_link
            trig.append("c:close_association")
        U = [cc.mismatch(f, self.u(f, md.omega0)) for md, b in zip(self.own.modes, self.incoming) if b
             for f in self.factors.get(b, ())]
        if max(U, default=0.0) >= s.flag_threshold:
            trig.append("b:large_mismatch")
            b_fired = True
        if b_fired:
            counts |= {self.n0 - j for j in range(1, len(unsupported) + 1)}
        probs, missing_history = self._record_probs(), False
        if probs is None or probs.get(self.n0, 0.0) <= 0:
            trig.append("c:no_count_evidence")
            missing_history = True
            counts |= set(range(self.ceiling + 1))
        else:
            close = {k for k, p in probs.items() if k != self.n0 and p > 0
                     and math.log(p) >= math.log(probs[self.n0]) - s.close_call_nats}
            if probs[self.n0] < s.confidence:
                trig.append("c:low_count_probability")
            if close:
                trig.append("c:close_alternative")
                counts |= close
        other = {len(h.modes) for h in self.held_all.values()} - {self.n0}
        if other:
            trig.append("d:neighbour_count")
            counts |= other
        if s.full_search:
            trig.append("e:full_search")
            counts |= set(range(self.ceiling + 1))
        counts = tuple(sorted(k for k in counts if 0 <= k <= self.ceiling)) if trig else ()
        return tuple(trig), counts, missing_history

    # -- starts and compositions
    def _obs(self, modes) -> list:
        return [ModeObs(ref=(self.snap.unit, self.snap.fit_id, m), omega0=float(e), gamma=float(g),
                        height=float(h), sigma_guard=None, sigma_provenance={}, branch=None)
                for m, (e, g, h) in enumerate(modes)]

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

    def base_modes(self, k: int) -> list:
        """The start modes (omega0, Gamma, h) at count k, ascending: the incoming modes at its own count,
        else the record's saved model at k, else the incoming modes with neighbour-predicted branches
        added (then splits) above, or the least supported modes deleted below."""
        inc = [(m.omega0, m.gamma, m.height) for m in self.own.modes]
        if k == self.n0:
            return inc
        rec = self._record_modes(k)
        if rec is not None:
            return rec
        modes = list(inc)
        if k > self.n0:
            for b in [b for b in self.pool if b in self.factors and self.inside(b) and b not in self.incoming]:
                if len(modes) == k:
                    break
                md = next(m for h in self.held_all.values() for m in h.modes if m.branch == b)
                e = float(np.mean([c for c in self.pred[b] if self.lo <= c <= self.hi]))
                modes.append((e, md.gamma, md.height))
            while len(modes) < k:          # split the highest mode (the ladder's split rule)
                if not modes:
                    modes.append((0.5 * (self.lo + self.hi), fast_pass.LADDER_GAMMA, 1.0))
                    continue
                e, g, h = modes.pop(max(range(len(modes)), key=lambda i: modes[i][2]))
                lo_f, hi_f = fast_pass.LADDER_SPLIT_HEIGHTS
                dw = fast_pass.LADDER_SPLIT_FWHM * self.fwhm
                modes += [(max(self.lo, e - dw), g, h * lo_f), (min(self.hi, e + dw), g, h * hi_f)]
        else:
            order = sorted(range(len(modes)), key=lambda m: (self.incoming[m] is not None,
                                                             -self.cost(modes[m][0], None), modes[m][2]))
            drop = set(order[:self.n0 - k])
            modes = [md for m, md in enumerate(modes) if m not in drop]
        return sorted(modes)

    def compositions(self):
        """[(count, slots)] to price and the merged coverage: every slot order match_link returns
        within the close window, for every candidate count."""
        s = self.snap.settings
        out, truncated, limited = [], False, False
        for k in self.counts:
            local = self._obs(self.base_modes(k))
            got = match_link(local, self.pool, lambda m, b, local=local: self.cost(local[m].omega0, b),
                             incoming=self.incoming if k == self.n0 else None,
                             keep=None if s.full_search else s.composition_limit, close=s.close_call_nats)
            truncated |= got["coverage"]["enumeration_truncated"]
            limited |= got["coverage"]["search_limited"]
            for p in got["proposals"]:
                for o in p["orders"]:
                    if (k, tuple(o["slots"])) not in out:
                        out.append((k, tuple(o["slots"])))
        untried = [k for k in range(self.ceiling + 1) if k not in self.counts]
        coverage = {"counts_tried": list(self.counts), "counts_untried": untried,
                    "enumeration_truncated": truncated, "missing_candidate_history": self.missing_history,
                    "full_search": bool(s.full_search)}
        return out, coverage, bool(limited or truncated or untried)

    def key(self, k: int, slots) -> str:
        """Everything a price depends on (remedy 1): the spectrum, its fit and inputs, the count and slots,
        every held fit, mode value and guard of its terms, and the full settings. Never the snapshot id, so
        an identical snapshot in a later round reuses the price; any moved value re-prices."""
        snap = self.snap

        def held(h):
            return [h.unit, h.fit, h.inputs, list(h.window), h.T,
                    [[m.branch, m.omega0, m.gamma, m.height, m.guard, m.pinned] for m in h.modes],
                    [[d.branch, d.last_seen_fit, d.last_seen_unit] for d in h.dormant]]
        payload = {"unit": snap.unit, "own": held(snap.own), "count": k, "slots": list(slots),
                   "batch_kind": snap.batch_kind, "settings": snap.settings.to_values(),
                   "terms": [[ti.term.kind, list(ti.term.members), list(ti.term.spacings), ti.term.t_frac,
                              [held(h) for h in ti.held]] for ti in snap.terms]}
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=repr).encode("utf-8")).hexdigest()

    def jobs(self):
        comps, coverage, limited = self.compositions()
        return [PricingJob(self.snap.unit, self.snap.snapshot_id, k, slots, self.key(k, slots))
                for k, slots in comps], coverage, limited

    def origins(self, slots) -> tuple[str, ...]:
        return tuple("new_branch" if b is None else "returning" if b in self.returning else "matched"
                     for b in slots)

    def incident(self, slots) -> tuple:
        return tuple(f for b in slots if b is not None for f in self.factors.get(b, ()))

    def skipped(self) -> Assignment:
        return Assignment(
            unit=self.snap.unit, snapshot_id=self.snap.stage_id, count=self.n0, slots=self.incoming,
            origins=self.origins(self.incoming), factors=self.incident(self.incoming), discrete="skipped",
            triggers=(), candidates=(),
            coverage={"counts_tried": [], "counts_untried": list(range(self.ceiling + 1)),
                      "enumeration_truncated": False, "missing_candidate_history": False,
                      "full_search": False, "discrete_skipped": "no_proposed_change"},
            search_limited=True, changed=False, reasons=())

    # -- the slot densities of one composition
    def held_value(self, unit: str, branch: str) -> float:
        h = self.held_all.get(unit)
        md = None if h is None else next((m for m in h.modes if m.branch == branch), None)
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
def assign_jobs(snapshot: Snapshot, *, cached: Mapping[str, Priced] = {}) -> tuple[Assignment | None, tuple[PricingJob, ...]]:
    """The trigger filter on the frozen snapshot. Untriggered: the incoming composition as a skipped
    Assignment and no jobs. Triggered: None and every pricing job whose key is not in ``cached``."""
    spec = _Spec(snapshot)
    if not spec.triggers:
        return spec.skipped(), ()
    jobs, _coverage, _limited = spec.jobs()
    return None, tuple(j for j in jobs if j.key not in cached)


def _starts(spec: _Spec, k: int, slots) -> list:
    """Slot-ordered starts of one composition: its own (the base modes) and neighbour-placed (every
    matched slot at its branch's predicted energy)."""
    own = [(e, g, h, False) for e, g, h in spec.base_modes(k)]
    placed = []
    for (e, g, h, d), b in zip(own, slots):
        cs = [c for c in spec.pred.get(b, ()) if spec.lo <= c <= spec.hi] if b is not None else []
        placed.append((float(np.mean(cs)) if cs else e, g, h, d))
    return [("own", own)] + ([("neighbour", placed)] if placed != own else [])


def price(snapshot: Snapshot, job: PricingJob) -> Priced:
    """One composition at one count: every start in every admissible variant (the base engine's TRF,
    twin resolution and delta alternatives), each distinct basin priced by Laplace under the coupled
    prior; ln Z sums the basins' masses with their variant/delta weights, then adds ``ln w0`` and, for a
    pinned unmatched energy, its entry charge outside the integral (spec 2)."""
    t0 = time.perf_counter()
    spec = _Spec(snapshot)
    k, slots = job.count, tuple(job.slots)
    if len(slots) != k:
        raise ValueError(f"job {job.key}: {len(slots)} slots for count {k}")
    objs = spec.slot_objects(slots)
    bad = [b for b, o in zip(slots, objs) if isinstance(o, cc.MatchedDensity) and o.status != "ok"]
    if bad:
        return Priced(job.key, k, slots, None, "no_evidence", None,
                      f"the neighbour density of {bad[0]} has no verified normaliser", 0, time.perf_counter() - t0)
    u = sum(b is None for b in slots)
    eng = _Labelled(spec.ctx, spec.fs, objs, structures=spec.structures)
    for v in eng.variants.get(k, ()):
        for family, modes in _starts(spec, k, slots):
            eng.fit(v, modes, family)
    while True:
        pending = [b for b in eng.basins() if b.count == k and b.status == "pending"]
        if not pending:
            break
        for b in pending:
            if b.status == "pending":
                eng.price(b)
    basins = [b for b in eng.basins() if b.count == k]
    priced = [b for b in basins if b.status in _PRICED and b.ln_z is not None]
    if not priced:
        twin = bool(basins) and all(b.status == "degenerate_twin" for b in basins)
        reason = next((b.reason for b in basins if b.reason), "no start converged")
        return Priced(job.key, k, slots, None, "degenerate_twin" if twin else "no_evidence", None, reason,
                      eng.n_fits, time.perf_counter() - t0)

    def charge(b):
        fixed = set(b.model.fixed_pos)
        return sum(rules.pinned_entry_charge(spec.entry, float(b.theta_map[a]))
                   for (a, _g, _c), sl in zip(b.model.modes, slots) if sl is None and a in fixed)
    terms = [b.ln_model_prior + b.ln_z + charge(b) for b in priced]
    best = priced[int(np.argmax(terms))]
    ln_z = float(logsumexp(terms)) + rules.ln_w0(k, k - u, len(spec.pool), spec.ceiling + 1)
    mode = (best.model.variant.elastic, best.model.variant.background, tuple(best.model.delta),
            tuple(float(v) for v in best.theta_map))
    return Priced(job.key, k, slots, ln_z, best.status, mode, None, eng.n_fits, time.perf_counter() - t0)


def assign_decide(snapshot: Snapshot, priced: Sequence[Priced]) -> Assignment:
    """Marginalise this snapshot's priced compositions (basins summed, composition prior normalised once
    across counts), apply the evidence rule (hysteresis, tie to the incoming) and return the frozen
    Assignment with every incident factor's definition. A job left unpriced stays unpriced, never zero."""
    spec = _Spec(snapshot)
    if not spec.triggers:
        return spec.skipped()
    s = snapshot.settings
    jobs, coverage, limited = spec.jobs()
    by_key = {p.key: p for p in priced}
    rows, cands = [], []
    for j in jobs:
        p = by_key.get(j.key)
        lz = p.ln_z if p is not None else None
        rows.append((j.count, j.slots, lz))
        cands.append({"count": j.count, "slots": j.slots, "ln_z": lz,
                      "status": p.status if p is not None else "unpriced", "key": j.key})
    d = rules.decide(rows, (spec.n0, spec.incoming), eps=s.evidence_hysteresis_nats, conf=s.confidence,
                     close=s.close_call_nats)
    for c in cands:
        c["weight"] = d["weights"].get((c["count"], tuple(c["slots"])), 0.0)
    winner = d["winner"] or (spec.n0, spec.incoming)
    k, slots = winner[0], tuple(winner[1])
    coverage = {**coverage, "decision": {
        "ambiguous": d["ambiguous"], "close_calls": [[c[0], list(c[1])] for c in d["close_calls"]],
        "count_p": {str(c): p for c, p in sorted(d["count_p"].items())}}}
    return Assignment(unit=snapshot.unit, snapshot_id=snapshot.stage_id, count=k, slots=slots,
                      origins=spec.origins(slots), factors=spec.incident(slots), discrete="priced",
                      triggers=spec.triggers, candidates=tuple(cands), coverage=coverage, search_limited=limited,
                      changed=bool(d["changed"]), reasons=tuple(d["reasons"]))


def assign(snapshot: Snapshot, *, cached: Mapping[str, Priced] = {}) -> tuple[Assignment, tuple[Priced, ...]]:
    """``assign_jobs``, ``price`` and ``assign_decide`` inline; returns the prices used or made."""
    a, todo = assign_jobs(snapshot, cached=cached)
    if a is not None:
        return a, ()
    jobs, _coverage, _limited = _Spec(snapshot).jobs()
    made = {j.key: price(snapshot, j) for j in todo}
    used = tuple(made[j.key] if j.key in made else cached[j.key] for j in jobs)
    return assign_decide(snapshot, used), used


# ---- stage 2 ---------------------------------------------------------------------------------------------
def _incoming_state(eng, spec: _Spec):
    """(model, theta) of the incoming fit's selected model at its stored point, or None."""
    sel = _selected_model(spec.snap.own_record)
    if sel is None or sel.get("n_peaks") != spec.n0:
        return None
    variant = eng.variant(spec.n0, sel.get("elastic"), sel.get("background"))
    model = None if variant is None else eng.model(variant, tuple(sel.get("delta_modes") or ()))
    p = sel.get("map_params") or sel.get("params") or {}
    if model is None or any(p.get(n) is None for n in model.names):
        return None
    return model, np.array([float(p[n]) for n in model.names])


def _with(theta, free, z):
    t = theta.copy()
    t[free] = z
    return t


def _materially_moved(eng, model, theta, old) -> bool:
    if old is None or old[0] is not model:
        return True
    mu_old, mu = eng.mean(model, old[1]), eng.mean(model, theta)
    if np.max(np.abs(mu - mu_old) / np.maximum(1.0, mu_old), initial=0.0) > MEAN_REL_TOL:
        return True
    energies = {a for a, _g, _c in model.modes}
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
             ConstantBackground: "constant_background"}
    components = []
    for c in model.hyp.build().components:
        kind = kinds.get(type(c))
        if kind is None:
            return None
        item = {"type": kind, "prefix": str(c.prefix)}
        if isinstance(c, PhononDHO):
            item["peak"] = str(c.peak)
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
        spec = _Spec(snapshot)
        if spec.triggers:
            hits = sum(j.key in cached for j in spec.jobs()[0])
        assignment, priced = assign(snapshot, cached=cached)
        timing["assign_s"] = time.perf_counter() - t0
        timing["likelihood_s"] = sum(p.seconds for p in priced if p.key not in cached)
    elif stage != "continuous":
        raise ValueError(f"unknown refit stage {stage!r}")
    if assignment is None:
        raise ValueError("the continuous stage needs the stage's Assignment")
    if assignment.unit != snapshot.unit or assignment.snapshot_id != snapshot.stage_id:
        raise ValueError(f"the assignment of {assignment.unit} on {assignment.snapshot_id} does not govern "
                         f"{snapshot.unit} on {snapshot.stage_id}")
    return _continuous(snapshot, assignment, priced, cached, hits, timing)


def _continuous(snapshot, a, priced, cached, hits, timing) -> RefitResult:
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
        by_branch.setdefault(f.branch_id, []).append(f)
    notes = [_reason(n["code"], "a neighbour's precision is unavailable; its guard is the window width / sqrt(12)",
                     n["ids"], **n["values"]) for n in spec.notes]
    objs = spec.slot_objects(slots, by_branch)
    eng = _Labelled(spec.ctx, spec.fs, objs, continuous=True, structures=spec.structures)
    old = _incoming_state(eng, spec) if k == spec.n0 else None

    # starts: the incoming state, the priced posterior mode, the composition's own and neighbour-placed starts
    starts = [] if old is None else [old]
    win = cached.get(spec.key(k, slots)) or next((p for p in priced if p.count == k and tuple(p.slots) == slots), None)
    if win is not None and win.mode is not None:
        el, bg, delta, theta = win.mode
        variant = eng.variant(k, el, bg)
        model = None if variant is None else eng.model(variant, delta)
        if model is not None:
            starts.append((model, np.array(theta, dtype=float)))
            priced = priced or (win,)
    variants = [old[0].variant] if old is not None else list(eng.variants.get(k, ()))
    for _family, modes in _starts(spec, k, slots):
        for v in variants:
            model = eng.model(v, ())
            if model is not None:
                starts.append((model, eng._start(model, modes, None)))
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
    topology_changed = k != spec.n0 or slots != spec.incoming
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
    P, U, unary = {}, {}, 0.0
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
    rel = _Labelled(spec.ctx, spec.fs, [ramp] * k, continuous=True, structures=spec.structures)
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
    for m, (ai, gi, ci) in enumerate(model.modes):
        e, g, h = float(theta[ai]), float(theta[gi]), float(theta[ci])
        cond, relsig = sd.get(ai), rel_sd.get(ai)
        found = [x for x in (cond, relsig) if x is not None]
        source = ("max" if len(found) == 2 else "conditional" if cond is not None
                  else "release" if relsig is not None else "unavailable")
        # the fast pass's own parameter diagnostics on the release fit: a usable covariance, an energy sd
        # below the flat-prior width (else "undetermined") and off its bounds (else "boundary_limited");
        # corr(omega0, Gamma) is the regime rule, not identifiability, so it does not enter here
        release_ok = (relsig is not None and relsig < spec.ell / math.sqrt(12.0)
                      and ai not in rel_railed)
        # only a slot with a neighbour factor can be held by neighbours; an unmatched one is just poorly pinned
        mstatus = ("poorly_determined" if cond is None else "determined_by_own_data" if release_ok
                   else "held_by_neighbours" if by_branch.get(slots[m]) else "poorly_determined")
        modes.append(HeldMode(branch=ids[m], omega0=e, gamma=g, height=h, guard=max(found) if found else None,
                              guard_source=source, status=mstatus,
                              regime=_regime(spec.fs, spec.fwhm, e, g, corr.get(m)), pinned=ai in model.fixed_pos))
        energies[ids[m]] = e
    fast = snapshot.fast or spec.own                 # what the changes are measured from (amendment 16, item 4)
    fast_by = _fast_modes(fast, ids, [md.omega0 for md in modes], exact_only=snapshot.fast is None)
    delta_fast = {"count": k - len(fast.modes), "omega0": {b: energies[b] - fast_by[b].omega0 for b in ids if b in fast_by}}

    flag = _flags(snapshot, spec, a, status, slots, ids, model, theta, modes, fast_by, F, R, cov, notes, dropped)
    why = _why(status, reason, spec.n0, k, slots, spec.incoming, J_old, J)
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
    whatever the score); the four note codes never set ``flagged``."""
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
    for b in spec.pool:
        cs = [c for c in spec.pred.get(b, ()) if spec.lo <= c <= spec.hi and spec.d_edge(c) >= spec.band]
        if cs and b not in final:
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


def _why(status, reason, n0, k, slots, incoming, J_old, J) -> str:
    if status == "accepted" and k != n0:
        return f"The neighbours' evidence changed the number of modes from {n0} to {k}."
    if status == "accepted" and slots != incoming:
        return "The neighbours' evidence changed which branch a mode belongs to."
    if status == "accepted":
        gain = "" if J_old is None else f" by {J_old - J:.3g}"
        return f"Fitting with the neighbours lowered the shared objective{gain}, so the modes moved."
    if status == "unchanged":
        return "The neighbours left this fit unchanged."
    words = {"no_supported_improvement": "the neighbours' evidence did not support the change",
             "material_change_without_gain": "it moved the fit without lowering the shared objective",
             "hard_constraint": "it broke a fixed value or left the energy window"}
    return f"The refit was not kept: {words.get(reason, reason)}."


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
                    "delta_modes": list(delta), "free": [cmodel.names[i] for i in cmodel.free],
                    "cost": D, "evidence_status": "conditional", "probability": p_k,
                    "params": params, "map_params": params}],
        "peaks": peaks,
        "elastic_height": None if cmodel.el is None else float(ctheta[cmodel.el]),
        "background": None if cmodel.bg is None else float(ctheta[cmodel.bg]),
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
        step = abs(float(link.step)) / s.reference_T_K
        base, d = ((s.t_tolerance_meV, step) if s.t_spacing_form == "divide"
                   else (s.t_tolerance_meV * step ** s.step_exponent, 0.0))
        w, kind = 0.0, "dT"
    else:
        base, d, w, kind = s.q_change_scale_meV, 0.0, s.q_tolerance_meV, "dq"
    first, second = (a, b) if a.unit == link.a else (b, a)
    ga = {m.branch: m for m in first.modes}
    gb = {m.branch: m for m in second.modes}
    pairs = []
    for br in sorted(set(ga) & set(gb)):
        # an unavailable guard widens by the spec's fallback ell/sqrt(12) (that spectrum's window), never 0
        sa, sb = (m.guard if m.guard is not None else _ell(r) / math.sqrt(12.0)
                  for m, r in ((ga[br], first), (gb[br], second)))
        f = cc.Factor(f"seam:{link.a}|{link.b}:{br}", br, (link.a, link.b), (1.0, -1.0),
                      s=math.sqrt(base * base + sa * sa + sb * sb), C=s.penalty_cap, kind=kind, w=w, d=d,
                      family=s.shape_family)
        u = ga[br].omega0 - gb[br].omega0
        pairs.append({"branch": br, "dE": u, "P": cc.potential(f, u), "U": cc.mismatch(f, u),
                      "guard_unavailable": ga[br].guard is None or gb[br].guard is None})
    max_u = max((p["U"] for p in pairs), default=0.0)
    return {"link": [link.a, link.b], "kind": link.kind, "pairs": pairs, "max_U": max_u,
            "threshold": s.flag_threshold, "over": max_u > s.flag_threshold,
            "only_one_side": sorted(set(ga) ^ set(gb))}

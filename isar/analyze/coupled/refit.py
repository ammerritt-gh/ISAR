"""The coupled refit (C-refit): one spectrum's two-stage refit against a frozen neighbour snapshot.

Stage 1 (``open_search``, ``price``; ``assign`` drives the search inline, ``search.drive``) compares counts
and branch associations by evidence under one normalised neighbour density per matched mode (the
hand-over ``engine``), with the unmatched entry price kept inside the odds and the loss price of each neighbour branch a candidate
does not name (the entry price's mirror, charged once per spectrum outside the normalised densities). Stage 2 (``refit(stage=
"continuous")``, or its three pool steps ``refit_step``: fit, close-pair restarts, finish) refines the continuous parameters under the one shared objective J with the stage's
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
engine's model, which this module never changes. No I/O, no pool, no Qt: given a task port (``tasks.Port``),
``price`` sends its inner work to the caller's pool, and ``run_task`` is that work's entry in a pool worker.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from collections.abc import Iterable, Mapping
from dataclasses import asdict, astuple, dataclass, replace
from typing import TYPE_CHECKING, Literal

import numpy as np
from scipy.integrate import quad
from scipy.special import logsumexp

from isar.analyze import fast_pass, loop, prior
from isar.analyze.components import (QE_WEIGHT_CONVENTION, CentralQE, ConstantBackground, Elastic, PhononDHO,
                                     QuasiElastic)
from isar.analyze.coupled import engine as cc
from isar.analyze.coupled import rules
from isar.analyze.coupled import tasks as _tasks
from isar.analyze.coupled.identity import _carried_ids, new_branch_id
from isar.analyze.coupled.settings import InterSettings
from isar.analyze.coupled.types import (SEVERITY_OF, Assignment, Dormant, Held, HeldMode, Priced, PricingJob,
                                        RefitResult, Snapshot)
from isar.analyze.model_manifest import MANIFEST_SCHEMA

if TYPE_CHECKING:
    from isar.analyze.coupled.engine import Factor
    from isar.analyze.coupled.search import Search
    from isar.session.inter_spectra.graph import Link

log = logging.getLogger(__name__)

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
    twin refit of a crossed basin would then hand one branch's density to another mode.

    Task mode (``price(..., tasks=port)``): the three heavy primitives, a start's fit, the posterior optimiser and
    the Laplace evidence, each a function of its exact key alone, go through ``_prim``. A pool worker runs a whole
    sub-computation with ``record`` on and returns the primitive outcomes it met; the job's thread replays today's
    code over its one ledger, answering each primitive from ``memo`` and sending a miss to the pool. A split
    integral's nodes are tasks of their own (``send_split``, ``split_evidence``). Without a port and a record, every
    primitive is computed in place: today's path."""

    port = None                             # the job's task port (replay side); None: compute in place
    record = None                           # {key: outcome} of the primitives a worker task met; None: not recording
    # evidence_assignment's switch: every basin by the base bounded Laplace (no split integral), no extra delta fits
    laplace_only = False

    def fit(self, variant, modes, family, linear=None):
        if variant is None:
            return None
        model = self.model(variant, tuple(i for i, m in enumerate(modes) if m[3]))
        if model is None:
            return None
        key = ("fit", _vkey(variant), tuple(tuple(m) for m in modes),
               tuple(sorted(linear.items())) if linear else None)
        out = self._prim(key, lambda: self._fit_core(model, modes, linear, family))
        if self.port is not None:
            self.n_fits += 1                # the start's one TRF ran in a worker
        if out is None:
            return None
        theta, cost, nfev, message = out
        if self.port is not None:
            theta = theta.copy()            # a memo outcome is shared; the ledger owns its own vector
        return self._place(model, theta, cost, family, nfev, message), cost

    def _posterior(self, model, theta):
        key = ("post", _vkey(model.variant), tuple(model.delta), theta.tobytes(), self.gradients, self.fd_posterior,
               self.posterior_jac)
        compute = super()._posterior
        return self._prim(key, lambda: compute(model, theta))

    def laplace(self, b, f, z, lo, hi, rel_step=1e-4):
        # f is objective(b.model, b.theta) on the price path, so the key need not carry it; fd_posterior decides
        # whether the curvature takes the joint gradient
        key = ("lap", _vkey(b.model.variant), tuple(b.model.delta), b.theta.tobytes(),
               np.asarray(z, dtype=float).tobytes(), rel_step, self.gradients, self.fd_posterior)
        compute = super().laplace
        return self._prim(key, lambda: compute(b, f, z, lo, hi, rel_step))

    def _prim(self, key, compute):
        if self.record is not None:
            value = compute()
            self.record.setdefault(key, value)
            return value
        if self.port is None:
            return compute()
        if key not in self.memo:            # a replay miss: one primitive task, waited for; never computed here
            h = self.port.submit((self.task_ctx, "prim", (key,)))
            self.handles.append(h)
            self.memo[key] = self.port.wait(h)
        return self.memo[key]

    # -- task mode
    def open_port(self, port, ctx: bytes):
        self.port, self.task_ctx, self.memo, self.handles = port, ctx, {}, []
        self.split_sent, self.splits = {}, {}   # integral key -> {f: node handle} not yet read; -> its SplitResult

    def submit(self, kind, args):
        """Queue one speculative task of this job; None without a port."""
        if self.port is None:
            return None
        h = self.port.submit((self.task_ctx, kind, args))
        self.handles.append(h)
        return h

    def absorb(self, h):
        """Wait for a speculative task and keep its primitive outcomes. A task that failed is logged and ignored:
        replay computes what it really needs and raises there if the step truly fails. ``Abandoned`` (a
        BaseException) always propagates."""
        if h is None:
            return
        try:
            got = self.port.wait(h)
        except Exception:
            log.warning("coupled refit: a speculative task failed; the replay prices without it", exc_info=True)
            return
        for k, v in got.items():
            self.memo.setdefault(k, v)

    def speculate_wave(self, pending) -> dict:
        """One ``"basin"`` task per pending basin with no hidden pair, {id(basin): handle}; {} without a port. A basin
        with one hidden pair whose pair point no other split basin of its model holds has its split integral's node
        tasks sent (``send_split``); its price, and so its integral's reading, runs in the replay. A basin with more
        than one pair gets no task: its price computes nothing."""
        out = {}
        if self.port is None:
            return out
        for b in pending:
            pairs = self.hidden_pairs(b.model, b.theta)
            if not pairs:
                out[id(b)] = self.submit("basin", (_vkey(b.model.variant), tuple(b.model.delta), b.index,
                                                   b.theta.copy(), b.cost, b.ln_like, b.ln_model_prior, b.family,
                                                   b.nfev, b.message))
            elif len(pairs) == 1 and not self._seen_by_another(b, pairs[0]):
                self.send_split(b.model, self._anchor(b.model, b.theta), pairs[0], cc.SPLIT_NODE_SETS)
        return out

    def _seen_by_another(self, b, pair) -> bool:
        """``_seen_split`` of the basin's pair point without its own entry, which its reprice pops."""
        done = self._split_done.get(b.model, {})
        own = done.pop(b.index, None)
        try:
            return self._seen_split(b.model, self._pair_point(b.model, b.theta, pair))
        finally:
            if own is not None:
                done[b.index] = own

    def end_wave(self, wave):
        """Drop a wave's unread tasks: basins merged or settled before their turn, integrals never read."""
        if self.port is None:
            return
        stale = list(wave.values())
        for sent in self.split_sent.values():
            stale.extend(sent.values())
        self.split_sent.clear()
        if stale:
            self.port.drop(stale)

    @staticmethod
    def _split_key(model, theta, pair, sets) -> tuple:
        return _vkey(model.variant), tuple(model.delta), theta.tobytes(), tuple(pair), tuple(sets)

    def send_split(self, model, theta, pair, sets) -> tuple:
        """Every node task of one split integral, once per key: chain order, sets in order, the first two sets
        at rank 0 (every agreement test reads them) and deeper ones at rank 1. A
        node's ``prepare`` starts it from the nearest node of this integral already solved when it is sent, else
        the joint solution (D1). Returns the integral's key."""
        key = self._split_key(model, theta, pair, sets)
        if key in self.split_sent or key in self.splits:
            return key
        f_joint = self.split_problem(model, theta, pair)[2]
        sent = self.split_sent[key] = {}
        args = (_vkey(model.variant), tuple(model.delta), theta.copy(), tuple(pair))
        port = self.port

        def prepare(payload):
            ctx, kind, a = payload
            f, best = a[4], None
            for g, h in tuple(sent.items()):     # one C-level copy: the job thread may still be adding nodes
                if g == f:
                    continue
                done, got = port.peek(h)
                if done and not isinstance(got, BaseException) and got[0][0] == "ok" \
                        and (best is None or abs(g - f) < abs(best[0] - f)):
                    best = (g, got[0][2])
            return ctx, kind, a[:5] + (best,)

        for n_set, n in enumerate(sets):
            fs, _w, chains = cc.split_set(n, f_joint)
            for i in chains[0] + chains[1]:
                f = float(fs[i])
                if f in sent:
                    continue
                h = port.submit((self.task_ctx, "node", args + (f, None)), prepare, 0 if n_set < 2 else 1)
                self.handles.append(h)
                sent[f] = h
        return key

    def split_evidence(self, b, theta, pair, *, sets=cc.SPLIT_NODE_SETS):
        """Without a port: today's serial integral. With one (D1): the nodes come from tasks (``send_split``) and
        ``integrate_split`` reads them in today's order. A node that failed is retried once from today's serial
        start (its solved neighbour) when its task started elsewhere; still failing, one ``"split_serial"`` task
        reruns the whole integral serially and its result is final. ``n_split_nodes`` counts each node read and
        each retry. An integral's result is kept per key, so the ABNORMAL retry's re-entry reads the same one."""
        if self.port is None:
            return super().split_evidence(b, theta, pair, sets=sets)
        model = b.model
        key = self.send_split(model, theta, pair, sets)
        if key in self.splits:
            return self.splits[key]
        sent = self.split_sent.pop(key)
        problem = self.split_problem(model, theta, pair)
        args = (_vkey(model.variant), tuple(model.delta), np.array(theta, dtype=float), tuple(pair))
        solved, lnz_at, read = {}, {}, set()

        def node(f, near):
            self.n_split_nodes += 1
            read.add(f)
            out, f0 = self._node_result(sent[f])
            if out[0] != "ok":
                serial = near if near is not None else min(solved, key=lambda g: abs(g - f), default=None)
                if serial is None or serial == f0:
                    raise _SplitFallback
                self.n_split_nodes += 1
                h = self.port.submit((self.task_ctx, "node", args + (f, (serial, solved[serial]))))
                self.handles.append(h)
                out, _f0 = self._node_result(h)
                if out[0] != "ok":
                    raise _SplitFallback
            solved[f], lnz_at[f] = out[2], out[1]
            return out[1]

        try:
            res = cc.split_representative(cc.integrate_split(node, problem[2], sets=sets), problem, solved, lnz_at)
        except _SplitFallback:
            res = None
        finally:
            self.port.drop([h for f, h in sent.items() if f not in read])
        if res is None:                         # only the serial integral can promise what the serial path finds
            h = self.port.submit((self.task_ctx, "split_serial", args + (tuple(sets),)))
            self.handles.append(h)
            res = self.port.wait(h)
            self.n_split_nodes += res.nodes
        self.splits[key] = res
        return res

    def _node_result(self, h):
        """A node task's (outcome, start f); a task that raised is a failed node from the joint start."""
        try:
            return self.port.wait(h)
        except Exception:
            log.warning("coupled refit: a split node task failed; treated as a failed node", exc_info=True)
            return ("fail", "the node task failed"), None

    def split_problem_of(self, vk, delta, theta, pair):
        """A worker's ``split_problem`` for one integral, kept per integral (a pure function of its inputs)."""
        memo = vars(self).setdefault("split_problems", {})
        key = (vk, delta, theta.tobytes(), pair)
        if key not in memo:
            memo[key] = self.split_problem(self.model(self.variant_of(vk), delta), theta, pair)
        return memo[key]

    def close_port(self):
        if self.port is not None:
            self.port.drop(self.handles)

    def variant_of(self, vk):
        k, label, elastic, background, qe, on_demand = vk
        if on_demand:
            return self.variant(k, elastic, background, qe)
        return next(v for v in self.variants[k] if v.label == label)

    def reset_ledger(self):
        """A worker task's clean ledger. The unit-column cache, the reduced variants and the QE start are pure or
        init-time state and stay."""
        self.models.clear()
        self._split_done.clear()
        self.fd_posterior, self.posterior_jac = False, None
        self.s, self.n_fits, self.n_split_nodes = self._s0, 0, 0
        self.record = {}

    def run_prim(self, key):
        """One primitive from its key alone, computed here."""
        self.record = None
        kind, variant = key[0], self.variant_of(key[1])
        if kind == "fit":
            modes, linear = list(key[2]), dict(key[3]) if key[3] else None
            model = self.model(variant, tuple(i for i, m in enumerate(modes) if m[3]))
            return self._fit_core(model, modes, linear, "replay")
        model = self.model(variant, key[2])
        theta = np.frombuffer(key[3], dtype=float).copy()
        if kind == "post":
            self.fd_posterior, self.posterior_jac = key[5], key[6]
            return self._posterior(model, theta)
        if kind == "lap":
            self.fd_posterior = key[7]
            f, lo, hi = self.objective(model, theta)
            return self.laplace(_BasinPoint(model, theta), f, np.frombuffer(key[4], dtype=float).copy(), lo, hi,
                                key[5])
        raise ValueError(f"unknown primitive {kind!r}")

    def _fit_core(self, model, modes, linear, family):
        """One start's TRF (always exactly one ``_least_squares`` call, so one ``n_fits``): (theta, cost, nfev,
        message) at its optimum, or None when the TRF failed or the cost is not finite. ``family`` labels the fit's
        statistics only."""
        theta0 = self._start(model, modes, linear)
        res = self._trf(model, theta0, family)
        if res is None:
            return None
        theta = self.zmap(model).to_theta(res.x, theta0)
        cost = self.cost(model, theta)
        if not math.isfinite(cost):
            return None
        return theta, cost, int(res.nfev), str(res.message)

    def price(self, b):
        """The coupled price, except that a posterior optimiser that stops ABNORMAL is retried once with a 3-point
        gradient (the 2-point one cannot finish on some resolution-limited models). The reason is reset first, so a
        stale 'ABNORMAL' never survives on a priced basin; a basin that still cannot converge stays no_evidence.
        The retry's posterior is the finite-difference one whatever ``gradients`` says: ``objective_and_gradient``
        returns None for that call, so the base engine passes its ``posterior_jac`` (a split integral keeps its
        gradient, so a not-converged split reruns to the same nodes). Both are engine attributes, never module
        state: job threads price concurrently."""
        base = self._laplace_price if self.laplace_only else super().price
        base(b)
        if b.status != "no_evidence" or "ABNORMAL" not in b.reason:
            return
        b.status, b.reason = "pending", ""
        self.fd_posterior, self.posterior_jac = True, "3-point"
        try:
            base(b)
        finally:
            self.fd_posterior, self.posterior_jac = False, None

    def _laplace_price(self, b):
        """The coupled price's own path for a basin without a hidden pair, taken for every basin: where the counts
        leave a close pair's intensity trade flat the posterior optimiser or the Hessian fails (no answer)."""
        fast_pass._Engine.price(self, b)
        self._relabel(b)

    def _near_zero(self, b):
        if not self.laplace_only:
            super()._near_zero(b)

    def _delta_proposal(self, model, point):
        if not self.laplace_only:
            super()._delta_proposal(model, point)

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


class _SplitFallback(Exception):
    """A split node failed after its retry (or with none possible): the parallel reading of that integral stops."""


def _vkey(v) -> tuple:
    """A starting or reduced variant's identity, from which a worker engine finds its own (``variant_of``)."""
    return v.k, v.label, v.elastic, v.background, v.qe, v.on_demand


@dataclass
class _BasinPoint:                          # the basin fields ``laplace`` reads
    model: object
    theta: np.ndarray


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
                  branch_ids: tuple[str, ...] | None = None, dormant: tuple[Dormant, ...] = (),
                  window: tuple[float, float] | None = None) -> Held:
    """A stored fit (fast-pass record shape) as a neighbour or incoming state reads it.

    The guard of each energy (spec 1): for the fast solo its own covariance sigma; for a refit the
    release sigma stored with it, or its maximum with the conditional sigma. ``None`` (source
    "unavailable") without a release sigma, since a conditional sigma alone is precision borrowed from
    the neighbours: a factor that uses it falls back to ``ell/sqrt(12)`` and notes
    ``neighbour_precision_unavailable``. Branch ids: ``branch_ids``, else those stored on the peaks, else
    ``fast:<unit>#<i>`` for the fast solo (``<fit>#<i>`` for a refit). A mode is pinned only by an
    explicit user energy pin (its energy is not a free coordinate of the stored model). ``window`` given: the
    energy window is taken from it and ``context`` is not read (it may be None)."""
    if window is None:
        ctx, fs, _structures = _parts(context)
        sp = prior.resolve_prior_for(ctx, fs.prior or prior.PriorSettings())
        window = (float(sp.e_lo), float(sp.e_hi))
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
    return Held(unit=unit, fit=fit, inputs=inputs, window=(float(window[0]), float(window[1])), modes=tuple(modes),
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
        L = snap.links
        self.link_pred = {} if L is None else {self.canon(p.branch): p for p in L.predictions}
        # a settled link lifts the width ceiling; a candidate's applied add or split unlifts its own (slot_lifted)
        self.settled = set() if L is None else {self.canon(b) for b in L.settled_branches}
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
        links = None
        if snap.links is not None:   # the settled set is written sorted: a frozenset follows hash order, which differs per process
            links = asdict(snap.links)      # the listing order of changes and ties is the linking step's, not part of the price
            links.update(settled_branches=sorted(snap.links.settled_branches),
                         changes=sorted(map(repr, links["changes"])), ties=sorted(map(repr, links["ties"])))
        return {"unit": snap.unit, "own": held(snap.own), "batch_kind": snap.batch_kind, "links": links,
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
            coverage={"counts_priced": [], "counts_unresolved": {}, "stopped": {},
                      "discrete_skipped": "no_proposed_change"},
            search_limited=True, changed=False, reasons=())

    # -- the slot densities of one composition
    def held_value(self, unit: str, branch: str) -> float:
        h = self.held_all.get(unit)
        md = None if h is None else next((m for m in h.modes if self.canon(m.branch) == self.canon(branch)), None)
        if md is None:
            raise KeyError(f"{unit} carries no branch {branch} in this snapshot")
        return md.omega0

    def slot_lifted(self, b: str | None, unlifted) -> bool:
        """Whether a slot naming ``b`` has the lifted width ceiling: a branch whose mode here is on a settled link,
        unless the candidate's own applied add or split produces it (``unlifted``, canonical): that slot is new and
        keeps the lone cap. Every other slot of the candidate, the incoming fit's included, keeps the lift."""
        return b is not None and self.canon(b) in self.settled and self.canon(b) not in unlifted

    def coupling(self, b: str) -> cc.SlotCoupling | None:
        """The width and height coupling of a slot naming ``b``: its link prediction, in this snapshot's settings. The
        height's centre is the prediction's intrinsic intensity (0: unknown, no height coupling), at this spectrum's T."""
        p = self.link_pred.get(self.canon(b))
        st, tau = self.snap.settings, self.snap.settings.width_tolerance_ln
        if p is None or not p.w > 0 or not tau > 0 or self.fs.prior is not None and self.fs.prior.legacy_v4:
            return None
        tau_a = st.intensity_tolerance_ln
        a_star = float(p.A) if math.isfinite(p.A) and p.A > 0 and tau_a > 0 else 0.0
        return cc.SlotCoupling(p.w, self.fwhm, tau, st.penalty_cap, (self.fs.prior or prior.PriorSettings()).width_knee,
                               self.sp.g_lo, a_star=a_star, tau_a=tau_a, T=self.own.T)

    def slot_objects(self, slots, factors=None):
        """One MatchedDensity per matched slot (``factors``: the frozen definitions to use, by branch;
        default this snapshot's; the width coupling from the link prediction), one Entry per unmatched slot;
        neighbour values from this snapshot."""
        out = []
        for b in slots:
            if b is None:
                out.append(self.entry)
                continue
            fs_ = list(self.factors.get(b, []) if factors is None else factors.get(b, []))
            frozen = {k: self.held_value(k, b) for f in fs_ for k in f.members if k != self.snap.unit}
            out.append(cc.MatchedDensity(self.ep, self.snap.unit, fs_, frozen, coupling=self.coupling(b)))
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
    not read); any other free width at the floor still counts. A delta mode's width is pinned, so it is never free."""
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


def _engine_for(spec: _Spec, objs, job: PricingJob, gradients: bool) -> _Labelled:
    """The job's engine, built the same way by ``price`` and by a pool worker (``run_task``)."""
    eng = _Labelled(spec.ctx, _ceiling(spec.fs, job.count), objs, structures=spec.structures,
                    gradients=gradients, lifted=tuple(spec.slot_lifted(b, job.unlifted) for b in job.slots))
    eng.job = job
    return eng


def _worker_engine(snapshot: Snapshot, job: PricingJob, gradients: bool) -> _Labelled:
    spec = _Spec(snapshot)
    return _engine_for(spec, spec.slot_objects(tuple(job.slots)), job, gradients)


def _start_modes(st) -> list:
    return [(e, g, h, False) for e, g, h in st.modes]


def _hint(job: PricingJob):
    return next((st for st in job.starts if st.family == "neighbour"), job.starts[0])


def run_task(ctx: bytes, kind: str, args: tuple) -> object:
    """A pool worker's entry (C-core): one task of one job's price, run to completion on this worker's engine for
    the job (``tasks.worker_engine``) with a clean ledger. ``"fit"`` (variant index, start index), ``"seed"`` and
    ``"basin"`` run that piece of today's serial price with the recorder on and return {primitive key: outcome};
    ``"prim"`` returns one primitive's outcome; ``"node"`` one split node from the given start (or the joint one),
    (``_split_node`` outcome, start f); ``"split_serial"`` one whole split integral by today's serial path, its
    ``SplitResult``. Plain picklable values only."""
    eng = _tasks.worker_engine(ctx, _worker_engine)
    eng.reset_ledger()
    job, k = eng.job, eng.job.count
    if kind == "fit":
        vi, si = args
        st = job.starts[si]
        eng.fit(eng.variants[k][vi], _start_modes(st), st.family)
    elif kind == "seed":
        hint = _hint(job)
        eng.seed_hidden(k, [(hint.family, _start_modes(hint))])
    elif kind == "basin":
        vk, delta, index, theta, cost, ln_like, ln_model_prior, family, nfev, message = args
        model = eng.model(eng.variant_of(vk), delta)
        b = fast_pass._Basin(model, index, theta, cost, ln_like, ln_model_prior, family, nfev, message)
        model.basins.append(b)
        eng.price(b)
    elif kind == "prim":
        return eng.run_prim(args[0])
    elif kind == "node":
        vk, delta, theta, pair, f, start = args
        problem = eng.split_problem_of(vk, delta, theta, pair)
        starts = [problem[1]] if start is None else [start[1], problem[1]]
        return eng._split_node(problem, f, starts), None if start is None else start[0]
    elif kind == "split_serial":
        vk, delta, theta, pair, sets = args
        model = eng.model(eng.variant_of(vk), delta)
        return eng.split_evidence(_BasinPoint(model, theta), theta, pair, sets=sets)
    else:
        raise ValueError(f"unknown task kind {kind!r}")
    return eng.record


def price(snapshot: Snapshot, job: PricingJob, *, gradients: bool = True,
          tasks: _tasks.Port | None = None) -> Priced:
    """One labelling at one count from the job's starts: every start in every admissible variant (the base
    engine's TRF, twin resolution and delta alternatives, plus the constructed hidden start of ``seed_hidden``),
    each distinct basin priced by Laplace under the coupled prior, or for a basin with a hidden pair by the
    integral over the split (engine.CoupledEngine.price); ln Z sums the basins' masses with their variant/delta
    weights, then adds the labelling's composition weight (``rules.ln_w_label``) and, for a pinned unmatched
    energy, its entry charge outside the integral (spec 2). Every basin term also carries the candidate's loss
    price (``_Spec.loss_terms``), once. The engine's ceiling is raised to the job's count. ``gradients``: the
    posterior and split-node optimisers and their Laplace curvature run on the joint objective and gradient
    (``CoupledEngine.objective_and_gradient``); False prices with finite differences, as before that existed.
    ``tasks``: a task port (C-port). The fits, the seed and each wave's basins without a hidden pair go to its pool
    as tasks and this thread replays today's code from their outcomes, so the price equals the serial one in every
    field but ``seconds`` for a job without a split basin; a split integral's nodes are tasks too, every node set
    sent at once and read in today's order, so a split basin's ln Z and point may shift within D1. None: today's
    serial path."""
    t0 = time.perf_counter()
    k, slots = job.count, tuple(job.slots)
    if len(slots) != k:
        raise ValueError(f"job {job.key}: {len(slots)} slots for count {k}")
    if not job.starts:
        raise ValueError(f"job {job.key}: no starts (a job is built by the search, with its starts)")
    spec = _Spec(snapshot)
    loss = spec.loss_terms(slots)           # recorded on every return, even a failed one
    objs = spec.slot_objects(slots)
    bad = [b for b, o in zip(slots, objs) if isinstance(o, cc.MatchedDensity) and o.status != "ok"]
    if bad:
        return Priced(job.key, k, slots, None, "no_evidence", None,
                      f"the neighbour density of {bad[0]} has no verified normaliser", 0, time.perf_counter() - t0,
                      loss=loss)
    u = sum(b is None for b in slots)
    eng = _engine_for(spec, objs, job, gradients)
    if not eng.variants.get(k):
        return Priced(job.key, k, slots, None, "no_structure", None,
                      f"the card's structures hold no model with {k} modes", 0, time.perf_counter() - t0,
                      loss=loss)
    sent = {}
    if tasks is not None:
        eng.open_port(tasks, _tasks.pack_context(snapshot, job, gradients))
    try:
        if tasks is not None:
            # every fit and the seed go out at once; the replay below reads them in today's order
            sent = {(vi, si): eng.submit("fit", (vi, si)) for vi in range(len(eng.variants[k]))
                    for si in range(len(job.starts))}
            sent["seed"] = eng.submit("seed", ())
        for vi, v in enumerate(eng.variants[k]):
            for si, st in enumerate(job.starts):
                eng.absorb(sent.get((vi, si)))
                eng.fit(v, _start_modes(st), st.family)
        # S1's constructed hidden start, from the neighbours' energies when the job carries them
        hint = _hint(job)
        eng.absorb(sent.get("seed"))
        eng.seed_hidden(k, [(hint.family, _start_modes(hint))])
        while True:
            pending = [b for b in eng.basins() if b.count == k and b.status == "pending"]
            if not pending:
                break
            wave = eng.speculate_wave(pending)
            for b in pending:
                if b.status == "pending":
                    eng.absorb(wave.pop(id(b), None))
                    eng.price(b)
            eng.end_wave(wave)
    finally:
        eng.close_port()
    basins = [b for b in eng.basins() if b.count == k]
    scored = [b for b in basins if b.status in _PRICED and b.ln_z is not None]
    priced = [b for b in scored if not _free_width_at_floor(b)]
    if not priced:
        twin = bool(basins) and all(b.status == "degenerate_twin" for b in basins)
        und = next((b for b in basins if b.status == "undetermined"), None)
        reason = und.reason if und is not None else _failure_reason(basins, floored=bool(scored))
        status = "degenerate_twin" if twin else "undetermined" if und is not None else "no_evidence"
        return Priced(job.key, k, slots, None, status, None, reason, eng.n_fits, time.perf_counter() - t0,
                      loss=loss)

    def charge(b):
        return _entry_charge(spec.entry, b, slots)
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
    peak = _residual_peak(eng, best, [m[1] for m in records[int(np.argmax(terms))][2]])
    return Priced(job.key, k, slots, ln_z, best.status, mode, None, eng.n_fits, time.perf_counter() - t0,
                  records, loss, peak)


def _entry_charge(entry, b, slots) -> float:
    """The entry charge of each unmatched slot whose energy is pinned, outside the integral (spec 2)."""
    fixed = set(b.model.fixed_pos)
    return sum(rules.pinned_entry_charge(entry, float(b.theta_map[a]))
               for (a, _g, _c), sl in zip(b.model.modes, slots) if sl is None and a in fixed)


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


def fixed_assignment(snapshot: Snapshot, *, apply_changes: bool) -> Assignment:
    """The frozen mode set of one spectrum for one stage, built from the link without pricing: the incoming fit's
    modes with the link's labels and, when ``apply_changes``, the link's listed adds and splits (never its drops).
    Several splits of one mode apply together, the mode becoming one mode per branch they name (no cap). A change that
    meets a mode an earlier one touched, or would name a branch twice, is recorded in ``search["skipped"]``. The
    incoming fit's variant (elastic, background, QE) and its delta modes are kept, an added or split-off mode has a
    free width; ``selected_model`` is the start of that model. Raises ValueError("no incoming model") when the
    incoming record's selected model has another count."""
    from isar.analyze.coupled import search as SE
    spec = _Spec(snapshot)
    variant_flags = spec.inc_variant
    sel = _selected_model(snapshot.own_record)
    if variant_flags is None:
        raise ValueError("no incoming model")
    L = snapshot.links
    usable, skipped = SE.usable_changes(spec, L) if apply_changes and L is not None else ([], [])
    chosen = []
    for x in usable:
        if x[1].kind == "drop":
            continue
        made = SE.compose_rows(spec, L, [*chosen, x], union=True)
        if isinstance(made, str):
            skipped.append({"change": x[0], "id": SE.change_id(x[1]), "why": made})
        else:
            chosen.append(x)
    rows = SE.compose_rows(spec, L, chosen, union=True)
    count, slots = len(rows), tuple(r[0] for r in rows)
    delta = tuple(j for j, r in enumerate(rows) if r[2] is not None and r[2] in spec.inc_delta)
    unlifted = tuple(sorted({spec.canon(b) for _i, c in chosen for b in c.branches}))
    eng = _Labelled(spec.ctx, _ceiling(spec.fs, count), spec.slot_objects(slots), structures=spec.structures,
                    lifted=tuple(spec.slot_lifted(b, unlifted) for b in slots))
    variant = eng.variant(count, *variant_flags)
    model = None if variant is None else eng.model(variant, delta)
    if model is None:
        raise ValueError("no model for the fixed mode set")
    theta = eng._start(model, [(e, g, h, False) for _s, (e, g, h), _o in rows], _record_linear(sel, model))
    v = model.variant
    return Assignment(
        unit=snapshot.unit, snapshot_id=snapshot.stage_id, count=count, slots=slots, origins=spec.origins(slots),
        factors=spec.incident(slots), discrete="fixed", triggers=(), candidates=(), coverage={},
        search_limited=False, changed=count != spec.n0 or slots != spec.incoming, reasons=(),
        selected_model=(v.elastic, v.background, v.qe, tuple(model.delta), tuple(float(x) for x in theta)),
        mask_only=False, unlifted=unlifted,
        search={"version": 1, "domain": "fixed", "link": None if L is None else L.link_id,
                "applied": sorted(i for i, _c in chosen), "skipped": skipped})


def _record_linear(sel: dict, model) -> dict:
    """The incoming record's elastic, background and QE values for ``model``'s start (the keys ``_start`` reads)."""
    p = sel.get("map_params") or sel.get("params") or {}
    return {key: float(p[model.names[pos]]) for key, pos in
            (("elastic", model.el), ("bg", model.bg), ("qe_h", model.qe_h), ("qe_width", model.qe_w))
            if pos is not None and p.get(model.names[pos]) is not None}


@dataclass
class _SetFit:                              # one fitted mode set of evidence_assignment
    count: int
    slots: tuple
    unlifted: tuple
    ln_z: float | None = None               # the set's total (D1); None: no answer
    status: str = "no_evidence"
    why: str | None = None                  # "<status>: <reason>" when there is no answer
    mode: tuple | None = None               # the posterior mode as Assignment.selected_model
    fits: int = 0
    seconds: float = 0.0
    delta_slots: frozenset = frozenset()    # the slots priced as resolution-limited (delta) modes, kept by later candidates


def _fit_set(spec: _Spec, S: list, flags: tuple, sel: dict, *, incumbent: bool,
             carried: frozenset = frozenset()) -> _SetFit:
    """The incoming modes with the changes ``S`` applied (``compose_rows``, union), fitted once from the link's start in
    the incoming variant and width mask, its one basin priced by bounded Laplace (``_Labelled.laplace_only``). An added
    or split-off mode starts with a free width; when the twin screens rail it to the floor, the engine's own delta twin
    is priced instead (D2). Any other twin, a failed fit or a free width at the floor is no answer. ``carried``: the
    slots the incumbent prices as delta, which this candidate keeps as delta, so only the change priced differs."""
    from isar.analyze.coupled import search as SE
    t0 = time.perf_counter()
    rows = SE.compose_rows(spec, spec.snap.links, S, union=True)
    count, slots = len(rows), tuple(r[0] for r in rows)
    new = {j for j, r in enumerate(rows) if r[2] is None}
    delta = tuple(j for j, r in enumerate(rows) if (r[2] is not None and r[2] in spec.inc_delta) or r[0] in carried)
    out = _SetFit(count, slots, tuple(sorted({spec.canon(b) for _i, c in S for b in c.branches})))
    objs = spec.slot_objects(slots)
    eng = _Labelled(spec.ctx, _ceiling(spec.fs, count), objs, structures=spec.structures,
                    lifted=tuple(spec.slot_lifted(b, out.unlifted) for b in slots))
    eng.laplace_only = True
    variant = eng.variant(count, *flags)
    model = None if variant is None else eng.model(variant, delta)
    if model is None:
        if incumbent:
            raise ValueError("no model for the fixed mode set")
        out.status = "no_structure"
        out.why = f"no_structure: the card holds no model with {count} modes in the incoming variant"
        return out
    bad = [b for b, o in zip(slots, objs) if isinstance(o, cc.MatchedDensity) and o.status != "ok"]
    got = None if bad else eng.fit(variant, [(e, g, h, j in delta) for j, (_s, (e, g, h), _o) in enumerate(rows)],
                                   "fixed", _record_linear(sel, model))
    b = None if got is None else got[0]
    while b is not None:
        if b.status == "pending":
            eng.price(b)
        if b.status != "degenerate_twin" or b.twin is None or not b.reason.startswith("width railed"):
            break
        railed = next((j for j, (_a, g, _c) in enumerate(b.model.modes) if b.model.names[g] == b.limit), None)
        if railed not in new:
            break                           # a railed incoming width would change the mask the design keeps
        b = b.twin                          # the engine's own resolution-limited reading of a new mode (D2)
    out.fits, out.seconds = eng.n_fits, time.perf_counter() - t0
    if b is None:
        out.why = (f"no_evidence: the neighbour density of {bad[0]} has no verified normaliser" if bad
                   else "no_evidence: no start converged")
        return out
    out.status = b.status
    if b.status not in _PRICED or b.ln_z is None or not math.isfinite(b.ln_z):
        out.why = f"{b.status}: {b.reason or 'no evidence'}"
        return out
    if _free_width_at_floor(b):
        out.why = f"{b.status}: a free width is at the floor"
        return out
    u = sum(s is None for s in slots)
    out.ln_z = float(b.ln_z + b.ln_model_prior + _entry_charge(spec.entry, b, slots)
                     + rules.ln_w_label(count, count - u, len(spec.pool)))
    v = b.model.variant
    out.mode = (v.elastic, v.background, v.qe, tuple(b.model.delta), tuple(float(x) for x in b.theta_map))
    out.delta_slots = frozenset(slots[j] for j in b.model.delta if slots[j] is not None)
    return out


def evidence_assignment(snapshot: Snapshot) -> Assignment:
    """The frozen mode set of one spectrum for one stage, each listed add and split entered by evidence (C-priced).

    The incoming fit's modes with the link's labels (the incumbent) are fitted once against the frozen neighbours;
    then each listed add and split, strongest predicted intensity first, is fitted as the incumbent plus that change,
    every continuous parameter reoptimised, and enters when its ln Z beats the incumbent's by more than
    ``evidence_hysteresis_nats``; an entered change is the new incumbent. ln Z is the bounded Laplace evidence of the
    one basin with the priors the pass already has, the model weights (``ln_model_prior``, ``rules.ln_w_label``) and the
    pinned entry charge, each once; no loss price, no split integral, no structural variant (the incoming variant and
    width mask are kept). A change with no answer stays out; an unresolved split is listed in
    ``search["pricing"]["unresolved_splits"]``. An incumbent with no answer keeps the incoming set and nothing is
    tried. Drops are never tried. Raises as ``fixed_assignment`` does."""
    from isar.analyze.coupled import search as SE
    spec = _Spec(snapshot)
    flags = spec.inc_variant
    if flags is None:
        raise ValueError("no incoming model")
    sel = _selected_model(snapshot.own_record)
    L = snapshot.links
    eps = float(snapshot.settings.evidence_hysteresis_nats)
    usable, skipped = SE.usable_changes(spec, L) if L is not None else ([], [])

    def intensity(x):
        ps = (SE._prediction(spec, L, x[1], n) for n in range(len(x[1].branches)))
        return float(sum(p.A for p in ps if p is not None and math.isfinite(p.A) and p.A > 0))

    todo = sorted((x for x in usable if x[1].kind != "drop"), key=lambda x: -intensity(x))
    pricing = {"hysteresis_nats": eps, "incumbent": None, "tried": [], "unresolved_splits": [],
               "final_count": spec.n0, "final_ln_z": None}
    search = {"version": 2, "domain": "evidence", "link": None if L is None else L.link_id, "applied": [],
              "skipped": skipped, "pricing": pricing}
    if not todo:
        return replace(fixed_assignment(snapshot, apply_changes=False), search=search)
    inc = _fit_set(spec, [], flags, sel, incumbent=True)
    pricing["incumbent"] = {"count": inc.count, "ln_z": inc.ln_z, "status": inc.status, "why": inc.why,
                            "fits": inc.fits, "seconds": inc.seconds}
    if inc.ln_z is None:
        skipped += [{"change": i, "id": SE.change_id(c), "why": "incumbent has no answer"} for i, c in todo]
        return replace(fixed_assignment(snapshot, apply_changes=False), search=search)
    chosen = []
    for x in todo:
        i, c = x
        carried = {spec.canon(s) for s in inc.slots if s is not None}
        if all(spec.canon(b) in carried for b in c.branches):
            skipped.append({"change": i, "id": SE.change_id(c), "why": "branch already carried"})
            continue
        made = SE.compose_rows(spec, L, [*chosen, x], union=True)
        if isinstance(made, str):
            skipped.append({"change": i, "id": SE.change_id(c), "why": made})
            continue
        cand = _fit_set(spec, [*chosen, x], flags, sel, incumbent=False, carried=inc.delta_slots)
        d = None if cand.ln_z is None else cand.ln_z - inc.ln_z
        outcome = "no_answer" if d is None else "entered" if d > eps else "kept_out"
        row = {"change": i, "id": SE.change_id(c), "kind": c.kind, "mode": c.mode, "branches": list(c.branches),
               "intensity": intensity(x), "count": cand.count, "ln_z": cand.ln_z, "delta_ln_z": d,
               "outcome": outcome, "why": cand.why if d is None else None, "fits": cand.fits, "seconds": cand.seconds}
        pricing["tried"].append(row)
        if outcome == "entered":
            chosen.append(x)
            inc = cand
        elif outcome == "no_answer" and c.kind == "split":
            pricing["unresolved_splits"].append({k: row[k] for k in ("change", "id", "mode", "branches", "why")})
    pricing["final_count"], pricing["final_ln_z"] = inc.count, inc.ln_z
    search["applied"] = sorted(i for i, _c in chosen)
    return Assignment(
        unit=snapshot.unit, snapshot_id=snapshot.stage_id, count=inc.count, slots=inc.slots,
        origins=spec.origins(inc.slots), factors=spec.incident(inc.slots), discrete="fixed", triggers=(),
        candidates=(), coverage={}, search_limited=False,
        changed=inc.count != spec.n0 or inc.slots != spec.incoming, reasons=(), selected_model=inc.mode,
        mask_only=False, unlifted=inc.unlifted, search=search)


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


def _cov(f, eng, model, theta):
    """Free-coordinate covariance of ``f`` (= objective/2) at theta by the fast pass's Hessian method, taken in the z
    coordinate (the widths as ``t`` on their own range, the fast pass's step floor) and mapped back to meV by the
    delta method; None when not positive definite. The Laplace log evidence of this objective is discarded (spec 6)."""
    if model.free.size == 0:
        return None
    zm = eng.zmap(model)
    cov = prior.laplace_log_evidence(lambda z: f(zm.to_theta(z, theta)), zm.to_z(theta), zm.lo, zm.hi,
                                     step_floor=zm.step_floor(theta) if zm.mapped else None).cov
    return None if cov is None else zm.cov_to_theta(theta, cov)


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
    fs = _fit_stage(snapshot, assignment)
    t0 = time.perf_counter()
    results = [_restart(snapshot, assignment, fs.model_key, x) for x in fs.restarts]
    return _finish(snapshot, assignment, fs, results, time.perf_counter() - t0, priced=priced, hits=hits,
                   timing=timing)


# ---- the continuous stage as three steps (C-refit-steps) -----------------------------------------------------
# the close-pair restarts (C-restarts); algorithm settings, uncalibrated
RESTART_SHARE = 0.5          # (a), (b): h_i += 0.5 h_j and h_j *= 0.5, then the reverse
RESTART_STEP_FWHM = 0.25     # (c): each free energy of the pair moves outward from its mid-energy by 0.25 resolution FWHM
RESTART_NARROW = 0.5         # (d): the half-separation halved


@dataclass(frozen=True)
class FitStage:
    """Step 1 of a refit, handed to its restarts and its finish (picklable). ``theta`` is the best main start's
    solution in the model's layout; ``restarts`` the C-restarts start vectors, in order."""
    unit: str
    snapshot_id: str
    model_key: tuple                        # (elastic, background, qe, delta mask)
    theta: tuple[float, ...]
    J: float
    restarts: tuple[tuple[float, ...], ...]
    failure: str | None                     # "no_finite_candidate" when no main start reached a finite fit
    timing: Mapping[str, float]             # continuous_s
    starts: int = 0                         # the main starts' TRF runs (RefitResult.starts adds restarts and release)


def refit_step(step: str, *args) -> object:
    """``refit(stage="continuous")`` as the runner's pool steps; inline, ``refit`` composes the same three:
    ``("fit", snapshot, assignment)`` -> FitStage; ``("restart", snapshot, assignment, model_key, theta0)`` ->
    (theta, J), (None, inf) on failure; ``("finish", snapshot, assignment, stage, results[, restart_s])`` ->
    RefitResult, ``results`` the restarts' (theta, J) in restart order and ``restart_s`` their summed seconds."""
    steps = {"fit": _fit_stage, "restart": _restart, "finish": _finish}
    if step not in steps:
        raise ValueError(f"unknown refit step {step!r}")
    return steps[step](*args)


def _governs(snapshot, a):
    if a.unit != snapshot.unit or a.snapshot_id != snapshot.stage_id:
        raise ValueError(f"the assignment of {a.unit} on {a.snapshot_id} does not govern "
                         f"{snapshot.unit} on {snapshot.stage_id}")


def _setup(snapshot, a):
    """What every step rebuilds alike from the snapshot and the assignment: (spec, factors by branch, dropped
    factors, notes, slot objects, the labelled continuous engine, the incoming state or None)."""
    _governs(snapshot, a)
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
    eng = _Labelled(spec.ctx, _ceiling(spec.fs, k), objs, continuous=True, structures=spec.structures,
                    lifted=tuple(spec.slot_lifted(b, a.unlifted) for b in slots))
    old = _incoming_state(eng, spec) if k == spec.n0 else None
    return spec, by_branch, dropped, notes, objs, eng, old


def _main_starts(spec, a, eng, old) -> list:
    k, slots = a.count, tuple(a.slots)
    # starts: the incoming state, the fixed assignment's selected model (the link's start, ``a.selected_model``) and one
    # neighbour-placed start, all on the incoming fit's delta mask, which is frozen by design. The priced wording
    # (a delta or free width chosen by the evidence comparison) applies only to the retained search, which no batch runs.
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
    return starts


def _model_of(eng, k: int, key: tuple):
    """The model of a FitStage's ``model_key`` on this engine (the engine caches it: the same object each time)."""
    variant = eng.variant(k, *key[:3])
    return None if variant is None else eng.model(variant, tuple(key[3]))


def _fit_stage(snapshot, a) -> FitStage:
    """Step 1: every main start by TRF on the continuous objective (``_trf``, never ``fit``: no twin screen can merge
    or drop a mode), the lowest J kept, and the restarts of its close pairs."""
    t0 = time.perf_counter()
    spec, _by_branch, _dropped, _notes, _objs, eng, old = _setup(snapshot, a)
    best = None
    for model, theta0 in _main_starts(spec, a, eng, old):
        res = eng._trf(model, theta0, "coupled")
        if res is None:
            continue
        theta = eng.zmap(model).to_theta(res.x, theta0)
        J = eng.J(model, theta)
        if math.isfinite(J) and (best is None or J < best[2]):
            best = (model, theta, J)
    if best is None:
        return FitStage(snapshot.unit, snapshot.snapshot_id, (), (), math.inf, (), "no_finite_candidate",
                        {"continuous_s": time.perf_counter() - t0}, eng.n_fits)
    model, theta, J = best
    v = model.variant
    restarts = tuple(tuple(float(x) for x in t) for t in _restart_starts(eng, model, theta))
    return FitStage(snapshot.unit, snapshot.snapshot_id, (v.elastic, v.background, v.qe, tuple(model.delta)),
                    tuple(float(x) for x in theta), float(J), restarts, None,
                    {"continuous_s": time.perf_counter() - t0}, eng.n_fits)


def _restart_starts(eng, model, theta) -> list[np.ndarray]:
    """C-restarts: per close pair (both heights free), in order: (a) intensity moved from j toward i, (b) the
    reverse, (c) the separation wider, (d) narrower. A pinned coordinate (``model.fixed_pos``) is never moved: with
    one energy pinned (c) and (d) move the free one alone, with both pinned they are not made. Each start is clipped
    into the box, its pins reset to the solution; a start equal to the solution is not made."""
    fixed, free = set(model.fixed_pos), {int(i) for i in model.free}
    zm = eng.zmap(model)
    out = []
    for i, j in eng.close_pairs(model, theta):
        (ai, _gi, ci), (aj, _gj, cj) = model.modes[i], model.modes[j]
        if ci not in free or cj not in free:
            continue
        made = []
        for to, frm in ((ci, cj), (cj, ci)):
            x = theta.copy()
            x[to] += RESTART_SHARE * theta[frm]
            x[frm] *= 1.0 - RESTART_SHARE
            made.append(x)
        moving = [p for p in (ai, aj) if p not in fixed]
        if moving:
            mid = 0.5 * (theta[ai] + theta[aj])
            lower = ai if (theta[ai], i) <= (theta[aj], j) else aj     # coincident: the earlier slot goes down
            wide, narrow = theta.copy(), theta.copy()
            for p in moving:
                wide[p] += (-1.0 if p == lower else 1.0) * RESTART_STEP_FWHM * eng.fwhm
                narrow[p] = mid + RESTART_NARROW * (theta[p] - mid)
            made += [wide, narrow]
        for x in made:
            x = np.clip(x, model.lo, model.hi)
            zm.clip(x)
            x[model.fixed_pos] = theta[model.fixed_pos]
            if not np.array_equal(x, theta):
                out.append(x)
    return out


def _restart(snapshot, a, model_key, theta0) -> tuple[tuple[float, ...] | None, float]:
    """Step 2: one TRF from a restart start on the engine and model step 1 used; (None, inf) on failure."""
    _spec, _by_branch, _dropped, _notes, _objs, eng, _old = _setup(snapshot, a)
    model = _model_of(eng, a.count, model_key)
    if model is None:
        return None, math.inf
    theta0 = np.array(theta0, dtype=float)
    res = eng._trf(model, theta0, "restart")
    if res is None:
        return None, math.inf
    theta = eng.zmap(model).to_theta(res.x, theta0)
    J = eng.J(model, theta)
    return (tuple(float(x) for x in theta), float(J)) if math.isfinite(J) else (None, math.inf)


def _finish(snapshot, a, stage: FitStage, results, restart_s: float = 0.0, *, priced=(), hits=0,
            timing=None) -> RefitResult:
    """Step 3: the lowest J of the main solution and the restarts (a tie keeps the earlier), then acceptance, error
    bars, the release fit, the modes, the flags and the record."""
    timing = {"assign_s": 0.0, "likelihood_s": 0.0, **(timing or {}),
              "continuous_s": float(stage.timing.get("continuous_s", 0.0)), "restart_s": float(restart_s)}
    timing.setdefault("uncertainty_s", 0.0)
    timing.setdefault("release_s", 0.0)
    spec, by_branch, dropped, notes, objs, eng, old = _setup(snapshot, a)
    k, slots = a.count, tuple(a.slots)
    if stage.failure is not None:
        return _failed(snapshot, a, stage.failure,
                       "The refit failed: no start reached a finite fit, so the previous fit stays.",
                       timing, priced, hits, notes)
    model = _model_of(eng, k, stage.model_key)
    if model is None:
        raise ValueError(f"{snapshot.unit}: the fit stage's model {stage.model_key} is not this assignment's")
    theta, J_new = np.array(stage.theta, dtype=float), stage.J
    for t, Jr in results:
        if t is not None and math.isfinite(Jr) and Jr < J_new:
            theta, J_new = np.array(t, dtype=float), Jr

    # a width mask is part of the topology: positions are slot positions, and at the incoming count the slot order
    # is the incoming's ascending order, so the comparison is positional and valid for relabelled slots too.
    # So is a variant (elastic, background, QE) change: the incoming's J is then not comparable
    variant_changed = old is not None and _variant_key(model.variant) != _variant_key(old[0].variant)
    topology_changed = (k != spec.n0 or slots != spec.incoming
                        or tuple(sorted(model.delta)) != spec.inc_delta or variant_changed)
    J_old = eng.J(*old) if old is not None and not topology_changed else None
    fixed_ok = all(abs(theta[p] - v) <= 1e-12 * max(1.0, abs(v)) for p, v in zip(model.fixed_pos, model.fixed_vals))
    in_window = all(spec.lo <= theta[e] <= spec.hi for e, _g, _c in model.modes)
    linked = a.discrete == "fixed"          # the link's mode set: a count or labelling change is the link's
    status, reason = rules.acceptance(
        any_finite=True, evidence_missing_for_change=topology_changed and "incomplete_comparison" in a.reasons,
        hard_violation=not (fixed_ok and in_window), topology_changed=topology_changed, evidence_won=a.changed,
        J_old=J_old, J_new=J_new, materially_moved=_materially_moved(eng, model, theta, old), linked=linked)
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
        if tid.startswith(("entry:", "ramp:", "width:", "area:", "pair:")):
            unary += p
        else:
            P[tid] = p
    for (ai, _g, _c), obj in zip(model.modes, objs):
        if isinstance(obj, cc.MatchedDensity):
            for f in obj.factors:
                U[f.factor_id] = cc.mismatch(f, obj._u(f, float(theta[ai])))

    # conditional covariance from f_cont = J_i / 2 (spec 4, 6)
    t1 = time.perf_counter()
    cov = _cov(lambda t: 0.5 * eng.J(model, t), eng, model, theta)
    sd, corr = _sigmas(model, cov)
    timing["uncertainty_s"] = time.perf_counter() - t1

    # release fit: the same count with no inter-spectra potential and no entry price, own ramp kept (spec 6)
    t2 = time.perf_counter()
    ramp = cc.MatchedDensity(spec.ep, snapshot.unit, [], {})
    rel = _Labelled(spec.ctx, _ceiling(spec.fs, k), [ramp] * k, continuous=True, structures=spec.structures,
                    lifted=tuple(spec.slot_lifted(b, a.unlifted) for b in slots))   # the same ceilings, no coupling
    rel_model = rel.model(model.variant, tuple(model.delta))
    rel_best = None
    for theta0 in [theta] + ([old[1]] if old is not None and old[0] is model else []):
        res = rel._trf(rel_model, theta0, "release")
        if res is None:
            continue
        t = rel.zmap(rel_model).to_theta(res.x, theta0)
        Jr = rel.J(rel_model, t)
        if math.isfinite(Jr) and (rel_best is None or Jr < rel_best[1]):
            rel_best = (t, Jr)
    R = R_signed = None
    rel_sd, rel_railed = {}, set()
    if rel_best is not None:
        t_rel = rel_best[0]
        R_signed = D - rel.cost(rel_model, t_rel)
        R = max(0.0, R_signed)
        rel_sd, _corr = _sigmas(rel_model, _cov(lambda t: 0.5 * rel.J(rel_model, t), rel, rel_model, t_rel))
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

    more = _connection_reasons(snapshot.links)
    more += [_reason("width_at_limit", f"the width of {ids[m]} ended on its {limit}", [ids[m]], limit=limit)
             for m, limit in _width_limits(eng, model, theta)]
    flag = _flags(snapshot, spec, a, status, slots, ids, model, theta, modes, fast_by, F, R, cov, notes, dropped,
                  linked=linked, more=more)
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
        starts=stage.starts + len(results) + rel.n_fits, timing=timing, priced=tuple(priced), cache_hits=hits)


def _width_limits(eng, model, theta) -> list[tuple[int, str]]:
    """(mode, "floor" | "ceiling") of each free, non-delta width that ends on its lower bound (the width floor, the
    railed rule's 1e-6 of span) or on its ceiling (``at_width_cap``'s rule)."""
    zm, free = eng.zmap(model), {int(i) for i in model.free}
    out = []
    for m, (_a, g, _c) in enumerate(model.modes):
        if g not in free or m in model.delta:
            continue
        if theta[g] - model.lo[g] <= 1e-6 * (model.hi[g] - model.lo[g]):
            out.append((m, "floor"))
        elif zm.at_cap(theta, m):
            out.append((m, "ceiling"))
    return out


def _connection_reasons(links) -> list[dict]:
    """One connection_ambiguous reason per near tie the link reports for this spectrum (its tie sides): ids the
    branches the tied labelling moves, values the tie group and its kind."""
    if links is None:
        return []
    out = []
    for t in links.ties:
        moved = sorted({b for x, y in zip(links.labels, t.slots) if x != y for b in (x, y) if b is not None})
        out.append(_reason("connection_ambiguous", f"the link found a near tie ({t.kind}) in connecting "
                           f"{', '.join(moved) or 'these modes'}", moved, group=t.group, kind=t.kind))
    return out


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


def _flags(snapshot, spec, a, status, slots, ids, model, theta, modes, fast_by, F, R, cov, notes, dropped, *,
           linked=False, more=()) -> dict:
    """The flag (N2): F against the threshold, and every own reason of the design's list (they flag
    whatever the score); the four note codes and a neighbour-supported split never set ``flagged``. ``linked``: the
    link set the count, so no reason says the neighbours' evidence changed it; ``more``: reasons made by the caller."""
    s = snapshot.settings
    k = len(slots)
    reasons = list(notes)
    if F is not None and F > s.flag_threshold:
        reasons.append(_reason("score_over_threshold", f"flag score {F:.1f} is over the threshold {s.flag_threshold:g}",
                               [snapshot.unit], F=F, threshold=s.flag_threshold))
    if status == "accepted" and a.changed and k != spec.n0 and not linked:
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
    reasons += list(more)
    flagged = (F is not None and F > s.flag_threshold) or any(r["code"] not in NOTE_CODES for r in reasons)
    return {"score": F, "flagged": bool(flagged), "reasons": reasons}


def _why(status, reason, n0, k, slots, incoming, J_old, J, variant_changed=False) -> str:
    if status == "accepted" and reason == "linked" and k != n0:
        return f"The evidence accepted the link's listed changes: the number of modes went from {n0} to {k}."
    if status == "accepted" and reason == "linked":
        return "The link changed which branch a mode belongs to."
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


def _area(eng, zm, model, theta, cov, src: int):
    """(Stokes area, its delta-method sigma or None) of mode ``src`` as the fast pass computes them
    (``fast_pass._peaks``): ``h`` times ``eng._area_per_h``, the gradient by a finite difference that steps
    inward at a width's ceiling. ``cov`` is the covariance of ``model.free`` (None: no sigma)."""
    ai, gi, ci = model.modes[src]
    e, gam, h = float(theta[ai]), float(theta[gi]), float(theta[ci])
    peak = model.variant.peaks[src]
    per_s, _ = eng._area_per_h(peak, e, gam)
    if cov is None:
        return h * per_s, None
    free = model.free.tolist()
    grad = np.zeros(len(theta))
    grad[ci] = per_s
    for j, val in ((ai, e), (gi, gam)):
        if j not in free:
            continue
        step = 1e-4 * max(1.0, abs(val))
        upper = zm.ceiling(theta, src) if j == gi and zm.mapped_mode(src) else model.hi[j]
        step = -step if val + step > upper else step
        ds, _ = eng._area_per_h(peak, *((e + step, gam) if j == ai else (e, gam + step)))
        grad[j] = h * (ds - per_s) / step
    g = grad[free]
    return h * per_s, float(math.sqrt(max(g @ cov @ g, 0.0)))


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
    zm = eng.zmap(model)
    for dst, src in enumerate(order):
        ai, gi, ci = model.modes[src]
        md = modes[src]
        area, sigma_area = _area(eng, zm, model, theta, cov, src)
        peaks.append({"index": dst, "omega0": md.omega0, "Gamma": md.gamma, "delta": src in model.delta,
                      "height": md.height, "area_stokes": area, "sigma_area": sigma_area,
                      "sigma_omega0": sd.get(ai), "sigma_Gamma": sd.get(gi),
                      "sigma_height": sd.get(ci), "sigma_release": rel_sd.get(ai), "regime": md.regime,
                      "mode_status": md.status, "branch": ids[src], "confident": md.status != "poorly_determined",
                      "at_width_cap": zm.at_cap(theta, src)})
    dec = a.coverage.get("decision") or {}
    # a fixed mode set compared no count, so no solo count probability is carried into the refit
    cp = {} if a.discrete == "fixed" else dec.get("count_p") or spec.snap.own_record.get("count_probabilities") or {}
    p_k = cp.get(str(a.count))
    label = str(cmodel.hyp.label)
    params = {n: float(v) for n, v in zip(cmodel.names, ctheta)}
    fit = _finite({
        "schema": RECORD_SCHEMA, "source": "inter_refit", "status": status,
        "state": "close_call" if p_k is not None and p_k < spec.snap.settings.confidence else "resolved",
        "exposure_basis": {"source": eng.ctx.exposure_source, "reference": eng.ctx.exposure_reference},
        "energy_support_meV": [spec.lo, spec.hi],
        "preferred_count": a.count, "count_probabilities": dict(cp),
        "count_probabilities_partial": bool(dec.get("partial")), "count_p_range": dec.get("count_p_range"),
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

"""One spectrum's adaptive candidate search: the C-search boundary (design change 5, build contracts 1-8).

The search asks for a few prices, reads them, then asks for the next ones (``next_jobs`` and ``feed``) until
``done``; ``result`` then gives the frozen decision. It starts at the seeds (the incoming fit and each held
neighbour's own fitted modes, every seed priced whatever the margin), steps one count at a time both ways while
the count's accumulated total (every distinct labelling scored there, read after the count's first wave of
labelling moves) stays within the stopping margin of the best count, and moves labellings in ranked waves. Every
returned basin is filed once, under the labelling of its fitted energies, in one ledger (contract 2); a price
with no evidence is an unresolved step, never a low one (contract 5). Nothing here fits: the prices come from
``refit.price`` through the caller. The stop codes of ``Assignment.coverage["stopped"]`` are
``behind_margin:<k>``, ``collapsed:<k>``, ``unresolved:<k>``, ``no_structure:<k>``, ``floor``, ``no_parent``,
``no_step:<k>`` and ``full_search``. ``drive`` is the one inline loop over this protocol.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Callable, Sequence
from itertools import combinations, permutations

import numpy as np
from scipy.optimize import linear_sum_assignment
from scipy.special import logsumexp

from isar.analyze import fast_pass
from isar.analyze.coupled import refit as R
from isar.analyze.coupled import rules
from isar.analyze.coupled.types import Assignment, Priced, PricingJob, Snapshot, Start

MOVE_WAVE = 6            # algorithm setting, uncalibrated: labelling moves of one key priced per wave
SPLIT_UNDER_FWHM = 2.0   # algorithm setting, uncalibrated: an added branch this close (x FWHM) to a mode also starts as its split
SAME_START_FWHM = 0.25   # algorithm setting, uncalibrated: a start this close (x FWHM, every mode) to an attempted one is not priced again
_QUIET = ("degenerate_twin", "no_structure")     # no price, and no reason in the decision (D3)
_PURPOSE = {"seed": 0, "step_down": 1, "step_up": 2, "move": 3, "full": 4}


def _energy_order(k, slots, modes):
    """Slot positions in ascending energy of one start's modes (ties by label): the order its labelling reads in."""
    return tuple(sorted(range(k), key=lambda i: (modes[i][0], str(slots[i]))))


def _labels(k, slots, modes):
    """(labels, energies) of a start or basin, both in ascending energy (ties by label)."""
    order = _energy_order(k, slots, modes)
    return tuple(slots[i] for i in order), tuple(float(modes[i][0]) for i in order)


def _ascending(modes, order):
    """Whether a start's energies never fall when read in a slot order: the fitter's own requirement (engine.py)."""
    return all(modes[a][0] <= modes[b][0] for a, b in zip(order, order[1:]))


def _record_start(rec, k, slots, modes):
    """The incoming record's saved energies at count k as a start: ascending and unlabelled, so laid on the job's slots
    by rank in the energy order of the job's first start (modes), as the record was always read; [] when absent."""
    if rec is None or len(rec) != k:
        return []
    order = _energy_order(k, slots, modes)
    return [Start("record", tuple(rec[order.index(i)] for i in range(k)))]


def _priced(p: Priced) -> bool:
    return p.ln_z is not None and math.isfinite(p.ln_z) and bool(p.basins)


class Search:
    """One spectrum's adaptive candidate search on one frozen snapshot: search, price, resume."""

    def __init__(self, snapshot: Snapshot) -> None:
        self.snap, self.s = snapshot, snapshot.settings
        self.spec = spec = R._Spec(snapshot)
        self.names, self.pool = spec.names, spec.pool        # the assignable pool (D9), one name per canonical branch
        self.M = len(self.pool)
        self.margin = float(self.s.stop_margin_nats)
        self.tol = SAME_START_FWHM * spec.fwhm
        self.content = spec.content_key()
        self.in_window = [b for b in self.pool if spec.canon(b) in spec.expected]
        self.dormant = [b for b in self.pool if b not in self.in_window and b not in spec.factors]
        self.neighbours = {h.unit: h for ti in snapshot.terms for h in ti.held}
        self.attempted: dict[tuple, list] = defaultdict(list)    # (k, labels) -> (split?, energies) started or fitted
        self.meta: dict[str, dict] = {}                           # job key -> purpose, source, new ledger keys
        self.ledger: dict[tuple, dict] = {}                       # (k, labels in energy order) -> basins and origin
        self.outcomes: list[tuple[PricingJob, Priced]] = []
        self.by_count: dict[int, list[Priced]] = defaultdict(list)
        self.fed: set[str] = set()
        self.steps: dict[tuple, dict] = {}                        # (c, d) -> {"walk", "jobs", "judged"}
        self.walks: list[dict] = []
        self.follow: dict[tuple, int] = {}                        # (c, d) -> the walk that continues through it
        self.judged: set[int] = set()
        self.wave1: dict[int, set] = {}                           # count -> keys of the first wave on its best key
        self.keys: dict[tuple, dict] = {}                         # expanded ledger key -> queue, waves, stop
        self.tried = {"jobs": 0, "starts": 0, "fits": 0, "failed": 0, "rounds": 0, "evidence_s": 0.0,
                      "repeated_basins": 0}
        self._result: Assignment | None = None
        self.n0, self.inc = spec.n0, spec.incoming
        seeds = self._seeds()
        self.seeds = [{"source": src, "count": k, "slots": list(slots)}
                      for (k, slots), (_st, sources) in seeds.items() for src in sources]
        self.seed_counts = {k for k, _s in seeds}
        self.full = None
        if self.s.full_search:
            self.pending = self._full_jobs()
        else:
            jobs = [j for (k, slots), (starts, sources) in seeds.items()
                    for j in self._jobs(k, slots, starts, "seed", "seed:" + sources[0])]
            self.pending = self._order(jobs)

    # -- jobs ---------------------------------------------------------------------------------------------------
    def _jobs(self, k, slots, starts, purpose, source):
        """The jobs for the starts not within SAME_START_FWHM of one attempted at their labelling. The fitter reads
        each start in the job's slot order, so a start whose energies do not ascend in it gets a job of its own,
        with its slots in its own energy order (finding aa71ec9548ff1-1)."""
        groups = []                                             # (slot order, starts) of each job
        for st in starts:
            lab, es = _labels(k, slots, st.modes)
            split = st.family == "split"       # a split is not a repeat of its own step: splits dedupe against splits only
            seen = self.attempted[(k, lab)]
            if any(all(abs(a - b) <= self.tol for a, b in zip(es, x)) for s_, x in seen if s_ == split):
                continue
            seen.append((split, es))
            for order, kept in groups:
                if _ascending(st.modes, order):
                    kept.append(st)
                    break
            else:
                groups.append((_energy_order(k, slots, st.modes), [st]))
        jobs = []
        for order, group in groups:
            js = tuple(slots[i] for i in order)
            sts = tuple(Start(st.family, tuple(st.modes[i] for i in order)) for st in group)
            part = json.dumps([k, list(js), [[st.family, [list(m) for m in st.modes]] for st in sts]])
            key = hashlib.sha256((self.content + part).encode("utf-8")).hexdigest()
            self.meta[key] = {"purpose": purpose, "source": source, "new": ()}
            jobs.append(PricingJob(self.snap.unit, self.snap.snapshot_id, k, js, key, sts))
        return jobs

    def _order(self, jobs):
        return tuple(sorted(jobs, key=lambda j: (j.count, _PURPOSE[self.meta[j.key]["purpose"]], str(j.slots), j.key)))

    def _seeds(self):
        """(k, slots) -> ([starts], [sources]): the incoming fit, and each held neighbour's own in-window modes at
        its own fitted energies (one mode per canonical branch, heights left to the optimizer, D5)."""
        spec, seeds = self.spec, {}

        def add(k, slots, start, source):
            starts, sources = seeds.setdefault((k, slots), ([], []))
            if start not in starts:
                starts.append(start)
            sources.append(source)
        add(self.n0, self.inc, Start("own", tuple((m.omega0, m.gamma, m.height) for m in spec.own.modes)), "incoming")
        for h in self.neighbours.values():
            modes, used = [], set()
            for md in h.modes:
                b = self.names.get(spec.canon(md.branch))
                if b is not None and spec.lo <= md.omega0 <= spec.hi and b not in used:
                    modes.append((b, md.omega0, md.gamma))
                    used.add(b)
            add(len(modes), tuple(b for b, _e, _g in modes),
                Start("neighbour", tuple((e, g, None) for _b, e, g in modes)), "neighbour:" + h.unit)
        return seeds

    def _full_jobs(self):
        """D13: every count 0..N and every injective placement of the in-window pool branches on its positions,
        all from one energy set per count; a seed count above N is reported, not priced."""
        spec, N = self.spec, int(self.s.full_search_max_count)
        own = sorted((m.omega0, m.gamma, m.height) for m in spec.own.modes)
        extra = sorted(spec.expected[spec.canon(b)][0] for b in self.in_window) + [0.5 * spec.hi, 0.8 * spec.hi]
        jobs = []
        for k in range(N + 1):
            E = sorted(sorted(own, key=lambda m: -m[2])[:k])
            grid = [spec.lo + (i + 1) / (k + 1) * (spec.hi - spec.lo) for i in range(k)]
            for e in extra + grid:
                if len(E) < k and all(abs(e - m[0]) > 0.5 * spec.fwhm for m in E):
                    E.append((e, fast_pass.LADDER_GAMMA, None))
            E += [(e, fast_pass.LADDER_GAMMA, None) for e in grid[:k - len(E)]]
            start = Start("full", tuple(sorted(E, key=lambda m: m[0])))
            for r in range(min(k, len(self.in_window)) + 1):
                for pos in combinations(range(k), r):
                    for bs in permutations(self.in_window, r):
                        slots = [None] * k
                        for p, b in zip(pos, bs):
                            slots[p] = b
                        jobs += self._jobs(k, tuple(slots), [start], "full", "full")
        untried = sum(k > N for k in self.seed_counts)
        self.full = {"max_count": N, "jobs": len(jobs), "untried": untried, "truncated": untried > 0}
        return self._order(jobs)

    # -- the protocol -------------------------------------------------------------------------------------------
    def next_jobs(self) -> tuple[PricingJob, ...]:
        """Every job to price next, deterministic, distinct keys; () if and only if done. Idempotent without a feed."""
        return self.pending

    def feed(self, priced: Sequence[Priced]) -> None:
        """One Priced per job of the last next_jobs(), in that order; ValueError on a protocol violation."""
        if len(priced) != len(self.pending) or any(p.key != j.key for j, p in zip(self.pending, priced)):
            raise ValueError("feed needs exactly one price per asked job, in the order asked")
        t = self.tried
        t["rounds"] += 1
        for job, p in zip(self.pending, priced):
            self.outcomes.append((job, p))
            self.fed.add(job.key)
            ok = _priced(p)
            t["jobs"] += 1
            t["starts"] += len(job.starts)
            t["fits"] += int(p.starts)
            t["evidence_s"] += float(p.seconds)
            t["failed"] += not ok
            self.by_count[job.count].append(p)
            if ok:
                self.meta[job.key]["new"] = tuple(k for k in (self._file(rec, p, job) for rec in p.basins) if k)
        self.pending = () if self.full is not None else self._order(self._advance())

    @property
    def done(self) -> bool:
        return not self.pending

    def result(self) -> Assignment:
        """The frozen decision; only once done."""
        if not self.done:
            raise RuntimeError("the search is not done")
        if self._result is None:
            self._result = self._decide()
        return self._result

    # -- the ledger ---------------------------------------------------------------------------------------------
    def _file(self, rec, p, job):
        """File one basin under the labelling of its fitted energies (the base engine's closeness rule decides a
        repeat, which adds nothing); returns its key when the key is new."""
        modes = tuple(sorted(rec[2], key=lambda m: (m[1], str(m[0]))))
        rec = (rec[0], rec[1], modes, *rec[3:])
        key = (len(modes), tuple(m[0] for m in modes))
        e = self.ledger.get(key)
        new = e is None
        if new:
            e = self.ledger[key] = {"basins": [], "status": p.status, "source": self.meta[job.key]["source"],
                                    "key": job.key, "peak": p.residual_peak}
        for i, x in enumerate(e["basins"]):
            if R._same_basin(self.spec, x, rec):
                self.tried["repeated_basins"] += 1
                if rec[0] > x[0]:
                    e["basins"][i] = rec
                return None
        if e["basins"] and rec[0] > max(b[0] for b in e["basins"]):
            e["peak"] = p.residual_peak
        e["basins"].append(rec)
        self.attempted[key].append((False, tuple(m[1] for m in modes)))
        return key if new else None

    def _mass(self, key) -> float:
        k, labels = key
        terms = [b[0] for b in self.ledger[key]["basins"]]
        return float(logsumexp(terms)) + rules.ln_w_label(k, sum(b is not None for b in labels), self.M)

    def _totals(self):
        masses = {key: self._mass(key) for key in self.ledger}
        by_k = defaultdict(list)
        for (k, _l), v in masses.items():
            by_k[k].append(v)
        return masses, {k: float(logsumexp(v)) for k, v in by_k.items()}

    def _state(self, k):
        """D3: priced if any job at k priced; else failed, collapsed (every job a lower-count twin) or no_structure."""
        ps = self.by_count.get(k)
        if not ps:
            return None
        if any(_priced(p) for p in ps):
            return "priced"
        if any(p.status not in _QUIET for p in ps):
            return "failed"
        return "collapsed" if any(p.status == "degenerate_twin" for p in ps) else "no_structure"

    def _failure(self, k):
        p = next((p for p in self.by_count[k] if not _priced(p) and p.status not in _QUIET),
                 next(p for p in self.by_count[k] if not _priced(p)))
        return p.status, p.reason or p.status

    @staticmethod
    def _best_key(k, masses):
        keys = [key for key in masses if key[0] == k]
        return max(keys, key=lambda key: (masses[key], str(key))) if keys else None

    def _parent(self, key):
        """(labels, [(e, Gamma, h)] in energy order, swap-free pairs) of a ledger key's best basin. A split basin's
        pair is a placed representative (D18): its two modes carry one set of values and are never swapped (D4)."""
        rec = max(self.ledger[key]["basins"], key=lambda b: b[0])
        modes = [(m[1], m[2], m[3]) for m in rec[2]]
        pairs = set()
        if rec[1].endswith("|split"):
            pairs = {(i, j) for i, j in combinations(range(len(modes)), 2) if modes[i] == modes[j]}
        return key[1], modes, pairs

    # -- the walks ----------------------------------------------------------------------------------------------
    def _advance(self):
        masses, tot = self._totals()
        best = max(tot.values()) if tot else None
        live = {k for k, v in tot.items() if v >= best - self.margin}
        jobs: list[PricingJob] = []
        while True:
            moved = False
            # judge every step whose jobs, and when its target priced the target's first wave, have been fed (D2)
            for (c, d), st in list(self.steps.items()):
                if st["judged"] or not st["jobs"] <= self.fed:
                    continue
                t, walk = c + d, self.walks[st["walk"]]
                state = self._state(t)
                if state == "priced" and not self._first_wave(t, masses, jobs) <= self.fed:
                    continue
                st["judged"], walk["reached"], moved = True, t, True
                self.judged.add(t)
                if state == "priced" and t in live:
                    if (t, d) in self.steps:
                        walk["joined"] = self.steps[(t, d)]["walk"]
                    else:
                        self.follow[(t, d)] = st["walk"]
                elif state == "priced":
                    walk["stopped"], walk["behind_nats"] = f"behind_margin:{t}", best - tot[t]
                else:
                    walk["stopped"] = {None: "no_step", "failed": "unresolved", "collapsed": "collapsed",
                                       "no_structure": "no_structure"}[state] + f":{t}"
                    walk["reason"] = self._failure(t)[1] if state == "failed" else None
            # step from every seed count and every judged live count, both ways, and continue every live walk
            todo = [(c, d) for c in sorted(self.seed_counts | (live & self.judged)) for d in (-1, 1)]
            for c, d in todo + list(self.follow):
                if (c, d) in self.steps:
                    continue
                moved = True
                wid = self.follow.get((c, d))
                if wid is None:
                    wid = len(self.walks)
                    self.walks.append({"origin": c, "direction": "down" if d < 0 else "up", "reached": c,
                                       "stopped": None, "behind_nats": None})
                if c + d < 0 or self._state(c) != "priced":
                    self.walks[wid]["stopped"] = "floor" if c + d < 0 else "no_parent"
                    self.steps[(c, d)] = {"walk": wid, "jobs": set(), "judged": True}
                    continue
                new = self._step_jobs(c, c + d, masses)
                jobs += new
                self.steps[(c, d)] = {"walk": wid, "jobs": {j.key for j in new}, "judged": False}
            if not moved:
                break
        # labelling moves of the live counts (D4): expand every key within the margin of its count's best, then one
        # more wave of each expanded key whose last wave was productive
        for k in sorted(live):
            top = masses[self._best_key(k, masses)]
            for key in sorted((key for key in masses if key[0] == k), key=str):
                if key not in self.keys and masses[key] >= top - self.margin:
                    self._expand(key)
        for key, ks in self.keys.items():
            if ks["stopped"] is not None or key[0] not in live or not all(w <= self.fed for w in ks["waves"]):
                continue
            if ks["waves"] and not self._productive(ks["waves"][-1], masses):
                ks["stopped"] = "wave_behind_margin"
                continue
            jobs += self._wave(key)
        return jobs

    def _productive(self, wave, masses) -> bool:
        """A wave is productive when it created a ledger key within the margin of its count's best."""
        made = [key for j in wave for key in self.meta[j]["new"]]
        return any(masses[key] >= masses[self._best_key(key[0], masses)] - self.margin for key in made)

    def _first_wave(self, t, masses, jobs) -> set:
        """The keys of the first wave of labelling moves on count t's best key, asked now when it has none."""
        if t not in self.wave1:
            key = self._best_key(t, masses)
            if key in self.keys:
                self.wave1[t] = set().union(*self.keys[key]["waves"][:1])
            else:
                self._expand(key)
                new = self._wave(key)
                jobs += new
                self.wave1[t] = {j.key for j in new}
        return self.wave1[t]

    def _step_jobs(self, c, t, masses):
        """The jobs of one count step from the best key at c: going up, an add of each missing in-window branch at
        each neighbour energy of it (with a split start of the nearest mode) and the residual add; going down,
        every removal. Every step job also starts from the incoming record's saved model at t, when it has one."""
        spec = self.spec
        parent = self._best_key(c, masses)
        L, P, _pairs = self._parent(parent)
        rec = spec._record_modes(t)
        out = []
        if t < c:
            for i in range(c):
                slots, modes = L[:i] + L[i + 1:], P[:i] + P[i + 1:]
                out += self._jobs(t, slots, [Start("step", tuple(modes))] + _record_start(rec, t, slots, modes),
                                  "step_down", "step_down")
            return out
        named = {spec.canon(b) for b in L if b is not None}
        for b in self.in_window:
            cb = spec.canon(b)
            if cb in named:
                continue
            seen: list[tuple[float, float]] = []
            for h in self.neighbours.values():
                for md in h.modes:
                    if spec.canon(md.branch) == cb and spec.lo <= md.omega0 <= spec.hi \
                            and all(abs(md.omega0 - e) > self.tol for e, _g in seen):
                        seen.append((md.omega0, md.gamma))
            starts = []
            for eb, gb in seen or [(spec.expected[cb][0], fast_pass.LADDER_GAMMA)]:
                starts.append(Start("step", tuple(P) + ((eb, gb, None),)))
                near = [i for i in range(c) if abs(P[i][0] - eb) <= SPLIT_UNDER_FWHM * spec.fwhm]
                if near:
                    i = min(near, key=lambda i: abs(P[i][0] - eb))
                    split = tuple((e, g, 0.6 * h if n == i else h) for n, (e, g, h) in enumerate(P))
                    starts.append(Start("split", split + ((eb, P[i][1], 0.4 * P[i][2]),)))
            slots = L + (b,)
            out += self._jobs(t, slots, starts + _record_start(rec, t, slots, starts[0].modes), "step_up", "step_up")
        peak = self.ledger[parent]["peak"]
        if peak is not None:
            resid = Start("residual", tuple(P) + ((peak[0], fast_pass.LADDER_GAMMA, peak[1]),))
            slots = L + (None,)
            out += self._jobs(t, slots, [resid] + _record_start(rec, t, slots, resid.modes), "step_up", "step_up")
        return out

    # -- labelling moves ----------------------------------------------------------------------------------------
    def _expand(self, key):
        """The ranked moves of one ledger key (D4): the cheapest matching first, then every swap, every 'call it
        new' and every match or reassignment to an unused branch, cheapest shift-cost change first."""
        spec = self.spec
        L, P, pairs = self._parent(key)
        k, E = len(L), [m[0] for m in P]
        named = {spec.canon(b) for b in L if b is not None}
        unused = [b for b in self.pool if spec.canon(b) not in named and (b in self.in_window or b in self.dormant)]
        moves = {}
        for i in range(k):
            for new in [None] + unused:
                if new != L[i]:
                    moves[L[:i] + (new,) + L[i + 1:]] = spec.cost(E[i], new) - spec.cost(E[i], L[i])
        for i, j in combinations(range(k), 2):
            if L[i] != L[j] and (i, j) not in pairs:
                m = list(L)
                m[i], m[j] = L[j], L[i]
                moves[tuple(m)] = (spec.cost(E[i], L[j]) + spec.cost(E[j], L[i])
                                   - spec.cost(E[i], L[i]) - spec.cost(E[j], L[j]))
        queue = [lab for lab, _c in sorted(moves.items(), key=lambda x: (x[1], str(x[0])))]
        if k:
            cols = self.in_window
            C = np.full((k, len(cols) + k), 1e12)
            for i in range(k):
                C[i, :len(cols)] = [spec.cost(E[i], b) for b in cols]
                C[i, len(cols) + i] = spec.cost(E[i], None)
            rows, sel = linear_sum_assignment(C)
            cheap = tuple(cols[c] if c < len(cols) else None for _r, c in sorted(zip(rows, sel)))
            for i, j in pairs:                       # a placed pair's two orders are one basin: keep the key's own
                if cheap[i] == L[j] and cheap[j] == L[i] and all(cheap[n] == L[n] for n in range(k) if n not in (i, j)):
                    cheap = L
            queue = [cheap] + [lab for lab in queue if lab != cheap]
        self.keys[key] = {"queue": queue, "waves": [], "stopped": None, "start": tuple(P)}

    def _wave(self, key):
        """The next MOVE_WAVE moves of a key that still need a price (a start already attempted is skipped)."""
        ks, jobs = self.keys[key], []
        while ks["queue"] and len(jobs) < MOVE_WAVE:
            jobs += self._jobs(key[0], ks["queue"].pop(0), [Start("move", ks["start"])], "move", "move")
        ks["waves"].append({j.key for j in jobs})
        if not jobs:
            ks["stopped"] = "no_moves" if len(ks["waves"]) == 1 else "no_move_within_margin"
        return jobs

    # -- the decision and the record ----------------------------------------------------------------------------
    def _decide(self) -> Assignment:
        spec, s = self.spec, self.s
        masses, tot = self._totals()
        rows = []
        for (k, labels), e in self.ledger.items():
            groups = defaultdict(list)
            for b in e["basins"]:
                groups[(tuple(b[4][:3]), tuple(i for i, m in enumerate(b[2]) if m[4]))].append(b[0])
            w = rules.ln_w_label(k, sum(x is not None for x in labels), self.M)
            rows += [(k, labels, mask, float(logsumexp(v)) + w, var) for (var, mask), v in groups.items()]
        unpriced = [(j, p) for j, p in self.outcomes if not _priced(p)]
        rows += [(j.count, _labels(j.count, j.slots, j.starts[0].modes)[0], (), None)
                 for j, p in unpriced if p.status not in _QUIET]
        inc_v = spec.inc_variant
        d = rules.decide(rows, (self.n0, self.inc, spec.inc_delta, inc_v), eps=s.evidence_hysteresis_nats,
                         conf=s.confidence, close=s.close_call_nats)
        k, slots = d["winner"] or (self.n0, self.inc)
        chosen = [b for b in self.ledger.get((k, slots), {"basins": ()})["basins"]
                  if tuple(i for i, m in enumerate(b[2]) if m[4]) == d["delta"]
                  and (d["variant"] is None or tuple(b[4][:3]) == d["variant"])]
        walks = [self._walk(w) for w in self.walks]
        full = self.full is not None
        if full:
            stopped = {"down": "full_search", "up": "full_search"}
        else:
            ends = {(w["origin"], w["direction"]): w["stopped"] for w in walks}
            stopped = {"down": ends.get((min(self.seed_counts), "down")) or "no_parent",
                       "up": ends.get((max(self.seed_counts), "up")) or "no_parent"}
        failed = [w for w in walks if (w["stopped"] or "").startswith("unresolved:")]
        reasons = list(d["reasons"]) + [f"unresolved_step:{w['stopped'].split(':')[1]}:{w['reason']}" for w in failed]
        limited = (any((w["stopped"] or "").split(":")[0] in ("unresolved", "no_structure") for w in walks)
                   or any(self.meta[j.key]["purpose"] == "seed" for j, _p in unpriced)
                   or bool(full and self.full["truncated"]))
        cands = []
        for key, e in self.ledger.items():
            wgt, cp = d["weights"].get(key, 0.0), d["count_p"].get(key[0], 0.0)
            cands.append({"count": key[0], "slots": key[1], "ln_z": masses[key], "status": e["status"], "reason": None,
                          "weight": wgt, "weight_in_count": wgt / cp if cp > 0 else 0.0, "source": e["source"],
                          "key": e["key"], "basins": len(e["basins"]), **R._loss_record(spec, key[1])})
        for j, p in unpriced:
            lab = _labels(j.count, j.slots, j.starts[0].modes)[0]
            cands.append({"count": j.count, "slots": lab, "ln_z": None, "status": p.status, "reason": p.reason,
                          "weight": 0.0, "weight_in_count": 0.0, "source": self.meta[j.key]["source"], "key": j.key,
                          "basins": 0, **R._loss_record(spec, lab)})
        mask_p = sorted((dl, p) for (c, s_, dl), p in d["mask_weights"].items() if (c, s_) == (k, slots))
        states = {c: self._state(c) for c in sorted(self.by_count)}
        best = max(tot.values()) if tot else None
        counts = {}
        for c, st in states.items():
            bk = self._best_key(c, masses)
            counts[str(c)] = {"state": "priced" if st == "priced" else "unresolved", "ln_total": tot.get(c),
                              "labellings": sum(key[0] == c for key in masses),
                              "best_slots": None if bk is None else list(bk[1]),
                              "behind_nats": None if c not in tot else best - tot[c],
                              "reason": None if st == "priced" else self._failure(c)[1]}
        coverage = {
            "counts_priced": [c for c, st in states.items() if st == "priced"],
            "counts_unresolved": {str(c): "{}: {}".format(*self._failure(c)) for c, st in states.items()
                                  if st != "priced"},
            "stopped": stopped, "full_search": full, "enumeration_truncated": bool(full and self.full["truncated"]),
            "decision": {
                "ambiguous": bool(d["ambiguous"] or failed), "count_confident": d["count_confident"],
                "label_confident": d["label_confident"],
                "count_p": {str(c): p for c, p in sorted(d["count_p"].items())}, "label_p": d["label_p"],
                "close_calls": [[c[0], list(c[1])] for c in d["close_calls"]], "close_counts": d["close_counts"],
                "mask": {"incoming": list(spec.inc_delta), "chosen": list(d["delta"]),
                         "weights": [{"delta": list(dl), "p": p} for dl, p in mask_p]},
                "variant": {"incoming": None if inc_v is None else list(inc_v),
                            "chosen": None if d["variant"] is None else list(d["variant"])}}}
        search = {"version": 1, "domain": "full" if full else "adaptive", "margin_nats": self.margin,
                  "seeds": self.seeds, "counts": counts,
                  "walks": [{x: w[x] for x in ("origin", "direction", "reached", "stopped", "behind_nats")}
                            for w in walks],
                  "labelling": self._labelling_record(masses, tot), "tried": dict(self.tried), "full": self.full}
        return Assignment(
            unit=self.snap.unit, snapshot_id=self.snap.stage_id, count=k, slots=slots, origins=spec.origins(slots),
            factors=spec.incident(slots), discrete="priced",
            triggers=tuple(dict.fromkeys("seed:" + sd["source"] for sd in self.seeds)), candidates=tuple(cands),
            coverage=coverage, search_limited=limited, changed=bool(d["changed"]), reasons=tuple(reasons),
            selected_model=max(chosen, key=lambda b: b[0])[4] if chosen else None, mask_only=bool(d["mask_only"]),
            search=search)

    def _walk(self, w):
        """A walk that ran into a step another walk had taken ends where that walk ended."""
        while w.get("joined") is not None:
            w = {**self.walks[w["joined"]], "origin": w["origin"], "direction": w["direction"]}
        return w

    def _labelling_record(self, masses, tot):
        best = max(tot.values()) if tot else None
        out = {}
        for k in sorted({key[0] for key in masses}):
            keys = [ks for key, ks in self.keys.items() if key[0] == k]
            moves = [p for j, p in self.outcomes if self.meta[j.key]["purpose"] == "move" and j.count == k]
            stops = {ks["stopped"] for ks in keys}
            if tot[k] < best - self.margin:
                why = "not_live"
            elif "wave_behind_margin" in stops:
                why = "wave_behind_margin"
            elif not moves:
                why = "no_moves"
            else:
                why = "no_move_within_margin"
            out[str(k)] = {"expanded": len(keys), "moves_priced": sum(_priced(p) for p in moves),
                           "moves_unpriced": sum(len(ks["queue"]) for ks in keys), "stopped": why}
        return out


def drive(search: Search, price_one: Callable[[PricingJob], Priced]) -> tuple[Assignment, tuple[Priced, ...]]:
    """The inline loop: next_jobs, price each job with price_one, feed, until done. Returns result() and every
    Priced it was given, in the order asked. The one driver of refit.assign and of the scripted tests."""
    used: list[Priced] = []
    while not search.done:
        jobs = search.next_jobs()
        if not jobs:
            raise RuntimeError("the search asked for no job while it is not done")
        got = [price_one(j) for j in jobs]
        used += got
        search.feed(got)
    return search.result(), tuple(used)

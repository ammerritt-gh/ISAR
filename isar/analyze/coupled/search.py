"""One spectrum's listed-candidate search: the C-search boundary (design 2.3).

Inside a batch a spectrum no longer walks the mode count or tries labellings. It prices only what the linking
step listed: the incoming fit with the link's labelling, subsets of the changes listed for it (add a predicted
branch, split a mode into two branches, drop an unsupported mode) and, at a reported crossing, each of its grouped
alternative labellings. Up to ``MAX_JOINT_CHANGES`` changes every subset is priced in one pass; above that the
search extends the best candidate one change at a time (forward selection by evidence). The decision is
``rules.decide`` (count first, ``evidence_hysteresis_nats``) over the rows those prices file in one ledger.

The search asks for prices (``next_jobs`` and ``feed``) until ``done``; ``result`` then gives the frozen
decision. Nothing here fits: the prices come from ``refit.price`` through the caller. ``drive`` is the one
inline loop over this protocol. ``crossing_flags`` reads a stage's decisions back for confident disagreement
across a tie group.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from itertools import combinations

from scipy.special import logsumexp

from isar.analyze import prior
from isar.analyze.coupled import refit as R
from isar.analyze.coupled import rules
from isar.analyze.coupled.linking import FWHM_PER_GAMMA
from isar.analyze.coupled.types import Assignment, Priced, PricingJob, Snapshot, Start

MAX_JOINT_CHANGES = 4    # algorithm setting, uncalibrated: every subset of up to this many changes is priced at once
SAME_START_FWHM = 0.25   # algorithm setting, uncalibrated: a start this close (x FWHM, every mode) to an attempted one is not priced again
DEFAULT_SPLIT_FRACTION = 0.6   # algorithm setting, uncalibrated: the lower branch's share of a split mode when a prediction has no intensity
_QUIET = ("degenerate_twin", "no_structure")     # no price, and no reason in the decision (D3)
_KIND_ORDER = {"add": 0, "split": 1, "drop": 2}
_NEG = -math.inf


def _lse(xs) -> float:
    xs = [x for x in xs if x > _NEG]
    return float(logsumexp(xs)) if xs else _NEG


def gamma_start(w: float, fwhm: float) -> float:
    """The damping whose ``matching_width`` in resolution ``fwhm`` is ``w`` (0 below the resolution)."""
    return math.sqrt(max(w * w - fwhm * fwhm, 0.0)) / FWHM_PER_GAMMA


def _canon(rec):
    """A basin record with its modes in energy order (ties by label): the order its ledger key reads in."""
    return (rec[0], rec[1], tuple(sorted(rec[2], key=lambda m: (m[1], str(m[0])))), *rec[3:])


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


def _mask(b):
    return tuple(i for i, m in enumerate(b[2]) if m[4])


def change_id(c) -> str:
    """A listed change's name, free of its position in the listing."""
    if c.kind == "add":
        return f"add:{c.branches[0]}"
    if c.kind == "split":
        return f"split:{c.mode}:{'+'.join(c.branches)}"
    return f"drop:{c.mode}"


class _Ledger:
    """Every price filed once, under the labelling of its fitted energies: key (count, labels in energy order) ->
    {"basins", "status", "source", "key"} where source and key name the job that found the key's best basin."""

    def __init__(self, search) -> None:
        self.search = search
        self.entries: dict[tuple, dict] = {}
        self.by_k: dict[int, list] = defaultdict(list)
        self.repeated = 0

    def file(self, p: Priced, job: PricingJob) -> None:
        """File every basin of a priced job; a repeat (the base engine's closeness rule) adds nothing, but the
        higher term stays."""
        for rec in p.basins:
            rec = _canon(rec)
            modes = rec[2]
            key = (len(modes), tuple(m[0] for m in modes))
            e = self.entries.get(key)
            if e is None:
                e = self.entries[key] = {"basins": [], "status": p.status, "source": None, "key": None}
                self.by_k[key[0]].append(key)
            best = max((b[0] for b in e["basins"]), default=_NEG)
            for i, x in enumerate(e["basins"]):
                if R._same_basin(self.search.spec, x, rec):
                    self.repeated += 1
                    if rec[0] > x[0]:
                        e["basins"][i] = rec
                    break
            else:
                e["basins"].append(rec)
            if rec[0] > best:
                e["source"], e["key"] = self.search.meta[job.key], job.key


class Search:
    """One spectrum's listed-candidate search on one frozen snapshot: list, price, resume."""

    def __init__(self, snapshot: Snapshot) -> None:
        self.snap, self.s = snapshot, snapshot.settings
        self.spec = spec = R._Spec(snapshot)
        self.names, self.pool = spec.names, spec.pool        # the assignable pool (D9), one name per canonical branch
        self.M = len(self.pool)
        self.tol = SAME_START_FWHM * spec.fwhm
        self.content = spec.content_key()
        self.n0, self.inc = spec.n0, spec.incoming
        self.own = tuple((m.omega0, m.gamma, m.height) for m in spec.own.modes)
        self.attempted: dict[tuple, list] = defaultdict(list)    # (k, labels) -> (split?, energies) started (append-only)
        self.meta: dict[str, str] = {}                            # job key -> the id of the candidate it was made for
        self.cands: list[dict] = []                               # every listed candidate, in listing order
        self.current: dict[str, tuple] = {}                       # job key -> (job, Priced), feed order
        self.outcomes: list[tuple[PricingJob, Priced]] = []       # every price fed, in order
        self.by_count: dict[int, list[Priced]] = defaultdict(list)
        self.ledger_ = _Ledger(self)
        self.ledger = self.ledger_.entries
        self.skipped: list[dict] = []
        self.steps: list[dict] = []                               # forward selection: the best after each step
        self.tried = {"jobs": 0, "starts": 0, "fits": 0, "failed": 0, "rounds": 0, "evidence_s": 0.0,
                      "repeated_basins": 0}
        self._result: Assignment | None = None
        L = self.links = snapshot.links
        self.usable = self._usable(L) if L is not None else []   # [(index in L.changes, Change)], canonical order
        self.big = len(self.usable) > MAX_JOINT_CHANGES
        self.best = self._add("incoming", "incoming", (), self.n0, self.inc, Start("own", self.own))
        first = [self.best]
        if L is not None:
            sets = [(x,) for x in self.usable] if self.big else \
                [S for r in range(1, len(self.usable) + 1) for S in combinations(self.usable, r)]
            first += [c for c in map(self._change_cand, sets) if c is not None]
            first += self._tie_cands(L)
        self.pending = self._ask(first)

    # -- the listed candidates ----------------------------------------------------------------------------------
    def _name(self, b):
        """The pool name of branch ``b`` (None: not carried by a conditioning neighbour in this snapshot)."""
        return None if b is None else self.names.get(self.spec.canon(b))

    def _prediction(self, c, i):
        if i < len(c.predictions):
            return c.predictions[i]
        canon = self.spec.canon
        return next((p for p in self.links.predictions if canon(p.branch) == canon(c.branches[i])), None)

    def _usable(self, L):
        """The listed changes the pool can carry, in canonical order (kind add < split < drop, mode, branches);
        every other change is recorded in ``skipped`` with its reason, never silently dropped."""
        ok = []
        for i, c in sorted(enumerate(L.changes), key=lambda x: (_KIND_ORDER[x[1].kind],
                                                                   -1 if x[1].mode is None else x[1].mode,
                                                                   x[1].branches, x[0])):
            why = None
            if c.kind == "add":
                if self._name(c.branches[0]) is None:
                    why = "branch not carried by a conditioning neighbour"
                elif self._prediction(c, 0) is None:
                    why = "no prediction"
            elif c.kind == "split":
                if c.mode is None or not 0 <= c.mode < self.n0:
                    why = "no such mode"
                elif len(c.branches) != 2 or any(self._name(b) is None for b in c.branches):
                    why = "branch not carried by a conditioning neighbour"
                elif len(c.predictions) != 2:
                    why = "no prediction"
            elif c.mode is None or not 0 <= c.mode < self.n0:
                why = "no such mode"
            if why is None:
                ok.append((i, c))
            else:
                self.skipped.append({"change": i, "id": change_id(c), "why": why})
        return ok

    def _compose(self, S):
        """(count, slots, modes) of the incoming candidate with the changes S applied, modes ascending in energy and
        the easy modes at their incoming (omega0, Gamma, height); None when two changes meet on one mode or a branch
        would be named twice."""
        spec = self.spec
        R_ = spec.fwhm
        rows = {i: [(self.inc[i], m)] for i, m in enumerate(self.own)}
        adds, touched = [], set()
        for _i, c in S:
            if c.kind == "add":
                p = self._prediction(c, 0)
                g0 = min(max(gamma_start(p.w, R_), 0.1 * R_), prior.width_ceiling(p.E, lifted=False, settings=spec.fs.prior))
                adds.append((self._name(c.branches[0]), (p.E, g0, None)))
                continue
            if c.mode in touched:
                return None
            touched.add(c.mode)
            ((_slot, (_e, g, h)),) = rows[c.mode]
            if c.kind == "drop":
                rows[c.mode] = []
                continue
            lo, hi = sorted(zip(c.predictions, c.branches), key=lambda x: x[0].E)
            a, b = lo[0].A, hi[0].A
            f = a / (a + b) if a > 0 and b > 0 else DEFAULT_SPLIT_FRACTION
            rows[c.mode] = [(self._name(lo[1]), (lo[0].E, g, h * f)), (self._name(hi[1]), (hi[0].E, g, h * (1.0 - f)))]
        both = [x for v in rows.values() if v for x in v] + adds
        both.sort(key=lambda x: (x[1][0], str(x[0])))
        slots = tuple(s for s, _m in both)
        named = [s for s in slots if s is not None]
        if len(named) != len(set(named)):
            return None
        return len(both), slots, tuple(m for _s, m in both)

    def _change_cand(self, S):
        made = self._compose(S)
        if made is None:
            return None
        k, slots, modes = made
        split = any(c.kind == "split" for _i, c in S)
        return self._add("changes", "+".join(change_id(c) for _i, c in S), tuple(i for i, _c in S), k, slots,
                         Start("split" if split else "own", modes))

    def _tie_cands(self, L):
        out = []
        for t in sorted(L.ties, key=lambda t: t.group):
            cid = f"{'cross' if t.kind == 'swap' else 'compete'}:{t.group}"
            slots = tuple(self._name(b) for b in t.slots)
            if len(slots) != self.n0 or any(s is None and b is not None for s, b in zip(slots, t.slots)):
                self.skipped.append({"change": cid, "id": cid, "why": "branch not carried by a conditioning neighbour"})
                continue
            out.append(self._add("cross" if t.kind == "swap" else "compete", cid, (), self.n0, slots,
                                 Start("own", self.own)))
        return out

    def _add(self, kind, cid, changes, k, slots, start):
        c = {"id": cid, "kind": kind, "changes": changes, "count": k, "slots": slots, "start": start,
             "key": (k, _labels(k, slots, start.modes)[0]), "jobs": []}
        self.cands.append(c)
        return c

    # -- jobs ---------------------------------------------------------------------------------------------------
    def _ask(self, cands):
        """The jobs of the given candidates, in the order they are asked: count, candidate listing order, slots, key."""
        order = {c["id"]: n for n, c in enumerate(self.cands)}
        jobs = []
        for c in cands:
            starts = [c["start"]]
            if c["count"] != self.n0:               # the saved solo model at that count, when the record has one
                starts += _record_start(self.spec._record_modes(c["count"]), c["count"], c["slots"], c["start"].modes)
            made = self._jobs(c["count"], c["slots"], starts, c["id"])
            c["jobs"] += [j.key for j in made]
            jobs += made
        return tuple(sorted(jobs, key=lambda j: (j.count, order[self.meta[j.key]], str(j.slots), j.key)))

    def _jobs(self, k, slots, starts, cid):
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
            self.meta.setdefault(key, cid)
            jobs.append(PricingJob(self.snap.unit, self.snap.snapshot_id, k, js, key, sts))
        return jobs

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
            self.current[job.key] = (job, p)
            self.by_count[job.count].append(p)
            ok = _priced(p)
            t["jobs"] += 1
            t["starts"] += len(job.starts)
            t["fits"] += int(p.starts)
            t["evidence_s"] += float(p.seconds)
            t["failed"] += not ok
            if ok:
                self.ledger_.file(p, job)
        t["repeated_basins"] = self.ledger_.repeated
        self.pending = self._step() if self.big else ()

    def _step(self) -> tuple[PricingJob, ...]:
        """Forward selection (above MAX_JOINT_CHANGES changes): the best candidate so far is the one the decision
        picks with the current best as incumbent; the next batch is that best plus each remaining change not in
        conflict with it. () ends the search with the best standing."""
        b = self.best
        spec = self.spec
        same = b["count"] == self.n0
        d = rules.decide(self._rows(), (*b["key"], spec.inc_delta if same else (), spec.inc_variant if same else None),
                         eps=self.s.evidence_hysteresis_nats, conf=self.s.confidence, close=self.s.close_call_nats)
        w = d["winner"]
        nxt = next((c for c in self.cands if c["key"] == w and c["kind"] in ("incoming", "changes")), None)
        if w is None or w == b["key"] or nxt is None or len(self.steps) >= len(self.usable):
            return ()
        self.best = nxt
        self.steps.append({"step": len(self.steps) + 1, "best": nxt["id"]})
        have = {c["changes"] for c in self.cands}
        held = [x for x in self.usable if x[0] in nxt["changes"]]
        fresh = []
        for x in self.usable:
            if x[0] in nxt["changes"]:
                continue
            S = tuple(sorted(held + [x], key=lambda y: self.usable.index(y)))
            if tuple(i for i, _c in S) in have:
                continue
            c = self._change_cand(S)
            if c is not None:
                fresh.append(c)
        self.steps[-1]["jobs"] = len(fresh)
        return self._ask(fresh)

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

    # -- the ledger and the decision ----------------------------------------------------------------------------
    def _rows(self):
        """rules.decide's rows: one per (key, variant, mask) group of the ledger, then each unpriced job that is not
        a quiet no-structure/twin outcome (a row with no price, its reason kept)."""
        rows = []
        for (k, labels), e in self.ledger.items():
            groups: dict[tuple, list] = defaultdict(list)
            for b in e["basins"]:
                groups[(tuple(b[4][:3]), _mask(b))].append(b[0])
            w = rules.ln_w_label(k, sum(x is not None for x in labels), self.M)
            rows += [(k, labels, mask, _lse(ts) + w, var) for (var, mask), ts in groups.items()]
        rows += [(j.count, _labels(j.count, j.slots, j.starts[0].modes)[0], (), None)
                 for j, p in self.current.values() if not _priced(p) and p.status not in _QUIET]
        return rows

    def _mass(self, key) -> float:
        """A ledger key's mass: its basins' terms summed, with the labelling's composition weight."""
        return _lse(b[0] for b in self.ledger[key]["basins"]) + rules.ln_w_label(
            key[0], sum(x is not None for x in key[1]), self.M)

    def _state(self, k):
        """priced if any job at k priced; else failed, collapsed (every job a lower-count twin) or no_structure."""
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

    def _decide(self) -> Assignment:
        spec, s = self.spec, self.s
        d = rules.decide(self._rows(), (self.n0, self.inc, spec.inc_delta, spec.inc_variant),
                         eps=s.evidence_hysteresis_nats, conf=s.confidence, close=s.close_call_nats)
        key = d["winner"] or (self.n0, self.inc)
        k, slots = key
        entry = self.ledger.get(key)
        chosen = None
        if entry is not None:
            rows = [(b, n) for n, b in enumerate(entry["basins"]) if _mask(b) == d["delta"]
                    and (d["variant"] is None or tuple(b[4][:3]) == d["variant"])]
            chosen = max(rows, key=lambda x: (x[0][0], -x[1]))[0] if rows else None
        # the candidate whose job found the winning basin: several can share a key (drops of unlinked modes)
        by_id = {c["id"]: c for c in self.cands}
        listed = by_id.get(entry["source"]) if d["winner"] and entry is not None else None
        if listed is None:
            listed = next((c for c in self.cands if c["key"] == key), None)
        if listed is None and entry is not None:       # a refit's own relabelling: the candidate whose job found it
            listed = by_id.get(entry["source"])
        unpriced = [(j, p) for j, p in self.current.values() if not _priced(p)]
        single = {c["id"] for c in self.cands if len(c["changes"]) <= 1}
        limited = any(self.meta[j.key] in single and p.status not in _QUIET for j, p in unpriced)

        def fin(x):
            return None if x is None or x == _NEG else x
        cands = []
        for lk, e in self.ledger.items():
            wgt, cp = d["weights"].get(lk, 0.0), d["count_p"].get(lk[0], 0.0)
            cands.append({"count": lk[0], "slots": lk[1], "ln_z": fin(self._mass(lk)), "status": e["status"],
                "reason": None, "weight": wgt, "weight_in_count": wgt / cp if cp > 0 else 0.0, "source": e["source"], "key": e["key"],
                "basins": len(e["basins"]), **R._loss_record(spec, lk[1])})
        for j, p in unpriced:
            lab = _labels(j.count, j.slots, j.starts[0].modes)[0]
            cands.append({"count": j.count, "slots": lab, "ln_z": None, "status": p.status, "reason": p.reason,
                          "weight": 0.0, "weight_in_count": 0.0, "source": self.meta[j.key], "key": j.key,
                          "basins": 0, **R._loss_record(spec, lab)})
        states = {c: self._state(c) for c in sorted(self.by_count)}
        mask_p = sorted((dl, p) for (c, s_, dl), p in d["mask_weights"].items() if (c, s_) == (k, slots))
        cp = d["count_p"].get(k, 0.0)
        inc_v = spec.inc_variant
        coverage = {
            "counts_priced": [c for c, st in states.items() if st == "priced"],
            "counts_unresolved": {str(c): "{}: {}".format(*self._failure(c)) for c, st in states.items()
                                  if st != "priced"},
            "stopped": {"down": "listed", "up": "listed"}, "listed": len(self.cands),
            "decision": {
                "ambiguous": bool(d["ambiguous"]), "count_confident": d["count_confident"],
                "label_confident": d["label_confident"],
                "count_p": {str(c): p for c, p in sorted(d["count_p"].items())}, "label_p": d["label_p"],
                "partial": False, "count_p_range": [cp, cp], "label_p_range": [d["label_p"], d["label_p"]],
                "close_calls": [[c[0], list(c[1])] for c in d["close_calls"]], "close_counts": d["close_counts"],
                "mask": {"incoming": list(spec.inc_delta), "chosen": list(d["delta"]),
                         "weights": [{"delta": list(dl), "p": p} for dl, p in mask_p]},
                "variant": {"incoming": None if inc_v is None else list(inc_v),
                            "chosen": None if d["variant"] is None else list(d["variant"])}}}
        L = self.links
        search = {"version": 3, "domain": "listed", "link": None if L is None else L.link_id,
                  "stage": None if L is None else L.stage,
                  "listed": [{"id": c["id"], "kind": c["kind"], "count": c["count"], "slots": list(c["slots"]),
                              "changes": list(c["changes"]), "jobs": list(c["jobs"])} for c in self.cands],
                  "skipped": list(self.skipped),
                  "chosen": next((c["id"] for c in self.cands if c["key"] == key), "other"),
                  # the indices into Snapshot.links.changes of the changes the adopted candidate applied
                  "applied": [] if listed is None else sorted(listed["changes"]),
                  "steps": list(self.steps), "tried": dict(self.tried)}
        return Assignment(
            unit=self.snap.unit, snapshot_id=self.snap.stage_id, count=k, slots=slots, origins=spec.origins(slots),
            factors=spec.incident(slots), discrete="priced",
            triggers=tuple("listed:" + c["id"] for c in self.cands), candidates=tuple(cands),
            coverage=coverage, search_limited=limited, changed=bool(d["changed"]), reasons=tuple(d["reasons"]),
            selected_model=chosen[4] if chosen else None, mask_only=bool(d["mask_only"]), search=search)


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


def crossing_flags(snaps, A: Mapping[str, Assignment]) -> dict[str, list[dict]]:
    """Confident disagreement across a tie group: {unit: [flag reason]}. ``snaps``: the stage's snapshots (a mapping
    unit -> Snapshot, or an iterable of them); ``A``: unit -> Assignment. A spectrum took the original side when
    its search chose "incoming", the alternative side when it chose its own "cross:<group>" / "compete:<group>"
    candidate, and no side otherwise (an add, a refit's own relabelling). Both sides present in one group flags
    every spectrum that took a side. A group with an anchored end has no alternative for that end, so one side
    alone never flags."""
    snaps = snaps.values() if isinstance(snaps, Mapping) else snaps
    groups: dict[str, set] = defaultdict(set)
    for sn in snaps:
        for t in (sn.links.ties if sn.links is not None else ()):
            groups[t.group].add(sn.unit)       # only a spectrum with an alternative of its own takes a side
    out: dict[str, list[dict]] = {}
    for g, units in sorted(groups.items()):
        side = {}
        for u in sorted(units):
            a = A.get(u)
            chosen = (a.search or {}).get("chosen") if a is not None else None
            if chosen == "incoming":
                side[u] = "original"
            elif chosen in (f"cross:{g}", f"compete:{g}"):
                side[u] = "alternative"
        if len(set(side.values())) < 2:
            continue
        kept = [u for u, v in side.items() if v == "original"]
        took = [u for u, v in side.items() if v == "alternative"]
        text = f"crossing {g}: {', '.join(kept)} kept the original labelling, {', '.join(took)} took the alternative one"
        for u in side:
            out.setdefault(u, []).append({"code": "identity_ambiguous", "severity": "serious", "text": text})
    return out

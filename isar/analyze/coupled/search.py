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

Screens (mode ``"on"``): seeds are priced in full, every step and move first as a screen (each split integral cut
to its first node set). A screened basin's term is bounded, never trusted: lo leaves it out, hi takes it at its
screen plus SCREEN_ALLOWANCE_NATS. Every comparison is three-valued; an undecided one promotes the screened
candidates it reads to a full price, the largest optimistic contribution first, PROMOTE_WAVE a pass, and a full
price replaces its screen in the ledger. Mode ``"shadow"`` prices everything in full, drives on it, and judges
every comparison a second time on the screens, into ``search["shadow"]`` (the calibration run's record).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import replace
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
SCREEN_ALLOWANCE_NATS = 5.0  # algorithm setting, uncalibrated (S1U5): how far a screened term may sit below its full price
PROMOTE_WAVE = 6             # algorithm setting, uncalibrated: screened candidates promoted to a full price per pass
PROB_TOL = 0.01              # algorithm setting, uncalibrated: how far a saved probability may move under the remainder
_QUIET = ("degenerate_twin", "no_structure")     # no price, and no reason in the decision (D3)
_PURPOSE = {"seed": 0, "step_down": 1, "step_up": 2, "move": 3, "promote": 4, "full": 5, "shadow_screen": 6}
_SCREENED = ("step_down", "step_up", "move")     # the purposes priced as a screen first; seeds are always full
_NEG = -math.inf


def _lse(xs) -> float:
    xs = [x for x in xs if x > _NEG]
    return float(logsumexp(xs)) if xs else _NEG


def _within(x_lo, x_hi, ref_lo, ref_hi, m):
    """Three-valued "x is within m of the reference": True or False when every value inside the bounds agrees."""
    if x_lo >= ref_hi - m:
        return True
    if x_hi < ref_lo - m:
        return False
    return None


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


class _View:
    """One ledger of a search: every current price filed once, under the labelling of its fitted energies, each basin
    exact or screened (a split basin of a job sent as a screen). A key's lo leaves its screened basins out (-inf when
    it has no exact one), its hi takes each at its screen plus SCREEN_ALLOWANCE_NATS; a count's are the logsumexp of
    its keys'. Every undecided comparison adds the screened candidates it reads to ``requests``."""

    def __init__(self, search, entries):
        self.search, self.margin = search, search.margin
        self.ledger: dict[tuple, dict] = {}
        self.new: dict[str, tuple] = {}                           # candidate -> ledger keys its price created
        self.attempted: dict[tuple, list] = defaultdict(list)     # (k, labels) -> (False, energies) of each basin
        self.repeated, self.requests = 0, set()
        for cand, job, p in entries:
            if _priced(p):
                scr = [job.depth == "screen" and i < len(p.screen_terms) and p.screen_terms[i] is not None
                       for i in range(len(p.basins))]
                self.new[cand] = tuple(k for k in (self._file(rec, p, job, cand, s) for rec, s in zip(p.basins, scr))
                                       if k)
        A = SCREEN_ALLOWANCE_NATS
        self.lo, self.hi, self.face, self.opt = {}, {}, {}, {}
        self.by_k: dict[int, list] = defaultdict(list)
        for key, e in self.ledger.items():
            w = rules.ln_w_label(key[0], sum(b is not None for b in key[1]), search.M)
            ex = [b[0] for b, s in zip(e["basins"], e["scr"]) if not s]
            sc = [b[0] for b, s in zip(e["basins"], e["scr"]) if s]
            self.lo[key], self.hi[key] = _lse(ex) + w, _lse(ex + [t + A for t in sc]) + w
            self.face[key] = _lse(ex + sc) + w if sc else None
            for b, s, c in zip(e["basins"], e["scr"], e["by"]):
                if s:
                    self.opt[c] = max(self.opt.get(c, _NEG), b[0] + A + w)
            self.by_k[key[0]].append(key)
        self.tot_lo = {k: _lse([self.lo[x] for x in ks]) for k, ks in self.by_k.items()}
        self.tot_hi = {k: _lse([self.hi[x] for x in ks]) for k, ks in self.by_k.items()}

    def _file(self, rec, p, job, cand, screened):
        """File one basin; a repeat (the base engine's closeness rule) adds nothing, but an exact record replaces a
        screened one whatever their terms, and between two of one kind the higher term stays. Returns its key when
        the key is new."""
        rec = _canon(rec)
        modes = rec[2]
        key = (len(modes), tuple(m[0] for m in modes))
        e = self.ledger.get(key)
        new = e is None
        if new:
            e = self.ledger[key] = {"basins": [], "scr": [], "by": [], "status": p.status,
                                    "source": self.search.meta[job.key]["source"], "key": job.key,
                                    "peak": p.residual_peak}
        for i, x in enumerate(e["basins"]):
            if R._same_basin(self.search.spec, x, rec):
                self.repeated += 1
                if (e["scr"][i] and not screened) or (e["scr"][i] == screened and rec[0] > x[0]):
                    e["basins"][i], e["scr"][i], e["by"][i] = rec, screened, cand
                    if rec[0] > max((b[0] for j, b in enumerate(e["basins"]) if j != i), default=_NEG):
                        e["peak"], e["source"], e["key"] = p.residual_peak, self.search.meta[job.key]["source"], job.key
                return None
        if e["basins"] and rec[0] > max(b[0] for b in e["basins"]):
            e["peak"] = p.residual_peak
        e["basins"].append(rec)
        e["scr"].append(screened)
        e["by"].append(cand)
        self.attempted[key].append((False, tuple(m[1] for m in modes)))
        return key if new else None

    def screened(self, keys) -> set:
        return {c for key in keys for c, s in zip(self.ledger[key]["by"], self.ledger[key]["scr"]) if s}

    def count_within(self, t):
        """Count t's total within the margin of the best count's."""
        rl, rh = max(self.tot_lo.values()), max(self.tot_hi.values())
        v = _within(self.tot_lo.get(t, _NEG), self.tot_hi.get(t, _NEG), rl, rh, self.margin)
        if v is None:
            self.requests |= self.screened([x for k, ks in self.by_k.items() if k == t or self.tot_hi[k] >= rl
                                            for x in ks])
        return v

    def key_within(self, key):
        """A key's mass within the margin of its count's best key's (a key this view lacks: False)."""
        if key not in self.lo:
            return False
        ks = self.by_k[key[0]]
        rl, rh = max(self.lo[x] for x in ks), max(self.hi[x] for x in ks)
        v = _within(self.lo[key], self.hi[key], rl, rh, self.margin)
        if v is None:
            self.requests |= self.screened([key] + [x for x in ks if self.hi[x] >= rl])
        return v

    def best_key(self, k):
        """(count k's best key, whether the bounds determine it): its lo, ties by str(key), at least every other's hi."""
        ks = self.by_k.get(k)
        if not ks:
            return None, True
        c = max(ks, key=lambda x: (self.lo[x], str(x)))
        det = all((self.lo[c], str(c)) >= (self.hi[x], str(x)) for x in ks if x != c)
        if not det:
            self.requests |= self.screened([x for x in ks if self.hi[x] >= self.lo[c]])
        return c, det

    def top(self, key, keep=None):
        """(the highest-term basin of a key among those ``keep`` passes, whether the bounds determine it)."""
        e = self.ledger[key]
        rows = [(b, s, c) for b, s, c in zip(e["basins"], e["scr"], e["by"]) if keep is None or keep(b)]
        if not rows:
            return None, True
        lo = [_NEG if s else b[0] for b, s, _c in rows]
        hi = [b[0] + SCREEN_ALLOWANCE_NATS if s else b[0] for b, s, _c in rows]
        i = max(range(len(rows)), key=lambda n: (lo[n], -n))           # the first maximum, as max() picks it
        det = all(lo[i] >= hi[j] for j in range(len(rows)) if j != i)
        if not det:
            self.requests |= {c for _b, s, c in rows if s}
        return rows[i][0], det


class Search:
    """One spectrum's adaptive candidate search on one frozen snapshot: search, price, resume."""

    def __init__(self, snapshot: Snapshot, *, screens: str | None = None) -> None:
        if screens is None:             # the calibration tool's switch, never an operator setting
            screens = os.environ.get("ISAR_INTER_SCREENS") or "on"
        if screens not in ("on", "shadow"):
            raise ValueError(f"screens must be 'on' or 'shadow', not {screens!r}")
        self.screens = screens
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
        self.attempted: dict[tuple, list] = defaultdict(list)    # (k, labels) -> (split?, energies) started (append-only)
        self.meta: dict[str, dict] = {}                           # job key -> purpose, source; a screen's "of": its full key
        self.current: dict[str, tuple] = {}                       # candidate (full key) -> (job, Priced), first-feed order
        self.shadow_current: dict[str, tuple] = {}                # mode shadow: candidate -> its shadow screen's (job, Priced)
        self.promoted: set[str] = set()                           # candidates asked at full depth after a screen
        self.promoting: set[str] = set()                          # ... asked in the pass now pending
        self.decisions: list[dict] = []                           # mode shadow: where the screened search would differ
        self.first_divergence: int | None = None
        self.outcomes: list[tuple[PricingJob, Priced]] = []       # every price fed (screens and fulls), in order
        self.by_count: dict[int, list[Priced]] = defaultdict(list)
        self.fed: set[str] = set()
        self.view = _View(self, [])                               # the ledger of the current prices
        self.ledger = self.view.ledger                            # (k, labels in energy order) -> basins and origin
        self.sview: _View | None = None                           # mode shadow: the ledger the screens would give
        self.steps: dict[tuple, dict] = {}                        # (c, d) -> {"walk", "jobs", "judged"}
        self.walks: list[dict] = []
        self.follow: dict[tuple, int] = {}                        # (c, d) -> the walk that continues through it
        self.judged: set[int] = set()
        self.wave1: dict[int, set] = {}                           # count -> keys of the first wave on its best key
        self.keys: dict[tuple, dict] = {}                         # expanded ledger key -> queue, waves, stop
        self.tried = {"jobs": 0, "starts": 0, "fits": 0, "failed": 0, "rounds": 0, "evidence_s": 0.0,
                      "repeated_basins": 0, "screens": 0, "promotions": 0}
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
            if any(all(abs(a - b) <= self.tol for a, b in zip(es, x))
                   for s_, x in seen + self.view.attempted.get((k, lab), []) if s_ == split):
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
            self.meta[key] = {"purpose": purpose, "source": source}
            job = PricingJob(self.snap.unit, self.snap.snapshot_id, k, js, key, sts)
            if purpose not in _SCREENED:
                jobs.append(job)
                continue
            # a screen's key differs from its full key, so no cache ever serves a screen for a full request
            skey = hashlib.sha256((self.content + part + "|screen").encode("utf-8")).hexdigest()
            screen = replace(job, key=skey, depth="screen")
            if self.screens == "on":
                self.meta[skey] = {"purpose": purpose, "source": source, "of": key}
                jobs.append(screen)
            else:
                self.meta[skey] = {"purpose": "shadow_screen", "source": source, "of": key}
                jobs += [job, screen]
        return jobs

    def _order(self, jobs):
        def purpose(j):
            return _PURPOSE["promote" if j.key in self.promoted else self.meta[j.key]["purpose"]]
        return tuple(sorted(jobs, key=lambda j: (j.count, purpose(j), str(j.slots), j.key)))

    def _cand(self, key):
        """The candidate (its full job key) a job key prices."""
        return self.meta[key].get("of", key)

    def _ready(self, keys) -> bool:
        """Every job fed, and no candidate among them waiting for the full price now asked for it."""
        return all(k in self.fed and self._cand(k) not in self.promoting for k in keys)

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
            t["screens"] += job.depth == "screen"
            # the depth is the job's as sent, so a pool-failed screen still reads as a failed screen
            shadow = self.meta[job.key]["purpose"] == "shadow_screen"
            (self.shadow_current if shadow else self.current)[self._cand(job.key)] = (job, p)
        self._rebuild()
        if self.full is not None:
            self.pending = ()
            return
        self.promoting = set()
        # a failed screen means "price it properly", at once and unranked; a quiet one is final as a full price would be
        jobs = self._promote([c for c, (j, p) in self.current.items()
                              if j.depth == "screen" and not _priced(p) and p.status not in _QUIET])
        jobs += self._advance()
        if not jobs:
            f = self._final(self.view)
            if not f["settled"]:
                jobs = self._promote(self._rank(self.view, f["requests"]))
        self.pending = self._order(jobs)

    def _rebuild(self):
        """The ledger as a view of the current prices (a full price having replaced its screen), and in mode shadow
        the ledger the screens would have given."""
        self.view = _View(self, [(c, j, p) for c, (j, p) in self.current.items()])
        self.ledger = self.view.ledger
        self.tried["repeated_basins"] = self.view.repeated
        self.by_count = defaultdict(list)
        for j, p in self.current.values():
            self.by_count[j.count].append(p)
        if self.screens == "shadow":
            ents = []
            for c, (j, p) in self.current.items():
                sj, sp = self.shadow_current.get(c, (j, p))
                ents.append((c, sj, sp) if _priced(sp) or sp.status in _QUIET else (c, j, p))   # a failed one: promoted
            self.sview = _View(self, ents)

    def _promote(self, cands):
        """Full-depth jobs for screened candidates not asked at full depth before: same slots and starts, full key."""
        jobs = []
        for c in cands:
            if c not in self.promoted:
                self.promoted.add(c)
                self.promoting.add(c)
                jobs.append(replace(self.current[c][0], key=c, depth="full"))
        self.tried["promotions"] = len(self.promoted)
        return jobs

    def _rank(self, view, cands):
        """The first PROMOTE_WAVE candidates not yet promoted, by optimistic contribution (screened term + allowance +
        labelling weight, the largest of their screened basins), descending, ties by key."""
        return sorted((c for c in cands if c not in self.promoted),
                      key=lambda c: (-view.opt.get(c, _NEG), c))[:PROMOTE_WAVE]

    def _shadow(self, kind, at, full, screen):
        """Mode shadow: record a comparison the screened search would have judged otherwise (None: it would promote)."""
        if self.sview is None or screen == full:
            return
        rec = {"kind": kind, "at": str(at), "full": full, "screen": screen}
        if rec in self.decisions:
            return
        if screen is not None and self.first_divergence is None:
            self.first_divergence = len(self.decisions)
        self.decisions.append(rec)

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
    def _mass(self, key) -> float:
        """A ledger key's fully priced mass (its lo: screened basins left out)."""
        return self.view.lo[key]

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

    def _best_key(self, k):
        """Count k's best ledger key, or None while screens leave it undetermined (their promotion is requested)."""
        key, det = self.view.best_key(k)
        if self.sview is not None:
            sk, sdet = self.sview.best_key(k)
            self._shadow("best_key", k, True, (sk == key) if sdet else None)
        return key if det else None

    def _parent(self, key):
        """(labels, [(e, Gamma, h)] in energy order, swap-free pairs) of a ledger key's best basin, or None while
        screens leave it undetermined. A split basin's pair is a placed representative (D18): its two modes carry
        one set of values and are never swapped (D4)."""
        rec, det = self.view.top(key)
        if self.sview is not None:
            srec, sdet = self.sview.top(key) if key in self.sview.ledger else (None, True)
            self._shadow("best_basin", key, True,
                         (srec is not None and R._same_basin(self.spec, srec, rec)) if sdet else None)
        if not det:
            return None
        modes = [(m[1], m[2], m[3]) for m in rec[2]]
        pairs = set()
        if rec[1].endswith("|split"):
            pairs = {(i, j) for i, j in combinations(range(len(modes)), 2) if modes[i] == modes[j]}
        return key[1], modes, pairs

    # -- the walks ----------------------------------------------------------------------------------------------
    def _advance(self):
        V, S = self.view, self.sview
        # each count live (within the margin of the best), behind, or undecided (None) while screens leave it open
        live3 = {t: V.count_within(t) for t in sorted(V.tot_lo)}
        s_live = {t: S.count_within(t) for t in live3} if S is not None else {}
        live = {t for t, v in live3.items() if v}
        jobs: list[PricingJob] = []
        while True:
            moved = False
            # judge every step whose jobs, and when its target priced the target's first wave, have been fed (D2)
            for (c, d), st in list(self.steps.items()):
                if st["judged"] or not self._ready(st["jobs"]):
                    continue
                t, walk = c + d, self.walks[st["walk"]]
                state = self._state(t)
                if state == "priced":
                    wave = self._first_wave(t, jobs)
                    if wave is None or not self._ready(wave):
                        continue
                    self._shadow("walk", t, live3[t], s_live.get(t))
                    if live3[t] is None:
                        continue
                st["judged"], walk["reached"], moved = True, t, True
                self.judged.add(t)
                if state == "priced" and live3[t]:
                    if (t, d) in self.steps:
                        walk["joined"] = self.steps[(t, d)]["walk"]
                    else:
                        self.follow[(t, d)] = st["walk"]
                elif state == "priced":
                    walk["stopped"] = f"behind_margin:{t}"
                    walk["behind_nats"] = max(V.tot_lo.values()) - V.tot_hi[t]   # at least this far behind
                else:
                    walk["stopped"] = {None: "no_step", "failed": "unresolved", "collapsed": "collapsed",
                                       "no_structure": "no_structure"}[state] + f":{t}"
                    walk["reason"] = self._failure(t)[1] if state == "failed" else None
            # step from every seed count and every judged live count, both ways, and continue every live walk
            todo = [(c, d) for c in sorted(self.seed_counts | (live & self.judged)) for d in (-1, 1)]
            for c, d in todo + list(self.follow):
                if (c, d) in self.steps:
                    continue
                stop = c + d < 0 or self._state(c) != "priced"
                new = [] if stop else self._step_jobs(c, c + d)
                if new is None:                 # its parent waits for promotions
                    continue
                moved = True
                wid = self.follow.get((c, d))
                if wid is None:
                    wid = len(self.walks)
                    self.walks.append({"origin": c, "direction": "down" if d < 0 else "up", "reached": c,
                                       "stopped": None, "behind_nats": None})
                if stop:
                    self.walks[wid]["stopped"] = "floor" if c + d < 0 else "no_parent"
                    self.steps[(c, d)] = {"walk": wid, "jobs": set(), "judged": True}
                    continue
                jobs += new
                self.steps[(c, d)] = {"walk": wid, "jobs": {j.key for j in new}, "judged": False}
            if not moved:
                break
        # labelling moves of the live counts (D4): expand every key within the margin of its count's best, then one
        # more wave of each expanded key whose last wave was productive
        for k in sorted(live):
            for key in sorted(V.by_k[k], key=str):
                if key not in self.keys:
                    v = V.key_within(key)
                    if S is not None:
                        self._shadow("expand", key, v, S.key_within(key))
                    if v:
                        self._expand(key)
        for key, ks in self.keys.items():
            if ks["stopped"] is not None or key[0] not in live or not all(self._ready(w) for w in ks["waves"]):
                continue
            if ks["waves"]:
                v = self._productive(V, ks["waves"][-1])
                if S is not None:
                    self._shadow("productive", key, v, self._productive(S, ks["waves"][-1]))
                if v is None:
                    continue
                if not v:
                    ks["stopped"] = "wave_behind_margin"
                    continue
            jobs += self._wave(key)
        return jobs + self._promote(self._rank(V, V.requests))

    def _productive(self, view, wave):
        """A wave is productive when it created a ledger key within the margin of its count's best (None: undecided)."""
        made = [key for j in wave for key in view.new.get(self._cand(j), ())]
        before = set(view.requests)
        vs = [view.key_within(key) for key in made]
        if any(vs):
            view.requests = before          # decided: nothing to promote for it
            return True
        return False if all(v is False for v in vs) else None

    def _first_wave(self, t, jobs):
        """The keys of the first wave of labelling moves on count t's best key, asked now when it has none; None while
        screens leave that key or its best basin undetermined."""
        if t not in self.wave1:
            key = self._best_key(t)
            if key is None:
                return None
            if key in self.keys:
                self.wave1[t] = set().union(*self.keys[key]["waves"][:1])
            else:
                if not self._expand(key):
                    return None
                new = self._wave(key)
                jobs += new
                self.wave1[t] = {j.key for j in new}
        return self.wave1[t]

    def _step_jobs(self, c, t):
        """The jobs of one count step from the best key at c: going up, an add of each missing in-window branch at
        each neighbour energy of it (with a split start of the nearest mode) and the residual add; going down,
        every removal. Every step job also starts from the incoming record's saved model at t, when it has one.
        None while screens leave the parent undetermined."""
        spec = self.spec
        parent = self._best_key(c)
        got = None if parent is None else self._parent(parent)
        if got is None:
            return None
        L, P, _pairs = got
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
        new' and every match or reassignment to an unused branch, cheapest shift-cost change first. False (nothing
        expanded) while screens leave the key's best basin undetermined."""
        spec = self.spec
        got = self._parent(key)
        if got is None:
            return False
        L, P, pairs = got
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
        return True

    def _wave(self, key):
        """The next MOVE_WAVE moves of a key that still need a price (a start already attempted is skipped; a shadow
        screen rides with its move and is not one)."""
        ks, jobs = self.keys[key], []
        while ks["queue"] and sum(self.meta[j.key]["purpose"] != "shadow_screen" for j in jobs) < MOVE_WAVE:
            jobs += self._jobs(key[0], ks["queue"].pop(0), [Start("move", ks["start"])], "move", "move")
        ks["waves"].append({j.key for j in jobs})
        if not jobs:
            ks["stopped"] = "no_moves" if len(ks["waves"]) == 1 else "no_move_within_margin"
        return jobs

    # -- the decision and the record ----------------------------------------------------------------------------
    @staticmethod
    def _mask(b):
        return tuple(i for i, m in enumerate(b[2]) if m[4])

    def _rows(self, view):
        """One row per (key, variant, mask) group in ledger order: (k, labels, mask, lo, variant, hi, screened?)."""
        rows = []
        for (k, labels), e in view.ledger.items():
            groups: dict[tuple, tuple] = {}
            for b, s in zip(e["basins"], e["scr"]):
                groups.setdefault((tuple(b[4][:3]), self._mask(b)), ([], []))[s].append(b[0])
            w = rules.ln_w_label(k, sum(x is not None for x in labels), self.M)
            rows += [(k, labels, mask, _lse(ex) + w, var, _lse(ex + [x + SCREEN_ALLOWANCE_NATS for x in sc]) + w,
                      bool(sc)) for (var, mask), (ex, sc) in groups.items()]
        return rows

    def _decision(self, rows, pick):
        """rules.decide on the rows, each at the bound ``pick`` gives it; a row at -inf (no exact basin) is left out."""
        s, spec = self.s, self.spec
        pr = [(r[0], r[1], r[2], z, r[4]) for r in rows for z in (pick(r),) if z > _NEG]
        pr += [(j.count, _labels(j.count, j.slots, j.starts[0].modes)[0], (), None)
               for j, p in self.current.values() if not _priced(p) and p.status not in _QUIET]
        return rules.decide(pr, (self.n0, self.inc, spec.inc_delta, spec.inc_variant), eps=s.evidence_hysteresis_nats,
                            conf=s.confidence, close=s.close_call_nats)

    def _final(self, view):
        """The final decision on the lo rows, and whether the screens' remainder can still change it: rules.decide on
        each level's adverse and favourable extremes (the level's rows at lo and the rest at hi, then the reverse)
        must agree with it, and the selected basin must be determined. Each criterion is monotone in each row with a
        fixed sign, so these extremes bound it."""
        rows = self._rows(view)
        d = self._decision(rows, lambda r: r[3])
        w = d["winner"]
        kw = None if w is None else w[0]
        cp, lp = d["count_p"].get(kw, 0.0), d["label_p"]
        out = {"d": d, "settled": True, "partial": any(r[6] for r in rows), "requests": set(), "chosen": None,
               "count_p_range": [cp, cp], "label_p_range": [lp, lp], "remainder": None}
        saved, view.requests = view.requests, set()
        key = w or (self.n0, self.inc)
        if key in view.ledger:
            out["chosen"], det = view.top(key, lambda b: self._mask(b) == d["delta"]
                                          and (d["variant"] is None or tuple(b[4][:3]) == d["variant"]))
            out["settled"] = det
        if out["partial"]:
            levels = {"count": lambda r: r[0] == kw, "label": lambda r: (r[0], r[1]) == w,
                      "model": lambda r: ((r[0], r[1]) == w and r[2] == d["delta"]
                                          and (d["variant"] is None or tuple(r[4]) == tuple(d["variant"])))}
            ext = {name: (self._decision(rows, lambda r, f=inside: r[3] if f(r) else r[5]),     # adverse
                          self._decision(rows, lambda r, f=inside: r[5] if f(r) else r[3]))     # favourable
                   for name, inside in levels.items()}

            def sig(x):
                return (x["winner"], x["delta"], x["variant"], x["count_confident"], x["label_confident"],
                        sorted(map(str, x["close_calls"])), sorted(x["close_counts"]))
            evs = [d] + [x for pair in ext.values() for x in pair]
            cps, lps = [x["count_p"].get(kw, 0.0) for x in evs], [x["label_p"] for x in evs]
            if not (all(sig(x) == sig(d) for x in evs) and max(cps) - min(cps) <= PROB_TOL
                    and max(lps) - min(lps) <= PROB_TOL):
                out["settled"] = False
                view.requests |= view.screened(view.ledger)
            out["count_p_range"] = [x["count_p"].get(kw, 0.0) for x in ext["count"]]
            out["label_p_range"] = [x["label_p"] for x in ext["label"]]
            out["remainder"] = _lse([r[5] for r in rows if r[6]])
        out["requests"], view.requests = view.requests, saved | view.requests
        return out

    def _same_choice(self, sf, f):
        """Whether a shadow's final choice is the actual one: its winner, variant, mask and selected basin (the one
        handed to the continuous stage as selected_model)."""
        (sd, sc), (ad, ac) = (sf["d"], sf["chosen"]), (f["d"], f["chosen"])
        if (sd["winner"], sd["variant"], sd["delta"]) != (ad["winner"], ad["variant"], ad["delta"]):
            return False
        if sc is None or ac is None:
            return sc is ac
        return R._same_basin(self.spec, sc, ac)

    def _decide(self) -> Assignment:
        spec, V, S = self.spec, self.view, self.sview
        inc_v = spec.inc_variant
        f = self._final(V)
        d = f["d"]
        k, slots = d["winner"] or (self.n0, self.inc)
        chosen = f["chosen"]
        shadow = None
        if S is not None:
            sf = self._final(S)
            self._shadow("final", "decision", True, self._same_choice(sf, f) if sf["settled"] else None)
            shadow = {"basins": self._shadow_basins(), "decisions": list(self.decisions),
                      "first_divergence": self.first_divergence}
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
        unpriced = [(j, p) for j, p in self.current.values() if not _priced(p)]
        limited = (any((w["stopped"] or "").split(":")[0] in ("unresolved", "no_structure") for w in walks)
                   or any(self.meta[j.key]["purpose"] == "seed" for j, _p in unpriced)
                   or bool(full and self.full["truncated"]))

        def fin(x):
            return None if x is None or x == _NEG else x
        cands = []
        for key, e in self.ledger.items():
            wgt, cp = d["weights"].get(key, 0.0), d["count_p"].get(key[0], 0.0)
            face = V.face[key] if S is None else S.face.get(key)
            cands.append({"count": key[0], "slots": key[1], "ln_z": fin(V.lo[key]), "status": e["status"],
                          "reason": None, "weight": wgt, "weight_in_count": wgt / cp if cp > 0 else 0.0,
                          "source": e["source"], "key": e["key"], "basins": len(e["basins"]),
                          "price": "screen" if V.face[key] is not None else "full", "ln_z_screen": fin(face),
                          **R._loss_record(spec, key[1])})
        for j, p in unpriced:
            lab = _labels(j.count, j.slots, j.starts[0].modes)[0]
            cands.append({"count": j.count, "slots": lab, "ln_z": None, "status": p.status, "reason": p.reason,
                          "weight": 0.0, "weight_in_count": 0.0, "source": self.meta[j.key]["source"], "key": j.key,
                          "basins": 0, "price": "screen" if j.depth == "screen" else "full", "ln_z_screen": None,
                          **R._loss_record(spec, lab)})
        mask_p = sorted((dl, p) for (c, s_, dl), p in d["mask_weights"].items() if (c, s_) == (k, slots))
        states = {c: self._state(c) for c in sorted(self.by_count)}
        tot = {c: v for c, v in V.tot_lo.items() if v > _NEG}
        best = max(tot.values()) if tot else None
        counts = {}
        for c, st in states.items():
            bk, _det = V.best_key(c)
            counts[str(c)] = {"state": "priced" if st == "priced" else "unresolved", "ln_total": tot.get(c),
                              "ln_total_optimistic": fin(V.tot_hi.get(c)), "labellings": len(V.by_k.get(c, ())),
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
                # screened rows left at the decision: the probabilities are bounds, not values
                "partial": f["partial"], "count_p_range": f["count_p_range"], "label_p_range": f["label_p_range"],
                "close_calls": [[c[0], list(c[1])] for c in d["close_calls"]], "close_counts": d["close_counts"],
                "mask": {"incoming": list(spec.inc_delta), "chosen": list(d["delta"]),
                         "weights": [{"delta": list(dl), "p": p} for dl, p in mask_p]},
                "variant": {"incoming": None if inc_v is None else list(inc_v),
                            "chosen": None if d["variant"] is None else list(d["variant"])}}}
        search = {"version": 2, "domain": "full" if full else "adaptive", "margin_nats": self.margin,
                  "seeds": self.seeds, "counts": counts,
                  "walks": [{x: w[x] for x in ("origin", "direction", "reached", "stopped", "behind_nats")}
                            for w in walks],
                  "labelling": self._labelling_record(tot), "tried": dict(self.tried), "full": self.full,
                  "screens": {"mode": self.screens, "allowance_nats": SCREEN_ALLOWANCE_NATS,
                              "promote_wave": PROMOTE_WAVE, "screened": self.tried["screens"],
                              "promoted": self.tried["promotions"], "remainder_nats": f["remainder"]},
                  "shadow": shadow}
        return Assignment(
            unit=self.snap.unit, snapshot_id=self.snap.stage_id, count=k, slots=slots, origins=spec.origins(slots),
            factors=spec.incident(slots), discrete="priced",
            triggers=tuple(dict.fromkeys("seed:" + sd["source"] for sd in self.seeds)), candidates=tuple(cands),
            coverage=coverage, search_limited=limited, changed=bool(d["changed"]), reasons=tuple(reasons),
            selected_model=chosen[4] if chosen else None, mask_only=bool(d["mask_only"]), search=search)

    def _shadow_basins(self):
        """[count, labels, full term or None, screen term] for every split basin of a shadow screen, paired with the
        basin of the same candidate's full price that is the same physical basin."""
        out = []
        for c, (sj, sp) in self.shadow_current.items():
            if not _priced(sp):
                continue
            fp = self.current[c][1]
            full = [_canon(b) for b in fp.basins] if _priced(fp) else []
            for i, b in enumerate(sp.basins):
                if i < len(sp.screen_terms) and sp.screen_terms[i] is not None:
                    b = _canon(b)
                    m = next((x for x in full if R._same_basin(self.spec, x, b)), None)
                    out.append([sj.count, [x[0] for x in b[2]], None if m is None else float(m[0]), float(b[0])])
        return out

    def _walk(self, w):
        """A walk that ran into a step another walk had taken ends where that walk ended."""
        while w.get("joined") is not None:
            w = {**self.walks[w["joined"]], "origin": w["origin"], "direction": w["direction"]}
        return w

    def _labelling_record(self, tot):
        best = max(tot.values()) if tot else None
        out = {}
        for k in sorted(self.view.by_k):
            keys = [ks for key, ks in self.keys.items() if key[0] == k]
            moves = [p for c, (j, p) in self.current.items() if self.meta[c]["purpose"] == "move" and j.count == k]
            stops = {ks["stopped"] for ks in keys}
            if best is None or tot.get(k, _NEG) < best - self.margin:
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

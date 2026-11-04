"""A fake numerical core for the batch runner's tests and S1U13's timing tool (public name: ``_inter_fakes``).

Module-level functions with the core's signatures (``isar.analyze.coupled.refit``): ``held_from_fit``,
``evidence_assignment``, ``fixed_assignment``, ``refit_step``, ``open_search``, ``price``, ``refit``,
``stage_factors``, plus the real ``total_objective`` and ``seam_report``. A batch calls ``evidence_assignment`` and
``refit_step`` (``fixed_assignment`` stays for the tests of the unpriced set); ``open_search`` (the
C-search core's entry point: a ``_FakeSearch`` per spectrum, driven by the knobs below) and ``price`` are for the
tests of the search itself and of ``_Run.assign``.
They are picklable by name, so the runner's pool workers import this module the way they import the real core
(``tests/session`` must be on the path: the tests put it there, and spawned workers inherit it). Pass the module
itself as ``run_batch(..., core=_inter_fakes)``; ``run_batch`` below is that call, for ``--inter-target``.

Every behaviour is read from the fit record the refit starts from (``Snapshot.own_record["fake"]``), so it
travels into pool workers with the snapshot and onto the refit's own record:

- ``toy``: ``"mean"`` (default; omega0 := mean of the held neighbours' values of its branch + 0.1),
  ``"average"`` (the quadratic toy: omega0 := (fast omega0 + sum of neighbour values) / (1 + their number)),
  ``"oscillate"`` (omega0 := fast omega0 + (sweep number mod 2): never settles), ``"keep"`` (omega0 := fast
  omega0: never moves);
- ``slots``: the stage-1 slot ids (default: the incoming branches); ``change_rounds``: rounds whose stage 1
  is priced and reports a changed assignment (``triggered``: priced every round, never changed);
- the search (a spectrum that is not triggered is done at once with nothing to price): ``iterations`` (default 1)
  passes of ``jobs_per_iteration`` (default 1) jobs each, ``starts`` (default 1) starts per job, then done;
  ``stopped`` (the result's ``coverage["stopped"]``, default ``{"down": "fake", "up": "fake"}``);
  ``search_raise`` (``"open"``, ``"next"``, ``"feed"`` or ``"result"``: that call raises); ``search_repeat`` (the
  second pass asks the first pass's jobs again);
- the price: ``fits`` (the optimizer fits it reports, default 1), ``price_sleep`` (seconds it takes, and reports),
  ``price_raise`` (job indexes within a pass whose price raises ``RuntimeError("boom")``), ``price_none`` (every
  price is ``ln_z None``, ``no_evidence``). A job key is a hash of the unit, its own and its neighbours' energies,
  the slots and the pass and index, never a fit id: an unchanged spectrum reproduces its keys, a neighbour that
  moved changes them;
- ``fixed_raise``: ``evidence_assignment`` and ``fixed_assignment`` raise; ``slots`` also sets the fixed mode set
  (default: the incoming branches);
- ``pricing``: the scripted ``search["pricing"]`` of ``evidence_assignment`` (C-priced's form). Absent: every applied
  change is a tried row, ``entered`` with ``delta_ln_z`` +5.0. A dict may give ``tried`` (rows with ``id``, ``kind``,
  ``outcome``, and optionally ``delta_ln_z``, ``why``; the rest is filled in) and ``incumbent`` (a dict or None);
  ``unresolved_splits`` follows from the rows. ``slots`` still decides the modes and ``search["applied"]``;
- ``restarts``: restart calls the fit step asks for (default 0), ``restart_sleep`` seconds each takes,
  ``J`` the main fit's J and ``restart_J`` each restart's (default 0.0: a tie, so the main fit is kept, as the core
  breaks one), ``step_raise`` (``"fit"``, ``"restart"`` or ``"finish"``): that step raises;
- ``fail``: the refit fails; ``sleep``: seconds the fit step (a refit) sleeps;
- ``pid_dir``: each refit writes ``pid-<pid>`` there; ``hang``: then it sleeps until killed.

A stored record's peaks may carry ``omega0`` and ``branch``; a missing omega0 reads 5.0 meV and a missing branch
``"B"``, so every spectrum of a fixture shares one branch. No numbers here are measured.

``NullLinker`` is the runner's linker with no linking: every mode keeps the branch id it has, nothing is predicted,
changed or tied. ``ScriptedLinker`` answers the stage links from a script (``label(unit, stage, held)`` gives a unit's
branch id per mode, None = on no branch). Both save through the real writer, so a batch that uses either leaves real
link records.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import time
from dataclasses import replace
from pathlib import Path

from isar.analyze.coupled.engine import Factor
from isar.analyze.coupled.refit import FitStage, seam_report, total_objective  # noqa: F401  (the core's own, re-exported)
from isar.analyze.coupled.types import Assignment, Held, HeldMode, Priced, PricingJob, RefitResult, Start
from isar.session.inter_spectra.runner import BatchItem  # noqa: F401  (the service builds the item from the target's module)

WINDOW = (0.0, 20.0)


def _cfg(snapshot) -> dict:
    return dict((snapshot.own_record or {}).get("fake") or {})


def _number(sid: str, word: str) -> int:
    m = re.search(rf":{word}(\d+)$", sid)
    return int(m.group(1)) if m else 0


def held_from_fit(record: dict, *, unit: str, fit: str, inputs, context, T, branch_ids=None, dormant=()) -> Held:
    peaks = record.get("peaks") or [{}]
    modes = tuple(sorted((HeldMode(branch=str(p.get("branch") or "B"), omega0=float(p.get("omega0", 5.0)),
                                   gamma=float(p.get("Gamma", 0.5)), height=float(p.get("height", 1.0)), guard=0.1,
                                   guard_source="solo" if fit == "fast" else "max",
                                   status="determined_by_own_data", regime="underdamped", pinned=False)
                          for p in peaks), key=lambda m: m.omega0))
    return Held(unit=unit, fit=fit, inputs=inputs, window=WINDOW, modes=modes, dormant=tuple(dormant), T=T)


def _slots(snapshot) -> tuple:
    cfg = _cfg(snapshot)
    return tuple(cfg["slots"]) if "slots" in cfg else tuple(m.branch for m in snapshot.own.modes)


def _factors(snapshot, slots) -> tuple:
    out = []
    for ti in snapshot.terms:
        t, members = ti.term, tuple(ti.term.members)
        for b in slots:
            if b is None or not all(any(m.branch == b for m in h.modes) for h in ti.held):
                continue
            if t.kind == "curvature":
                a = (-(1.0 - t.t_frac), 1.0, -t.t_frac)
            else:
                a = (1.0, -1.0)
            out.append(Factor(f"{t.kind}:{'|'.join(members)}:{b}", b, members, a, s=1.0, C=12.0,
                              kind={"T-change": "dT", "q-change": "dq", "curvature": "curv"}[t.kind]))
    return tuple(out)


def stage_factors(snapshot) -> dict:
    return {f.factor_id: f for f in _factors(snapshot, tuple(m.branch for m in snapshot.own.modes))}


def _applied(snapshot, slots) -> list:
    """What the real search records: the listed adds whose branches the chosen slots carry."""
    L = snapshot.links
    return [] if L is None else [i for i, c in enumerate(L.changes) if c.kind == "add" and all(b in slots for b in c.branches)]


def _assignment(snapshot, slots, *, discrete: str, changed: bool, stopped=None) -> Assignment:
    cands = ({"count": len(slots), "slots": slots, "ln_z": 0.0, "status": "ok", "weight": 1.0},) \
        if discrete == "priced" else ()
    return Assignment(unit=snapshot.unit, snapshot_id=snapshot.stage_id, count=len(slots), slots=slots,
                      origins=tuple("matched" if b is not None else "new_branch" for b in slots),
                      factors=_factors(snapshot, slots), discrete=discrete, triggers=("fake",) if changed else (),
                      candidates=cands, coverage={} if stopped is None else {"stopped": stopped},
                      search_limited=False, changed=changed, reasons=(), search={"applied": _applied(snapshot, slots)})


def _pricing(snapshot, applied) -> dict:
    """The fake's ``search["pricing"]``: the ``pricing`` knob's rows filled in, else one entered row per applied change."""
    from isar.analyze.coupled.search import change_id
    cfg = _cfg(snapshot).get("pricing")
    if cfg is None:
        rows = [{"id": change_id(snapshot.links.changes[i]), "kind": snapshot.links.changes[i].kind,
                 "outcome": "entered", "delta_ln_z": 5.0} for i in applied]
    else:
        rows = [dict(r) for r in cfg.get("tried", ())]
    tried = []
    for n, r in enumerate(rows):
        d = r.get("delta_ln_z")
        outcome = r["outcome"]
        tried.append({"change": r.get("change", n), "id": r["id"], "kind": r["kind"], "mode": r.get("mode"),
                      "branches": list(r.get("branches", ())), "intensity": 1.0, "count": r.get("count", 0),
                      "ln_z": None if outcome == "no_answer" else -100.0,
                      "delta_ln_z": None if outcome == "no_answer" else d,
                      "outcome": outcome, "why": r.get("why", "undetermined: scripted") if outcome == "no_answer" else None,
                      "fits": 1, "seconds": 0.0})
    inc = {"count": len(snapshot.own.modes), "ln_z": -110.0, "status": "ok", "why": None, "fits": 1, "seconds": 0.0}
    if cfg is not None and "incumbent" in cfg:
        inc = cfg["incumbent"]
    elif not tried:
        inc = None
    return {"hysteresis_nats": 0.1, "incumbent": inc, "tried": tried,
            "unresolved_splits": [{k: r[k] for k in ("change", "id", "mode", "branches", "why")}
                                  for r in tried if r["kind"] == "split" and r["outcome"] == "no_answer"],
            "final_count": 0, "final_ln_z": None}


def _triggered(snapshot) -> bool:
    cfg = _cfg(snapshot)
    return bool(cfg.get("triggered")) or _number(snapshot.stage_id, "round") in cfg.get("change_rounds", ())


def _key(snapshot, slots, p: int = 0, i: int = 0) -> str:
    held = [(h.unit, [m.omega0 for m in h.modes]) for ti in snapshot.terms for h in ti.held]
    own = [m.omega0 for m in snapshot.own.modes]
    return hashlib.sha256(repr((snapshot.unit, own, slots, held, p, i)).encode()).hexdigest()


def _changed(snapshot) -> bool:
    return _number(snapshot.stage_id, "round") in _cfg(snapshot).get("change_rounds", ())


def _index(snapshot, job) -> int:
    """The index within its pass of one of this spectrum's jobs (the key is a hash, so find it by regenerating)."""
    cfg, slots = _cfg(snapshot), _slots(snapshot)
    for p in range(int(cfg.get("iterations", 1))):
        for i in range(int(cfg.get("jobs_per_iteration", 1))):
            if _key(snapshot, slots, p, i) == job.key:
                return i
    return 0


def price(snapshot, job) -> Priced:
    cfg = _cfg(snapshot)
    if _index(snapshot, job) in cfg.get("price_raise", ()):
        raise RuntimeError("boom")
    secs = float(cfg.get("price_sleep", 0.0))
    if secs:
        time.sleep(secs)
    if cfg.get("price_none"):
        return Priced(job.key, job.count, tuple(job.slots), None, "no_evidence", None, "fake no evidence", 1,
                      secs or 0.001)
    return Priced(job.key, job.count, tuple(job.slots), 0.0, "ok", None, None, int(cfg.get("fits", 1)),
                  secs or 0.001)


class _FakeSearch:
    """The fake core's Search: a triggered spectrum asks ``jobs_per_iteration`` jobs for ``iterations`` passes and is
    done once the last is fed; any other is done at once. ``search_raise`` makes one call raise."""

    def __init__(self, snapshot) -> None:
        cfg = _cfg(snapshot)
        self._raise = cfg.get("search_raise")
        if self._raise == "open":
            raise RuntimeError("the fake search failed to open")
        self._snapshot, self._cfg = snapshot, cfg
        self._slots = _slots(snapshot)
        self._passes = int(cfg.get("iterations", 1)) if _triggered(snapshot) else 0
        self._pass = 0
        self._outstanding: tuple = ()

    def _jobs(self) -> tuple:
        snap, cfg, slots = self._snapshot, self._cfg, self._slots
        own = snap.own.modes
        start = Start("own", tuple((m.omega0, m.gamma, m.height) for m in
                                   (own[min(i, len(own) - 1)] for i in range(len(slots)))))
        p = 0 if (self._pass == 1 and cfg.get("search_repeat")) else self._pass
        return tuple(PricingJob(snap.unit, snap.snapshot_id, len(slots), slots, _key(snap, slots, p, i),
                                starts=(start,) * int(cfg.get("starts", 1)))
                     for i in range(int(cfg.get("jobs_per_iteration", 1))))

    def next_jobs(self) -> tuple:
        if self._raise == "next":
            raise RuntimeError("the fake search failed to ask")
        if self.done:
            return ()
        self._outstanding = self._outstanding or self._jobs()
        return self._outstanding

    def feed(self, priced) -> None:
        jobs = self._outstanding
        if self._raise == "feed":
            raise RuntimeError("the fake search failed to read")
        if self.done or not jobs or len(priced) != len(jobs) or any(p.key != j.key for p, j in zip(priced, jobs)):
            raise ValueError("feed takes one Priced per job of the last next_jobs(), in that order")
        self._pass += 1
        self._outstanding = ()

    @property
    def done(self) -> bool:
        return self._pass >= self._passes

    def result(self) -> Assignment:
        if self._raise == "result":
            raise RuntimeError("the fake search failed to answer")
        if not self.done:
            raise RuntimeError("result() before the search is done")
        if self._passes == 0:
            return _assignment(self._snapshot, self._slots, discrete="skipped", changed=False)
        return _assignment(self._snapshot, self._slots, discrete="priced", changed=_changed(self._snapshot),
                           stopped=self._cfg.get("stopped", {"down": "fake", "up": "fake"}))


def open_search(snapshot) -> _FakeSearch:
    return _FakeSearch(snapshot)


def fixed_assignment(snapshot, *, apply_changes: bool) -> Assignment:
    """The fake's frozen mode set: the ``slots`` knob or the incoming branches, whichever the round."""
    if _cfg(snapshot).get("fixed_raise"):
        raise ValueError("no incoming model")
    slots = _slots(snapshot)
    return _assignment(snapshot, slots, discrete="fixed",
                       changed=slots != tuple(m.branch for m in snapshot.own.modes))


def evidence_assignment(snapshot) -> Assignment:
    """The fake's priced mode set: what ``fixed_assignment`` gives, with a version-2 ``search`` carrying the scripted
    ``pricing`` (``changed`` and ``search["applied"]`` still follow ``slots``)."""
    a = fixed_assignment(snapshot, apply_changes=True)
    applied = a.search["applied"]
    pricing = _pricing(snapshot, applied)
    pricing["final_count"] = a.count
    search = {"version": 2, "domain": "evidence", "link": None, "applied": applied, "skipped": [], "pricing": pricing}
    return replace(a, search=search)


def _own(snapshot, a) -> None:
    if a is None or a.unit != snapshot.unit or a.snapshot_id != snapshot.stage_id:
        raise ValueError("the fake core needs the stage's own assignment")


def _step_raise(snapshot, step: str) -> None:
    if _cfg(snapshot).get("step_raise") == step:
        raise RuntimeError(f"the fake {step} step failed")


def refit_step(step: str, *args):
    """The core's three pool steps (C-refit-steps): ``fit`` asks ``restarts`` restart calls, each ``restart``
    returns its start and its J, ``finish`` keeps the lowest J (``_kept``) and builds the result."""
    snapshot, a = args[0], args[1]
    _own(snapshot, a)
    _step_raise(snapshot, step)
    cfg = _cfg(snapshot)
    if step == "fit":
        t0 = time.perf_counter()
        if cfg.get("pid_dir"):
            Path(cfg["pid_dir"], f"pid-{os.getpid()}").write_text("", encoding="utf-8")
            while cfg.get("hang"):                   # a pool worker that keeps its core until killed
                time.sleep(0.05)
        if cfg.get("sleep"):
            time.sleep(float(cfg["sleep"]))
        return FitStage(snapshot.unit, snapshot.stage_id, (), (), float(cfg.get("J", 0.0)),
                        tuple((float(i),) for i in range(int(cfg.get("restarts", 0)))), None,
                        {"continuous_s": time.perf_counter() - t0})
    if step == "restart":
        if cfg.get("restart_sleep"):
            time.sleep(float(cfg["restart_sleep"]))
        n, Js = int(args[3][0]), list(cfg.get("restart_J", ()))     # the fit step numbers its starts 0, 1, ...
        return args[3], float(Js[n]) if n < len(Js) else 0.0
    if step == "finish":
        return _result(snapshot, a, 1, float(args[2].timing["continuous_s"]), float(args[4]) if len(args) > 4 else 0.0,
                       _kept(args[2], args[3]))
    raise ValueError(f"unknown refit step {step!r}")


def refit(snapshot, *, stage, assignment=None, cached={}, workers: int = 1) -> RefitResult:
    """The steps run inline, as the core's ``refit`` does."""
    if stage != "continuous":
        raise ValueError("the fake core needs the stage's own assignment")
    st = refit_step("fit", snapshot, assignment)
    got = [refit_step("restart", snapshot, assignment, st.model_key, t) for t in st.restarts]
    return refit_step("finish", snapshot, assignment, st, got, 0.0)


def _kept(stage, results) -> str:
    """The fit the core keeps (``refit._finish``): the lowest J of the main fit and the restarts, a tie keeping the earlier."""
    kept, best = "main", stage.J
    for n, (theta, J) in enumerate(results):
        if theta is not None and math.isfinite(J) and J < best:
            kept, best = f"restart{n}", J
    return kept


def _result(snapshot, a, workers: int, continuous_s: float, restart_s: float, kept: str) -> RefitResult:
    cfg = _cfg(snapshot)
    fast = snapshot.fast or snapshot.own
    seen = {h.unit: [m.omega0 for m in h.modes] for ti in snapshot.terms for h in ti.held}
    modes, energies, peaks = [], {}, []
    for i, b in enumerate(a.slots):
        branch = b if b is not None else f"{snapshot.fit_id}#{i}"
        vals = [m.omega0 for ti in snapshot.terms for h in ti.held for m in h.modes if m.branch == b]
        f0 = fast.modes[min(i, len(fast.modes) - 1)].omega0
        toy = cfg.get("toy", "mean")
        if toy == "average":
            e = (f0 + sum(vals)) / (1 + len(vals))
        elif toy == "oscillate":
            e = f0 + _number(snapshot.snapshot_id, "sweep") % 2
        elif toy == "keep":
            e = f0
        else:
            e = sum(vals) / len(vals) + 0.1 if vals else snapshot.own.modes[min(i, len(snapshot.own.modes) - 1)].omega0
        modes.append(HeldMode(branch, e, 0.5, 1.0, 0.1, "max", "determined_by_own_data", "underdamped", False))
        energies[branch] = e
        peaks.append({"omega0": e, "Gamma": 0.5, "height": 1.0, "branch": branch})
    failed = bool(cfg.get("fail"))
    fit = None if failed else {"schema": "isar.fast-pass/1", "source": "fake", "peaks": peaks, "fake": cfg,
                               "workers": workers, "seen": seen, "energy_support_meV": list(WINDOW), "kept": kept}
    D = sum((e - v) ** 2 for b, e in energies.items()
            for ti in snapshot.terms for h in ti.held for m in h.modes if m.branch == b for v in [m.omega0])
    return RefitResult(
        unit=snapshot.unit, snapshot_id=snapshot.snapshot_id, status="failed" if failed else "accepted",
        reason="the fake was told to fail" if failed else None, assignment=a, fit=fit,
        modes=() if failed else tuple(modes), D=None if failed else float(D), J=None if failed else float(D),
        P={}, U={}, unary=0.0, energies={} if failed else energies, R=0.0, R_signed=0.0, F=0.0,
        delta_fast={"count": 0, "omega0": {}}, flag={"score": 0.0, "flagged": False, "reasons": []},
        why_changed="the toy moved it", overlay=None if failed else {"fast_pass": fit, "n_modes": len(modes)},
        overlay_unavailable="the refit failed" if failed else None, covariance=None, errors="unavailable",
        normalisers={}, starts=1,
        timing={"assign_s": 0.0, "likelihood_s": 0.0, "continuous_s": continuous_s, "restart_s": restart_s,
                "uncertainty_s": 0.0, "release_s": 0.0}, priced=(), cache_hits=0)


def _link_result(units, labels, *, lone=None, predictions=None, changes=None, ties=(), next_name=1, stamp="s"):
    """A real ``LinkResult`` over ``units`` (unit -> Held), ``labels`` unit -> branch id (or None) per mode."""
    from isar.analyze.coupled import linking as lk
    members: dict[str, list] = {}
    for u in sorted(labels):
        for i, b in enumerate(labels[u]):
            if b is not None:
                members.setdefault(b, []).append((u, i))
    branches = tuple(lk.Branch(b, f"B{next_name + n}", tuple(m)) for n, (b, m) in enumerate(sorted(members.items())))
    return lk.LinkResult(
        schema=lk.LINK_SCHEMA, made_from={u: (h.fit, h.inputs) for u, h in sorted(units.items())},
        stamps={u: f"{stamp}:{h.fit}" for u, h in sorted(units.items())}, branches=branches,
        labels={u: tuple(labels[u]) for u in sorted(labels)},
        lone={u: tuple(None for _ in labels[u]) for u in sorted(labels)} if lone is None else lone,
        peak_E={u: tuple(m.omega0 for m in units[u].modes) for u in sorted(labels)},
        settled={u: tuple(b is not None for b in labels[u]) for u in sorted(labels)},
        predictions={u: () for u in sorted(labels)} if predictions is None else predictions,
        changes={u: () for u in sorted(labels)} if changes is None else changes, ties=tuple(ties),
        contradictions=(), markers=(), excluded={}, settings={}, name_start=next_name,
        next_name=next_name + len(branches))


class NullLinker:
    """No linking: every spectrum keeps the branch ids it carries (the relabelling is the identity)."""

    def __init__(self) -> None:
        self.last = None
        self.calls: list = []

    def begin(self, root, units):
        return None, 1

    def stage(self, root, state, *, links, anchors, source, settings, previous, next_name):
        self.calls.append(("stage", sorted(state), tuple(anchors), previous, next_name))
        self.last = _link_result(state, {u: [m.branch for m in h.modes] for u, h in state.items()},
                                 next_name=next_name)
        return self.last

    def final(self, root, scans):
        self.calls.append(("final", list(scans)))
        return _link_result({}, {}, next_name=1)

    def stored(self, root, link_id):
        return self._read(root, link_id)

    @staticmethod
    def _read(root, link_id):
        from isar.session.inter_spectra import records
        for line in records.read_links(root):
            if line.link == link_id:
                return records.read_link_payload(root, line)
        raise ValueError(link_id)


class ScriptedLinker(NullLinker):
    """``label(unit, stage, held) -> tuple`` gives the branch id of each mode of a spectrum at a stage (stage is
    ``ring<r>`` / ``round<n>``); ``extra(stage, labels, state)`` may return keyword arguments of ``_link_result``
    (lone, predictions, changes, ties); ``boom`` names a stage whose link raises."""

    def __init__(self, label, extra=None, boom=None) -> None:
        super().__init__()
        self.label, self.extra, self.boom, self.stages = label, extra, boom, []

    def stage(self, root, state, *, links, anchors, source, settings, previous, next_name):
        self.calls.append(("stage", sorted(state), tuple(anchors), previous, next_name))
        if self.boom is not None and len(self.stages) + 1 == self.boom:
            raise RuntimeError("the scripted link failed")
        labels = {u: tuple(self.label(u, len(self.stages) + 1, h)) for u, h in state.items()}
        kw = {} if self.extra is None else self.extra(len(self.stages) + 1, labels, state)
        self.stages.append(labels)
        self.last = _link_result(state, labels, next_name=next_name, **kw)
        return self.last

    def final(self, root, scans):
        return _link_result({}, {}, next_name=1)


def run_batch(root, item, *, progress, emit, ack, members=None, linker=None) -> None:
    """The real runner on this fake core: the service's ``--inter-target _inter_fakes:run_batch``."""
    import sys
    from isar.session.inter_spectra import runner
    runner.run_batch(root, item, progress=progress, emit=emit, ack=ack, core=sys.modules[__name__], members=members,
                     linker=NullLinker() if linker is None else linker)

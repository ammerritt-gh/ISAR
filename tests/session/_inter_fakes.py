"""A fake numerical core for the batch runner's tests and S1U13's timing tool (public name: ``_inter_fakes``).

Module-level functions with the core's signatures (``isar.analyze.coupled.refit``): ``held_from_fit``,
``assign_jobs``, ``price``, ``assign_decide``, ``refit``, plus the real ``total_objective`` and ``seam_report``.
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
- ``fail``: the refit fails; ``sleep``: seconds each refit sleeps;
- ``pid_dir``: each refit writes ``pid-<pid>`` there; ``hang``: then it sleeps until killed.

A stored record's peaks may carry ``omega0`` and ``branch``; a missing omega0 reads 5.0 meV and a missing branch
``"B"``, so every spectrum of a fixture shares one branch. No numbers here are measured.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path

from isar.analyze.coupled.engine import Factor
from isar.analyze.coupled.refit import seam_report, total_objective  # noqa: F401  (the core's own, re-exported)
from isar.analyze.coupled.types import Assignment, Held, HeldMode, Priced, PricingJob, RefitResult
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


def _assignment(snapshot, slots, *, discrete: str, changed: bool) -> Assignment:
    cands = ({"count": len(slots), "slots": slots, "ln_z": 0.0, "status": "ok", "weight": 1.0},) \
        if discrete == "priced" else ()
    return Assignment(unit=snapshot.unit, snapshot_id=snapshot.stage_id, count=len(slots), slots=slots,
                      origins=tuple("matched" if b is not None else "new_branch" for b in slots),
                      factors=_factors(snapshot, slots), discrete=discrete, triggers=("fake",) if changed else (),
                      candidates=cands, coverage={}, search_limited=False, changed=changed, reasons=())


def _triggered(snapshot) -> bool:
    cfg = _cfg(snapshot)
    return bool(cfg.get("triggered")) or _number(snapshot.stage_id, "round") in cfg.get("change_rounds", ())


def assign_jobs(snapshot, *, cached={}):
    slots = _slots(snapshot)
    if not _triggered(snapshot):
        return _assignment(snapshot, slots, discrete="skipped", changed=False), ()
    held = [(h.unit, h.fit, [m.omega0 for m in h.modes]) for ti in snapshot.terms for h in ti.held]
    key = hashlib.sha256(repr((snapshot.unit, snapshot.own.fit, slots, held)).encode()).hexdigest()
    job = PricingJob(snapshot.unit, snapshot.snapshot_id, len(slots), slots, key)
    return None, (() if key in cached else (job,))


def price(snapshot, job) -> Priced:
    return Priced(job.key, job.count, tuple(job.slots), 0.0, "ok", None, None, 1, 0.001)


def assign_decide(snapshot, priced) -> Assignment:
    changed = _number(snapshot.stage_id, "round") in _cfg(snapshot).get("change_rounds", ())
    return _assignment(snapshot, _slots(snapshot), discrete="priced", changed=changed)


def refit(snapshot, *, stage, assignment=None, cached={}, workers: int = 1) -> RefitResult:
    t0 = time.perf_counter()
    cfg = _cfg(snapshot)
    if cfg.get("pid_dir"):
        Path(cfg["pid_dir"], f"pid-{os.getpid()}").write_text("", encoding="utf-8")
        while cfg.get("hang"):                       # a pool worker that keeps its core until killed
            time.sleep(0.05)
    if cfg.get("sleep"):
        time.sleep(float(cfg["sleep"]))
    a = assignment
    if stage != "continuous" or a is None or a.unit != snapshot.unit or a.snapshot_id != snapshot.stage_id:
        raise ValueError("the fake core needs the stage's own assignment")
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
                               "workers": workers, "seen": seen, "energy_support_meV": list(WINDOW)}
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
        timing={"assign_s": 0.0, "likelihood_s": 0.0, "continuous_s": time.perf_counter() - t0,
                "uncertainty_s": 0.0, "release_s": 0.0}, priced=(), cache_hits=0)


def run_batch(root, item, *, progress, emit, ack) -> None:
    """The real runner on this fake core: the service's ``--inter-target _inter_fakes:run_batch``."""
    import sys
    from isar.session.inter_spectra import runner
    runner.run_batch(root, item, progress=progress, emit=emit, ack=ack, core=sys.modules[__name__])

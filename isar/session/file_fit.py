"""One analyzer file fitted fast or deep from the experiment's latest saved inputs (C-file-fit).

The analysis queue's item: it runs in the item's own process, reads the experiment once at its start (card,
ledger, intake entries, curves: the freeze), prepares the unit exactly as ``isar run`` does (the same parse,
reduction, start point and hashes) and fits it with the experiment's fitting choices and declared
hypotheses. Pooled masters, branch identity and the dispersion prior take no part: the fit sees a card whose
dispersion prior is empty. Nothing is written here; every result leaves through ``emit`` as a C-payload
the writer (:func:`isar.session.writer.save_results`) applies.

A deep item builds on the stored fast pass when it is current, and otherwise runs the fast stage first and
emits it before any deep work. Its sampling pool is sized to the distinct models in the combined initial stage
(:func:`isar.analyze.deep_pass.stage_width`); when that is fewer cores than it was given it says so first
(status ``"cores"``).
"""
from __future__ import annotations

import copy
import logging
import os
import time
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

from isar.analyze import deep_pass
from isar.analyze.progress import ProgressFn
from isar.intake import curves, scans as intake_scans
from isar.reduce import combine, qc
from isar.schema import _json
from isar.session import (calibrations, currentness, deep_delivery, driver, guidelines as guide_mod, ingest,
                          scan_delivery, unit_data)
from isar.session.start_point import StartPoint
from isar.session.state import ScanEntry, canonical_hash, content_hash
from isar.session.workspace import Workspace

log = logging.getLogger(__name__)

SCHEMA = "isar.file-result/1"
STATUSES = ("done", "done_warning", "held", "failed", "already_current")
# status "attempt" (pass_kind "attempt") is internal to S1: the pre-fit record, never shown
# status "cores" (pass_kind "cores") is internal to S1: the service turns it into a "cores" event
SCAN_M = 50                  # isar run's per-scan bootstrap draws (cli/main.py), errors tier "fast"
_NO_ZERO = "no energy zero in calibration coverage"


@dataclass(frozen=True)
class FileItem:
    item_id: str
    unit_key: str            # "experiment:scan_number:detector" == ScanEntry.scan_id
    pass_kind: str           # "fast" | "deep"
    cores: int               # >= 1; deep: the most its sampling pool may use (it reports fewer: "cores"); fast: ignored beyond 1


@dataclass(frozen=True)
class FileResult:
    item_id: str
    unit_key: str
    pass_kind: str           # the pass THIS result is for ("fast" for a deep item's fast stage)
    status: str              # "done" | "done_warning" | "held" | "failed" | "already_current" | "attempt" | "cores"
    reason: str | None       # plain words; None only when status == "done", "attempt" or "cores"
    payload: dict | None     # C-payload; None for already_current, "cores" and a failure that produced nothing
    cores: int | None = None # status "cores" only: the cores this deep item will use


def fit_file(workspace_root: Path, item: FileItem, *, progress: ProgressFn | None,
             emit: Callable[[FileResult], None]) -> None:
    """Fit one analyzer file (``item.unit_key``) fast or deep; every result goes to ``emit``. Never raises
    for an analysis failure: it is logged and emitted as ``failed``."""
    try:
        _fit(Path(workspace_root), item, progress, emit)
    except Exception as exc:  # noqa: BLE001 — one item's failure is its result, never the service's
        log.exception("fit_file: %s pass of %s failed", item.pass_kind, item.unit_key)
        emit(FileResult(item.item_id, item.unit_key, item.pass_kind, "failed",
                        f"analysis error ({type(exc).__name__}: {exc})", None))


def _fit(root: Path, item: FileItem, progress, emit) -> None:
    def result(pass_kind, status, reason=None, payload=None, cores=None):
        emit(FileResult(item.item_id, item.unit_key, pass_kind, status, reason, payload, cores))

    exp, scan_s, det = item.unit_key.rsplit(":", 2)
    scan = int(scan_s)
    key = f"{exp}:{scan}"
    # The freeze's time orders saves by how new their inputs are (C-save). shortcut: wall clock across item
    # processes, a clock stepped back misorders two starts; take a sequence from the service if that bites.
    inputs_at = time.time_ns()
    card, session = Workspace.open(root).load()               # the freeze (design step 9)
    prev = session.get(item.unit_key)
    if prev is None:
        return result(item.pass_kind, "failed", f"{item.unit_key}: not read in yet")
    parsed = _parse_unit_record(card, session, prev, exp, scan)
    if parsed is None:
        where = (prev.provenance or {}).get("source_file")
        return result(item.pass_kind, "failed", f"the data file for this analyzer is no longer at {where}")
    rec, source_by_name = parsed
    combine.reduce_record(rec, card)
    qc.qc_record(rec, card)
    g = guide_mod.resolve(card)
    if det not in guide_mod.fit_detectors(g, card) or det not in rec.spectra:
        return result(item.pass_kind, "failed", f"the card's fitting choices leave {det} out")
    chash = content_hash(rec)
    tabulated = (card.policy.calibration or {}).get("method") == "tabulated_elastic"
    loaded = version = None
    if tabulated:
        folder, pattern, _source = curves.resolution_location(card, root)
        loaded = curves.load_curves(folder, pattern)
        body = {"method": "tabulated_elastic", "folder": None if loaded.folder is None else str(loaded.folder),
                "pattern": loaded.pattern, "library": loaded.fingerprint}
        version = body if calibrations._append_version_if_changed(card, dict(body)) else None
    nop_card = replace(card, state=replace(card.state, dispersion_prior=[]))     # no prior (design step 11)
    runcal = calibrations.calibration_view(nop_card)
    sp_q = rec.spectra[det].q
    q = [float(v) for v in sp_q] if sp_q is not None else None
    seed = 6000 + scan
    sample = driver._sample_of(rec, card)            # the effective record keeps the experiment id
    sp = up = None
    if intake_scans.applies(card):
        sp = StartPoint(root, nop_card, g, runcal, parsed={key: rec}, rows={key: intake_scans.scan_row(rec, chash)},
                        data_revision={key: chash},
                        passes=("fast",) if item.pass_kind == "fast" else ("fast", "deep"), run_id="",
                        session=session, is_fresh=None, write_stub=None, loaded=loaded)
        up = sp.plan([(rec, det, q, None, sample, item.unit_key, chash, "", seed)])[0]
        task, eff, view = up.task, sp.frozen[key], sp.views[key] or runcal
        now = currentness.UnitNow(chash, up.ghash, up.calfp, up.inputs_hash)
    else:
        T = unit_data.scan_temperature(rec)
        ghash = canonical_hash(guide_mod.guidelines_signature(g, scan_number=scan, q=q, T=T))
        task, eff, view = (rec, det, q, T, sample, item.unit_key, chash, ghash, seed), rec, runcal
        now = currentness.UnitNow(chash, ghash, calibrations.calibration_fingerprint(runcal, rec, det), None)

    request = currentness.queue_deep_request()
    have = currentness.judge(prev, det, now, request)
    if item.pass_kind in have:
        return result(item.pass_kind, "already_current", f"already has a current {item.pass_kind} pass")
    stored_cal = (card.state.tabulated_calibration.get(key) or {}).get("inputs_key")
    calibration = ({"scan": key, "state": sp.recalibrated[key], "replaces": stored_cal}
                   if sp is not None and key in sp.recalibrated else None)

    def fit_inputs_of(entry, pass_kind):
        if sp is None:
            return None
        finished = sp.finished_lines(entry)            # also stamps entry.provenance["fit_inputs"] (run "")
        return {"hash": up.inputs_hash, "fallbacks": list(entry.provenance["fit_inputs"]["fallbacks"]),
                "condition": up.condition is not None,
                "started": [ln for ln in up.started if ln["pass"] == pass_kind],
                "finished": [ln for ln in finished if ln["pass"] == pass_kind]}

    fast_entry = prev
    if "fast" not in have:
        if up is not None and up.condition is not None:          # a unit the user declared out: never fitted
            entry = up.condition
        else:
            result("attempt", "attempt", None, {"schema": SCHEMA, "unit": item.unit_key, "pass": "attempt",
                                                "measurement_record": driver._stub_record(card, task)})
            entry = scan_delivery._process_one(
                *task[:6], nop_card, g, view, driver._exclusion_ids(card), task[6], task[7], M=SCAN_M, seed=seed,
                errors_tier="fast", boot_workers=1, bayes_opts=driver._resolve_bayes_opts(), progress=progress)
        if det in eff.spectra:          # as the batch stamps; a dead analyzer keeps its import stamp
            entry.provenance = {**driver._core_provenance(eff, det, source_by_name), **(entry.provenance or {})}
        payload = {"schema": SCHEMA, "unit": item.unit_key, "pass": "fast",
                   "base": {"content_hash": prev.content_hash, "fast_id": None},
                   "deep": None, "fit_inputs": fit_inputs_of(entry, "fast"),
                   "calibration": calibration, "resolution_version": version, "inputs_at": inputs_at}
        payload["entry"] = _json.to_jsonable(entry)
        status, reason = _fast_status(entry, det, tabulated)
        result("fast", status, reason, payload)
        if item.pass_kind == "fast":
            return
        fast_entry, calibration, version = entry, None, None       # the fast payload carried them

    oc = (fast_entry.detectors or {}).get(det)
    if fast_entry.status != "fitted" or not oc or not oc.get("fast_pass"):
        status, reason = _fast_status(fast_entry, det, tabulated)
        return result("deep", "held" if status == "held" else "failed",
                      f"Deep pass not run: no fast-pass fit to build on ({reason})")
    n = min(item.cores, deep_pass.stage_width(oc["fast_pass"]))
    if n < item.cores:
        result("cores", "cores", cores=n)
    record, timing, stop = _deep_stage(fast_entry, oc, task, nop_card, g, view, driver._exclusion_ids(card),
                                       replace(request, workers=n), progress)
    if stop is not None:
        return result("deep", *stop)
    with_deep = copy.deepcopy(fast_entry)
    with_deep.detectors[det]["deep_pass"] = record
    payload = {"schema": SCHEMA, "unit": item.unit_key, "pass": "deep",
               "base": {"content_hash": fast_entry.content_hash, "fast_id": canonical_hash(oc["fast_pass"])},
               "entry": None, "deep": {"record": record, "timing": timing},
               "fit_inputs": fit_inputs_of(with_deep, "deep"),
               "calibration": calibration, "resolution_version": version, "inputs_at": inputs_at}
    if record.get("state") == "refused":
        return result("deep", "failed", f"Deep pass not run: {record.get('reason')}", payload)
    warning = currentness.result_warning({"deep_pass": record}, "deep")
    result("deep", "done_warning" if warning else "done", warning, payload)


def _fast_status(entry: ScanEntry, det: str, tabulated: bool) -> tuple[str, str | None]:
    """``(status, reason)`` of a fast result from its entry."""
    if entry.status == "fitted":
        oc = (entry.detectors or {}).get(det) or {}
        if not (oc.get("fast_pass") or {}).get("search_limit_reason"):
            return "done", None
        return "done_warning", currentness.result_warning(oc, "fast")
    reason = "; ".join(str(r) for r in entry.reasons or ()) or entry.status
    if entry.status.startswith("held") or entry.status == "excluded":
        if not tabulated:            # operator's answer 6 (a): the series is isar run's to build
            reason = reason.replace(_NO_ZERO, f"{_NO_ZERO} (energy-zero series are built by `isar run`; "
                                              "queued analysis does not build them)")
        return "held", reason
    return "failed", reason


def _parse_unit_record(card, session, prev: ScanEntry, exp: str, scan: int):
    """``(record, source_by_name)`` of the unit's scan, parsed from its file (or its file set) as the run
    parses it; None when the data is no longer there. The file is the ingest-stamped ``source_file``, else
    a manifest file of the scan (``isar run`` stamps a file set's stem there, not a path)."""
    prov = prev.provenance or {}
    src = Path(prov.get("source_file") or "")
    if not (src.is_absolute() and src.is_file()):
        src = next((p for p, row in session.files.items() if row.get("format") == "measurement"
                    and f"{exp}:{scan}" in (row.get("scan_ids") or ()) and Path(p).is_file()), None)
        if src is None:
            return None
        src = Path(src)
    folder = src.parent
    if prov.get("file_set"):
        found = ingest.discover([folder], card)
        fs = next((f for f in found.file_sets if f.key == prov["file_set"]
                   and Path(f.folder).resolve() == folder.resolve()), None)
        if fs is None:
            return None
        sub = ingest.IngestPlan(measurement_files=[p for p, _g in fs.measurements], file_sets=[fs],
                                sidecar_files=list(fs.sidecars))
        # the set's record names the set; its stamp keeps the path ingest gives it (the set's first file)
        source_by_name = {fs.key: str(Path(fs.measurements[0][0]).resolve())}
    else:
        sub = ingest.IngestPlan(measurement_files=[src])
        source_by_name = {}
    rec = next((r for r in ingest.parse_all(sub, card, session=session)
                if (str(r.experiment_id), r.scan_number) == (exp, scan) and r.scan_type == "measurement"), None)
    if rec is None:
        return None
    return rec, {**{Path(f).name: str(Path(f).resolve()) for f in sub.measurement_files}, **source_by_name}


def _deep_stage(fast_entry, oc, task, card, g, view, exclusions, request, progress) -> tuple:
    """``(record, timing, None)`` of the deep pass on the fast record ``oc`` builds on, run on
    ``request.workers`` cores; ``(None, None, (status, reason))`` when the unit cannot be sampled."""
    rec, det, q, T, sample, scan_id, chash, ghash, _seed = task
    base = dict(scan_id=scan_id, scan_number=rec.scan_number, scan_type=rec.scan_type,
                content_hash=chash, guidelines_hash=ghash)
    gated, calres = scan_delivery._gate_scan(rec, det, card, g, view, exclusions, base)
    if gated is not None:
        return None, None, ("held", "Deep pass not run: " + "; ".join(map(str, gated.reasons)))
    working = scan_delivery._resolve_prior_and_context(rec, det, q, T, sample, scan_id, card, g, calres, base)
    if isinstance(working, ScanEntry):
        return None, None, ("failed", "Deep pass not run: " + "; ".join(map(str, working.reasons)))
    ctx = working[0]
    # A stored fast pass a hard-fix escalation replaced (a batch with a prior) is refused, as _process_one does.
    hard_fix = any(str(r).startswith("hard-fix escalation")
                   for r in [*(oc.get("reasons") or ()), *(fast_entry.reasons or ())])
    pool = None
    if request.workers > 1:
        for name in driver._THREAD_ENV:     # one BLAS thread per process, as run_batch pins it
            os.environ.setdefault(name, "1")
        pool = ProcessPoolExecutor(max_workers=request.workers)
    try:
        record, timing = deep_delivery.deliver(
            {"fast_pass": oc["fast_pass"]}, ctx, request, executor=pool,
            refuse="hard-fix escalation replaced the fast-pass fit" if hard_fix else None, progress=progress)
    finally:
        if pool is not None:
            pool.shutdown()
    return scan_delivery._finite_json(record), scan_delivery._finite_json(timing), None

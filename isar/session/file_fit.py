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

``freeze``, ``prepare_scan`` and ``prepare_unit`` are that preparation, split so the batch runner reuses it;
``fit_context`` is the spectrum's context a coupled refit reads.
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


@dataclass(frozen=True)
class Frozen:
    root: Path
    card: object
    session: object
    inputs_at: int           # the freeze's time: orders saves by how new their inputs are (C-save)
    rows: dict | None = None # the import record's rows (isar.intake.scans.read_scans); None: unreadable


def freeze(root: Path) -> Frozen:
    """The experiment as it is now, read once: its card, ledger and import record, and the time the inputs
    were read at."""
    # The freeze's time orders saves by how new their inputs are (C-save). shortcut: wall clock across item
    # processes, a clock stepped back misorders two starts; take a sequence from the service if that bites.
    inputs_at = time.time_ns()
    card, session = Workspace.open(root).load()               # the freeze (design step 9)
    try:
        rows = intake_scans.read_scans(root)
    except (ValueError, OSError):
        log.exception("freeze: the import record of %s is unreadable; fits take the ledger's files", root)
        rows = None
    return Frozen(root, card, session, inputs_at, rows)


def prepare_scan(frozen: Frozen, exp: str, scan: int, prev: ScanEntry) -> tuple | str:
    """``(record, source_by_name)`` of the scan as ``isar run`` parses, reduces and QCs it, from the files the
    user gave for it; the reason (a str) when it cannot be prepared. Never None."""
    parsed = _parse_unit_record(frozen.card, frozen.session, frozen.rows, prev, exp, scan)
    if isinstance(parsed, str):
        return parsed
    rec, source_by_name = parsed
    combine.reduce_record(rec, frozen.card)
    qc.qc_record(rec, frozen.card)
    return rec, source_by_name


@dataclass(frozen=True)
class Prepared:
    """A unit ready to fit or to hand to a refit. Only ``reason`` is set when the unit cannot be prepared."""
    reason: str | None = None
    task: tuple | None = None        # (rec, det, q, T, sample, scan_id, chash, ghash, seed), as _process_one takes it
    eff: object = None               # the effective record the scan starts with
    view: object = None              # the scan's calibration view (the run's when it has none)
    now: object = None               # currentness.UnitNow: what the unit's result would be made from now
    card: object = None              # the card with no dispersion prior (design step 11)
    g: object = None                 # the guidelines
    exclusions: set | None = None
    version: dict | None = None      # the resolution version appended to the in-memory card, when new
    tabulated: bool = False          # the card's calibration method is tabulated_elastic
    source_by_name: dict | None = None
    sp: StartPoint | None = None     # the scan's start point (intake BL43 only)
    up: object = None                # the start point's plan of this unit (intake BL43 only)


def prepare_unit(frozen: Frozen, scan_prep, unit_key: str, passes) -> Prepared:
    """The unit as ``isar run`` prepares it: its fit task, calibration view and hashes, and its start point's
    plan for the ``passes`` given. Writes nothing; the tabulated-calibration version is appended in memory."""
    exp, scan_s, det = unit_key.rsplit(":", 2)
    scan = int(scan_s)
    key = f"{exp}:{scan}"
    card = frozen.card
    rec, source_by_name = scan_prep
    g = guide_mod.resolve(card)
    if det not in guide_mod.fit_detectors(g, card) or det not in rec.spectra:
        return Prepared(reason=f"the card's fitting choices leave {det} out")
    chash = content_hash(rec)
    tabulated = (card.policy.calibration or {}).get("method") == "tabulated_elastic"
    loaded = version = None
    if tabulated:
        folder, pattern, _source = curves.resolution_location(card, frozen.root)
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
        row = intake_scans.scan_row(rec, chash)
        gpi_entry = _gpi_entry(frozen.root, card, key, row, list(source_by_name.values()))
        sp = StartPoint(frozen.root, nop_card, g, runcal, parsed={key: rec}, rows={key: row},
                        data_revision={key: chash}, passes=passes, run_id="", session=frozen.session,
                        is_fresh=None, write_stub=None, loaded=loaded,
                        gpi={} if gpi_entry is None else {key: gpi_entry})
        up = sp.plan([(rec, det, q, None, sample, unit_key, chash, "", seed)])[0]
        task, eff, view = up.task, sp.frozen[key], sp.views[key] or runcal
        now = currentness.UnitNow(chash, up.ghash, up.calfp, up.inputs_hash)
    else:
        T = unit_data.scan_temperature(rec)
        ghash = canonical_hash(guide_mod.guidelines_signature(g, scan_number=scan, q=q, T=T))
        task, eff, view = (rec, det, q, T, sample, unit_key, chash, ghash, seed), rec, runcal
        now = currentness.UnitNow(chash, ghash, calibrations.calibration_fingerprint(runcal, rec, det), None)
    return Prepared(task=task, eff=eff, view=view, now=now, card=nop_card, g=g,
                    exclusions=driver._exclusion_ids(card), version=version, tabulated=tabulated,
                    source_by_name=source_by_name, sp=sp, up=up)


@dataclass(frozen=True)
class FitContext:
    """A refit's ``Snapshot.context`` (C-refit): the spectrum's fast-pass context with the card's own fast-pass
    settings and structure whitelist, as the fast pass fits it."""
    ctx: object                  # ScanContext
    settings: object             # FastPassSettings (guidelines.fast_pass_settings of the card)
    structures: tuple | None     # the card's structure labels for this scan; None: every structure


def fit_context(prep: Prepared) -> FitContext | tuple[str, str]:
    """The spectrum's fit context, or ``(status, reason)`` when it cannot be fitted: held or failed by the gate,
    or undersampled after the guideline masks. Resolved as the fast pass resolves it (prior, context, labels)."""
    rec, det, q, T, sample, scan_id, chash, ghash, _seed = prep.task
    base = dict(scan_id=scan_id, scan_number=rec.scan_number, scan_type=rec.scan_type,
                content_hash=chash, guidelines_hash=ghash)
    gated, calres = scan_delivery._gate_scan(rec, det, prep.card, prep.g, prep.view, prep.exclusions, base)
    if gated is not None:
        return "held", "; ".join(map(str, gated.reasons))
    working = scan_delivery._resolve_prior_and_context(rec, det, q, T, sample, scan_id, prep.card, prep.g,
                                                       calres, base)
    if isinstance(working, ScanEntry):
        return "failed", "; ".join(map(str, working.reasons))
    ctx, labels = working[0], working[6]
    settings = guide_mod.fast_pass_settings(prep.card)
    return FitContext(ctx, settings, scan_delivery._structure_whitelist(labels, ctx, settings.mode_ceiling)[0])


def _fit(root: Path, item: FileItem, progress, emit) -> None:
    def result(pass_kind, status, reason=None, payload=None, cores=None):
        emit(FileResult(item.item_id, item.unit_key, pass_kind, status, reason, payload, cores))

    exp, scan_s, det = item.unit_key.rsplit(":", 2)
    scan = int(scan_s)
    key = f"{exp}:{scan}"
    frozen = freeze(root)
    prev = frozen.session.get(item.unit_key)
    if prev is None:
        return result(item.pass_kind, "failed", f"{item.unit_key}: not read in yet")
    scan_prep = prepare_scan(frozen, exp, scan, prev)
    if isinstance(scan_prep, str):
        return result(item.pass_kind, "failed", scan_prep)
    prep = prepare_unit(frozen, scan_prep, item.unit_key, ("fast",) if item.pass_kind == "fast" else ("fast", "deep"))
    if prep.reason is not None:
        return result(item.pass_kind, "failed", prep.reason)
    task, eff, view, now = prep.task, prep.eff, prep.view, prep.now
    nop_card, g, exclusions = prep.card, prep.g, prep.exclusions
    sp, up, tabulated, version = prep.sp, prep.up, prep.tabulated, prep.version
    inputs_at = frozen.inputs_at
    request = currentness.queue_deep_request()
    have = currentness.judge(prev, det, now, request)
    if item.pass_kind in have:
        return result(item.pass_kind, "already_current", f"already has a current {item.pass_kind} pass")
    stored_cal = (frozen.card.state.tabulated_calibration.get(key) or {}).get("inputs_key")
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
                                                "measurement_record": driver._stub_record(frozen.card, task)})
            entry = scan_delivery._process_one(
                *task[:6], nop_card, g, view, exclusions, task[6], task[7], M=SCAN_M, seed=task[8],
                errors_tier="fast", boot_workers=1, bayes_opts=driver._resolve_bayes_opts(), progress=progress)
        if det in eff.spectra:          # as the batch stamps; a dead analyzer keeps its import stamp
            entry.provenance = {**driver._core_provenance(eff, det, prep.source_by_name), **(entry.provenance or {})}
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
    record, timing, stop = _deep_stage(fast_entry, oc, prep, replace(request, workers=n), progress)
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


def _gpi_entry(root: Path, card, key: str, row: dict, data_paths: list):
    """The queued fit's own input check: the gpi check over the stored rows with this scan's row rebuilt from
    the fresh parse, published as at import and run start; the scan's entry, or None (no file, or the check
    failed: no gpi value is used)."""
    from isar.intake import gpi
    try:
        check = gpi.check_gpi(root, card, data_paths, by="fit", rows={**intake_scans.read_scans(root), key: row})
    except Exception:  # noqa: BLE001 — a failed check leaves the fit without gpi values, never stops it
        log.exception("fit_file: gpi check failed; no gpi value is used")
        return None
    return None if check is None else check.scans.get(key)


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


def _given_files(rows, session, key: str, file_set: str) -> list[Path] | str:
    """The files the user gave for scan ``key`` (``"exp:scan"``), in the order given, or the reason there is
    no record of them or one is gone. Never a folder: ISAR uses what the user gave (operator, 2026-10-30)."""
    listed = intake_scans.recorded_files(rows, session.files, key)
    if not listed:
        return (f"no record of which files were imported for scan set {file_set}: neither the import record "
                "nor the ledger lists them; add the data again (the same files or their folder)")
    gone = next((p for p in listed if not Path(p).is_file()), None)
    if gone is not None:
        return f"the data file {Path(gone).name} recorded at import is no longer at {gone}"
    return [Path(p) for p in listed]


def given_files(session, rows) -> list[Path] | str:
    """Every file the user gave the experiment that is still there, for readers of the whole experiment (the
    fit preview, the deep-pass tool); the reason when none is recorded. Never a folder walk."""
    listed = intake_scans.all_recorded_files(rows, session.files)
    out = []
    for p in listed:
        if Path(p).is_file():
            out.append(Path(p))
        else:
            log.warning("given_files: the recorded file %s is no longer there", p)
    if out:
        return out
    return ("no record of which files were imported into this experiment; add the data again" if not listed
            else "none of the files recorded at import is still there; add the data again")


def _parse_unit_record(card, session, rows, prev: ScanEntry, exp: str, scan: int):
    """``(record, source_by_name)`` of the unit's scan, parsed as the run parses it from the files the user gave
    for it (the import record's members, else its paths, else the ledger's files; a file set never from its
    folder; the card's sidecars beside them are added); the reason (a str) when the data is no longer there.
    A single-file scan is read from the ingest-stamped ``source_file``, else a manifest file of the scan."""
    prov = prev.provenance or {}
    if prov.get("file_set"):
        given = _given_files(rows, session, f"{exp}:{scan}", prov["file_set"])
        if isinstance(given, str):
            return given
        found = ingest.discover(given, card)
        fs = next((f for f in found.file_sets if f.key == prov["file_set"]), None)
        if fs is None:
            return f"the files recorded for scan set {prov['file_set']} no longer form that set under the card's file rules"
        sub = ingest.IngestPlan(measurement_files=[p for p, _g in fs.measurements], file_sets=[fs],
                                sidecar_files=list(fs.sidecars))
        # the set's record names the set; its stamp keeps the path ingest gives it (the set's first file)
        source_by_name = {fs.key: str(Path(fs.measurements[0][0]).resolve())}
    else:
        src = Path(prov.get("source_file") or "")
        if not (src.is_absolute() and src.is_file()):
            src = next((p for p, row in session.files.items() if row.get("format") == "measurement"
                        and f"{exp}:{scan}" in (row.get("scan_ids") or ()) and Path(p).is_file()), None)
            if src is None:
                return f"the data file for this analyzer is no longer at {prov.get('source_file')}"
            src = Path(src)
        sub = ingest.IngestPlan(measurement_files=[src])
        source_by_name = {}
    rec = next((r for r in ingest.parse_all(sub, card, session=session)
                if (str(r.experiment_id), r.scan_number) == (exp, scan) and r.scan_type == "measurement"), None)
    if rec is None:
        return f"the recorded data no longer yields scan {exp}:{scan}"
    return rec, {**{Path(f).name: str(Path(f).resolve()) for f in sub.measurement_files}, **source_by_name}


def _deep_stage(fast_entry, oc, prep, request, progress) -> tuple:
    """``(record, timing, None)`` of the deep pass on the fast record ``oc`` builds on, run on
    ``request.workers`` cores; ``(None, None, (status, reason))`` when the unit cannot be sampled."""
    fitted = fit_context(prep)
    if isinstance(fitted, tuple):
        return None, None, (fitted[0], "Deep pass not run: " + fitted[1])
    ctx = fitted.ctx
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

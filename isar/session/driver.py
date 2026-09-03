"""The pipeline driver (the retired unification plan §2.5, git history; current contract: docs/WORKSPACE_AND_CLI.md) — parse → reduce → route → calibrate → fit → collate.

`run_batch` is the whole pipeline behind `isar run`: idempotent (re-parses everything, re-fits only
new / content-drifted / guideline-drifted / previously-held scans), context-updates before
measurements, calibration-gated fits seeded by the prior/guidelines, verdict + confidence per scan,
results written to the ledger + `results.csv`.

`build_context` and `fit_one` generalize the two gold-check `run_pipeline` recipes — same analysis
calls, same order — with the inputs (zero, resolution, slit, prior, guidelines) derived from card +
state instead of hardcoded. Nothing in `isar/analyze/` is modified (§4).
"""
from __future__ import annotations

import copy
import hashlib
import logging
import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from isar.reduce import collate as collate_mod
from isar.reduce import combine, qc
from isar.predictive import json_fingerprint as _json_fingerprint
from isar.schema.card import Card
from isar.schema.measurement import (MeasurementArena, campaign_provenance,
                                     current_lattice_epoch)
from isar.session import calibrations, guidelines as guide_mod, ingest, router
from isar.session import measurement_store
from isar.session.outcome import (
    _coerce_fit_outcome, _outcome_from_entry, _write_results_csv,
)
from isar.session import unit_data
from isar.session.state import ScanEntry, SessionState, canonical_hash, content_hash
from isar.session.timing import NullTimer
from isar.session.workspace import Workspace
from isar.session import evidence_update, master_delivery, scan_delivery
from isar.session.scan_delivery import (
    AnalysisWorkIncomplete, FULL_TIERS, _campaign_hypotheses, _dispersion_period,
)
from isar.session.master_delivery import (
    _MASTER_SCAN_BASE, _assert_canonical_indices_cover_path, _certification_entry_reusable,
    _master_fit_fingerprint, _persist_master_predictive,
    _predicted_master_analysis_seconds, _publication_hashes, unresolved_current_promotions,
)

log = logging.getLogger(__name__)


@dataclass
class RunSummary:
    outcomes: list = field(default_factory=list)     # (scan_number, FitOutcome)
    counts: dict = field(default_factory=dict)       # status/disposition tallies
    gaps: list = field(default_factory=list)
    context_lines: list = field(default_factory=list)
    n_fitted_this_run: int = 0     # scans that reached the fit/non-result stage this run
    n_reprocessed: int = 0         # scans (re)processed this run, any outcome (incl. excluded/held)
    collation: object = None
    analysis_incomplete: bool = False  # checkpointed work exists; no ledger/results generation published


@dataclass
class _AnalysisDeadline:
    """Shared wave gate for scan and master fits in one top-level runner invocation."""

    deadline: float | None
    force_initial_wave: bool = False
    waves_started: int = 0

    def begin_wave(self) -> bool:
        now = time.monotonic()
        permitted = (self.deadline is None or now < self.deadline
                     or (self.force_initial_wave and self.waves_started == 0))
        if permitted:
            self.waves_started += 1
        return permitted

    def remaining_seconds(self) -> float | None:
        if self.deadline is None:
            return None
        return max(self.deadline - time.monotonic(), 0.0)


# ===========================================================================
# the batch loop
# ===========================================================================
def _sample_of(record, card: Card) -> str:
    return (card.policy.sample or {}).get("name") or record.experiment_id or ""


def _exclusion_ids(card: Card) -> set:
    ids = set(card.policy.exclusions_seed or [])
    for e in (card.state.exclusions or []):
        if isinstance(e, dict) and "scan" in e:
            ids.add(e["scan"])
    return ids


# ===========================================================================
# worker-budget resolution (performance §; owner approved up to 30 workers)
# ===========================================================================
def _resolve_workers(cli_workers=None) -> int:
    """Scan-level worker count: explicit ``--workers`` (>0) wins, then ``ISAR_WORKERS``, then a
    default of ``min(30, cpu-2)`` (leave two threads for the OS / this parent process)."""
    for src in (cli_workers, os.environ.get("ISAR_WORKERS")):
        if src is None or src == "":
            continue
        try:
            n = int(src)
        except (TypeError, ValueError):
            continue
        if n > 0:
            return n
    cpu = os.cpu_count() or 1
    return max(1, min(30, cpu - 2))


def _resolve_boot_workers(default_n) -> int:
    """Parametric-bootstrap worker count. ``ISAR_BOOTSTRAP_WORKERS`` (>0) overrides; otherwise the
    caller's ``default_n`` (1 inside a scan-pool worker to avoid nested oversubscription; the full
    budget when scans run serially — e.g. the drive loop's one-scan-per-``isar run``). Note the
    asymmetry: the env always wins over ``default_n``, so a caller passing a deliberate serial
    default (e.g. ``1``) is silently overridden under pytest (where the repo-root conftest sets
    ``ISAR_BOOTSTRAP_WORKERS``) — don't call this expecting ``default_n=1`` to force serial."""
    env = os.environ.get("ISAR_BOOTSTRAP_WORKERS")
    if env:
        try:
            n = int(env)
            if n > 0:
                return n
        except ValueError:
            pass
    return max(1, int(default_n))


# Bayes-tier (--errors bayes) nested-sampling knobs. Production defaults are deliberately modest to
# bound the per-scan cost of the posterior; overridable per-run via CLI (--bayes-nlive etc.) or env.
# n_live=250 caveat (2026-07-11 bench, docs/stats/bench_error_methods.py): on very-high-count scans
# 250 gave narrower credible intervals than the step-7-validated 400 despite passing the ESS gate
# (mvp_zone_test16 scans 469/470); elsewhere 250≈400 at ~1.6x less cost. Acceptable while the
# posterior is diagnostics-only — a coverage campaign (score --truth --interval bayes) must gate any
# promotion of this interval beyond diagnostics.
_BAYES_DEFAULTS = {"n_live": 250, "dlogz": 0.5, "max_iter": 30_000}
_BAYES_ENV = {"n_live": "ISAR_BAYES_NLIVE", "dlogz": "ISAR_BAYES_DLOGZ", "max_iter": "ISAR_BAYES_MAXITER"}
_BAYES_CASTS = {"n_live": int, "dlogz": float, "max_iter": int}


def _resolve_bayes_opts(cli_opts=None) -> dict:
    """Resolve the bayes-tier sampler options with precedence CLI > env > defaults. Returns a plain
    picklable dict ``{n_live, dlogz, max_iter}`` threaded through the fit task tuples to
    ``errors.attach(..., bayes_opts=…)``. Mirrors :func:`_resolve_boot_workers`'s env/override style."""
    cli_opts = cli_opts or {}
    out = dict(_BAYES_DEFAULTS)
    for key, env_name in _BAYES_ENV.items():
        env = os.environ.get(env_name)
        if env not in (None, ""):
            try:
                out[key] = _BAYES_CASTS[key](env)
            except (TypeError, ValueError):
                pass
    for key, cast in _BAYES_CASTS.items():
        v = cli_opts.get(key)
        if v is not None:
            out[key] = cast(v)
    return out


def run_batch(workspace: Workspace, data_paths, *, force=False, force_masters=None, llm=False,
              M=200, scan_M=None,
              detectors=None, formats=("html", "markdown"), workers=None,
              errors_tier="full", scan_errors_tier=None, master_errors_tier=None,
              bayes_cli=None, timer=None,
              certification_manifest=None, analysis_allowance_seconds=None,
              finish_initial_wave=False) -> RunSummary:
    timer = timer or NullTimer()
    # ``force_masters``: refit exactly these master ledger ids (e.g. "master:2.65_0.0_0.0_300")
    # even when their member set is unchanged, without forcing anything else. This is the
    # predictive-escalation path: one master is owed a full-tier basis, and a global ``force``
    # re-fits every scan and every master (the parallel-across-groups path whose per-child
    # bootstrap is deliberately serial) when only one group's work was wanted.
    # A bare string is one id, not an iterable of characters — silently iterating it would turn a
    # valid single-target request into a set of one-character ids that match nothing.
    if isinstance(force_masters, str):
        force_masters = (force_masters,)
    force_masters = frozenset(str(m) for m in (force_masters or ()))
    scan_M = 50 if scan_M is None else int(scan_M)
    if scan_M <= 0 or int(M) <= 0:
        raise ValueError("M and scan_M must be positive")
    scan_tier = errors_tier if scan_errors_tier is None else str(scan_errors_tier)
    master_tier = errors_tier if master_errors_tier is None else str(master_errors_tier)
    with timer.stage("workspace_load"):
        card, session = workspace.load()
    checkpoint_store = None
    if certification_manifest is not None:
        from isar.session.certification import CheckpointStore
        checkpoint_store = CheckpointStore(certification_manifest)
    master_certification = bool(
        checkpoint_store is not None and checkpoint_store.is_master_certification)
    if master_certification and (force or force_masters):
        raise ValueError(
            "force=True / force_masters are invalid for isar.certification/2 master-only runs")
    if master_certification:
        unresolved = unresolved_current_promotions(workspace.root, session)
        if unresolved:
            ids = ", ".join(str(record.get("promotion_id")) for record in unresolved)
            raise RuntimeError(
                f"certification refused: unresolved current master promotion(s): {ids}")
    deadline_gate = _AnalysisDeadline(
        None if analysis_allowance_seconds is None
        else time.monotonic() + max(float(analysis_allowance_seconds), 0.0),
        force_initial_wave=bool(finish_initial_wave))
    if not master_certification:
        for p in map(str, data_paths):
            if p not in session.data_paths:
                session.data_paths.append(p)

    with timer.stage("ingest_parse") as _m_ingest:
        plan = ingest.discover(data_paths, card)
        records = ingest.parse_all(plan, card)
        _m_ingest["n_records"] = len(records)
    # Basename -> resolved absolute source path, so run's provenance source_file matches ingest's.
    source_by_name = {Path(f).name: str(Path(f).resolve()) for f in plan.measurement_files}

    # Reduce (both no-ops where not applicable; QC never raises).
    with timer.stage("reduce_qc"):
        for rec in records:
            combine.reduce_record(rec, card)
            qc.qc_record(rec, card)

    # File-manifest bookkeeping.
    if not master_certification:
        for f in plan.measurement_files + plan.companion_files:
            rp = str(Path(f).resolve())
            try:
                stat = Path(f).stat()
                # Merge, not replace: ingest may have written richer rows (n_scans/scan_ids) for
                # these keys — preserve them across an ordinary ingest-then-run workflow.
                session.files.setdefault(rp, {}).update(
                    {"size": stat.st_size, "mtime": stat.st_mtime,
                     "format": "companion" if f in plan.companion_files else "measurement"})
            except OSError as exc:
                log.warning("run: could not stat %s (%s); manifest row skipped", f, exc)

    # Context updates BEFORE measurements (calibration/alignment/environment).
    with timer.stage("route_context"):
        routed = router.route(records)
        ctx_lines = ([] if master_certification else router.apply_context_updates(
            routed, card, session, companion_files=plan.companion_files))

        # Optional LLM adjudication (off critical path; degrade, never stall).
        if not master_certification and (llm or (card.policy.adjudication or {}).get("enabled")):
            ctx_lines += _maybe_adjudicate(routed.measurements, card)

    # Per-unit data files for the GUI inspector, exactly as `isar ingest` writes them — so a run-only
    # workspace (never ingested) still has a counts plot per unit. Reduced+QC'd records; failures are
    # logged and skipped, never fatal. (unit_data.write_unit_data mirrors the ingest contract.)
    _unit_data_dir = getattr(workspace, "root", None)
    if _unit_data_dir is not None and not master_certification:
        _unit_data_dir = Path(_unit_data_dir) / "data"
        for rec in routed.measurements:
            for det_id in rec.spectra:
                try:
                    unit_data.write_unit_data(rec, det_id, _unit_data_dir)
                except Exception as exc:  # noqa: BLE001 — one bad unit must never fail the run
                    log.warning("run: failed to write unit data for %s:%s:%s (%s); continuing",
                                rec.experiment_id, rec.scan_number, det_id, exc)

    runcal = calibrations.calibration_view(card)
    g = guide_mod.resolve(card)
    fit_dets = guide_mod.fit_detectors(g, card, cli_detectors=_split_csv(detectors))
    exclusions = _exclusion_ids(card)
    seed_base = 6000

    # Register these scans in the ledger at status "ingested" and save now — before the fit stage,
    # which can take ~70s per scan — so a GUI watcher sees the scan with its counts (already parsed,
    # reduced, and unit-data-written above) instead of waiting for the fit to finish. Only the
    # detectors actually slated to fit (fit_dets) — matches Pass 1 below, so nothing is left stuck at
    # "ingested" for a detector the fit stage never visits. Reuses ingest's own upsert (never
    # downgrades a fitted/held/excluded entry with unchanged content); the fit pass below already
    # treats an "ingested" entry as fresh (`prev.status == "ingested"`).
    if not master_certification:
        with timer.stage("ingest_save") as _m_save:
            _now_iso = datetime.now(timezone.utc).isoformat()
            _dirty = False
            for rec in routed.measurements:
                _chash = content_hash(rec)
                for det in fit_dets:
                    if det not in rec.spectra:
                        continue
                    _prov = {**_core_provenance(rec, det, source_by_name), "ingested_at": _now_iso}
                    if ingest.upsert_ingested_entry(
                            session, rec, det, chash=_chash, prov=_prov) != "unchanged":
                        _dirty = True
            _m_save["dirty"] = _dirty
            if _dirty:
                workspace.save(card, session)

    summary = RunSummary(gaps=ingest.file_gaps(records), context_lines=ctx_lines)

    if master_certification:
        fit_options = dict(checkpoint_store.manifest.get("fit_options") or {})
        return _run_master_certification(
            workspace, card, session, records, runcal, g, checkpoint_store,
            summary=summary, M=int(fit_options.get("M", M)), errors_tier=errors_tier,
            workers=_resolve_workers(workers),
            bayes_opts=dict(fit_options.get("bayes_opts") or _resolve_bayes_opts(bayes_cli)),
            deadline_gate=deadline_gate, timer=timer)

    # --- Pass 1: split fresh scan-detectors (to fit) from carried ones, preserving report order. ---
    from isar.session.state import canonical_hash
    tasks = []          # (rec, det, q, T, sample, scan_id, chash, ghash, seed)
    plan = []           # ordered: ("carry", scan, outcome) | ("task", scan, det, task_idx)
    for rec in routed.measurements:
        scan = rec.scan_number
        chash = content_hash(rec)
        for det in fit_dets:
            if det not in rec.spectra:
                continue
            sp = rec.spectra[det]
            q = [float(v) for v in (sp.q if sp.q is not None else [0.0, 0.0, 0.0])]
            T = float(np.median(np.asarray(rec.temperature.setpoint, dtype=float)))
            sample = _sample_of(rec, card)
            scan_id = f"{rec.experiment_id}:{scan}:{det}"

            gsig = guide_mod.guidelines_signature(g, scan_number=scan, q=q, T=T)
            ghash = canonical_hash(gsig)

            prev = session.get(scan_id)
            fresh = (prev is None or prev.content_hash != chash
                     or prev.guidelines_hash != ghash or prev.status.startswith("held")
                     or prev.status == "ingested" or force)
            if not fresh:
                # Unchanged & already resolved: keep the ledger entry, re-list its outcome for the report.
                if prev.detectors.get(det):
                    plan.append(("carry", scan, _outcome_from_entry(prev, det)))
                continue
            task_idx = len(tasks)
            tasks.append((rec, det, q, T, sample, scan_id, chash, ghash, seed_base + scan))
            plan.append(("task", scan, det, task_idx))

    # Ordinary deadline-bounded work uses the same immutable/checkpointed reduction contract as final
    # certification.  The fingerprint freezes every parsed record plus policy/state and the canonical
    # scan/master worklist; worker count and the remaining allowance may change harmlessly on resume.
    if checkpoint_store is None and analysis_allowance_seconds is not None:
        from isar.session.certification import CheckpointStore, create_analysis_manifest

        def _file_sha256(path):
            candidate = Path(path)
            return hashlib.sha256(candidate.read_bytes()).hexdigest() if candidate.exists() else None

        master_worklist = []
        try:
            preview = collate_mod.collate(records, card)
            for group in sorted(getattr(preview, "groups", ()), key=lambda item: str(item.key)):
                merged = getattr(group, "merged", None)
                if merged is None:
                    continue
                master_worklist.append({
                    "key": list(group.key),
                    "members": [list(member) for member in merged.members],
                })
        except Exception as exc:  # noqa: BLE001 — the ordinary run keeps collation best-effort
            log.warning("could not preview master worklist for analysis checkpoints: %s", exc)

        manifest_path = create_analysis_manifest(workspace.root, {
            "tier": errors_tier,
            **({"scan_errors_tier": scan_tier} if scan_errors_tier is not None else {}),
            **({"master_errors_tier": master_tier} if master_errors_tier is not None else {}),
            "force": bool(force),
            "M": int(M),
            "scan_M": int(scan_M),
            "inputs": {
                "policy_sha256": _file_sha256(workspace.root / "card.policy.toml"),
                "state_sha256": _file_sha256(workspace.root / "card.state.json"),
                "records": [
                    {"experiment_id": rec.experiment_id, "scan_number": rec.scan_number,
                     "content_hash": content_hash(rec)}
                    for rec in records
                ],
            },
            "seeds": {"scan_seed_base": seed_base, "scan_rule": "6000 + scan_number",
                      "master_rule": "6000 + canonical_group_index",
                      "error_attachment_default": 12345,
                      "bayes_seed_offset": 1_000_003},
            "worklist": {
                "scans": [
                    {"unit_key": f"scan:{task[5]}", "content_hash": task[6],
                     "guidelines_hash": task[7], "seed": task[8]}
                    for task in tasks
                ],
                "masters": master_worklist,
            },
        })
        checkpoint_store = CheckpointStore(manifest_path)
        summary.context_lines.append(
            f"analysis checkpoints: {manifest_path.parent.relative_to(workspace.root)}")

    # --- Fit the fresh scans (parallel across scans when it pays; §performance). ---
    # Only the `full` tier bootstraps. When a full batch is small enough that the Layer-A scan pool
    # would idle cores (each per-scan bootstrap forced serial), bypass Layer A: fit serially in the
    # parent and funnel every bootstrap (scans + masters) through ONE shared pool at full budget
    # (the workorder — one spawn, not N). Large full reprocess batches (tasks > workers, where Layer
    # A already saturates) and the fast/cheap tiers (no bootstrap) keep the Layer-A parallel-fit path.
    scan_workers = _resolve_workers(workers)
    is_full = scan_tier in FULL_TIERS
    bayes_opts = _resolve_bayes_opts(bayes_cli)
    use_workorder = is_full and len(tasks) <= scan_workers
    if use_workorder:
        use_pool = False
        boot_workers = _resolve_boot_workers(scan_workers)
    else:
        use_pool = scan_workers > 1 and len(tasks) > 1
        boot_workers = 1 if use_pool else _resolve_boot_workers(scan_workers)
    # ``auto`` may promote after its HESSE-first point estimate; promoted bootstrap uses the same
    # deterministic adaptive rule as an explicitly full fit.
    scan_boot_adaptive = is_full or scan_tier == "auto"
    master_boot_adaptive = master_tier in FULL_TIERS or master_tier == "auto"
    master_boot_workers = _resolve_boot_workers(scan_workers)

    shared_pool = None
    if boot_workers > 1 and not use_pool:
        # Full tiers: scans + masters bootstrap through one pool at full budget (one spawn).
        # Fast/cheap tiers never bootstrap, but every tier's evidence selection runs its
        # surviving dynesty escalations through the same pool (candidate_set_evidence,
        # executor=) — at most one process per candidate structure, so a small pool suffices
        # and the spawn cost stays a fraction of one escalation (2026-09-03).
        pool_workers = boot_workers if is_full else min(boot_workers, 6)
        shared_pool = ProcessPoolExecutor(max_workers=pool_workers)
    try:
        with timer.stage("fit_scans", n_tasks=len(tasks), use_pool=use_pool,
                         boot_workers=boot_workers) as _m_fit:
            cached: dict[int, ScanEntry] = {}
            remaining = []
            remaining_indices = []
            if checkpoint_store is not None:
                for task_index, task in enumerate(tasks):
                    scan_id, chash, ghash = task[5], task[6], task[7]
                    entry = checkpoint_store.load(
                        f"scan:{scan_id}", content_hash=chash, guidelines_hash=ghash)
                    if entry is not None:
                        cached[task_index] = entry
                    else:
                        remaining.append(task)
                        remaining_indices.append(task_index)
            else:
                remaining = list(tasks)
                remaining_indices = list(range(len(tasks)))

            # Pre-dispatch stub records (SCENE-4 PR 4.0 review response, W4): the parent is the
            # single writer of report/measurements.jsonl (workers never touch the file), so one
            # stub per fresh task is appended here, BEFORE any worker starts. A hard worker kill
            # (or an ``on_complete`` that never runs) then still leaves proof a scan was attempted
            # — the enriched record ``checkpoint_entry`` appends below is a later line for the
            # same ``scan_ref`` and wins on read (last-line-wins, measurement_store's contract).
            for task in remaining:
                _rec, _det, _q, _T, _sample, _scan_id, _chash, _ghash, _seed = task
                stub = MeasurementArena(
                    scan_ref=_scan_id, scan_id=_scan_id, scan_number=_rec.scan_number,
                    detector=_det, sample=_sample,
                    conditions=(("temperature_K", float(_T)),) if _T is not None else (),
                    q=(tuple(float(v) for v in _q) if _q is not None else None),
                    lattice_epoch=current_lattice_epoch(
                        getattr(card.state, "lattice_epochs", None) or []),
                    energy_window=None, energy_bands=None, effective_exposure=None,
                    exposure_provenance=None, count_trust="attempted", reasons=(),
                    written_at=datetime.now(timezone.utc).isoformat(), content_hash=_chash,
                    # Same request provenance the enriched record carries (PR 4.3, D2): a hard
                    # worker kill leaves ONLY this stub, and a stub that cannot say which program
                    # step the neutrons were spent on is not proof of the attempt.
                    **campaign_provenance(
                        scan_delivery._acquisition_provenance_for(_rec, card, _scan_id)[1]))
                measurement_store.append_measurement(workspace.report_dir, stub.to_dict())

            def checkpoint_entry(entry):
                # Measurement-arena records first, and unconditionally (SCENE-4 PR 4.0, #26): a
                # TRUSTED record is built before fit_one is even attempted, so it must reach
                # report/measurements.jsonl regardless of what the entry's fit did — including a
                # caught fit_one exception (non_result) — and independent of, and before,
                # results.csv (written later in _apply_and_finish). ``on_complete`` is the single
                # seam that sees every fresh entry, serial or pooled, so appending here can never
                # be skipped by a later stage failing.
                if entry.measurement_record is not None:
                    measurement_store.append_measurement(
                        workspace.report_dir, entry.measurement_record)
                if checkpoint_store is not None:
                    checkpoint_store.save(f"scan:{entry.scan_id}", entry)

            fresh_entries = scan_delivery._run_tasks(
                remaining, card, g, runcal, exclusions, M=scan_M, errors_tier=scan_tier,
                boot_workers=boot_workers, use_pool=use_pool,
                scan_workers=scan_workers, boot_executor=shared_pool,
                boot_adaptive=scan_boot_adaptive, bayes_opts=bayes_opts,
                on_complete=checkpoint_entry, deadline_gate=deadline_gate,
                shadow_enabled=deadline_gate.deadline is None)
            cached.update(zip(remaining_indices, fresh_entries))
            entries = [cached[index] for index in range(len(tasks))]
            _m_fit["per_task"] = [
                {"scan_id": e.scan_id,
                 "seconds": e.provenance.get("fit_seconds"),
                 "two_stage_seconds": e.provenance.get("two_stage_seconds"),
                 "errors_attach_seconds": e.provenance.get("errors_attach_seconds"),
                 "bootstrap_seconds": e.provenance.get("bootstrap_seconds"),
                 "minos_seconds": e.provenance.get("minos_seconds"),
                 "profile_seconds": e.provenance.get("profile_seconds"),
                 "bayes_seconds": e.provenance.get("bayes_seconds")}
                | {"bootstrap_M_requested": e.provenance.get("bootstrap_M_requested"),
                   "bootstrap_M_effective": e.provenance.get("bootstrap_M_effective")}
                for e in entries if e.provenance.get("fit_seconds") is not None]
        completed_master_keys: list[str] = []
        _apply_and_finish(summary, session, card, records, runcal, g, plan, entries,
                          workspace, force=force, force_masters=force_masters,
                          M=M, seed_base=seed_base, errors_tier=master_tier,
                          master_boot_workers=master_boot_workers, boot_executor=shared_pool,
                          boot_adaptive=master_boot_adaptive, scan_workers=scan_workers,
                          bayes_opts=bayes_opts, timer=timer, source_by_name=source_by_name,
                          checkpoint_store=checkpoint_store,
                          completed_master_keys=completed_master_keys,
                          deadline_gate=deadline_gate)
        if checkpoint_store is not None:
            checkpoint_store.mark_complete(
                unit_keys=[f"scan:{entry.scan_id}" for entry in entries] + completed_master_keys)
    except AnalysisWorkIncomplete as exc:
        summary.analysis_incomplete = True
        summary.context_lines.append(f"checkpointed incomplete analysis: {exc}")
    finally:
        if shared_pool is not None:
            shared_pool.shutdown()
    return summary


def _core_provenance(rec, det, source_by_name=None) -> dict:
    """The ingest-parity core provenance keys for one (record, detector), reused from the Phase-2
    helper so `isar run` on a workspace that was never `isar ingest`-ed still carries per-unit q, T,
    the target #Q, command, calibration state, etc. — the same keys the GUI's per-unit q display and
    scan-group #Q labels read. Absent an ingest, this is the ONLY place those keys get stamped.

    ``source_by_name`` maps a record's source basename → its resolved absolute path (so run's
    source_file matches ingest's absolute form); missing → the parser's recorded name."""
    sp = rec.spectra.get(det)
    q = [float(v) for v in sp.q] if sp is not None and sp.q is not None else None
    T = float(np.median(np.asarray(rec.temperature.setpoint, dtype=float)))
    core = {"T": T, "q": q, **unit_data._provenance_extras(rec)}
    fname = getattr(getattr(rec, "source", None), "file", None)
    src = None
    if source_by_name and fname:
        src = source_by_name.get(fname) or source_by_name.get(Path(fname).name)
    src = src or fname
    if src is not None:
        core["source_file"] = src
    return core


def _apply_and_finish(summary, session, card, records, runcal, g, plan, entries,
                      workspace, *, force, force_masters=frozenset(), M, seed_base, errors_tier,
                      master_boot_workers,
                      boot_executor, boot_adaptive, scan_workers=1, bayes_opts=None, timer=None,
                      source_by_name=None, checkpoint_store=None, completed_master_keys=None,
                      deadline_gate=None):
    """Pass 2 (ledger + prior) → collate → master fits → tally → write. Split out so the shared
    bootstrap pool from :func:`run_batch` stays open across the whole full-tier batch."""
    timer = timer or NullTimer()
    # --- Pass 2: apply ledger + prior mutations serially, in submission order (determinism). ---
    # Prior seeding is START-OF-BATCH: a scan is fit against the dispersion prior as of the batch
    # start, never a same-batch neighbour's fresh point (this is what makes serial == parallel).
    # The re-fit idempotency makes it self-healing across runs — a later `isar run` re-seeds from
    # the now-complete prior only for scans that actually drifted.
    t_round = float((card.policy.collation or {}).get("t_round", 5.0))
    # scan_id -> (record, detector) for stamping ingest-parity core provenance onto fitted entries.
    rec_by_scan_id: dict[str, tuple] = {}
    for rec in records:
        for det_id in rec.spectra:
            rec_by_scan_id[f"{rec.experiment_id}:{rec.scan_number}:{det_id}"] = (rec, det_id)
    for item in plan:
        if item[0] == "carry":
            _, scan, outcome = item
            summary.outcomes.append((scan, outcome))
            continue
        _, scan, det, task_idx = item
        entry = entries[task_idx]
        # Stamp per-unit core provenance (q/T/source_file/q_target/…) the fitter never sets, and
        # MERGE under any existing (e.g. ingest-stamped) provenance so an ingest-then-run workspace
        # keeps ingest-only keys (ingested_at) while run values win where both are set. Absent an
        # ingest, this is what gives the GUI its per-unit q and scan-group #Q labels in the fit path.
        rd = rec_by_scan_id.get(entry.scan_id)
        if rd is not None:
            core = _core_provenance(rd[0], rd[1], source_by_name)
            entry.provenance = {**core, **(entry.provenance or {})}
        prev = session.get(entry.scan_id)
        if prev is not None and prev.provenance:
            entry.provenance = {**prev.provenance, **(entry.provenance or {})}
        session.put(entry)
        summary.n_reprocessed += 1
        summary.n_fitted_this_run += 1 if entry.status in ("fitted", "non_result") else 0
        oc = entry.detectors.get(det)
        if oc:
            summary.outcomes.append((scan, _coerce_fit_outcome(oc)))

    # Collate (all records, whole-experiment view).
    with timer.stage("collate"):
        try:
            summary.collation = collate_mod.collate(records, card)
        except Exception as exc:  # noqa: BLE001 — collation is a report aid, never fail the batch on it
            summary.context_lines.append(f"collation skipped: {exc}")

    # Fit the merged "master scan" per (Q,T) group (combine-before-divide pooling), when the card
    # opts in with collation.merge == "merge". These pooled fits are what let a campaign accumulate
    # statistics by repeating a measurement; s30 (merge="separate") produces none. Never fatal.
    with timer.stage("fit_masters") as _m_masters:
        try:
            # Masters run after the scan phase; their bootstraps take the full worker budget and
            # reuse the batch's shared pool when one is open (else _fit_masters opens its own).
            # Masters fit serially w.r.t. each other; only their refits hit the pool.
            per_master: list = []
            n_masters = master_delivery._fit_masters(
                summary.collation, records, card, runcal, g, session,
                data_dir=workspace.root / "data",
                M=M, seed_base=seed_base, errors_tier=errors_tier, force=force,
                force_masters=force_masters,
                boot_workers=master_boot_workers, boot_executor=boot_executor,
                boot_adaptive=boot_adaptive, scan_workers=scan_workers,
                bayes_opts=bayes_opts, per_master=per_master,
                checkpoint_store=checkpoint_store,
                completed_master_keys=completed_master_keys,
                deadline_gate=deadline_gate)
            _m_masters["n_masters"] = n_masters
            _m_masters["per_master"] = per_master
            if n_masters:
                summary.context_lines.append(f"fit {n_masters} merged master scan(s)")
        except Exception as exc:  # noqa: BLE001 — ordinary master fitting is additive
            # Certification must fail closed: suppressing a corrupt/mismatched checkpoint here
            # would let the runner publish a superficially complete revision with a missing unit.
            # A targeted force_masters run fails closed too: the run exists solely to refit that
            # master, and a swallowed failure would let the predictive escalation record a
            # "completed" promotion (a durable, never-retried charge) with the basis still missing.
            if checkpoint_store is not None or force_masters:
                raise
            summary.context_lines.append(f"master fitting skipped: {exc}")

    if _campaign_hypotheses(card):
        evidence_update._materialize_branch_candidates(session)
    # Identity assignment is deliberately after pooled-master updates. It reads the complete
    # candidate field, may revise earlier labels, then rebuilds the connected fold-only prior.
    track = evidence_update._apply_tracked_branch(session, card, t_round=t_round)
    # ADR-019: only now is the target authoritative — triage has had its say and the tracker has
    # reassigned or cleared every selection. The projection is resolved against `/2`'s stored
    # per-draw mode energies, so a track revision changes it without any refit.
    evidence_update._resolve_target_projections(session, workspace.root / "data")
    if track is not None:
        confirmed = sum(1 for row in track.get("assignments") or []
                        if row.get("track_state") == "confirmed")
        summary.context_lines.append(
            f"branch track revision {track['revision']}: "
            f"{confirmed} point(s) with a decided identity")

    # Persist cross-q views only after the authoritative prior has been rebuilt.
    evidence_update._store_branch_models(card.state, t_round)
    evidence_update._store_branch_beliefs(card.state, t_round, period=_dispersion_period(card),
                          hypotheses=_campaign_hypotheses(card))
    evidence_update._update_hypothesis_mismatch(card.state, t_round, period=_dispersion_period(card),
                                hypotheses=_campaign_hypotheses(card))

    summary.counts = _tally(session)
    session.last_run = {"counts": summary.counts,
                        "scan_gaps": [f"{e}: {a}->{b}" for (e, a, b) in summary.gaps],
                        "n_fitted_this_run": summary.n_fitted_this_run}

    with timer.stage("write_results"):
        _write_results_csv(workspace, session)
    with timer.stage("session_save"):
        workspace.save(card, session)
    # NO scene build here. SCENE-4.3 D1 (owner ruling 2026-08-18) moved scene-building OUT of the
    # analysis child: `run_batch` parses, QCs, fits and persists, and nothing else. The canonical
    # build operation is `isar.session.scene_hook.build_and_store`, invoked by whoever owns the
    # delivery — the `isar run` CLI composes it after analysis (preserving the GUI's behaviour,
    # since the GUI shells that command), and the campaign parent invokes it once per delivery and
    # pins the digest it returns. This supersedes ruling H14(d) ("the driver calls it,
    # observe-only"); the docs record that in SCENE-4.3 C9.
    #
    # The invariant this deletion buys: exactly ONE scene build per delivery on the campaign path,
    # parent-invoked. A hook here would build a second one inside the analysis child — from the
    # same ledger, off the parent's critical path and outside its pin — which is the double-build
    # and the split ownership D1 rejected.


def _run_master_certification(workspace, card, session, records, runcal, g, checkpoint_store,
                              *, summary, M, errors_tier, workers, bayes_opts,
                              deadline_gate, timer) -> RunSummary:
    """Execute an ``isar.certification/2`` worklist without fitting or mutating physical rows."""
    manifest = checkpoint_store.manifest
    requested_tier = str(manifest.get("tier") or errors_tier)
    if requested_tier != errors_tier:
        raise ValueError(
            f"certification tier mismatch: manifest={requested_tier!r}, run={errors_tier!r}")
    worklist = list(manifest.get("worklist") or ())
    worklist.sort(key=lambda unit: int(unit.get("canonical_index", -1)))
    _assert_canonical_indices_cover_path(worklist, manifest.get("excluded_points") or ())
    unit_keys = [str(unit.get("unit_key") or "") for unit in worklist]
    master_ids = [str(unit.get("master_id") or "") for unit in worklist]
    if (not all(unit_keys) or len(set(unit_keys)) != len(unit_keys)
            or not all(master_ids) or len(set(master_ids)) != len(master_ids)):
        raise ValueError("certification worklist has missing or duplicate unit/master IDs")

    with timer.stage("collate"):
        collation = collate_mod.collate(records, card)
    summary.collation = collation
    sorted_groups = sorted(collation.groups, key=lambda item: str(item.key))
    groups_by_id = {
        "master:" + "_".join(str(value) for value in group.key): (index, group)
        for index, group in enumerate(sorted_groups)
        if getattr(group, "merged", None) is not None
    }
    rec_by_member = {
        (str(rec.experiment_id), int(rec.scan_number), str(detector)): (rec, detector)
        for rec in records for detector in rec.spectra
    }

    fit_card = copy.deepcopy(card)
    frozen_state = (manifest.get("inputs") or {}).get("frozen_state")
    if not isinstance(frozen_state, dict):
        raise ValueError("certification manifest lacks its frozen scientific state")
    for field_name, value in frozen_state.items():
        if hasattr(fit_card.state, field_name):
            setattr(fit_card.state, field_name, copy.deepcopy(value))

    prepared = []
    boot_adaptive = requested_tier in FULL_TIERS
    for unit in worklist:
        master_id = str(unit["master_id"])
        matched = groups_by_id.get(master_id)
        if matched is None:
            raise ValueError(f"certification master is missing from frozen collation: {master_id}")
        group_index, group = matched
        merged = group.merged
        members = [list(member) for member in merged.members]
        digest = collate_mod.merged_spectrum_digest(merged)
        if members != list(unit.get("ordered_members") or ()):
            raise ValueError(f"certification member drift for {master_id}")
        if digest != unit.get("spectrum_digest"):
            raise ValueError(f"certification spectrum drift for {master_id}")
        if int(unit.get("group_index", -1)) != group_index:
            raise ValueError(f"certification group-index drift for {master_id}")
        spectrum_ref = str(unit.get("spectrum_ref") or "")
        spectrum_path = (workspace.root / spectrum_ref).resolve()
        root = workspace.root.resolve()
        if not spectrum_ref or not spectrum_path.is_relative_to(root) or not spectrum_path.is_file():
            raise ValueError(f"certification spectrum reference is missing or invalid for {master_id}")
        if hashlib.sha256(spectrum_path.read_bytes()).hexdigest() != digest:
            raise ValueError(f"certification spectrum blob digest mismatch for {master_id}")
        first_member = tuple(merged.members[0]) if merged.members else None
        if first_member is None or first_member not in rec_by_member:
            raise ValueError(f"certification master has no unique source member: {master_id}")
        member_rec, member_det = rec_by_member[first_member]
        calres = calibrations.resolve(member_rec, member_det, fit_card, runcal)
        if _json_fingerprint(calres.summary()) != _json_fingerprint(
                dict(unit.get("resolved_calibration") or {})):
            raise ValueError(f"certification calibration drift for {master_id}")
        seed = int(unit["seed"])
        expected_fingerprint = _master_fit_fingerprint(
            merged, fit_card, calres, g, None, errors_tier=requested_tier, M=M, seed=seed,
            boot_adaptive=boot_adaptive, bayes_opts=bayes_opts)
        if unit.get("master_fit_fingerprint") != expected_fingerprint:
            raise ValueError(f"certification fit fingerprint drift for {master_id}")
        chash = canonical_hash(members)
        prepared.append({
            "unit": unit, "group": group, "merged": merged, "group_index": group_index,
            "member_rec": member_rec, "member_det": member_det, "content_hash": chash,
            "spectrum_digest": digest, "fit_fingerprint": expected_fingerprint,
        })

    reusable_workspace = reusable_checkpoint = 0
    completed: dict[int, ScanEntry] = {}
    missing = []
    for item in prepared:
        unit = item["unit"]
        index = int(unit["canonical_index"])
        existing = session.get(str(unit["master_id"]))
        if _certification_entry_reusable(
                existing, content_hash_value=item["content_hash"],
                spectrum_digest=item["spectrum_digest"],
                fit_fingerprint=item["fit_fingerprint"], requested_tier=requested_tier):
            completed[index] = copy.deepcopy(existing)
            reusable_workspace += 1
            continue
        cached = checkpoint_store.load(
            str(unit["unit_key"]), content_hash=item["content_hash"], guidelines_hash="",
            fit_fingerprint=item["fit_fingerprint"], requested_tier=requested_tier)
        if _certification_entry_reusable(
                cached, content_hash_value=item["content_hash"],
                spectrum_digest=item["spectrum_digest"],
                fit_fingerprint=item["fit_fingerprint"], requested_tier=requested_tier):
            completed[index] = cached
            reusable_checkpoint += 1
        else:
            missing.append(item)

    def progress(*, active=0, last_hkl=None, phase="fitting", published=False):
        checkpoint_store.report_progress(
            len(completed), reused_workspace=reusable_workspace,
            reused_checkpoint=reusable_checkpoint,
            newly_fitted=max(len(completed) - reusable_workspace - reusable_checkpoint, 0),
            active=active, last_hkl=last_hkl, phase=phase,
            remaining_analysis_seconds=deadline_gate.remaining_seconds(), published=published)

    progress()
    if missing:
        max_workers = min(max(int(workers), 1), len(missing))
        for offset in range(0, len(missing), max_workers):
            wave = missing[offset:offset + max_workers]
            if not deadline_gate.begin_wave():
                progress(phase="interrupted")
                summary.analysis_incomplete = True
                summary.context_lines.append(
                    f"checkpointed incomplete certification: {len(completed)}/{len(worklist)} masters")
                return summary
            args = [(
                item["merged"], item["group"], item["member_rec"], item["member_det"],
                fit_card, runcal, g, str(item["unit"]["master_id"]),
                _MASTER_SCAN_BASE + item["group_index"], item["content_hash"], M,
                int(item["unit"]["seed"]), requested_tier, boot_adaptive, None, bayes_opts,
                "certification", deadline_gate.deadline is None,
            ) for item in wave]
            progress(active=len(wave))
            pool = None
            failures = []

            def accept_completion(entry, item, active_count):
                unit = item["unit"]
                if not _certification_entry_reusable(
                        entry, content_hash_value=item["content_hash"],
                        spectrum_digest=item["spectrum_digest"],
                        fit_fingerprint=item["fit_fingerprint"],
                        requested_tier=requested_tier):
                    # A deterministic non-result is reusable; an exception-aborted fit is not.
                    if not (entry.status == "non_result"
                            and not any("fit error (" in reason for reason in entry.reasons)):
                        raise RuntimeError(
                            f"certification fit did not produce a durable outcome: {entry.scan_id}")
                checkpoint_store.save(str(unit["unit_key"]), entry)
                completed[int(unit["canonical_index"])] = entry
                active_count -= 1
                progress(active=active_count, last_hkl=unit.get("hkl"))
                return active_count

            try:
                active = len(wave)
                if len(wave) == 1 or max_workers == 1:
                    active = accept_completion(
                        master_delivery._fit_master_group_task(args[0]), wave[0], active)
                else:
                    pool = ProcessPoolExecutor(max_workers=len(wave))
                    future_items = {pool.submit(master_delivery._fit_master_group_task, arg): item
                                    for arg, item in zip(args, wave)}
                    for future in as_completed(future_items):
                        item = future_items[future]
                        try:
                            active = accept_completion(future.result(), item, active)
                        except Exception as exc:  # drain the wave; preserve every other result
                            failures.append((item, exc))
                            active -= 1
                            progress(active=active, last_hkl=item["unit"].get("hkl"))
            finally:
                if pool is not None:
                    pool.shutdown()
            if failures:
                progress(active=0, phase="failed")
                failed_unit, error = failures[0]
                raise RuntimeError(
                    f"certification worker failed for {failed_unit['unit']['master_id']}: {error}"
                ) from error

    if len(completed) != len(worklist):
        raise RuntimeError("certification worklist completed without every canonical unit")

    progress(phase="publishing")
    physical_rows = {
        key: copy.deepcopy(value) for key, value in session.scans.items()
        if not key.startswith("master:")
    }
    for item in prepared:
        unit = item["unit"]
        entry = completed[int(unit["canonical_index"])]
        spectrum_ref = collate_mod.persist_merged_spectrum(workspace.root / "data", item["merged"])
        for outcome in entry.detectors.values():
            if outcome.get("pooled"):
                outcome["spectrum_ref"] = spectrum_ref
        entry.provenance["spectrum_ref"] = spectrum_ref
        predictive_payload = entry.provenance.pop("predictive_payload", None)
        if predictive_payload is not None:
            predictive_payload["predicted_analysis_seconds"] = (
                _predicted_master_analysis_seconds(predictive_payload))
            predictive_ref = _persist_master_predictive(workspace.root / "data", predictive_payload)
            for outcome in entry.detectors.values():
                if outcome.get("pooled"):
                    outcome["predictive_ref"] = predictive_ref
            entry.provenance["predictive_ref"] = predictive_ref
        session.put(entry)

    t_round = float((card.policy.collation or {}).get("t_round", 5.0))
    if _campaign_hypotheses(card):
        evidence_update._materialize_branch_candidates(session)
    track = evidence_update._apply_tracked_branch(session, card, t_round=t_round)
    evidence_update._resolve_target_projections(session, workspace.root / "data")
    # Tracking may annotate individual rows for display. Certification is master-only, so restore
    # their exact pre-publication ledger payload after the master-derived state has been rebuilt.
    for key, entry in physical_rows.items():
        session.scans[key] = entry
    evidence_update._store_branch_models(card.state, t_round)
    evidence_update._store_branch_beliefs(card.state, t_round, period=_dispersion_period(card),
                          hypotheses=_campaign_hypotheses(card))
    evidence_update._update_hypothesis_mismatch(card.state, t_round, period=_dispersion_period(card),
                                hypotheses=_campaign_hypotheses(card))
    summary.counts = _tally(session)
    summary.context_lines.append(
        f"certified {len(worklist)} pooled master(s); fitted {len(missing)}, "
        f"reused {reusable_workspace + reusable_checkpoint}")
    if track is not None:
        summary.context_lines.append(f"branch track revision {track['revision']}")
    session.last_run = {
        "counts": summary.counts,
        "scan_gaps": [f"{experiment}: {start}->{end}"
                      for experiment, start, end in summary.gaps],
        "n_fitted_this_run": 0,
        "certification_fingerprint": manifest["fingerprint"],
    }
    publication_root = checkpoint_store.root / "publication"
    staged_workspace = Workspace.open(publication_root)
    _write_results_csv(staged_workspace, session)
    staged_workspace.save(card, session)
    checkpoint_store.begin_publication()
    workspace.root.mkdir(parents=True, exist_ok=True)
    workspace.report_dir.mkdir(parents=True, exist_ok=True)
    # Every payload is complete and fsynced before the first replacement. The current marker is
    # already non-published, so a crash between these atomic replaces is visible and repairable.
    os.replace(staged_workspace.session_path, workspace.session_path)
    os.replace(staged_workspace.state_path, workspace.state_path)
    os.replace(staged_workspace.report_dir / "results.csv",
               workspace.report_dir / "results.csv")
    hashes = _publication_hashes(workspace)
    checkpoint_store.mark_published(unit_keys=unit_keys, published_hashes=hashes)
    progress(phase="completed", published=True)
    return summary


# ===========================================================================
# helpers
# ===========================================================================
def _split_csv(s):
    return [x.strip() for x in s.split(",") if x.strip()] if s else None


def _maybe_adjudicate(measurements, card) -> list[str]:
    try:
        from isar.reduce import adjudicate
        cfg = adjudicate.adjudication_config(card)
        client = adjudicate.make_client(cfg)
        adjudicate.adjudicate_records(measurements, card, client)
        return ["LLM adjudication: applied"]
    except Exception as exc:  # noqa: BLE001 — degrade, never stall the batch (ARCHITECTURE §9)
        return [f"LLM adjudication skipped (no consult): {exc}"]


def _tally(session: SessionState) -> dict:
    counts = {"by_status": {}, "by_disposition": {}}
    for e in session.scans.values():
        counts["by_status"][e.status] = counts["by_status"].get(e.status, 0) + 1
        for oc in e.detectors.values():
            d = oc.get("disposition")
            if d:
                counts["by_disposition"][d] = counts["by_disposition"].get(d, 0) + 1
    return counts

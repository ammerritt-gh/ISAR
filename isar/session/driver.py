"""The pipeline driver (UNIFICATION_DESIGN §2.5) — parse → reduce → route → calibrate → fit → collate.

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
import json
import logging
import math
import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from isar.reduce import collate as collate_mod
from isar.reduce import combine, qc
from isar.predictive import PREDICTIVE_CONTRACT
from isar.predictive import json_fingerprint as _json_fingerprint
from isar.schema.card import Card
from isar.schema.steering import AUXILIARY_TRACK_NODE, CANDIDATE_NOT_DETECTED, realized_outcome_for
from isar.session import branch_assignment as BA
from isar.session import branch_model, branch_tracking, calibrations, guidelines as guide_mod, ingest, router, session_prior
from isar.session import predictive_artifact
# ``FitOutcome`` and ``PREDICTIVE_CONTRACT`` are re-exported unused: the driver stays the public
# name consumers reach for even though P2.6a/P2.6b moved their producers out.
from isar.session.outcome import (
    FitOutcome, _coerce_fit_outcome, _outcome_from_entry, _write_results_csv,
)
from isar.session import unit_data
from isar.session.state import ScanEntry, SessionState, canonical_hash, content_hash
from isar.session.timing import NullTimer
from isar.session.workspace import Workspace
from isar.session import master_delivery, scan_delivery
from isar.session.scan_delivery import (
    AnalysisWorkIncomplete, FULL_TIERS, _campaign_hypotheses, _campaign_selector,
    _campaign_target, _dispersion_period, _refresh_interval_semantics,
    _refresh_probability_semantics,
)
from isar.session.master_delivery import (
    _MASTER_SCAN_BASE, _assert_canonical_indices_cover_path, _certification_entry_reusable,
    _master_fit_fingerprint, _persist_content_addressed, _persist_master_predictive,
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
    if is_full and boot_workers > 1 and not use_pool:
        shared_pool = ProcessPoolExecutor(max_workers=boot_workers)   # scans + masters, one spawn
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

            def checkpoint_entry(entry):
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
        _apply_and_finish(summary, session, session_prior, card, records, runcal, g, plan, entries,
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


def _store_branch_models(state, t_round: float) -> None:
    """Build the cross-q branch(es) from the (now complete) dispersion prior and persist them into
    ``state.branch_models`` — one per distinct ``(sample, t_bin)`` — so the DRIVE (a separate,
    crash-prone process that cannot import :mod:`isar.session`) reads the prediction durably from
    ``card.state.json``. Replace-by-key so a re-run refreshes rather than accretes; best-effort (a
    build failure never fails the batch). ``build_branch`` returns ``None`` below ``MIN_ANCHORS``."""
    points = state.dispersion_prior or []
    keys: list[tuple] = []
    for p in points:
        sample = p.get("sample")
        tb = p.get("t_bin")
        if tb is None and p.get("T") is not None:
            tb = round(float(p["T"]) / t_round) * t_round if t_round else float(p["T"])
        if sample is None or tb is None:
            continue
        key = (sample, float(tb))
        if key not in keys:
            keys.append(key)
    for sample, tb in keys:
        try:
            bm = branch_model.build_branch(points, sample=sample, t_bin=tb)
        except Exception:   # noqa: BLE001
            bm = None
        if bm is None:
            continue
        bid = bm.get("branch_id", branch_model.BRANCH_ID)
        state.branch_models = [
            m for m in (state.branch_models or [])
            if not (m.get("sample") == sample
                    and abs(float(m.get("t_bin", -1e18)) - float(tb)) < 1e-9
                    and m.get("branch_id") == bid)]
        state.branch_models.append(bm)


def _store_branch_beliefs(state, t_round: float, *, period: float | None,
                          hypotheses: list[dict] | None = None) -> None:
    """Persist the data/prior ingredients of every branch belief for the Qt-free drive/viewers."""
    if period is None:
        state.branch_belief = []
        state.branch_belief_models = []
        return
    points = state.dispersion_prior or []
    keys: list[tuple[str, float]] = []
    for point in points:
        sample = point.get("sample")
        tb = point.get("t_bin", point.get("T"))
        if sample is None or tb is None:
            continue
        tb = round(float(tb) / t_round) * t_round if t_round else float(tb)
        key = (str(sample), float(tb))
        if key not in keys:
            keys.append(key)
    if not keys and hypotheses:
        # A prior-only snapshot is useful but has no sample/T identity until the first scan lands.
        state.branch_belief = []
        state.branch_belief_models = []
        return
    rows: list[dict] = []
    for sample, tb in keys:
        beliefs = BA.build_beliefs(points, sample=sample, t_bin=tb, period=period,
                                   t_round=t_round, hypotheses=hypotheses)
        for branch_id, belief in sorted(beliefs.items()):
            anchors = [{"u": float(u), "omega0": float(y), "weight": float(w)}
                       for u, y, w in zip(belief.us, belief.ys, belief.ws)]
            rows.append({"sample": sample, "t_bin": tb, "branch_id": branch_id,
                         "period": float(period), "anchors": anchors,
                         "n_anchors": belief.n_anchors,
                         "parameter_prior": dict(belief.parameter_prior or {}),
                         "label_probabilities": {}})
    state.branch_belief_models = rows
    target = (getattr(state, "hypothesis_snapshot", None) or {}).get("target_hypothesis")
    point_rows: list[dict] = []
    for point in sorted(points, key=lambda item: (
            str(item.get("sample", "")), float(item.get("t_bin", item.get("T", 0.0))),
            tuple(float(value) for value in (item.get("q") or ())),
            str(item.get("scan_id", "")))):
        branch_id = str(point.get("branch_id") or BA.ACOUSTIC)
        probability = min(max(float(point.get("weight", 1.0)), 0.0), 1.0)
        complete = point.get("branch_posterior")
        probabilities = None
        if isinstance(complete, dict):
            usable = {}
            for key, value in complete.items():
                try:
                    numeric = float(value)
                except (TypeError, ValueError):
                    continue
                if np.isfinite(numeric) and numeric >= 0.0:
                    usable[str(key)] = numeric
            total = sum(usable.values())
            if usable and total > 0.0:
                probabilities = {key: value / total for key, value in usable.items()}
        if probabilities is None:
            # Compatibility for points persisted before complete vectors were additive.
            probabilities = {branch_id: probability, BA.NEW: 1.0 - probability}
        entropy = -sum(value * math.log(value) for value in probabilities.values() if value > 0.0)
        point_rows.append({
            "sample": point.get("sample"), "t_bin": point.get("t_bin", point.get("T")),
            "q": list(point.get("q") or []), "omega0": point.get("omega0"),
            "sigma": BA.SIGMA_SLACK / math.sqrt(max(probability, 1e-9)),
            "branch_id": branch_id, "label_probabilities": probabilities,
            "assignment_entropy": entropy,
            "p_is_target": float(probabilities.get(target, 0.0)) if target is not None else 0.0,
            "resolvable": None, "scan_id": point.get("scan_id")})
    state.branch_belief = point_rows


def _update_hypothesis_mismatch(state, t_round: float, *, period: float | None,
                                hypotheses: list[dict] | None = None) -> None:
    """Persist O11's prior-vs-data-only posterior-predictive mismatch assessment."""
    snapshot = getattr(state, "hypothesis_snapshot", None) or {}
    target = snapshot.get("target_hypothesis")
    policy = snapshot.get("steering") or {}
    threshold = float(policy.get("mismatch_z_threshold", 3.0))
    hypothesis = next((h for h in (hypotheses or []) if h.get("name") == target), None)
    if period is None or hypothesis is None:
        state.hypothesis_mismatch = {}
        return
    target_points = [p for p in (state.dispersion_prior or []) if p.get("branch_id") == target]
    groups: dict[tuple[str, float], list[dict]] = {}
    for point in target_points:
        sample_value = point.get("sample")
        tb_value = point.get("t_bin", point.get("T"))
        if sample_value is None or tb_value is None:
            continue
        tb = round(float(tb_value) / t_round) * t_round if t_round else float(tb_value)
        groups.setdefault((str(sample_value), float(tb)), []).append(point)
    eligible = {key: rows for key, rows in groups.items() if len(rows) >= 2}
    if not eligible:
        state.hypothesis_mismatch = {
            "active": False, "evaluated": False, "target_hypothesis": target,
            "comparison": "prior_vs_data_only_posterior_predictive",
            "reason": "fewer_than_two_real_anchors", "threshold_z": threshold,
            "fingerprint": snapshot.get("fingerprint")}
        return
    sample, tb = max(sorted(eligible), key=lambda key: (len(eligible[key]), key))
    points = sorted(eligible[(sample, tb)], key=lambda point: (
        tuple(float(value) for value in (point.get("q") or ())),
        str(point.get("scan_id", ""))))
    belief = BA.build_beliefs(points, sample=sample, t_bin=tb, period=period,
                              t_round=t_round, hypotheses=hypotheses).get(target)
    prior_only = BA.BranchBelief(target, np.array([]), np.array([]), np.array([]),
                                 parameter_prior=hypothesis)
    comparisons = []
    for u in sorted(set(float(value) for value in belief.us)):
        prior_mu, prior_sigma = prior_only.predict(u)
        data_mu, data_sigma = belief.predict_data_only(u)
        denom = math.sqrt(prior_sigma ** 2 + data_sigma ** 2)
        z = abs(prior_mu - data_mu) / max(denom, 1e-12)
        comparisons.append({"u": u, "prior_mean": prior_mu, "prior_sigma": prior_sigma,
                            "data_mean": data_mu, "data_sigma": data_sigma, "z": z})
    worst = max(comparisons, key=lambda row: row["z"])
    active = bool(worst["z"] >= threshold)
    state.hypothesis_mismatch = {
        "active": active, "evaluated": True, "target_hypothesis": target,
        "comparison": "prior_vs_data_only_posterior_predictive",
        "max_z": worst["z"], "threshold_z": threshold,
        "evaluated_points": len(comparisons), "worst": worst,
        "sample": sample, "t_bin": tb,
        "point_ids": [str(point.get("scan_id", "")) for point in points],
        "fingerprint": snapshot.get("fingerprint"),
        "message": (f"declared {target} hypothesis disagrees with data-only branch prediction"
                    if active else "declared hypothesis remains compatible with data-only prediction")}


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


def _tracked_selector(card) -> bool:
    return _campaign_selector(card)[0] == "tracked_branch"


def _mode_candidates(scan_id: str, detector: str, outcome: dict) -> list[dict]:
    """Materialise every fitted mode with its own uncertainty and provenance.

    Full profile/bootstrap/Bayes intervals currently belong to the fit's scalar primary mode. They
    are copied only to that matching candidate; a secondary receives its own HESSE interval, never
    the primary mode's interval (which would be numerically tidy and scientifically wrong).
    """
    # A tracking revision may have rewritten the backwards-compatible scalar omega0 to a secondary
    # mode. The previously materialised table is immutable fit evidence and remembers which mode
    # actually owned the fitter's profile/bootstrap/Bayes intervals. Reuse it on carry/no-op reruns;
    # a genuine refit replaces the whole outcome and arrives without this table.
    stored = outcome.get("branch_candidates")
    if isinstance(stored, list) and stored:
        rows = []
        for candidate in stored:
            if not isinstance(candidate, dict):
                continue
            row = dict(candidate)
            # Assignment probabilities are derived state, never fit evidence.  Keeping them in the
            # next Viterbi input would let yesterday's assignment masquerade as a new local label.
            for field in ("probabilities", "association_probability",
                          "character_probability", "trajectory_identity"):
                row.pop(field, None)
            rows.append(row)
        if rows:
            return rows
    modes = list(outcome.get("modes") or [])
    if not modes and outcome.get("omega0") is not None:
        modes = [{"omega0": outcome.get("omega0"), "omega0_err": outcome.get("omega0_err"),
                  "gamma": outcome.get("gamma"), "amplitude": None}]
    primary = outcome.get("omega0")
    rows = []
    for index, mode in enumerate(modes):
        energy = mode.get("omega0")
        if energy is None:
            continue
        try:
            energy = float(energy)
        except (TypeError, ValueError):
            continue
        if not np.isfinite(energy):
            continue
        error = mode.get("omega0_err")
        try:
            error = float(error)
        except (TypeError, ValueError):
            error = None
        if error is not None and (not np.isfinite(error) or error <= 0.0):
            error = None
        is_primary = primary is not None and abs(float(primary) - energy) <= 1e-6
        hesse = ([energy - error, energy + error] if error is not None else None)
        row = {
            "candidate_ref": f"{scan_id}:{detector}:mode:{index}", "source_scan_id": scan_id,
            "detector": detector, "mode_index": index, "energy_meV": energy,
            "omega0_err": error, "sigma_meV": error, "gamma": mode.get("gamma"),
            "gamma_err": mode.get("gamma_err"), "amplitude": mode.get("amplitude"),
            "interval": (list(outcome.get("interval")) if is_primary and outcome.get("interval") else None),
            "profile_interval": (list(outcome.get("omega0_profile"))
                                 if is_primary and outcome.get("omega0_profile") else None),
            "hesse_interval": (list(outcome.get("hesse_interval"))
                               if is_primary and outcome.get("hesse_interval") else hesse),
            "bayes_interval": (list(outcome.get("bayes_interval"))
                               if is_primary and outcome.get("bayes_interval") else None),
            "reliable": bool(outcome.get("interval_reliable", True)),
            "pooled": bool(outcome.get("pooled")), "n_members": outcome.get("n_members"),
            "fit_kind": outcome.get("fit_kind"), "model": outcome.get("best_label"),
            "verdict": outcome.get("verdict"), "modality": outcome.get("modality"),
        }
        rows.append(row)
    return rows


def _path_index(q: list, path: list[list[float]], tolerance: float = 1e-5) -> int | None:
    if not q or not path:
        return None
    distances = [math.dist([float(value) for value in q], point) for point in path]
    index = int(np.argmin(distances))
    return index if distances[index] <= tolerance else None


# A clean null answers "no rival in this window" but does not measure detection power, so its evidence
# of absence is real but not certain. Until PR3 computes a predicted detection power per probe, a
# trustworthy clean null relaxes the missing-branch pull by this bounded, conservative fraction rather
# than fully cancelling it -- the null moves the posterior without ever claiming the branch cannot exist.
CLEAN_NULL_DETECTION_SENSITIVITY = 0.5


def _clean_null_window(outcome: dict) -> tuple[float, float, float] | None:
    """The (lo, hi, sensitivity) negative-evidence window for a trustworthy clean-null auxiliary probe.

    A clean null searched [search_energy_lo, search_energy_hi] and found no mode there; the tracker
    reads this as "the branch predicted in this window is confirmed absent, not merely unmeasured"
    (:func:`branch_tracking._null_relaxation`). Returns ``None`` for anything that is not a trustworthy
    clean null -- a broken analysis, a detection, or a probe with no recorded window -- so a failure can
    never masquerade as evidence of absence.
    """
    if outcome.get("evidence_sink") != AUXILIARY_TRACK_NODE:
        return None
    if realized_outcome_for(outcome.get("disposition"),
                            has_candidate=bool(outcome.get("branch_candidates"))) != CANDIDATE_NOT_DETECTED:
        return None
    lo, hi = outcome.get("search_energy_lo"), outcome.get("search_energy_hi")
    try:
        lo, hi = float(lo), float(hi)
    except (TypeError, ValueError):
        return None
    if not (np.isfinite(lo) and np.isfinite(hi) and lo < hi):
        return None
    return (lo, hi, CLEAN_NULL_DETECTION_SENSITIVITY)


def _tracked_authorities(session: SessionState, path: list[list[float]]) -> tuple[list[dict], dict]:
    """Return one authoritative fit per path point and all outcome references.

    Only a published pooled master may be an identity authority.  Physical fits remain in ``refs`` so
    the GUI can display their candidate tables, but they never fill a gap in the authoritative track
    and never vote on a master assignment.  Internal consistency ranks masters here rather than
    excluding them — an inconsistent pool is still fitted evidence of *which modes exist*, and the
    gates that consistency does own live elsewhere (see the ``pool_consistent`` comment below).

    A scan acquired as an ``auxiliary_track_node`` probe is the one exception, and a deliberately
    narrow one.  It was fired to decide *which mode* a candidate is, not to deliver a better ω₀, so it
    is never pooled and never certification-eligible; its fitted modes join the observation at its path
    index as extra candidates tagged ``evidence_source="auxiliary_probe"``.  It can therefore change the
    identity assigned at that point and can never supply the point's ω₀ or become its ``authority``.
    It merges only where a pooled master already holds the index: alone it would be the *sole* identity
    evidence at that point, which is precisely what the rule above exists to prevent.
    """
    by_index: dict[int, list[tuple]] = {}
    auxiliary_by_index: dict[int, list[dict]] = {}
    null_windows_by_index: dict[int, list[tuple]] = {}
    refs: dict[str, tuple] = {}
    for order, entry in enumerate(session.scans.values()):
        for detector, outcome in entry.detectors.items():
            candidates = _mode_candidates(entry.scan_id, detector, outcome)
            outcome["branch_candidates"] = candidates or None
            key = f"{entry.scan_id}:{detector}"
            refs[key] = (entry, detector, outcome)
            index = _path_index(outcome.get("q") or [], path)
            # A clean null from an auxiliary probe fits no mode, so it never becomes a candidate; it is
            # negative evidence that the branch predicted in its searched window is absent, carried to
            # the tracker as a ``null_window`` that relaxes (never forces) that branch's missing pull.
            null_window = _clean_null_window(outcome)
            if index is not None and null_window is not None:
                null_windows_by_index.setdefault(index, []).append(null_window)
                continue
            if (index is not None and candidates
                    and outcome.get("evidence_sink") == AUXILIARY_TRACK_NODE):
                for candidate in candidates:
                    tagged = dict(candidate)
                    tagged["evidence_source"] = "auxiliary_probe"
                    try:
                        search_window = branch_tracking.normalize_energy_domain(
                            (outcome.get("search_energy_lo"), outcome.get("search_energy_hi")))
                    except ValueError:
                        search_window = ()
                    if search_window:
                        tagged["authorized_energy_window_meV"] = list(search_window[0])
                    auxiliary_by_index.setdefault(index, []).append(tagged)
                continue
            if index is not None and candidates and outcome.get("pooled"):
                exposure = outcome.get("effective_exposure")
                try:
                    exposure = float(exposure)
                except (TypeError, ValueError):
                    exposure = 0.0
                # A ``pool_consistent is False`` master's members disagree on ω₀, but the acoustic mode
                # is still fitted — so its candidate is valid IDENTITY/connectivity evidence and must
                # not be dropped. Dropping it emptied the q's observation, broke the tracked trajectory,
                # and cascaded every confirmed downstream point to ``blocked`` (AP8). It stays out of
                # the certifiable interval (stopping.py ``_master_hw``) and the dispersion prior
                # (``_rebuild_*_prior``) independently, so keeping it here cannot certify a bad ω₀ or
                # pollute the prior. When a q holds both a consistent and an inconsistent master the
                # rank prefers the consistent one rather than excluding the other.
                consistent = outcome.get("pool_consistent") is not False
                rank = (int(consistent), int(outcome.get("n_members") or 0), exposure, order)
                by_index.setdefault(index, []).append((rank, key, candidates))
    observations = []
    for index in range(len(path)):
        choices = by_index.get(index) or []
        null_windows = null_windows_by_index.get(index) or []
        if not choices:
            # Any auxiliary probe at this index is deliberately dropped: see the docstring. It stays
            # in ``refs`` for display and its beliefs update elsewhere, but it does not become the
            # identity evidence for a point no master has measured. A clean-null probe is the one
            # exception that leaves a trace here: its negative evidence rides on ``null_windows``.
            observation = {"candidates": [], "authority": None}
        else:
            _rank, key, candidates = max(choices, key=lambda item: item[0])
            _entry, _detector, outcome = refs[key]
            merged = [dict(candidate) for candidate in candidates]
            merged.extend(auxiliary_by_index.get(index) or [])
            observation = {"candidates": merged, "authority": key,
                           "gauss_fwhm": (outcome.get("gauss_fwhm")
                                          or (outcome.get("calib") or {}).get("gauss_fwhm"))}
        # Only present when a clean null was recorded here: a campaign that never used the auxiliary
        # channel must produce observations byte-identical to the pre-channel ones, so its identity
        # signature is unperturbed.
        if null_windows:
            observation["null_windows"] = null_windows
        observations.append(observation)
    return observations, refs


def _tracked_authority_signature(
        observations: list[dict], *, target_branch: str, period: float | None,
        hypotheses: list[dict], entropy_stop: float, maximum_bridge_steps: int,
        energy_domain_meV=None,
        scorer_revision: int = branch_tracking.BRANCH_TRACKING_SCORER_REVISION,
) -> str:
    """Stable identity input; derived probabilities are deliberately excluded."""
    authorities = []
    for observation in observations:
        candidates = []
        for candidate in observation.get("candidates") or []:
            fitted = dict(candidate)
            for field in ("probabilities", "association_probability",
                          "character_probability", "trajectory_identity"):
                fitted.pop(field, None)
            candidates.append(fitted)
        authority = {
            "authority": observation.get("authority"),
            "gauss_fwhm": observation.get("gauss_fwhm"),
            "candidates": candidates,
        }
        # Negative evidence is part of the identity basis: a new clean null changes the question being
        # asked at this q, so an unchanged basis (Increment C) cannot re-offer the same probe. Added only
        # when present, so a campaign with no probes hashes byte-identically to the pre-channel signature.
        if observation.get("null_windows"):
            authority["null_windows"] = observation["null_windows"]
        authorities.append(authority)
    return canonical_hash({
        "authorities": authorities, "target_branch": target_branch,
        "period": period, "hypotheses": hypotheses,
        "assignment_entropy_stop_nats": float(entropy_stop),
        "maximum_bridge_steps": int(maximum_bridge_steps),
        "energy_domain_meV": [
            list(interval) for interval in branch_tracking.normalize_energy_domain(energy_domain_meV)],
        "scorer_revision": int(scorer_revision),
    })


def _materialize_branch_candidates(session: SessionState) -> None:
    """Persist unordered fitted-mode evidence independently of target delivery.

    Local branch triage may deliberately clear the scalar omega0.  Keeping the immutable candidate
    table means the viewer and a later connected tracker can still see what was actually fitted.
    """
    for entry in session.scans.values():
        for detector, outcome in entry.detectors.items():
            candidates = _mode_candidates(entry.scan_id, detector, outcome)
            outcome["branch_candidates"] = candidates or None


AUXILIARY_EVIDENCE_SOURCE = "auxiliary_probe"


def _is_auxiliary_candidate(candidate: dict) -> bool:
    """Whether this fitted mode came from an auxiliary probe rather than the point's own master.

    The tag is applied in :func:`_tracked_authorities` when the probe's modes are merged onto the
    canonical observation, and absence of it means canonical.
    """
    return (candidate or {}).get("evidence_source") == AUXILIARY_EVIDENCE_SOURCE


def _select_mode(outcome: dict, candidate: dict, candidate_index: int, assignment: dict,
                 revision: int, target_branch: str = "optic") -> None:
    if _is_auxiliary_candidate(candidate):
        # Defence in depth behind _apply_tracked_branch's check. An auxiliary probe is never pooled
        # and never certification-eligible, so copying its energy and intervals onto a pooled master
        # builds a row that certifies the target from exposure the target window never spent.
        raise ValueError(
            f"auxiliary candidate {candidate.get('candidate_ref')!r} cannot supply a target "
            "delivery; withhold the delivery instead (see _withhold_tracked_delivery)")
    outcome["selected_candidate_index"] = int(candidate_index)
    outcome["track_state"] = assignment.get("track_state")
    outcome["track_revision"] = int(revision)
    outcome["candidate_state"] = assignment.get("track_state")
    outcome["candidate_id"] = candidate.get("candidate_ref")
    outcome["omega0"] = candidate.get("energy_meV")
    outcome["omega0_err"] = candidate.get("omega0_err")
    outcome["gamma"] = candidate.get("gamma")
    outcome["interval"] = candidate.get("interval")
    outcome["omega0_profile"] = candidate.get("profile_interval")
    outcome["hesse_interval"] = candidate.get("hesse_interval")
    outcome["bayes_interval"] = candidate.get("bayes_interval")
    outcome["interval_reliable"] = bool(candidate.get("reliable", False))
    probabilities = dict(candidate.get("probabilities") or {})
    outcome["branch_id"] = target_branch
    outcome["branch_posterior"] = probabilities
    outcome["branch_weight"] = float(probabilities.get(target_branch, 0.0))
    outcome["assignment_entropy"] = assignment.get("assignment_entropy")
    _carry_local_identity(outcome, assignment)
    outcome["structural_endpoint_status"] = assignment.get("structural_endpoint_status")
    outcome["is_reduced_q_origin"] = bool(assignment.get("is_reduced_q_origin", False))
    # Auxiliary withholding is a temporary delivery gate, not a durable property of the q. A later
    # canonical selection can immediately reuse the fit's preserved model delivery without a refit.
    outcome["target_delivery_eligible"] = True
    outcome["identity_branch_id"] = None
    outcome["delivery_withheld_reason"] = None
    _refresh_interval_semantics(outcome)
    outcome["probability_semantics"] = _refresh_probability_semantics(outcome)


def _carry_local_identity(outcome: dict, assignment: dict) -> None:
    """Carry the tracker's local identity verdict onto the outcome, selected or not.

    These survive :func:`_clear_tracked_selection` on purpose.  That helper withdraws the *scalar
    selection* so nothing stale is certifiable, but acquisition needs the local identity evidence
    precisely when the selection is absent — that is how it tells an undecided label (count more)
    from a decided label behind a path gap (counting cannot help).
    """
    outcome["target_probability"] = assignment.get("target_probability")
    outcome["identity_entropy"] = assignment.get("identity_entropy")
    clear = assignment.get("local_identity_clear")
    outcome["local_identity_clear"] = None if clear is None else bool(clear)


def _clear_tracked_selection(outcome: dict, assignment: dict, revision: int) -> None:
    """Persist a missing/ambiguous target without leaving an old scalar selection certifiable."""
    outcome["selected_candidate_index"] = None
    outcome["track_state"] = assignment.get("track_state")
    outcome["track_revision"] = int(revision)
    outcome["candidate_state"] = assignment.get("track_state")
    outcome["candidate_id"] = None
    outcome["branch_id"] = None
    outcome["branch_posterior"] = None
    outcome["branch_weight"] = None
    outcome["assignment_entropy"] = assignment.get("assignment_entropy")
    _carry_local_identity(outcome, assignment)
    outcome["structural_endpoint_status"] = assignment.get("structural_endpoint_status")
    outcome["is_reduced_q_origin"] = bool(assignment.get("is_reduced_q_origin", False))
    # A newly ambiguous/missing assignment also withdraws any earlier auxiliary-only identity
    # conclusion. Keep a pre-existing delivery gate closed until a canonical selection explicitly
    # re-enables it, but do not keep claiming a branch identity the tracker no longer confirms.
    outcome["identity_branch_id"] = None
    outcome["delivery_withheld_reason"] = None
    # The immutable branch_candidates table remains the complete fit evidence and can restore these
    # mode-specific values after a later reassignment.  The scalar compatibility view must abstain.
    for field in ("omega0", "omega0_err", "gamma", "interval", "omega0_profile",
                  "hesse_interval", "bayes_interval"):
        outcome[field] = None
    outcome["interval_reliable"] = False
    _refresh_interval_semantics(outcome)
    outcome["probability_semantics"] = _refresh_probability_semantics(outcome)


def _withhold_tracked_delivery(outcome: dict, candidate: dict, assignment: dict, revision: int,
                               target_branch: str) -> None:
    """Record a confirmed identity whose only supporting mode came from an auxiliary probe.

    This is neither a selection nor an ambiguity. The tracker reached a conclusion — that is what the
    probe was fired to buy, and discarding it would waste the beam time — but the mode it concluded
    from lives in a scan that is never pooled and never certification-eligible. So the *answer* is
    kept and the *number* is withheld: this point has no canonical measurement of the branch it has
    now identified, and the honest next action is an ordinary target scan at that energy.

    ``branch_id`` is deliberately NOT set. On a master that field means "this row supplies the target
    delivery", and every reader predating this state would take it at face value. The conclusion is
    carried by ``identity_branch_id`` instead, alongside the local-identity fields acquisition already
    reads when a selection is absent.
    """
    _clear_tracked_selection(outcome, assignment, revision)
    outcome["target_delivery_eligible"] = False
    outcome["identity_branch_id"] = target_branch
    outcome["delivery_withheld_reason"] = "auxiliary_only_selection"
    # Preserve the fit's model delivery. The eligibility flag gates both scalar and model paths, and a
    # later canonical selection can therefore restore certification without an unnecessary refit.
    log.warning(
        "withholding target delivery at q=%s: the tracked %s identity rests on auxiliary candidate "
        "%r, which is never pooled and never certification-eligible. Identity retained; this point "
        "still needs a canonical target measurement.",
        outcome.get("q"), target_branch, candidate.get("candidate_ref"))


def _rebuild_tracked_prior(card, session: SessionState, track: dict, observations: list[dict],
                           refs: dict, *, t_round: float) -> None:
    source_ids = {key.rsplit(":", 1)[0] for row in observations
                  for key in [row.get("authority")] if key}
    # Replace only derived points sourced from fits participating in this tracked campaign. Raw fit
    # evidence remains in the ledger/candidate tables and reassignment history remains in the track.
    card.state.dispersion_prior = [point for point in (card.state.dispersion_prior or [])
                                   if str(point.get("scan_id", "")).split("#", 1)[0] not in source_ids]
    revision = int(track.get("revision", 0))
    for assignment, observation in zip(track.get("assignments") or [], observations):
        if assignment.get("track_state") != "confirmed" or not observation.get("authority"):
            continue
        _entry, _detector, outcome = refs[observation["authority"]]
        if (not outcome.get("pooled") or outcome.get("modality") != "fold"
                or not _master_prior_admissible(_entry, outcome)
                or outcome.get("pool_consistent") is False):
            continue
        for branch in ("optic", "acoustic"):
            candidate_index = assignment.get(f"{branch}_candidate_index")
            if candidate_index is None:
                continue
            candidate = assignment["candidates"][candidate_index]
            if _is_auxiliary_candidate(candidate):
                # The prior accepts only clean single-answer folds from pooled masters. This candidate
                # was fitted by a probe that is not pooled at all, and the outcome gating this loop is
                # the *canonical* authority — so without this check the auxiliary energy would enter
                # the running dispersion prior wearing the master's fold provenance.
                continue
            probability = float((candidate.get("probabilities") or {}).get(branch, 0.0))
            if probability < branch_tracking.CONFIRMED_PROBABILITY:
                continue
            session_prior.add_point(
                card.state,
                scan_id=f"{candidate['source_scan_id']}#tracked:{branch}",
                sample=outcome.get("sample"), T=outcome.get("T"), q=outcome.get("q"),
                omega0=candidate.get("energy_meV"), modality="fold", verdict="resolved",
                gamma=candidate.get("gamma"), gamma_err=candidate.get("gamma_err"),
                intensity=(outcome.get("intensity") if branch == "optic" else None),
                fit_kind=outcome.get("fit_kind"), branch_id=branch, weight=probability,
                branch_posterior=candidate.get("probabilities"),
                assignment_entropy=assignment.get("assignment_entropy"), t_round=t_round,
                require_fold=True)
    for point in card.state.dispersion_prior:
        if "#tracked:" in str(point.get("scan_id", "")):
            point["track_revision"] = revision


def _authorized_tracking_domain(target: dict, observations: list[dict]):
    """Normalized union of policy- and provenance-authorized tracking windows.

    Candidate energies never contribute to this union. An auxiliary candidate with missing or malformed
    acquisition provenance remains visible evidence, but gains no support outside the declared target
    and identity bands.
    """
    intervals = []
    target_band = target.get("energy_search_meV")
    if target_band is None:
        intervals.extend(branch_tracking.normalize_energy_domain(None))
    else:
        try:
            intervals.extend(branch_tracking.normalize_energy_domain(target_band))
        except ValueError as exc:
            raise ValueError(
                f"malformed persisted energy_search_meV tracking authorization: {target_band!r}"
            ) from exc
    identity_band = target.get("identity_search_meV")
    if identity_band is not None:
        try:
            intervals.extend(branch_tracking.normalize_energy_domain(identity_band))
        except ValueError:
            log.warning("ignoring malformed persisted identity_search_meV tracking authorization: %r",
                        identity_band)
    for observation in observations:
        for candidate in observation.get("candidates") or []:
            if not _is_auxiliary_candidate(candidate):
                continue
            window = candidate.get("authorized_energy_window_meV")
            if window is None:
                continue
            try:
                intervals.extend(branch_tracking.normalize_energy_domain(window))
            except ValueError:
                log.warning("ignoring malformed auxiliary candidate tracking authorization: %r", window)
        for null_window in observation.get("null_windows") or []:
            try:
                intervals.extend(branch_tracking.normalize_energy_domain(null_window[:2]))
            except (TypeError, ValueError):
                log.warning("ignoring malformed auxiliary null-window tracking authorization: %r",
                            null_window)
    return branch_tracking.normalize_energy_domain(intervals)


def _apply_tracked_branch(session: SessionState, card, *, t_round: float) -> dict | None:
    if not _tracked_selector(card):
        return None
    target = getattr(card.state, "campaign_target", None) or {}
    path = target.get("path") or []
    if not path:
        log.warning("tracked_branch requested without a persisted objective path; assignment deferred")
        return None
    path = [[float(value) for value in q] for q in path]
    observations, refs = _tracked_authorities(session, path)
    policy = (getattr(card.state, "hypothesis_snapshot", None) or {}).get("steering") or {}
    previous = getattr(card.state, "branch_tracks", None) or {}
    target_branch = _campaign_target(card)[0]
    period = _dispersion_period(card)
    hypotheses = _campaign_hypotheses(card)
    entropy_stop = float(policy.get("assignment_entropy_stop_nats", 0.2))
    maximum_bridge_steps = int(policy.get("maximum_bridge_steps", 1))
    energy_domain = _authorized_tracking_domain(target, observations)
    authority_signature = _tracked_authority_signature(
        observations, target_branch=target_branch, period=period, hypotheses=hypotheses,
        entropy_stop=entropy_stop, maximum_bridge_steps=maximum_bridge_steps,
        energy_domain_meV=energy_domain,
        scorer_revision=branch_tracking.BRANCH_TRACKING_SCORER_REVISION)
    if (previous.get("authoritative_evidence_signature") == authority_signature
            and previous.get("target_branch") == target_branch
            and previous.get("assignments") is not None):
        track = previous
    else:
        track = branch_tracking.track_path(
            path, observations, period=period, hypotheses=hypotheses,
            target_branch=target_branch, previous=previous,
            assignment_entropy_stop_nats=entropy_stop,
            maximum_bridge_steps=maximum_bridge_steps,
            energy_domain_meV=energy_domain)
        track["authoritative_evidence_signature"] = authority_signature
    revision = int(track["revision"])
    for assignment, observation in zip(track["assignments"], observations):
        key = observation.get("authority")
        selected = assignment.get("selected_candidate_index")
        if not key:
            continue
        _entry, _detector, outcome = refs[key]
        outcome["branch_candidates"] = assignment["candidates"]
        # A MAP state is still only an assignment hypothesis.  Ambiguous/provisional/blocked target
        # rows remain candidate evidence and must not publish a scalar target delivery.
        if selected is None or assignment.get("track_state") != "confirmed":
            _clear_tracked_selection(outcome, assignment, revision)
            for _other_key, (_other_entry, _other_detector, other) in refs.items():
                if other is outcome or _path_index(other.get("q") or [], path) != assignment["path_index"]:
                    continue
                local_assignment = dict(assignment)
                local_assignment["track_state"] = "individual"
                _clear_tracked_selection(other, local_assignment, revision)
            continue
        candidate = assignment["candidates"][selected]
        if _is_auxiliary_candidate(candidate):
            _withhold_tracked_delivery(outcome, candidate, assignment, revision, target_branch)
        else:
            _select_mode(outcome, candidate, selected, assignment, revision, target_branch)
        # Individual fits at the same q remain muted evidence but follow the current identity for
        # backwards-compatible scalar columns, using their own nearest fitted mode. Referenced against
        # the selected candidate's energy rather than ``outcome["omega0"]``: identical on the delivery
        # path, and still the identity the tracker concluded when the delivery was withheld.
        reference_energy = float(candidate["energy_meV"])
        for _other_key, (_other_entry, _other_detector, other) in refs.items():
            if other is outcome or _path_index(other.get("q") or [], path) != assignment["path_index"]:
                continue
            candidates = other.get("branch_candidates") or []
            if not candidates:
                continue
            nearest = min(range(len(candidates)), key=lambda index: abs(
                float(candidates[index]["energy_meV"]) - reference_energy))
            local_assignment = dict(assignment)
            local_assignment["track_state"] = "individual"
            _select_mode(other, candidates[nearest], nearest, local_assignment, revision,
                         target_branch)
    _rebuild_tracked_prior(card, session, track, observations, refs, t_round=t_round)
    card.state.branch_tracks = track
    return track


def _rebuild_pooled_prior(card, session: SessionState, session_prior, *, t_round: float) -> None:
    """Rebuild the non-tracked running dispersion prior from pooled masters only."""
    if _tracked_selector(card):
        return
    source_ids = set(session.scans)
    card.state.dispersion_prior = [
        point for point in (card.state.dispersion_prior or [])
        if str(point.get("scan_id", "")).split("#", 1)[0] not in source_ids]
    for entry in sorted(session.scans.values(), key=lambda item: item.scan_number):
        for outcome in entry.detectors.values():
            if (not outcome.get("pooled") or outcome.get("modality") != "fold"
                    or not _master_prior_admissible(entry, outcome)
                    or outcome.get("pool_consistent") is False):
                continue
            session_prior.add_point(
                card.state, scan_id=entry.scan_id, sample=outcome.get("sample"),
                T=outcome.get("T"), q=outcome.get("q"), omega0=outcome.get("omega0"),
                modality="fold", verdict=outcome.get("verdict"), gamma=outcome.get("gamma"),
                intensity=outcome.get("intensity"), fit_kind=outcome.get("fit_kind"),
                branch_id=outcome.get("branch_id"), weight=outcome.get("branch_weight"),
                branch_posterior=outcome.get("branch_posterior"),
                assignment_entropy=outcome.get("assignment_entropy"), t_round=t_round,
                require_fold=True)


def _master_prior_admissible(entry: ScanEntry, outcome: dict) -> bool:
    """Full errors or a trusted HESSE master may seed the prior; cheap/untrusted fits may not."""
    provenance = entry.provenance or {}
    tier = provenance.get("errors_tier_effective") or provenance.get(
        "analysis_tier_effective") or provenance.get("analysis_tier")
    # Additive compatibility: ledgers predating tier provenance already passed the resolved-fold
    # gate and must not silently lose their prior points merely by being reopened on newer code.
    if tier is None:
        return True
    if tier in FULL_TIERS:
        return True
    return tier == "fast" and bool(outcome.get("hesse_trusted"))


def _apply_and_finish(summary, session, session_prior, card, records, runcal, g, plan, entries,
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
        _materialize_branch_candidates(session)
    _rebuild_pooled_prior(card, session, session_prior, t_round=t_round)
    # Identity assignment is deliberately after pooled-master updates. It reads the complete
    # candidate field, may revise earlier labels, then rebuilds the connected fold-only prior.
    track = _apply_tracked_branch(session, card, t_round=t_round)
    # ADR-019: only now is the target authoritative — triage has had its say and the tracker has
    # reassigned or cleared every selection. The projection is resolved against `/2`'s stored
    # per-draw mode energies, so a track revision changes it without any refit.
    _resolve_target_projections(session, workspace.root / "data")
    if track is not None:
        summary.context_lines.append(
            f"branch track revision {track['revision']}: "
            f"{len(track['frontiers']['confirmed_indices'])} connected point(s)")
        if track["frontiers"].get("frontier_blocked"):
            summary.context_lines.append("branch_ambiguity; frontier_blocked")

    # Persist cross-q views only after the authoritative prior has been rebuilt.
    _store_branch_models(card.state, t_round)
    _store_branch_beliefs(card.state, t_round, period=_dispersion_period(card),
                          hypotheses=_campaign_hypotheses(card))
    _update_hypothesis_mismatch(card.state, t_round, period=_dispersion_period(card),
                                hypotheses=_campaign_hypotheses(card))

    summary.counts = _tally(session)
    session.last_run = {"counts": summary.counts,
                        "scan_gaps": [f"{e}: {a}->{b}" for (e, a, b) in summary.gaps],
                        "n_fitted_this_run": summary.n_fitted_this_run}

    with timer.stage("write_results"):
        _write_results_csv(workspace, session)
    with timer.stage("session_save"):
        workspace.save(card, session)


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
        _materialize_branch_candidates(session)
    _rebuild_pooled_prior(card, session, session_prior, t_round=t_round)
    track = _apply_tracked_branch(session, card, t_round=t_round)
    _resolve_target_projections(session, workspace.root / "data")
    # Tracking may annotate individual rows for display. Certification is master-only, so restore
    # their exact pre-publication ledger payload after the master-derived state has been rebuilt.
    for key, entry in physical_rows.items():
        session.scans[key] = entry
    _store_branch_models(card.state, t_round)
    _store_branch_beliefs(card.state, t_round, period=_dispersion_period(card),
                          hypotheses=_campaign_hypotheses(card))
    _update_hypothesis_mismatch(card.state, t_round, period=_dispersion_period(card),
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


def _resolve_target_projections(session: SessionState, data_dir) -> int:
    """Resolve and publish one ``isar.target_projection/2`` per pooled master (ADR-019).

    Called ONLY after branch triage, ``_materialize_branch_candidates`` and the connected tracker
    have run, because all three can still move the target: triage can withhold the delivery, the
    tracker can reassign the selected candidate or clear it entirely, and neither is visible at fit
    time. Each projection is bound to the authority basis that decided it, so a later track revision
    produces a NEW projection against the unchanged ``/2`` — no refit is involved.
    """
    if data_dir is None:
        return 0
    published = 0
    for key in sorted(session.scans):
        if not key.startswith("master:"):
            continue
        entry = session.scans[key]
        authority = (entry.provenance or {}).get("target_projection_authority") or {}
        for outcome in entry.detectors.values():
            ref = outcome.get("predictive_ref")
            if not outcome.get("pooled") or not ref:
                continue
            try:
                artifact = json.loads(
                    (Path(data_dir).parent / str(ref)).read_text(encoding="utf-8"))
                projection = predictive_artifact.build_target_projection(
                    artifact, outcome,
                    master_revision=authority.get("master_revision")
                    or outcome.get("master_revision"),
                    calibration_fingerprint=authority.get("calibration_fingerprint"),
                    policy_fingerprint=authority.get("policy_fingerprint"),
                    action_role=authority.get("action_role") or "master")
                projection_ref = _persist_content_addressed(
                    data_dir, "target_projection", projection)
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
                # A projection is steering input, never a scientific number: record why it is
                # missing and leave the master's fit and every other artifact untouched.
                log.warning("%s: target projection unavailable (%s): %s",
                            key, type(error).__name__, error)
                entry.provenance["target_projection_unavailable"] = {
                    "reason_code":
                        predictive_artifact.PROJECTION_UNAVAILABLE_CONSTRUCTION_FAILED,
                    "error_type": type(error).__name__, "message": str(error)}
                continue
            outcome["target_projection_ref"] = projection_ref
            entry.provenance["target_projection_ref"] = projection_ref
            entry.provenance.pop("target_projection_unavailable", None)
            published += 1
    return published


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
    except Exception as exc:  # noqa: BLE001 — degrade, never stall the batch (DESIGN §9)
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

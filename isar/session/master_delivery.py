"""Master (pooled) delivery: pooled-master fitting, promotion bookkeeping, and its primitives.

Split verbatim out of :mod:`isar.session.driver` (P2.6b). The certification *stage orchestrator*
(`_run_master_certification`) stays in the driver; the certification primitives it calls live here.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import tempfile
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from pathlib import Path

import numpy as np

from isar.analyze import gold as gold_mod
from isar.analyze.context import ScanContext, normalized_exposure
from isar.reduce import collate as collate_mod
from isar.predictive import PREDICTIVE_CONTRACT
from isar.predictive import json_fingerprint as _json_fingerprint
from isar.session import branch_assignment as BA
from isar.session import branch_model, calibrations, session_prior
from isar.session import predictive_artifact
from isar.session.outcome import FitOutcome
from isar.session.state import ScanEntry, SessionState
from isar.session.workspace import Workspace
from isar.session import scan_delivery
from isar.session.scan_delivery import (AnalysisWorkIncomplete, FULL_TIERS,
                                        _PRIOR_NARROW_SIGMA)

log = logging.getLogger(__name__)


# Synthetic scan_number range for master (pooled) ledger entries so they sort AFTER real scans and
# never collide with a real scan_number. The real identity is the scan_id + member-set content_hash.
_MASTER_SCAN_BASE = 9_000_000


def _member_labels(members) -> list[str]:
    """Chain-of-custody labels for a pooled master's members (MergedSpectrum.members tuples
    ``(experiment_id, scan_number, detector_id)``): always the full ledger key
    ``experiment:scan:detector``. Preserves ``ms.members`` order.

    Never a bare scan number. The collation key is (sample, q, T) and may span detectors, while the
    master row itself is stamped with only ``members[0]``'s detector — so a bare label left a
    consumer unable to tell which detector's arena a member belongs to. A reader that must not
    count the same neutrons twice (the scene layer's observation arenas) then either misses a
    member (double-counting its evidence) or suppresses an unrelated detector's scan. Qualifying
    every label is the only form that answers the question from the cell alone."""
    return [f"{m[0]}:{m[1]}:{m[2]}" for m in members]


_MASTER_FIT_CONTRACT = "isar.master_fit/2"


def _master_fit_fingerprint(ms, card, calres, g, prior, *, errors_tier, M, seed,
                            boot_adaptive, bayes_opts) -> str:
    """Fingerprint the complete numerical input closure of one pooled-master fit."""
    from isar.session.certification import master_fit_fingerprint

    state = getattr(card, "state", None)
    target = getattr(state, "campaign_target", None) or {}
    frozen_state = {
        "dispersion_prior": getattr(state, "dispersion_prior", None),
        "branch_models": getattr(state, "branch_models", None),
        "branch_belief": getattr(state, "branch_belief", None),
        "branch_belief_models": getattr(state, "branch_belief_models", None),
        "branch_tracks": getattr(state, "branch_tracks", None),
        "hypothesis_snapshot": getattr(state, "hypothesis_snapshot", None),
        "campaign_target": target,
    }
    return master_fit_fingerprint(
        spectrum_digest=collate_mod.merged_spectrum_digest(ms),
        policy=card.policy,
        resolved_guidelines={
            "min_fit_points": g.min_fit_points,
            "monitor_frac": g.monitor_frac,
        },
        resolved_calibration=calres.summary(), frozen_state=frozen_state,
        errors_tier=errors_tier, M=M, seed=seed,
        boot_adaptive=boot_adaptive, bayes_opts=bayes_opts)


def _persist_master_predictive(data_dir, payload: dict) -> str:
    """Blob-first, content-addressed publication of one predictive ensemble."""
    return _persist_content_addressed(data_dir, "master_predictive", payload)


def _persist_content_addressed(data_dir, subdirectory: str, payload: dict) -> str:
    """Blob-first, content-addressed publication of one JSON side artifact."""
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    root = Path(data_dir) / subdirectory
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{digest}.json"
    if not target.exists():
        fd, temporary = tempfile.mkstemp(prefix=f".{digest}.", suffix=".tmp", dir=root)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError as cleanup_error:
                log.warning("could not remove incomplete predictive artifact %s: %s",
                            temporary, cleanup_error)
            raise
    return target.relative_to(Path(data_dir).parent).as_posix()


def _predicted_master_analysis_seconds(payload: dict) -> float:
    """Deterministic seconds estimate for ranking future master-analysis work.

    Observed fit wall time depends on worker placement and host load, so persisting it inside a
    content-addressed scientific artifact made serial and parallel reductions hash differently.
    The stable proxy scales with the plausible-model parameter dimensions and evaluated energy
    bins—the dominant repeatable work dimensions—while remaining explicitly an estimate.
    """
    complexity = 0
    for model in payload.get("models") or ():
        n_parameters = max(len(model.get("parameter_names") or ()), 1)
        expected = model.get("expected_counts") or ()
        n_points = len(expected[0]) if expected and isinstance(expected[0], list) else 1
        complexity += n_parameters * max(n_points, 1)
    return round(max(0.1, 0.1 + 0.00015 * complexity), 4)


_PROMOTION_CANDIDATE_IDENTITY_FIELDS = (
    "label",
    "k",
    "ndof",
    "cost",
    "cost_per_dof",
    "n_modes",
    "delta_aic",
    "akaike_weight",
    "usable",
)


def _promotion_candidate_identity(candidates) -> list[dict] | None:
    """Pre-STAT-1 numerical candidate projection used by promotion identity."""
    if candidates is None:
        return None
    return [
        {
            key: row.get(key)
            for key in _PROMOTION_CANDIDATE_IDENTITY_FIELDS
            if key in row
        }
        for row in candidates
    ]


def _record_master_promotion(data_dir, entry: ScanEntry, *, source_revision: str,
                             M: int, seed: int) -> None:
    """Persist and publish a synchronous auto-tier promotion for this exact master revision."""
    provenance = entry.provenance or {}
    reasons = list(provenance.get("escalation_reasons") or ())
    if (provenance.get("errors_tier_requested") != "auto"
            or provenance.get("errors_tier_effective") != "full" or not reasons):
        return
    from isar.session.promotions import PromotionKey, PromotionStore

    outcomes = [outcome for outcome in entry.detectors.values() if outcome.get("pooled")]
    # Candidate rows gained additive STAT-1 classifications.  Promotion identity remains the
    # pre-STAT-1 numerical contract: content-addressed semantics may change without reminting the
    # scientific retry for an otherwise identical master fit.
    candidates = outcomes[0].get("candidates") if outcomes else None
    scientific_candidates = _promotion_candidate_identity(candidates)
    point_fingerprint = _json_fingerprint({
        "master_id": entry.scan_id,
        "source_revision": source_revision,
        "best_label": outcomes[0].get("best_label") if outcomes else None,
        "candidates": scientific_candidates,
    })
    key = PromotionKey(
        master_id=entry.scan_id, source_revision=source_revision,
        spectrum_digest=str(provenance.get("spectrum_digest") or ""),
        point_fingerprint=point_fingerprint, tier="full", M=int(M), seed=int(seed))
    store = PromotionStore(Path(data_dir).parent)
    store.begin(key, escalation_reasons=reasons)
    store.complete(key, asdict(entry))
    record = store.resolve_publication(key, current_revision=source_revision)
    if record["status"] == "published":
        store.supersede_other_attempts(key)
    entry.provenance.update({
        "promotion_id": key.digest,
        "promotion_state": record["status"],
        "promotion_source_revision": source_revision,
        "promotion_publication_outcome": record["publication_outcome"],
        "point_fit_fingerprint": point_fingerprint,
    })


def current_master_revisions(session: SessionState) -> dict[str, str]:
    """Return the authoritative published content revision for every pooled master."""
    return {
        master_id: str(entry.content_hash)
        for master_id, entry in session.scans.items()
        if master_id.startswith("master:") and entry.content_hash
    }


def unresolved_current_promotions(workspace_root, session: SessionState) -> list[dict]:
    """Reconcile stale attempts and return promotions blocking a terminal/certification action."""
    from isar.session.promotions import PromotionStore
    return PromotionStore(workspace_root).reconcile(current_master_revisions(session))


def _fit_master_group_task(args):
    """Top-level (picklable) entry point for a master-group pool worker: fit one merged group.

    Bootstrap is forced serial inside the child (``boot_workers=1``, no shared pool) — Layer A owns
    the process workers, the same trade-off as the scan pool in :func:`_run_tasks`, and the seeded
    bootstrap is invariant to worker count / where the refits run (errors.bootstrap). The returned
    :class:`ScanEntry` carries its own timings in ``provenance``; the parent applies ``session.put``
    serially afterward in sorted-group order. Every argument is picklable (ms/grp are plain collation
    dataclasses, member_rec a ScanRecord, card/g/runcal as the scan pool already ships)."""
    (ms, grp, member_rec, member_det, card, runcal, g, master_id, scan_number, chash,
     M, seed, errors_tier, boot_adaptive, structure_hint, bayes_opts, *role_tail) = args
    fit_role = role_tail[0] if role_tail else "master"
    shadow_enabled = bool(role_tail[1]) if len(role_tail) > 1 else True
    # Pass structure_hint only when set: with the reuse policy off (default) hint is None and this
    # call is byte-identical to the pre-B2 call (keeps the "change nothing when off" guarantee).
    hint_kw = {"structure_hint": structure_hint} if structure_hint is not None else {}
    role_kw = {"fit_role": fit_role} if fit_role != "master" else {}
    shadow_kw = {} if shadow_enabled else {"shadow_enabled": False}
    return _fit_master_group(ms, grp, member_rec, member_det, card, runcal, g, master_id,
                             scan_number, chash, M=M, seed=seed, errors_tier=errors_tier,
                             boot_workers=1, boot_executor=None, boot_adaptive=boot_adaptive,
                             bayes_opts=bayes_opts, **shadow_kw, **role_kw, **hint_kw)


def _stamp_master_analysis(entry: ScanEntry, *, errors_tier: str,
                           fit_fingerprint: str | None,
                           spectrum_digest: str | None) -> ScanEntry:
    entry.provenance = {
        **(entry.provenance or {}),
        "analysis_tier": errors_tier,
        "analysis_tier_effective": (entry.provenance or {}).get(
            "errors_tier_effective", errors_tier),
        "master_fit_contract": _MASTER_FIT_CONTRACT,
        # The predictive artifact this master was produced under. The carry path in
        # :func:`_fit_masters` requires the current value, so a contract bump regenerates the
        # artifact from an unchanged pool on the next run instead of carrying a stale one.
        "predictive_contract": PREDICTIVE_CONTRACT,
    }
    if fit_fingerprint is not None:
        entry.provenance["master_fit_fingerprint"] = fit_fingerprint
    if spectrum_digest is not None:
        entry.provenance["spectrum_digest"] = spectrum_digest
    return entry


def _certification_entry_reusable(entry: ScanEntry | None, *, content_hash_value: str,
                                  spectrum_digest: str, fit_fingerprint: str,
                                  requested_tier: str) -> bool:
    """Integrity and scientific-completion gate shared by workspace/checkpoint reuse."""
    from isar.session.certification import tier_satisfies

    if entry is None or entry.content_hash != content_hash_value:
        return False
    provenance = entry.provenance or {}
    if provenance.get("spectrum_digest") != spectrum_digest:
        return False
    if provenance.get("master_fit_fingerprint") != fit_fingerprint:
        return False
    if not tier_satisfies(provenance.get("analysis_tier"), requested_tier):
        return False
    # A deterministic scientific non-result is complete.  A caught fit exception is not: retry it.
    if entry.status == "non_result" and any("fit error (" in reason for reason in entry.reasons):
        return False
    if entry.status == "fitted":
        pooled = [outcome for outcome in entry.detectors.values() if outcome.get("pooled")]
        return len(pooled) == 1 and pooled[0].get("n_members") is not None
    return entry.status == "non_result"


def _publication_hashes(workspace: Workspace) -> dict[str, str]:
    result = {}
    for name, path in (("session.json", workspace.session_path),
                       ("card.state.json", workspace.state_path),
                       ("results.csv", workspace.report_dir / "results.csv"),
                       ("card.policy.toml", workspace.policy_path)):
        result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _assert_canonical_indices_cover_path(worklist, excluded) -> None:
    """Every objective-path point must be accounted for exactly once — certified or excluded.

    The worklist alone is NOT dense: the manifest builder legitimately omits structurally-
    unresolvable points (the inferred reduced-q origin has no pooled master by construction) into
    ``excluded_points``.  So the contiguity invariant is over the UNION of the worklist and excluded
    canonical indices, not the worklist by itself.  Requiring the worklist to equal
    ``range(len(worklist))`` on its own broke every campaign whose path starts at a zone-center
    origin, where the surviving canonical indices begin at 1.
    """
    combined = sorted([int(unit.get("canonical_index", -1)) for unit in worklist]
                      + [int(item.get("canonical_index", -1)) for item in excluded])
    if combined != list(range(len(combined))):
        raise ValueError("certification worklist and excluded points must together cover "
                         "contiguous, unique canonical indices")


def _fit_masters(collation, records, card, runcal, g, session, *, data_dir=None, M, seed_base,
                 errors_tier="full", force=False, force_masters=frozenset(),
                 boot_workers=1, boot_executor=None,
                 boot_adaptive=False, scan_workers=1, bayes_opts=None, per_master=None,
                 checkpoint_store=None, completed_master_keys=None, deadline_gate=None) -> int:
    """Fit the merged master scan for each (Q,T) group; returns how many were (re)fit this run.

    Runs only when the card opts in (``collation.merge == "merge"``). Each group's pooled
    ``MergedSpectrum`` (combine-before-divide ΣN counts) is fit through the same ``fit_one`` path as a
    single scan and stored as a ``master:<key>`` ledger entry marked ``pooled=True``. Idempotent on the
    member SET: the entry's ``content_hash`` is a hash of ``ms.members``, so a master refits exactly
    when a member scan arrives or drifts (``collate`` re-runs over all records every batch, so masters
    cannot piggyback on the per-scan freshness flags). ``force_masters`` (ledger ids) exempts exactly
    those masters from the unchanged-member carry, so a predictive escalation re-fits its one target
    at the requested tier without forcing the whole batch. Never fatal — a bad group yields a
    ``non_result`` entry, never a crash.

    When more than one group needs refitting and the scan-worker budget allows, the group fits run in
    parallel over an order-preserving ``ProcessPoolExecutor.map`` (bootstrap forced serial in each
    child, same Layer-A trade-off as the scan pool); ``session.put`` is still applied serially in the
    original sorted-group order afterward, so the result is bit-identical to the serial path. A single
    refit (the normal-iteration case) keeps the in-process serial path with the shared bootstrap pool.
    """
    if collation is None or getattr(collation, "merge", "separate") != "merge":
        if force_masters:
            raise ValueError(
                f"force_masters: {sorted(force_masters)!r} requested but no merged collation "
                "exists — the targeted refit cannot happen")
        return 0
    from isar.session.state import canonical_hash

    rec_by_scan = {rec.scan_number: rec for rec in records}
    # Card policy opt-in (default OFF): reuse an invalidated group's previous fitted structure as a
    # cold-refit hint, skipping the full enumeration when the gates permit. OFF ⇒ hint is always
    # None ⇒ nothing below the fit call changes (see fit_structures_two_stage_hinted).
    _pol = getattr(card, "policy", None)
    reuse_structure = bool((getattr(_pol, "fitting", None) or {}).get("reuse_master_structure", False))
    # --- Refit list first: apply the membership-hash skip unchanged (carry path untouched). ---
    # idx is the ORIGINAL sorted-group enumeration position (NOT the refit-list position) so the
    # per-group seed `seed_base + _MASTER_SCAN_BASE + idx` and synthetic scan_number stay stable.
    refits = []     # (idx, grp, ms, master_id, chash, member_rec, member_det, structure_hint)
    seen_master_ids: list[str] = []
    for idx, grp in enumerate(sorted(collation.groups, key=lambda gg: str(gg.key))):
        ms = getattr(grp, "merged", None)
        if ms is None or ms.n_members < 1:
            continue
        master_id = "master:" + "_".join(str(k) for k in grp.key)
        seen_master_ids.append(master_id)
        chash = canonical_hash([list(m) for m in ms.members])
        prev = session.get(master_id)               # read BEFORE the refit overwrites the ledger
        carried_contract = (prev.provenance or {}).get("predictive_contract") if prev else None
        if not force and master_id not in force_masters and prev is not None \
                and prev.content_hash == chash \
                and carried_contract == PREDICTIVE_CONTRACT:
            # Member set unchanged → carry the prior master. Backfill the member provenance onto an
            # older ledger entry that predates this field: the content-hash match proves the member
            # set is byte-identical, so `_member_labels(ms.members)` is exact — pure bookkeeping, no
            # refit. In-place mutation persists (session.get returns the live ledger ref).
            for oc in prev.detectors.values():
                if oc.get("pooled") and "members" not in oc:
                    oc["members"] = _member_labels(ms.members)
            if prev.provenance and "members" not in prev.provenance:
                prev.provenance["members"] = [list(m) for m in ms.members]
            if data_dir is not None and not any(
                    oc.get("spectrum_ref") for oc in prev.detectors.values() if oc.get("pooled")):
                spectrum_ref = collate_mod.persist_merged_spectrum(data_dir, ms)
                for oc in prev.detectors.values():
                    if oc.get("pooled"):
                        oc["spectrum_ref"] = spectrum_ref
                prev.provenance["spectrum_ref"] = spectrum_ref
            continue
        member_scan, member_det = ms.members[0][1], ms.members[0][2]
        # Structure hint: the previous entry's best_label, iff it was a completed (fitted) master
        # and the reuse policy is on. Anything else (cold group, held/non_result, policy off) → None.
        hint = None
        if reuse_structure and prev is not None and prev.status == "fitted":
            hint = (prev.detectors.get(member_det or "merged") or {}).get("best_label")
        elif reuse_structure and ms.n_members == 1:
            # No previous master to reuse (first visit to this q — the common case), but a
            # one-member pool is the SAME data and the SAME enumeration as its member scan, fit
            # seconds ago on the scan-fit pass. That scan's winner is therefore just as legitimate
            # a hint as a previous master's: _try_structure_hint's gates (same as always) decide
            # whether it actually explains the pooled data, so a bad hint still falls back to the
            # full enumeration rather than silently mis-fitting.
            member_exp = ms.members[0][0]
            member_entry = session.get(f"{member_exp}:{member_scan}:{member_det}")
            if member_entry is not None and member_entry.status == "fitted":
                hint = (member_entry.detectors.get(member_det) or {}).get("best_label")
        refits.append((idx, grp, ms, master_id, chash, rec_by_scan.get(member_scan), member_det,
                       hint))
    if force_masters:
        # A requested id that matches no collation group means the caller's targeted refit would
        # silently not happen — and the predictive-escalation charge that requested it is durable
        # (the same master revision is never re-promoted), so a quiet no-op would record a
        # "completed" promotion whose basis is still missing. Fail the run instead: the escalation
        # then records a visible "failed" outcome rather than a false success.
        unmatched = sorted(force_masters - set(seen_master_ids))
        if unmatched:
            raise ValueError(
                f"force_masters: {unmatched!r} match no collation group "
                f"(known masters: {sorted(seen_master_ids)!r})")
    if not refits:
        return 0

    cached_entries: dict[int, ScanEntry] = {}
    pending_refits = []
    for refit_index, refit in enumerate(refits):
        _idx, _grp, _ms, master_id, chash, _rec, _det, _hint = refit
        cached = (None if checkpoint_store is None else checkpoint_store.load(
            f"master:{master_id}", content_hash=chash, guidelines_hash=""))
        if cached is None:
            pending_refits.append((refit_index, refit))
        else:
            cached_entries[refit_index] = cached
    master_completed = len(cached_entries)
    if checkpoint_store is not None and master_completed:
        checkpoint_store.report_progress(master_completed)
    pending = [refit for _index, refit in pending_refits]
    fresh_entries: list[ScanEntry] = []

    # --- Fit the refits: parallel across groups when it pays, else serial in-process. ---
    if len(pending) > 1 and scan_workers > 1:
        # Parallel: bootstrap forced serial in each child (boot_workers=1, no shared pool). map
        # preserves order → entries[k] aligns with refits[k], keeping the serial ledger order.
        arg_list = [(ms, grp, member_rec, member_det, card, runcal, g, master_id,
                     _MASTER_SCAN_BASE + idx, chash, M, seed_base + _MASTER_SCAN_BASE + idx,
                     errors_tier, boot_adaptive, hint, bayes_opts, "master",
                     deadline_gate is None or deadline_gate.deadline is None)
                    for (idx, grp, ms, master_id, chash, member_rec, member_det, hint) in pending]
        max_workers = min(scan_workers, len(arg_list))
        with ProcessPoolExecutor(max_workers=max_workers) as ex_pool:
            for offset in range(0, len(arg_list), max_workers):
                if deadline_gate is not None and not deadline_gate.begin_wave():
                    raise AnalysisWorkIncomplete(
                        f"analysis deadline reached after {len(fresh_entries)}/{len(arg_list)} "
                        "master fit units")
                wave = arg_list[offset:offset + max_workers]
                for entry in ex_pool.map(_fit_master_group_task, wave):
                    if checkpoint_store is not None:
                        checkpoint_store.save(f"master:{entry.scan_id}", entry)
                        master_completed += 1
                        checkpoint_store.report_progress(master_completed)
                    fresh_entries.append(entry)
    else:
        # Serial (normal iteration, ≤1 refit, or single worker) — hot path unchanged. Reuse the
        # batch's shared bootstrap pool when given; else open one for the master pass (only the full
        # tier bootstraps, so a pool is pointless otherwise).
        own_pool = None
        ex = boot_executor
        if ex is None and errors_tier in FULL_TIERS and boot_workers > 1:
            own_pool = ProcessPoolExecutor(max_workers=boot_workers)
            ex = own_pool
        try:
            for (idx, grp, ms, master_id, chash, member_rec, member_det, hint) in pending:
                if deadline_gate is not None and not deadline_gate.begin_wave():
                    raise AnalysisWorkIncomplete(
                        f"analysis deadline reached after {len(fresh_entries)}/{len(pending)} "
                        "master fit units")
                shadow_kw = (
                    {}
                    if deadline_gate is None or deadline_gate.deadline is None
                    else {"shadow_enabled": False}
                )
                entry = _fit_master_group(
                    ms, grp, member_rec, member_det, card, runcal, g, master_id,
                    _MASTER_SCAN_BASE + idx, chash, M=M,
                    seed=seed_base + _MASTER_SCAN_BASE + idx, errors_tier=errors_tier,
                    boot_workers=boot_workers, boot_executor=ex,
                    boot_adaptive=boot_adaptive, bayes_opts=bayes_opts,
                    **shadow_kw,
                    **({"structure_hint": hint} if hint is not None else {}))
                if checkpoint_store is not None:
                    checkpoint_store.save(f"master:{entry.scan_id}", entry)
                    master_completed += 1
                    checkpoint_store.report_progress(master_completed)
                fresh_entries.append(entry)
        finally:
            if own_pool is not None:
                own_pool.shutdown()

    for (refit_index, _refit), entry in zip(pending_refits, fresh_entries):
        cached_entries[refit_index] = entry
    entries = [cached_entries[index] for index in range(len(refits))]

    # Apply ledger mutations + timing entries serially, in the original sorted-group order.
    for (idx, grp, ms, master_id, chash, member_rec, member_det, hint), entry in zip(refits, entries):
        # Blob-first publication means interruption can leave an orphan, never a dangling ref.
        if data_dir is not None:
            spectrum_ref = collate_mod.persist_merged_spectrum(data_dir, ms)
            for outcome in entry.detectors.values():
                if outcome.get("pooled"):
                    outcome["spectrum_ref"] = spectrum_ref
            entry.provenance["spectrum_ref"] = spectrum_ref
            predictive_payload = entry.provenance.pop("predictive_payload", None)
            if predictive_payload is not None:
                predictive_payload["predicted_analysis_seconds"] = (
                    _predicted_master_analysis_seconds(predictive_payload))
                predictive_ref = _persist_master_predictive(data_dir, predictive_payload)
                for outcome in entry.detectors.values():
                    if outcome.get("pooled"):
                        outcome["predictive_ref"] = predictive_ref
                entry.provenance["predictive_ref"] = predictive_ref
            _record_master_promotion(
                data_dir, entry, source_revision=chash, M=M,
                seed=seed_base + _MASTER_SCAN_BASE + idx)
        session.put(entry)
        if completed_master_keys is not None:
            completed_master_keys.append(f"master:{master_id}")
        if per_master is not None:
            per_master.append(
                {"master_id": master_id,
                 "seconds": entry.provenance.get("fit_seconds"),
                 "two_stage_seconds": entry.provenance.get("two_stage_seconds"),
                 "errors_attach_seconds": entry.provenance.get("errors_attach_seconds"),
                 "bootstrap_seconds": entry.provenance.get("bootstrap_seconds"),
                 "minos_seconds": entry.provenance.get("minos_seconds"),
                 "profile_seconds": entry.provenance.get("profile_seconds"),
                 "bayes_seconds": entry.provenance.get("bayes_seconds"),
                 "bootstrap_M_requested": entry.provenance.get("bootstrap_M_requested"),
                 "bootstrap_M_effective": entry.provenance.get("bootstrap_M_effective")})
    return len(refits)


def _fit_master_group(ms, grp, member_rec, member_det, card, runcal, g, master_id, scan_number,
                      chash, *, M, seed, errors_tier="full", boot_workers=1, boot_executor=None,
                      boot_adaptive=False, structure_hint=None, bayes_opts=None,
                      fit_role="master", shadow_enabled=True) -> ScanEntry:
    """Fit one merged group's pooled spectrum → a ``pooled=True`` ScanEntry (or a non_result).

    ``structure_hint`` (the group's previous fitted best_label, opt-in via card policy
    ``[fitting].reuse_master_structure``) offers the structure-skip shortcut; an entry produced via
    an accepted hint is stamped ``provenance["structure_skipped"] = True``."""
    _t_fit0 = time.perf_counter()
    det = member_det or "merged"
    base = dict(scan_id=master_id, scan_number=int(scan_number), scan_type="measurement",
                content_hash=chash, guidelines_hash="")
    q = list(grp.key_fields.get("q") or [0.0, 0.0, 0.0])
    T_disp = grp.key_fields.get("T")
    T = float(T_disp) if T_disp is not None else float("nan")
    spectrum_digest = collate_mod.merged_spectrum_digest(ms)
    if member_rec is None:
        return _stamp_master_analysis(
            ScanEntry(status="non_result",
                      reasons=[f"{master_id}: no member record for resolution"], **base),
            errors_tier=errors_tier, fit_fingerprint=None,
            spectrum_digest=spectrum_digest)
    if (getattr(card.policy, "calibration", None) or {}).get("method") == "tabulated_elastic":
        # Members carry their own curve and zero; one member's applied to the pooled counts would be
        # wrong, so no pooled master is fitted (the members' own fits stand).
        return _stamp_master_analysis(
            ScanEntry(status="held_for_calibration",
                      reasons=[f"{master_id}: pooling across tabulated calibrations not supported"], **base),
            errors_tier=errors_tier, fit_fingerprint=None,
            spectrum_digest=spectrum_digest)
    try:
        calres = calibrations.resolve(member_rec, member_det, card, runcal)
    except Exception as exc:  # noqa: BLE001
        return _stamp_master_analysis(
            ScanEntry(status="non_result",
                      reasons=[f"{master_id}: resolution failed ({exc})"], **base),
            errors_tier=errors_tier, fit_fingerprint=None,
            spectrum_digest=spectrum_digest)

    sample = str(grp.key_fields.get("sample", ""))
    period = scan_delivery._dispersion_period(card)
    t_round = float((card.policy.collation or {}).get("t_round", 5.0))
    target_branch, target_label, energy_search = scan_delivery._campaign_target(card)
    prior = (session_prior.prior_for(card.state, sample=sample, T=T, q=q, period=period,
                                     t_round=t_round, branch_id=target_branch)
             if period is not None else None)
    fit_fingerprint = _master_fit_fingerprint(
        ms, card, calres, g, prior, errors_tier=errors_tier, M=M, seed=seed,
        boot_adaptive=boot_adaptive, bayes_opts=bayes_opts)

    x = np.asarray(ms.energy, dtype=float)
    counts = np.asarray(ms.intensity, dtype=float)
    mask = np.asarray(ms.mask, dtype=bool)
    raw_monitor = getattr(ms, "monitor", None)
    monitor = None if raw_monitor is None else np.asarray(raw_monitor, dtype=float)
    # The reference is CARRIED into the fit context (never recomputed at publication time): it is
    # the amplitude basis the predictive artifact publishes as ``fit_reference_exposure``.
    exposure_scale, reference_exposure = ((None, None) if monitor is None
                                          else normalized_exposure(monitor, mask))
    if exposure_scale is not None:
        mask = mask & (exposure_scale > 0)
    if int(mask.sum()) < g.min_fit_points:
        return _stamp_master_analysis(
            ScanEntry(status="non_result",
                      reasons=[f"{master_id}: undersampled pool ({int(mask.sum())} < "
                               f"{g.min_fit_points} kept points)"], **base),
            errors_tier=errors_tier, fit_fingerprint=fit_fingerprint,
            spectrum_digest=spectrum_digest)
    # Pooled spectrum → ScanContext directly (bypasses build_context: a merged group has no single
    # ScanRecord/normalization; MergedSpectrum.mask already encodes monitor validity).
    prior_ao = float(prior.omega0) if (prior is not None and np.isfinite(prior.omega0)) else None
    target_anchors = scan_delivery._target_fold_anchor_count(card, sample=sample, branch=target_branch)
    established = target_branch == "acoustic" or target_anchors >= branch_model.MIN_ANCHORS
    # A cold optic target has no numeric anchor.  Keep both the IC and primary-mode choice broad;
    # Bayesian assignment below will select a candidate without forcing the upper-energy mode.
    ao = prior_ao if established else None
    # Narrow the IC only when the belief is confident (see _PRIOR_NARROW_SIGMA); else keep the acoustic
    # mode hint but a wide IC so the pooled fit still finds the resolvable acoustic.
    ic_ao = (prior_ao if (established and prior_ao is not None
                          and float(prior.omega0_sigma or 1e9) <= _PRIOR_NARROW_SIGMA) else None)
    ctx = ScanContext(x=x, counts=counts, mask=mask, T=T, center=calres.center,
                      gauss_fwhm=calres.gauss_fwhm, lorentz_fwhm=calres.lorentz_fwhm,
                      instrument=card.policy.instrument, energy_status=card.policy.energy_status,
                      prior_omega=ic_ao, quasielastic_fwhm=card.policy.quasielastic_fwhm,
                      acoustic_omega=ao, exposure_scale=exposure_scale,
                      exposure_reference=reference_exposure,
                      exposure_source=None if reference_exposure is None else "monitor")
    near_gamma = (target_branch == "acoustic" and ao is not None
                  and ao < BA.RESOLVABLE_FWHM_FACTOR * calres.gauss_fwhm)
    descriptor = scan_delivery._build_descriptor(ctx)
    try:
        fit = scan_delivery.fit_one(ctx, card, prior=prior, M=M, seed=seed, errors_tier=errors_tier,
                      boot_workers=boot_workers, boot_executor=boot_executor,
                      boot_adaptive=boot_adaptive, descriptor=descriptor,
                      structure_hint=structure_hint, bayes_opts=bayes_opts,
                      predictive_n_members=int(ms.n_members), fit_role=fit_role,
                      shadow_enabled=shadow_enabled)
    except Exception as exc:  # noqa: BLE001 — one unfittable pool must not kill the batch (N=16 wrinkle)
        return _stamp_master_analysis(
            ScanEntry(status="non_result",
                      reasons=[f"{master_id}: fit error ({type(exc).__name__}): {exc}"], **base),
            errors_tier=errors_tier, fit_fingerprint=fit_fingerprint,
            spectrum_digest=spectrum_digest)

    # Phase A: the pooled master runs the SAME branch triage as a per-scan fit — the acoustic hint
    # (ctx.acoustic_omega) makes primary_phonon_index report the acoustic mode (not the brighter optic),
    # and the triage withholds a non-acoustic/unresolvable pool as branch_continuation. Without this a
    # boundary master delivers the optic as the acoustic ω₀.
    (branch_id, assignment_entropy, branch_weight, resolvable, secondary_features,
     branch_posterior, candidate_id, candidate_state, selected_energy) = scan_delivery._apply_branch_triage(
        fit, descriptor, ctx, card=card, sample=sample, T=T, q=q, t_round=t_round,
        near_gamma=near_gamma, master=True)

    disposition = gold_mod.verdict_disposition(fit["verdict"])
    exposure_info = scan_delivery._effective_exposure_for(
        None, ctx, fit, absolute_exposure=monitor,
        absolute_exposure_source=("monitor" if monitor is not None else None),
        target_center=selected_energy)
    predictive_payload, predictive_unavailable = _build_predictive_payload(fit, ctx, master_id)
    model_interval = ((predictive_payload or {}).get("omega_interval")
                      if predictive_payload is not None else None)
    outcome = FitOutcome(
        detector_id=det, verdict=fit["verdict"], disposition=disposition,
        confidence=fit["confidence"], omega0=fit["omega0"], omega0_err=fit["omega0_err"],
        interval=fit["interval"], modality=fit["modality"], gamma=fit["gamma"],
        gamma_provisional=(card.policy.energy_status == "absolute"),
        best_label=fit["best_label"], cost_per_dof=fit["cost_per_dof"],
        q=q, T=T, sample=str(grp.key_fields.get("sample", "")), prior_used=None,
        calib=calres.summary(), reasons=fit["reasons"], n_modes=fit["n_modes"], modes=fit["modes"],
        amplitudes=fit.get("amplitudes"), candidates=fit.get("candidates"),
        model_manifest=fit.get("model_manifest"),
        interval_reliable=fit.get("interval_reliable", True), omega0_profile=fit.get("omega0_profile"),
        hesse_interval=fit.get("hesse_interval"), hesse_trusted=fit.get("hesse_trusted", False),
        pooled=True, n_members=int(ms.n_members), members=_member_labels(ms.members),
        master_revision=chash, gamma_at_bound=bool(fit.get("gamma_at_bound", False)),
        model_interval=(tuple(model_interval) if model_interval is not None else None),
        model_robust=bool(model_interval is not None and fit.get("modality") != "fork"),
        model_entropy=((predictive_payload or {}).get("model_entropy_nats")
                       if predictive_payload is not None else None),
        linewidth_state=("resolution_limited" if fit.get("gamma_at_bound") else "identified"),
        pool_consistent=getattr(ms, "pool_consistent", None),
        consistency_statistic=getattr(ms, "consistency_statistic", None),
        consistency_pvalue=getattr(ms, "consistency_pvalue", None),
        consistency_alpha=getattr(ms, "consistency_alpha", None),
        consistency_seed=getattr(ms, "consistency_seed", None),
        fit_selection_semantics=fit.get("fit_selection_semantics"),
        interval_semantics=fit.get("interval_semantics"),
        shadow_model_selection=fit.get("shadow_model_selection"),
        at_limit=fit.get("at_limit"),
        energy_window=fit.get("energy_window"), energy_bands=fit.get("energy_bands"),
        evidence_identity=fit.get("evidence_identity"),
        identity_ba_disagreement=fit.get("identity_ba_disagreement"),
        descriptor=(descriptor.to_dict() if descriptor is not None else None),
        fit_kind=fit.get("fit_kind", "unconstrained"),
        branch_id=branch_id, assignment_entropy=assignment_entropy,
        resolvable=resolvable, branch_weight=branch_weight,
        secondary_features=secondary_features,
        branch_posterior=branch_posterior, candidate_id=candidate_id,
        candidate_state=candidate_state,
        bayes_interval=fit.get("bayes_interval"), bayes_ok=fit.get("bayes_ok"),
        bayes_ess=fit.get("bayes_ess"), bayes_fail_reason=fit.get("bayes_fail_reason"),
        log_evidence=fit.get("log_evidence"), log_evidence_err=fit.get("log_evidence_err"),
        bayes_freq_ratio=fit.get("bayes_freq_ratio"), hesse_freq_ratio=fit.get("hesse_freq_ratio"),
        bayes_prior_driven=fit.get("bayes_prior_driven", False),
        bayes_fork_suspected=fit.get("bayes_fork_suspected", False),
        bayes_unconstrained=fit.get("bayes_unconstrained", False),
        target_branch=target_branch, target_label=target_label,
        effective_exposure=exposure_info.get("value"), exposure_provenance=exposure_info,
        acquisition_id=f"pooled:{chash}")
    outcome.probability_semantics = scan_delivery._refresh_probability_semantics(
        outcome.__dict__,
        predictive_payload=predictive_payload,
    )
    entry = ScanEntry(status="fitted", **base)
    entry.detectors[det] = outcome.to_dict()
    entry.provenance = {"pooled": True, "n_members": int(ms.n_members),
                        "members": [list(m) for m in ms.members],
                        "fit_seconds": round(time.perf_counter() - _t_fit0, 4),
                        "two_stage_seconds": fit.get("two_stage_seconds"),
                        "errors_attach_seconds": fit.get("errors_attach_seconds"),
                        **scan_delivery._timing_provenance(fit)}
    calibration_fingerprint = _json_fingerprint(calres.summary())
    policy_fingerprint = _json_fingerprint(card.policy)
    if predictive_payload is not None:
        predictive_payload.update({
            "master_content_hash": chash,
            "calibration_fingerprint": calibration_fingerprint,
            "policy_fingerprint": policy_fingerprint,
            "prior_fingerprint": _json_fingerprint(
                None if prior is None else {"omega0": prior.omega0,
                                            "omega0_sigma": prior.omega0_sigma}),
            "q": q, "T": T, "members": [list(member) for member in ms.members],
        })
        entry.provenance["predictive_payload"] = predictive_payload
        # The authority basis the target projection is later bound to. Carried here because the
        # projection is resolved after the connected tracker, in a scope that has no calibration.
        entry.provenance["target_projection_authority"] = {
            "master_revision": chash,
            "calibration_fingerprint": calibration_fingerprint,
            "policy_fingerprint": policy_fingerprint,
            "action_role": str(fit_role),
        }
    if predictive_unavailable is not None:
        entry.provenance["predictive_unavailable"] = predictive_unavailable
    if fit.get("structure_skipped"):
        entry.provenance["structure_skipped"] = True
    return _stamp_master_analysis(
        entry, errors_tier=errors_tier, fit_fingerprint=fit_fingerprint,
        spectrum_digest=spectrum_digest)


def _build_predictive_payload(fit, ctx, master_id: str) -> tuple[dict | None, dict | None]:
    """Build ``isar.master_predictive/2`` from a finished, triaged fit — isolated from the science.

    Returns ``(payload, unavailable)``: exactly one is not None. ADR-019 producer isolation — the
    predictive side is a *steering* artifact, so its failure degrades steering to the legacy path
    and must never turn a perfectly good scientific fit into a ``non_result``. The reason is
    structured and logged; nothing is swallowed.
    """
    inputs = fit.pop("predictive_inputs", None)
    if inputs is None:
        return None, None
    try:
        payload = predictive_artifact.build_master_predictive(
            inputs["best"], inputs["scores"], inputs["fits"], inputs["by_hyp"], ctx,
            seed=inputs["seed"], n_members=inputs["n_members"],
            fit_selection_semantics=inputs.get("fit_selection_semantics"))
    except Exception as exc:  # noqa: BLE001 — the fit survives any predictive-construction failure
        log.warning("%s: predictive artifact construction failed (%s): %s",
                    master_id, type(exc).__name__, exc)
        return None, {
            "reason_code": predictive_artifact.PREDICTIVE_UNAVAILABLE_CONSTRUCTION_FAILED,
            "error_type": type(exc).__name__, "message": str(exc)}
    if payload is None:
        return None, {
            "reason_code": predictive_artifact.PREDICTIVE_UNAVAILABLE_NO_RETAINED_MODELS,
            "message": "no fitted candidate produced a usable manifest and draw basis"}
    return payload, None

"""``plan_next_action`` (PR 4.2b C7 / B6): the integration slice — one discovery decision, start
to finish, over a snapshotted scene and a frozen program.

**Atomic snapshot.** The scene, agenda, and program store pointers are each read exactly ONCE, up
front: the scene payload/digest becomes the input to both derivation passes, and the agenda and
program pointer digests each become their own store's ``expected_parent`` at publication — the
optimistic-concurrency token a losing writer's ``ConcurrentRevisionWrite`` refers back to. No
scene stored yet is a controller fault with nothing persisted except the decision record itself
(there is no basis to build anything else against). Immediately before the agenda write, the
scene pointer is re-read a second time (PR #33 A-P0-4) and a mismatch against the snapshot raises
``StaleSceneAtPublication`` with nothing published — narrowing, not closing, the gap between
reading the scene and publishing something built from it (see that exception's docstring in
``isar.scene.store`` for what the agenda store's own lock still covers beneath this check).

**Two agenda passes, one pure function.** Pass-1 derives the agenda with no exhaustion evidence
(the basis every :class:`~isar.schema.generators.QuestionActionAssociation` and
:class:`~isar.scene.questions.QuestionExhaustion` in this decision binds against). Pass-2
re-derives — the SAME ``isar.scene.agenda.derive_agenda`` call, this time with whatever proofs
this decision produced and whatever ``blocker_evidence`` the caller supplied (PR #33 A-P0-2) —
and its own :func:`~isar.scene.agenda.evaluate_agenda` VERDICT decides completion (PR #33
A-P0-3: never inferred from raw open/closed statuses — a question can leave "open" by closing
unresolvable rather than answering yes, and that is not completion). Both passes are pure;
nothing is written to the agenda store until persistence, and that write reuses
``build_and_store_agenda`` (the "build path" the C7 contract names) so pass-2 and the persisted
revision can never diverge — the double-run byte-identity test is what catches it if they ever do.

**Faults never raise out of the decision, except one.** A dependency-blocked/spec-mismatch
refusal from C5 or a dedup conflict from C4/C6's ``ActionInventory.dedupe_discovery_evaluations``
is caught and becomes ``outcome=controller_fault`` — recorded, with siblings still persisted in
the inventory artifact for diagnosis. ``ConcurrentRevisionWrite`` is the one exception that is
NOT caught: it means another writer moved the agenda pointer between this call's snapshot and its
flip, and the caller (4.3) decides what a losing writer does next, not this function.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from isar.drive.acquisition.inventory import ActionInventory, GeneratorReport, GeneratorStatus
from isar.schema.action_identity import canonical_json
from isar.schema.campaign_program import CampaignProgram, ProgramError, program_fingerprint
from isar.schema.generators import DISCOVERY_GENERATOR_REGISTRY, DISCOVERY_GENERATOR_REGISTRY_VERSION, registry_manifest
from isar.schema.placement import PLACEMENT_RULES, PLACEMENT_RULES_SCHEMA
from isar.scene.agenda import (
    build_and_store_agenda, derive_agenda, evaluate_agenda, AGENDA_STORE)
from isar.scene.model import canonical_bytes, fingerprint
from isar.scene.store import RevisionStore, SCENE_STORE, StaleSceneAtPublication

from isar.drive.steering.exhaustion import CONTROLLER_FAULT as _PRODUCER_FAULT
from isar.drive.steering.exhaustion import produce_exhaustion
from isar.drive.steering.generators import DiscoveryContext
from isar.drive.steering.readiness import RunResult, classify_readiness, run_ready_questions
from isar.drive.steering.scheduler import SCHEDULER_POLICY, select

__all__ = [
    "SELECTED", "AGENDA_COMPLETE", "CONTROLLER_FAULT", "NO_ACTION_WITH_HONEST_EXHAUSTION",
    "NO_ELIGIBLE_ACTION", "PROGRAM_STORE", "PlanResult", "plan_next_action",
]

# Outcome vocabulary (plan resolution — the five-member enum).
SELECTED = "selected"
AGENDA_COMPLETE = "agenda_complete"
CONTROLLER_FAULT = "controller_fault"
NO_ACTION_WITH_HONEST_EXHAUSTION = "no_action_with_honest_exhaustion"
NO_ELIGIBLE_ACTION = "no_eligible_action"

DECISION_SCHEMA = "isar.discovery-decision/1"

# The workspace's first persisted program document (resolution (g)): planner-written, idempotent
# on the same program. A second binding of the SAME content-addressed store core the scene/agenda
# stores already use — never a third implementation.
PROGRAM_STORE = RevisionStore("program", "isar.program-pointer/1", "program")


# ----------------------------------------------------------------------------
# Small local plumbing (kept local rather than imported: the atomic-write idiom is
# reimplemented per-module throughout isar/scene precisely because the boundary bans importing
# isar.session, and this mirrors that same posture for isar.drive — see isar/scene/store.py's
# own docstring on why _write_atomic exists in three places instead of one).
# ----------------------------------------------------------------------------
def _write_atomic(target: Path, payload: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


@dataclass(frozen=True, slots=True)
class _ProgramRevision:
    """Duck-typed for ``RevisionStore`` (``.canonical`` bytes, their ``.digest``) — the SAME
    content-addressing rule ``AgendaRevision``/``SceneRevision`` use, so ``program_fingerprint``
    is not a second definition of the program's address, only the schema-side name for it
    (pinned equal by test in ``tests/schema/test_campaign_program.py``)."""

    canonical: bytes
    digest: str

    @classmethod
    def of(cls, program: CampaignProgram) -> "_ProgramRevision":
        return cls(canonical=canonical_bytes(program.to_dict()), digest=program_fingerprint(program))


def _resolve_discover_step(program: CampaignProgram, step_id: str):
    step = next((s for s in program.steps if s.id == step_id), None)
    if step is None:
        raise ProgramError(f"program has no step {step_id!r}")
    if step.kind != "discover_and_map":
        raise ProgramError(
            f"step {step_id!r} is a {step.kind!r} step; plan_next_action only plans "
            "discover_and_map steps")
    return step


def _raw_placement_manifest(profile: str) -> dict:
    """The RAW per-profile placement registry (``isar.schema.placement.PLACEMENT_RULES``), with
    NO class-list validation.

    ``isar.schema.placement`` is deliberately class-list-blind (it doesn't know which classes a
    catalogue profile declares); the one class-list-AWARE wrapper is
    ``isar.analyze.catalogue.placement_rules``, which lives on the analyze side ``isar.drive`` may
    never import (``tests/drive/test_boundary.py``). This manifest is therefore the raw registry
    for the profile, not a catalogue-validated set — ``class_list_source`` says so explicitly on
    the manifest itself, so a reader can never mistake this for the validated fingerprint
    ``isar.analyze.catalogue.placement_rules_fingerprint`` computes.
    """
    rules = PLACEMENT_RULES.get(profile, {})
    return {
        "schema": PLACEMENT_RULES_SCHEMA, "profile": profile,
        "class_list_source": "raw_registry",
        "rules": {name: {"anchor_kind": rule.anchor_kind,
                         "omega_band_hint_meV": (list(rule.omega_band_hint_meV)
                                                 if rule.omega_band_hint_meV is not None
                                                 else None),
                         "order_weight": rule.order_weight, "note": rule.note}
                 for name, rule in sorted(rules.items())},
    }


def _placement_fingerprint(manifest: dict) -> str:
    import hashlib
    return PLACEMENT_RULES_SCHEMA + ":" + hashlib.sha256(
        canonical_json(manifest).encode("utf-8")).hexdigest()[:12]


def _decision_basis_common(program_fp: str, step_id: str, step_payload: Mapping,
                           registry_fp: str) -> dict:
    """Every ``decision_basis`` field that does NOT depend on a stored scene existing — computed
    even on the scene-missing fault path, so that record is as diagnostic as any other."""
    placement_manifest = _raw_placement_manifest(step_payload["catalogue_profile"])
    return {
        "program_fingerprint": program_fp,
        "step_id": step_id,
        "resolved_policy_fingerprint": fingerprint(step_payload["resolved_policy"]),
        "generator_registry": {"fingerprint": registry_fp,
                              "manifest": registry_manifest(DISCOVERY_GENERATOR_REGISTRY)},
        "scheduler_policy": SCHEDULER_POLICY,
        "placement": {"fingerprint": _placement_fingerprint(placement_manifest),
                     "manifest": placement_manifest},
        "staleness_policy": {"mode": "synchronous", "lag": 0},
    }


def _build_inventory(run_result: RunResult, survivors: list, deduped_by_generator: dict
                     ) -> ActionInventory:
    """One :class:`GeneratorReport` per registered discovery generator, aggregated across every
    question this run touched — the accounting a discovery inventory rests its own completeness
    claim on, same invariants :class:`ActionInventory.structural_problems` checks."""
    outcomes_by_generator: dict[str, list] = {}
    for record in run_result.records:
        for outcome in record.generator_outcomes:
            outcomes_by_generator.setdefault(outcome.generator, []).append(outcome)

    reports = []
    for spec in DISCOVERY_GENERATOR_REGISTRY:
        generator_id = spec.generator_id
        outcomes = outcomes_by_generator.get(generator_id, [])
        # PR #33 A-P1-c: `generated` is ALWAYS the generator's actual surviving-evaluation count,
        # computed BEFORE the failed/inapplicable branches -- a FAILED status still forces
        # incompleteness (GeneratorStatus.FAILED is never completed_cleanly), but a generator
        # that failed on one question and succeeded on a sibling still has real survivors, and
        # reporting generated=0 over them made the inventory contradict its own evaluations
        # (structural_problems' `report.generated == evaluated` invariant).
        generated = sum(1 for e in survivors if e.generator == generator_id)
        deduped = deduped_by_generator.get(generator_id, 0)
        failed = [o for o in outcomes if o.kind == "failed"]
        if failed:
            reports.append(GeneratorReport(generator=generator_id, status=GeneratorStatus.FAILED,
                                           generated=generated, deduped=deduped,
                                           error_code=failed[0].error_code))
            continue
        if not outcomes:
            # Never invoked this derivation (its family had no ready question this round) --
            # NOT a scientific decline, just nothing to report against.
            reports.append(GeneratorReport(
                generator=generator_id, status=GeneratorStatus.INAPPLICABLE, generated=0,
                reason_code="not_required_this_derivation"))
            continue
        reason = None
        if generated == 0:
            reason = next((o.clean_zero_reason or o.inapplicable_reason for o in outcomes
                          if o.clean_zero_reason or o.inapplicable_reason), None)
        reports.append(GeneratorReport(generator=generator_id, status=GeneratorStatus.COMPLETED,
                                       generated=generated, deduped=deduped, reason_code=reason))

    return ActionInventory(
        evaluations=list(survivors), reports=reports,
        inventory_version="isar.action-inventory/2",
        generator_registry_fingerprint=DISCOVERY_GENERATOR_REGISTRY_VERSION,
        generator_registry_manifest=tuple(tuple(pair) for pair in
                                          registry_manifest(DISCOVERY_GENERATOR_REGISTRY)))


def _write_inventory(report_dir: Path, inventory: ActionInventory) -> dict:
    """Content-addressed, atomic, skip-rewrite-if-identical — under ``report/inventory/discovery/``
    (never the top-level ``report/inventory/*.json`` the legacy replay tooling globs
    non-recursively, so a discovery artifact is invisible to it by construction, not by a kind
    filter that could be forgotten somewhere).

    The stored bytes ARE the canonical bytes the digest addresses (unlike the legacy
    ``report/inventory/<hash>.json`` writer, which pretty-prints with ``indent=2`` and then
    separately re-hashes the canonical form to verify — two encodings of one file that can never
    agree on raw bytes). Storing the canonical bytes directly keeps "does this file match its own
    address" a plain byte comparison, the same idiom ``isar.scene.store`` already uses.
    """
    digest = inventory.content_hash()
    target = report_dir / "inventory" / "discovery" / f"{digest}.json"
    canonical = canonical_json(inventory.to_full_dict()).encode("utf-8")
    if not (target.exists() and target.read_bytes() == canonical):
        _write_atomic(target, canonical)
    return {"kind": "discovery_action_inventory", "sha256": digest,
            "path": f"report/inventory/discovery/{digest}.json"}


def _append_decision(report_dir: Path, record: dict) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    with open(report_dir / "decisions.jsonl", "a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _current_scene_pointer_digest(report_dir: Path) -> str | None:
    """Hook point for the publication-time scene re-check (PR #33 A-P0-4) -- a thin wrapper so
    tests can simulate a scene write racing between snapshot and publication without fighting
    ``RevisionStore``'s frozen dataclass (it refuses attribute patching)."""
    return SCENE_STORE.pointer_digest(report_dir)


def _current_program_pointer_digest(report_dir: Path) -> str | None:
    """Hook point for the program store's planning-start snapshot (PR #33 A-P0-4 program CAS),
    same reasoning as :func:`_current_scene_pointer_digest`."""
    return PROGRAM_STORE.pointer_digest(report_dir)


@dataclass(frozen=True)
class PlanResult:
    """What one ``plan_next_action`` call decided, and enough to find everything it wrote."""

    outcome: str
    step_id: str
    scene_digest: str | None
    basis_digest: str | None
    agenda_digest: str | None
    program_fingerprint: str | None
    selection_fingerprint: str | None
    inventory_ref: Mapping | None
    run_records: tuple
    reason: str | None = None


def plan_next_action(report_dir, program: CampaignProgram, step_id: str, *,
                     locality_coordinate: float | None = None,
                     blocker_evidence: tuple = ()) -> PlanResult:
    report_dir = Path(report_dir)
    step = _resolve_discover_step(program, step_id)
    step_payload = step.payload.to_dict()
    program_fp = program_fingerprint(program)
    registry_fp = DISCOVERY_GENERATOR_REGISTRY_VERSION
    basis_common = _decision_basis_common(program_fp, step_id, step_payload, registry_fp)

    # ---- atomic snapshot -----------------------------------------------------------------
    scene_current = SCENE_STORE.read_current(report_dir)
    if scene_current is None:
        record = {
            "schema": DECISION_SCHEMA, "outcome": CONTROLLER_FAULT,
            "decision_basis": {**basis_common, "scene_digest": None, "agenda_digest": None,
                              "basis_digest": None},
            "selection": None,
            "scan_design_provenance": {"inventory_fingerprint": None, "context_fingerprint": None,
                                      "selected_candidate_id": None, "status": CONTROLLER_FAULT,
                                      "recovery_state": None},
            "scan_design_events": [], "inventory_ref": None,
        }
        _append_decision(report_dir, record)
        return PlanResult(outcome=CONTROLLER_FAULT, step_id=step_id, scene_digest=None,
                          basis_digest=None, agenda_digest=None, program_fingerprint=program_fp,
                          selection_fingerprint=None, inventory_ref=None, run_records=(),
                          reason="no scene stored for this workspace")
    scene_pointer, scene_payload = scene_current
    scene_digest = fingerprint(scene_payload)
    agenda_snapshot = AGENDA_STORE.pointer_digest(report_dir)
    program_snapshot = _current_program_pointer_digest(report_dir)

    # ---- pass 1: pure, no exhaustion evidence (blocker_evidence threaded through anyway --
    # PR #33 A-P0-2/W4: both passes receive IDENTICAL blocker_evidence, so pass-1's own
    # assessments -- what the exhaustion producer and every generator construction reads --
    # never disagree with pass-2's about which blockers are attached) ----------------------
    pass1 = derive_agenda(scene_payload, step_payload, step_id=step_id,
                          program_fingerprint=program_fp, blocker_evidence=blocker_evidence,
                          generator_registry_fingerprint=registry_fp)
    basis_digest = pass1.payload["basis_digest"]
    context = DiscoveryContext(scene_payload=scene_payload, agenda_payload=pass1.payload,
                               step_payload=step_payload, basis_agenda_digest=basis_digest,
                               scene_digest=scene_digest)

    fault_reason = None
    run_result = RunResult(records=(), evaluations=())
    survivors: list = []
    deduped_by_generator: dict = {}
    try:
        readiness = classify_readiness(pass1.payload)
        run_result = run_ready_questions(context, readiness)
        survivors, deduped_by_generator = ActionInventory.dedupe_discovery_evaluations(
            list(run_result.evaluations))
    except ValueError as error:
        fault_reason = str(error)
        survivors = list(run_result.evaluations)      # best-effort, undeduped
        deduped_by_generator = {}

    producer_result = None
    per_question_fault = None
    proofs: tuple = ()
    if fault_reason is None:
        producer_result = produce_exhaustion(
            context, run_result, generator_registry_fingerprint=registry_fp,
            blocker_evidence=blocker_evidence)
        proofs = producer_result.proofs
        faulted = [o for o in producer_result.outcomes if o.outcome == _PRODUCER_FAULT]
        if faulted:
            per_question_fault = "; ".join(f"{o.question_id}: {o.detail}" for o in faulted)

    # ---- pass 2: re-derive with whatever proofs this decision produced ------------------
    pass2 = derive_agenda(scene_payload, step_payload, step_id=step_id,
                          program_fingerprint=program_fp, exhaustion_evidence=proofs,
                          blocker_evidence=blocker_evidence,
                          generator_registry_fingerprint=registry_fp)
    pass2_open = {a["question_id"] for a in pass2.payload["assessments"] if a["status"] == "open"}
    # PR #33 A-P0-3/W5: completion is THE AGENDA VERDICT'S call, never inferred from raw
    # statuses -- a question can leave "open" by closing UNRESOLVABLE rather than answering
    # YES, and the four-reasons law says that is not completion (evaluate_agenda's own
    # verify_yes / generator_defect-reasons checks already know the difference; re-deriving
    # it here from pass2_open alone was exactly the bug).
    verdict = evaluate_agenda(pass2.payload)

    reason = fault_reason or per_question_fault
    if reason is not None:
        outcome = CONTROLLER_FAULT
    elif verdict.state == "complete":
        outcome = AGENDA_COMPLETE
    elif survivors:
        outcome = SELECTED
    elif not pass2_open:
        # Nothing left OPEN, no eligible action, yet the verdict is not complete -- the only
        # way both hold is that every remaining question closed via an APPLIED exhaustion
        # proof (status -> unresolvable), never merely an emitted one: a proof apply_
        # exhaustion REJECTED (generator_defect: exhausted, but no durable blocker) leaves its
        # question sitting in pass2_open, so it can never reach this branch. Reachable in
        # 4.2b only with synthetic/authenticated blocker_evidence (see W4) -- no live
        # evaluator emits a durable blocker yet, so this is 4.3+ territory otherwise.
        outcome = NO_ACTION_WITH_HONEST_EXHAUSTION
    else:
        # Something is still open: either ordinary unresolved dependencies, or an exhaustion
        # proof that WAS emitted but that apply_exhaustion honestly could not turn into a
        # closure (clean-zero with no durable blocker to attach it to) -- a real, expected,
        # non-faulty state in a world with no durable-blocker producer yet, not a lie to
        # penalize as controller_fault.
        outcome = NO_ELIGIBLE_ACTION

    # ---- persistence, in order: inventory -> agenda -> program -> decision --------------
    inventory = _build_inventory(run_result, survivors, deduped_by_generator)
    inventory_ref = _write_inventory(report_dir, inventory)

    # ---- publication guard: scene re-check (PR #33 A-P0-4) -------------------------------
    # This narrows the window, it does not close it: the check runs OUTSIDE the agenda store's
    # own lock, against a DIFFERENT store than the one about to be written. See
    # StaleSceneAtPublication's docstring for exactly what the agenda store's expected_parent
    # CAS + interprocess lock beneath this still covers.
    republished_scene_digest = _current_scene_pointer_digest(report_dir)
    if republished_scene_digest != scene_pointer["digest"]:
        raise StaleSceneAtPublication(
            f"scene pointer moved to {republished_scene_digest!r} since this decision "
            f"snapshotted it at {scene_pointer['digest']!r}; refusing to publish an agenda "
            "built against the stale scene. No agenda, program, or decision record has been "
            "published; the content-addressed inventory blob may already exist and is harmless "
            "(idempotent -- a retry over the fresh scene writes its own).")

    build_and_store_agenda(report_dir, scene_payload, step_payload, step_id=step_id,
                           program_fingerprint=program_fp, exhaustion_evidence=proofs,
                           blocker_evidence=blocker_evidence,
                           generator_registry_fingerprint=registry_fp,
                           expected_parent=agenda_snapshot, source="planner")

    PROGRAM_STORE.write_revision(report_dir, _ProgramRevision.of(program), source="planner",
                                 expected_parent=program_snapshot)

    selection_block = None
    selection_fingerprint = None
    if outcome == SELECTED:
        winner, rank_vector, supplying, _table = select(survivors, step_payload,
                                                        locality_coordinate)
        selection_fingerprint = winner.intent.scientific_action_fingerprint
        selection_block = {
            "fingerprint": selection_fingerprint,
            "rank_vector": list(rank_vector),
            "reason": f"lexicographic minimum over {len(survivors)} eligible action(s)",
            "associations": [a.to_dict() for a in winner.associations],
            "supplying_association": supplying.to_dict(),
        }

    context_fingerprint = fingerprint(
        {"scene_digest": scene_digest, "basis_digest": basis_digest, "step_id": step_id})
    record = {
        "schema": DECISION_SCHEMA,
        "outcome": outcome,
        "decision_basis": {**basis_common, "scene_digest": scene_digest,
                          "agenda_digest": pass2.digest, "basis_digest": basis_digest},
        "selection": selection_block,
        "scan_design_provenance": {
            "inventory_fingerprint": inventory.content_hash(),
            "context_fingerprint": context_fingerprint,
            "selected_candidate_id": selection_fingerprint,
            "status": outcome, "recovery_state": None},
        "scan_design_events": [],
        "inventory_ref": inventory_ref,
    }
    if reason is not None:
        record["reason"] = reason
    _append_decision(report_dir, record)

    return PlanResult(outcome=outcome, step_id=step_id, scene_digest=scene_digest,
                      basis_digest=basis_digest, agenda_digest=pass2.digest,
                      program_fingerprint=program_fp, selection_fingerprint=selection_fingerprint,
                      inventory_ref=inventory_ref, run_records=run_result.records, reason=reason)

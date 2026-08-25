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
import math
import os
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping

from isar.drive.acquisition.inventory import (
    ActionEligibility, ActionInventory, GeneratorReport, GeneratorStatus, RejectionClass)
from isar.schema.action_identity import canonical_json, physical_action_key
from isar.schema.campaign_program import CampaignProgram, ProgramError, program_fingerprint
from isar.schema.consultation import strip_consultation_overrides
from isar.schema.generators import (
    DISCOVERY_GENERATOR_REGISTRY, DISCOVERY_GENERATOR_REGISTRY_VERSION, aggregate_rank,
    registry_manifest)
from isar.schema.placement import PLACEMENT_RULES, PLACEMENT_RULES_SCHEMA
from isar.scene import eig as scene_eig
from isar.scene.agenda import (
    build_and_store_agenda, derive_agenda, evaluate_agenda, AGENDA_STORE)
from isar.scene.model import canonical_bytes, fingerprint
from isar.scene.phenomena import Design
from isar.scene.store import RevisionStore, SCENE_STORE, StaleSceneAtPublication

from isar.drive.steering.cold_start import DeclaredPartition
from isar.drive.steering.exhaustion import CONTROLLER_FAULT as _PRODUCER_FAULT
from isar.drive.steering.exhaustion import produce_exhaustion
from isar.drive.steering.generators import DiscoveryContext
from isar.drive.steering.readiness import RunResult, classify_readiness, run_ready_questions
from isar.drive.steering.scheduler import LOCALITY_VERSION, SCHEDULER_POLICY, rank_all, select

__all__ = [
    "SELECTED", "AGENDA_COMPLETE", "CONTROLLER_FAULT", "NO_ACTION_WITH_HONEST_EXHAUSTION",
    "NO_ELIGIBLE_ACTION", "PROGRAM_STORE", "PlanResult", "plan_next_action",
    "program_revision",
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


def program_revision(program: CampaignProgram) -> _ProgramRevision:
    """The store-shaped revision for ``program`` — the PUBLIC name for what ``report/program/``
    writes, so a second writer (the consultation recompile) never reaches for the private class."""
    return _ProgramRevision.of(program)


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
                           registry_fp: str, locality_coordinate: float | None,
                           declared_partition=None,
                           retired_physical_actions: frozenset[str] = frozenset(),
                           declined_fingerprints: frozenset[str] = frozenset(),
                           hypotheses_fingerprint: str | None = None,
                           scoring_hypotheses_fingerprint: str | None = None) -> dict:
    """Every ``decision_basis`` field that does NOT depend on a stored scene existing — computed
    even on the scene-missing fault path, so that record is as diagnostic as any other.

    ``locality`` (PR #33 A-P1-e) makes the caller's ``locality_coordinate`` visible in the
    record: it already perturbs which action ``select()`` picks (``scheduler.py``'s rank
    vector), so leaving it out of the decision basis let two decisions differing ONLY in
    locality look identical everywhere a reader would check provenance.

    ``declared_partition`` (PR 4.3 D4) embeds the cold-start declaration's FULL key components —
    replay reproduces the declared partition from THIS record, never from the mutable card, so a
    card edited between runs is visible in the ledger by construction. The key is present only
    when a declaration was actually supplied: a decision that used none says so by carrying
    nothing, which also keeps every non-cold-start record byte-identical to its 4.2b shape.

    ``retired_physical_actions`` (PR 4.3 C6) is the same shape of finding as ``locality`` above:
    the caller's broken-scan offramp set NARROWS what ``select()`` may pick, so two decisions
    differing only in it were made on different grounds and must not read identically. Embedded on
    the SAME conditional rule as ``declared_partition`` — present only when non-empty — so every
    decision taken with nothing retired stays byte-identical to its pre-C6 shape.

    ``declined_fingerprints`` (PR 4.3 C7) is the third member of that family and rides on the SAME
    omitted-when-empty rule. It is a STRONGER narrowing than the two above — a declined scientific
    action is re-marked REJECTED/STRUCTURAL in the run record itself, so it leaves the selection
    input, the producer's eligible set, and the persisted inventory's eligibility together — which
    is exactly why it must be visible here: a decision that structurally rejected an action the
    previous decision selected was made on different grounds, and the two must not read identically.
    Distinct from ``retired_physical_actions`` on purpose: that channel narrows selection ONLY and
    keeps the evaluations eligible (a broken detector is not a statement about the physics), and
    both channels stay separately visible in the basis rather than merged into one suppression set.
    """
    placement_manifest = _raw_placement_manifest(step_payload["catalogue_profile"])
    declared = ({"declared_partition": declared_partition.to_dict()}
                if declared_partition is not None else {})
    retired = ({"retired_physical_actions": sorted(retired_physical_actions)}
               if retired_physical_actions else {})
    declined = ({"declined_scientific_actions": sorted(declined_fingerprints)}
                if declined_fingerprints else {})
    # Omitted-when-absent on the same rule as the three above, so a decision taken with no declared
    # hypotheses stays byte-identical to its 4.2b shape. Present, it is the DECLARED PHYSICS this
    # decision was taken under — readable without rehashing the intent.
    hypotheses = ({"hypotheses_fingerprint": hypotheses_fingerprint}
                  if hypotheses_fingerprint else {})
    # SCENE-5 5.2: the CONTENT fingerprint of the SAME declared ``[hypotheses.*]`` mapping,
    # distinct from ``hypotheses_fingerprint`` above (a pre-existing, name-only fingerprint —
    # flagged to the owner, not repaired here). Omitted-when-empty on the same rule.
    scoring_hypotheses = ({"scoring_hypotheses_fingerprint": scoring_hypotheses_fingerprint}
                          if scoring_hypotheses_fingerprint else {})
    return {
        **hypotheses,
        **scoring_hypotheses,
        **declared,
        **retired,
        **declined,
        "program_fingerprint": program_fp,
        "step_id": step_id,
        "resolved_policy_fingerprint": fingerprint(step_payload["resolved_policy"]),
        "generator_registry": {"fingerprint": registry_fp,
                              "manifest": registry_manifest(DISCOVERY_GENERATOR_REGISTRY)},
        "scheduler_policy": SCHEDULER_POLICY,
        "locality": {"fn": LOCALITY_VERSION, "coordinate": locality_coordinate},
        "placement": {"fingerprint": _placement_fingerprint(placement_manifest),
                     "manifest": placement_manifest},
        "staleness_policy": {"mode": "synchronous", "lag": 0},
    }


#: The rejection reason stamped on an evaluation the instrument declined as infeasible. One string,
#: so the producer-side classification and anything reading the persisted inventory cannot drift.
DECLINED_REJECTION_REASON = "instrument_declined_as_infeasible"


def _mark_declined(run_result: RunResult, declined_fingerprints: frozenset[str]) -> RunResult:
    """Re-mark every evaluation whose scientific action the instrument declined (C7/D5).

    Rejected with ``RejectionClass.STRUCTURAL`` — the existing vocabulary, not a new one: the
    machine cannot close the scattering triangle for this action on this evidence basis, which is
    precisely what STRUCTURAL names. Applied to the whole flat evaluation list rather than to
    post-dedup survivors, because dedup keeps the FIRST-seen evaluation for a fingerprint and a
    peer generator's copy of the same scientific action must not survive as eligible.
    """
    if not declined_fingerprints:
        return run_result
    return replace(run_result, evaluations=tuple(
        evaluation if evaluation.intent.scientific_action_fingerprint not in declined_fingerprints
        else replace(evaluation, eligibility=ActionEligibility.REJECTED,
                     rejection_class=RejectionClass.STRUCTURAL,
                     rejection_reason=DECLINED_REJECTION_REASON)
        for evaluation in run_result.evaluations))


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

    LOSING-WRITER ORPHAN POLICY (PR 4.3 C6, ratified): a blob written by a decision that then failed
    to publish — a losing CAS, a ``StaleSceneAtPublication`` retry — is left where it is, and there
    is no cleanup daemon. It is content-addressed litter: nothing references it (a decision cites its
    inventory by address, and no reader enumerates this directory), and the retry writes its own
    blob, so an orphan can neither be mistaken for a decision's inventory nor perturb the next one.
    The idempotence is the policy — ``tests/drive/test_planner.py`` pins that an orphan leaves the
    next decision byte-identical.
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


def _pointer_names_consultation_descendant(report_dir: Path, program: CampaignProgram) -> bool:
    """Does ``report/program/current.json`` name ``program`` PLUS consultation overrides?

    True only when the stored revision differs from ``program`` and strips down to exactly it — the
    one case where publishing ``program`` would be a REGRESSION rather than a no-op. A missing,
    unrelated, or unreadable stored revision all answer False, which leaves the ordinary write (and
    its CAS) exactly as it was.
    """
    try:
        current = PROGRAM_STORE.read_current(report_dir)
    except ValueError:
        return False           # corruption is the store's own loud problem, not a write guard's
    if current is None:
        return False
    try:
        stored = CampaignProgram.from_dict(current[1])
    except (ProgramError, ValueError):
        return False
    authored = program_fingerprint(program)
    return (program_fingerprint(stored) != authored
            and program_fingerprint(strip_consultation_overrides(stored)) == authored)


def _current_program_pointer_digest(report_dir: Path) -> str | None:
    """Hook point for the program store's planning-start snapshot (PR #33 A-P0-4 program CAS),
    same reasoning as :func:`_current_scene_pointer_digest`."""
    return PROGRAM_STORE.pointer_digest(report_dir)


# ----------------------------------------------------------------------------
# Scene-EIG scoring path (SCENE-5 5.2, docs/plans/scene5-eig.md §3 "5.2 Controller integration")
# ----------------------------------------------------------------------------
SCENE_EIG_SCHEDULER_POLICY_ID = "isar.scheduler.scene-eig/1"
SCENE_EIG_BASE_SEED_RULE = "isar.scheduler.scene-eig.base-seed/1"
#: The closed D7 fallback-reason vocabulary this module may itself add to the two
#: ``SceneScoringError`` reasons ``isar.scene.eig`` raises.
SCORED_SET_EMPTY = "scored_set_empty"

_PARTITION_PREFIX = "isar.partition/"


def _scoring_axis(step_payload: Mapping) -> int:
    """Which hkl component varies along the step's declared region — the SAME selection
    ``isar.drive.steering.generators._hkl_at`` (and ``isar.drive.steering.scheduler``'s own
    ``_dominant_axis``) make. Re-derived here rather than imported (both are private, and the
    idiom throughout this package is a small pinned-equal mirror, never reaching into another
    module's underscore names) so a scored design's coordinate is the exact inverse of how the
    generators built the winning intent's ``hkl`` in the first place."""
    region = step_payload["region"]
    start, end = region["q_start"], region["q_end"]
    deltas = [abs(b - a) for a, b in zip(start, end)]
    return deltas.index(max(deltas))


def _partition_of(intent) -> str | None:
    """The partition an intent's ``campaign_slice_fingerprint`` names, or ``None`` when the
    intent carries none (a program-scoped intent — unreachable for a live selectable discovery
    evaluation, per ``isar.drive.steering.generators``'s own module docstring)."""
    csf = intent.campaign_slice_fingerprint
    if csf is None or not csf.startswith(_PARTITION_PREFIX):
        return None
    return csf[len(_PARTITION_PREFIX):]


def _first_partition_temperature(scene_payload: Mapping, partition_id: str | None) -> float | None:
    """The first DECLARED ``temperature_K`` among the partition's footprints, in persisted
    order. ``None`` (Bose-blind, honest) when the partition carries no temperature declaration
    at all — never a fabricated default."""
    if partition_id is None:
        return None
    for footprint in scene_payload.get("footprints") or ():
        if footprint.get("partition_id") == partition_id:
            temperature = footprint.get("temperature_K")
            if temperature is not None:
                return float(temperature)
    return None


def _design_for_evaluation(evaluation, step_payload: Mapping, scene_payload: Mapping) -> Design:
    """One candidate :class:`~isar.scene.phenomena.Design` for one eligible evaluation (SCENE-5
    5.2's declared scoring-grid construction: a coarser grid than the winner's real executed
    request, ADR-018's identity untouched — see the plan's "declared approximation" note).

    Raises on anything that makes THIS evaluation unscoreable (no energy window, a degenerate
    step axis, ...); the caller drops such a candidate from the scored set with a recorded skip
    entry rather than treating it as a scoring-layer fault (D7's per-candidate carve-out).
    """
    intent = evaluation.intent
    axis = _scoring_axis(step_payload)
    coordinate = float(intent.hkl[axis])
    if intent.energy_lo is None or intent.energy_hi is None:
        raise ValueError(
            f"evaluation {intent.scientific_action_fingerprint} has no energy window "
            "(energy_lo/energy_hi); cannot build a scoring design for it")
    energy_lo, energy_hi = float(intent.energy_lo), float(intent.energy_hi)
    step_meV = scene_eig.scoring_grid_step_meV()
    n_points = max(2, math.ceil((energy_hi - energy_lo) / step_meV) + 1)
    ncount = float(step_payload["resolved_policy"]["thresholds"]["discovery_monitor_ncount"])
    partition_id = _partition_of(intent)
    temperature_K = _first_partition_temperature(scene_payload, partition_id)
    return Design(axis_coordinate=coordinate, window=(energy_lo, energy_hi), n_points=n_points,
                 ncount=ncount, temperature_K=temperature_K)


def _resolve_winning_evaluation(evaluations):
    """The one evaluation a winning DESIGN resolves to (SCENE-5 5.2): the lexicographic minimum
    by :func:`~isar.schema.generators.aggregate_rank` over each backing evaluation's FULL
    association set, then the scientific-action fingerprint — deterministic and
    replay-identical regardless of which same-temperature partition/path/epoch salt produced
    each backing copy, and never the duplicate-key refusal ``score()`` itself guards."""
    return min(evaluations, key=lambda e: (aggregate_rank(e.associations)[0],
                                           e.intent.scientific_action_fingerprint))


def _base_seed_for(scene_digest: str, basis_digest: str, step_id: str) -> int:
    """Content-derived, never a clock or a running counter: the first 16 hex chars of a
    fingerprint over exactly the three identifiers this plan is already keyed to."""
    payload = {"scene_digest": scene_digest, "basis_digest": basis_digest, "step_id": step_id}
    return int(fingerprint(payload)[:16], 16)


def _rank_table_entries(table) -> list[dict]:
    """The persisted shape of one ``rank_all``/``select`` table — shared by the fallback path
    (where it IS winner-first) and the scored path's audit table (where it is not)."""
    return [
        {"scientific_action_fingerprint": ranked.evaluation.intent.scientific_action_fingerprint,
         "rank_vector": list(ranked.rank_vector),
         "supplying_association": {"question_id": ranked.supplying_association.question_id,
                                  "generator": ranked.supplying_association.generator}}
        for ranked in table]


def _canonical_view(evaluation):
    """A TRANSIENT copy of ``evaluation`` carrying only its canonical associations, for ranking
    only (SCENE-5 5.2, D7's "construct filtered views, never mutate the persisted ones") —
    ``None`` when the evaluation has no canonical association at all, so a caller filtering to
    canonical survivors excludes it outright rather than ranking it on an empty association set
    (``aggregate_rank`` refuses that itself, but excluding it earlier keeps the pool's meaning
    honest: "no canonical association" is not "canonical association, unranked")."""
    canonical_associations = tuple(a for a in evaluation.associations if a.canonical)
    if not canonical_associations:
        return None
    return replace(evaluation, associations=canonical_associations)


def _lexicographic_selection(pool, step_payload: Mapping, locality_coordinate: float | None, *,
                             reason_suffix: str) -> dict:
    """The UNCHANGED ``select()`` path, packaged into the persisted ``selection`` shape — shared
    by every fallback decision (SCENE-5 5.2, D7) regardless of which pool ranked it."""
    winner, rank_vector, supplying, table = select(pool, step_payload, locality_coordinate)
    return {
        "fingerprint": winner.intent.scientific_action_fingerprint,
        "rank_vector": list(rank_vector),
        "reason": f"lexicographic minimum over {len(pool)} eligible action(s) ({reason_suffix})",
        "associations": [a.to_dict() for a in winner.associations],
        "supplying_association": supplying.to_dict(),
        "rank_table": _rank_table_entries(table),
    }


def _scene_eig_selection(scene_payload: Mapping, agenda_payload: Mapping,
                         hypotheses: Mapping[str, dict] | None, selectable, step_payload: Mapping,
                         locality_coordinate: float | None, scene_digest: str, basis_digest: str,
                         step_id: str) -> tuple[dict, dict | None, dict | None]:
    """``(selection_block, fallback_block, scheduler_policy_override)``. ``fallback_block`` is
    ``None`` on a scored decision, present on a fallback one; ``scheduler_policy_override`` is
    the reverse — present (the ``isar.scheduler.scene-eig/1`` block) only on a scored decision,
    ``None`` on a fallback one (where ``decision_basis.scheduler_policy`` keeps its untouched
    lexicographic default from :data:`SCHEDULER_POLICY`).

    Tries scene-EIG scoring first (per-candidate design refusals are dropped with a recorded
    skip, never a fallback by themselves); a :class:`~isar.scene.eig.SceneScoringError` or an
    empty scored set falls back to the UNCHANGED lexicographic ``select()`` over CANONICAL
    associations only — widening to every selectable evaluation when the canonical pool is
    itself empty (the beam-idle guard, D7), so this function never raises.
    """
    designs_by_key: dict[str, Design] = {}
    evaluations_by_key: dict[str, list] = {}
    score_skips: list[dict] = []
    for evaluation in selectable:
        try:
            design = _design_for_evaluation(evaluation, step_payload, scene_payload)
        except Exception as error:
            score_skips.append(
                {"fingerprint": evaluation.intent.scientific_action_fingerprint,
                 "reason": str(error)})
            continue
        key = scene_eig.design_key(design)
        designs_by_key.setdefault(key, design)
        evaluations_by_key.setdefault(key, []).append(evaluation)

    fallback_reason = None
    table = None
    if not designs_by_key:
        fallback_reason = SCORED_SET_EMPTY
    else:
        base_seed = _base_seed_for(scene_digest, basis_digest, step_id)
        try:
            table = scene_eig.score(scene_payload, agenda_payload, hypotheses,
                                    list(designs_by_key.values()), base_seed)
        except scene_eig.SceneScoringError as error:
            fallback_reason = error.reason
        # Any OTHER exception (a plain ValueError from bad policy/duplicate design keys, or a
        # genuine planner bug) is deliberately NOT caught here — D7's non-scoring-fault
        # propagation. It reaches the caller unchanged.

    if fallback_reason is not None:
        canonical_pool = [view for view in (_canonical_view(e) for e in selectable)
                          if view is not None]
        pool, pool_name = ((canonical_pool, "canonical") if canonical_pool
                           else (list(selectable), "all_selectable"))
        selection_block = _lexicographic_selection(
            pool, step_payload, locality_coordinate, reason_suffix=f"fallback: {fallback_reason}")
        return selection_block, {"reason": fallback_reason, "pool": pool_name}, None

    winning_row = table.rows[0]
    score_table = []
    for row in table.rows:
        resolved = _resolve_winning_evaluation(evaluations_by_key[row.design_key])
        score_table.append({
            "scientific_action_fingerprint": resolved.intent.scientific_action_fingerprint,
            "design_key": row.design_key,
            "total": row.total,
            "terms": [{"question_id": t.question_id, "family": t.family, "priority": t.priority,
                      "weight": t.weight, "mi_nats": t.mi_nats,
                      "contributing_clusters": t.contributing_clusters} for t in row.terms],
        })
    winner = _resolve_winning_evaluation(evaluations_by_key[winning_row.design_key])
    ranked_one = rank_all((winner,), step_payload, locality_coordinate)[0]
    # The audit table: EVERY selectable evaluation's lexicographic rank, computed regardless of
    # who scoring picked. On a SCORED decision row 0 here is NOT necessarily the winner
    # (score_table row 0 is) — it lets a reader see where the EIG winner sat in the old ordering.
    audit_table = rank_all(selectable, step_payload, locality_coordinate)
    selection_block = {
        "fingerprint": winner.intent.scientific_action_fingerprint,
        "rank_vector": list(ranked_one.rank_vector),
        "reason": f"scene-EIG scoring: total {winning_row.total!r} across "
                 f"{len(table.rows)} design(s)",
        "associations": [a.to_dict() for a in winner.associations],
        "supplying_association": ranked_one.supplying_association.to_dict(),
        "rank_table": _rank_table_entries(audit_table),
        "score_table": score_table,
    }
    if score_skips:
        selection_block["score_skips"] = score_skips
    scheduler_policy = {
        "id": SCENE_EIG_SCHEDULER_POLICY_ID,
        "relevance_fingerprint": table.relevance_fingerprint,
        "family_registry_fingerprint": table.family_registry_fingerprint,
        "scoring_grid_step_meV": scene_eig.scoring_grid_step_meV(),
        "base_seed_rule": SCENE_EIG_BASE_SEED_RULE,
        "fallback_scheduler_policy_id": SCHEDULER_POLICY["id"],
    }
    return selection_block, None, scheduler_policy


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


def _scene_fault(report_dir: Path, basis_common: dict, step_id: str, program_fp: str,
                 reason: str) -> PlanResult:
    """Record and return the controller fault for "this decision has no scene to stand on".

    Nothing but the decision record is persisted: with no scene there is no basis to build an
    agenda, an inventory, or a program revision against.
    """
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
                      reason=reason)


def plan_next_action(report_dir, program: CampaignProgram, step_id: str, *,
                     locality_coordinate: float | None = None,
                     blocker_evidence: tuple = (),
                     declared_partition: DeclaredPartition | None = None,
                     hypotheses_fingerprint: str | None = None,
                     hypotheses: Mapping[str, dict] | None = None,
                     scene_pin: str | None = None,
                     retired_physical_actions: frozenset[str] = frozenset(),
                     declined_fingerprints: frozenset[str] = frozenset()) -> PlanResult:
    """``declared_partition`` (PR 4.3 D4) is the caller-minted cold-start declaration
    (:func:`isar.drive.steering.cold_start.declare_partition`). Passing none keeps the 4.2b
    behaviour exactly: an unmeasured workspace derives the program-scoped lane and every
    generator answers ``program_scoped_subject_unfanned``.

    ``scene_pin`` (PR 4.3 D1) is the CONTENT ADDRESS of the scene revision to plan against — the
    digest the caller's own build just returned. Supplied, this call never touches
    ``current.json``: the revision is loaded by content address, and the publication-time pointer
    re-read is SKIPPED (see below). Omitted, the pointer-derived behaviour is byte-for-byte what
    it was — ``read_current`` at the snapshot, the guard in force at publication — which is what
    ``isar report``, the tests, and every non-pinned caller keep getting.

    ``retired_physical_actions`` (PR 4.3 C6) is the caller's STEP-LOCAL broken-scan offramp set,
    keyed by :func:`~isar.schema.action_identity.physical_action_key`: actions whose deliveries kept
    coming back with untrusted counts until the frozen retry budget ran out. The key is the
    SCENE-INVARIANT one on purpose — the scientific fingerprint carries the scene digest, which moves
    on every delivery, so a filter keyed on it would never match the action it just retired.
    It narrows THE SELECTION INPUT ONLY. The evaluations stay eligible in the run record, in the
    exhaustion producer's survivor set, and in the persisted inventory, because a broken detector is
    not a statement about the physics — turning "we could not get counts today" into structural
    exhaustion would close a question the instrument never actually answered. (That is the exact
    opposite of C7's declined-fingerprint path, which IS a durable structural rejection.)

    ``declined_fingerprints`` (PR 4.3 C7/D5) is the caller's set of ``scientific_action_fingerprint``
    values the INSTRUMENT declined as geometrically infeasible on THIS pinned scene. Every matching
    evaluation is re-marked ``REJECTED``/``RejectionClass.STRUCTURAL`` in the run record itself —
    UPSTREAM of both ``dedupe_discovery_evaluations`` calls, so the exhaustion producer (which
    re-derives its own survivors from ``RunResult.evaluations``), the persisted inventory, and the
    selection input all read ONE re-marked set rather than three views that could disagree. That is
    what lets producer rule 6 (rejected-only, all-STRUCTURAL, matching
    :class:`~isar.schema.generators.QuestionBlockerEvidence`) emit the proof that closes the
    question unresolvable instead of rule 2 blocking it as ``not_exhausted``.

    ``hypotheses`` (SCENE-5 5.2) is the caller-supplied declared ``[hypotheses.*]`` mapping
    (name -> resolved declaration dict) — the SAME source ``hypotheses_fingerprint`` is derived
    from, threaded once by the caller (``program_runner.py``) rather than re-read here. It feeds
    ``isar.scene.eig.score`` directly; a content fingerprint of it rides in ``decision_basis`` and
    folds into ``context_fingerprint`` (below), distinct from the pre-existing name-only
    ``hypotheses_fingerprint``.
    """
    report_dir = Path(report_dir)
    step = _resolve_discover_step(program, step_id)
    step_payload = step.payload.to_dict()
    program_fp = program_fingerprint(program)
    registry_fp = DISCOVERY_GENERATOR_REGISTRY_VERSION
    scoring_hypotheses_fingerprint = (fingerprint(dict(hypotheses)) if hypotheses else None)
    basis_common = _decision_basis_common(program_fp, step_id, step_payload, registry_fp,
                                          locality_coordinate, declared_partition,
                                          retired_physical_actions, declined_fingerprints,
                                          hypotheses_fingerprint, scoring_hypotheses_fingerprint)
    # Below the planner the declaration travels as the primitive triple the scene layer and the
    # opening book both take — the DTO itself never crosses into isar.scene (import boundary).
    # The step id rides along because it is the RETIREMENT test (owner ruling 2026-08-18): the
    # declaration is live until an observation carries this same step, then reality owns the
    # fan-out. ``step_id`` is the planner's resolved step, and the DTO was minted against it.
    declared_frame = (None if declared_partition is None
                      else (declared_partition.partition_id, declared_partition.lattice_epoch,
                            declared_partition.step_id,
                            declared_partition.path_fingerprint))

    # ---- atomic snapshot -----------------------------------------------------------------
    if scene_pin is not None:
        # By content address, never the pointer. An unloadable pin is store corruption — the one
        # thing D1 classifies as a genuine machine failure on this path — and it becomes a
        # recorded controller fault rather than a silent fall back onto whatever the pointer
        # currently names, which would be steering from an older scene.
        try:
            scene_payload = SCENE_STORE.read_revision(report_dir, scene_pin)
        except ValueError as error:
            return _scene_fault(report_dir, basis_common, step_id, program_fp,
                                f"pinned scene revision {scene_pin} is unloadable: {error}")
        scene_pointer = None
    else:
        scene_current = SCENE_STORE.read_current(report_dir)
        if scene_current is None:
            return _scene_fault(report_dir, basis_common, step_id, program_fp,
                                "no scene stored for this workspace")
        scene_pointer, scene_payload = scene_current
    # Equal to ``scene_pin`` by construction when pinned: ``read_revision`` authenticates the blob
    # against exactly this recomputation before returning it.
    scene_digest = fingerprint(scene_payload)
    agenda_snapshot = AGENDA_STORE.pointer_digest(report_dir)
    program_snapshot = _current_program_pointer_digest(report_dir)

    # ---- pass 1: pure, no exhaustion evidence (blocker_evidence threaded through anyway --
    # PR #33 A-P0-2/W4: both passes receive IDENTICAL blocker_evidence, so pass-1's own
    # assessments -- what the exhaustion producer and every generator construction reads --
    # never disagree with pass-2's about which blockers are attached) ----------------------
    pass1 = derive_agenda(scene_payload, step_payload, step_id=step_id,
                          program_fingerprint=program_fp, blocker_evidence=blocker_evidence,
                          generator_registry_fingerprint=registry_fp,
                          declared_partition=declared_frame)
    basis_digest = pass1.payload["basis_digest"]
    context = DiscoveryContext(scene_payload=scene_payload, agenda_payload=pass1.payload,
                               step_payload=step_payload, basis_agenda_digest=basis_digest,
                               scene_digest=scene_digest, declared_partition=declared_frame,
                               hypotheses_fingerprint=hypotheses_fingerprint)

    fault_reason = None
    run_result = RunResult(records=(), evaluations=())
    survivors: list = []
    deduped_by_generator: dict = {}
    try:
        readiness = classify_readiness(pass1.payload)
        run_result = run_ready_questions(context, readiness)
        # THE injection point (C7/D5, epoch-6 fix). Applied to the RUN RECORD, before any dedup:
        # ``produce_exhaustion`` dedups ``run_result.evaluations`` itself and would ignore a list
        # the planner re-marked privately, leaving the declined action ELIGIBLE for producer rule 2.
        run_result = _mark_declined(run_result, declined_fingerprints)
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
                          generator_registry_fingerprint=registry_fp,
                          declared_partition=declared_frame)
    pass2_open = {a["question_id"] for a in pass2.payload["assessments"] if a["status"] == "open"}
    # PR #33 A-P0-3/W5: completion is THE AGENDA VERDICT'S call, never inferred from raw
    # statuses -- a question can leave "open" by closing UNRESOLVABLE rather than answering
    # YES, and the four-reasons law says that is not completion (evaluate_agenda's own
    # verify_yes / generator_defect-reasons checks already know the difference; re-deriving
    # it here from pass2_open alone was exactly the bug).
    verdict = evaluate_agenda(pass2.payload)

    # Selection input, narrowed twice. (a) ELIGIBILITY (C7/D5): only ``ActionEligibility.ELIGIBLE``
    # survivors may be selected. Latent-safe until now — every live evaluation was constructed
    # eligible (``readiness.py``) — but the declined-fingerprint path mints the first live REJECTED
    # evaluations, and without this filter the planner would re-select the action the instrument
    # just refused. (b) The caller's step-local offramp set (C6). ``survivors`` itself is left
    # alone: it is what the inventory records and what the producer already classified.
    selectable = [e for e in survivors
                  if e.is_eligible
                  and physical_action_key(e.intent) not in retired_physical_actions]

    reason = fault_reason or per_question_fault
    if reason is not None:
        outcome = CONTROLLER_FAULT
    elif verdict.state == "complete":
        outcome = AGENDA_COMPLETE
    elif selectable:
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
    #
    # SCOPED, not removed (PR 4.3 D1). A PINNED caller skips it: the guard exists to catch "the
    # scene I read is no longer the current scene", and a pin says the current scene was never
    # this decision's authority in the first place — the agenda snapshot is keyed to the pinned
    # digest explicitly, and the campaign parent built that revision itself. Making a read-only
    # `isar report --scene` publish able to fault a live campaign is exactly the false machine
    # failure the four-reasons stop law forbids. Every POINTER-DERIVED caller keeps the guard
    # unchanged, because for them the pointer IS the authority they snapshotted.
    republished_scene_digest = (None if scene_pointer is None
                                else _current_scene_pointer_digest(report_dir))
    if scene_pointer is not None and republished_scene_digest != scene_pointer["digest"]:
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
                           declared_partition=declared_frame,
                           expected_parent=agenda_snapshot, source="planner")

    # The planner publishes the program it planned under — UNLESS the pointer already names a
    # CONSULTATION DESCENDANT of it (SCENE-4.3 C8). A recompiled revision is this same program plus
    # the operator's answers; writing the override-free program over it would revert the pointer,
    # and the next run would adopt nothing and silently discard a settled answer. Same
    # strip-and-compare rule the adoption at ``run_program`` start uses, so the two cannot drift.
    if not _pointer_names_consultation_descendant(report_dir, program):
        PROGRAM_STORE.write_revision(report_dir, _ProgramRevision.of(program), source="planner",
                                     expected_parent=program_snapshot)

    selection_block = None
    selection_fingerprint = None
    fallback_block = None
    scheduler_policy_override = None
    if outcome == SELECTED:
        # SCENE-5 5.2: every SELECTED decision is scored by scene-EIG first, over the pass-2
        # agenda payload (the one this decision published) — the lexicographic scheduler
        # survives only as the software-fault fallback (D7), and only over canonical
        # associations. See ``_scene_eig_selection``'s own docstring for the full contract.
        selection_block, fallback_block, scheduler_policy_override = _scene_eig_selection(
            scene_payload, pass2.payload, hypotheses, selectable, step_payload,
            locality_coordinate, scene_digest, basis_digest, step_id)
        selection_fingerprint = selection_block["fingerprint"]

    # PR #33 A-P1-e: locality_coordinate rides INSIDE the fingerprinted input here, not beside
    # it — the reviewer's exact finding was that it "can change the selected action without
    # appearing in the decision basis or context fingerprint". It is now visible in
    # decision_basis (above) AND folds into this digest, so two decisions differing ONLY in
    # locality_coordinate produce different context_fingerprints, never identical ones.
    #
    # The retirement set (C6) folds in on the same terms, and on the same conditional rule it uses
    # in the basis: a narrowed selection input that did not reach this digest would let two
    # decisions taken over different candidate sets carry one context fingerprint.
    #
    # ``declared_partition`` rides the same rule (review finding 2026-08-18). It was left out while
    # the two narrowing sets went in, even though it is a LARGER change to the candidate set — it
    # decides the fan-out itself, not merely which survivors may be picked. It reaches
    # ``basis_digest`` indirectly through the agenda, so the omission was not exploitable, but the
    # rule is "everything that changes what could have been selected", and an exception nobody
    # wrote down is how the next field gets forgotten too.
    context_fingerprint = fingerprint(
        {"scene_digest": scene_digest, "basis_digest": basis_digest, "step_id": step_id,
         "locality": basis_common["locality"],
         **{key: basis_common[key]
            for key in ("declared_partition", "retired_physical_actions",
                        "declined_scientific_actions", "hypotheses_fingerprint",
                        "scoring_hypotheses_fingerprint")
            if key in basis_common}})
    decision_basis = {**basis_common, "scene_digest": scene_digest,
                      "agenda_digest": pass2.digest, "basis_digest": basis_digest}
    if scheduler_policy_override is not None:
        # Scored decision: the declared scene-EIG block replaces the default lexicographic
        # declaration ``_decision_basis_common`` set above. A fallback decision keeps that
        # default untouched (D7) — it never reaches this branch.
        decision_basis["scheduler_policy"] = scheduler_policy_override
    if fallback_block is not None:
        decision_basis["fallback"] = fallback_block
    record = {
        "schema": DECISION_SCHEMA,
        "outcome": outcome,
        "decision_basis": decision_basis,
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

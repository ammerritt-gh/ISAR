"""``plan_next_action`` (PR 4.2b C7 / B6): the integration slice, end to end over a real stored
scene and a real frozen program. One scenario per acceptance item in the plan file's C7 section.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from isar.drive.acquisition.inventory import ActionEligibility, ActionEvaluation, GeneratorStatus
from isar.eig.eligibility import EligibilityInput, _scan_design_identity
from isar.schema.action_identity import MeasurementIntent
from isar.schema.campaign_program import (
    CampaignProgram, CampaignStep, DiscoverAndMapSpec, program_fingerprint)
from isar.schema.generators import (
    DISCOVERY_GENERATOR_REGISTRY_VERSION, QuestionActionAssociation, QuestionBlockerEvidence)
from isar.schema.question_policy import resolve_policy
from isar.scene.agenda import AGENDA_STORE, derive_agenda
from isar.scene.model import canonical_bytes, digest_bytes
from isar.scene.store import ConcurrentRevisionWrite, SCENE_STORE

import isar.drive.steering.planner as planner_module
import isar.drive.steering.readiness as readiness_module
from isar.drive.steering.generators import GENERATOR_FUNCTIONS, GeneratorOutcome, dependency_depth
from isar.drive.steering.planner import (
    AGENDA_COMPLETE,
    CONTROLLER_FAULT,
    NO_ACTION_WITH_HONEST_EXHAUSTION,
    NO_ELIGIBLE_ACTION,
    PROGRAM_STORE,
    SELECTED,
    _build_inventory,
    _ProgramRevision,
    plan_next_action,
)
from isar.drive.steering.readiness import GeneratorRunOutcome, QuestionRunRecord, RunResult
from isar.scene.store import StaleSceneAtPublication

P1 = "a" * 64


# ----------------------------------------------------------------------------
# Builders
# ----------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _SceneRevision:
    """Duck-typed for ``SCENE_STORE`` -- mirrors ``AgendaRevision.of``'s exact content-addressing
    rule so a hand-built scene payload can be stored without pulling in ``isar.scene.build``'s
    full observation pipeline."""

    canonical: bytes
    digest: str

    @classmethod
    def of(cls, payload: dict) -> "_SceneRevision":
        blob = canonical_bytes(payload)
        return cls(canonical=blob, digest=digest_bytes(blob))


def scene(footprints=(), observations=(), clusters=(), sample_knowledge=None):
    return {"observations": list(observations), "footprints": list(footprints),
            "clusters": list(clusters), "non_scoring": [],
            "sample_knowledge": sample_knowledge or {}, "condition_t_round": None}


def obs(scan_ref, candidates=(), count_trust="trusted", epoch=1):
    return {"scan_ref": scan_ref, "candidates": list(candidates), "count_trust": count_trust,
            "sample": "s1", "lattice_epoch": epoch, "conditions": [], "best_label": None}


def cand(label, omegas=(), identity=None, usable=True):
    return {"label": label, "omegas": list(omegas), "identity": identity, "usable": usable,
            "components": [{"kind": "mode", "slot": i} for i in range(len(omegas))],
            "n_modes": len(omegas), "mode_intervals": [None] * len(omegas)}


def alt(assignment, posterior):
    return {"assignment": [list(pair) for pair in assignment], "posterior": posterior,
            "links": []}


def cluster(scan_refs, alternatives, cluster_id="c0"):
    return {"cluster_id": cluster_id, "scan_refs": list(scan_refs),
            "alternatives": list(alternatives), "truncated": False,
            "truncation_unbounded": False, "log_discarded_mass_bound": None,
            "linkage_truncated": False}


def fp(scan_ref, coord, bands=((0.0, 10.0),), exposure=5.0, partition=P1):
    return {"scan_ref": scan_ref, "axis_coordinate": coord, "partition_id": partition,
            "effective_exposure": exposure,
            "bands": [{"lo": lo, "hi": hi, "sides": "stokes"} for lo, hi in bands]}


def discover_step(step_id="discover-1", q_start=(2.0, 0.0, 0.0), q_end=(3.0, 0.0, 0.0),
                  spacing=0.5, domain=(0.0, 10.0), exposure_floor=1.0,
                  catalogue_profile="isar.catalogue.default/1"):
    spec = DiscoverAndMapSpec(
        q_start=q_start, q_end=q_end, maximum_initial_spacing_rlu=spacing,
        intrinsic_omega_domain_meV=domain, catalogue_profile=catalogue_profile,
        resolved_policy=resolve_policy(user_ask={"min_effective_exposure": exposure_floor}))
    return CampaignStep(id=step_id, kind="discover_and_map", payload=spec)


def program_with(step) -> CampaignProgram:
    return CampaignProgram(steps=(step,))


def write_scene(report_dir: Path, scene_payload: dict) -> None:
    SCENE_STORE.write_revision(report_dir, _SceneRevision.of(scene_payload))


def decision_lines(report_dir: Path) -> list[dict]:
    path = report_dir / "decisions.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _two_action_generator(coords):
    """A synthetic generator (patched onto ``opening_book_survey``) proposing one action per
    coordinate in ``coords`` -- same family_priority/dependency_depth (both from the ONE spec
    invoked), differing only in ``hkl`` and therefore in scheduler `locality` and scientific
    action fingerprint. Used to make ``locality_coordinate`` deterministically pick a winner."""
    def generator(context, spec_dict):
        pairs = []
        for coord in coords:
            intent = MeasurementIntent(
                action_class="discovery_scan", hkl=(coord, 0.0, 0.0), energy_lo=0.0,
                energy_hi=10.0, window_family="discovery",
                evidence_basis_fingerprint=context.scene_digest,
                # The declared physics rides on the intent (review finding 2026-08-18), and a
                # synthetic generator has to honour that contract too: `proposal_from_intent`
                # VERIFIES the fingerprint, so an intent minted without the context's would refuse
                # at realization and this stand-in would stop standing in for the real thing.
                hypotheses_fingerprint=context.hypotheses_fingerprint)
            association = QuestionActionAssociation(
                basis_agenda_digest=context.basis_agenda_digest,
                question_id=spec_dict["question_id"], generator="opening_book_survey",
                family_priority=int(spec_dict["priority"]),
                dependency_depth=dependency_depth(spec_dict, context.agenda_payload),
                reason="two_action test")
            pairs.append((intent, association))
        return GeneratorOutcome.proposed(pairs, proposals_exhaustive=True)
    return generator


# ----------------------------------------------------------------------------
# Double-run byte identity
# ----------------------------------------------------------------------------
def test_double_run_is_byte_identical(tmp_path):
    step = discover_step()
    program = program_with(step)
    write_scene(tmp_path, scene())        # unmeasured workspace: stable, deterministic outcome

    first = plan_next_action(tmp_path, program, "discover-1")
    agenda_after_first = AGENDA_STORE.pointer_digest(tmp_path)
    program_after_first = PROGRAM_STORE.pointer_digest(tmp_path)
    inventory_bytes_first = Path(tmp_path, "inventory", "discovery",
                                 f"{first.inventory_ref['sha256']}.json").read_bytes()

    second = plan_next_action(tmp_path, program, "discover-1")
    agenda_after_second = AGENDA_STORE.pointer_digest(tmp_path)
    program_after_second = PROGRAM_STORE.pointer_digest(tmp_path)
    inventory_bytes_second = Path(tmp_path, "inventory", "discovery",
                                  f"{second.inventory_ref['sha256']}.json").read_bytes()

    assert first.outcome == second.outcome == NO_ELIGIBLE_ACTION
    assert first.agenda_digest == second.agenda_digest
    assert agenda_after_first == agenda_after_second        # agenda store: unchanged
    assert program_after_first == program_after_second       # program store: unchanged
    assert inventory_bytes_first == inventory_bytes_second   # inventory blob: byte-identical

    lines = decision_lines(tmp_path)
    assert len(lines) == 2
    assert lines[0] == lines[1]                              # decision line: byte-identical shape


def test_an_orphan_inventory_blob_does_not_perturb_the_next_decision(tmp_path):
    """The losing-writer orphan policy (PR 4.3 C6), ratified as documented-idempotent: a blob left
    behind by a decision that failed to publish is content-addressed litter — nothing references it
    and no reader enumerates the directory — so the next decision is byte-identical with it present.
    That idempotence IS the policy; there is no cleanup daemon, and adding one would be work in
    service of nothing."""
    step = discover_step()
    program = program_with(step)
    write_scene(tmp_path, scene())

    first = plan_next_action(tmp_path, program, "discover-1")
    orphan = Path(tmp_path, "inventory", "discovery", f"{'0' * 64}.json")
    orphan.write_text('{"evaluations": [], "reports": []}', encoding="utf-8")

    second = plan_next_action(tmp_path, program, "discover-1")

    lines = decision_lines(tmp_path)
    assert lines[0] == lines[1]
    assert first.inventory_ref == second.inventory_ref
    assert orphan.is_file(), "the orphan was cleaned up: the policy is to leave it alone"


# ----------------------------------------------------------------------------
# controller_fault: a raising generator, siblings still persisted
# ----------------------------------------------------------------------------
def test_controller_fault_from_a_raising_generator_persists_siblings(monkeypatch, tmp_path):
    step = discover_step()
    program = program_with(step)

    def boom(context, spec_dict):
        raise RuntimeError("kaboom")

    patched = dict(GENERATOR_FUNCTIONS)
    patched["opening_book_survey"] = boom
    monkeypatch.setattr(readiness_module, "GENERATOR_FUNCTIONS", patched)

    write_scene(tmp_path, scene(footprints=[fp("e:1:d", 2.1)], observations=[obs("e:1:d")]))
    result = plan_next_action(tmp_path, program, "discover-1")

    assert result.outcome == CONTROLLER_FAULT
    assert result.selection_fingerprint is None
    assert result.reason is not None

    # Siblings are still persisted in the inventory artifact -- VerifyCompleteness's own
    # accounting (dependency_blocked, since DiscoverRegion never resolved) is visible.
    inventory_path = tmp_path / "inventory" / "discovery" / f"{result.inventory_ref['sha256']}.json"
    record = json.loads(inventory_path.read_text(encoding="utf-8"))
    generators = {g["generator"]: g for g in record["reports"]}
    assert generators["opening_book_survey"]["status"] == "failed"

    line = decision_lines(tmp_path)[-1]
    assert line["outcome"] == CONTROLLER_FAULT
    assert line["selection"] is None


# ----------------------------------------------------------------------------
# Association forgery (PR #33 A-P1-a): the runner refuses a mismatched association
# ----------------------------------------------------------------------------
def test_association_forgery_by_a_generator_is_a_controller_fault(monkeypatch, tmp_path):
    """A generator returning an association whose fields disagree with the controller-owned
    spec (a forged priority here) is refused by the runner (readiness.py), and the planner's
    existing ValueError-to-controller_fault conversion catches it -- never a silent scheduling
    corruption reaching persistence."""
    step = discover_step()
    program = program_with(step)

    def forge(context, spec_dict):
        intent = MeasurementIntent(action_class="discovery_scan", hkl=(2.5, 0.0, 0.0),
                                   energy_lo=1.0, energy_hi=8.0, window_family="discovery",
                                   evidence_basis_fingerprint=context.scene_digest)
        association = QuestionActionAssociation(
            basis_agenda_digest=context.basis_agenda_digest, question_id=spec_dict["question_id"],
            generator=spec_dict["generator_ids"][0],
            family_priority=int(spec_dict["priority"]) + 999,       # forged
            dependency_depth=0, reason="forged")
        return GeneratorOutcome.proposed(((intent, association),), proposals_exhaustive=True)

    patched = dict(GENERATOR_FUNCTIONS)
    patched["opening_book_survey"] = forge
    monkeypatch.setattr(readiness_module, "GENERATOR_FUNCTIONS", patched)

    write_scene(tmp_path, scene())
    result = plan_next_action(tmp_path, program, "discover-1")

    assert result.outcome == CONTROLLER_FAULT
    assert "family_priority" in result.reason


# ----------------------------------------------------------------------------
# Inventory accounting (PR #33 A-P1-c): a FAILED generator's real survivors on a sibling
# question must not desync `generated` from the actual evaluations
# ----------------------------------------------------------------------------
def test_generator_failed_on_one_question_succeeding_on_a_sibling_keeps_the_inventory_sound():
    """The bug: `generated` was hard-zeroed for any generator with a failed outcome anywhere in
    the run, even when that SAME generator id also produced real survivors on another question
    in the same decision -- the inventory then contradicted its own evaluations
    (`structural_problems()` flagged it). The fix reports the actual surviving count regardless
    of status; FAILED still forces `is_complete=False` on its own."""
    intent = MeasurementIntent(action_class="discovery_scan", hkl=(2.5, 0.0, 0.0),
                               energy_lo=1.0, energy_hi=8.0, window_family="discovery")
    association = QuestionActionAssociation(
        basis_agenda_digest="b" * 64, question_id="q:B", generator="opening_book_survey",
        family_priority=0, dependency_depth=0, reason="test")
    survivor = ActionEvaluation(intent=intent, generator="opening_book_survey",
                                eligibility=ActionEligibility.ELIGIBLE, associations=(association,))

    run_result = RunResult(records=(
        QuestionRunRecord("q:A", "DiscoverRegion", ("opening_book_survey",), "ran",
                          (GeneratorRunOutcome("opening_book_survey", "failed",
                                              error_code="RuntimeError: boom"),)),
        QuestionRunRecord("q:B", "DiscoverRegion", ("opening_book_survey",), "ran",
                          (GeneratorRunOutcome("opening_book_survey", "proposals", generated=1),)),
    ), evaluations=(survivor,))

    inventory = _build_inventory(run_result, [survivor], {})
    assert inventory.structural_problems() == []
    assert inventory.is_complete is False               # FAILED still forces incompleteness
    report = {r.generator: r for r in inventory.reports}["opening_book_survey"]
    assert report.status == GeneratorStatus.FAILED
    assert report.generated == 1                         # the fix: real survivor count, not 0


# ----------------------------------------------------------------------------
# Locality + rank table persistence (PR #33 A-P1-e)
# ----------------------------------------------------------------------------
def test_locality_coordinate_changes_the_winner_and_surfaces_in_the_fingerprinted_basis(
        monkeypatch, tmp_path):
    """The reviewer's exact finding: locality_coordinate can change the selected action; it must
    not do so invisibly. Two decisions over the IDENTICAL scene/program, differing only in
    locality_coordinate, pick DIFFERENT winners AND produce different decision_basis.locality
    blocks AND different context_fingerprints -- locality rides INSIDE the fingerprinted input,
    never merely beside it."""
    step = discover_step(q_start=(2.0, 0.0, 0.0), q_end=(3.0, 0.0, 0.0), spacing=0.3)
    program = program_with(step)
    patched = dict(GENERATOR_FUNCTIONS)
    patched["opening_book_survey"] = _two_action_generator((2.0, 2.9))
    monkeypatch.setattr(readiness_module, "GENERATOR_FUNCTIONS", patched)
    write_scene(tmp_path, scene(footprints=[fp("e:1:d", 2.0)]))

    near_start = plan_next_action(tmp_path, program, "discover-1", locality_coordinate=2.0)
    near_end = plan_next_action(tmp_path, program, "discover-1", locality_coordinate=2.9)

    assert near_start.outcome == SELECTED and near_end.outcome == SELECTED
    assert near_start.selection_fingerprint != near_end.selection_fingerprint

    lines = decision_lines(tmp_path)
    basis_a, basis_b = lines[0]["decision_basis"], lines[1]["decision_basis"]
    assert basis_a["locality"] == {"fn": planner_module.LOCALITY_VERSION, "coordinate": 2.0}
    assert basis_b["locality"] == {"fn": planner_module.LOCALITY_VERSION, "coordinate": 2.9}
    assert basis_a != basis_b

    fp_a = lines[0]["scan_design_provenance"]["context_fingerprint"]
    fp_b = lines[1]["scan_design_provenance"]["context_fingerprint"]
    assert fp_a != fp_b

    # Every eligible evaluation appears exactly once, in scheduler order (winner row 0).
    table = lines[1]["selection"]["rank_table"]
    assert len(table) == 2
    assert table[0]["scientific_action_fingerprint"] == near_end.selection_fingerprint
    assert len({row["scientific_action_fingerprint"] for row in table}) == 2


def test_a_retirement_is_visible_in_the_basis_and_in_the_context_fingerprint(monkeypatch, tmp_path):
    """The A-P1-e shape again (PR 4.3 C6): the retirement set narrows what ``select()`` may pick, so
    it may not do so invisibly. Two decisions over the IDENTICAL scene and program, differing ONLY
    in ``retired_physical_actions``, pick different winners AND differ in both the decision basis and
    the context fingerprint. The empty set embeds as nothing at all — the same conditional rule
    ``declared_partition`` uses — which is what keeps every pre-C6 record byte-identical
    (``test_double_run_is_byte_identical`` is that pin)."""
    from isar.schema.action_identity import physical_action_key

    step = discover_step(q_start=(2.0, 0.0, 0.0), q_end=(3.0, 0.0, 0.0), spacing=0.3)
    program = program_with(step)
    patched = dict(GENERATOR_FUNCTIONS)
    patched["opening_book_survey"] = _two_action_generator((2.0, 2.9))
    monkeypatch.setattr(readiness_module, "GENERATOR_FUNCTIONS", patched)
    write_scene(tmp_path, scene(footprints=[fp("e:1:d", 2.0)]))

    plain = plan_next_action(tmp_path, program, "discover-1")
    assert plain.outcome == SELECTED
    # Built with NO evidence basis at all and still the right key: the physical key is invariant to
    # the scene digest the generator stamped, which is the whole reason the retirement bound binds.
    retired = physical_action_key(MeasurementIntent(
        action_class="discovery_scan", hkl=(2.0, 0.0, 0.0), energy_lo=0.0, energy_hi=10.0,
        window_family="discovery"))
    narrowed = plan_next_action(tmp_path, program, "discover-1",
                                retired_physical_actions=frozenset({retired}))

    assert narrowed.outcome == SELECTED
    assert narrowed.selection_fingerprint != plain.selection_fingerprint

    without, with_retirement = decision_lines(tmp_path)
    assert "retired_physical_actions" not in without["decision_basis"]
    assert with_retirement["decision_basis"]["retired_physical_actions"] == [retired]
    assert (without["scan_design_provenance"]["context_fingerprint"]
            != with_retirement["scan_design_provenance"]["context_fingerprint"])
    # Narrowed for SELECTION only: the retired action is still in the persisted inventory.
    blob = json.loads(Path(tmp_path, with_retirement["inventory_ref"]["path"].removeprefix("report/")
                           ).read_text(encoding="utf-8"))
    assert len(blob["evaluations"]) == 2


def test_rank_table_replays_byte_identically_on_a_double_run(monkeypatch, tmp_path):
    step = discover_step(q_start=(2.0, 0.0, 0.0), q_end=(3.0, 0.0, 0.0), spacing=0.3)
    program = program_with(step)
    patched = dict(GENERATOR_FUNCTIONS)
    patched["opening_book_survey"] = _two_action_generator((2.0, 2.9))
    monkeypatch.setattr(readiness_module, "GENERATOR_FUNCTIONS", patched)
    write_scene(tmp_path, scene(footprints=[fp("e:1:d", 2.0)]))

    first = plan_next_action(tmp_path, program, "discover-1", locality_coordinate=2.0)
    second = plan_next_action(tmp_path, program, "discover-1", locality_coordinate=2.0)
    assert first.outcome == second.outcome == SELECTED

    lines = decision_lines(tmp_path)
    assert len(lines) == 2
    assert lines[0]["selection"]["rank_table"] == lines[1]["selection"]["rank_table"]
    assert lines[0] == lines[1]                                    # the whole record, byte-shape


# ----------------------------------------------------------------------------
# Shared action serving priorities 0 and 4 ranks by 0, order-invariant
# ----------------------------------------------------------------------------
def test_shared_action_serving_priorities_0_and_4_ranks_by_0(monkeypatch, tmp_path):
    """Synthetic: two REAL generators (opening_book_survey at family_priority 0,
    parameter_refine at family_priority 4) are patched to both emit the identical scientific
    action, so dedup collapses them onto one survivor whose rank must be the MINIMUM (0)."""
    step = discover_step(q_start=(2.0, 0.0, 0.0), q_end=(3.0, 0.0, 0.0), spacing=0.3)
    program = program_with(step)

    def collide(context, spec_dict):
        intent = MeasurementIntent(action_class="collide_action", hkl=(2.5, 0.0, 0.0),
                                   energy_lo=1.0, energy_hi=8.0, window_family="discovery",
                                   evidence_basis_fingerprint=context.scene_digest)
        association = QuestionActionAssociation(
            basis_agenda_digest=context.basis_agenda_digest, question_id=spec_dict["question_id"],
            generator=spec_dict["generator_ids"][0], family_priority=int(spec_dict["priority"]),
            dependency_depth=dependency_depth(spec_dict, context.agenda_payload),
            reason="collision test")
        return GeneratorOutcome.proposed(((intent, association),), proposals_exhaustive=True)

    patched = dict(GENERATOR_FUNCTIONS)
    patched["opening_book_survey"] = collide
    patched["parameter_refine"] = collide
    monkeypatch.setattr(readiness_module, "GENERATOR_FUNCTIONS", patched)

    # A confirmed anchor (zone_period declared, a confident mode) chains EstimateParameter open
    # (no trusted interval by default) while DiscoverRegion in the SAME partition stays open too
    # (spacing 0.3 with one footprint at 2.0 leaves a gap up to 3.0 far wider than that).
    write_scene(tmp_path, scene(
        footprints=[fp("e:1:d", 2.0)],
        observations=[obs("e:1:d", candidates=[cand("A", omegas=(5.0,))])],
        clusters=[cluster(["e:1:d"], [alt([("e:1:d", "A")], 0.95)])],
        sample_knowledge={"zone_period": 1.0, "lattice_epoch": 1}))
    result = plan_next_action(tmp_path, program, "discover-1")

    assert result.outcome == SELECTED
    line = decision_lines(tmp_path)[-1]
    selection = line["selection"]
    assert selection["rank_vector"][0] == 0                 # ranks by priority 0, never 4
    priorities = {a["family_priority"] for a in selection["associations"]}
    assert priorities == {0, 4}                              # BOTH associations survive the union
    assert selection["supplying_association"]["family_priority"] == 0


# ----------------------------------------------------------------------------
# Cold start: unmeasured workspace
# ----------------------------------------------------------------------------
def test_unmeasured_workspace_is_no_eligible_action_never_a_fault(tmp_path):
    step = discover_step()
    program = program_with(step)
    write_scene(tmp_path, scene())

    result = plan_next_action(tmp_path, program, "discover-1")
    assert result.outcome == NO_ELIGIBLE_ACTION

    discover_records = [r for r in result.run_records if r.family == "DiscoverRegion"]
    assert len(discover_records) == 1
    outcomes = discover_records[0].generator_outcomes
    assert len(outcomes) == 1
    assert outcomes[0].kind == "inapplicable"
    assert outcomes[0].inapplicable_reason == "program_scoped_subject_unfanned"


# ----------------------------------------------------------------------------
# Honest exhaustion (PR #33 A-P0-3/W5, reconciled against apply_exhaustion's real semantics):
# apply_exhaustion requires a DURABLE BLOCKER on the assessment for ANY closure, including an
# all-clean-zero proof (agenda.py:596-606) -- "nothing left to measure" is not by itself a
# physical reason a question can never be answered. A clean-zero proof with no durable blocker
# to attach to is therefore REJECTED as generator_defect and the question stays open: the
# honest planner reading of "no eligible action, zero actions, and the one exhaustion claim we
# had never actually closed anything" is no_eligible_action, NOT no_action_with_honest_
# exhaustion. Since no 4.1/4.2 evaluator emits a durable blocker yet (isar.scene.agenda's own
# docstring), no_action_with_honest_exhaustion is reachable in THIS PR only with
# synthetic/authenticated blocker_evidence (W4) -- 4.3's durable-blocker producers are the live
# path. Both readings are pinned below.
# ----------------------------------------------------------------------------
def test_clean_zero_without_a_durable_blocker_is_no_eligible_action_not_honest_exhaustion(
        tmp_path):
    step = discover_step(q_start=(2.0, 0.0, 0.0), q_end=(2.5, 0.0, 0.0), spacing=1.0)
    program = program_with(step)
    write_scene(tmp_path, scene(
        footprints=[fp("e:1:d", 2.25, bands=((0.0, 10.0),))],       # fully covers the tiny region
        observations=[obs("e:1:d", candidates=())]))                # trusted + fitless

    result = plan_next_action(tmp_path, program, "discover-1")
    assert result.outcome == NO_ELIGIBLE_ACTION

    verify_records = [r for r in result.run_records if r.family == "VerifyCompleteness"]
    assert len(verify_records) == 1
    assert verify_records[0].disposition == "ran"
    outcome = verify_records[0].generator_outcomes[0]
    # The clean-zero proof was honestly EMITTED by the generator/producer layer...
    assert outcome.generator == "exposure_fill" and outcome.kind == "clean_zero"
    assert outcome.exhaustion_clean is True
    # ...but never APPLIED: the persisted agenda still shows the question open, with a
    # generator_defect note recording exactly why apply_exhaustion refused to close it.
    _pointer, payload = AGENDA_STORE.read_current(tmp_path)
    verify_assessment = next(a for a in payload["assessments"] if a["question_id"] ==
                             next(s for s in payload["specs"]
                                 if s["family"] == "VerifyCompleteness")["question_id"])
    assert verify_assessment["status"] == "open"
    assert dict(verify_assessment["notes"]).get("generator_defect")


def test_synthetic_blocker_evidence_reaches_honest_exhaustion_via_an_applied_closure(tmp_path):
    """The reachable variant: authenticated blocker_evidence lets the SAME clean-zero proof
    actually CLOSE the question (unresolvable), and the planner reads that as
    no_action_with_honest_exhaustion -- an applied closure, never a merely-emitted one."""
    step = discover_step(q_start=(2.0, 0.0, 0.0), q_end=(2.5, 0.0, 0.0), spacing=1.0)
    program = program_with(step)
    scene_payload = scene(
        footprints=[fp("e:1:d", 2.25, bands=((0.0, 10.0),))],
        observations=[obs("e:1:d", candidates=())])
    write_scene(tmp_path, scene_payload)

    # Learn pass-1's own digests exactly as the planner will compute them (blocker_evidence
    # never changes basis_digest -- attachment runs strictly AFTER it is captured), so the
    # evidence constructed here authenticates against the SAME derivation the planner runs.
    step_payload = step.payload.to_dict()
    pass1 = derive_agenda(scene_payload, step_payload, step_id="discover-1",
                          program_fingerprint=program_fingerprint(program),
                          generator_registry_fingerprint=DISCOVERY_GENERATOR_REGISTRY_VERSION)
    verify_qid = next(s for s in pass1.payload["specs"]
                      if s["family"] == "VerifyCompleteness")["question_id"]

    evidence = QuestionBlockerEvidence(
        basis_agenda_digest=pass1.payload["basis_digest"], question_id=verify_qid,
        scene_digest=pass1.payload["scene_digest"],
        generator_registry_fingerprint=DISCOVERY_GENERATOR_REGISTRY_VERSION,
        blocker_name="instrument_geometry_infeasible", supporting_generator_ids=("exposure_fill",))

    result = plan_next_action(tmp_path, program, "discover-1", blocker_evidence=(evidence,))
    assert result.outcome == NO_ACTION_WITH_HONEST_EXHAUSTION

    _pointer, payload = AGENDA_STORE.read_current(tmp_path)
    verify_assessment = next(a for a in payload["assessments"] if a["question_id"] == verify_qid)
    assert verify_assessment["status"] == "unresolvable"
    assert any(b["name"] == "instrument_geometry_infeasible" for b in verify_assessment["blockers"])


def test_agenda_complete_when_nothing_is_open(tmp_path):
    """A fully-empty region (q_start==region edge, trivially covered) with NOTHING to report
    incomplete leaves the agenda complete on the first pass -- the other honestly-reachable
    terminal state, distinct from no_action_with_honest_exhaustion."""
    step = discover_step(q_start=(2.0, 0.0, 0.0), q_end=(2.5, 0.0, 0.0), spacing=1.0,
                         exposure_floor=1.0)
    program = program_with(step)
    write_scene(tmp_path, scene(
        footprints=[fp("e:1:d", 2.25, bands=((0.0, 10.0),), exposure=5.0)],
        observations=[obs("e:1:d", candidates=[{"label": "B", "omegas": [], "identity": None,
                                                "usable": True, "components": [], "n_modes": 0,
                                                "mode_intervals": []}])]))
    result = plan_next_action(tmp_path, program, "discover-1")
    assert result.outcome == AGENDA_COMPLETE


# ----------------------------------------------------------------------------
# Persisted record round-trips through EligibilityInput / _scan_design_identity
# ----------------------------------------------------------------------------
def test_scan_design_provenance_round_trips_through_eligibility(tmp_path):
    step = discover_step()
    program = program_with(step)
    write_scene(tmp_path, scene())
    plan_next_action(tmp_path, program, "discover-1")
    record = decision_lines(tmp_path)[-1]

    provenance = record["scan_design_provenance"]
    assert set(provenance) == {"inventory_fingerprint", "context_fingerprint",
                               "selected_candidate_id", "status", "recovery_state"}
    value = EligibilityInput.from_current_contract(
        action_intent=None, action_provenance=None, target_outcome=None, selection_record=None,
        qoi_evidence=None, shadow_record=None, scan_design_provenance=provenance,
        scan_design_events=record["scan_design_events"])
    identity = _scan_design_identity(value.scan_design_provenance, value.scan_design_events)
    assert identity["inventory_fingerprint"] == provenance["inventory_fingerprint"]
    assert identity["context_fingerprint"] == provenance["context_fingerprint"]
    assert identity["selected_candidate_id"] == provenance["selected_candidate_id"]
    assert identity["status"] == provenance["status"]
    assert identity["recovery_state"] == provenance["recovery_state"]
    assert identity["events"] == sorted(record["scan_design_events"])


# ----------------------------------------------------------------------------
# Census kind-check refuses a discovery ref
# ----------------------------------------------------------------------------
def test_census_kind_check_refuses_a_discovery_inventory_ref(tmp_path):
    from tools.stats._eig_eligibility_census import _inventory_intent

    step = discover_step()
    program = program_with(step)
    write_scene(tmp_path, scene())
    result = plan_next_action(tmp_path, program, "discover-1")

    record, problem = _inventory_intent(tmp_path, result.inventory_ref, "irrelevant")
    assert record is None and problem == "inventory_reference_missing"
    # The legacy kind is what the census actually accepts -- confirms the check is a real kind
    # filter, not a path/shape rejection that happens to also catch ours.
    legacy_ref = {**result.inventory_ref, "kind": "action_inventory"}
    _record2, problem2 = _inventory_intent(tmp_path, legacy_ref, "irrelevant")
    assert problem2 != "inventory_reference_missing"


# ----------------------------------------------------------------------------
# Replay glob blindness
# ----------------------------------------------------------------------------
def test_replay_glob_is_blind_to_the_discovery_subdirectory(tmp_path):
    """tools/eig_replay/replay_o41_test4.py globs (workspace/"report"/"inventory").glob("*.json")
    -- non-recursive. A discovery artifact one level deeper is invisible to it by construction."""
    top_level = tmp_path / "report" / "inventory"
    top_level.mkdir(parents=True)
    (top_level / "legacy123.json").write_text("{}", encoding="utf-8")
    discovery_dir = top_level / "discovery"
    discovery_dir.mkdir()
    (discovery_dir / "x.json").write_text("{}", encoding="utf-8")

    seen = sorted(p.name for p in top_level.glob("*.json"))
    assert seen == ["legacy123.json"]


# ----------------------------------------------------------------------------
# Program store: round trip + concurrency
# ----------------------------------------------------------------------------
def test_program_store_round_trip_re_fingerprints_to_its_own_name(tmp_path):
    step = discover_step()
    program = program_with(step)
    revision = _ProgramRevision.of(program)
    PROGRAM_STORE.write_revision(tmp_path, revision)

    pointer, payload = PROGRAM_STORE.read_current(tmp_path)
    assert pointer["digest"] == revision.digest
    from isar.schema.campaign_program import CampaignProgram as CP
    from isar.schema.campaign_program import program_fingerprint
    reread = CP.from_dict(payload)
    assert program_fingerprint(reread) == pointer["digest"]


def test_program_store_round_trips_unicode_source_text_without_corruption(tmp_path):
    """PR #33 A-P0-5/B-1: ``program_fingerprint`` used to hash ``ensure_ascii=True`` bytes while
    the program store writes ``ensure_ascii=False`` -- any non-ASCII program (a Unicode
    ``source_text``, here) wrote a digest that did not match the store's own recomputation, and
    the very next read raised "does not match its digest" on content this same process wrote."""
    step = discover_step()
    program = CampaignProgram(steps=(step,), source_text="map ω near Γ")
    PROGRAM_STORE.write_revision(tmp_path, _ProgramRevision.of(program))

    pointer, payload = PROGRAM_STORE.read_current(tmp_path)      # must not raise
    assert payload["source_text"] == "map ω near Γ"


def test_program_store_concurrent_write_is_refused(tmp_path):
    step_a = discover_step(spacing=0.5)
    step_b = discover_step(spacing=0.3)          # a genuinely different program
    program_a = program_with(step_a)
    program_b = program_with(step_b)

    snapshot = PROGRAM_STORE.pointer_digest(tmp_path)          # None: empty store
    PROGRAM_STORE.write_revision(tmp_path, _ProgramRevision.of(program_a),
                                 expected_parent=snapshot)
    with pytest.raises(ConcurrentRevisionWrite):
        # A second writer still holding the STALE (empty) snapshot tries to publish a different
        # program after the first writer already moved the pointer.
        PROGRAM_STORE.write_revision(tmp_path, _ProgramRevision.of(program_b),
                                     expected_parent=snapshot)


# ----------------------------------------------------------------------------
# Publication guards (PR #33 W6/A-P0-4): scene re-check + program store CAS
# ----------------------------------------------------------------------------
def test_a_scene_write_racing_between_snapshot_and_publication_is_refused(monkeypatch, tmp_path):
    """The publication-time re-check (hook point ``_current_scene_pointer_digest``) catches a
    scene pointer moved between the planner's snapshot and its agenda write: the decision raises
    rather than publishing an agenda built against the stale scene."""
    step = discover_step()
    program = program_with(step)
    write_scene(tmp_path, scene())

    monkeypatch.setattr(planner_module, "_current_scene_pointer_digest",
                        lambda report_dir: "f" * 64)  # never the real snapshot's digest

    with pytest.raises(StaleSceneAtPublication):
        plan_next_action(tmp_path, program, "discover-1")

    assert decision_lines(tmp_path) == []                # zero decision lines
    assert AGENDA_STORE.pointer_digest(tmp_path) is None  # agenda pointer unmoved


def test_program_store_cas_refuses_a_concurrent_write_during_planning(monkeypatch, tmp_path):
    """A second writer publishing a DIFFERENT program between the planner's own planning-start
    snapshot and its write surfaces ConcurrentRevisionWrite -- the program store CAS wired
    through ``plan_next_action``, not just ``PROGRAM_STORE.write_revision`` in isolation."""
    step = discover_step()
    program = program_with(step)
    write_scene(tmp_path, scene())

    real_snapshot = planner_module._current_program_pointer_digest

    def racing_snapshot(report_dir):
        digest = real_snapshot(report_dir)
        other = program_with(discover_step(spacing=0.9))
        PROGRAM_STORE.write_revision(report_dir, _ProgramRevision.of(other),
                                     expected_parent=digest)
        return digest                                     # the now-STALE snapshot

    monkeypatch.setattr(planner_module, "_current_program_pointer_digest", racing_snapshot)

    with pytest.raises(ConcurrentRevisionWrite):
        plan_next_action(tmp_path, program, "discover-1")


# ----------------------------------------------------------------------------
# Scene missing
# ----------------------------------------------------------------------------
def test_scene_store_missing_is_controller_fault_with_reason(tmp_path):
    step = discover_step()
    program = program_with(step)

    result = plan_next_action(tmp_path, program, "discover-1")
    assert result.outcome == CONTROLLER_FAULT
    assert "scene" in result.reason
    assert result.inventory_ref is None

    # Nothing persisted except the decision record.
    assert not (tmp_path / "inventory").exists()
    assert not (tmp_path / "agenda").exists()
    assert not (tmp_path / "program").exists()
    lines = decision_lines(tmp_path)
    assert len(lines) == 1
    assert lines[0]["outcome"] == CONTROLLER_FAULT
    assert lines[0]["decision_basis"]["scene_digest"] is None


# ----------------------------------------------------------------------------
# Pinned scene input (SCENE-4.3 D1): plan from a content address, not from the pointer
# ----------------------------------------------------------------------------
def test_a_pinned_decision_steers_from_its_pin_while_the_pointer_moves_under_it(tmp_path):
    """The campaign's case. The parent built revision A and pinned it; a read-only
    ``isar report --scene`` publishes B before the decision reaches publication. The pinned
    decision plans against A start to finish and does not fault — the pointer was never its
    authority, so a report CLI cannot stop a live campaign (which the four-reasons law forbids)."""
    program = program_with(discover_step())
    pinned = _SceneRevision.of(scene())
    write_scene(tmp_path, scene())
    assert SCENE_STORE.pointer_digest(tmp_path) == pinned.digest

    write_scene(tmp_path, scene(observations=[obs("e:9:d")]))     # the racing publish
    assert SCENE_STORE.pointer_digest(tmp_path) != pinned.digest

    result = plan_next_action(tmp_path, program, "discover-1", scene_pin=pinned.digest)

    assert result.outcome != CONTROLLER_FAULT
    assert result.scene_digest == pinned.digest
    line, = decision_lines(tmp_path)
    assert line["decision_basis"]["scene_digest"] == pinned.digest


def test_the_same_pointer_move_still_refuses_a_pointer_derived_decision(tmp_path):
    """The guard is SCOPED, not removed. Byte for byte the same race, with no pin: the caller
    snapshotted the pointer, so the pointer moving IS its staleness, and PR #33's A-P0-4 guard
    fires exactly as it did before pinning existed."""
    program = program_with(discover_step())
    write_scene(tmp_path, scene())

    # The move lands between the planner's snapshot and its publication re-read.
    def moved(report_dir):
        write_scene(report_dir, scene(observations=[obs("e:9:d")]))
        return SCENE_STORE.pointer_digest(report_dir)

    import isar.drive.steering.planner as pm
    original = pm._current_scene_pointer_digest
    pm._current_scene_pointer_digest = moved
    try:
        with pytest.raises(StaleSceneAtPublication):
            plan_next_action(tmp_path, program, "discover-1")
    finally:
        pm._current_scene_pointer_digest = original

    assert decision_lines(tmp_path) == []
    assert AGENDA_STORE.pointer_digest(tmp_path) is None


def test_the_pinned_planner_input_is_the_stored_canonical_payload(monkeypatch, tmp_path):
    """Live-vs-replay representation probe. What the pinned path hands the derivation is the same
    deserialized canonical payload an offline replay reads off disk — not an in-memory object that
    happens to agree today. Two representations is how live and replay drift apart."""
    program = program_with(discover_step())
    payload = scene(observations=[obs("e:1:d", [cand("A", [-4.0])])])
    pinned = _SceneRevision.of(payload)
    write_scene(tmp_path, payload)

    seen = []
    real_derive = planner_module.derive_agenda
    monkeypatch.setattr(planner_module, "derive_agenda",
                        lambda scene_payload, *a, **kw: (seen.append(scene_payload)
                                                         or real_derive(scene_payload, *a, **kw)))
    plan_next_action(tmp_path, program, "discover-1", scene_pin=pinned.digest)

    assert seen, "the derivation never ran"
    on_disk = json.loads((tmp_path / "scene" / f"{pinned.digest}.json").read_text(encoding="utf-8"))
    assert all(observed == on_disk for observed in seen)
    assert all(canonical_bytes(observed) == pinned.canonical for observed in seen)


def test_an_unloadable_pin_is_a_recorded_controller_fault(tmp_path):
    """Store corruption is the ONE machine failure D1 admits on this path, and it is recorded as a
    fault rather than degraded into a plan against whatever the pointer names — steering from a
    silently older scene is the failure the pin exists to make impossible."""
    program = program_with(discover_step())
    write_scene(tmp_path, scene())

    result = plan_next_action(tmp_path, program, "discover-1", scene_pin="b" * 64)

    assert result.outcome == CONTROLLER_FAULT and result.scene_digest is None
    assert "unloadable" in result.reason
    line, = decision_lines(tmp_path)
    assert line["outcome"] == CONTROLLER_FAULT
    assert line["decision_basis"]["scene_digest"] is None
    assert AGENDA_STORE.pointer_digest(tmp_path) is None


# ----------------------------------------------------------------------------
# placement_rank: the generator's physics ordering outranks the hash tiebreak
# ----------------------------------------------------------------------------
def _ranked_pair(scene_digest: str, *, first_coord: float, second_coord: float):
    """Two evaluations for one question, identical but for hkl and placement_rank (0 then 1)."""
    from isar.drive.acquisition.inventory import ActionEligibility, ActionEvaluation

    out = []
    for rank, coord in enumerate((first_coord, second_coord)):
        intent = MeasurementIntent(
            action_class="discovery_scan", hkl=(coord, 0.0, 0.0), energy_lo=0.0, energy_hi=10.0,
            window_family="discovery", evidence_basis_fingerprint=scene_digest)
        association = QuestionActionAssociation(
            basis_agenda_digest="a" * 64, question_id="q1", generator="opening_book_survey",
            family_priority=1, dependency_depth=0, reason="placement test",
            placement_rank=rank)
        out.append(ActionEvaluation(intent=intent, generator="opening_book_survey",
                                    eligibility=ActionEligibility.ELIGIBLE,
                                    associations=(association,)))
    return out


def test_the_scene_moving_does_not_reshuffle_which_action_is_selected():
    """The failure this pins: an untrusted delivery moves the scene without buying coverage, and
    the scene digest is inside every ``scientific_action_fingerprint``. With every opening-book
    proposal for one question tying on family priority and dependency depth, and locality 0.0 for
    every action (the live runner supplies no coordinate), the winner fell through to that
    fingerprint — a SHA-256 that reshuffles the moment the scene moves. The campaign wandered to
    another q instead of re-measuring the broken one, so the broken-scan counter (which keys on
    the PHYSICAL action) never saw the same key twice and its bound could not bind.
    """
    from isar.drive.steering.scheduler import select

    step_payload = discover_step().payload.to_dict()
    winners = set()
    for nonce in range(12):
        pair = _ranked_pair(f"{nonce:064d}", first_coord=2.0, second_coord=2.9)
        winner, _, _, _ = select(pair, step_payload)
        winners.add(winner.intent.hkl[0])
    assert winners == {2.0}, (
        f"the selected action moved with the scene digest: {sorted(winners)}")


def test_placement_rank_decides_before_the_fingerprint_tiebreak():
    """And it is placement_rank doing it, not the coordinate: swap which proposal the generator
    ranked first and the winner swaps with it, on the identical scene."""
    from isar.drive.steering.scheduler import select

    step_payload = discover_step().payload.to_dict()
    forward, _, _, _ = select(_ranked_pair("d" * 64, first_coord=2.0, second_coord=2.9),
                              step_payload)
    reversed_, _, _, _ = select(_ranked_pair("d" * 64, first_coord=2.9, second_coord=2.0),
                                step_payload)
    assert (forward.intent.hkl[0], reversed_.intent.hkl[0]) == (2.0, 2.9)


def test_the_opening_book_order_reaches_the_association():
    """``propose_survey`` returns placement-informed order; the generator must carry it, or the
    ordering is computed and thrown away."""
    from isar.drive.steering.generators import DiscoveryContext, opening_book_survey

    context = DiscoveryContext(
        scene_payload=scene(footprints=[fp("e:1:d", 2.0)]), agenda_payload={"assessments": []},
        step_payload=discover_step().payload.to_dict(), basis_agenda_digest="a" * 64, scene_digest="b" * 64)
    spec = {"question_id": "q1", "priority": 1,
            "subject": {"kind": "region", "partition_id": P1, "region_rlu": [2.0, 3.0],
                        "omega_band_meV": [0.0, 10.0]}}
    outcome = opening_book_survey(context, spec)
    ranks = [a.placement_rank for _, a in (outcome.proposals or ())]
    assert ranks == sorted(ranks), f"proposal order was not preserved: {ranks}"
    if len(ranks) > 1:
        assert ranks == list(range(len(ranks)))


def test_the_declared_physics_reaches_the_intent_not_just_the_request():
    """``DiscoveryContext`` carries the card's hypothesis fingerprint and the generator stamps it
    on every intent, so the action's own scientific identity accounts for the physics that will
    interpret it. Left at ``None`` until realization, two campaigns testing different physics
    minted the SAME scientific action fingerprint for the same q and window."""
    from isar.drive.steering.generators import DiscoveryContext, opening_book_survey

    spec = {"question_id": "q1", "priority": 1,
            "subject": {"kind": "region", "partition_id": P1, "region_rlu": [2.0, 3.0],
                        "omega_band_meV": [0.0, 10.0]}}

    def fingerprints(hypotheses):
        context = DiscoveryContext(
            scene_payload=scene(footprints=[fp("e:1:d", 2.0)]),
            agenda_payload={"assessments": []}, step_payload=discover_step().payload.to_dict(),
            basis_agenda_digest="a" * 64, scene_digest="b" * 64,
            hypotheses_fingerprint=hypotheses)
        outcome = opening_book_survey(context, spec)
        return [i.hypotheses_fingerprint for i, _ in outcome.proposals], \
               [i.scientific_action_fingerprint for i, _ in outcome.proposals]

    carried, first = fingerprints("physics-A")
    assert carried and set(carried) == {"physics-A"}
    _, second = fingerprints("physics-B")
    assert first and not set(first) & set(second), (
        "two different declared hypothesis sets minted the same scientific action identity")

"""Focused Slice 6 tests for pure shadow inventory scoring."""
from __future__ import annotations

import json
from pathlib import Path
import runpy
import subprocess
import sys
from dataclasses import replace

import pytest

from isar.drive.acquisition.inventory import canonical_json
from isar.drive.acquisition.scan_design import (
    SCAN_DESIGN_INVENTORY_SCHEMA_V2,
    SELECTION_BASIS_LEGACY_CONTROL_OFF,
    SELECTION_BASIS_SHADOW_LEGACY_CONTROL_SCORED,
    ScanDesignInventory,
    design_scan_windows,
    load_scan_design_inventory,
    validate_scan_design_inventory,
)
from isar.drive.objective import ObjectiveSpec, current_objective_comparison_scope
from isar.eig.shadow_inventory import (
    ScanDesignShadowScoring,
    _candidate_pair,
    derive_shadow_seed,
    score_shadow_inventory,
    validate_scan_design_shadow_scoring,
)
from isar.eig.scorer import score_scan_window_candidates
from isar.predictive import evaluate_master_predictive
from isar.schema.eig_policy import (
    QoIDistinctionPolicy,
    QoIDistinctionPolicySet,
    QoIDistinctionVariant,
    ScanWindowShadowScoringConfig,
)
from isar.schema.statistics import semantics_fingerprint


def _fp(name: str) -> str:
    return semantics_fingerprint("tests.eig.shadow_inventory", {"name": name})


def _producer_bundle(monkeypatch):
    eig_helpers = runpy.run_path(str(Path(__file__).with_name("test_design_measure.py")))
    predictive, _projection, _qoi, eligibility = eig_helpers["_bundle"](monkeypatch)
    scan_helpers = runpy.run_path(str(
        Path(__file__).parents[1] / "drive" / "test_scan_design.py"))
    inventory = design_scan_windows(
        scan_helpers["_context"](scan_window_eig_mode="authoritative"))
    return predictive, eligibility, inventory


def _policies(scope: str, *, unsupported: bool = False) -> QoIDistinctionPolicySet:
    variants = (
        QoIDistinctionVariant(
            "coarse", {"origin": 0.0, "bin_width": 1.0, "offsets": [0.0]}),
        QoIDistinctionVariant(
            "fine", {"origin": 0.0, "bin_width": 0.5, "offsets": [0.0]}),
    )
    policy = QoIDistinctionPolicy(
        "policy",
        "isar.future_partitions/1" if unsupported
        else "isar.scalar_absolute_partitions/o36.5",
        {"id": "excitation.energy", "unit": "meV"},
        {"kind": "campaign_configuration", "fingerprint": scope,
         "annotation": {"campaign_name": "fixture", "experiment_description": "Slice 6",
                        "date": "2026-08-03"}},
        {"kind": "explicit_user_analysis/1", "source_id": "owner",
         "rationale": "exercise the live shadow scorer"},
        variants,
    )
    return QoIDistinctionPolicySet((policy,))


def _complete_scoring(monkeypatch):
    predictive, eligibility, inventory = _producer_bundle(monkeypatch)
    scope = _fp("campaign")
    scoring = score_shadow_inventory(
        inventory, predictive_artifact=predictive, eligibility_input=eligibility,
        policy_set=_policies(scope),
        scoring_config=ScanWindowShadowScoringConfig(simulation_count=8, block_count=2),
        scope_kind="campaign_configuration", scope_fingerprint=scope)
    assert scoring.status == "complete"
    return inventory, scoring


def test_shadow_seed_recipe_is_golden_and_signed_63_bit():
    evidence = derive_shadow_seed(
        scoring_config_fingerprint="a" * 64,
        design_measure_fingerprint="b" * 64,
        candidate_pair_fingerprint="c" * 64,
    )
    assert evidence.seed == 2531981767377547511
    assert 0 <= evidence.seed < 2**63
    assert type(evidence).from_dict(evidence.to_dict()) == evidence


def test_real_producer_chain_evaluates_pair_once_and_reuses_it(monkeypatch):
    predictive, eligibility, inventory = _producer_bundle(monkeypatch)
    scope = _fp("campaign")
    calls = {"evaluator": 0, "scorer": 0}

    def counted_evaluator(*args, **kwargs):
        calls["evaluator"] += 1
        return evaluate_master_predictive(*args, **kwargs)

    def counted_scorer(*args, **kwargs):
        calls["scorer"] += 1
        return score_scan_window_candidates(*args, **kwargs)

    result = score_shadow_inventory(
        inventory, predictive_artifact=predictive, eligibility_input=eligibility,
        policy_set=_policies(scope),
        scoring_config=ScanWindowShadowScoringConfig(
            simulation_count=16, block_count=4, max_runtime_variants=8),
        scope_kind="campaign_configuration", scope_fingerprint=scope,
        predictive_evaluator=counted_evaluator, scorer=counted_scorer,
    )

    assert calls == {"evaluator": 2, "scorer": 2}, (
        result.status, result.reason_codes, result.design_measure)
    assert result.status == "complete"
    assert len(result.variants) == 2
    assert "score_refusal_evidence" not in result.to_dict()
    assert ScanDesignShadowScoring.from_dict(result.to_dict()).to_dict() == result.to_dict()
    assert {item.score_set["seed"] for item in result.variants} == {
        result.seed_evidence.seed}
    attached = inventory.with_shadow_scoring(result)
    assert attached.selected_candidate_id == inventory.selected_candidate_id
    assert attached.selection_basis == SELECTION_BASIS_SHADOW_LEGACY_CONTROL_SCORED
    assert validate_scan_design_inventory(attached).content_hash() == attached.content_hash()


def test_policy_refusals_precede_predictive_evaluation(monkeypatch):
    predictive, eligibility, inventory = _producer_bundle(monkeypatch)
    scope = _fp("campaign")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("predictive evaluation must not run")

    config = ScanWindowShadowScoringConfig(simulation_count=8, block_count=2)
    missing = score_shadow_inventory(
        inventory, predictive_artifact=predictive, eligibility_input=eligibility,
        policy_set=QoIDistinctionPolicySet(()), scoring_config=config,
        scope_kind="campaign_configuration", scope_fingerprint=scope,
        predictive_evaluator=forbidden,
    )
    assert missing.status == "unavailable"
    assert missing.reason_codes == ("qoi_policy_missing",)

    unsupported = score_shadow_inventory(
        inventory, predictive_artifact=predictive, eligibility_input=eligibility,
        policy_set=_policies(scope, unsupported=True), scoring_config=config,
        scope_kind="campaign_configuration", scope_fingerprint=scope,
        predictive_evaluator=forbidden,
    )
    assert unsupported.status == "unavailable"
    assert all(item.reason_codes == ("qoi_policy_unsupported",)
               for item in unsupported.variants)
    assert all(item.compiled_partition is None and item.score_set is None
               and item.target_core_minus_legacy is None
               for item in unsupported.variants)


def test_historical_objective_two_shadow_omission_fails_open_through_real_scorer(monkeypatch):
    predictive, eligibility, inventory = _producer_bundle(monkeypatch)
    objective_helpers = runpy.run_path(str(
        Path(__file__).parents[1] / "schema" / "test_eig_policy.py"))
    frozen = objective_helpers["_build_spec"](
        objective_helpers["_goal_data"](), {"tier": "deterministic", "step_h": 0.05})
    historical = frozen.to_dict()
    del historical["scan_window_eig"]
    # The steering mode here is deliberately orthogonal to what this test exercises (a historical
    # objective/2 missing the later-added scan_window_eig block defaults to a no-policy config, and
    # score_shadow_inventory must fail open rather than evaluate) — "off" keeps ObjectiveSpec
    # construction simple; "authoritative" would additionally require a full O41 profile the
    # deleted scan_window_eig block does not supply. ("shadow" itself is retired — SCENE-4 PR 4.4,
    # D3 — and now refuses BY NAME at this exact construction site; see
    # test_a_new_compile_refuses_shadow_scan_window_eig_mode and
    # test_an_absent_mode_frozen_steering_refuses_by_name_with_a_recompile_message in
    # tests/drive/test_goal_to_spec.py for that refusal.)
    historical["steering"]["scan_window_eig_mode"] = "off"
    from isar.schema.hypotheses import content_fingerprint
    historical["hypothesis_fingerprint"] = content_fingerprint(
        historical["hypotheses"], historical["steering"])
    objective = ObjectiveSpec.from_dict(historical)
    assert objective.steering["scan_window_eig_mode"] == "off"
    assert objective.scan_window_eig.policy_set is None
    before = (inventory.selected_candidate_id, inventory.selection_basis)
    evaluations = 0

    def forbidden(*_args, **_kwargs):
        nonlocal evaluations
        evaluations += 1
        raise AssertionError("a historical no-policy objective must not evaluate candidates")

    scope = current_objective_comparison_scope(objective)
    scoring = score_shadow_inventory(
        inventory, predictive_artifact=predictive, eligibility_input=eligibility,
        policy_set=objective.scan_window_eig.policy_set,
        scoring_config=objective.scan_window_eig.scoring_config,
        scope_kind="campaign_configuration", scope_fingerprint=scope["fingerprint"],
        predictive_evaluator=forbidden)
    attached = inventory.with_shadow_scoring(scoring)

    assert evaluations == 0
    assert scoring.reason_codes == ("qoi_policy_missing",)
    assert (attached.selected_candidate_id, attached.selection_basis) == before
    selected = [item for item in attached.candidates
                if item.candidate_id == attached.selected_candidate_id]
    assert len(selected) == 1 and selected[0].candidate_family == "legacy_uniform/1"


@pytest.mark.parametrize(("field", "changed"), [
    ("kind", "identity_probe"),
    ("reason", "tampered target-core rationale"),
])
def test_candidate_pair_rejects_kind_and_reason_mismatch_before_evaluation(
        monkeypatch, field, changed):
    _predictive, _eligibility, inventory = _producer_bundle(monkeypatch)
    target_index = next(index for index, item in enumerate(inventory.candidates)
                        if item.candidate_family == "target_core/1")
    candidates = list(inventory.candidates)
    candidates[target_index] = replace(candidates[target_index], **{field: changed})
    unchecked = type("UncheckedInventory", (), {
        "context": inventory.context, "generator_config": inventory.generator_config,
        "candidates": tuple(candidates)})()

    with pytest.raises(ValueError, match="candidate_pair_invalid"):
        _candidate_pair(unchecked)


@pytest.mark.parametrize(("mutation", "reason"), [
    ("equal_grid", "candidate_pair_degenerate"),
    ("alias", "candidate_pair_degenerate"),
    ("missing", "candidate_family_missing"),
    ("ambiguous", "candidate_family_ambiguous"),
    ("exposure", "candidate_exposure_mismatch"),
    ("structural", "candidate_pair_invalid"),
])
def test_candidate_pair_refusal_taxonomy_is_ordered(monkeypatch, mutation, reason):
    _predictive, _eligibility, inventory = _producer_bundle(monkeypatch)
    legacy = next(item for item in inventory.candidates
                  if item.candidate_family == "legacy_uniform/1")
    target = next(item for item in inventory.candidates
                  if item.candidate_family == "target_core/1")
    if mutation == "equal_grid":
        target = replace(
            target, e_lo=legacy.e_lo, e_hi=legacy.e_hi, e_step=legacy.e_step,
            n_points=legacy.n_points,
            # nominal_cost is a derived invariant of (n_points, ncount); keep it consistent so the
            # mutation under test stays the grid, not the cost field.
            nominal_cost=legacy.n_points * target.ncount)
        candidates = (legacy, target)
    elif mutation == "alias":
        candidates = (replace(legacy, alias_families=(*legacy.alias_families,
                                                       "target_core/1")),)
    elif mutation == "missing":
        candidates = (legacy,)
    elif mutation == "ambiguous":
        alternate = next(item for item in inventory.candidates
                         if item.candidate_family not in {"legacy_uniform/1", "target_core/1"})
        candidates = (legacy, target, replace(alternate, candidate_family="target_core/1"))
    elif mutation == "exposure":
        candidates = (legacy, replace(target, ncount=target.ncount + 1,
                                      nominal_cost=target.n_points * (target.ncount + 1)))
    else:
        candidates = (legacy, replace(target, reason="different action"))
    unchecked = type("UncheckedInventory", (), {
        "context": inventory.context, "generator_config": inventory.generator_config,
        "candidates": candidates, "refusals": (),
    })()

    with pytest.raises(ValueError, match=reason):
        _candidate_pair(unchecked)


def test_exact_target_generator_refusal_is_preserved_with_zero_evaluations(monkeypatch):
    predictive, eligibility, _inventory = _producer_bundle(monkeypatch)
    scan_helpers = runpy.run_path(str(
        Path(__file__).parents[1] / "drive" / "test_scan_design.py"))
    inventory = design_scan_windows(scan_helpers["_context"](
        scan_window_eig_mode="authoritative", minimum_target_support_points=10_000))
    scope = _fp("campaign")
    calls = 0

    def forbidden(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("predictive evaluator must not run")

    result = score_shadow_inventory(
        inventory, predictive_artifact=predictive, eligibility_input=eligibility,
        policy_set=_policies(scope),
        scoring_config=ScanWindowShadowScoringConfig(simulation_count=8, block_count=2),
        scope_kind="campaign_configuration", scope_fingerprint=scope,
        predictive_evaluator=forbidden)

    exact = next(item.reason_code for item in inventory.refusals
                 if item.candidate_family == "target_core/1")
    assert calls == 0
    assert result.reason_codes == (exact,)
    assert not result.variants
    assert "score_refusal_evidence" not in result.to_dict()


def test_deadline_crossing_after_scorer_return_discards_that_variant(monkeypatch):
    predictive, eligibility, inventory = _producer_bundle(monkeypatch)
    scope = _fp("campaign")
    scorer_calls = 0

    class AnalysisLimitExceeded(Exception):
        pass

    def counted_scorer(*args, **kwargs):
        nonlocal scorer_calls
        result = score_scan_window_candidates(*args, **kwargs)
        scorer_calls += 1
        return result

    def deadline():
        if scorer_calls == 2:
            raise AnalysisLimitExceeded("crossed while scorer was running")

    result = score_shadow_inventory(
        inventory, predictive_artifact=predictive, eligibility_input=eligibility,
        policy_set=_policies(scope),
        scoring_config=ScanWindowShadowScoringConfig(simulation_count=8, block_count=2),
        scope_kind="campaign_configuration", scope_fingerprint=scope,
        scorer=counted_scorer, deadline_check=deadline)

    assert scorer_calls == 2
    assert result.status == "partial"
    assert result.variants[0].status == "complete"
    assert result.variants[0].score_set is not None
    assert result.variants[1].reason_codes == ("shadow_scoring_deadline",)
    assert result.variants[1].score_set is None


def test_variant_failure_is_bounded_and_retains_prior_complete_evidence(monkeypatch):
    predictive, eligibility, inventory = _producer_bundle(monkeypatch)
    scope = _fp("campaign")
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("second variant failed")
        return score_scan_window_candidates(*args, **kwargs)

    result = score_shadow_inventory(
        inventory, predictive_artifact=predictive, eligibility_input=eligibility,
        policy_set=_policies(scope),
        scoring_config=ScanWindowShadowScoringConfig(
            simulation_count=8, block_count=2, max_runtime_variants=8),
        scope_kind="campaign_configuration", scope_fingerprint=scope,
        scorer=fail_second,
    )

    assert result.status == "partial"
    assert calls == 2
    by_id = {item.variant_id: item for item in result.variants}
    assert by_id["coarse"].status == "complete"
    assert by_id["coarse"].score_set is not None
    assert by_id["fine"].status == "unavailable"
    assert by_id["fine"].reason_codes == ("shadow_scoring_failed",)
    assert by_id["fine"].score_set is None
    assert by_id["fine"].target_core_minus_legacy is None
    assert by_id["fine"].score_refusal_evidence is None


def test_variant_deadline_retains_complete_evidence_and_marks_remaining(monkeypatch):
    predictive, eligibility, inventory = _producer_bundle(monkeypatch)
    scope = _fp("campaign")
    checks = 0

    class AnalysisLimitExceeded(Exception):
        pass

    def deadline():
        nonlocal checks
        checks += 1
        # Evaluations plus pre/post first-variant checks succeed. Refuse the second variant.
        if checks == 5:
            raise AnalysisLimitExceeded("soft allowance")

    result = score_shadow_inventory(
        inventory, predictive_artifact=predictive, eligibility_input=eligibility,
        policy_set=_policies(scope),
        scoring_config=ScanWindowShadowScoringConfig(
            simulation_count=8, block_count=2, max_runtime_variants=8),
        scope_kind="campaign_configuration", scope_fingerprint=scope,
        deadline_check=deadline,
    )

    assert result.status == "partial"
    by_id = {item.variant_id: item for item in result.variants}
    assert by_id["coarse"].status == "complete"
    assert by_id["fine"].reason_codes == ("shadow_scoring_deadline",)
    assert by_id["fine"].score_set is None


def test_strict_inventory_loader_checks_content_address_and_nested_evidence(
        monkeypatch, tmp_path):
    predictive, eligibility, inventory = _producer_bundle(monkeypatch)
    scope = _fp("campaign")
    scored = inventory.with_shadow_scoring(score_shadow_inventory(
        inventory, predictive_artifact=predictive, eligibility_input=eligibility,
        policy_set=_policies(scope),
        scoring_config=ScanWindowShadowScoringConfig(simulation_count=8, block_count=2),
        scope_kind="campaign_configuration", scope_fingerprint=scope,
    ))
    digest = scored.content_hash()
    path = tmp_path / "report" / "scan-design" / f"{digest}.json"
    path.parent.mkdir(parents=True)
    path.write_text(canonical_json(scored.to_full_dict()), encoding="utf-8")
    reference = {"kind": "scan_design_inventory", "sha256": digest,
                 "path": f"report/scan-design/{digest}.json"}
    assert load_scan_design_inventory(reference, tmp_path).content_hash() == digest

    tampered = scored.to_full_dict()
    tampered["shadow_scoring"]["variants"][0]["score_set"]["candidate_scores"][0][
        "conditional_target_qoi_information_nats"] += 1.0
    path.write_text(json.dumps(tampered, indent=2), encoding="utf-8")
    with pytest.raises(ValueError, match="address"):
        load_scan_design_inventory(reference, tmp_path)


@pytest.mark.parametrize(("section", "field", "value"), [
    ("scoring_config", "simulation_count", 4),
    ("policy_set", "contract", "isar.qoi_distinction_policy_set/tampered"),
])
def test_persisted_shadow_inputs_reauthenticate_frozen_content(
        monkeypatch, section, field, value):
    predictive, eligibility, inventory = _producer_bundle(monkeypatch)
    scope = _fp("campaign")
    scored = inventory.with_shadow_scoring(score_shadow_inventory(
        inventory, predictive_artifact=predictive, eligibility_input=eligibility,
        policy_set=_policies(scope),
        scoring_config=ScanWindowShadowScoringConfig(simulation_count=8, block_count=2),
        scope_kind="campaign_configuration", scope_fingerprint=scope,
    ))
    payload = scored.to_full_dict()
    payload["shadow_scoring"][section][field] = value

    with pytest.raises(ValueError):
        ScanDesignInventory.from_dict(payload)


def test_strict_validator_rejects_structurally_reminted_numerical_config_join(monkeypatch):
    predictive, eligibility, inventory = _producer_bundle(monkeypatch)
    scope = _fp("campaign")
    scored = inventory.with_shadow_scoring(score_shadow_inventory(
        inventory, predictive_artifact=predictive, eligibility_input=eligibility,
        policy_set=_policies(scope),
        scoring_config=ScanWindowShadowScoringConfig(simulation_count=8, block_count=2),
        scope_kind="campaign_configuration", scope_fingerprint=scope))
    changed = ScanWindowShadowScoringConfig(simulation_count=16, block_count=4)
    reminted_scoring = replace(
        scored.shadow_scoring, scoring_config=changed.to_dict(),
        scoring_config_fingerprint=changed.scoring_config_fingerprint)
    reminted = replace(scored, shadow_scoring=reminted_scoring)

    with pytest.raises(ValueError, match="scoring inputs"):
        validate_scan_design_inventory(reminted)


@pytest.mark.parametrize("mutation", ["energy_grid", "exposure", "source_draw"])
def test_complete_scores_reject_consistently_reminted_pair_and_design_tampering(
        monkeypatch, mutation):
    _inventory, scoring = _complete_scoring(monkeypatch)
    if mutation in {"energy_grid", "exposure"}:
        pair = scoring.to_dict()["candidate_pair"]
        target = pair["target_core"]
        if mutation == "energy_grid":
            target["energy_meV"][0] += 0.125
            target["grid_fingerprint"] = _fp("reminted-grid")
        else:
            target["exposure_per_point"] += 1
            target["nominal_cost"] = (
                target["exposure_per_point"] * target["executed_point_count"])
        pair["candidate_pair_fingerprint"] = semantics_fingerprint(
            "isar.scan_design_candidate_pair_fingerprint/o36.6",
            {key: pair[key] for key in ("contract", "legacy", "target_core")})
        seed = replace(
            scoring.seed_evidence,
            candidate_pair_fingerprint=pair["candidate_pair_fingerprint"])
        tampered = replace(scoring, candidate_pair=pair, seed_evidence=seed)
        expected = "candidate score"
    else:
        design = scoring.to_dict()["design_measure"]
        design["source_draw_identity_fingerprint"] = _fp("reminted-source-draw")
        tampered = replace(scoring, design_measure=design)
        expected = "source draws"

    with pytest.raises(ValueError, match=expected):
        validate_scan_design_shadow_scoring(tampered)


def test_candidate_pair_fingerprint_is_recomputed_without_inventory(monkeypatch):
    _inventory, scoring = _complete_scoring(monkeypatch)
    pair = scoring.to_dict()["candidate_pair"]
    pair["target_core"]["reason"] = "outer scoring identity was reminted"
    tampered = replace(scoring, candidate_pair=pair)

    with pytest.raises(ValueError, match="candidate-pair fingerprint"):
        validate_scan_design_shadow_scoring(tampered)


def test_strict_validator_rejects_reminted_refusal_candidate_join(monkeypatch):
    predictive, eligibility, inventory = _producer_bundle(monkeypatch)
    scope = _fp("campaign")
    evaluations = 0

    def unavailable_target(*args, **kwargs):
        nonlocal evaluations
        evaluations += 1
        result = evaluate_master_predictive(*args, **kwargs)
        return (replace(result, available=False, reason_code="missing_exposure_basis")
                if evaluations == 2 else result)

    scoring = score_shadow_inventory(
        inventory, predictive_artifact=predictive, eligibility_input=eligibility,
        policy_set=_policies(scope),
        scoring_config=ScanWindowShadowScoringConfig(simulation_count=8, block_count=2),
        scope_kind="campaign_configuration", scope_fingerprint=scope,
        predictive_evaluator=unavailable_target)
    variant = scoring.variants[0]
    assert variant.score_refusal_evidence.score_set_status == "partial"
    assert [
        (item["candidate_id"], item["candidate_family"], item["status"],
         tuple(item["reason_codes"]), item["source_reason_code"])
        for item in variant.score_refusal_evidence.candidate_refusals
    ] == sorted([
        (scoring.candidate_pair["legacy"]["candidate_id"], "legacy_uniform/1",
         "available", (), None),
        (scoring.candidate_pair["target_core"]["candidate_id"], "target_core/1",
         "unavailable", ("predictive_evaluation_unavailable",),
         "missing_exposure_basis"),
    ])
    refusal_type = type(variant.score_refusal_evidence)
    candidates = [dict(item) for item in variant.score_refusal_evidence.candidate_refusals]
    candidates[0]["candidate_id"] = "f" * 64
    refusal = refusal_type(
        score_set_status=variant.score_refusal_evidence.score_set_status,
        score_set_reason_codes=variant.score_refusal_evidence.score_set_reason_codes,
        candidate_refusals=tuple(candidates))
    variants = (replace(variant, score_refusal_evidence=refusal), *scoring.variants[1:])
    reminted = inventory.with_shadow_scoring(
        replace(scoring, variants=variants))

    with pytest.raises(ValueError, match="refusal candidates"):
        validate_scan_design_inventory(reminted)


def test_importing_eig_does_not_import_campaign_or_acquisition_modules():
    code = (
        "import sys; import isar.eig; "
        "bad=[name for name in sys.modules if name.startswith('isar.drive')]; "
        "assert not bad, bad"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_internal_unavailable_evidence_claims_no_source_identity():
    result = ScanDesignShadowScoring.unavailable_internal(
        diagnostics={"message": "synthetic failure"})
    second = ScanDesignShadowScoring.unavailable_internal(
        diagnostics={"message": "different prose"})
    payload = result.to_dict()
    assert result.status == "unavailable"
    assert payload["eligibility"] is None
    assert payload["candidate_pair"] is None
    assert second.shadow_scoring_fingerprint == result.shadow_scoring_fingerprint
    assert "synthetic failure" not in canonical_json({
        key: value for key, value in payload.items() if key != "diagnostics"})


@pytest.mark.parametrize("field", ["winner", "recommendation", "threshold"])
def test_shadow_evidence_recursively_rejects_authoritative_decision_fields(field):
    with pytest.raises(ValueError, match="authoritative decision field"):
        ScanDesignShadowScoring.unavailable_internal(
            diagnostics={"nested": {field: "target_core/1"}})


def test_historical_v2_inventory_remains_loadable_from_canonical_bytes(monkeypatch, tmp_path):
    _predictive, _eligibility, current = _producer_bundle(monkeypatch)
    # /2 predates Slice 6 scoring entirely and was only ever built under "off" — its expected basis
    # collapsed to LEGACY_CONTROL_OFF unconditionally once the "shadow" branch this used to
    # exercise was deleted (SCENE-4 PR 4.4, D3).
    historical = replace(
        current, inventory_version=SCAN_DESIGN_INVENTORY_SCHEMA_V2,
        shadow_scoring=None, selection_basis=SELECTION_BASIS_LEGACY_CONTROL_OFF)
    digest = historical.content_hash()
    path = tmp_path / "report" / "scan-design" / f"{digest}.json"
    path.parent.mkdir(parents=True)
    path.write_text(canonical_json(historical.to_full_dict()), encoding="utf-8")
    loaded = load_scan_design_inventory(
        {"kind": "scan_design_inventory", "sha256": digest,
         "path": f"report/scan-design/{digest}.json"}, tmp_path)
    assert loaded.inventory_version == SCAN_DESIGN_INVENTORY_SCHEMA_V2
    assert loaded.shadow_scoring is None

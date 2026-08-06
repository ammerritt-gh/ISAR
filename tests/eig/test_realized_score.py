"""Pure paired Monte Carlo scoring for Slice 7 realized requests."""
from __future__ import annotations

import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest

from isar.drive.acquisition.acquire import ScanProposal
from isar.drive.acquisition.realized_comparison import construct_realized_pair
from isar.eig.realized_score import score_realized_pair
from isar.eig.scorer import score_scan_window_candidates
from isar.eig.shadow_inventory import score_shadow_inventory
from isar.predictive import evaluate_master_predictive
from isar.schema.eig_policy import ScanWindowShadowScoringConfig
from isar.schema.eig_realized import REALIZED_SEED_DOMAIN


def _bundle(monkeypatch):
    helpers = runpy.run_path(str(Path(__file__).with_name("test_shadow_inventory.py")))
    predictive, eligibility, inventory = helpers["_producer_bundle"](monkeypatch)
    scope = helpers["_fp"]("campaign")
    policy_set = helpers["_policies"](scope)
    candidate = inventory.selected_candidate
    proposal = ScanProposal(
        h=candidate.hkl[0], k=candidate.hkl[1], l=candidate.hkl[2],
        e_lo=candidate.e_lo, e_hi=candidate.e_hi, e_step=candidate.e_step,
        ncount=candidate.ncount - 1, n_points=candidate.n_points,
        kind="production", reason="realized score", meta={"proposal_fingerprint": "a" * 64})

    def finalize(target, *_args, **_kwargs):
        target.meta["proposal_fingerprint"] = "b" * 64

    pair = construct_realized_pair(
        proposal, inventory, SimpleNamespace(), SimpleNamespace(), {},
        it=1, finalizer=finalize)
    return predictive, eligibility, pair, policy_set, scope


def test_realized_pair_is_scored_together_with_target_minus_legacy_orientation(monkeypatch):
    predictive, eligibility, pair, policy_set, scope = _bundle(monkeypatch)

    variants, seed = score_realized_pair(
        pair, predictive_artifact=predictive, eligibility_input=eligibility,
        policy_set=policy_set,
        scoring_config=ScanWindowShadowScoringConfig(simulation_count=8, block_count=2),
        scope_kind="campaign_configuration", scope_fingerprint=scope)

    assert seed.domain == REALIZED_SEED_DOMAIN
    assert seed.realized_pair_fingerprint == pair.realized_pair_fingerprint
    assert len(variants) == 2
    assert all(item.status == "complete" and item.score_disposition == "rescored"
               for item in variants)
    expected_ids = {
        pair.legacy.realized_request_fingerprint,
        pair.target_core.realized_request_fingerprint,
    }
    for variant in variants:
        scores = variant.score_set
        assert {item["candidate_id"] for item in scores["candidate_scores"]} == expected_ids
        comparison = scores["pairwise_comparisons"][0]
        sign = 1.0 if comparison["candidate_b_id"] == (
            pair.target_core.realized_request_fingerprint) else -1.0
        assert variant.target_core_minus_legacy["delta_information_nats"] == pytest.approx(
            sign * comparison["delta_information_nats"])


def test_process_interrupts_are_not_converted_to_unavailable_variants(monkeypatch):
    predictive, eligibility, pair, policy_set, scope = _bundle(monkeypatch)

    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        score_realized_pair(
            pair, predictive_artifact=predictive, eligibility_input=eligibility,
            policy_set=policy_set,
            scoring_config=ScanWindowShadowScoringConfig(simulation_count=8, block_count=2),
            scope_kind="campaign_configuration", scope_fingerprint=scope,
            scorer=interrupt)


def test_planned_and_realized_scoring_stages_remain_role_normalized(monkeypatch):
    helpers = runpy.run_path(str(Path(__file__).with_name("test_shadow_inventory.py")))
    predictive, eligibility, inventory = helpers["_producer_bundle"](monkeypatch)
    scope = helpers["_fp"]("campaign")
    policy_set = helpers["_policies"](scope)
    config = ScanWindowShadowScoringConfig(simulation_count=8, block_count=2)
    evaluation_calls = {"shadow": [], "realized": []}
    scorer_calls = {"shadow": [], "realized": []}

    def evaluator_for(stage):
        def evaluate(artifact, energies, ncount):
            evaluation_calls[stage].append((tuple(energies), int(ncount)))
            return evaluate_master_predictive(artifact, energies, ncount)
        return evaluate

    def scorer_for(stage):
        def score(measure, candidate_inputs, **kwargs):
            scorer_calls[stage].append({
                "design_measure_fingerprint": measure.design_measure_fingerprint,
                "candidate_roles": tuple(
                    (tuple(item.evaluation.energy_meV), item.evaluation.exposure_per_point)
                    for item in candidate_inputs),
                "partition": kwargs["qoi_discretization"].to_dict(),
                "simulation_count": kwargs["simulation_count"],
                "block_count": kwargs["block_count"],
                "seed": kwargs["seed"],
            })
            return score_scan_window_candidates(measure, candidate_inputs, **kwargs)
        return score

    shadow = score_shadow_inventory(
        inventory, predictive_artifact=predictive, eligibility_input=eligibility,
        policy_set=policy_set, scoring_config=config,
        scope_kind="campaign_configuration", scope_fingerprint=scope,
        predictive_evaluator=evaluator_for("shadow"), scorer=scorer_for("shadow"))
    inventory = inventory.with_shadow_scoring(shadow)
    candidate = inventory.selected_candidate
    proposal = ScanProposal(
        h=candidate.hkl[0], k=candidate.hkl[1], l=candidate.hkl[2],
        e_lo=candidate.e_lo, e_hi=candidate.e_hi, e_step=candidate.e_step,
        ncount=candidate.ncount, n_points=candidate.n_points,
        kind="production", reason="drift lock", meta={"proposal_fingerprint": "a" * 64})

    def finalize(target, *_args, **_kwargs):
        target.meta["proposal_fingerprint"] = "b" * 64

    pair = construct_realized_pair(
        proposal, inventory, SimpleNamespace(), SimpleNamespace(), {},
        it=1, finalizer=finalize)
    realized, realized_seed = score_realized_pair(
        pair, predictive_artifact=predictive, eligibility_input=eligibility,
        policy_set=policy_set, scoring_config=config,
        scope_kind="campaign_configuration", scope_fingerprint=scope,
        predictive_evaluator=evaluator_for("realized"), scorer=scorer_for("realized"))

    assert evaluation_calls["realized"] == evaluation_calls["shadow"]
    assert len(scorer_calls["realized"]) == len(scorer_calls["shadow"]) == 2
    for planned, exact in zip(scorer_calls["shadow"], scorer_calls["realized"], strict=True):
        assert {key: value for key, value in planned.items() if key != "seed"} == {
            key: value for key, value in exact.items() if key != "seed"}
        assert planned["seed"] != exact["seed"]
    legacy_id = pair.legacy.realized_request_fingerprint
    target_id = pair.target_core.realized_request_fingerprint
    for variant in realized:
        comparison = variant.score_set["pairwise_comparisons"][0]
        sign = 1.0 if comparison["candidate_b_id"] == target_id else -1.0
        assert {comparison["candidate_a_id"], comparison["candidate_b_id"]} == {
            legacy_id, target_id}
        assert variant.target_core_minus_legacy["delta_information_nats"] == pytest.approx(
            sign * comparison["delta_information_nats"])
    for variant in shadow.variants:
        comparison = variant.score_set["pairwise_comparisons"][0]
        legacy_id = shadow.candidate_pair["legacy"]["candidate_id"]
        target_id = shadow.candidate_pair["target_core"]["candidate_id"]
        sign = 1.0 if comparison["candidate_b_id"] == target_id else -1.0
        assert {comparison["candidate_a_id"], comparison["candidate_b_id"]} == {
            legacy_id, target_id}
        assert variant.target_core_minus_legacy["delta_information_nats"] == pytest.approx(
            sign * comparison["delta_information_nats"])
    assert realized_seed.domain == REALIZED_SEED_DOMAIN

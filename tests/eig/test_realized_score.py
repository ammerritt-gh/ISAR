"""Pure paired Monte Carlo scoring for Slice 7 realized requests."""
from __future__ import annotations

import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest

from isar.drive.acquisition.acquire import ScanProposal
from isar.drive.acquisition.realized_comparison import construct_realized_pair
from isar.eig.realized_score import score_realized_pair
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

"""The question-policy profile: precedence, authority postures, refusal (SCENE-4 PR 4.1).

The card lane is wired but EMPTY in production (no ``[questions]`` table exists yet), so the
precedence machinery is exercised with synthetic dicts — that is the contract until the owner
names the card keys.
"""
import json

import pytest

from isar.schema.question_policy import (
    AUTHORITY_CAPABILITY, AUTHORITY_IDENTITY, AUTHORITY_TARGET, QUESTION_POLICY_SCHEMA,
    resolve_policy, validate_resolved_block)


def test_pure_defaults_resolve_to_a_valid_block_with_default_provenance():
    block = resolve_policy()
    validate_resolved_block(block)
    assert block["schema"] == QUESTION_POLICY_SCHEMA
    assert all(record["source"] == "default" for record in block["provenance"].values())
    assert block["provenance"]["confirm_presence_min_mass"]["authority"] == AUTHORITY_TARGET
    assert block["provenance"]["emergent_omega_tile_meV"]["authority"] == AUTHORITY_IDENTITY
    assert (block["provenance"]["parameter_statistical_floor_meV"]["authority"]
            == AUTHORITY_CAPABILITY)
    assert block["staleness_policy"] == {"mode": "synchronous", "lag": 0}


def test_precedence_user_beats_card_beats_default():
    block = resolve_policy(user_ask={"confirm_presence_min_mass": 0.95},
                           card_guidelines={"confirm_presence_min_mass": 0.8,
                                            "classify_identity_min_mass": 0.85})
    assert block["thresholds"]["confirm_presence_min_mass"] == 0.95
    assert block["provenance"]["confirm_presence_min_mass"]["source"] == "user"
    assert block["thresholds"]["classify_identity_min_mass"] == 0.85
    assert block["provenance"]["classify_identity_min_mass"]["source"] == "card"
    assert block["provenance"]["connect_track_min_mass"]["source"] == "default"


@pytest.mark.parametrize("ask", [
    {"parameter_statistical_floor_meV": 0.05},        # lowering an uncertainty floor
    {"min_effective_exposure": 0.1},                  # lowering a detection floor
    {"maximum_justifiable_zc_confidence": 0.99},      # raising a justifiability ceiling
])
def test_a_user_ask_cannot_weaken_a_capability_limit(ask):
    with pytest.raises(ValueError, match="may be strengthened by asking, never weakened"):
        resolve_policy(user_ask=ask)


def test_a_user_ask_may_strengthen_a_capability_limit():
    block = resolve_policy(user_ask={"parameter_statistical_floor_meV": 0.4,
                                     "min_effective_exposure": 2.0})
    assert block["thresholds"]["parameter_statistical_floor_meV"] == 0.4
    assert block["thresholds"]["min_effective_exposure"] == 2.0
    assert block["provenance"]["min_effective_exposure"]["source"] == "user"


def test_an_unknown_field_refuses_instead_of_silently_resolving_defaults():
    with pytest.raises(ValueError, match="unknown question-policy fields"):
        resolve_policy(user_ask={"confirm_presense_min_mass": 0.9})   # the typo case


@pytest.mark.parametrize("mutate, message", [
    (lambda b: b["thresholds"].pop("mismatch_min_mass"), "missing frozen values"),
    (lambda b: b["thresholds"].update(mismatch_min_mass=1.5), "within"),
    (lambda b: b.update(emergent_question_policy="chase_everything"), "not supported"),
    (lambda b: b.update(staleness_policy={"mode": "fixed_lag", "lag": 2}), "not supported"),
    (lambda b: b["staleness_policy"].update(lag=1), "lag 0"),
    (lambda b: b["prompt_defaults"].update(unresolvable_closure="hold"), "not supported"),
    (lambda b: b["provenance"].pop("mismatch_min_mass"), "every threshold field"),
    (lambda b: b["provenance"]["mismatch_min_mass"].update(authority="capability"),
     "authority posture is the profile's"),
    (lambda b: b["priorities"].pop("VerifyCompleteness"), "exactly the families"),
    (lambda b: b.update(surprise=1), "unknown fields"),
])
def test_a_damaged_or_unsupported_frozen_block_refuses_by_name(mutate, message):
    block = json.loads(json.dumps(resolve_policy()))
    mutate(block)
    with pytest.raises(ValueError, match=message):
        validate_resolved_block(block)

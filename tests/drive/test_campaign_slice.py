"""The campaign slice key: which (sample, temperature) evidence an action belongs to (PR2.4).

Belief evidence is stored per ``(sample, t_bin)``. The controller used to pick ``max(complete)`` — the
newest temperature slice with both branches present — whenever more than one qualified. That is a
guess, and a silent one: a campaign measuring at 100 K would happily classify against 200 K beliefs
because a stale slice happened to sort higher, and nothing in the run record would say so.

The rule here is: use the temperature the campaign explicitly declared; failing that, use the single
complete slice if there is exactly one; and if the evidence is genuinely ambiguous, **fail closed**.

Failing closed does not mean crashing an unattended overnight run. An unresolved slice makes the
generators unrunnable, so they report as failed and the inventory becomes *incomplete* — which the
terminal already renders as a resumable ``controller_fault`` (PR1.7b). The campaign stops with its
checkpoints intact and a structured reason, rather than dying or silently guessing.

The key also travels on every ``MeasurementIntent``, so two actions asking the same question of
different temperature slices are different scientific actions and cannot share a fingerprint.
"""
from __future__ import annotations

import pytest

from isar.drive import loop as loop_mod
from isar.drive.acquisition import acquire as acq
from isar.drive.acquisition.inventory import CampaignSliceKey, MeasurementIntent
from isar.drive.objective import ObjectiveSpec, shipped_hypothesis_snapshot


def _objective(target_temperature_K=None):
    frozen = shipped_hypothesis_snapshot("optic", (-2.0, 12.0))
    return ObjectiveSpec.segment(
        q_start=(2.0, 0.0, 0.0), q_end=(2.0, 0.1, 0.0), step_rlu=0.1,
        tolerance_meV=0.5, budget={"kind": "neutrons", "amount": 1e12},
        target_branch="optic", energy_search_meV=(-2.0, 12.0),
        target_temperature_K=target_temperature_K, **frozen)


def _row(sample, temperature, branch, objective):
    return {"sample": sample, "t_bin": temperature, "branch_id": branch,
            "anchors": [{"u": u, "omega0": 3.0, "weight": 1.0} for u in (0.0, 0.1, 0.2)],
            "n_anchors": 3, "period": None,
            "parameter_prior": next(h for h in objective.hypotheses if h["name"] == branch)}


def _state(objective, temperatures, sample="active"):
    rows = []
    for temperature in temperatures:
        rows.append(_row(sample, temperature, "acoustic", objective))
        rows.append(_row(sample, temperature, "optic", objective))
    return {"branch_belief_models": rows}


# -- resolving the slice ---------------------------------------------------------------------------

def test_one_complete_slice_resolves_without_a_declaration():
    objective = _objective()
    key, error = loop_mod.resolve_campaign_slice(
        _state(objective, [200.0]), objective, sample="active")
    assert error is None
    assert key.sample == "active" and key.temperature_bin == pytest.approx(200.0)


def test_an_explicit_campaign_temperature_wins_over_several_complete_slices():
    """The declaration is the answer, not a tie-break: with it there is nothing to guess about."""
    objective = _objective(target_temperature_K=100.0)
    key, error = loop_mod.resolve_campaign_slice(
        _state(objective, [100.0, 200.0]), objective, sample="active")
    assert error is None
    assert key.temperature_bin == pytest.approx(100.0)


def test_several_complete_slices_without_a_declaration_fail_closed():
    """The behaviour this increment removes. ``max(complete)`` silently classified a 100 K campaign
    against 200 K beliefs whenever a newer complete slice existed."""
    objective = _objective()
    key, error = loop_mod.resolve_campaign_slice(
        _state(objective, [100.0, 200.0]), objective, sample="active")
    assert key is None
    assert error is not None
    assert "100" in error and "200" in error       # the reason names the slices it refused to choose


def test_a_declared_temperature_with_no_matching_slice_fails_closed():
    """Declaring 300 K and silently classifying against the 200 K slice would be the same bug wearing
    a declaration."""
    objective = _objective(target_temperature_K=300.0)
    key, error = loop_mod.resolve_campaign_slice(
        _state(objective, [100.0, 200.0]), objective, sample="active")
    assert key is None
    assert error is not None and "300" in error


def test_no_complete_slice_is_a_cold_start_not_a_failure():
    """A campaign with no measured beliefs yet runs on the common prior. That is the ordinary opening
    state of every campaign and must never be confused with ambiguous evidence."""
    objective = _objective()
    key, error = loop_mod.resolve_campaign_slice(
        {"branch_belief_models": []}, objective, sample="active")
    assert error is None
    assert key.sample == "active" and key.temperature_bin is None


def test_slices_for_other_samples_are_not_candidates():
    objective = _objective()
    state = _state(objective, [100.0], sample="other")
    key, error = loop_mod.resolve_campaign_slice(state, objective, sample="active")
    assert error is None and key.temperature_bin is None


# -- the key's fingerprint -------------------------------------------------------------------------

def test_the_fingerprint_separates_sample_temperature_and_binning_policy():
    base = CampaignSliceKey(sample="A", temperature_bin=200.0, binning_policy_fingerprint="rule-1")
    assert base.fingerprint == CampaignSliceKey(
        sample="A", temperature_bin=200.0, binning_policy_fingerprint="rule-1").fingerprint
    assert base.fingerprint != CampaignSliceKey(
        sample="B", temperature_bin=200.0, binning_policy_fingerprint="rule-1").fingerprint
    assert base.fingerprint != CampaignSliceKey(
        sample="A", temperature_bin=100.0, binning_policy_fingerprint="rule-1").fingerprint
    # A changed binning rule can silently reroute evidence into a different bin, so it is part of the
    # identity rather than an invisible input to it.
    assert base.fingerprint != CampaignSliceKey(
        sample="A", temperature_bin=200.0, binning_policy_fingerprint="rule-2").fingerprint


def test_the_cold_start_slice_is_distinct_from_a_zero_kelvin_bin():
    """``None`` means "no measured slice yet, running on the common prior". A 0 K bin would be real
    measured evidence. Collapsing them would let a cold start inherit a measured slice's identity."""
    cold_start = CampaignSliceKey(sample="A", temperature_bin=None)
    zero_kelvin = CampaignSliceKey(sample="A", temperature_bin=0.0)
    assert cold_start.fingerprint != zero_kelvin.fingerprint


def test_the_binning_fingerprint_covers_the_rules_that_actually_decide_the_bin(tmp_path):
    """The bin is ``round(T / t_round) * t_round`` with ``t_round`` from ``collation``, while
    ``temperature_rule`` decides which field T is read from. Hashing only one of them would let the
    other be changed silently — evidence regrouped into different slices, fingerprint unmoved."""
    import re
    from pathlib import Path

    # IN8's card declares both blocks concretely: collation.t_round = 5.0 and a temperature_rule
    # naming the setpoint parameter and sensor columns.
    shipped = Path("isar/parsers/cards/in8.policy.toml").read_text(encoding="utf-8")
    assert "t_round" in shipped and "setpoint_param" in shipped

    def fingerprint(t_round, setpoint_param):
        workspace = tmp_path / f"ws-{t_round}-{setpoint_param}"
        workspace.mkdir(parents=True, exist_ok=True)
        policy = re.sub(r"(?m)^t_round\s*=.*$", f"t_round = {t_round}", shipped)
        policy = re.sub(r"(?m)^setpoint_param\s*=.*$", f'setpoint_param = "{setpoint_param}"',
                        policy)
        (workspace / "card.policy.toml").write_text(policy, encoding="utf-8")
        (workspace / "card.state.json").write_text("{}", encoding="utf-8")
        return loop_mod._temperature_binning_fingerprint(workspace)

    base = fingerprint(5.0, "RT")
    assert base is not None
    assert fingerprint(5.0, "RT") == base
    assert fingerprint(1.0, "RT") != base            # different binning granularity
    assert fingerprint(5.0, "TSET") != base          # different temperature source


def test_two_slices_asking_the_same_question_are_different_scientific_actions():
    def intent(slice_fingerprint):
        return MeasurementIntent(
            action_class="production", hkl=(2.1, 0.0, 0.0), energy_lo=-2.0, energy_hi=7.0,
            window_family="target", campaign_slice_fingerprint=slice_fingerprint)

    hot = CampaignSliceKey(sample="A", temperature_bin=300.0)
    cold = CampaignSliceKey(sample="A", temperature_bin=100.0)
    assert intent(hot.fingerprint).scientific_action_fingerprint != \
        intent(cold.fingerprint).scientific_action_fingerprint


# -- the key reaches the intents --------------------------------------------------------------------


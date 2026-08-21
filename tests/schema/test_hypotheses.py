import pytest

from isar.schema.hypotheses import (
    SteeringPolicy, content_fingerprint, preset, resolve_hypothesis,
)


def test_optic_preset_freezes_search_band_derived_gap():
    optic = preset("optic", (-2.0, 12.0))
    assert optic.zc_energy.mean == pytest.approx(6.0)
    assert optic.zc_energy.sigma == pytest.approx(3.0)


def test_hypothesis_is_weight_not_force_and_energy_is_nonnegative():
    h = resolve_hypothesis("optic", {"zc_energy": {"mode": 5.0, "sigma": 2.0},
                                     "confidence": 0.25})
    assert h.confidence == 0.25
    with pytest.raises(ValueError, match="non-negative"):
        resolve_hypothesis("optic", {"zc_energy": {"mode": -1.0, "sigma": 2.0}})


def test_null_is_explicit_and_differs_from_absence():
    assert preset("null").name == "null"
    assert preset("null").confidence == 0.0


def test_unimplemented_dispersion_mechanics_are_rejected():
    with pytest.raises(ValueError, match="declared but not implemented"):
        resolve_hypothesis("cef", {"dispersion_type": "level"})


def test_fingerprint_changes_with_preset_or_estimator_inputs():
    h = [preset("optic").to_dict()]
    a = content_fingerprint(h, SteeringPolicy().to_dict())
    changed = content_fingerprint(h, SteeringPolicy(classification_weight=0.5).to_dict())
    assert a != changed
    assert len(a) == 64


def test_quadrature_order_is_committed_to_gh32():
    with pytest.raises(ValueError, match="GH32"):
        SteeringPolicy(quadrature_order=16)


@pytest.mark.parametrize("field", [
    "classification_weight", "assignment_entropy_stop_nats", "minimum_information_per_cost",
    "absent_signal_widen_step", "mismatch_z_threshold", "ncount_growth_cap", "tolerance_margin",
    "interval_floor_min_improvement", "interval_floor_min_exposure_ratio",
])
def test_steering_policy_rejects_non_finite_numeric_values(field):
    with pytest.raises(ValueError, match="finite"):
        SteeringPolicy(**{field: float("nan")})


@pytest.mark.parametrize("kwargs", [
    {"absent_signal_widen_step": 0.0}, {"absent_signal_widen_steps": 0},
    {"dynamic_ncount": "false"},
])
def test_steering_policy_rejects_invalid_widening_and_boolean(kwargs):
    with pytest.raises(ValueError):
        SteeringPolicy(**kwargs)


def test_bridge_policy_defaults_to_one_step_and_rejects_negative_values():
    assert SteeringPolicy().maximum_bridge_steps == 1
    with pytest.raises(ValueError, match="maximum_bridge_steps"):
        SteeringPolicy(maximum_bridge_steps=-1)


def test_shadow_scan_window_eig_mode_refuses_by_name_with_a_recompile_message():
    """SCENE-4 PR 4.4, D3: "shadow" is retired — a distinct branch names it and directs
    recompilation, rather than falling through to the generic "must be one of" enum message."""
    with pytest.raises(ValueError, match="retired in SCENE-4 PR 4.4"):
        SteeringPolicy(scan_window_eig_mode="shadow")


def test_unknown_scan_window_eig_mode_gets_the_generic_enum_message_not_the_named_one():
    with pytest.raises(ValueError, match="must be one of") as raised:
        SteeringPolicy(scan_window_eig_mode="bogus")
    assert "retired" not in str(raised.value)

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

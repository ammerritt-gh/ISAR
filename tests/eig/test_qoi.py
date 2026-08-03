"""Failure-closed behavior of the current omega0 QoI adapter."""
from __future__ import annotations

from isar.eig import adapt_omega0_target_projection


def test_missing_predictive_and_projection_records_are_explicit_gaps():
    evidence = adapt_omega0_target_projection(None, None, expected_authority={})
    assert evidence.status == "missing"
    assert evidence.observability_codes == ("predictive_record_missing",)


def test_malformed_predictive_record_is_never_interpreted_as_mass():
    evidence = adapt_omega0_target_projection(
        {"schema": "isar.master_predictive/2"}, None, expected_authority={}
    )
    assert evidence.status == "invalid"
    assert evidence.observability_codes == ("predictive_record_invalid",)
    assert not evidence.scientific_codes


def test_unrecognised_projection_cannot_be_treated_as_target_absence():
    evidence = adapt_omega0_target_projection(
        {"schema": "not-predictive"}, {"schema": "isar.target_projection/2"},
        expected_authority={},
    )
    assert evidence.status == "invalid"
    assert evidence.observability_codes == ("predictive_record_invalid",)

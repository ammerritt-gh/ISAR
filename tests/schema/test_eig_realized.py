"""Field-level validation of the realized-request contract."""
from __future__ import annotations

from dataclasses import replace

import pytest

from isar.schema.eig_realized import RealizedScanRequest


def _request(**overrides) -> RealizedScanRequest:
    values = {
        "source_candidate_id": "candidate",
        "candidate_family": "legacy_uniform/1",
        "source_grid_fingerprint": "a" * 64,
        "validation_status": "submitted_legacy",
        "hkl": (2.1, 0.0, 0.0),
        "e_lo": 0.0,
        "e_hi": 1.0,
        "e_step": 1.0,
        "scan_command": "deltaE 0 1 1",
        "grid_adapter": {"adapter": "tavi/1"},
        "executed_energies": (0.0,),
        "grid_fingerprint": "b" * 64,
        "executed_point_count": 1,
        "ncount": 1,
        "point_neutron_cost": 1,
        "request_fingerprint": "c" * 64,
    }
    values.update(overrides)
    return RealizedScanRequest(**values)


def test_a_valid_single_point_request_is_accepted():
    assert _request().executed_point_count == 1


@pytest.mark.parametrize(
    "field", ["executed_point_count", "ncount", "point_neutron_cost"])
def test_booleans_are_not_accepted_as_integer_counts(field):
    # ``True == 1`` satisfies every arithmetic check on a one-point grid, but it serialises as
    # ``true`` rather than ``1`` — so a bool would give two semantically identical records
    # different content addresses.
    with pytest.raises(ValueError, match="realized request is invalid"):
        _request(**{field: True})


def test_a_non_integer_point_cost_is_refused():
    with pytest.raises(ValueError, match="realized request is invalid"):
        _request(point_neutron_cost=1.0)


def test_the_point_cost_must_be_the_product_of_exposure_and_points():
    with pytest.raises(ValueError, match="realized request is invalid"):
        replace(_request(), point_neutron_cost=2)

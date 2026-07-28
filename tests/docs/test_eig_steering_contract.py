"""Integrity checks for the planned arbitrary-grid predictive contract fixture."""
from __future__ import annotations

import json
import math
from pathlib import Path


_FIXTURE = Path(__file__).parents[1] / "fixtures" / "eig_predictive_v2.json"


def _walk_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from _walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_keys(child)


def test_predictive_v2_fixture_has_the_locked_contract_shape():
    fixture = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert fixture["fixture_schema"] == "isar.eig_predictive_fixture/1"
    contract = fixture["artifact_contract"]
    assert contract["schema"] == "isar.master_predictive/2"
    assert contract["energy_meV"]
    assert set(contract["evaluation_context"]) == {
        "center_meV",
        "gaussian_fwhm_meV",
        "lorentzian_fwhm_meV",
        "temperature_K",
        "quasielastic_fwhm_meV",
    }
    assert set(contract["exposure_basis"]) == {
        "unit",
        "fit_reference_exposure",
        "reference_incremental_exposure",
        "n_members",
    }
    exposure = contract["exposure_basis"]
    assert exposure["reference_incremental_exposure"] == (
        exposure["fit_reference_exposure"] / exposure["n_members"])
    assert contract["seed"] == 940301
    assert contract["uncertainty_provenance"]["deterministic"] is True

    requests = {request["name"]: request for request in fixture["evaluation_requests"]}
    assert set(requests) == {"original", "shifted", "narrower", "coarser"}
    assert requests["original"]["energy_meV"] == contract["energy_meV"]
    assert requests["original"]["exposure_per_point"] == (
        exposure["reference_incremental_exposure"])
    assert requests["shifted"]["energy_meV"] != contract["energy_meV"]
    assert len(requests["narrower"]["energy_meV"]) < len(contract["energy_meV"])
    assert len(requests["coarser"]["energy_meV"]) < len(contract["energy_meV"])


def test_predictive_v2_fixture_matrices_are_complete_finite_and_nonnegative():
    fixture = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    requests = {request["name"]: request for request in fixture["evaluation_requests"]}
    cases = {case["name"]: case for case in fixture["cases"]}
    assert set(cases) == {
        "phonon_background",
        "null_background",
        "multimode_background",
    }

    for case in cases.values():
        assert case["models"]
        assert math.isclose(
            sum(float(model["weight"]) for model in case["models"]), 1.0)
        for model in case["models"]:
            names = model["parameter_names"]
            draws = model["parameter_draws"]
            assert draws and all(len(draw) == len(names) for draw in draws)
            manifest = model["model_manifest"]
            assert manifest["schema"] == "isar.model_manifest/1"
            assert manifest["components"]
            assert len(model["expected_counts"]) == len(draws)
            assert model["expected_counts"] == case["expected_counts_by_request"]["original"]
            for request_name, request in requests.items():
                matrix = case["expected_counts_by_request"][request_name]
                assert len(matrix) == len(draws)
                assert all(len(row) == len(request["energy_meV"]) for row in matrix)
                assert all(
                    math.isfinite(float(value)) and float(value) >= 0.0
                    for row in matrix for value in row
                )

    assert cases["null_background"]["models"][0]["omega0_draws"] == []
    assert len(cases["multimode_background"]["models"][0][
        "model_manifest"]["components"]) == 3


def test_predictive_v2_contract_contains_no_instrument_truth_or_selection_fields():
    fixture = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    keys = {key.lower() for key in _walk_keys(fixture)}
    assert keys.isdisjoint({
        "tavi_preset",
        "background_truth",
        "instrument_rate",
        "rate_assumption",
        "candidate_utility",
        "selected_candidate",
    })

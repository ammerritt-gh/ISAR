"""Integrity checks for the normative arbitrary-grid predictive contract fixture.

Structure only: that the fixture states the EIG-0-amended ``isar.master_predictive/2`` contract and
carries matrices an evaluator can be held to. The evaluator is held to them in
``tests/session/test_master_predictive.py``.
"""
from __future__ import annotations

import json
import math
from pathlib import Path


_FIXTURE = Path(__file__).parents[1] / "fixtures" / "eig_predictive_v2.json"

_EXPOSURE_BASIS_FIELDS = {
    "unit",
    "request_unit",
    "request_to_exposure_factor",
    "fit_reference_exposure",
    "n_members",
    "reference_incremental_exposure",
    "per_energy_monitor",
    "stored_exposure_ratio",
    "scalar_exposure_eligible",
    "exposure_homogeneous",
    "full_member_support",
    "exposure_ratio_span",
    "kept_point_count",
}

_COMPLETENESS_FIELDS = {
    "source_model_weight_total",
    "retained_model_weight_total",
    "pruned_model_weight",
    "dropped_models",
    "model_weight_floor",
    "max_pruned_mass",
    "max_dropped_draw_mass",
    "predictive_complete_for_eig",
}


def _load():
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _walk_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from _walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_keys(child)


def test_predictive_v2_fixture_has_the_locked_contract_shape():
    fixture = _load()
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
    assert set(contract["exposure_basis"]) == _EXPOSURE_BASIS_FIELDS
    exposure = contract["exposure_basis"]
    # ADR-017: the exposure unit and the request unit are declared separately even where the map is
    # the identity, so an instrument whose monitor is not its request needs no schema change.
    assert exposure["unit"] == "monitor_counts"
    assert exposure["request_unit"] == "number_neutrons"
    assert exposure["request_to_exposure_factor"] == 1.0
    assert math.isfinite(float(exposure["fit_reference_exposure"]))
    assert float(exposure["fit_reference_exposure"]) > 0.0
    assert isinstance(exposure["n_members"], int) and exposure["n_members"] > 0
    # A MEAN over members, never a per-scan exposure (EIG-0 §5).
    assert exposure["reference_incremental_exposure"] == (
        exposure["fit_reference_exposure"] / exposure["n_members"])
    assert len(exposure["per_energy_monitor"]) == len(contract["energy_meV"])
    assert len(exposure["stored_exposure_ratio"]) == len(contract["energy_meV"])
    assert exposure["kept_point_count"] == len(contract["energy_meV"])
    assert exposure["exposure_homogeneous"] is True
    assert exposure["scalar_exposure_eligible"] is True
    assert exposure["exposure_ratio_span"] == 1.0
    assert set(contract["completeness"]) == _COMPLETENESS_FIELDS
    assert contract["completeness"]["predictive_complete_for_eig"] is True
    assert contract["completeness"]["model_weight_floor"] == 0.005
    assert contract["completeness"]["max_pruned_mass"] == 0.01
    assert contract["completeness"]["max_dropped_draw_mass"] == 0.01
    # ADR-019: /2 is PHYSICAL ONLY. The target projection is a separate, authority-bound artifact
    # resolved after branch triage and the connected tracker, so a /2 that still carried one would
    # be asserting a target the fit could not yet know.
    assert "target_projection" not in contract
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
    requested_exposures = {
        float(request["exposure_per_point"]) for request in requests.values()}
    assert len(requested_exposures) == len(requests)
    assert all(math.isfinite(value) and value > 0.0 for value in requested_exposures)


def test_predictive_v2_fixture_states_every_refusal_it_covers():
    unavailable = _load()["unavailable_contract"]
    assert {entry["reason_code"] for entry in unavailable.values()} == {
        "missing_exposure_basis", "artifact_schema_unsupported", "predictive_basis_incomplete"}
    assert all(entry["available"] is False and entry["message"]
               for entry in unavailable.values())


def test_predictive_v2_fixture_matrices_are_complete_finite_and_nonnegative():
    fixture = _load()
    requests = {request["name"]: request for request in fixture["evaluation_requests"]}
    cases = {case["name"]: case for case in fixture["cases"]}
    assert set(cases) == {
        "null_vs_one_phonon",
        "one_mode_vs_two_mode",
        "branch_hypotheses",
    }

    for case in cases.values():
        assert case["models"]
        assert case["model_order"] == [model["label"] for model in case["models"]]
        assert math.isclose(
            sum(float(model["weight"]) for model in case["models"]), 1.0)
        assert len({float(model["weight"]) for model in case["models"]}) > 1
        for model in case["models"]:
            names = model["parameter_names"]
            draws = model["parameter_draws"]
            assert draws and all(len(draw) == len(names) for draw in draws)
            assert model["draw_ids"] == [
                f"draw-{index}" for index in range(len(draws))]
            # ADR-014: the draw measure is declared, never inferred.
            assert model["draw_weighting"] == "equal_weight_resampled"
            assert model["draw_weights"] is None
            # ADR-009: the identity seam defaults to the label and earns no reward.
            assert model["identity_label"] == model["label"]
            # ADR-019: no target state inside /2 — only the per-draw physical menu of all fitted
            # mode energies, which a later projection selects the target from without a refit.
            assert "target_present_by_draw" not in model
            assert "target_qoi_draws" not in model
            assert len(model["mode_energies"]) == len(draws)
            assert all(all(math.isfinite(float(value)) for value in row)
                       for row in model["mode_energies"])
            manifest = model["model_manifest"]
            assert manifest["schema"] == "isar.model_manifest/1"
            assert manifest["components"]
            assert set(model["expected_counts_by_request"]) == set(requests)
            for request_name, request in requests.items():
                matrix = model["expected_counts_by_request"][request_name]
                assert len(matrix) == len(draws)
                assert all(len(row) == len(request["energy_meV"]) for row in matrix)
                assert all(
                    math.isfinite(float(value)) and float(value) >= 0.0
                    for row in matrix for value in row
                )
        for request_name in requests:
            matrices = [
                model["expected_counts_by_request"][request_name]
                for model in case["models"]
            ]
            assert matrices[0] != matrices[1], (
                f"{case['name']} must retain model-discriminating predictions "
                f"for {request_name}")

    null_model = cases["null_vs_one_phonon"]["models"][0]
    assert null_model["omega0_draws"] == []
    # A no-phonon model has no modes at all, so no projection can ever select a target from it.
    assert null_model["mode_energies"] == [[], []]
    fit_reference = fixture["artifact_contract"]["exposure_basis"][
        "fit_reference_exposure"]
    for request_name, request in requests.items():
        ratio = float(request["exposure_per_point"]) / float(fit_reference)
        for draw, row in zip(null_model["parameter_draws"],
                             null_model["expected_counts_by_request"][request_name]):
            assert all(math.isclose(value, draw[0] * ratio) for value in row)
    assert len(cases["one_mode_vs_two_mode"]["models"][1][
        "model_manifest"]["components"]) == 3


def test_every_fixture_case_carries_a_reproducing_basis_fingerprint():
    # ADR-020: every reader recomputes the stored fingerprint and refuses a mismatch, so a fixture
    # that cannot reproduce its own fingerprint would be unusable by the evaluator it normalises.
    from isar.predictive import predictive_basis_fingerprint

    fixture = _load()
    contract = fixture["artifact_contract"]
    for case in fixture["cases"]:
        assert case["predictive_basis_fingerprint"] == predictive_basis_fingerprint(
            models=case["models"], energy_meV=contract["energy_meV"],
            evaluation_context=contract["evaluation_context"],
            exposure_basis=contract["exposure_basis"])
    pool = fixture["heterogeneous_pool"]
    pool_contract = pool["artifact_contract"]
    assert pool["predictive_basis_fingerprint"] == predictive_basis_fingerprint(
        models=pool["models"], energy_meV=pool_contract["energy_meV"],
        evaluation_context=pool_contract["evaluation_context"],
        exposure_basis=pool_contract["exposure_basis"])


def test_predictive_v2_fixture_pins_model_draw_and_parameter_order():
    fixture = _load()
    cases = {case["name"]: case for case in fixture["cases"]}
    assert cases["null_vs_one_phonon"]["model_order"] == [
        "null+background", "one-phonon+background"]
    assert cases["one_mode_vs_two_mode"]["model_order"] == [
        "one-phonon+background", "two-phonon+background"]
    assert cases["branch_hypotheses"]["model_order"] == [
        "acoustic-branch+background", "optic-branch+background"]
    assert cases["one_mode_vs_two_mode"]["models"][1]["parameter_names"] == [
        "phonon.Omega_d",
        "phonon.Gamma",
        "phonon.A",
        "phonon2.Omega_d",
        "phonon2.Gamma",
        "phonon2.A",
        "bg.BG",
    ]


def test_heterogeneous_support_pool_would_expose_a_scalar_vector_confusion():
    # EIG-0 c5: the per-bin summed monitor alternates, so the stored fitted-grid matrices are
    # reproduced only by the per-energy exposure vector. Without this pool the fixture set cannot
    # detect a scalar/vector confusion at all (EIG-0 §8.7).
    pool = _load()["heterogeneous_pool"]
    contract = pool["artifact_contract"]
    exposure = contract["exposure_basis"]
    assert set(exposure) == _EXPOSURE_BASIS_FIELDS
    assert "target_projection" not in contract
    assert exposure["exposure_homogeneous"] is False
    assert exposure["scalar_exposure_eligible"] is False
    assert exposure["exposure_ratio_span"] == 2.0
    assert len(set(exposure["per_energy_monitor"])) == 2
    reference = float(exposure["fit_reference_exposure"])
    members = int(exposure["n_members"])
    assert exposure["stored_exposure_ratio"] == [
        value / reference / members for value in exposure["per_energy_monitor"]]

    requests = {request["name"]: request for request in pool["evaluation_requests"]}
    assert requests["original"]["energy_meV"] == contract["energy_meV"]
    assert float(requests["original"]["exposure_per_point"]) == (
        exposure["reference_incremental_exposure"])
    assert pool["model_order"] == [model["label"] for model in pool["models"]]
    for model in pool["models"]:
        stored = model["stored_expected_counts"]
        scalar = model["expected_counts_by_request"]["original"]
        assert len(stored) == len(model["parameter_draws"])
        assert all(len(row) == len(contract["energy_meV"]) for row in stored)
        # Same grid, same draws, exposure requested at exactly the scalar reference: the two
        # matrices must still differ, which is the whole point of this pool.
        assert stored != scalar
        for stored_row, scalar_row in zip(stored, scalar):
            assert any(not math.isclose(left, right)
                       for left, right in zip(stored_row, scalar_row))


def test_predictive_v2_contract_contains_no_instrument_truth_or_selection_fields():
    fixture = _load()
    keys = {key.lower() for key in _walk_keys(fixture)}
    assert keys.isdisjoint({
        "tavi_preset",
        "background_truth",
        "instrument_rate",
        "rate_assumption",
        "candidate_utility",
        "selected_candidate",
    })

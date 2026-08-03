"""Failure-closed behavior of the current omega0 QoI adapter."""
from __future__ import annotations

from copy import deepcopy
import json
from types import SimpleNamespace

import numpy as np
import pytest

from isar.eig import adapt_omega0_target_projection
from isar.predictive import predictive_basis_fingerprint
from isar.schema.statistics import semantics_fingerprint
from isar.session.predictive_artifact import build_target_projection


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


def _authenticated_projection(monkeypatch, *, explicit_weights: bool = False):
    """Produce a real current `/2` artifact and its authority-bound projection."""
    from isar.analyze.context import ScanContext
    from isar.schema.statistics import (
        FIT_SELECTION_SCHEMA,
        authority_scopes,
        semantics_fingerprint,
        validate_for_write,
    )
    from isar.session import driver
    from isar.session import predictive_artifact

    manifest = {
        "schema": "isar.model_manifest/1",
        "components": [{"type": "phonon_dho", "prefix": "phonon", "peak": "stokes+astokes"},
                       {"type": "constant_background", "prefix": "bg"}],
        "parameters": {"phonon.Omega_d": 3.0, "phonon.Gamma": 0.3,
                       "phonon.A": 100.0, "bg.BG": 4.0},
    }
    names = ("phonon.Omega_d", "phonon.Gamma", "phonon.A", "bg.BG")
    draws = np.asarray([[3.0, 0.3, 100.0, 4.0], [3.1, 0.35, 110.0, 5.0]])
    monkeypatch.setattr(predictive_artifact, "model_manifest_of", lambda result, by_hyp: manifest)
    monkeypatch.setattr(predictive_artifact, "bounded_parameter_draws", lambda result, **kwargs: (
        names, draws, "parametric_bootstrap", {
            "draw_weighting": "equal_weight_resampled", "draw_weights": None,
            "draw_ids": ["draw-0", "draw-1"], "source_draw_count": 2,
            "retained_draw_count": 2, "dropped_draws": [], "draw_mass_retained": 1.0,
            "draw_cap": 64, "thinned_from_draw_count": 2,
        }))
    context = ScanContext(
        x=np.linspace(-2.0, 6.0, 5), counts=np.zeros(5), mask=np.ones(5, bool), T=100.0,
        center=0.0, gauss_fwhm=0.4, lorentz_fwhm=0.1, instrument="IN8",
        energy_status="absolute", exposure_scale=np.ones(5), exposure_reference=1.0)
    data = semantics_fingerprint("tests.eig.qoi.data", {"counts": [2.0, 3.0]})
    model = semantics_fingerprint("tests.eig.qoi.model", {"label": "one"})
    constraints = semantics_fingerprint("tests.eig.qoi.constraints", {"constraints": []})
    comparison = semantics_fingerprint("tests.eig.qoi.comparison", {
        "data_likelihood_fingerprint": data,
        "model_parameterization_fingerprint": model,
        "constraint_fingerprint": constraints,
        "estimator_kind": "data_likelihood_mle",
    })
    fit_semantics = validate_for_write({
        "schema": FIT_SELECTION_SCHEMA, "estimator_kind": "data_likelihood_mle",
        "fit_kind": "unconstrained", "effective_constraints": [],
        "aggregate_constraint_origin": "none", "selection_eligibility": "eligible",
        "selection_ineligibility_reasons": [], "data_likelihood_fingerprint": data,
        "model_parameterization_fingerprint": model, "constraint_fingerprint": constraints,
        "comparison_context_fingerprint": comparison, "conditioning_fingerprint": comparison,
        "authority_scopes": authority_scopes(single_fit_delivery="active"),
    }, FIT_SELECTION_SCHEMA)
    result = SimpleNamespace(label="one", omega0=3.0, fit_semantics=fit_semantics,
                             within_model={"omega0_samples": [3.0, 3.1]})
    score = SimpleNamespace(label="one", cost=10.0, k=2, aic=14.0, weight=1.0,
                            survives=True, fit_semantics=fit_semantics)
    selection = driver._build_selection_semantics(
        [result], SimpleNamespace(scores=[score], pooled=object()),
        {"route": "full_two_stage", "labels_before_guideline": ["one"],
         "guideline_structure_filter": ["one"], "offered_labels": ["one"],
         "scored_labels": ["one"]}, error_attachment_labels=["one"])
    artifact = predictive_artifact.build_master_predictive(
        SimpleNamespace(omega0=3.0), [score], {"one": result}, {"one": object()}, context,
        seed=11, n_members=1, fit_selection_semantics=selection)
    if explicit_weights:
        model = artifact["models"][0]
        model["draw_weighting"] = "explicit_weights"
        model["draw_weights"] = [0.9, 0.1]
        model["mode_energies"] = [[], list(model["mode_energies"][1])]
        artifact["predictive_basis_fingerprint"] = predictive_basis_fingerprint(
            models=artifact["models"], energy_meV=artifact["energy_meV"],
            evaluation_context=artifact["evaluation_context"], exposure_basis=artifact["exposure_basis"])
    outcome = {
        "pooled": True, "omega0": 3.0, "target_delivery_eligible": True,
        "track_revision": 1, "track_state": "confirmed", "candidate_state": "confirmed",
        "candidate_id": "target", "selected_candidate_index": 0,
    }
    projection = build_target_projection(
        artifact, outcome, master_revision="master-r1", calibration_fingerprint="cal-r1",
        policy_fingerprint="policy-r1", action_role="certification")
    return artifact, projection


def test_adapter_retains_authority_identities_and_serializes_json_deterministically(monkeypatch):
    predictive, projection = _authenticated_projection(monkeypatch)
    evidence = adapt_omega0_target_projection(
        predictive, projection, expected_authority=projection["authority"])
    first = evidence.to_dict()
    second = evidence.to_dict()
    assert first == second
    assert json.dumps(first, sort_keys=True, allow_nan=False)
    assert first["target_projection_set_fingerprint"] == evidence.projection_fingerprint
    assert "projection_fingerprint" not in first
    assert first["projection_authority"] == projection["authority"]
    assert first["projection_authority_fingerprint"]
    assert first["predictive_source_model_selection_fingerprint"]
    assert first["qoi_evidence_fingerprint"] == evidence.qoi_evidence_fingerprint


def test_adapter_normalizes_explicit_draw_weights_for_particle_mass(monkeypatch):
    predictive, projection = _authenticated_projection(monkeypatch, explicit_weights=True)
    evidence = adapt_omega0_target_projection(
        predictive, projection, expected_authority=projection["authority"])
    assert evidence.scientific_codes == ()
    assert evidence.observability_codes == ()
    assert evidence.models[0]["draw_weights"] == pytest.approx((0.9, 0.1))
    masses = evidence.to_dict()["diagnostics"]["particle_source_masses"]
    assert masses["target_absent"] == pytest.approx(0.9)
    assert masses["target_present"] == pytest.approx(0.1)


@pytest.mark.parametrize("mutation", [
    lambda projection: projection.__setitem__("predictive_basis_fingerprint", "other"),
    lambda projection: projection.__setitem__("source_predictive_retained_set_fingerprint", "other"),
    lambda projection: projection.__setitem__("source_predictive_semantics_fingerprint", "other"),
    lambda projection: projection.__setitem__("target_projection_set_fingerprint", "other"),
    lambda projection: projection["models"][0].__setitem__("identity_label", "other"),
    lambda projection: projection["models"][0].__setitem__("weight", 0.5),
    lambda projection: projection["models"][0]["target_qoi_draws"].pop(),
])
def test_cross_artifact_mismatches_never_publish_joined_particles(monkeypatch, mutation):
    predictive, projection = _authenticated_projection(monkeypatch)
    mutated = deepcopy(projection)
    mutation(mutated)
    evidence = adapt_omega0_target_projection(
        predictive, mutated, expected_authority=projection["authority"])
    assert "projection_join_mismatch" in evidence.scientific_codes
    assert evidence.models == ()
    assert evidence.to_dict()["diagnostics"]["particle_source_masses"] == {}


def test_semantic_payload_source_link_mismatch_never_publishes_particles(monkeypatch):
    predictive, projection = _authenticated_projection(monkeypatch)
    mutated = deepcopy(projection)
    semantics = mutated["statistical_semantics"]
    semantics["source_predictive_retained_set_fingerprint"] = semantics_fingerprint(
        "tests.eig.qoi.other_source", {})
    semantics["statistical_semantics_fingerprint"] = semantics_fingerprint(
        "isar.target_projection_statistical_semantics/1",
        {name: value for name, value in semantics.items()
         if name != "statistical_semantics_fingerprint"},
    )
    mutated["statistical_semantics_fingerprint"] = semantics[
        "statistical_semantics_fingerprint"]

    evidence = adapt_omega0_target_projection(
        predictive, mutated, expected_authority=projection["authority"])

    assert "projection_join_mismatch" in evidence.scientific_codes
    assert evidence.models == ()
    assert evidence.to_dict()["diagnostics"]["particle_source_masses"] == {}


@pytest.mark.parametrize(("present", "qoi"), [([True, False], [None, None]), ([True, False], [3.0, 1.0])])
def test_invalid_target_presence_qoi_pairs_are_structural(monkeypatch, present, qoi):
    predictive, projection = _authenticated_projection(monkeypatch)
    projection = deepcopy(projection)
    projection["models"][0]["target_present_by_draw"] = present
    projection["models"][0]["target_qoi_draws"] = qoi
    evidence = adapt_omega0_target_projection(
        predictive, projection, expected_authority=projection["authority"])
    assert evidence.observability_codes == ("projection_record_invalid",)

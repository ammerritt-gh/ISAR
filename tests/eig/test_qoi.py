"""Failure-closed behavior of the current omega0 QoI adapter."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace

import numpy as np
import pytest

from isar.eig import adapt_omega0_target_projection, validate_qoi_projection_evidence
from isar.eig.qoi import OMEGA0_ADAPTER_REVISION, QOI_EVIDENCE_CONTRACT
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
         "guideline_structure_filter": [], "guideline_changed_model_set": False,
         "offered_labels": ["one"], "scored_labels": ["one"]},
        error_attachment_labels=["one"])
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


def test_public_qoi_validator_returns_the_same_authenticated_object(monkeypatch):
    predictive, projection = _authenticated_projection(monkeypatch)
    evidence = adapt_omega0_target_projection(
        predictive, projection, expected_authority=projection["authority"])
    assert validate_qoi_projection_evidence(evidence) is evidence


@pytest.mark.parametrize("expected_status", ["missing", "invalid", "qualified", "valid"])
def test_genuine_adapter_statuses_validate_with_exact_derived_scopes(
        monkeypatch, expected_status):
    predictive, projection = _authenticated_projection(monkeypatch)
    if expected_status == "missing":
        evidence = adapt_omega0_target_projection(None, None, expected_authority={})
    elif expected_status == "invalid":
        evidence = adapt_omega0_target_projection(
            {"schema": "isar.master_predictive/2"}, None, expected_authority={})
    elif expected_status == "qualified":
        expected = dict(projection["authority"])
        expected["policy_fingerprint"] = "policy-current"
        evidence = adapt_omega0_target_projection(
            predictive, projection, expected_authority=expected)
    else:
        evidence = adapt_omega0_target_projection(
            predictive, projection, expected_authority=projection["authority"])

    assert evidence.status == expected_status
    assert validate_qoi_projection_evidence(evidence) is evidence
    expected_shadow = "candidate" if expected_status == "valid" else "ineligible"
    assert dict(evidence.authority_scopes) == {
        "eig_shadow": expected_shadow,
        "eig_authoritative": "ineligible",
        "acquisition_selection": "ineligible",
    }


def test_current_qoi_contract_and_adapter_revision_are_exact(monkeypatch):
    predictive, projection = _authenticated_projection(monkeypatch)
    evidence = adapt_omega0_target_projection(
        predictive, projection, expected_authority=projection["authority"])
    assert evidence.contract == "isar.eig_qoi_projection_evidence/o36.2.1"
    assert evidence.adapter_revision == "isar.target_projection_omega0_adapter/3"
    assert evidence.contract == QOI_EVIDENCE_CONTRACT
    assert evidence.adapter_revision == OMEGA0_ADAPTER_REVISION


def test_diagnostics_are_not_part_of_qoi_authentication(monkeypatch):
    predictive, projection = _authenticated_projection(monkeypatch)
    evidence = adapt_omega0_target_projection(
        predictive, projection, expected_authority=projection["authority"])
    changed = replace(evidence, diagnostics=(("operator_note", "not semantic"),))
    assert changed.qoi_evidence_fingerprint == evidence.qoi_evidence_fingerprint
    assert validate_qoi_projection_evidence(changed) is changed


def test_public_qoi_validator_rejects_wrong_runtime_type():
    with pytest.raises(TypeError, match="QoIProjectionEvidence"):
        validate_qoi_projection_evidence(SimpleNamespace())


@pytest.mark.parametrize("tamper", [
    "qoi_draws", "target_presence", "model_label", "authority", "status",
    "scientific_codes", "observability_codes", "source_scopes", "derived_scopes",
    "stored_fingerprint",
])
def test_public_qoi_validator_rejects_stale_or_tampered_content(monkeypatch, tamper):
    predictive, projection = _authenticated_projection(monkeypatch)
    evidence = adapt_omega0_target_projection(
        predictive, projection, expected_authority=projection["authority"])
    altered = replace(evidence)
    if tamper in {"qoi_draws", "target_presence", "model_label"}:
        models = list(altered._models)
        model = models[0]
        if tamper == "qoi_draws":
            model = replace(model, qoi_draws=(99.0, *model.qoi_draws[1:]))
        elif tamper == "target_presence":
            model = replace(model, target_present=(False, *model.target_present[1:]))
        else:
            model = replace(model, label="tampered")
        models[0] = model
        object.__setattr__(altered, "_models", tuple(models))
    elif tamper == "authority":
        authority = dict(altered.projection_authority)
        authority["policy_fingerprint"] = "tampered"
        object.__setattr__(altered, "projection_authority", tuple(sorted(authority.items())))
    elif tamper == "status":
        object.__setattr__(altered, "status", "qualified")
    elif tamper == "scientific_codes":
        object.__setattr__(altered, "scientific_codes", ("projection_stale",))
    elif tamper == "observability_codes":
        object.__setattr__(altered, "observability_codes", ("projection_record_invalid",))
    elif tamper == "source_scopes":
        scopes = dict(altered.source_authority_scopes)
        projection_scopes = dict(scopes["projection"])
        projection_scopes["operator_note"] = "changed"
        scopes["projection"] = projection_scopes
        object.__setattr__(altered, "source_authority_scopes", tuple(sorted(scopes.items())))
    elif tamper == "derived_scopes":
        scopes = dict(altered.authority_scopes)
        scopes["eig_shadow"] = "ineligible"
        object.__setattr__(altered, "authority_scopes", tuple(sorted(scopes.items())))
    elif tamper == "stored_fingerprint":
        object.__setattr__(altered, "qoi_evidence_fingerprint", "0" * 64)
    with pytest.raises(ValueError):
        validate_qoi_projection_evidence(altered)


@pytest.mark.parametrize(("field", "value"), [
    ("contract", "isar.eig_qoi_projection_evidence/future"),
    ("adapter_revision", "isar.target_projection_omega0_adapter/future"),
    ("qoi_id", "other.quantity"),
    ("qoi_unit", "eV"),
    ("source_qoi", "other_qoi"),
])
def test_public_qoi_validator_rejects_wrong_contract_metadata(monkeypatch, field, value):
    predictive, projection = _authenticated_projection(monkeypatch)
    evidence = adapt_omega0_target_projection(
        predictive, projection, expected_authority=projection["authority"])
    altered = replace(evidence)
    object.__setattr__(altered, field, value)
    with pytest.raises(ValueError, match="metadata"):
        validate_qoi_projection_evidence(altered)


def test_qoi_constructor_rejects_stale_supplied_hashes(monkeypatch):
    predictive, projection = _authenticated_projection(monkeypatch)
    evidence = adapt_omega0_target_projection(
        predictive, projection, expected_authority=projection["authority"])
    changed_model = replace(evidence._models[0], qoi_draws=(99.0, 3.1))
    with pytest.raises(ValueError, match="qoi_evidence_fingerprint"):
        replace(evidence, _models=(changed_model,))
    changed_authority = tuple(sorted({**dict(evidence.projection_authority),
                                      "policy_fingerprint": "tampered"}.items()))
    with pytest.raises(ValueError, match="projection_authority_fingerprint"):
        replace(evidence, projection_authority=changed_authority)


def test_qoi_constructor_rejects_valid_status_with_codes(monkeypatch):
    predictive, projection = _authenticated_projection(monkeypatch)
    evidence = adapt_omega0_target_projection(
        predictive, projection, expected_authority=projection["authority"])
    with pytest.raises(ValueError, match="status and reason codes"):
        replace(evidence, scientific_codes=("projection_stale",),
                qoi_evidence_fingerprint=None)


def test_qoi_constructor_rejects_nonvalid_status_without_codes(monkeypatch):
    predictive, projection = _authenticated_projection(monkeypatch)
    evidence = adapt_omega0_target_projection(
        predictive, projection, expected_authority=projection["authority"])
    with pytest.raises(ValueError, match="status and reason codes"):
        replace(evidence, status="qualified", qoi_evidence_fingerprint=None)


def test_qoi_constructor_rejects_elevated_derived_scope(monkeypatch):
    predictive, projection = _authenticated_projection(monkeypatch)
    expected = dict(projection["authority"])
    expected["policy_fingerprint"] = "policy-current"
    evidence = adapt_omega0_target_projection(
        predictive, projection, expected_authority=expected)
    candidate_scopes = tuple(sorted({
        "eig_shadow": "candidate", "eig_authoritative": "ineligible",
        "acquisition_selection": "ineligible",
    }.items()))
    with pytest.raises(ValueError, match="derived authority scopes"):
        replace(evidence, authority_scopes=candidate_scopes,
                qoi_evidence_fingerprint=None)


def test_qoi_constructor_rejects_elevated_source_scope(monkeypatch):
    predictive, projection = _authenticated_projection(monkeypatch)
    evidence = adapt_omega0_target_projection(
        predictive, projection, expected_authority=projection["authority"])
    scopes = dict(evidence.source_authority_scopes)
    elevated = dict(scopes["projection"])
    elevated["eig_shadow"] = "candidate"
    scopes["projection"] = elevated
    with pytest.raises(ValueError, match="source authority scopes exceed"):
        replace(evidence, source_authority_scopes=tuple(sorted(scopes.items())),
                qoi_evidence_fingerprint=None)


def test_qoi_constructor_rejects_old_adapter_revision(monkeypatch):
    predictive, projection = _authenticated_projection(monkeypatch)
    evidence = adapt_omega0_target_projection(
        predictive, projection, expected_authority=projection["authority"])
    with pytest.raises(ValueError, match="metadata"):
        replace(evidence, adapter_revision="isar.target_projection_omega0_adapter/2",
                qoi_evidence_fingerprint=None)


@pytest.mark.parametrize(("field", "value", "message"), [
    ("status", "future", "invalid status"),
    ("scientific_codes", ["projection_stale"], "unique non-empty string tuple"),
    ("scientific_codes", ("projection_stale", "projection_stale"),
     "unique non-empty string tuple"),
    ("scientific_codes", ("",), "unique non-empty string tuple"),
])
def test_qoi_validator_rejects_invalid_status_and_code_shapes(
        monkeypatch, field, value, message):
    predictive, projection = _authenticated_projection(monkeypatch)
    evidence = adapt_omega0_target_projection(
        predictive, projection, expected_authority=projection["authority"])
    altered = replace(evidence)
    object.__setattr__(altered, field, value)
    with pytest.raises(ValueError, match=message):
        validate_qoi_projection_evidence(altered)


def test_qoi_validator_rejects_erased_authentic_stale_verdict(monkeypatch):
    predictive, projection = _authenticated_projection(monkeypatch)
    expected = dict(projection["authority"])
    expected["policy_fingerprint"] = "policy-current"
    stale = adapt_omega0_target_projection(
        predictive, projection, expected_authority=expected)
    assert stale.status == "qualified"
    assert stale.scientific_codes == ("projection_stale",)

    erased = replace(stale)
    object.__setattr__(erased, "status", "valid")
    object.__setattr__(erased, "scientific_codes", ())
    object.__setattr__(erased, "authority_scopes", (
        ("acquisition_selection", "ineligible"),
        ("eig_authoritative", "ineligible"),
        ("eig_shadow", "candidate"),
    ))
    with pytest.raises(ValueError, match="qoi_evidence_fingerprint"):
        validate_qoi_projection_evidence(erased)


def test_invalid_upstream_scope_is_not_republished_as_qoi_authority(monkeypatch):
    predictive, projection = _authenticated_projection(monkeypatch)
    mutated = deepcopy(projection)
    mutated["authority_scopes"]["eig_shadow"] = "candidate"
    evidence = adapt_omega0_target_projection(
        predictive, mutated, expected_authority=projection["authority"])

    assert evidence.status == "invalid"
    assert evidence.observability_codes == ("projection_record_invalid",)
    assert dict(evidence.authority_scopes) == {
        "eig_shadow": "ineligible",
        "eig_authoritative": "ineligible",
        "acquisition_selection": "ineligible",
    }
    assert "projection" not in dict(evidence.source_authority_scopes)


def test_valid_qoi_requires_authenticated_source_scope_records(monkeypatch):
    predictive, projection = _authenticated_projection(monkeypatch)
    evidence = adapt_omega0_target_projection(
        predictive, projection, expected_authority=projection["authority"])
    incomplete = tuple(
        item for item in evidence.source_authority_scopes if item[0] != "projection")
    with pytest.raises(ValueError, match="missing source authority records"):
        replace(evidence, source_authority_scopes=incomplete,
                qoi_evidence_fingerprint=None)


def test_source_scope_extra_fields_are_retained_and_authenticated(monkeypatch):
    predictive, projection = _authenticated_projection(monkeypatch)
    evidence = adapt_omega0_target_projection(
        predictive, projection, expected_authority=projection["authority"])
    scopes = dict(evidence.source_authority_scopes)
    projection_scopes = dict(scopes["projection"])
    projection_scopes["operator_note"] = "retained"
    scopes["projection"] = projection_scopes
    changed = replace(
        evidence, source_authority_scopes=tuple(sorted(scopes.items())),
        qoi_evidence_fingerprint=None)
    assert changed.qoi_evidence_fingerprint != evidence.qoi_evidence_fingerprint
    assert dict(dict(changed.source_authority_scopes)["projection"])["operator_note"] == "retained"


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

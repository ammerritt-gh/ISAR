"""EIG-4 Slice 3 homogeneous predictive-scenario design measure."""
from __future__ import annotations

import ast
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from isar.analyze.shadow_selection import unavailable_shadow_selection
from isar.drive.acquisition.inventory import scientific_fingerprint_from_mapping
from isar.eig import (
    EligibilityInput,
    adapt_omega0_target_projection,
    build_scan_window_design_measure,
)
from isar.predictive import PREDICTIVE_MODEL_WEIGHT_FLOOR, predictive_basis_fingerprint
from isar.schema.statistics import (
    FIT_SELECTION_SCHEMA,
    authority_scopes,
    semantics_fingerprint,
    validate_for_write,
)
from isar.session import driver
from isar.session.predictive_artifact import build_target_projection


def _fit_semantics(label: str) -> dict:
    data = semantics_fingerprint("tests.eig.design_measure.data", {"counts": [2.0, 3.0]})
    model = semantics_fingerprint("tests.eig.design_measure.model", {"label": label})
    constraints = semantics_fingerprint("tests.eig.design_measure.constraints", {"values": []})
    comparison = semantics_fingerprint("tests.eig.design_measure.comparison", {
        "data_likelihood_fingerprint": data,
        "model_parameterization_fingerprint": model,
        "constraint_fingerprint": constraints,
        "estimator_kind": "data_likelihood_mle",
    })
    return validate_for_write({
        "schema": FIT_SELECTION_SCHEMA,
        "estimator_kind": "data_likelihood_mle",
        "fit_kind": "unconstrained",
        "effective_constraints": [],
        "aggregate_constraint_origin": "none",
        "selection_eligibility": "eligible",
        "selection_ineligibility_reasons": [],
        "data_likelihood_fingerprint": data,
        "model_parameterization_fingerprint": model,
        "constraint_fingerprint": constraints,
        "comparison_context_fingerprint": comparison,
        "conditioning_fingerprint": comparison,
        "authority_scopes": authority_scopes(single_fit_delivery="active"),
    }, FIT_SELECTION_SCHEMA)


def _neutral_shadow() -> dict:
    return unavailable_shadow_selection(
        "shadow_skipped_soft_deadline", stage="execution_policy",
        selection_context_fingerprint=semantics_fingerprint(
            "tests.eig.design_measure.shadow", {}),
        candidate_generation_route={"route_kind": "standard_two_stage"},
        legacy_model_selection_fingerprint=None,
        legacy_selected_label="bootstrap",
        legacy_model_weights={"bootstrap": 1.0},
        runtime_increment_seconds=0.0,
        shadow_execution_kind="skipped",
    )


def _eligibility_input(qoi, selection, target) -> EligibilityInput:
    intent = {
        "action_class": "production", "hkl": [2.0, 0.0, 0.0],
        "energy_lo": 1.0, "energy_hi": 4.0, "window_family": "target",
        "target_branch_role": None, "rival_branch_role": None,
        "evidence_basis_fingerprint": "basis", "hypotheses_fingerprint": "hyp",
        "campaign_slice_fingerprint": "slice", "evidence_sink": "canonical_master",
    }
    fingerprint = scientific_fingerprint_from_mapping(intent)
    intent["scientific_action_fingerprint"] = fingerprint
    provenance = {
        "action_class": "production", "scientific_action_fingerprint": fingerprint,
        "proposal_scientific_action_fingerprint": fingerprint,
        "acquisition_action_role": "production", "evidence_sink": "canonical_master",
        "driver_inserted": False, "insertion_purpose": None,
        "authoritative_evidence_revision": "master-r1", "hkl": [2.0, 0.0, 0.0],
    }
    return EligibilityInput.from_current_contract(
        action_intent=intent, action_provenance=provenance,
        target_outcome={**target, "hkl": [2.0, 0.0, 0.0], "pooled": True,
                        "pool_consistent": True, "modality": "fold",
                        "master_revision": "master-r1"},
        selection_record=selection, qoi_evidence=qoi, shadow_record=_neutral_shadow())


def _bundle(monkeypatch, specs=None):
    """Build real `/2` predictive/projection/QoI/eligibility producer chains."""
    from isar.analyze.context import ScanContext
    from isar.session import predictive_artifact

    specs = specs or [{
        "label": "bootstrap", "weight": 1.0, "basis": "parametric_bootstrap",
        "draw_ids": ["draw-0", "draw-1"], "phonon": True,
    }]
    phonon_manifest = {
        "schema": "isar.model_manifest/1",
        "components": [
            {"type": "phonon_dho", "prefix": "phonon", "peak": "stokes+astokes"},
            {"type": "constant_background", "prefix": "bg"},
        ],
        "parameters": {"phonon.Omega_d": 3.0, "phonon.Gamma": 0.3,
                       "phonon.A": 100.0, "bg.BG": 4.0},
    }
    background_manifest = {
        "schema": "isar.model_manifest/1",
        "components": [{"type": "constant_background", "prefix": "bg"}],
        "parameters": {"bg.BG": 4.0},
    }
    by_label = {item["label"]: item for item in specs}

    def manifest_of(result, _by_hyp):
        return phonon_manifest if by_label[result.label]["phonon"] else background_manifest

    def draws_of(result, **_kwargs):
        spec = by_label[result.label]
        n_draws = len(spec["draw_ids"])
        if spec["phonon"]:
            names = ("phonon.Omega_d", "phonon.Gamma", "phonon.A", "bg.BG")
            rows = np.asarray([[3.0 + 0.05 * index, 0.3, 100.0, 4.0]
                               for index in range(n_draws)])
        else:
            names = ("bg.BG",)
            rows = np.asarray([[4.0 + index] for index in range(n_draws)])
        return names, rows, spec["basis"], {
            "draw_weighting": "equal_weight_resampled", "draw_weights": None,
            "draw_ids": list(spec["draw_ids"]), "source_draw_count": n_draws,
            "retained_draw_count": n_draws, "dropped_draws": [],
            "draw_mass_retained": 1.0, "draw_cap": 64,
            "thinned_from_draw_count": n_draws,
        }

    monkeypatch.setattr(predictive_artifact, "model_manifest_of", manifest_of)
    monkeypatch.setattr(predictive_artifact, "bounded_parameter_draws", draws_of)
    context = ScanContext(
        x=np.linspace(-2.0, 6.0, 5), counts=np.zeros(5), mask=np.ones(5, bool), T=100.0,
        center=0.0, gauss_fwhm=0.4, lorentz_fwhm=0.1, instrument="IN8",
        energy_status="absolute", exposure_scale=np.ones(5), exposure_reference=1.0)
    semantics = {item["label"]: _fit_semantics(item["label"]) for item in specs}
    results = {
        item["label"]: SimpleNamespace(
            label=item["label"], omega0=3.0, fit_semantics=semantics[item["label"]],
            within_model={
                "omega0_samples": [3.0, 3.1],
                **({"bayes_prior_fingerprint": semantics_fingerprint(
                    "tests.eig.design_measure.prior", {"label": item["label"]})}
                   if item["basis"] == "nested_sampling" else {}),
            })
        for item in specs
    }
    scores = [SimpleNamespace(
        label=item["label"], cost=10.0 + index, k=2, aic=14.0 + index,
        weight=item["weight"], survives=True, fit_semantics=semantics[item["label"]])
        for index, item in enumerate(specs)]
    labels = [item["label"] for item in specs]
    selection = driver._build_selection_semantics(
        list(results.values()), SimpleNamespace(scores=scores, pooled=object()),
        {"route": "full_two_stage", "labels_before_guideline": labels,
         "guideline_structure_filter": labels, "offered_labels": labels,
         "scored_labels": labels}, error_attachment_labels=labels)
    artifact = predictive_artifact.build_master_predictive(
        SimpleNamespace(omega0=3.0), scores, results,
        {item["label"]: object() for item in specs}, context,
        seed=11, n_members=1, fit_selection_semantics=selection)
    target = {
        "omega0": 3.0, "target_delivery_eligible": True,
        "track_revision": 1, "track_state": "confirmed", "candidate_state": "confirmed",
        "candidate_id": "target", "selected_candidate_index": 0,
    }
    projection = build_target_projection(
        artifact, target, master_revision="master-r1", calibration_fingerprint="cal-r1",
        policy_fingerprint="policy-r1", action_role="certification")
    qoi = adapt_omega0_target_projection(
        artifact, projection, expected_authority=projection["authority"])
    eligibility = _eligibility_input(qoi, selection, target)
    return artifact, projection, qoi, eligibility


def _qoi_with_public_models(qoi, models):
    """A public-view-only QoI stand-in for testing Slice 3's defensive join."""
    names = (
        "status", "scientific_codes", "observability_codes", "qoi_id", "qoi_unit",
        "source_qoi", "predictive_basis_fingerprint", "predictive_retained_set_fingerprint",
        "predictive_statistical_semantics_fingerprint",
        "predictive_source_model_selection_fingerprint", "target_projection_set_fingerprint",
        "projection_statistical_semantics_fingerprint", "projection_authority_fingerprint",
        "qoi_evidence_fingerprint", "diagnostics",
    )
    return SimpleNamespace(**{name: getattr(qoi, name) for name in names}, models=tuple(models))


def test_available_measure_is_normalized_immutable_deterministic_and_shadow_only(monkeypatch):
    artifact, _projection, _qoi, eligibility = _bundle(monkeypatch)
    source = deepcopy(artifact)
    first = build_scan_window_design_measure(artifact, eligibility)
    second = build_scan_window_design_measure(artifact, eligibility)

    assert first.status == "available", (first.reason_codes, first.eligibility,
                                         first.construction_codes, first.diagnostics)
    assert first.to_dict() == second.to_dict()
    assert artifact == source
    assert sum(item["conditional_scenario_weight"] for item in first.predictive_scenarios) == pytest.approx(1.0)
    assert sum(item["design_ensemble_weight"] for item in first.models) == pytest.approx(1.0)
    assert [item["source_draw_id"] for item in first.predictive_scenarios] == ["draw-0", "draw-1"]
    assert first.to_dict()["authority_scopes"] == {
        "eig_shadow": "candidate", "eig_authoritative": "ineligible",
        "acquisition_selection": "ineligible"}
    assert json.dumps(first.to_dict(), sort_keys=True, allow_nan=False)
    with pytest.raises(TypeError):
        first.models[0]["label"] = "changed"
    with pytest.raises(TypeError):
        first.predictive_scenarios[0]["qoi_value"] = 99.0
    with pytest.raises(TypeError):
        dict(first.mass_diagnostics)["source_aic_scale"]["bootstrap_model_mass"] = 0.0
    completeness = first.to_dict()["mass_diagnostics"]["source_completeness"]
    assert completeness == {
        name: artifact["completeness"][name] for name in (
            "source_model_weight_total", "retained_model_weight_total", "pruned_model_weight",
            "model_weight_floor", "max_pruned_mass", "max_dropped_draw_mass",
            "predictive_complete_for_eig")}


def test_unequal_draw_counts_preserve_model_aic_mass(monkeypatch):
    artifact, _projection, _qoi, eligibility = _bundle(monkeypatch, [
        {"label": "one", "weight": 0.75, "basis": "parametric_bootstrap",
         "draw_ids": ["one-0"], "phonon": True},
        {"label": "two", "weight": 0.25, "basis": "parametric_bootstrap",
         "draw_ids": ["two-0", "two-1", "two-2"], "phonon": True},
    ])
    result = build_scan_window_design_measure(artifact, eligibility)
    by_label = {item["label"]: item for item in result.models}
    assert result.status == "available"
    assert by_label["one"]["design_ensemble_weight"] == pytest.approx(0.75)
    assert by_label["two"]["design_ensemble_weight"] == pytest.approx(0.25)
    assert [item["conditional_scenario_weight"] for item in result.predictive_scenarios
            if item["model_label"] == "two"] == pytest.approx([1 / 12] * 3)
    ids = [item["scenario_id"] for item in result.predictive_scenarios]
    assert len(ids) == len(set(ids))
    assert {item["retained_source_draw_count"] for item in result.models} == {1, 3}


def test_source_and_retained_draw_counts_are_distinct_and_authenticated(monkeypatch):
    artifact, _projection, _qoi, eligibility = _bundle(monkeypatch)
    artifact["models"][0]["source_draw_count"] = 5
    result = build_scan_window_design_measure(artifact, eligibility)
    assert result.status == "available"
    assert result.models[0]["source_draw_count"] == 5
    assert result.models[0]["retained_source_draw_count"] == 2

    for field, value in (("retained_draw_count", 1), ("source_draw_count", 1)):
        tampered = deepcopy(artifact)
        tampered["models"][0][field] = value
        refused = build_scan_window_design_measure(tampered, eligibility)
        assert refused.construction_codes == ("design_measure_input_mismatch",)


def test_submaterial_nonbootstrap_model_is_omitted_and_reported(monkeypatch):
    small = PREDICTIVE_MODEL_WEIGHT_FLOOR / 2
    artifact, _projection, _qoi, eligibility = _bundle(monkeypatch, [
        {"label": "bootstrap", "weight": 1.0 - small, "basis": "parametric_bootstrap",
         "draw_ids": ["b-0", "b-1"], "phonon": True},
        {"label": "laplace", "weight": small, "basis": "laplace_hesse",
         "draw_ids": ["l-0"], "phonon": True},
    ])
    result = build_scan_window_design_measure(artifact, eligibility)
    assert result.status == "available"
    assert [item["label"] for item in result.models] == ["bootstrap"]
    assert result.to_dict()["excluded_models"][0]["label"] == "laplace"
    assert result.to_dict()["mass_diagnostics"]["source_aic_scale"][
        "laplace_hesse_model_mass"] == pytest.approx(small)


@pytest.mark.parametrize("basis", ["nested_sampling", "laplace_hesse"])
def test_material_nonbootstrap_mass_is_an_upstream_scientific_refusal(monkeypatch, basis):
    artifact, _projection, _qoi, eligibility = _bundle(monkeypatch, [
        {"label": "bootstrap", "weight": 0.5, "basis": "parametric_bootstrap",
         "draw_ids": ["b-0"], "phonon": True},
        {"label": "other", "weight": 0.5, "basis": basis,
         "draw_ids": ["n-0"], "phonon": True},
    ])
    result = build_scan_window_design_measure(artifact, eligibility)
    assert result.status == "unavailable"
    assert result.reason_codes[0] == "predictive_material_nonbootstrap_mass"
    assert result.construction_codes == ()
    assert result.models == () and result.predictive_scenarios == ()


def test_split_nonbootstrap_mass_combines_at_the_upstream_gate(monkeypatch):
    part = PREDICTIVE_MODEL_WEIGHT_FLOOR * 0.6
    artifact, _projection, _qoi, eligibility = _bundle(monkeypatch, [
        {"label": "bootstrap", "weight": 1.0 - 2 * part, "basis": "parametric_bootstrap",
         "draw_ids": ["b-0"], "phonon": True},
        {"label": "nested", "weight": part, "basis": "nested_sampling",
         "draw_ids": ["n-0"], "phonon": True},
        {"label": "laplace", "weight": part, "basis": "laplace_hesse",
         "draw_ids": ["l-0"], "phonon": True},
    ])
    result = build_scan_window_design_measure(artifact, eligibility)
    assert result.status == "unavailable"
    assert "predictive_material_nonbootstrap_mass" in result.reason_codes


def test_material_unknown_basis_remains_an_upstream_observability_category(monkeypatch):
    artifact, _projection, qoi, eligibility = _bundle(monkeypatch)
    unknown = replace(
        qoi, observability_codes=("predictive_material_basis_unknown",),
        qoi_evidence_fingerprint=None)
    result = build_scan_window_design_measure(
        artifact, replace(eligibility, qoi_evidence=unknown))
    assert result.status == "unavailable"
    assert "predictive_material_basis_unknown" in result.reason_codes
    assert result.construction_codes == ()


def test_empty_conditional_bootstrap_ensemble_refuses_without_partial_output(monkeypatch):
    small = PREDICTIVE_MODEL_WEIGHT_FLOOR / 2
    artifact, _projection, _qoi, eligibility = _bundle(monkeypatch, [
        {"label": "bootstrap", "weight": 1.0 - small, "basis": "parametric_bootstrap",
         "draw_ids": ["b-0", "b-1"], "phonon": False},
        {"label": "nested", "weight": small, "basis": "nested_sampling",
         "draw_ids": ["n-0"], "phonon": True},
    ])
    result = build_scan_window_design_measure(artifact, eligibility)
    assert result.status == "unavailable"
    assert result.construction_codes == ("conditional_target_qoi_ensemble_empty",)
    assert result.models == () and result.predictive_scenarios == ()


def test_partial_target_absence_is_excluded_and_remaining_mass_renormalized(monkeypatch):
    artifact, projection, _qoi, eligibility = _bundle(monkeypatch)
    projection = deepcopy(projection)
    projection["models"][0]["target_present_by_draw"][0] = False
    projection["models"][0]["target_qoi_draws"][0] = None
    qoi = adapt_omega0_target_projection(
        artifact, projection, expected_authority=projection["authority"])
    result = build_scan_window_design_measure(
        artifact, replace(eligibility, qoi_evidence=qoi))
    assert result.status == "available"
    assert len(result.predictive_scenarios) == 1
    assert result.predictive_scenarios[0]["conditional_scenario_weight"] == pytest.approx(1.0)
    retained = result.to_dict()["mass_diagnostics"]["bootstrap_retained_scale"]
    assert retained["target_qoi_available_mass_before_conditioning"] == pytest.approx(0.5)
    assert retained["target_qoi_unavailable_mass_before_conditioning"] == pytest.approx(0.5)


def test_scenario_id_is_qoi_invariant_while_measure_fingerprint_is_not(monkeypatch):
    artifact, projection, _qoi, eligibility = _bundle(monkeypatch)
    first = build_scan_window_design_measure(artifact, eligibility)
    changed_projection = deepcopy(projection)
    changed_projection["models"][0]["target_qoi_draws"][0] += 0.01
    # The target-projection set identity intentionally covers labels/weights/counts, not QoI values.
    changed_qoi = adapt_omega0_target_projection(
        artifact, changed_projection, expected_authority=projection["authority"])
    changed_input = replace(eligibility, qoi_evidence=changed_qoi)
    second = build_scan_window_design_measure(artifact, changed_input)
    assert second.status == "available"
    assert first.predictive_scenarios[0]["scenario_id"] == second.predictive_scenarios[0]["scenario_id"]
    assert first.design_measure_fingerprint != second.design_measure_fingerprint


def test_source_draw_identity_changes_derived_identities(monkeypatch):
    artifact, _projection, _qoi, eligibility = _bundle(monkeypatch)
    first = build_scan_window_design_measure(artifact, eligibility)
    changed = deepcopy(artifact)
    changed["models"][0]["draw_ids"][0] = "replacement-draw"
    second = build_scan_window_design_measure(changed, eligibility)
    assert second.status == "available"
    assert first.source_draw_identity_fingerprint != second.source_draw_identity_fingerprint
    assert first.design_measure_fingerprint != second.design_measure_fingerprint
    assert first.predictive_scenarios[0]["scenario_id"] != second.predictive_scenarios[0]["scenario_id"]


@pytest.mark.parametrize(("field", "value"), [
    ("label", "other"),
    ("identity_label", "other-identity"),
    ("weight", 0.5),
    ("source_weight", 0.5),
    ("draw_basis", "laplace_hesse"),
    ("draw_weighting", "explicit_weights"),
])
def test_public_qoi_model_join_mismatch_refuses(monkeypatch, field, value):
    artifact, _projection, qoi, eligibility = _bundle(monkeypatch)
    models = [dict(model) for model in qoi.models]
    models[0][field] = value
    mismatched = _qoi_with_public_models(qoi, models)
    result = build_scan_window_design_measure(
        artifact, replace(eligibility, qoi_evidence=mismatched))
    assert result.status == "unavailable"
    assert result.construction_codes == ("design_measure_input_mismatch",)
    assert result.models == () and result.predictive_scenarios == ()


@pytest.mark.parametrize(("field", "value", "code"), [
    ("draw_ids", None, "scenario_identity_missing"),
    ("draw_ids", ["", "draw-1"], "scenario_identity_missing"),
])
def test_missing_source_draw_identity_refuses(monkeypatch, field, value, code):
    artifact, _projection, _qoi, eligibility = _bundle(monkeypatch)
    artifact = deepcopy(artifact)
    artifact["models"][0][field] = value
    result = build_scan_window_design_measure(artifact, eligibility)
    assert result.status == "unavailable"
    # draw IDs are outside the `/2` basis fingerprint, so this reaches the Slice 3 identity gate.
    assert result.construction_codes == (code,)


def test_explicit_weights_wait_for_authenticated_source_semantics(monkeypatch):
    artifact, projection, _qoi, original_eligibility = _bundle(monkeypatch)
    artifact = deepcopy(artifact)
    model = artifact["models"][0]
    model["draw_weighting"] = "explicit_weights"
    model["draw_weights"] = [0.9, 0.1]
    artifact["predictive_basis_fingerprint"] = predictive_basis_fingerprint(
        models=artifact["models"], energy_meV=artifact["energy_meV"],
        evaluation_context=artifact["evaluation_context"], exposure_basis=artifact["exposure_basis"])
    projection = build_target_projection(
        artifact, {"omega0": 3.0, "target_delivery_eligible": True,
                   "track_revision": 1, "track_state": "confirmed",
                   "candidate_state": "confirmed", "candidate_id": "target",
                   "selected_candidate_index": 0},
        master_revision="master-r1", calibration_fingerprint="cal-r1",
        policy_fingerprint="policy-r1", action_role="certification")
    qoi = adapt_omega0_target_projection(
        artifact, projection, expected_authority=projection["authority"])
    target = {"omega0": 3.0, "target_delivery_eligible": True, "track_revision": 1,
              "track_state": "confirmed", "candidate_state": "confirmed",
              "candidate_id": "target", "selected_candidate_index": 0}
    selection = original_eligibility.selection_record
    eligibility = _eligibility_input(qoi, selection, target)
    result = build_scan_window_design_measure(artifact, eligibility)
    assert result.status == "unavailable"
    assert result.construction_codes == ("design_measure_input_mismatch",)


def test_public_contract_has_no_statistical_particle_keys(monkeypatch):
    artifact, _projection, _qoi, eligibility = _bundle(monkeypatch)
    payload = build_scan_window_design_measure(artifact, eligibility).to_dict()

    def keys(value):
        if isinstance(value, dict):
            for key, item in value.items():
                yield key
                yield from keys(item)
        elif isinstance(value, list):
            for item in value:
                yield from keys(item)

    assert not [key for key in keys(payload) if "particle" in key]


def test_design_measure_import_boundary_in_fresh_process():
    module_path = Path(__file__).parents[2] / "isar" / "eig" / "design_measure.py"
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imported = {
        node.module for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert not any(name.startswith(("isar.drive", "isar.report", "isar.session"))
                   for name in imported)

    command = (
        "import isar.eig.design_measure,sys;"
        "print('\\n'.join(sorted(name for name in sys.modules "
        "if name.startswith(('isar.drive','isar.report','isar.session')))))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", command], check=True, capture_output=True,
        text=True, encoding="utf-8")
    loaded = set(completed.stdout.splitlines())
    # Eligibility's pre-existing scientific-action fingerprint dependency is the baseline.
    assert loaded <= {
        "isar.drive", "isar.drive.acquisition", "isar.drive.acquisition.inventory",
        "isar.drive.objective",
    }

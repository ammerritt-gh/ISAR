"""The published predictive basis (``isar.master_predictive/2``) and its pure evaluator.

Three layers are covered here:

* the shared manifest→expected-counts path (:mod:`isar.analyze.model_manifest`) and its by-name
  parameter mapping;
* the ``/2`` producer in the driver — exposure basis, completeness, target projection, fingerprint,
  and the self-consistency of its stored matrices with the shared evaluator;
* :func:`isar.predictive.evaluate_master_predictive` against the normative fixture and its refusals.
"""
import json
import math
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from isar.analyze import model_manifest as mm
from isar.analyze.context import ScanContext
from isar.parsers import ill_in8
from isar.predictive import (evaluate_master_predictive, predictive_basis_fingerprint,
                             predictive_score as _predictive_score)
from isar.session import driver, predictive_artifact
from isar.session.master_delivery import _persist_master_predictive
from isar.session.predictive_artifact import (bounded_parameter_draws as _bounded_parameter_draws,
                                              build_master_predictive as _predictive_ensemble)

_predictive_basis_fingerprint = predictive_basis_fingerprint
from isar.session.workspace import Workspace
from isar.synth_format import run_builder

_FIXTURE = Path(__file__).parents[1] / "fixtures" / "eig_predictive_v2.json"


def _fixture():
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Draw basis
# ---------------------------------------------------------------------------
def test_full_parameter_draw_basis_prefers_nested_then_bootstrap_then_laplace():
    nested = SimpleNamespace(
        within_model={"bayes_ok": True, "bayes_parameter_names": ["phonon.Omega_d", "bg.BG"],
                      "bayes_parameter_samples": [[2.0, 1.0], [2.1, 1.2]],
                      "parameter_names": ["wrong"], "parameter_samples": [[9.0]]},
        covariance_ok=True, parameter_covariance=np.eye(2),
        params={"phonon.Omega_d": 2.0, "bg.BG": 1.0})
    names, samples, basis, diagnostics = _bounded_parameter_draws(nested, seed=7)
    assert names == ("phonon.Omega_d", "bg.BG")
    assert samples.shape == (2, 2) and basis == "nested_sampling"
    assert diagnostics["source_draw_count"] == 2 and diagnostics["retained_draw_count"] == 2
    assert diagnostics["draw_mass_retained"] == 1.0 and diagnostics["dropped_draws"] == []

    nested.within_model = {"parameter_names": ["phonon.Omega_d", "bg.BG"],
                           "parameter_samples": [[2.0, 1.0], [2.1, 1.2]]}
    assert _bounded_parameter_draws(nested, seed=7)[2] == "parametric_bootstrap"

    nested.within_model = {}
    names, samples, basis, _diagnostics = _bounded_parameter_draws(nested, seed=7, maximum=8)
    assert names == ("phonon.Omega_d", "bg.BG")
    assert samples.shape == (8, 2) and basis == "laplace_hesse"


def test_every_draw_basis_declares_the_equal_weight_measure_it_actually_produces():
    # ADR-014: the measure is declared, not assumed. All three ISAR bases are equal-weight particle
    # sets at their source (dynesty is resample_equal'd; a bootstrap replicate and a Laplace draw are
    # one particle each), so none may publish implicit weights.
    assert set(predictive_artifact.DRAW_WEIGHTING) == {
        "nested_sampling", "parametric_bootstrap", "laplace_hesse"}
    assert set(predictive_artifact.DRAW_WEIGHTING.values()) == {"equal_weight_resampled"}
    result = SimpleNamespace(
        within_model={"parameter_names": ["bg.BG"], "parameter_samples": [[1.0], [2.0]]},
        covariance_ok=False, parameter_covariance=None, params={"bg.BG": 1.0})
    _names, _samples, _basis, diagnostics = _bounded_parameter_draws(result, seed=3)
    assert diagnostics["draw_weighting"] == "equal_weight_resampled"
    assert diagnostics["draw_weights"] is None


def test_nonfinite_draws_are_dropped_with_a_reason_and_reduce_the_retained_mass():
    result = SimpleNamespace(
        within_model={"parameter_names": ["bg.BG"],
                      "parameter_samples": [[1.0], [float("nan")], [2.0], [3.0]]},
        covariance_ok=False, parameter_covariance=None, params={"bg.BG": 1.0})
    _names, samples, _basis, diagnostics = _bounded_parameter_draws(result, seed=3)
    assert samples.shape == (3, 1)
    assert diagnostics["dropped_draws"] == [
        {"id": "draw-1", "reason": "nonfinite_parameter_draw"}]
    assert diagnostics["draw_ids"] == ["draw-0", "draw-2", "draw-3"]
    assert diagnostics["draw_mass_retained"] == 0.75


# ---------------------------------------------------------------------------
# Existing scoring surface (untouched by EIG-1)
# ---------------------------------------------------------------------------
def test_predictive_information_is_deterministic_and_model_discriminating():
    models = [
        {"weight": 0.5, "omega0_draws": [2.0, 2.1],
         "expected_counts": [[30.0, 1.0, 1.0], [28.0, 2.0, 1.0]]},
        {"weight": 0.5, "omega0_draws": [6.0, 6.1],
         "expected_counts": [[1.0, 1.0, 30.0], [1.0, 2.0, 28.0]]},
    ]
    first = _predictive_score(models, seed=123, n_simulations=256)
    second = _predictive_score(models, seed=123, n_simulations=256)
    assert first == second
    assert first["usable"] is True
    assert first["model_branch_information_nats"] > 0.0
    assert first["expected_information_nats"] >= first["model_branch_information_nats"]


def test_no_phonon_model_keeps_expected_counts_for_identity_information():
    models = [
        {"weight": 0.5, "omega0_draws": [],
         "expected_counts": [[30.0, 1.0, 1.0], [28.0, 2.0, 1.0]]},
        {"weight": 0.5, "omega0_draws": [6.0, 6.1],
         "expected_counts": [[1.0, 1.0, 30.0], [1.0, 2.0, 28.0]]},
    ]
    score = _predictive_score(models, seed=123, n_simulations=256)
    assert score["usable"] is True
    assert score["model_branch_information_nats"] > 0.0
    assert score["expected_omega_halfwidth_reduction_meV"] >= 0.0


def test_predictive_artifact_is_content_addressed_and_retained(tmp_path):
    payload = {"schema": "isar.master_predictive/2", "seed": 7, "models": [],
               "master_content_hash": "abc", "diagnostics": {"usable": False}}
    data = tmp_path / "data"
    first = _persist_master_predictive(data, payload)
    second = _persist_master_predictive(data, payload)
    assert first == second
    assert (tmp_path / first).exists()

    changed = _persist_master_predictive(data, {**payload, "master_content_hash": "def"})
    assert changed != first
    assert (tmp_path / first).exists() and (tmp_path / changed).exists()


# ---------------------------------------------------------------------------
# The shared evaluation path: substitution is BY NAME
# ---------------------------------------------------------------------------
_MANIFEST = {
    "schema": "isar.model_manifest/1",
    "components": [{"type": "phonon_dho", "prefix": "phonon", "peak": "stokes+astokes"},
                   {"type": "constant_background", "prefix": "bg"}],
    "parameters": {"phonon.Omega_d": 3.0, "phonon.Gamma": 0.3, "phonon.A": 100.0,
                   "bg.BG": 4.0},
}
_NAMES = ["phonon.Omega_d", "phonon.Gamma", "phonon.A", "bg.BG"]
_DRAW = [3.0, 0.3, 100.0, 4.0]
_GRID = [-2.0, 0.0, 2.0, 4.0, 6.0]
_CONTEXT = mm.EvaluationContext(center=0.0, gauss_fwhm=0.4, lorentz_fwhm=0.1, T=100.0)


def test_named_substitution_is_order_independent_but_identity_exact():
    canonical = mm.expected_counts(_MANIFEST, _NAMES, _DRAW, _CONTEXT, _GRID)
    order = [3, 0, 2, 1]
    shuffled_names = [_NAMES[index] for index in order]
    shuffled_draw = [_DRAW[index] for index in order]
    shuffled = mm.expected_counts(_MANIFEST, shuffled_names, shuffled_draw, _CONTEXT, _GRID)
    # A positional implementation would silently feed bg.BG into phonon.Omega_d here.
    assert np.allclose(canonical, shuffled, rtol=0.0, atol=0.0)
    assert not np.allclose(canonical, mm.expected_counts(
        _MANIFEST, _NAMES, shuffled_draw, _CONTEXT, _GRID))


@pytest.mark.parametrize("names,draw", [
    (["phonon.Omega_d", "phonon.Omega_d", "phonon.A", "bg.BG"], _DRAW),          # duplicate
    (["phonon.Omega_d", "phonon.Gamma", "phonon.A"], _DRAW[:3]),                 # missing
    (_NAMES + ["phonon.extra"], _DRAW + [1.0]),                                  # extra
    (_NAMES, _DRAW[:3]),                                                         # short row
    (_NAMES, [3.0, 0.3, 100.0, float("nan")]),                                   # non-finite
])
def test_a_draw_that_does_not_map_onto_the_manifest_is_refused(names, draw):
    with pytest.raises(mm.ModelManifestError) as excinfo:
        mm.expected_counts(_MANIFEST, names, draw, _CONTEXT, _GRID)
    assert excinfo.value.reason_code == "malformed_draw_shape"


def test_an_unknown_component_kind_refuses_the_whole_evaluation():
    manifest = {**_MANIFEST, "components": _MANIFEST["components"] + [
        {"type": "magnon_dho", "prefix": "magnon"}]}
    with pytest.raises(mm.ModelManifestError) as excinfo:
        mm.expected_counts(manifest, _NAMES, _DRAW, _CONTEXT, _GRID)
    assert excinfo.value.reason_code == "unknown_component_kind"


@pytest.mark.parametrize("grid,reason", [
    ([], "invalid_energy_grid"),
    ([0.0, float("inf")], "invalid_energy_grid"),
    ([[0.0, 1.0], [2.0, 3.0]], "invalid_energy_grid"),
])
def test_an_unusable_energy_grid_is_refused(grid, reason):
    with pytest.raises(mm.ModelManifestError) as excinfo:
        mm.expected_counts(_MANIFEST, _NAMES, _DRAW, _CONTEXT, grid)
    assert excinfo.value.reason_code == reason


def test_the_predictive_contract_has_exactly_one_definition():
    # Two independent literals could be bumped apart: raising only the driver's copy forces the
    # carry gate to refit while master_fit_fingerprint stays unchanged, so a checkpoint store keeps
    # serving entries built under the superseded contract.
    from isar import predictive
    from isar.predictive import PREDICTIVE_ARTIFACT_SCHEMA
    from isar.session import certification

    assert certification.PREDICTIVE_CONTRACT is predictive.PREDICTIVE_CONTRACT
    assert PREDICTIVE_ARTIFACT_SCHEMA == "isar.master_predictive/2"
    # The SCHEMA LABEL and the CONTRACT REVISION are different things: the payload shape changed
    # under an unchanged /2 label (ADR-019), and only the revision may drive the cache bump.
    assert predictive.PREDICTIVE_CONTRACT.startswith(PREDICTIVE_ARTIFACT_SCHEMA)
    assert predictive.PREDICTIVE_CONTRACT != PREDICTIVE_ARTIFACT_SCHEMA


@pytest.mark.parametrize("temperature", [0.0, -0.0, -1e-30, -300.0])
def test_a_nonpositive_temperature_is_refused_by_the_evaluation_context(temperature):
    # The Bose factor divides by T. Finiteness alone let T = 0 escape as a ZeroDivisionError from
    # deep inside the component, and let T < 0 through as an "available" anti-Stokes explosion.
    with pytest.raises(mm.ModelManifestError) as raised:
        mm.EvaluationContext(center=0.0, gauss_fwhm=0.4, lorentz_fwhm=0.1, T=temperature)
    assert raised.value.reason_code == mm.MALFORMED_MANIFEST


@pytest.mark.parametrize("temperature", [0.5, 4.2, 100.0, 1000.0])
def test_a_positive_temperature_still_evaluates(temperature):
    context = mm.EvaluationContext(center=0.0, gauss_fwhm=0.4, lorentz_fwhm=0.1, T=temperature)
    values = mm.expected_counts(_MANIFEST, _NAMES, _DRAW, context, _GRID)
    assert np.all(np.isfinite(values)) and np.all(values >= 0.0)


def test_the_workspace_curve_reader_uses_the_same_reconstruction():
    from isar.workspace_api.evaluate import _compose_manifest

    composite, theta = _compose_manifest(_MANIFEST, None)
    assert composite.param_names() == _NAMES
    assert theta == [_MANIFEST["parameters"][name] for name in _NAMES]


# ---------------------------------------------------------------------------
# /2 production
# ---------------------------------------------------------------------------
def _pool_context(monitor, *, n_points=None, T=100.0):
    """A pooled-master ScanContext whose kept bins carry the given summed monitor."""
    monitor = np.asarray(monitor, dtype=float)
    x = np.linspace(-2.0, 6.0, monitor.size)
    reference = float(np.median(monitor))
    return ScanContext(x=x, counts=np.zeros(monitor.size), mask=np.ones(monitor.size, bool),
                       T=T, center=0.0, gauss_fwhm=0.4, lorentz_fwhm=0.1, instrument="IN8",
                       energy_status="absolute", exposure_scale=monitor / reference,
                       exposure_reference=reference, exposure_source="monitor"), reference


def _stub_models(monkeypatch, manifests, draws, names=_NAMES):
    monkeypatch.setattr(predictive_artifact, "model_manifest_of",
                        lambda result, by_hyp: manifests[result.label])
    monkeypatch.setattr(
        predictive_artifact, "bounded_parameter_draws",
        lambda result, **kwargs: (
            tuple(names), np.asarray(draws[result.label], dtype=float), "laplace_hesse",
            {"draw_weighting": "equal_weight_resampled", "draw_weights": None,
             "draw_ids": [f"draw-{index}" for index in range(len(draws[result.label]))],
             "source_draw_count": len(draws[result.label]),
             "retained_draw_count": len(draws[result.label]), "dropped_draws": [],
             "draw_mass_retained": 1.0, "draw_cap": 64,
             "thinned_from_draw_count": len(draws[result.label])}))


def _ensemble(monkeypatch, ctx, *, weights, manifests, draws, n_members=2, omega0=3.0):
    _stub_models(monkeypatch, manifests, draws)
    fits = {label: SimpleNamespace(label=label, omega0=omega0) for label in weights}
    scores = [SimpleNamespace(label=label, weight=weight) for label, weight in weights.items()]
    by_hyp = {label: object() for label in weights}
    best = SimpleNamespace(omega0=omega0)
    return _predictive_ensemble(best, scores, fits, by_hyp, ctx, seed=11,
                                n_members=n_members)


def _reevaluate(payload, exposure_ratio, grid=None):
    """Every stored matrix re-derived through the shared path at the given exposure ratio."""
    values = payload["evaluation_context"]
    context = mm.EvaluationContext(
        center=values["center_meV"], gauss_fwhm=values["gaussian_fwhm_meV"],
        lorentz_fwhm=values["lorentzian_fwhm_meV"], T=values["temperature_K"],
        quasielastic_fwhm=values["quasielastic_fwhm_meV"], exposure_ratio=exposure_ratio)
    grid = payload["energy_meV"] if grid is None else grid
    return [[list(mm.expected_counts(model["model_manifest"], model["parameter_names"],
                                     draw, context, grid))
             for draw in model["parameter_draws"]] for model in payload["models"]]


def test_stored_matrices_are_reproduced_by_the_per_energy_vector_on_a_heterogeneous_pool(
        monkeypatch):
    # EIG-0 c5: alternating per-bin summed monitor. The fitted grid is reproduced by
    # ΣM_i / fit_reference_exposure / n_members and by nothing scalar.
    ctx, reference = _pool_context([1e9, 2e9, 1e9, 2e9, 1e9])
    payload = _ensemble(monkeypatch, ctx, weights={"one-phonon": 1.0},
                        manifests={"one-phonon": _MANIFEST},
                        draws={"one-phonon": [_DRAW, [3.1, 0.35, 110.0, 5.0]]})
    basis = payload["exposure_basis"]
    assert payload["schema"] == "isar.master_predictive/2"
    assert basis["fit_reference_exposure"] == reference
    assert basis["per_energy_monitor"] == [1e9, 2e9, 1e9, 2e9, 1e9]
    assert basis["exposure_homogeneous"] is False
    assert basis["scalar_exposure_eligible"] is False
    assert basis["exposure_ratio_span"] == 2.0
    assert basis["reference_incremental_exposure"] == reference / 2

    vector = np.asarray(basis["per_energy_monitor"]) / reference / basis["n_members"]
    assert np.allclose(_reevaluate(payload, vector),
                       [model["expected_counts"] for model in payload["models"]],
                       rtol=0.0, atol=0.0)
    # The scalar path is NOT a valid reconstruction of this fitted grid, and the artifact says so.
    scalar = basis["reference_incremental_exposure"] / reference
    assert not np.allclose(_reevaluate(payload, scalar),
                           [model["expected_counts"] for model in payload["models"]])


def test_stored_matrices_also_reproduce_from_the_scalar_path_when_homogeneous(monkeypatch):
    ctx, reference = _pool_context([2e9] * 5)
    payload = _ensemble(monkeypatch, ctx, weights={"one-phonon": 1.0},
                        manifests={"one-phonon": _MANIFEST}, draws={"one-phonon": [_DRAW]})
    basis = payload["exposure_basis"]
    assert basis["exposure_homogeneous"] is True and basis["scalar_exposure_eligible"] is True
    assert basis["exposure_ratio_span"] == 1.0
    stored = [model["expected_counts"] for model in payload["models"]]
    vector = np.asarray(basis["per_energy_monitor"]) / reference / basis["n_members"]
    scalar = basis["reference_incremental_exposure"] / reference
    assert np.allclose(_reevaluate(payload, vector), stored, rtol=0.0, atol=0.0)
    assert np.allclose(_reevaluate(payload, scalar), stored, rtol=0.0, atol=1e-9)


def test_a_master_without_a_monitor_publishes_no_exposure_basis(monkeypatch):
    ctx = ScanContext(x=np.linspace(-2.0, 6.0, 5), counts=np.zeros(5), mask=np.ones(5, bool),
                      T=100.0, center=0.0, gauss_fwhm=0.4, lorentz_fwhm=0.1, instrument="IN8",
                      energy_status="absolute")
    payload = _ensemble(monkeypatch, ctx, weights={"one-phonon": 1.0},
                        manifests={"one-phonon": _MANIFEST}, draws={"one-phonon": [_DRAW]})
    basis = payload["exposure_basis"]
    assert basis["fit_reference_exposure"] is None
    assert basis["per_energy_monitor"] is None
    assert basis["scalar_exposure_eligible"] is False
    # Still a usable ranking artifact; only the arbitrary-grid evaluator refuses it.
    assert payload["models"][0]["expected_counts"]
    assert evaluate_master_predictive(payload, payload["energy_meV"], 1.0).reason_code == (
        "missing_exposure_basis")


def test_predictive_ensemble_keeps_a_null_model_with_no_modes(monkeypatch):
    ctx, _reference = _pool_context([1e9] * 5)
    null_manifest = {"schema": "isar.model_manifest/1",
                     "components": [{"type": "constant_background", "prefix": "bg"}],
                     "parameters": {"bg.BG": 4.0}}
    _stub_models(monkeypatch, {"null": null_manifest}, {"null": [[4.0], [6.0]]}, names=["bg.BG"])
    fits = {"null": SimpleNamespace(label="null", omega0=None)}
    payload = _predictive_ensemble(
        SimpleNamespace(omega0=None), [SimpleNamespace(label="null", weight=1.0)], fits,
        {"null": object()}, ctx, seed=7, n_members=2)
    model = payload["models"][0]
    assert model["omega0_draws"] == []
    # ADR-019: /2 carries no target state at all — a no-phonon model simply has no modes.
    assert model["mode_energies"] == [[], []]
    assert "target_present_by_draw" not in model and "target_qoi_draws" not in model
    assert "target_projection" not in payload
    assert model["expected_counts"] == [[2.0] * 5, [3.0] * 5]
    assert payload["diagnostics"]["usable"] is True


def test_a_model_whose_draws_do_not_map_onto_its_manifest_is_dropped_not_guessed(monkeypatch):
    ctx, _reference = _pool_context([1e9] * 5)
    manifests = {"good": _MANIFEST, "mismatched": _MANIFEST}
    _stub_models(monkeypatch, manifests, {"good": [_DRAW], "mismatched": [_DRAW]})
    real = predictive_artifact.bounded_parameter_draws

    def draws(result, **kwargs):
        names, samples, basis, diagnostics = real(result, **kwargs)
        if result.label == "mismatched":
            return ("phonon.Omega_d", "phonon.Gamma", "phonon.A", "elastic.E"), samples, \
                basis, diagnostics
        return names, samples, basis, diagnostics

    monkeypatch.setattr(predictive_artifact, "bounded_parameter_draws", draws)
    fits = {label: SimpleNamespace(label=label, omega0=3.0) for label in manifests}
    payload = _predictive_ensemble(
        SimpleNamespace(omega0=3.0),
        [SimpleNamespace(label="good", weight=0.6),
         SimpleNamespace(label="mismatched", weight=0.4)],
        fits, {label: object() for label in manifests}, ctx, seed=7, n_members=1)
    assert [model["label"] for model in payload["models"]] == ["good"]
    assert payload["completeness"]["dropped_models"] == [
        {"label": "mismatched", "original_weight": 0.4, "reason": "malformed_draw_shape"}]
    # A dropped model far above the floor makes the whole basis ineligible — fail closed.
    assert payload["completeness"]["predictive_complete_for_eig"] is False
    assert evaluate_master_predictive(
        payload, payload["energy_meV"], 1e9).reason_code == "predictive_basis_incomplete"


def test_completeness_tolerates_only_pruning_below_the_declared_floors(monkeypatch):
    ctx, _reference = _pool_context([1e9] * 5)
    manifests = {"good": _MANIFEST, "tiny": _MANIFEST}
    _stub_models(monkeypatch, manifests, {"good": [_DRAW], "tiny": [_DRAW]})
    monkeypatch.setattr(predictive_artifact, "model_manifest_of",
                        lambda result, by_hyp: None if result.label == "tiny" else _MANIFEST)
    fits = {label: SimpleNamespace(label=label, omega0=3.0) for label in manifests}
    payload = _predictive_ensemble(
        SimpleNamespace(omega0=3.0),
        [SimpleNamespace(label="good", weight=0.999),
         SimpleNamespace(label="tiny", weight=0.001)],
        fits, {label: object() for label in manifests}, ctx, seed=7, n_members=1)
    completeness = payload["completeness"]
    assert completeness["dropped_models"] == [
        {"label": "tiny", "original_weight": 0.001, "reason": "unsupported_model_manifest"}]
    assert completeness["model_weight_floor"] == 0.005
    assert completeness["max_pruned_mass"] == 0.01
    assert completeness["predictive_complete_for_eig"] is True
    assert payload["models"][0]["weight"] == 1.0
    assert payload["models"][0]["source_weight"] == 0.999


# ---------------------------------------------------------------------------
# Fingerprint sensitivity — one bit per hashed category
# ---------------------------------------------------------------------------
def _fingerprint_inputs(monkeypatch):
    ctx, _reference = _pool_context([1e9] * 5)
    payload = _ensemble(monkeypatch, ctx, weights={"one-phonon": 1.0},
                        manifests={"one-phonon": _MANIFEST}, draws={"one-phonon": [_DRAW]})
    return payload, {
        "models": payload["models"], "energy_meV": payload["energy_meV"],
        "evaluation_context": payload["evaluation_context"],
        "exposure_basis": payload["exposure_basis"]}


def test_the_basis_fingerprint_is_stable_and_covers_every_scored_input(monkeypatch):
    payload, inputs = _fingerprint_inputs(monkeypatch)
    assert payload["predictive_basis_fingerprint"] == _predictive_basis_fingerprint(**inputs)

    def perturbed(**overrides):
        return _predictive_basis_fingerprint(**{**inputs, **overrides})

    def with_model(**changes):
        return [{**inputs["models"][0], **changes}]

    baseline = payload["predictive_basis_fingerprint"]
    assert perturbed(models=with_model(label="renamed")) != baseline
    assert perturbed(models=with_model(identity_label="acoustic")) != baseline
    assert perturbed(models=with_model(source_weight=0.9)) != baseline
    assert perturbed(models=with_model(model_manifest={
        **_MANIFEST, "parameters": {**_MANIFEST["parameters"], "bg.BG": 4.5}})) != baseline
    assert perturbed(models=with_model(parameter_names=list(reversed(_NAMES)))) != baseline
    assert perturbed(models=with_model(
        parameter_draws=[[3.0, 0.3, 100.0, 4.000001]])) != baseline
    assert perturbed(models=with_model(draw_weighting="explicit_weights")) != baseline
    assert perturbed(models=with_model(draw_weights=[1.0])) != baseline
    assert perturbed(models=with_model(mode_energies=[[9.0]])) != baseline
    assert perturbed(energy_meV=[value + 1e-9 for value in inputs["energy_meV"]]) != baseline
    assert perturbed(evaluation_context={**inputs["evaluation_context"],
                                         "temperature_K": 100.5}) != baseline
    assert perturbed(exposure_basis={**inputs["exposure_basis"],
                                     "fit_reference_exposure": 1.1e9}) != baseline
    assert perturbed(exposure_basis={**inputs["exposure_basis"],
                                     "scalar_exposure_eligible": False}) != baseline



def test_the_basis_fingerprint_ignores_diagnostics_and_bookkeeping(monkeypatch):
    payload, inputs = _fingerprint_inputs(monkeypatch)
    noisy = [{**inputs["models"][0], "weight": 0.5, "draw_ids": ["renumbered"],
              "expected_counts": [[0.0] * 5], "dropped_draws": [{"id": "x", "reason": "y"}],
              "source_draw_count": 999, "draw_mass_retained": 0.5}]
    assert _predictive_basis_fingerprint(**{**inputs, "models": noisy}) == (
        payload["predictive_basis_fingerprint"])


# ---------------------------------------------------------------------------
# The pure evaluator against the normative fixture
# ---------------------------------------------------------------------------
def _artifact_from_case(contract, case):
    """One evaluable artifact from the fixture, carrying the basis fingerprint it reproduces.

    The fingerprint is not decorative: every reader recomputes it (ADR-020), so an artifact
    assembled with a placeholder hash would be refused before a single count was evaluated.
    """
    return {"schema": contract["schema"], "energy_meV": contract["energy_meV"],
            "evaluation_context": contract["evaluation_context"],
            "exposure_basis": contract["exposure_basis"],
            "completeness": contract["completeness"],
            "predictive_basis_fingerprint": case["predictive_basis_fingerprint"],
            "models": case["models"]}


def _refingerprint(artifact):
    """The same artifact with its fingerprint recomputed — for tests that legitimately mutate it."""
    return {**artifact, "predictive_basis_fingerprint": predictive_basis_fingerprint(
        models=artifact["models"], energy_meV=artifact["energy_meV"],
        evaluation_context=artifact["evaluation_context"],
        exposure_basis=artifact["exposure_basis"])}


@pytest.mark.parametrize("case_name", [
    "null_vs_one_phonon", "one_mode_vs_two_mode", "branch_hypotheses"])
@pytest.mark.parametrize("request_name", ["original", "shifted", "narrower", "coarser"])
def test_evaluator_reproduces_the_normative_fixture_matrices(case_name, request_name):
    fixture = _fixture()
    tolerance = float(fixture["absolute_tolerance"])
    contract = fixture["artifact_contract"]
    case = next(item for item in fixture["cases"] if item["name"] == case_name)
    request = next(item for item in fixture["evaluation_requests"]
                   if item["name"] == request_name)
    result = evaluate_master_predictive(
        _artifact_from_case(contract, case), request["energy_meV"],
        request["exposure_per_point"])
    assert result.available and result.schema == "isar.predictive_design_evaluation/1"
    assert list(result.energy_meV) == request["energy_meV"]
    assert result.exposure_per_point == request["exposure_per_point"]
    assert [model["label"] for model in result.models] == case["model_order"]
    for produced, expected in zip(result.models, case["models"]):
        reference = expected["expected_counts_by_request"][request_name]
        assert np.allclose(produced["expected_counts"], reference, rtol=0.0, atol=tolerance)
        assert produced["draw_ids"] == expected["draw_ids"]
        assert produced["omega0_draws"] == expected["omega0_draws"]
        assert np.all(np.asarray(produced["expected_counts"]) >= 0.0)
        assert np.all(np.isfinite(produced["expected_counts"]))


@pytest.mark.parametrize("request_name", ["original", "shifted", "narrower", "coarser"])
def test_evaluator_uses_the_scalar_ratio_on_a_heterogeneous_pool(request_name):
    # ADR-017: forward evaluation of a new uniform-exposure candidate is valid on every pool and is
    # NOT gated on homogeneity — while the pool's own stored matrices need the per-energy vector.
    fixture = _fixture()
    pool = fixture["heterogeneous_pool"]
    contract = pool["artifact_contract"]
    request = next(item for item in pool["evaluation_requests"]
                   if item["name"] == request_name)
    artifact = _artifact_from_case(contract, pool)
    assert contract["exposure_basis"]["scalar_exposure_eligible"] is False
    result = evaluate_master_predictive(
        artifact, request["energy_meV"], request["exposure_per_point"])
    assert result.available
    tolerance = float(fixture["absolute_tolerance"])
    for produced, expected in zip(result.models, pool["models"]):
        assert np.allclose(produced["expected_counts"],
                           expected["expected_counts_by_request"][request_name],
                           rtol=0.0, atol=tolerance)
    if request_name == "original":
        # Same grid, same draws, exposure = the scalar reference: still NOT the stored matrices.
        for produced, expected in zip(result.models, pool["models"]):
            assert not np.allclose(produced["expected_counts"],
                                   expected["stored_expected_counts"])


def test_the_heterogeneous_pools_stored_matrices_need_the_per_energy_vector():
    fixture = _fixture()
    pool = fixture["heterogeneous_pool"]
    contract = pool["artifact_contract"]
    basis = contract["exposure_basis"]
    values = contract["evaluation_context"]
    context = mm.EvaluationContext(
        center=values["center_meV"], gauss_fwhm=values["gaussian_fwhm_meV"],
        lorentz_fwhm=values["lorentzian_fwhm_meV"], T=values["temperature_K"],
        quasielastic_fwhm=values["quasielastic_fwhm_meV"],
        exposure_ratio=np.asarray(basis["stored_exposure_ratio"], dtype=float))
    for model in pool["models"]:
        produced = [mm.expected_counts(model["model_manifest"], model["parameter_names"],
                                       draw, context, contract["energy_meV"])
                    for draw in model["parameter_draws"]]
        assert np.allclose(produced, model["stored_expected_counts"], rtol=0.0,
                           atol=float(fixture["absolute_tolerance"]))


def test_evaluation_is_deterministic_and_order_stable():
    fixture = _fixture()
    contract = fixture["artifact_contract"]
    case = next(item for item in fixture["cases"] if item["name"] == "one_mode_vs_two_mode")
    artifact = _artifact_from_case(contract, case)
    grid = [-1.0, 0.5, 3.0, 5.5]
    first = evaluate_master_predictive(artifact, grid, 750.0)
    second = evaluate_master_predictive(artifact, grid, 750.0)
    assert first.to_dict() == second.to_dict()
    assert [model["label"] for model in first.models] == case["model_order"]
    # Reordering the artifact's models reorders the result and nothing else.
    reordered = _refingerprint({**artifact, "models": list(reversed(case["models"]))})
    flipped = evaluate_master_predictive(reordered, grid, 750.0)
    assert [model["label"] for model in flipped.models] == list(reversed(case["model_order"]))
    assert flipped.models[0]["expected_counts"] == first.models[-1]["expected_counts"]


def test_evaluation_never_interpolates_stored_counts():
    # A grid point between two stored energies must come from the components, not from the stored
    # matrix: on a peaked model the interpolated value and the evaluated value differ.
    fixture = _fixture()
    contract = fixture["artifact_contract"]
    case = next(item for item in fixture["cases"] if item["name"] == "null_vs_one_phonon")
    artifact = _artifact_from_case(contract, case)
    exposure = contract["exposure_basis"]["reference_incremental_exposure"]
    stored = case["models"][1]["expected_counts_by_request"]["original"]
    grid = contract["energy_meV"]
    midpoints = [(grid[index] + grid[index + 1]) / 2.0 for index in range(len(grid) - 1)]
    result = evaluate_master_predictive(artifact, midpoints, exposure)
    assert result.available
    interpolated = np.interp(midpoints, grid, stored[0])
    assert not np.allclose(result.models[1]["expected_counts"][0], interpolated, atol=1e-6)


@pytest.mark.parametrize("mutate,reason", [
    (lambda a: {**a, "schema": "isar.master_predictive/1"}, "artifact_schema_unsupported"),
    (lambda a: {**a, "schema": "isar.master_predictive/3"}, "artifact_schema_unsupported"),
    (lambda a: {**a, "exposure_basis": {**a["exposure_basis"],
                                        "fit_reference_exposure": None}},
     "missing_exposure_basis"),
    (lambda a: {**a, "exposure_basis": {**a["exposure_basis"],
                                        "fit_reference_exposure": 0.0}},
     "missing_exposure_basis"),
    (lambda a: {**a, "completeness": {**a["completeness"],
                                      "predictive_complete_for_eig": False}},
     "predictive_basis_incomplete"),
    (lambda a: {**a, "models": [{**a["models"][0], "parameter_draws": [[1.0, 2.0]]}]},
     "malformed_draw_shape"),
    (lambda a: {**a, "models": [{**a["models"][0], "parameter_names": ["bg.BG", "bg.BG"]}]},
     "malformed_draw_shape"),
    (lambda a: {**a, "models": [{**a["models"][0], "model_manifest": {
        **a["models"][0]["model_manifest"],
        "components": [{"type": "roton", "prefix": "roton"}]}}]},
     "unknown_component_kind"),
    # An unphysical temperature is not merely non-finite: T = 0 divides by zero inside the Bose
    # factor and escaped as a ZeroDivisionError, and T < 0 evaluated to a meaningless anti-Stokes
    # explosion that was published with available=True.
    (lambda a: {**a, "evaluation_context": {**a["evaluation_context"],
                                            "temperature_K": 0.0}},
     "artifact_schema_unsupported"),
    (lambda a: {**a, "evaluation_context": {**a["evaluation_context"],
                                            "temperature_K": -0.0}},
     "artifact_schema_unsupported"),
    (lambda a: {**a, "evaluation_context": {**a["evaluation_context"],
                                            "temperature_K": -300.0}},
     "artifact_schema_unsupported"),
])
def test_evaluator_refusals_are_stable_and_never_partial(mutate, reason):
    fixture = _fixture()
    contract = fixture["artifact_contract"]
    case = next(item for item in fixture["cases"] if item["name"] == "null_vs_one_phonon")
    result = evaluate_master_predictive(
        mutate(_artifact_from_case(contract, case)), contract["energy_meV"], 500.0)
    assert result.available is False and result.reason_code == reason
    assert result.message and result.models == ()


@pytest.mark.parametrize("grid,exposure,reason", [
    ([], 500.0, "invalid_energy_grid"),
    ([0.0, float("nan")], 500.0, "invalid_energy_grid"),
    ([0.0, 1.0], 0.0, "invalid_requested_exposure"),
    ([0.0, 1.0], -5.0, "invalid_requested_exposure"),
    ([0.0, 1.0], float("inf"), "invalid_requested_exposure"),
    ([0.0, 1.0], "many", "invalid_requested_exposure"),
])
def test_evaluator_refuses_an_unusable_request(grid, exposure, reason):
    fixture = _fixture()
    contract = fixture["artifact_contract"]
    case = next(item for item in fixture["cases"] if item["name"] == "null_vs_one_phonon")
    result = evaluate_master_predictive(_artifact_from_case(contract, case), grid, exposure)
    assert result.available is False and result.reason_code == reason


# ---------------------------------------------------------------------------
# End-to-end: a real driver run publishes /2
# ---------------------------------------------------------------------------
_TRUTH_TOKENS = ("tavi", "planted", "source_id", "analytic_model", "truth", "preset",
                 "instrument_rate", "rate_assumption", "generated_background", "event")


def _walk_strings(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from _walk_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_strings(child)
    elif isinstance(value, str):
        yield value


@pytest.fixture(scope="module")
def published_artifact(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("predictive-v2")
    run = run_builder.build_in8_run("PRED2", n_scans=2, seed0=54500, first_numor=54500,
                                    q_point=[2.25, 0.05, 0.0])
    data = tmp_path / "PRED2.numor"
    data.write_bytes(ill_in8.emit_file(run, ill_in8.card_template()))
    ws = Workspace.open(tmp_path / "ws")
    ws.root.mkdir(parents=True, exist_ok=True)
    ws.policy_path.write_text(Path(ill_in8._CARD_PATH).read_text(encoding="utf-8"),
                              encoding="utf-8")
    from isar.analyze import count_ladder

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(count_ladder.RUNG2_BUDGET_ENV, "0")   # rung 2 is not what this test is about: a zero budget leaves an escalated count search limited, unsampled
        driver.run_batch(ws, [data], M=25, detectors="det0", formats=(), workers=1,
                         errors_tier="fast")
    _card, session = ws.load()
    refs = [outcome["predictive_ref"]
            for key, entry in session.scans.items() if key.startswith("master:")
            for outcome in entry.detectors.values() if outcome.get("predictive_ref")]
    assert refs, "a pooled master must publish a predictive artifact"
    return ws, json.loads((ws.root / refs[0]).read_text(encoding="utf-8"))


def test_a_real_master_publishes_the_v2_contract(published_artifact):
    _ws, payload = published_artifact
    assert payload["schema"] == "isar.master_predictive/2"
    assert payload["energy_meV"] and len(payload["energy_meV"]) == \
        payload["exposure_basis"]["kept_point_count"]
    assert set(payload["evaluation_context"]) == {
        "center_meV", "gaussian_fwhm_meV", "lorentzian_fwhm_meV", "temperature_K",
        "quasielastic_fwhm_meV"}
    assert payload["exposure_basis"]["unit"] == "monitor_counts"
    assert payload["exposure_basis"]["request_unit"] == "number_neutrons"
    assert payload["exposure_basis"]["request_to_exposure_factor"] == 1.0
    assert payload["exposure_basis"]["fit_reference_exposure"] > 0.0
    assert "target_projection" not in payload
    assert payload["uncertainty_provenance"]["deterministic"] is True
    assert payload["predictive_basis_fingerprint"]
    assert payload["completeness"]["predictive_complete_for_eig"] in (True, False)
    assert payload["construction_complete"] == \
        payload["completeness"]["predictive_complete_for_eig"]
    assert payload["homogeneous_design_eligible"] is False
    assert payload["statistical_semantics"]["formal_bayesian_model"] is False
    assert payload["statistical_semantics_fingerprint"]
    assert payload["predictive_retained_set_fingerprint"]
    assert payload["statistical_semantics"]["authority_scopes"]["eig_shadow"] == "ineligible"
    for model in payload["models"]:
        assert model["identity_label"] == model["label"]
        assert model["draw_weighting"] == "equal_weight_resampled"
        assert len(model["parameter_draws"]) == len(model["expected_counts"])
        assert len(model["draw_ids"]) == len(model["parameter_draws"])
        assert len(model["mode_energies"]) == len(model["parameter_draws"])
        assert model["statistical_semantics"]["selection_eligibility"]
        assert model["statistical_semantics"]["particle_measure_kind"]
    assert math.isclose(sum(model["weight"] for model in payload["models"]), 1.0)


def test_a_real_master_artifact_is_self_consistent_and_evaluable(published_artifact):
    _ws, payload = published_artifact
    basis = payload["exposure_basis"]
    reference = float(basis["fit_reference_exposure"])
    vector = np.asarray(basis["per_energy_monitor"]) / reference / basis["n_members"]
    stored = [model["expected_counts"] for model in payload["models"]]
    assert np.allclose(_reevaluate(payload, vector), stored, rtol=0.0, atol=0.0)
    if basis["exposure_homogeneous"]:
        scalar = evaluate_master_predictive(
            payload, payload["energy_meV"], basis["reference_incremental_exposure"])
        assert scalar.available
        assert np.allclose([model["expected_counts"] for model in scalar.models], stored,
                           rtol=0.0, atol=1e-9)
    # A narrower, shifted, coarser grid at a different exposure stays finite and non-negative.
    grid = list(np.linspace(payload["energy_meV"][0] + 0.37,
                            payload["energy_meV"][-1] - 0.37, 17))
    result = evaluate_master_predictive(payload, grid, 3.0 * reference)
    assert result.available and len(result.models) == len(payload["models"])
    for model in result.models:
        assert np.all(np.isfinite(model["expected_counts"]))
        assert np.all(np.asarray(model["expected_counts"]) >= 0.0)


def test_a_real_master_artifact_carries_no_instrument_truth(published_artifact):
    _ws, payload = published_artifact
    for text in _walk_strings(payload):
        lowered = text.lower()
        assert not any(token in lowered for token in _TRUTH_TOKENS), text


def test_the_workspace_facade_requires_the_v2_schema(published_artifact, tmp_path):
    from isar.workspace_api import Workspace as ReadOnlyWorkspace

    ws, payload = published_artifact
    facade = ReadOnlyWorkspace.open(ws.root)
    root = ws.root / "data" / "master_predictive"
    legacy = root / "legacy.json"
    legacy.write_text(json.dumps({**payload, "schema": "isar.master_predictive/1"}),
                      encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported master predictive schema"):
        facade.master_predictive("data/master_predictive/legacy.json")
    legacy.unlink()


# ---------------------------------------------------------------------------
# ADR-020 — the shared strict validator and the fail-closed measure
# ---------------------------------------------------------------------------
def _valid_artifact():
    fixture = _fixture()
    contract = fixture["artifact_contract"]
    case = next(item for item in fixture["cases"] if item["name"] == "one_mode_vs_two_mode")
    return _artifact_from_case(contract, case)


def _with_authenticated_semantics(artifact):
    from isar.schema.statistics import (
        PREDICTIVE_SCHEMA,
        authority_scopes,
        semantics_fingerprint,
        validate_for_write,
    )

    model_semantics = [
        {
            "label": str(model["label"]),
            "particle_measure_kind": "parametric_bootstrap_sampling_distribution",
        }
        for model in artifact["models"]
    ]
    record = validate_for_write(
        {
            "schema": PREDICTIVE_SCHEMA,
            "artifact_kind": "master_predictive",
            "models": model_semantics,
            "authority_scopes": authority_scopes(user_diagnostic="active"),
        },
        PREDICTIVE_SCHEMA,
    )
    record["statistical_semantics_fingerprint"] = semantics_fingerprint(
        "isar.predictive_statistical_semantics/1", record
    )
    record = validate_for_write(record, PREDICTIVE_SCHEMA)
    return {
        **artifact,
        "models": [
            {**model, "statistical_semantics": model_semantics[index]}
            for index, model in enumerate(artifact["models"])
        ],
        "statistical_semantics": record,
        "statistical_semantics_fingerprint": record[
            "statistical_semantics_fingerprint"
        ],
    }


def _fixture_single_model_artifact():
    """The one-phonon case reduced to a single normalized model, so weight edits are meaningful."""
    fixture = _fixture()
    contract = fixture["artifact_contract"]
    case = next(item for item in fixture["cases"] if item["name"] == "null_vs_one_phonon")
    model = {**case["models"][1], "weight": 1.0, "source_weight": 1.0}
    return _refingerprint({**_artifact_from_case(contract, case), "models": [model]})


def test_semantics_are_authenticated_when_present_and_absence_remains_legacy_compatible():
    from isar.predictive import (
        PREDICTIVE_SEMANTICS_INVALID,
        PredictiveArtifactError,
        validate_master_predictive_artifact,
    )

    legacy = _valid_artifact()
    assert validate_master_predictive_artifact(legacy) is legacy

    authenticated = _with_authenticated_semantics(legacy)
    assert validate_master_predictive_artifact(authenticated) is authenticated

    tampered = json.loads(json.dumps(authenticated))
    tampered["statistical_semantics"]["artifact_kind"] = "target_projection"
    with pytest.raises(PredictiveArtifactError) as raised:
        validate_master_predictive_artifact(tampered)
    assert raised.value.reason_code == PREDICTIVE_SEMANTICS_INVALID

    broken_chain = json.loads(json.dumps(authenticated))
    broken_chain["models"][0]["statistical_semantics"]["particle_measure_kind"] = (
        "laplace_hesse_parameter_approximation"
    )
    with pytest.raises(PredictiveArtifactError) as raised:
        validate_master_predictive_artifact(broken_chain)
    assert raised.value.reason_code == PREDICTIVE_SEMANTICS_INVALID


def test_all_three_readers_run_the_same_validator(tmp_path, monkeypatch):
    """The workspace facade, the evaluator and the drive-loop reader must agree exactly.

    Three independent subsets is how a basis one reader refuses reached another one and got scored.
    """
    from isar.drive import loop as drive_loop
    from isar.drive.isar_io import readers as drive_readers
    from isar.predictive import PredictiveArtifactError, validate_master_predictive_artifact
    from isar.workspace_api import Workspace as ReadOnlyWorkspace

    artifact = _valid_artifact()
    broken = _refingerprint({**artifact, "models": [
        {**artifact["models"][0], "draw_weighting": "guessed"}, *artifact["models"][1:]]})

    with pytest.raises(PredictiveArtifactError) as raised:
        validate_master_predictive_artifact(broken)
    assert raised.value.reason_code == "predictive_artifact_invalid"
    assert evaluate_master_predictive(
        broken, broken["energy_meV"], 500.0).reason_code == "predictive_artifact_invalid"

    root = tmp_path / "ws"
    (root / "data" / "master_predictive").mkdir(parents=True)
    root.joinpath("card.policy.toml").write_text("", encoding="utf-8")
    (root / "data" / "master_predictive" / "a.json").write_text(
        json.dumps(broken), encoding="utf-8")
    with pytest.raises(ValueError):
        ReadOnlyWorkspace.open(root).master_predictive("data/master_predictive/a.json")

    # The drive loop degrades to no-predictive rather than aborting, and says why.
    events = []
    monkeypatch.setattr(drive_readers, "log_event",
                        lambda path, kind, payload: events.append((kind, payload)))
    monkeypatch.setattr(drive_readers, "read_results_csv", lambda ws: [
        {"pooled": True, "h": 2.0, "k": 0.0, "l": 0.0, "T": 300.0,
         "predictive_ref": "data/master_predictive/a.json"}])
    monkeypatch.setattr(drive_loop, "_master_obs_from_row",
                        lambda row, calibration: {"usable": True})
    rows = drive_loop._read_master_rows(root, None)
    assert rows and "predictive" not in rows[0]
    assert [payload["reason"] for _kind, payload in events] == ["predictive_artifact_invalid"]


@pytest.mark.parametrize("payload", ["[]", '"a string"', "17", "null"],
                         ids=["array", "string", "number", "null"])
def test_a_non_object_predictive_payload_degrades_instead_of_crashing_the_loop(
        tmp_path, monkeypatch, payload):
    """Every malformed payload must leave the loop on its ordinary no-predictive path.

    The validator already refuses a non-object artifact with ``artifact_schema_unsupported`` — but
    the handler that recorded the refusal then read ``predictive.get("schema")`` off it. On a JSON
    array, string, number or ``null`` that is an AttributeError, and it escaped ``_read_master_rows``
    entirely: a file a consumer was supposed to shrug off aborted the campaign instead.
    """
    from isar.drive import loop as drive_loop
    from isar.drive.isar_io import readers as drive_readers

    root = tmp_path / "ws"
    (root / "data" / "master_predictive").mkdir(parents=True)
    (root / "data" / "master_predictive" / "a.json").write_text(payload, encoding="utf-8")

    events = []
    monkeypatch.setattr(drive_readers, "log_event",
                        lambda path, kind, event: events.append((kind, event)))
    monkeypatch.setattr(drive_readers, "read_results_csv", lambda ws: [
        {"pooled": True, "h": 2.0, "k": 0.0, "l": 0.0, "T": 300.0,
         "predictive_ref": "data/master_predictive/a.json"}])
    monkeypatch.setattr(drive_loop, "_master_obs_from_row",
                        lambda row, calibration: {"usable": True})

    rows = drive_loop._read_master_rows(root, None)

    assert rows and "predictive" not in rows[0]        # the master survives, without a basis
    assert len(events) == 1
    kind, event = events[0]
    assert kind == "exception"
    assert event["action"] == "predictive_artifact_unsupported"
    assert event["reason"] == "artifact_schema_unsupported"
    assert event["schema"] is None                     # nothing to read, and nothing raised


@pytest.mark.parametrize("mutate,reason", [
    (lambda a: {**a, "models": [{**a["models"][0], "draw_weighting": "explicit_weights"}]},
     "predictive_artifact_invalid"),                                   # weights must be present
    (lambda a: {**a, "models": [{**a["models"][0], "draw_weighting": "explicit_weights",
                                 "draw_weights": [0.0, 0.0]}]},
     "predictive_artifact_invalid"),                                   # no positive mass
    (lambda a: {**a, "models": [{**a["models"][0], "draw_weights": [1.0, 1.0]}]},
     "predictive_artifact_invalid"),                                   # equal-weight ⇒ None
    (lambda a: {**a, "models": [{**a["models"][0], "draw_ids": ["same", "same"]}]},
     "predictive_artifact_invalid"),                                   # duplicate ids
    (lambda a: {**a, "models": [{**a["models"][0], "draw_ids": ["only-one"]}]},
     "predictive_artifact_invalid"),                                   # misaligned ids
    (lambda a: {**a, "models": [{**a["models"][0], "omega0_draws": [1.0]}]},
     "malformed_draw_shape"),                                          # neither absent nor aligned
    (lambda a: {**a, "models": [{**a["models"][0], "omega0_draws": [1.0, float("nan")]}]},
     "malformed_draw_shape"),
    (lambda a: {**a, "models": [{**a["models"][0], "mode_energies": [[1.0]]}]},
     "malformed_draw_shape"),                                          # misaligned mode energies
    (lambda a: {**a, "models": [{**a["models"][0], "weight": 0.4}]},
     "predictive_artifact_invalid"),                                   # weights not normalized
    (lambda a: {**a, "exposure_basis": {key: value for key, value
                                        in a["exposure_basis"].items()
                                        if key != "request_to_exposure_factor"}},
     "predictive_artifact_invalid"),
    (lambda a: {**a, "exposure_basis": {**a["exposure_basis"], "kept_point_count": 99}},
     "predictive_artifact_invalid"),
    (lambda a: {**a, "evaluation_context": {**a["evaluation_context"],
                                            "gaussian_fwhm_meV": 0.0}},
     "artifact_schema_unsupported"),
    (lambda a: {**a, "evaluation_context": {**a["evaluation_context"],
                                            "lorentzian_fwhm_meV": -0.1}},
     "artifact_schema_unsupported"),
    (lambda a: {**a, "evaluation_context": {**a["evaluation_context"],
                                            "quasielastic_fwhm_meV": 0.0}},
     "artifact_schema_unsupported"),
    (lambda a: {**a, "models": [{**a["models"][0], "model_manifest": {
        **a["models"][0]["model_manifest"],
        "components": [{"type": "phonon_dho", "prefix": "phonon", "peak": "magnon"},
                       {"type": "constant_background", "prefix": "bg"}]}}]},
     "artifact_schema_unsupported"),                                   # peak vocabulary
])
def test_the_validator_refuses_every_measure_and_bound_violation(mutate, reason):
    artifact = mutate(_fixture_single_model_artifact())
    result = evaluate_master_predictive(_refingerprint(artifact),
                                        artifact["energy_meV"], 500.0)
    assert result.available is False and result.reason_code == reason


def test_a_tampered_basis_fingerprint_refuses_even_when_everything_else_is_valid():
    artifact = {**_valid_artifact(), "predictive_basis_fingerprint": "0" * 64}
    result = evaluate_master_predictive(artifact, artifact["energy_meV"], 500.0)
    assert result.reason_code == "predictive_basis_fingerprint_mismatch"
    # A silently edited draw changes the content, so the stored hash no longer reproduces.
    edited = _valid_artifact()
    edited["models"] = [{**edited["models"][0],
                         "parameter_draws": [[value + 1e-6 for value in draw]
                                             for draw in edited["models"][0]["parameter_draws"]]},
                        *edited["models"][1:]]
    assert evaluate_master_predictive(edited, edited["energy_meV"], 500.0).reason_code == (
        "predictive_basis_fingerprint_mismatch")


def test_machine_precision_negatives_clamp_but_substantial_negatives_refuse():
    # A Poisson mean of -1e-15 is floating-point noise around zero; -1.0 is a broken model, and
    # clamping it published a plausible prediction from an impossible one.
    manifest = {"schema": "isar.model_manifest/1",
                "components": [{"type": "constant_background", "prefix": "bg"}],
                "parameters": {"bg.BG": 1.0}}
    clamped = mm.expected_counts(manifest, ["bg.BG"], [-1e-15], _CONTEXT, _GRID)
    assert np.all(clamped == 0.0)
    with pytest.raises(mm.ModelManifestError) as raised:
        mm.expected_counts(manifest, ["bg.BG"], [-1.0], _CONTEXT, _GRID)
    assert raised.value.reason_code == "negative_prediction"
    assert mm.NEGATIVE_PREDICTION_EPS == 1e-9


def test_the_negative_prediction_tolerance_is_hashed_with_the_evaluator_version(monkeypatch):
    payload, inputs = _fingerprint_inputs(monkeypatch)
    baseline = payload["predictive_basis_fingerprint"]
    monkeypatch.setattr(mm, "NEGATIVE_PREDICTION_EPS", 1e-6)
    assert predictive_basis_fingerprint(**inputs) != baseline


def test_the_negative_prediction_verdict_is_a_property_of_the_model_not_the_exposure():
    """The same broken model must refuse at every exposure it is evaluated at.

    Expected counts scale linearly with the exposure ratio, and the tolerance used to be an
    ABSOLUTE 1e-9 applied to the already-scaled value. A model whose mean is -1e-8 counts per unit
    exposure therefore refused at ``reference x 1e6`` and passed at ``reference / 1e6``: a campaign
    could make an impossible prediction disappear by asking for fewer neutrons, and the surviving
    scores were computed from a clamped negative Poisson mean.
    """
    manifest = {"schema": "isar.model_manifest/1",
                "components": [{"type": "constant_background", "prefix": "bg"}],
                "parameters": {"bg.BG": 1.0}}

    def context(ratio):
        return mm.EvaluationContext(center=0.0, gauss_fwhm=0.4, lorentz_fwhm=0.1, T=300.0,
                                    exposure_ratio=ratio)

    for ratio in (1e6, 1.0, 1e-6, 1e-12):
        with pytest.raises(mm.ModelManifestError) as raised:
            mm.expected_counts(manifest, ["bg.BG"], [-1e-8], context(ratio), _GRID)
        assert raised.value.reason_code == "negative_prediction", ratio
        # And noise below the tolerance still clamps rather than refusing, at the same extremes.
        assert np.all(mm.expected_counts(
            manifest, ["bg.BG"], [-1e-15], context(ratio), _GRID) == 0.0), ratio

    # A zero exposure predicts zero everywhere; nothing there is negative.
    assert np.all(mm.expected_counts(
        manifest, ["bg.BG"], [-1e-8], context(0.0), _GRID) == 0.0)

    # The rule is part of the scoring identity, beside the value it qualifies: the same 1e-9 applied
    # absolutely and applied per unit exposure are different verdicts on the same artifact.
    assert mm.NEGATIVE_PREDICTION_EPS_RULE == "exposure_scaled"


def test_the_eps_rule_is_hashed_into_the_basis_fingerprint(monkeypatch):
    payload, inputs = _fingerprint_inputs(monkeypatch)
    baseline = payload["predictive_basis_fingerprint"]
    monkeypatch.setattr(mm, "NEGATIVE_PREDICTION_EPS_RULE", "absolute")
    assert predictive_basis_fingerprint(**inputs) != baseline


def test_the_request_to_exposure_factor_is_applied_not_merely_recorded():
    # exposure_per_point is in REQUEST units; the fitted amplitudes are in exposure-basis units.
    artifact = _fixture_single_model_artifact()
    reference = float(artifact["exposure_basis"]["fit_reference_exposure"])
    identity = evaluate_master_predictive(artifact, artifact["energy_meV"], reference)
    assert identity.available and math.isclose(identity.exposure_ratio, 1.0)

    scaled = _refingerprint({**artifact, "exposure_basis": {
        **artifact["exposure_basis"], "request_to_exposure_factor": 4.0}})
    result = evaluate_master_predictive(scaled, scaled["energy_meV"], reference)
    assert result.available and math.isclose(result.exposure_ratio, 4.0)
    assert np.allclose(result.models[0]["expected_counts"],
                       4.0 * np.asarray(identity.models[0]["expected_counts"]))

    bad = _refingerprint({**artifact, "exposure_basis": {
        **artifact["exposure_basis"], "request_to_exposure_factor": 0.0}})
    assert evaluate_master_predictive(bad, bad["energy_meV"], reference).reason_code == (
        "predictive_artifact_invalid")


def test_every_fingerprinted_field_is_structurally_validated_first():
    # A field the fingerprint hashes but the validator never checks surfaces as an unexplained
    # "the hash moved" instead of "this field is missing" — the exact confusion ADR-020 removes.
    artifact = _fixture_single_model_artifact()
    for field in ("identity_label", "source_weight"):
        stripped = {**artifact, "models": [
            {key: value for key, value in artifact["models"][0].items() if key != field}]}
        result = evaluate_master_predictive(stripped, stripped["energy_meV"], 500.0)
        assert result.reason_code == "predictive_artifact_invalid", field
        assert field in result.message


def test_a_published_artifact_still_fingerprints_after_a_json_round_trip(tmp_path, monkeypatch):
    # The producer hashes live Python objects; every reader hashes the JSON it read back. If those
    # two disagreed, every artifact on disk would refuse with a fingerprint mismatch.
    ctx, _reference = _pool_context([1e9] * 5)
    payload = _ensemble(monkeypatch, ctx, weights={"one-phonon": 1.0},
                        manifests={"one-phonon": _MANIFEST},
                        draws={"one-phonon": [_DRAW, [3.1, 0.35, 110.0, 5.0]]})
    path = tmp_path / "artifact.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":")), encoding="utf-8")
    reloaded = json.loads(path.read_text(encoding="utf-8"))
    assert predictive_basis_fingerprint(
        models=reloaded["models"], energy_meV=reloaded["energy_meV"],
        evaluation_context=reloaded["evaluation_context"],
        exposure_basis=reloaded["exposure_basis"]) == payload["predictive_basis_fingerprint"]
    assert evaluate_master_predictive(
        reloaded, reloaded["energy_meV"],
        reloaded["exposure_basis"]["reference_incremental_exposure"]).available

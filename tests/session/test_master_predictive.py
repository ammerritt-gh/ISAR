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
from isar.predictive import evaluate_master_predictive
from isar.session import driver
from isar.session.driver import (_bounded_parameter_draws, _persist_master_predictive,
                                 _predictive_basis_fingerprint, _predictive_ensemble,
                                 _predictive_score)
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
    assert set(driver._DRAW_WEIGHTING) == {
        "nested_sampling", "parametric_bootstrap", "laplace_hesse"}
    assert set(driver._DRAW_WEIGHTING.values()) == {"equal_weight_resampled"}
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
    from isar.predictive import PREDICTIVE_ARTIFACT_SCHEMA
    from isar.session import certification

    assert driver.PREDICTIVE_CONTRACT is certification.PREDICTIVE_CONTRACT
    assert driver.PREDICTIVE_CONTRACT is PREDICTIVE_ARTIFACT_SCHEMA
    assert PREDICTIVE_ARTIFACT_SCHEMA == "isar.master_predictive/2"


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
                       exposure_reference=reference), reference


def _stub_models(monkeypatch, manifests, draws, names=_NAMES):
    monkeypatch.setattr(driver, "_model_manifest_of",
                        lambda result, by_hyp: manifests[result.label])
    monkeypatch.setattr(
        driver, "_bounded_parameter_draws",
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


def test_predictive_ensemble_keeps_a_null_model_without_a_target_projection(monkeypatch):
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
    assert model["target_present_by_draw"] == [False, False]
    assert model["target_qoi_draws"] == [None, None]
    assert model["expected_counts"] == [[2.0] * 5, [3.0] * 5]
    assert payload["target_projection"]["complete"] is False
    assert payload["target_projection"]["reason"] == "no_reference_target_energy"
    assert payload["diagnostics"]["usable"] is True


def test_a_model_whose_draws_do_not_map_onto_its_manifest_is_dropped_not_guessed(monkeypatch):
    ctx, _reference = _pool_context([1e9] * 5)
    manifests = {"good": _MANIFEST, "mismatched": _MANIFEST}
    _stub_models(monkeypatch, manifests, {"good": [_DRAW], "mismatched": [_DRAW]})
    real = driver._bounded_parameter_draws

    def draws(result, **kwargs):
        names, samples, basis, diagnostics = real(result, **kwargs)
        if result.label == "mismatched":
            return ("phonon.Omega_d", "phonon.Gamma", "phonon.A", "elastic.E"), samples, \
                basis, diagnostics
        return names, samples, basis, diagnostics

    monkeypatch.setattr(driver, "_bounded_parameter_draws", draws)
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
    monkeypatch.setattr(driver, "_model_manifest_of",
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
        "exposure_basis": payload["exposure_basis"],
        "target_projection": payload["target_projection"]}


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
    assert perturbed(models=with_model(target_present_by_draw=[False])) != baseline
    assert perturbed(models=with_model(target_qoi_draws=[9.0])) != baseline
    assert perturbed(energy_meV=[value + 1e-9 for value in inputs["energy_meV"]]) != baseline
    assert perturbed(evaluation_context={**inputs["evaluation_context"],
                                         "temperature_K": 100.5}) != baseline
    assert perturbed(exposure_basis={**inputs["exposure_basis"],
                                     "fit_reference_exposure": 1.1e9}) != baseline
    assert perturbed(exposure_basis={**inputs["exposure_basis"],
                                     "scalar_exposure_eligible": False}) != baseline
    assert perturbed(target_projection={**inputs["target_projection"], "qoi": "gamma"}) != baseline


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
    return {"schema": contract["schema"], "energy_meV": contract["energy_meV"],
            "evaluation_context": contract["evaluation_context"],
            "exposure_basis": contract["exposure_basis"],
            "completeness": contract["completeness"],
            "target_projection": contract["target_projection"],
            "predictive_basis_fingerprint": "fixture",
            "models": case["models"]}


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
    reordered = {**artifact, "models": list(reversed(case["models"]))}
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
    assert payload["target_projection"]["schema"] == "isar.target_projection/1"
    assert payload["uncertainty_provenance"]["deterministic"] is True
    assert payload["predictive_basis_fingerprint"]
    assert payload["completeness"]["predictive_complete_for_eig"] in (True, False)
    for model in payload["models"]:
        assert model["identity_label"] == model["label"]
        assert model["draw_weighting"] == "equal_weight_resampled"
        assert len(model["parameter_draws"]) == len(model["expected_counts"])
        assert len(model["draw_ids"]) == len(model["parameter_draws"])
        assert len(model["target_present_by_draw"]) == len(model["parameter_draws"])
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

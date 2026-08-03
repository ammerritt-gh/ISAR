"""EIG-4 Slice 4 pure full-vector Poisson information scorer."""
from __future__ import annotations

import ast
from dataclasses import replace
import json
import math
from pathlib import Path
import runpy
from types import SimpleNamespace

import numpy as np
import pytest
from scipy.special import gammaln, logsumexp

import isar.eig.scorer as scorer_module
from isar.eig import (
    CandidateEvaluationInput,
    ScalarQoIDiscretization,
    build_scan_window_design_measure,
    score_scan_window_candidates,
    validate_scan_window_design_measure,
)
from isar.eig.design_measure import (
    _DesignModel,
    _PredictiveScenario,
    _finish,
    _predictive_scenario_id,
    _source_draw_identity_fingerprint,
)
from isar.predictive import PredictiveDesignEvaluation, evaluate_master_predictive
from isar.schema.statistics import semantics_fingerprint


def _fp(name: str) -> str:
    return semantics_fingerprint("tests.eig.scorer", {"name": name})


def _measure(
        *,
        qoi_values=(0.0, 1.0),
        draw_ids=None,
        unavailable_draw_ids=()):
    draw_ids = draw_ids or tuple(f"draw-{index}" for index in range(
        len(qoi_values) + len(unavailable_draw_ids)))
    source_draw_weight = 1.0 / len(draw_ids)
    conditional_weight = 1.0 / len(qoi_values)
    predictive_basis = _fp("predictive-basis")
    source_records = [{"model_label": "model", "source_draw_ids": list(draw_ids)}]
    source_fingerprint = _source_draw_identity_fingerprint(source_records)
    scenarios = tuple(
        _PredictiveScenario(
            scenario_id=_predictive_scenario_id(predictive_basis, "model", draw_id),
            model_label="model",
            identity_label="model-identity",
            source_draw_id=draw_id,
            qoi_value=float(qoi_value),
            source_weight=1.0,
            published_predictive_weight=1.0,
            retained_predictive_weight=1.0,
            source_draw_weight=source_draw_weight,
            preconditioning_scenario_mass=source_draw_weight,
            conditional_scenario_weight=conditional_weight,
        )
        for draw_id, qoi_value in zip(draw_ids, qoi_values)
    )
    model = _DesignModel(
        label="model",
        identity_label="model-identity",
        source_weight=1.0,
        published_predictive_weight=1.0,
        retained_predictive_weight=1.0,
        design_ensemble_weight=1.0,
        draw_basis="parametric_bootstrap",
        draw_weighting="equal_weight_resampled",
        source_draw_count=len(draw_ids),
        retained_source_draw_count=len(draw_ids),
        conditional_scenario_count=len(scenarios),
        source_target_qoi_available_mass=len(scenarios) / len(draw_ids),
        source_target_qoi_unavailable_mass=len(unavailable_draw_ids) / len(draw_ids),
    )
    eligibility = SimpleNamespace(
        contract="isar.eig_eligibility/o36.2.1",
        status="eligible",
        scientific_codes=(),
        observability_codes=(),
        input_fingerprint=_fp("eligibility-input"),
        result_fingerprint=_fp("eligibility-result"),
    )
    source_identities = {
        "predictive_basis_fingerprint": predictive_basis,
        "predictive_retained_set_fingerprint": _fp("retained"),
        "predictive_statistical_semantics_fingerprint": _fp("predictive-semantics"),
        "source_model_selection_fingerprint": _fp("selection"),
        "target_projection_set_fingerprint": _fp("projection-set"),
        "projection_statistical_semantics_fingerprint": _fp("projection-semantics"),
        "projection_authority_fingerprint": _fp("projection-authority"),
        "qoi_evidence_fingerprint": _fp("qoi-evidence"),
    }
    return _finish(
        eligibility=eligibility,
        status="available",
        qoi={
            "id": "excitation.energy",
            "unit": "meV",
            "source_qoi": "omega0",
            "conditioning": "authenticated_target_qoi_available",
        },
        source_identities=source_identities,
        source_draw_identity_fingerprint=source_fingerprint,
        models=(model,),
        scenarios=scenarios,
    )


def _evaluation(measure, rates, *, energy=(-1.0, 0.0, 1.0), exposure=10.0,
                draw_ids=None, available=True, reason_code=None):
    draw_ids = draw_ids or tuple(
        f"draw-{index}" for index in range(len(rates)))
    return PredictiveDesignEvaluation(
        available=available,
        reason_code=reason_code,
        message="source refusal" if reason_code else None,
        energy_meV=tuple(energy),
        exposure_per_point=exposure,
        exposure_ratio=1.0,
        predictive_basis_fingerprint=dict(measure.source_identities)[
            "predictive_basis_fingerprint"],
        models=({
            "label": "model",
            "identity_label": "model-identity",
            "weight": 1.0,
            "draw_basis": "parametric_bootstrap",
            "draw_weighting": "equal_weight_resampled",
            "draw_weights": None,
            "draw_ids": list(draw_ids),
            "expected_counts": [list(row) for row in rates],
        },),
    )


def _discretization(*, offsets=(0.0,), width=0.5):
    return ScalarQoIDiscretization(
        qoi_id="excitation.energy",
        qoi_unit="meV",
        origin=0.0,
        bin_width=width,
        offsets=offsets,
    )


def _score(measure, evaluations, **kwargs):
    candidates = [CandidateEvaluationInput(candidate_id, evaluation)
                  for candidate_id, evaluation in evaluations]
    return score_scan_window_candidates(
        measure,
        candidates,
        qoi_discretization=kwargs.pop("qoi_discretization", _discretization()),
        seed=kwargs.pop("seed", 17),
        simulation_count=kwargs.pop("simulation_count", 128),
        block_count=kwargs.pop("block_count", 8),
        **kwargs,
    )


def _refinish_measure(measure, *, models=None, scenarios=None):
    return _finish(
        eligibility=SimpleNamespace(**dict(measure.eligibility)),
        status="available",
        qoi=dict(measure.qoi),
        source_identities=dict(measure.source_identities),
        mass_diagnostics=dict(measure.mass_diagnostics),
        excluded_models=measure.excluded_models,
        source_draw_identity_fingerprint=measure.source_draw_identity_fingerprint,
        models=measure._models if models is None else tuple(models),
        scenarios=(measure._predictive_scenarios
                   if scenarios is None else tuple(scenarios)),
    )


def _refinish_score_set(score_set, candidate):
    return scorer_module._finish_score_set(
        status=score_set.status,
        reason_codes=score_set.reason_codes,
        measure_fingerprint=score_set.design_measure_fingerprint,
        source_draw_identity_fingerprint=score_set.source_draw_identity_fingerprint,
        discretization_fingerprint=score_set.qoi_discretization_fingerprint,
        seed=score_set.seed,
        simulation_count=score_set.simulation_count,
        block_count=score_set.block_count,
        backend=dict(score_set.numerical_backend),
        candidate_scores=(candidate,),
        comparisons=score_set._pairwise_comparisons,
        diagnostic_values=dict(score_set.diagnostics),
    )


def test_design_measure_read_gate_returns_same_instance_and_rejects_stale_content():
    measure = _measure()
    assert validate_scan_window_design_measure(measure) is measure

    stale = replace(measure)
    scenarios = list(stale._predictive_scenarios)
    scenarios[0] = replace(scenarios[0], qoi_value=99.0)
    object.__setattr__(stale, "_predictive_scenarios", tuple(scenarios))
    with pytest.raises(ValueError, match="fingerprint"):
        validate_scan_window_design_measure(stale)
    with pytest.raises(TypeError):
        validate_scan_window_design_measure(measure.to_dict())


@pytest.mark.parametrize("tamper", [
    "scenario_weight",
    "model_weight",
    "scenario_id",
    "source_draw_id",
    "source_identity",
    "authority",
    "measure_fingerprint",
])
def test_design_measure_read_gate_rejects_stale_authenticated_fields(tamper):
    measure = replace(_measure())
    if tamper in {"scenario_weight", "scenario_id", "source_draw_id"}:
        scenarios = list(measure._predictive_scenarios)
        if tamper == "scenario_weight":
            scenarios[0] = replace(scenarios[0], conditional_scenario_weight=0.4)
        elif tamper == "scenario_id":
            scenarios[0] = replace(scenarios[0], scenario_id="0" * 64)
        else:
            scenarios[0] = replace(scenarios[0], source_draw_id="changed-draw")
        object.__setattr__(measure, "_predictive_scenarios", tuple(scenarios))
    elif tamper == "model_weight":
        models = list(measure._models)
        models[0] = replace(models[0], design_ensemble_weight=0.9)
        object.__setattr__(measure, "_models", tuple(models))
    elif tamper == "source_identity":
        object.__setattr__(measure, "source_draw_identity_fingerprint", "1" * 64)
    elif tamper == "authority":
        object.__setattr__(measure, "authority_scopes", (
            ("acquisition_selection", "ineligible"),
            ("eig_authoritative", "ineligible"),
            ("eig_shadow", "diagnostic"),
        ))
    else:
        object.__setattr__(measure, "design_measure_fingerprint", "2" * 64)
    with pytest.raises(ValueError):
        validate_scan_window_design_measure(measure)


@pytest.mark.parametrize("tamper", [
    "preconditioning_mass",
    "conditional_weight",
    "equal_draw_weight",
    "retained_model_normalization",
])
def test_design_measure_read_gate_rejects_refingerprinted_mass_semantics(tamper):
    base = _measure()
    models = list(base._models)
    scenarios = list(base._predictive_scenarios)
    if tamper == "preconditioning_mass":
        scenarios[0] = replace(
            scenarios[0], preconditioning_scenario_mass=0.4,
            conditional_scenario_weight=0.4)
        scenarios[1] = replace(
            scenarios[1], preconditioning_scenario_mass=0.6,
            conditional_scenario_weight=0.6)
    elif tamper == "conditional_weight":
        scenarios[0] = replace(scenarios[0], conditional_scenario_weight=0.4)
        scenarios[1] = replace(scenarios[1], conditional_scenario_weight=0.6)
    elif tamper == "equal_draw_weight":
        scenarios[0] = replace(
            scenarios[0], source_draw_weight=0.4,
            preconditioning_scenario_mass=0.4,
            conditional_scenario_weight=0.4)
        scenarios[1] = replace(
            scenarios[1], source_draw_weight=0.6,
            preconditioning_scenario_mass=0.6,
            conditional_scenario_weight=0.6)
    else:
        models[0] = replace(models[0], retained_predictive_weight=0.9)
        scenarios = [
            replace(
                scenario,
                retained_predictive_weight=0.9,
                preconditioning_scenario_mass=0.45,
            )
            for scenario in scenarios
        ]
    refingerprinted = _refinish_measure(base, models=models, scenarios=scenarios)
    assert refingerprinted.design_measure_fingerprint != base.design_measure_fingerprint
    with pytest.raises(ValueError, match="mass|weight"):
        validate_scan_window_design_measure(refingerprinted)


def test_authentic_unavailable_measure_reaches_global_unavailable_code():
    available = _measure()
    eligibility = SimpleNamespace(
        contract="isar.eig_eligibility/o36.2.1",
        status="ineligible",
        scientific_codes=("target_not_confirmed",),
        observability_codes=(),
        input_fingerprint=_fp("ineligible-input"),
        result_fingerprint=_fp("ineligible-result"),
    )
    unavailable = _finish(eligibility=eligibility)
    assert validate_scan_window_design_measure(unavailable) is unavailable
    result = _score(unavailable, [("candidate", _evaluation(
        available, ([2.0, 2.0, 2.0], [4.0, 4.0, 4.0])))])
    assert result.status == "unavailable"
    assert result.reason_codes == ("design_measure_unavailable",)
    assert result.design_measure_fingerprint == unavailable.design_measure_fingerprint
    assert result.source_draw_identity_fingerprint is None
    assert result.qoi_discretization_fingerprint is None

    late_unavailable = _finish(
        eligibility=SimpleNamespace(**dict(available.eligibility)),
        construction_codes=("conditional_target_qoi_ensemble_empty",),
        qoi=dict(available.qoi),
        source_identities=dict(available.source_identities),
        source_draw_identity_fingerprint=available.source_draw_identity_fingerprint,
    )
    assert validate_scan_window_design_measure(late_unavailable) is late_unavailable
    late_result = _score(late_unavailable, [("candidate", _evaluation(
        available, ([2.0, 2.0, 2.0], [4.0, 4.0, 4.0])))])
    assert late_result.design_measure_fingerprint == late_unavailable.design_measure_fingerprint
    assert (late_result.source_draw_identity_fingerprint
            == late_unavailable.source_draw_identity_fingerprint)
    assert late_result.qoi_discretization_fingerprint is None


def test_global_refusal_records_only_identities_authenticated_before_failure():
    measure = _measure()
    evaluation = _evaluation(measure, ([2.0, 2.0, 2.0], [4.0, 4.0, 4.0]))
    stale_measure = replace(measure)
    object.__setattr__(stale_measure, "design_measure_fingerprint", "a" * 64)
    object.__setattr__(stale_measure, "source_draw_identity_fingerprint", "b" * 64)
    invalid_measure = _score(stale_measure, [("candidate", evaluation)])
    assert invalid_measure.reason_codes == ("design_measure_invalid",)
    assert invalid_measure.design_measure_fingerprint is None
    assert invalid_measure.source_draw_identity_fingerprint is None
    assert invalid_measure.qoi_discretization_fingerprint is None

    stale_discretization = replace(_discretization())
    object.__setattr__(stale_discretization, "origin", 0.1)
    invalid_discretization = _score(
        measure,
        [("candidate", evaluation)],
        qoi_discretization=stale_discretization,
    )
    assert invalid_discretization.reason_codes == ("qoi_discretization_invalid",)
    assert invalid_discretization.design_measure_fingerprint == measure.design_measure_fingerprint
    assert (invalid_discretization.source_draw_identity_fingerprint
            == measure.source_draw_identity_fingerprint)
    assert invalid_discretization.qoi_discretization_fingerprint is None

    discretization = _discretization()
    invalid_configuration = _score(
        measure,
        [("candidate", evaluation)],
        qoi_discretization=discretization,
        simulation_count=1,
        block_count=2,
    )
    assert invalid_configuration.reason_codes == ("simulation_configuration_invalid",)
    assert invalid_configuration.design_measure_fingerprint == measure.design_measure_fingerprint
    assert (invalid_configuration.source_draw_identity_fingerprint
            == measure.source_draw_identity_fingerprint)
    assert (invalid_configuration.qoi_discretization_fingerprint
            == discretization.discretization_fingerprint)


def test_score_set_read_gate_rejects_impossible_empty_complete_set():
    forged = scorer_module._finish_score_set(
        status="complete",
        reason_codes=(),
        measure_fingerprint=_fp("measure"),
        source_draw_identity_fingerprint=_fp("source-draws"),
        discretization_fingerprint=_fp("discretization"),
        seed=7,
        simulation_count=8,
        block_count=2,
        backend=scorer_module._backend_identities(),
    )

    with pytest.raises(ValueError, match="must contain only available candidates"):
        scorer_module.validate_scan_window_score_set(forged)


def test_real_producer_chain_joins_arbitrary_grid_evaluation(monkeypatch):
    helpers = runpy.run_path(str(Path(__file__).with_name("test_design_measure.py")))
    artifact, _projection, _qoi, eligibility = helpers["_bundle"](monkeypatch)
    measure = build_scan_window_design_measure(artifact, eligibility)
    evaluation = evaluate_master_predictive(
        artifact,
        energy_meV=(-1.5, 0.0, 2.5, 4.0),
        exposure_per_point=10.0,
    )
    result = _score(measure, [("producer-grounded", evaluation)])
    assert measure.status == "available"
    assert evaluation.available is True
    assert result.status == "complete"
    assert result.candidate_scores[0]["candidate_evaluation_fingerprint"]


@pytest.mark.parametrize("kwargs", [
    {"offsets": ()},
    {"offsets": (0.0, 0.0)},
    {"offsets": (-0.1,)},
    {"offsets": (0.5,)},
    {"width": 0.0},
])
def test_discretization_rejects_invalid_width_or_offsets(kwargs):
    with pytest.raises(ValueError):
        _discretization(**kwargs)


def test_discretization_offsets_are_canonical_and_fingerprinted():
    first = _discretization(offsets=(0.25, 0.0))
    second = _discretization(offsets=(0.0, 0.25))
    assert first.offsets == (0.0, 0.25)
    assert first.to_dict() == second.to_dict()
    assert first.discretization_fingerprint != _discretization(width=0.6).discretization_fingerprint


def test_discretization_signed_zero_is_canonical_and_read_gate_enforced():
    positive = ScalarQoIDiscretization(
        qoi_id="excitation.energy", qoi_unit="meV",
        origin=0.0, bin_width=0.5, offsets=(0.0, 0.25))
    negative = ScalarQoIDiscretization(
        qoi_id="excitation.energy", qoi_unit="meV",
        origin=-0.0, bin_width=0.5, offsets=(-0.0, 0.25))
    assert positive.to_dict() == negative.to_dict()
    assert positive.discretization_fingerprint == negative.discretization_fingerprint
    assert math.copysign(1.0, negative.origin) == 1.0
    assert math.copysign(1.0, negative.offsets[0]) == 1.0

    tampered = replace(positive)
    object.__setattr__(tampered, "origin", -0.0)
    with pytest.raises(ValueError, match="signed zero"):
        scorer_module._validated_discretization(tampered)


def test_one_bin_partition_is_exact_zero():
    measure = _measure(qoi_values=(0.1, 0.2))
    evaluation = _evaluation(measure, ([2.0, 3.0, 4.0], [7.0, 1.0, 1.0]))
    result = _score(
        measure,
        [("one", evaluation)],
        qoi_discretization=_discretization(width=10.0),
    )
    candidate = result.candidate_scores[0]
    assert result.status == "complete"
    assert candidate["conditional_target_qoi_information_nats"] == 0.0
    assert candidate["conditional_target_qoi_information_standard_error_nats"] == 0.0
    assert candidate["partition_information_nats"] == (0.0,)
    assert all(value == 0.0 for value in candidate["block_information_nats"])


def test_full_vector_detects_information_with_equal_total_and_centroid():
    measure = _measure()
    evaluation = _evaluation(measure, ([4.0, 2.0, 4.0], [1.0, 8.0, 1.0]))
    result = _score(measure, [("full", evaluation)], simulation_count=512)
    assert result.status == "complete"
    assert result.candidate_scores[0]["conditional_target_qoi_information_nats"] > 0.05


def test_identical_predictive_distributions_across_bins_are_numerical_zero():
    measure = _measure()
    evaluation = _evaluation(measure, ([3.0, 4.0, 5.0], [3.0, 4.0, 5.0]))
    result = _score(measure, [("same", evaluation)])
    assert result.candidate_scores[0][
        "conditional_target_qoi_information_nats"] == pytest.approx(0.0, abs=1e-14)


def test_selected_scenario_bin_conditions_each_contribution():
    scenarios = _measure().predictive_scenarios
    likelihoods = np.asarray([
        [-1.0, -4.0],
        [-3.0, -0.5],
    ])
    selected = np.asarray([0, 1])
    contributions, _ = scorer_module._information_contributions(
        likelihoods, selected, scenarios, _discretization())
    log_weights = np.log([0.5, 0.5])
    expected = np.asarray([
        likelihoods[0, 0] - logsumexp(likelihoods[0] + log_weights),
        likelihoods[1, 1] - logsumexp(likelihoods[1] + log_weights),
    ])
    assert contributions == pytest.approx(expected)


def test_zero_rate_simulation_bypasses_scipy_ppf(monkeypatch):
    measure = _measure()
    evaluation = _evaluation(measure, ([0.0, 0.0, 0.0], [0.0, 0.0, 0.0]))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("poisson.ppf must not be called for exact zero rates")

    monkeypatch.setattr(scorer_module.poisson, "ppf", forbidden)
    result = _score(measure, [("zero", evaluation)])
    assert result.status == "complete"
    assert result.candidate_scores[0]["conditional_target_qoi_information_nats"] == 0.0


@pytest.mark.parametrize("returned", [3.5, -1.0, math.inf, math.nan])
def test_poisson_ppf_backend_anomalies_refuse_candidate(monkeypatch, returned):
    measure = _measure()
    evaluation = _evaluation(measure, ([2.0, 2.0, 2.0], [3.0, 3.0, 3.0]))
    monkeypatch.setattr(
        scorer_module.poisson, "ppf",
        lambda uniforms, rates: np.full_like(uniforms, returned, dtype=float),
    )
    result = _score(measure, [("bad", evaluation)])
    assert result.status == "unavailable"
    assert result.candidate_scores[0]["reason_codes"] == ("poisson_simulation_failed",)
    assert result.candidate_scores[0]["conditional_target_qoi_information_nats"] is None


def test_nonfinite_mixture_evaluation_has_its_own_refusal_code(monkeypatch):
    measure = _measure()
    evaluation = _evaluation(measure, ([2.0, 2.0, 2.0], [3.0, 3.0, 3.0]))
    monkeypatch.setattr(
        scorer_module,
        "_likelihood_matrix",
        lambda observations, rates: np.full(
            (observations.shape[0], rates.shape[0]), math.inf),
    )
    with np.errstate(invalid="ignore"):
        result = _score(measure, [("bad", evaluation)])
    assert result.status == "unavailable"
    assert result.candidate_scores[0]["reason_codes"] == (
        "information_evaluation_failed",)


def test_integer_valued_float_ppf_output_is_accepted(monkeypatch):
    measure = _measure()
    evaluation = _evaluation(measure, ([2.0, 2.0, 2.0], [3.0, 3.0, 3.0]))
    monkeypatch.setattr(
        scorer_module.poisson, "ppf",
        lambda uniforms, rates: np.full_like(uniforms, 3.0, dtype=float),
    )
    assert _score(measure, [("good", evaluation)]).status == "complete"


def test_large_integer_valued_float_count_is_retained_without_int64_overflow(monkeypatch):
    measure = _measure()
    evaluation = _evaluation(measure, ([2.0], [3.0]), energy=(0.0,))
    prepared = scorer_module._prepare_candidate(
        CandidateEvaluationInput("large", evaluation), measure)
    selected = np.asarray([0], dtype=int)
    large = float(2 ** 63)
    monkeypatch.setattr(
        scorer_module.poisson, "ppf",
        lambda uniforms, rates: np.full_like(uniforms, large, dtype=float),
    )
    observations = scorer_module._observation_matrix(prepared, measure, selected, seed=1)
    assert observations[0, 0] == large


def test_target_unavailable_source_rows_are_authenticated_but_not_consumed():
    measure = _measure(
        qoi_values=(0.0, 1.0),
        draw_ids=("draw-0", "draw-1", "draw-2"),
        unavailable_draw_ids=("draw-2",),
    )
    first = _evaluation(measure, (
        [4.0, 2.0, 4.0],
        [1.0, 8.0, 1.0],
        [999.0, 999.0, 999.0],
    ), draw_ids=("draw-0", "draw-1", "draw-2"))
    second = replace(first, models=({
        **dict(first.models[0]),
        "expected_counts": [
            [4.0, 2.0, 4.0],
            [1.0, 8.0, 1.0],
            [0.0, 0.0, 0.0],
        ],
    },))
    scored_first = _score(measure, [("candidate", first)])
    scored_second = _score(measure, [("candidate", second)])
    assert scored_first.status == scored_second.status == "complete"
    assert (scored_first.candidate_scores[0]["candidate_evaluation_fingerprint"]
            == scored_second.candidate_scores[0]["candidate_evaluation_fingerprint"])
    assert (scored_first.candidate_scores[0]["candidate_score_fingerprint"]
            == scored_second.candidate_scores[0]["candidate_score_fingerprint"])


def test_explicitly_excluded_model_rows_are_authenticated_but_not_consumed():
    base = _measure()
    eligibility_values = dict(base.eligibility)
    eligibility = SimpleNamespace(
        contract=eligibility_values["contract"],
        status=eligibility_values["status"],
        scientific_codes=eligibility_values["scientific_codes"],
        observability_codes=eligibility_values["observability_codes"],
        input_fingerprint=eligibility_values["input_fingerprint"],
        result_fingerprint=eligibility_values["result_fingerprint"],
    )
    source_records = [
        {"model_label": "model", "source_draw_ids": ["draw-0", "draw-1"]},
        {"model_label": "excluded", "source_draw_ids": ["excluded-0"]},
    ]
    measure = _finish(
        eligibility=eligibility,
        status="available",
        qoi=dict(base.qoi),
        source_identities=dict(base.source_identities),
        source_draw_identity_fingerprint=_source_draw_identity_fingerprint(source_records),
        excluded_models=({
            "label": "excluded",
            "identity_label": "excluded-identity",
            "draw_basis": "laplace_hesse",
            "basis_class": "laplace_hesse",
            "source_weight": 0.001,
            "published_predictive_weight": 0.001,
            "reason": "nonbootstrap_source_basis",
        },),
        models=base._models,
        scenarios=base._predictive_scenarios,
    )
    evaluation = _evaluation(measure, ([4.0, 2.0, 4.0], [1.0, 8.0, 1.0]))
    excluded_model = {
        "label": "excluded",
        "identity_label": "excluded-identity",
        "weight": 0.001,
        "draw_basis": "laplace_hesse",
        "draw_weighting": "equal_weight_resampled",
        "draw_weights": None,
        "draw_ids": ["excluded-0"],
        "expected_counts": [[999.0, 999.0, 999.0]],
    }
    evaluation = replace(evaluation, models=(*evaluation.models, excluded_model))
    changed = replace(evaluation, models=(evaluation.models[0], {
        **excluded_model, "expected_counts": [[0.0, 0.0, 0.0]],
    }))
    first = _score(measure, [("candidate", evaluation)])
    second = _score(measure, [("candidate", changed)])
    assert first.status == second.status == "complete"
    assert (first.candidate_scores[0]["candidate_evaluation_fingerprint"]
            == second.candidate_scores[0]["candidate_evaluation_fingerprint"])


def test_source_identity_mismatch_refuses_candidate():
    measure = _measure()
    evaluation = _evaluation(
        measure, ([2.0, 2.0, 2.0], [4.0, 4.0, 4.0]),
        draw_ids=("wrong-0", "wrong-1"))
    result = _score(measure, [("wrong", evaluation)])
    assert result.candidate_scores[0]["reason_codes"] == ("source_draw_identity_mismatch",)


def test_invalid_candidate_is_local_and_preserves_valid_score():
    measure = _measure()
    valid = _evaluation(measure, ([2.0, 2.0, 2.0], [4.0, 4.0, 4.0]))
    invalid = _evaluation(measure, ([2.0, 2.0, 2.0], [-1.0, 4.0, 4.0]))
    result = _score(measure, [("a-valid", valid), ("b-invalid", invalid)])
    assert result.status == "partial"
    assert [candidate["status"] for candidate in result.candidate_scores] == [
        "available", "unavailable"]
    assert result.candidate_scores[1]["reason_codes"] == ("expected_counts_invalid",)
    assert dict(result.authority_scopes)["eig_shadow"] == "diagnostic"


def test_unavailable_evaluator_reason_is_preserved_separately():
    measure = _measure()
    unavailable = _evaluation(
        measure, ([2.0, 2.0, 2.0], [4.0, 4.0, 4.0]),
        available=False, reason_code="missing_exposure_basis")
    result = _score(measure, [("held", unavailable)])
    candidate = result.candidate_scores[0]
    assert candidate["reason_codes"] == ("predictive_evaluation_unavailable",)
    assert candidate["source_reason_code"] == "missing_exposure_basis"


def test_candidate_diagnostic_prose_is_not_score_set_identity():
    measure = _measure()
    invalid = _evaluation(measure, ([2.0, 2.0, 2.0], [-1.0, 4.0, 4.0]))
    first = _score(measure, [("invalid", invalid)])
    candidate = first._candidate_scores[0]
    changed_diagnostic = replace(
        candidate, diagnostics=(("message", "different diagnostic prose"),))
    second = _refinish_score_set(first, changed_diagnostic)

    assert first.to_dict()["candidate_scores"][0]["diagnostics"] != (
        second.to_dict()["candidate_scores"][0]["diagnostics"])
    assert first.score_set_fingerprint == second.score_set_fingerprint
    assert candidate.candidate_score_fingerprint == changed_diagnostic.candidate_score_fingerprint
    assert scorer_module._candidate_semantic_payload(
        candidate.to_dict(), include_fingerprint=False) == (
            scorer_module._candidate_semantic_payload(
                changed_diagnostic.to_dict(), include_fingerprint=False))

    changed_reason = replace(candidate, reason_codes=("different_structured_reason",))
    third = _refinish_score_set(first, changed_reason)
    assert third.score_set_fingerprint != first.score_set_fingerprint
    assert scorer_module._candidate_semantic_payload(
        candidate.to_dict(), include_fingerprint=False) != (
            scorer_module._candidate_semantic_payload(
                changed_reason.to_dict(), include_fingerprint=False))


@pytest.mark.parametrize(("candidate_id", "reason"), [
    (" bad", "candidate_id_invalid"),
    ("bad ", "candidate_id_invalid"),
    ("", "candidate_id_invalid"),
])
def test_candidate_id_validation_is_global(candidate_id, reason):
    measure = _measure()
    evaluation = _evaluation(measure, ([2.0, 2.0, 2.0], [4.0, 4.0, 4.0]))
    result = _score(measure, [(candidate_id, evaluation)])
    assert result.status == "unavailable"
    assert result.reason_codes == (reason,)
    assert result.candidate_scores == ()


def test_duplicate_candidate_ids_are_global_refusal():
    measure = _measure()
    evaluation = _evaluation(measure, ([2.0, 2.0, 2.0], [4.0, 4.0, 4.0]))
    result = _score(measure, [("same", evaluation), ("same", evaluation)])
    assert result.reason_codes == ("candidate_id_duplicate",)


def test_signed_zero_is_one_grid_identity_and_one_crn_stream():
    measure = _measure()
    positive = _evaluation(measure, ([2.0], [7.0]), energy=(0.0,))
    negative = _evaluation(measure, ([2.0], [7.0]), energy=(-0.0,))
    result = _score(measure, [("a", positive), ("b", negative)])
    first, second = result.candidate_scores
    pair = result.pairwise_comparisons[0]
    assert first["executed_grid_fingerprint"] == second["executed_grid_fingerprint"]
    assert first["block_information_nats"] == second["block_information_nats"]
    assert pair["delta_information_nats"] == 0.0
    assert pair["delta_information_standard_error_nats"] == 0.0

    duplicate = _evaluation(measure, ([2.0, 2.0], [7.0, 7.0]), energy=(0.0, -0.0))
    refused = _score(measure, [("duplicate", duplicate)])
    assert refused.candidate_scores[0]["reason_codes"] == ("candidate_grid_invalid",)


def test_candidate_order_and_addition_leave_existing_records_unchanged():
    measure = _measure()
    first_eval = _evaluation(measure, ([4.0, 2.0, 4.0], [1.0, 8.0, 1.0]))
    second_eval = _evaluation(measure, ([2.0, 3.0, 2.0], [4.0, 1.0, 4.0]))
    third_eval = _evaluation(measure, ([1.0, 1.0, 1.0], [5.0, 5.0, 5.0]))
    first = _score(measure, [("a", first_eval), ("b", second_eval)])
    reordered = _score(measure, [("b", second_eval), ("a", first_eval)])
    expanded = _score(measure, [("c", third_eval), ("b", second_eval), ("a", first_eval)])
    assert first.to_dict() == reordered.to_dict()
    assert first.candidate_scores == expanded.candidate_scores[:2]
    assert first.pairwise_comparisons[0] == expanded.pairwise_comparisons[0]
    assert first.score_set_fingerprint != expanded.score_set_fingerprint


def test_pairwise_delta_and_per_point_delta_use_shared_blocks():
    measure = _measure()
    short = _evaluation(measure, ([2.0], [7.0]), energy=(0.0,))
    long = _evaluation(measure, ([2.0, 2.0], [7.0, 7.0]), energy=(0.0, 1.0))
    result = _score(measure, [("a-short", short), ("b-long", long)])
    first, second = result.candidate_scores
    pair = result.pairwise_comparisons[0]
    assert pair["delta_information_nats"] == (
        second["conditional_target_qoi_information_nats"]
        - first["conditional_target_qoi_information_nats"])
    assert pair["delta_information_per_executed_point_nats"] == (
        second["information_per_executed_point_nats"]
        - first["information_per_executed_point_nats"])
    deltas = np.asarray(pair["paired_block_deltas"])
    assert pair["delta_information_standard_error_nats"] == pytest.approx(
        np.std(deltas, ddof=1) / math.sqrt(len(deltas)))


def test_seed_changes_score_set_identity_and_prefix_helpers_are_stable():
    measure = _measure()
    evaluation = _evaluation(measure, ([2.0, 2.0, 2.0], [5.0, 5.0, 5.0]))
    first = _score(measure, [("one", evaluation)], seed=1)
    second = _score(measure, [("one", evaluation)], seed=2)
    assert first.score_set_fingerprint != second.score_set_fingerprint
    short = scorer_module._scenario_schedule(measure, 1, 32)
    long = scorer_module._scenario_schedule(measure, 1, 64)
    assert np.array_equal(short, long[:32])


def test_negative_monte_carlo_estimate_is_preserved_and_fingerprinted():
    measure = _measure()
    evaluation = _evaluation(measure, ([4.0], [4.05]), energy=(0.0,))
    result = _score(
        measure,
        [("one", evaluation)],
        seed=0,
        simulation_count=8,
        block_count=2,
    )
    candidate = result.candidate_scores[0]
    assert candidate["conditional_target_qoi_information_nats"] < 0.0
    assert candidate["candidate_score_fingerprint"]
    assert json.dumps(result.to_dict(), allow_nan=False)


def test_public_output_is_immutable_finite_and_has_no_selection_or_particle_keys():
    measure = _measure()
    evaluation = _evaluation(measure, ([2.0, 2.0, 2.0], [5.0, 5.0, 5.0]))
    result = _score(measure, [("one", evaluation)])
    payload = result.to_dict()
    assert json.dumps(payload, sort_keys=True, allow_nan=False)

    def keys(value):
        if isinstance(value, dict):
            for key, item in value.items():
                yield key
                yield from keys(item)
        elif isinstance(value, list):
            for item in value:
                yield from keys(item)

    public_keys = set(keys(payload))
    assert not any("particle" in key for key in public_keys)
    assert public_keys.isdisjoint({"winner", "selected_candidate", "recommended_candidate"})
    with pytest.raises(TypeError):
        result.candidate_scores[0]["status"] = "changed"


def test_scorer_import_surface_has_no_campaign_or_qt_dependencies():
    path = Path(__file__).parents[2] / "isar" / "eig" / "scorer.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = {
        node.module for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    forbidden = ("isar.drive", "isar.report", "isar.session", "isar_gui", "PySide6")
    assert not any(name.startswith(forbidden) for name in imported)


def _exact_one_point_information(weights, rates):
    total = 0.0
    for selected, (weight, rate) in enumerate(zip(weights, rates)):
        for count in range(80):
            log_probability = count * math.log(rate) - rate - gammaln(count + 1)
            probability = math.exp(log_probability)
            mixture = math.fsum(
                other_weight * math.exp(
                    count * math.log(other_rate) - other_rate - gammaln(count + 1))
                for other_weight, other_rate in zip(weights, rates))
            total += weight * probability * math.log(math.exp(log_probability) / mixture)
    return total


@pytest.mark.slow
def test_one_point_monte_carlo_converges_to_exact_information():
    measure = _measure(qoi_values=(0.0, 0.0, 1.0, 1.0, 1.0))
    evaluation = _evaluation(
        measure, ([1.0], [1.0], [6.0], [6.0], [6.0]), energy=(0.0,))
    result = _score(
        measure,
        [("one", evaluation)],
        seed=940301,
        simulation_count=8192,
        block_count=16,
    )
    candidate = result.candidate_scores[0]
    exact = _exact_one_point_information((0.4, 0.6), (1.0, 6.0))
    error = abs(candidate["conditional_target_qoi_information_nats"] - exact)
    tolerance = max(
        5 * candidate["conditional_target_qoi_information_standard_error_nats"],
        0.01,
    )
    assert error <= tolerance

from __future__ import annotations

import copy
from types import SimpleNamespace

import numpy as np

from isar.analyze import loop, select
from isar.analyze import errors as errors_mod
from isar.drive import loop as drive_loop
from isar.predictive import predictive_basis_fingerprint
from isar.schema.statistics import (
    FIT_SELECTION_SCHEMA,
    PREDICTIVE_SCHEMA,
    authority_scopes,
    compact_for_csv,
    semantics_fingerprint,
    validate_for_write,
)
from isar.schema.steering import ParamConstraints
from isar.session import driver, predictive_artifact
from isar.workspace_api.views import FitView


def _fit_semantics(*, estimator="data_likelihood_mle", eligibility="eligible", constraints=None):
    constraints = list(constraints or [])
    return validate_for_write(
        {
            "schema": FIT_SELECTION_SCHEMA,
            "estimator_kind": estimator,
            "fit_kind": "unconstrained",
            "effective_constraints": constraints,
            "aggregate_constraint_origin": "none" if not constraints else constraints[0]["origin"],
            "selection_eligibility": eligibility,
            "selection_ineligibility_reasons": [],
            "conditioning_fingerprint": semantics_fingerprint(
                "test.conditioning", {"effective_constraints": constraints}
            ),
            "authority_scopes": authority_scopes(single_fit_delivery="active"),
        },
        FIT_SELECTION_SCHEMA,
    )


def _selection_semantics():
    fingerprints = {
        name: semantics_fingerprint(f"test.{name}", ["one", "two"])
        for name in (
            "offered_model_set_fingerprint",
            "scored_model_set_fingerprint",
            "weight_denominator_fingerprint",
            "error_attachment_set_fingerprint",
            "legacy_pool_set_fingerprint",
        )
    }
    return validate_for_write(
        {
            "schema": FIT_SELECTION_SCHEMA,
            **fingerprints,
            "model_selection_fingerprint": semantics_fingerprint(
                "test.selection", fingerprints
            ),
            "model_weight_kind": "akaike_relative_weight",
            "authority_scopes": authority_scopes(single_fit_delivery="active"),
        },
        FIT_SELECTION_SCHEMA,
    )


def test_effective_constraints_are_classified_from_actual_origins():
    constraints = ParamConstraints(
        soft={"phonon.Gamma": (0.3, 0.1)},
        hard={"phonon.A": 25.0},
        origins={
            "phonon.Gamma": "prior_predictive",
            "phonon.A": "data_dependent_rescue",
        },
    )
    problem = loop.Problem(
        composite=object(),
        costv=lambda theta: 0.0,
        x0=np.array([0.3, 25.0]),
        bounds=[(0.0, 1.0), (0.0, 100.0)],
        free_names=["phonon.Gamma", "phonon.A"],
        k=1,
        n_used=10,
        penalty=lambda theta: 0.0,
        fixed_pos=[1],
        fixed_vals=[25.0],
    )
    semantics = loop._fit_semantics_for(
        problem, SimpleNamespace(constraints=constraints), "branch_conditional"
    )
    assert semantics["estimator_kind"] == "soft_penalized_likelihood"
    assert semantics["aggregate_constraint_origin"] == "mixed"
    assert semantics["selection_eligibility"] == "ineligible_soft_penalized"
    assert [item["parameter"] for item in semantics["effective_constraints"]] == [
        "phonon.A",
        "phonon.Gamma",
    ]


def test_missing_constraint_origin_fails_soft_at_fit_producer(caplog):
    problem = loop.Problem(
        composite=object(),
        costv=lambda theta: 0.0,
        x0=np.array([0.3]),
        bounds=[(0.0, 1.0)],
        free_names=["phonon.Gamma"],
        k=1,
        n_used=10,
        penalty=lambda theta: 0.0,
    )
    result = SimpleNamespace(label="stokes+bg", fit_kind="soft_prior", fit_semantics="sentinel")
    loop._attach_fit_semantics(
        result,
        problem,
        SimpleNamespace(
            constraints=ParamConstraints(soft={"phonon.Gamma": (0.3, 0.1)})
        ),
    )
    assert result.fit_semantics is None
    assert "semantics_construction_failed" in caplog.text
    assert "missing_constraint_origin" in caplog.text


def test_selection_publishes_each_distinct_model_set_transition():
    fit_semantics = _fit_semantics()
    results = [
        SimpleNamespace(
            label="one",
            fit_semantics=fit_semantics,
            within_model={"omega0_samples": np.array([1.0, 1.1])},
        ),
        SimpleNamespace(label="two", fit_semantics=fit_semantics, within_model={}),
    ]
    scores = [
        SimpleNamespace(
            label="one", cost=10.0, k=2, aic=14.0, weight=0.8, survives=True,
            fit_semantics=fit_semantics,
        ),
        SimpleNamespace(
            label="two", cost=11.0, k=3, aic=17.0, weight=0.2, survives=True,
            fit_semantics=fit_semantics,
        ),
    ]
    selection = SimpleNamespace(scores=scores, pooled=object())
    record = driver._build_selection_semantics(
        results,
        selection,
        {
            "route": "full_two_stage",
            "multimode_route": "residual_triggered",
            "offered_labels": ["one", "two"],
            "scored_labels": ["one", "two"],
        },
        error_attachment_labels=["one", "two"],
    )
    names = (
        "offered_model_set_fingerprint",
        "scored_model_set_fingerprint",
        "weight_denominator_fingerprint",
        "error_attachment_set_fingerprint",
        "legacy_pool_set_fingerprint",
    )
    assert all(record[name] for name in names)
    assert record["legacy_pool_set_fingerprint"] == semantics_fingerprint(
        "isar.legacy_pool_set/1", ["one"]
    )
    assert record["model_selection_fingerprint"] not in {record[name] for name in names}


def test_interval_metadata_failure_does_not_remove_interval(caplog):
    result = SimpleNamespace(label="one", fit_semantics=None)
    within = {"freq_interval": (1.0, 1.1, 1.2)}
    errors_mod._attach_interval_semantics(result, within)
    assert within["freq_interval"] == (1.0, 1.1, 1.2)
    assert within["interval_semantics"] is None
    assert "semantics_construction_failed" in caplog.text


def test_pooled_interval_references_exact_selection_and_fit_conditioning():
    fit_semantics = _fit_semantics()
    selection_semantics = _selection_semantics()
    record = driver._outcome_interval_semantics(
        best=SimpleNamespace(fit_semantics=fit_semantics, within_model={}),
        selection=SimpleNamespace(
            pooled=object(), fit_selection_semantics=selection_semantics
        ),
        interval=(1.0, 1.1, 1.2),
        omega0_profile=None,
        hesse_interval=(1.0, 1.1, 1.2),
        hesse_trusted=True,
        bayes_interval=None,
    )

    pooled = record["quantities"]["interval"]
    assert pooled["legacy_pool_set_fingerprint"] == (
        selection_semantics["legacy_pool_set_fingerprint"]
    )
    assert pooled["source_model_selection_fingerprint"] == (
        selection_semantics["model_selection_fingerprint"]
    )
    assert pooled["conditioning_fingerprint"] == semantics_fingerprint(
        "isar.legacy_pooled_interval_conditioning/1",
        {
            "legacy_pool_set_fingerprint": selection_semantics[
                "legacy_pool_set_fingerprint"
            ],
            "source_model_selection_fingerprint": selection_semantics[
                "model_selection_fingerprint"
            ],
        },
    )
    hesse = record["quantities"]["hesse_interval"]
    assert hesse["estimator_kind"] == fit_semantics["estimator_kind"]
    assert hesse["effective_constraints"] == fit_semantics["effective_constraints"]
    assert hesse["conditioning_fingerprint"] == fit_semantics[
        "conditioning_fingerprint"
    ]


def test_full_session_semantics_win_over_compact_csv_and_legacy_is_none():
    constraint = {
        "parameter": "phonon.Gamma",
        "constraint_kind": "hard",
        "value": 0.2,
        "origin": "prior_predictive",
    }
    full = _fit_semantics(
        estimator="hard_conditioned_likelihood",
        eligibility="ineligible_unapproved_conditioning",
        constraints=[constraint],
    )
    compact = compact_for_csv(full)
    view = FitView.from_row(
        {"fit_selection_semantics": compact},
        outcome={"fit_selection_semantics": full},
    )
    assert view.fit_selection_semantics["effective_constraints"] == [constraint]
    csv_only = FitView.from_row({"fit_selection_semantics": compact})
    assert "effective_constraints" not in csv_only.fit_selection_semantics
    assert FitView.from_row({}).fit_selection_semantics is None


def test_predictive_semantics_exposes_heterogeneous_measures_and_bayesian_limits():
    selection_semantics = _selection_semantics()
    fit_semantics = _fit_semantics()
    prior_fingerprint = semantics_fingerprint("test.prior", {"bounds": [0.0, 1.0]})
    fits = {
        "nested": SimpleNamespace(
            fit_semantics=fit_semantics,
            within_model={"bayes_prior_fingerprint": prior_fingerprint},
        ),
        "bootstrap": SimpleNamespace(fit_semantics=fit_semantics, within_model={}),
    }
    models = [
        {"label": "nested", "source_weight": 0.6, "weight": 0.6, "draw_basis": "nested_sampling"},
        {
            "label": "bootstrap",
            "source_weight": 0.4,
            "weight": 0.4,
            "draw_basis": "parametric_bootstrap",
        },
    ]
    record, by_label = predictive_artifact._predictive_statistical_semantics(
        models, fits, selection_semantics, complete=True
    )
    assert record["schema"] == PREDICTIVE_SCHEMA
    assert record["particle_measure_kind"] == "heterogeneous_model_dependent"
    assert record["actual_particle_basis_homogeneous"] is False
    assert record["formal_bayesian_model"] is False
    assert record["homogeneous_design_eligible"] is False
    assert by_label["nested"]["prior_kind"] == "optimizer_bound_data_dependent"
    assert by_label["nested"]["prior_data_dependent"] is True
    assert by_label["nested"]["evidence_comparable"] is False
    assert by_label["nested"]["bayesian_model_average"] is False


def test_predictive_and_projection_metadata_fail_soft(caplog):
    models = [
        {"label": "one", "source_weight": 1.0, "weight": 1.0, "draw_basis": "laplace_hesse"}
    ]
    record, per_model = predictive_artifact._predictive_statistical_semantics(
        models, {"one": SimpleNamespace(fit_semantics=None)}, None, complete=True
    )
    assert record is None and per_model == {}

    artifact = {
        "models": [
            {
                "label": "one",
                "identity_label": "one",
                "weight": 1.0,
                "mode_energies": [[2.0], [2.1]],
            }
        ],
        "evaluation_context": {"gaussian_fwhm_meV": 0.4},
        "predictive_basis_fingerprint": "basis",
    }
    projection = predictive_artifact.build_target_projection(
        artifact,
        {"omega0": 2.0},
        master_revision="master",
        calibration_fingerprint="calibration",
        policy_fingerprint="policy",
    )
    assert projection["complete"] is True
    assert projection["target_projection_complete"] is True
    assert projection["statistical_semantics"] is None
    assert projection["target_projection_set_fingerprint"]
    assert "semantics_construction_failed" in caplog.text


def test_probability_like_fields_have_distinct_declared_measures():
    retained = semantics_fingerprint("test.retained", ["one", "two"])
    outcome = {
        "confidence": 0.7,
        "branch_posterior": {"acoustic": 0.8, "new": 0.2},
        "branch_weight": 0.8,
        "assignment_entropy": 0.5,
        "target_probability": 0.9,
        "identity_entropy": 0.1,
        "target_branch": "acoustic",
        "model_entropy": 0.3,
        "probability_semantics": None,
    }
    record = driver._refresh_probability_semantics(
        outcome,
        predictive_payload={"predictive_retained_set_fingerprint": retained},
    )
    quantities = record["quantities"]
    assert quantities["confidence"]["quantity_kind"] == "heuristic_score"
    assert quantities["branch_posterior"]["source_measure_semantics"] == \
        "normalized_assignment_weight"
    assert quantities["target_probability"]["source_measure_semantics"] == \
        "normalized_factor_graph_score"
    assert quantities["identity_entropy"]["conditioning"] == "known_branch"
    assert quantities["model_entropy"]["state_space"]["fingerprint"] == retained


def test_metadata_is_excluded_from_scientific_selection_and_identity_firewalls():
    base_results = [
        SimpleNamespace(
            label="one", optimizer_converged=True, cost=10.0, k=2, ndof=20,
            params={"a": 1.0}, fit_semantics=None,
        ),
        SimpleNamespace(
            label="two", optimizer_converged=True, cost=11.0, k=3, ndof=19,
            params={"a": 1.0, "b": 2.0}, fit_semantics=None,
        ),
    ]
    before = select.score_models(base_results)
    for result in base_results:
        result.fit_semantics = _fit_semantics()
    after = select.score_models(base_results)
    numeric = lambda rows: [
        (row.label, row.aic, row.aicc, row.bic, row.weight, row.survives) for row in rows
    ]
    assert numeric(before) == numeric(after)
    promotion_candidate = {
        "label": "one",
        "k": 2,
        "ndof": 20,
        "cost": 10.0,
        "cost_per_dof": 0.5,
        "n_modes": 1,
        "delta_aic": 0.0,
        "akaike_weight": 1.0,
        "usable": True,
    }
    assert driver._promotion_candidate_identity([promotion_candidate]) == (
        driver._promotion_candidate_identity(
            [
                {
                    **promotion_candidate,
                    "estimator_kind": "data_likelihood_mle",
                    "selection_eligibility": "eligible",
                    "conditioning_fingerprint": "0" * 64,
                }
            ]
        )
    )

    model = {
        "label": "one",
        "identity_label": "one",
        "source_weight": 1.0,
        "model_manifest": {"schema": "isar.model_manifest/1"},
        "parameter_names": ["x"],
        "parameter_draws": [[1.0]],
        "draw_weighting": "equal_weight_resampled",
        "draw_weights": None,
        "mode_energies": [[1.0]],
    }
    fp_before = predictive_basis_fingerprint(
        models=[model],
        energy_meV=[0.0],
        evaluation_context={"temperature_K": 1.0},
        exposure_basis={"unit": "monitor_counts"},
    )
    fp_after = predictive_basis_fingerprint(
        models=[{**model, "statistical_semantics": {"schema": PREDICTIVE_SCHEMA}}],
        energy_meV=[0.0],
        evaluation_context={"temperature_K": 1.0},
        exposure_basis={"unit": "monitor_counts"},
    )
    assert fp_before == fp_after

    observation = {
        "authority": "master:1",
        "gauss_fwhm": 0.4,
        "candidates": [{"energy_meV": 2.0, "omega0_err": 0.1}],
    }
    signature_args = {
        "target_branch": "acoustic",
        "period": 1.0,
        "hypotheses": [],
        "entropy_stop": 0.2,
        "maximum_bridge_steps": 1,
    }
    sig_before = driver._tracked_authority_signature([observation], **signature_args)
    sig_after = driver._tracked_authority_signature(
        [{**observation, "probability_semantics": {"schema": "ignored"}}],
        **signature_args,
    )
    assert sig_before == sig_after

    row = {
        "interval_lo": 1.0,
        "interval_hi": 1.2,
        "interval_reliable": True,
    }
    assert drive_loop._pick_interval(row) == drive_loop._pick_interval(
        {**row, "interval_semantics": {"schema": "ignored"}}
    )

    proposal = SimpleNamespace(
        h=1.0,
        k=0.0,
        l=0.0,
        kind="production",
        meta={"target_branch_role": "target", "authoritative_evidence_revision": "r1"},
    )
    proposal_with_semantics = copy.deepcopy(proposal)
    proposal_with_semantics.meta["statistical_semantics"] = {"schema": "ignored"}
    kwargs = {"e_lo": 1.0, "e_hi": 3.0, "e_step": 0.1, "ncount": 100}
    assert drive_loop._request_fingerprint_for(proposal, **kwargs) == \
        drive_loop._request_fingerprint_for(proposal_with_semantics, **kwargs)

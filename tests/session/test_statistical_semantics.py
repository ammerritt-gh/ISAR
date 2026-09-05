from __future__ import annotations

import copy
import inspect
from dataclasses import dataclass
from types import SimpleNamespace

import numpy as np
import pytest

from isar.analyze import loop, select
from isar.analyze import errors as errors_mod
from isar.analyze.evidence import EVIDENCE_REVISION
from isar.drive import loop as drive_loop
from isar.predictive import (
    PREDICTIVE_SEMANTICS_INVALID,
    PredictiveArtifactError,
    predictive_basis_fingerprint,
    validate_master_predictive_artifact,
)
from isar.schema.statistics import (
    FIT_SELECTION_SCHEMA,
    PREDICTIVE_SCHEMA,
    SemanticsConstructionError,
    authority_scopes,
    compact_for_csv,
    read_optional,
    semantics_fingerprint,
    validate_fit_selection_semantics,
    validate_predictive_semantics,
    validate_for_write,
)
from isar.schema.steering import ParamConstraints
from isar.session import branch_tracking, certification, driver, evidence_update, master_delivery, predictive_artifact, scan_delivery
from isar.session.state import canonical_hash, content_hash
from isar.workspace_api.views import FitView


def _fit_semantics(*, estimator="data_likelihood_mle", eligibility="eligible", constraints=None):
    constraints = list(constraints or [])
    data_fingerprint = semantics_fingerprint(
        "test.data_likelihood", {"energy": [0.0, 1.0], "counts": [2.0, 3.0]}
    )
    model_fingerprint = semantics_fingerprint(
        "test.model_parameterization", {"label": "one", "parameters": ["a"]}
    )
    constraint_fingerprint = semantics_fingerprint(
        "test.constraints", {"effective_constraints": constraints}
    )
    comparison_fingerprint = semantics_fingerprint(
        "test.comparison",
        {
            "data_likelihood_fingerprint": data_fingerprint,
            "model_parameterization_fingerprint": model_fingerprint,
            "constraint_fingerprint": constraint_fingerprint,
            "estimator_kind": estimator,
        },
    )
    return validate_for_write(
        {
            "schema": FIT_SELECTION_SCHEMA,
            "estimator_kind": estimator,
            "fit_kind": "unconstrained",
            "effective_constraints": constraints,
            "aggregate_constraint_origin": "none" if not constraints else constraints[0]["origin"],
            "selection_eligibility": eligibility,
            "selection_ineligibility_reasons": [],
            "data_likelihood_fingerprint": data_fingerprint,
            "model_parameterization_fingerprint": model_fingerprint,
            "constraint_fingerprint": constraint_fingerprint,
            "comparison_context_fingerprint": comparison_fingerprint,
            "conditioning_fingerprint": comparison_fingerprint,
            "authority_scopes": authority_scopes(single_fit_delivery="active"),
        },
        FIT_SELECTION_SCHEMA,
    )


def _v2_selection_record(_omit=(), **overrides):
    """A hand-built /2 selection record on the evidence lane; override/omit to break it."""
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
    record = {
        "schema": FIT_SELECTION_SCHEMA,
        **fingerprints,
        "model_selection_fingerprint": semantics_fingerprint(
            "test.selection", fingerprints
        ),
        "selection_method": "laplace_evidence_selection",
        "selection_fallback": None,
        "model_weight_kind": "posterior_probability",
        "evidence_arena_fingerprint": semantics_fingerprint(
            "test.arena", {"counts": [2.0, 3.0]}
        ),
        "evidence_revision": EVIDENCE_REVISION,
        "selection_eligibility": "eligible",
        "selection_ineligibility_reasons": [],
        "authority_scopes": authority_scopes(single_fit_delivery="active"),
    }
    record.update(overrides)
    for key in _omit:
        record.pop(key, None)
    return record


def _selection_semantics():
    return validate_for_write(_v2_selection_record(), FIT_SELECTION_SCHEMA)


def _fit_context(*, constraints=None, **changes):
    values = {
        "x": np.array([-1.0, 0.0, 1.0]),
        "counts": np.array([3.0, 8.0, 4.0]),
        "mask": np.array([False, False, False]),
        "exposure_scale": np.array([1.0, 1.0, 1.0]),
        "exposure_reference": 1.0,
        "T": 300.0,
        "center": 0.0,
        "gauss_fwhm": 0.4,
        "lorentz_fwhm": 0.1,
        "quasielastic_fwhm": 0.2,
        "instrument": "test",
        "energy_status": "absolute",
        "prior_omega": None,
        "acoustic_omega": None,
        "constraints": constraints,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _problem(*, composite=None, bounds=None, free_names=None, fixed_pos=None, fixed_vals=None):
    names = list(free_names or ["phonon.Gamma", "phonon.A"])
    return loop.Problem(
        composite=object() if composite is None else composite,
        costv=lambda theta: 0.0,
        x0=np.array([0.3, 25.0][:len(names)]),
        bounds=list(bounds or [(0.0, 1.0), (0.0, 100.0)][:len(names)]),
        free_names=names,
        k=len(names) - len(fixed_pos or []),
        n_used=10,
        penalty=lambda theta: 0.0,
        fixed_pos=list(fixed_pos or []),
        fixed_vals=list(fixed_vals or []),
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
    problem = _problem(fixed_pos=[1], fixed_vals=[25.0])
    semantics = loop._fit_semantics_for(
        problem, _fit_context(constraints=constraints), "branch_conditional"
    )
    assert semantics["estimator_kind"] == "soft_penalized_likelihood"
    assert semantics["aggregate_constraint_origin"] == "mixed"
    assert semantics["selection_eligibility"] == "ineligible_data_dependent_conditioning"
    # Re-baselined under H4(c): the prespecified soft prior is no longer a reason for anything —
    # only the data-dependent hard rescue is. The verdict is unchanged; its justification shrank.
    assert semantics["selection_ineligibility_reasons"] == [
        "data_dependent_or_post_selection_hard_constraint",
    ]
    assert [item["parameter"] for item in semantics["effective_constraints"]] == [
        "phonon.A",
        "phonon.Gamma",
    ]


def test_prespecified_soft_prior_is_selection_eligible_but_still_a_map_fit():
    """H4(c): a prespecified-origin soft prior IS its parameter's prior and enters lnZ exactly
    once, so it no longer costs selection eligibility.

    The estimator stays ``soft_penalized_likelihood`` with the constraint still recorded — the
    fit is a MAP, and that is what keeps it out of acquisition (see the eig eligibility test).
    This ruling covers the selection lane only."""
    constraints = ParamConstraints(soft={"phonon.Gamma": (0.3, 0.1)},
                                   origins={"phonon.Gamma": "prior_predictive"})
    semantics = loop._fit_semantics_for(
        _problem(), _fit_context(constraints=constraints), "soft_prior")

    assert semantics["selection_eligibility"] == "eligible"
    assert semantics["selection_ineligibility_reasons"] == []
    assert semantics["estimator_kind"] == "soft_penalized_likelihood"
    assert semantics["effective_constraints"]          # still recorded, never hidden


def test_data_dependent_soft_prior_stays_ineligible():
    """The other half of H4(c): a rescue prior is fit from this scan's own counts, so it is not
    prespecified and must keep costing eligibility. An unrecognised origin fails closed too."""
    for origin, expected in (("data_dependent_rescue", "ineligible_data_dependent_conditioning"),
                             ("post_selection", "ineligible_data_dependent_conditioning"),
                             ("prespecified_scientific", "eligible")):
        constraints = ParamConstraints(soft={"phonon.Gamma": (0.3, 0.1)},
                                       origins={"phonon.Gamma": origin})
        semantics = loop._fit_semantics_for(
            _problem(), _fit_context(constraints=constraints), "soft_prior")
        assert semantics["selection_eligibility"] == expected, origin


def test_user_declared_soft_prior_is_selection_eligible():
    """SCENE-1 PR-c: a scientist's pre-fit soft declaration (``user_declared``) is prespecified
    by construction, mirroring ``prespecified_scientific``/``prior_predictive`` above."""
    constraints = ParamConstraints(soft={"phonon.Gamma": (0.3, 0.1)},
                                   origins={"phonon.Gamma": "user_declared"})
    semantics = loop._fit_semantics_for(
        _problem(), _fit_context(constraints=constraints), "soft_prior")

    assert semantics["selection_eligibility"] == "eligible"
    assert semantics["estimator_kind"] == "soft_penalized_likelihood"
    assert [item["origin"] for item in semantics["effective_constraints"]] == ["user_declared"]


def test_user_declared_hard_constraint_is_unapproved_conditioning():
    """A HARD pin from ``user_declared`` is not a prior — H4(c) recast soft priors only — so it
    stays ineligible, same as any other prespecified-origin hard constraint."""
    constraints = ParamConstraints(hard={"phonon.A": 25.0},
                                   origins={"phonon.A": "user_declared"})
    problem = _problem(fixed_pos=[1], fixed_vals=[25.0])
    semantics = loop._fit_semantics_for(
        problem, _fit_context(constraints=constraints), "branch_conditional")

    assert semantics["selection_eligibility"] == "ineligible_unapproved_conditioning"


def test_any_hard_constraint_is_unapproved_conditioning_whatever_its_origin():
    """SCENE-1 PR-c hardening: eligibility is now an ANY-hard rule, not a prespecified-origin one.

    ``catalogue_variant`` is deliberately absent from ``PRESPECIFIED_SOFT_ORIGINS``, so under the
    old rule its hard pin walked through to ``eligible`` and then crashed in ``validate_for_write``
    (eligible ⇒ every effective constraint soft AND prespecified). The producer now mirrors that
    invariant, and the record it writes validates. (A literal ``"none"`` origin cannot be tested
    here: the constraint validator rejects it as a per-constraint origin.)"""
    constraints = ParamConstraints(hard={"phonon.A": 25.0},
                                   origins={"phonon.A": "catalogue_variant"})
    problem = _problem(fixed_pos=[1], fixed_vals=[25.0])
    semantics = loop._fit_semantics_for(
        problem, _fit_context(constraints=constraints), "branch_conditional")

    assert semantics["selection_eligibility"] == "ineligible_unapproved_conditioning"
    assert semantics["selection_ineligibility_reasons"] == ["unapproved_hard_constraint"]
    assert validate_for_write(semantics, FIT_SELECTION_SCHEMA) == semantics


def test_data_dependent_hard_constraint_keeps_only_its_own_reason():
    """The two hard-constraint reasons stay disjoint: a rescue pin is named by the
    data-dependent reason alone, not doubled by the ANY-hard rule above."""
    constraints = ParamConstraints(hard={"phonon.A": 25.0},
                                   origins={"phonon.A": "data_dependent_rescue"})
    problem = _problem(fixed_pos=[1], fixed_vals=[25.0])
    semantics = loop._fit_semantics_for(
        problem, _fit_context(constraints=constraints), "branch_conditional")

    assert semantics["selection_eligibility"] == "ineligible_data_dependent_conditioning"
    assert semantics["selection_ineligibility_reasons"] == [
        "data_dependent_or_post_selection_hard_constraint"]


def test_missing_constraint_origin_fails_soft_at_fit_producer(caplog):
    problem = _problem(
        free_names=["phonon.Gamma"],
        bounds=[(0.0, 1.0)],
    )
    result = SimpleNamespace(label="stokes+bg", fit_kind="soft_prior", fit_semantics="sentinel")
    loop._attach_fit_semantics(
        result,
        problem,
        _fit_context(
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
            label="one", cost=10.0, k=2, aic=14.0, weight=0.8, akaike_weight=0.8,
            posterior_probability=0.8, candidate_prior=0.5, log_evidence=-5.0,
            log_evidence_err=None, evidence_method="laplace", evidence_validity={},
            survives=True, fit_semantics=fit_semantics,
        ),
        SimpleNamespace(
            label="two", cost=11.0, k=3, aic=17.0, weight=0.2, akaike_weight=0.2,
            posterior_probability=0.2, candidate_prior=0.5, log_evidence=-6.4,
            log_evidence_err=None, evidence_method="laplace", evidence_validity={},
            survives=True, fit_semantics=fit_semantics,
        ),
    ]
    selection = SimpleNamespace(
        scores=scores, pooled=object(), selection_fallback=None, best="one",
        evidence=SimpleNamespace(
            arena_fingerprint=fit_semantics["data_likelihood_fingerprint"]
        ),
    )
    record = scan_delivery._build_selection_semantics(
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
        "isar.legacy_pool_set/1",
        [scan_delivery._model_set_record(results[0].label, results[0].fit_semantics)],
    )
    assert record["selection_eligibility"] == "eligible"
    assert record["selection_ineligibility_reasons"] == []
    assert record["model_selection_fingerprint"] not in {record[name] for name in names}
    # /2 evidence lane: the record names its authority and pins the shared arena.
    assert record["selection_method"] == "laplace_evidence_selection"
    assert record["selection_fallback"] is None
    assert record["model_weight_kind"] == "posterior_probability"
    assert record["evidence_arena_fingerprint"] == (
        fit_semantics["data_likelihood_fingerprint"]
    )
    assert record["evidence_revision"] == EVIDENCE_REVISION
    by_label = {item["label"]: item for item in record["candidate_records"]}
    assert by_label["one"]["posterior_probability"] == 0.8
    assert by_label["one"]["log_evidence"] == -5.0
    assert by_label["one"]["evidence_method"] == "laplace"
    assert by_label["one"]["candidate_prior"] == 0.5
    # And the persisted record survives a read-side validation round-trip.
    assert validate_fit_selection_semantics(record) == record


def test_v2_lane_coherence_is_validator_enforced():
    fallback = _v2_selection_record(
        _omit=("evidence_arena_fingerprint", "evidence_revision"),
        selection_method="aic_fallback",
        selection_fallback="aic_no_evidence_inputs",
        model_weight_kind="akaike_relative_weight",
    )
    assert validate_for_write(fallback, FIT_SELECTION_SCHEMA)["selection_method"] == (
        "aic_fallback"
    )
    with pytest.raises(SemanticsConstructionError) as raised:
        validate_for_write(
            _v2_selection_record(model_weight_kind="akaike_relative_weight"),
            FIT_SELECTION_SCHEMA,
        )
    assert raised.value.reason_code == "inconsistent_selection_lane"
    with pytest.raises(SemanticsConstructionError) as raised:
        validate_for_write(
            _v2_selection_record(
                _omit=("evidence_arena_fingerprint", "evidence_revision")
            ),
            FIT_SELECTION_SCHEMA,
        )
    assert raised.value.reason_code == "missing_evidence_arena_fingerprint"
    with pytest.raises(SemanticsConstructionError) as raised:
        validate_for_write(
            _v2_selection_record(_omit=("selection_method", "selection_fallback")),
            FIT_SELECTION_SCHEMA,
        )
    assert raised.value.reason_code == "missing_selection_lane_fields"
    # The arena travels with its revision: one without the other is incoherent.
    with pytest.raises(SemanticsConstructionError) as raised:
        validate_for_write(
            _v2_selection_record(_omit=("evidence_revision",)),
            FIT_SELECTION_SCHEMA,
        )
    assert raised.value.reason_code == "inconsistent_selection_lane"


def test_v2_rejects_cross_arena_candidate_records():
    candidate = {
        "label": "one",
        "cost": 10.0,
        "effective_parameter_count": 2,
        "aic": 14.0,
        "akaike_weight": 1.0,
        "log_evidence": -5.0,
        "log_evidence_err": None,
        "evidence_method": "laplace",
        "evidence_validity": {},
        "candidate_prior": 1.0,
        "posterior_probability": 1.0,
        "fit_semantics": _fit_semantics(),
    }
    with pytest.raises(SemanticsConstructionError) as raised:
        validate_for_write(
            _v2_selection_record(candidate_records=[candidate]),
            FIT_SELECTION_SCHEMA,
        )
    assert raised.value.reason_code == "evidence_arena_mismatch"
    assert validate_for_write(
        _v2_selection_record(
            candidate_records=[candidate],
            evidence_arena_fingerprint=candidate["fit_semantics"][
                "data_likelihood_fingerprint"
            ],
        ),
        FIT_SELECTION_SCHEMA,
    )
    # A MIXED set is also refused: one matching candidate must not launder another
    # arena's candidate through the single-arena guarantee.
    stranger = copy.deepcopy(candidate)
    stranger["label"] = "two"
    stranger["fit_semantics"] = dict(stranger["fit_semantics"])
    stranger["fit_semantics"]["data_likelihood_fingerprint"] = semantics_fingerprint(
        "test.other_arena", {"counts": [9.0]}
    )
    with pytest.raises(SemanticsConstructionError) as raised:
        validate_for_write(
            _v2_selection_record(
                candidate_records=[candidate, stranger],
                evidence_arena_fingerprint=candidate["fit_semantics"][
                    "data_likelihood_fingerprint"
                ],
            ),
            FIT_SELECTION_SCHEMA,
        )
    assert raised.value.reason_code == "evidence_arena_mismatch"


def test_dominated_candidate_persists_excluded_with_its_ceiling():
    fit_semantics = _fit_semantics()
    arena = fit_semantics["data_likelihood_fingerprint"]
    leader = SimpleNamespace(
        label="one", cost=10.0, k=2, aic=14.0, weight=1.0, akaike_weight=0.9,
        posterior_probability=1.0, candidate_prior=1.0, log_evidence=-5.0,
        log_evidence_err=None, evidence_method="laplace", evidence_validity={},
        survives=True, fit_semantics=fit_semantics,
    )
    dominated = SimpleNamespace(
        label="two", cost=300.0, k=7, aic=314.0, weight=0.0, akaike_weight=0.1,
        posterior_probability=0.0, candidate_prior=0.0, log_evidence=float("nan"),
        log_evidence_err=None, evidence_method="dominated",
        evidence_validity={
            "dominated_by_leader_log_evidence": -5.0,
            "prune_margin": 100.0,
        },
        survives=False, fit_semantics=fit_semantics,
    )
    results = [
        SimpleNamespace(label="one", fit_semantics=fit_semantics, within_model={}),
        SimpleNamespace(label="two", fit_semantics=fit_semantics, within_model={}),
    ]
    record = scan_delivery._build_selection_semantics(
        results,
        SimpleNamespace(
            scores=[leader, dominated], pooled=None, selection_fallback=None, best="one",
            evidence=SimpleNamespace(arena_fingerprint=arena),
        ),
        {
            "route": "full_two_stage",
            "offered_labels": ["one", "two"],
            "scored_labels": ["one", "two"],
        },
        error_attachment_labels=[],
    )
    by_label = {item["label"]: item for item in record["candidate_records"]}
    assert by_label["two"]["log_evidence"] is None
    assert by_label["two"]["evidence_method"] == "dominated"
    assert by_label["two"]["posterior_probability"] == 0.0
    assert by_label["two"]["evidence_validity"]["prune_margin"] == 100.0
    # Excluded from the posterior normalization: the denominator is the leader alone.
    assert record["weight_denominator_fingerprint"] == (
        scan_delivery._model_set_fingerprint(
            "model_weight_denominator",
            [
                {
                    **scan_delivery._model_set_record("one", fit_semantics),
                    "log_evidence": -5.0,
                }
            ],
        )
    )
    assert validate_fit_selection_semantics(record) == record


def test_evidence_lane_without_arena_fails_the_builder_soft(caplog):
    fit_semantics = _fit_semantics()
    scores = [
        SimpleNamespace(
            label="one", cost=10.0, k=2, aic=14.0, weight=1.0, akaike_weight=1.0,
            posterior_probability=1.0, candidate_prior=1.0, log_evidence=-5.0,
            log_evidence_err=None, evidence_method="laplace", evidence_validity={},
            survives=True, fit_semantics=fit_semantics,
        )
    ]
    results = [
        SimpleNamespace(label="one", fit_semantics=fit_semantics, within_model={})
    ]
    record = scan_delivery._build_selection_semantics(
        results,
        SimpleNamespace(
            scores=scores, pooled=None, selection_fallback=None, best="one", evidence=None
        ),
        {"route": "full_two_stage", "offered_labels": ["one"], "scored_labels": ["one"]},
        error_attachment_labels=[],
    )
    assert record is None
    assert "missing_evidence_arena" in caplog.text


def test_fallback_lane_records_aic_authority():
    fit_semantics = _fit_semantics()
    scores = [
        SimpleNamespace(
            label="one", cost=10.0, k=2, aic=14.0, weight=1.0, akaike_weight=1.0,
            posterior_probability=None, candidate_prior=None, log_evidence=None,
            log_evidence_err=None, evidence_method=None, evidence_validity=None,
            survives=True, fit_semantics=fit_semantics,
        )
    ]
    results = [
        SimpleNamespace(label="one", fit_semantics=fit_semantics, within_model={})
    ]
    record = scan_delivery._build_selection_semantics(
        results,
        SimpleNamespace(
            scores=scores, pooled=None, best="one",
            selection_fallback="aic_no_evidence_inputs", evidence=None,
        ),
        {"route": "full_two_stage", "offered_labels": ["one"], "scored_labels": ["one"]},
        error_attachment_labels=[],
    )
    assert record["selection_method"] == "aic_fallback"
    assert record["selection_fallback"] == "aic_no_evidence_inputs"
    assert record["model_weight_kind"] == "akaike_relative_weight"
    assert "evidence_arena_fingerprint" not in record
    assert "evidence_revision" not in record
    row = record["candidate_records"][0]
    assert row["posterior_probability"] is None
    assert row["log_evidence"] is None
    assert row["evidence_method"] is None
    assert validate_fit_selection_semantics(record) == record


def test_comparison_fingerprint_mutation_matrix(monkeypatch):
    base_problem = _problem()
    base_context = _fit_context()
    baseline = loop._fit_semantics_for(base_problem, base_context, "unconstrained")

    data_changes = (
        {"counts": np.array([3.0, 9.0, 4.0])},
        {"x": np.array([-1.1, 0.0, 1.0])},
        {"mask": np.array([False, True, False])},
        {"exposure_scale": np.array([1.0, 2.0, 1.0])},
    )
    for change in data_changes:
        changed = loop._fit_semantics_for(
            base_problem, _fit_context(**change), "unconstrained"
        )
        assert changed["data_likelihood_fingerprint"] != baseline[
            "data_likelihood_fingerprint"
        ]
        assert changed["comparison_context_fingerprint"] != baseline[
            "comparison_context_fingerprint"
        ]

    monkeypatch.setattr(loop, "DATA_LIKELIHOOD_REVISION", "test.changed_likelihood/1")
    changed_likelihood = loop._fit_semantics_for(
        base_problem, base_context, "unconstrained"
    )
    assert changed_likelihood["data_likelihood_fingerprint"] != baseline[
        "data_likelihood_fingerprint"
    ]
    monkeypatch.undo()

    component = SimpleNamespace(
        prefix="phonon.",
        params=[SimpleNamespace(name="omega0", kind="positive")],
        peak="stokes",
    )
    model_changes = (
        _problem(composite=SimpleNamespace(components=[component])),
        _problem(bounds=[(0.0, 2.0), (0.0, 100.0)]),
        _problem(
            free_names=["phonon.Gamma", "phonon.scale"],
            bounds=[(0.0, 1.0), (0.0, 100.0)],
        ),
    )
    for problem in model_changes:
        changed = loop._fit_semantics_for(problem, base_context, "unconstrained")
        assert changed["model_parameterization_fingerprint"] != baseline[
            "model_parameterization_fingerprint"
        ]
        assert changed["comparison_context_fingerprint"] != baseline[
            "comparison_context_fingerprint"
        ]

    constrained_problem = _problem(fixed_pos=[1], fixed_vals=[25.0])
    constraints = ParamConstraints(
        hard={"phonon.A": 25.0},
        origins={"phonon.A": "prior_predictive"},
    )
    changed_constraint = loop._fit_semantics_for(
        constrained_problem,
        _fit_context(constraints=constraints),
        "branch_conditional",
    )
    assert changed_constraint["constraint_fingerprint"] != baseline[
        "constraint_fingerprint"
    ]
    assert changed_constraint["comparison_context_fingerprint"] != baseline[
        "comparison_context_fingerprint"
    ]


def test_selection_fingerprints_are_order_invariant_and_route_sensitive():
    first = _fit_semantics()
    second = _fit_semantics()
    results = [
        SimpleNamespace(
            label="one",
            fit_semantics=first,
            within_model={"omega0_samples": np.array([1.0])},
        ),
        SimpleNamespace(
            label="two",
            fit_semantics=second,
            within_model={"omega0_samples": np.array([2.0])},
        ),
    ]
    evidence = SimpleNamespace(
        arena_fingerprint=first["data_likelihood_fingerprint"]
    )
    scores = [
        SimpleNamespace(
            label="one", cost=10.0, k=2, aic=14.0, weight=0.7, akaike_weight=0.7,
            posterior_probability=0.7, candidate_prior=0.5, log_evidence=-5.0,
            log_evidence_err=None, evidence_method="laplace", evidence_validity={},
            survives=True, fit_semantics=first,
        ),
        SimpleNamespace(
            label="two", cost=11.0, k=2, aic=15.0, weight=0.3, akaike_weight=0.3,
            posterior_probability=0.3, candidate_prior=0.5, log_evidence=-5.8,
            log_evidence_err=None, evidence_method="laplace", evidence_validity={},
            survives=True, fit_semantics=second,
        ),
    ]
    route = {
        "route": "full_two_stage",
        "multimode_route": "residual_triggered",
        "offered_labels": ["one", "two"],
        "scored_labels": ["one", "two"],
    }
    baseline = scan_delivery._build_selection_semantics(
        results,
        SimpleNamespace(
            scores=scores, pooled=object(), selection_fallback=None, best="one", evidence=evidence
        ),
        route,
        error_attachment_labels=["one", "two"],
    )
    permuted = scan_delivery._build_selection_semantics(
        list(reversed(results)),
        SimpleNamespace(
            scores=list(reversed(scores)), pooled=object(), selection_fallback=None,
            best="one", evidence=evidence,
        ),
        {
            **route,
            "offered_labels": ["two", "one"],
            "scored_labels": ["two", "one"],
        },
        error_attachment_labels=["two", "one"],
    )
    for name in (
        "offered_model_set_fingerprint",
        "scored_model_set_fingerprint",
        "weight_denominator_fingerprint",
        "error_attachment_set_fingerprint",
        "legacy_pool_set_fingerprint",
        "model_selection_fingerprint",
    ):
        assert permuted[name] == baseline[name]

    changed_route = scan_delivery._build_selection_semantics(
        results,
        SimpleNamespace(
            scores=scores, pooled=object(), selection_fallback=None, best="one", evidence=evidence
        ),
        {**route, "multimode_route": "declaration_relaxed"},
        error_attachment_labels=["one", "two"],
    )
    assert changed_route["model_selection_fingerprint"] != baseline[
        "model_selection_fingerprint"
    ]
    changed_denominator_scores = [copy.copy(scores[0])]
    changed_denominator_scores[0].weight = 1.0
    changed_denominator = scan_delivery._build_selection_semantics(
        results,
        SimpleNamespace(
            scores=changed_denominator_scores, pooled=object(),
            selection_fallback=None, best="one", evidence=evidence,
        ),
        route,
        error_attachment_labels=["one"],
    )
    assert changed_denominator["weight_denominator_fingerprint"] != baseline[
        "weight_denominator_fingerprint"
    ]


def test_selection_aggregate_preserves_strongest_reason_and_incomparability():
    eligible = _fit_semantics()
    data_dependent = _fit_semantics(
        estimator="hard_conditioned_likelihood",
        eligibility="ineligible_data_dependent_conditioning",
        constraints=[
            {
                "parameter": "phonon.A",
                "constraint_kind": "hard",
                "value": 25.0,
                "origin": "data_dependent_rescue",
            }
        ],
    )
    data_dependent["selection_ineligibility_reasons"] = [
        "data_dependent_or_post_selection_hard_constraint"
    ]
    data_dependent["data_likelihood_fingerprint"] = semantics_fingerprint(
        "test.other_data_likelihood", {"counts": [99.0]}
    )
    comparison = semantics_fingerprint(
        "test.other_comparison",
        {
            "data_likelihood_fingerprint": data_dependent[
                "data_likelihood_fingerprint"
            ]
        },
    )
    data_dependent["comparison_context_fingerprint"] = comparison
    data_dependent["conditioning_fingerprint"] = comparison
    data_dependent = validate_for_write(data_dependent, FIT_SELECTION_SCHEMA)
    candidates = [
        {"label": "one", "fit_semantics": eligible},
        {"label": "two", "fit_semantics": data_dependent},
    ]
    aggregate, reasons = scan_delivery._aggregate_selection_eligibility(candidates)
    assert aggregate == "ineligible_incomparable_context"
    assert {item["reason_code"] for item in reasons} == {
        "candidate_data_likelihood_contexts_differ",
        "data_dependent_or_post_selection_hard_constraint",
    }


def _catalogue_variant_loser():
    """A non-winning candidate constrained solely by an offered catalogue variant."""
    variant = _fit_semantics(
        estimator="hard_conditioned_likelihood",
        eligibility="ineligible_unapproved_conditioning",
        constraints=[
            {
                "parameter": "phonon.Omega_d",
                "constraint_kind": "hard",
                "value": 0.001,
                "origin": "catalogue_variant",
            }
        ],
    )
    variant["selection_ineligibility_reasons"] = ["catalogue_variant_pin"]
    return validate_for_write(variant, FIT_SELECTION_SCHEMA)


def test_offered_catalogue_variant_loser_does_not_disqualify_the_aggregate():
    """Owner ruling 2026-08-09: a deliberately-offered catalogue variant that loses the
    comparison contributes neither its eligibility nor its reasons to the aggregate."""
    winner = _fit_semantics()
    loser = _catalogue_variant_loser()
    candidates = [
        {"label": "unconstrained", "fit_semantics": winner},
        {"label": "variant", "fit_semantics": loser},
    ]
    aggregate, reasons = scan_delivery._aggregate_selection_eligibility(
        candidates, winner="unconstrained"
    )
    assert aggregate == "eligible"
    assert reasons == []


def test_offered_catalogue_variant_winner_still_disqualifies_the_aggregate():
    """Owner ruling 2026-08-09: the exemption is presence-only — a catalogue variant that WINS
    the comparison still fails closed."""
    winner = _fit_semantics()
    loser = _catalogue_variant_loser()
    candidates = [
        {"label": "unconstrained", "fit_semantics": winner},
        {"label": "variant", "fit_semantics": loser},
    ]
    aggregate, reasons = scan_delivery._aggregate_selection_eligibility(
        candidates, winner="variant"
    )
    assert aggregate == "ineligible_unapproved_conditioning"
    assert {item["reason_code"] for item in reasons} == {"catalogue_variant_pin"}
    assert {"variant"} <= {
        label for item in reasons for label in item["candidate_labels"]
    }


def test_offered_catalogue_variant_exemption_does_not_weaken_the_arena_guard():
    """Owner ruling 2026-08-09: the exempted variant's likelihood context still counts in the
    incomparable-context check — the exemption is about eligibility/reasons, not the arena."""
    winner = _fit_semantics()
    loser = _catalogue_variant_loser()
    loser["data_likelihood_fingerprint"] = semantics_fingerprint(
        "test.other_data_likelihood", {"counts": [7.0]}
    )
    candidates = [
        {"label": "unconstrained", "fit_semantics": winner},
        {"label": "variant", "fit_semantics": loser},
    ]
    aggregate, reasons = scan_delivery._aggregate_selection_eligibility(
        candidates, winner="unconstrained"
    )
    assert aggregate == "ineligible_incomparable_context"
    assert "candidate_data_likelihood_contexts_differ" in {
        item["reason_code"] for item in reasons
    }


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
    record = scan_delivery._outcome_interval_semantics(
        best=SimpleNamespace(fit_semantics=fit_semantics, within_model={}),
        selection=SimpleNamespace(
            pooled=object(), fit_selection_semantics=selection_semantics
        ),
        interval=(1.0, 1.1, 1.2),
        omega0_profile=None,
        hesse_interval=(1.0, 1.1, 1.2),
        hesse_trusted=True,
        bayes_interval=None,
        interval_reliable=True,
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


@pytest.mark.parametrize(
    (
        "interval",
        "profile",
        "hesse",
        "hesse_trusted",
        "reliable",
        "selected",
        "certifiable",
    ),
    [
        ((1.0, 1.1, 1.2), None, None, False, True, "interval", True),
        (
            (1.0, 1.1, 1.2),
            (0.9, 1.1, 1.3),
            None,
            False,
            False,
            "omega0_profile",
            False,
        ),
        (None, (0.9, 1.1, 1.3), None, False, True, "omega0_profile", True),
        (None, None, (1.0, 1.1, 1.2), True, False, "hesse_interval", True),
        (None, None, (1.0, 1.1, 1.2), False, False, None, False),
    ],
)
def test_interval_authority_matches_realized_delivery(
    interval,
    profile,
    hesse,
    hesse_trusted,
    reliable,
    selected,
    certifiable,
):
    fit_semantics = _fit_semantics()
    produced = {
        "freq_interval": interval,
        "profile_interval": profile,
    }
    producer_semantics = errors_mod._interval_semantics_for(
        SimpleNamespace(label="one", fit_semantics=fit_semantics),
        produced,
    )
    record = scan_delivery._outcome_interval_semantics(
        best=SimpleNamespace(
            fit_semantics=fit_semantics,
            within_model={"interval_semantics": producer_semantics},
        ),
        selection=SimpleNamespace(pooled=None, fit_selection_semantics=None),
        interval=interval,
        omega0_profile=profile,
        hesse_interval=hesse,
        hesse_trusted=hesse_trusted,
        bayes_interval=None,
        interval_reliable=reliable,
    )
    assert record["selected_interval_quantity"] == selected
    assert record["selected_interval_certifiable"] is certifiable
    for name, item in record["quantities"].items():
        assert item["selected_for_delivery"] is (name == selected)
    if selected is not None:
        scopes = record["quantities"][selected]["authority_scopes"]
        assert scopes["single_fit_delivery"] == "active"
        assert scopes["stopping"] == ("active" if certifiable else "ineligible")
    if interval is not None and not reliable:
        assert record["quantities"]["interval"]["authority_scopes"][
            "single_fit_delivery"
        ] == "inactive"
    if hesse is not None and not hesse_trusted:
        scopes = record["quantities"]["hesse_interval"]["authority_scopes"]
        assert scopes["single_fit_delivery"] == "inactive"
        assert scopes["user_diagnostic"] == "active"


def test_interval_authority_is_revoked_when_tracking_withholds_delivery():
    fit_semantics = _fit_semantics()
    producer = errors_mod._interval_semantics_for(
        SimpleNamespace(label="one", fit_semantics=fit_semantics),
        {"freq_interval": (1.0, 1.1, 1.2)},
    )
    outcome = {
        "interval": (1.0, 1.1, 1.2),
        "omega0_profile": None,
        "hesse_interval": None,
        "bayes_interval": None,
        "hesse_trusted": False,
        "interval_reliable": True,
        "interval_semantics": scan_delivery._outcome_interval_semantics(
            best=SimpleNamespace(
                fit_semantics=fit_semantics,
                within_model={"interval_semantics": producer},
            ),
            selection=SimpleNamespace(pooled=None, fit_selection_semantics=None),
            interval=(1.0, 1.1, 1.2),
            omega0_profile=None,
            hesse_interval=None,
            bayes_interval=None,
            hesse_trusted=False,
            interval_reliable=True,
        ),
    }
    evidence_update._clear_tracked_selection(outcome, {}, 2)
    assert outcome["interval"] is None
    assert outcome["interval_semantics"]["selected_interval_quantity"] is None
    assert outcome["interval_semantics"]["quantities"]["interval"][
        "producer_availability"
    ] is True
    assert outcome["interval_semantics"]["quantities"]["interval"][
        "authority_scopes"
    ]["single_fit_delivery"] == "inactive"


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
    assert record["actual_particle_measure_kind"] == "heterogeneous_model_dependent"
    assert record["actual_particle_basis_homogeneous"] is False
    assert record["contract_permits_heterogeneous_fallback"] is True
    assert record["quantities"]["model_interval"][
        "actual_particle_measure_kind"
    ] == "heterogeneous_model_dependent"
    assert record["formal_bayesian_model"] is False
    assert record["homogeneous_design_eligible"] is False
    # The predictive re-publishes the selection record's weight kind, never a hardcode.
    assert record["model_weight_kind"] == "posterior_probability"
    assert record["quantities"]["model_interval"]["model_weight_kind"] == (
        "posterior_probability"
    )
    assert by_label["nested"]["model_weight_kind"] == "posterior_probability"
    assert by_label["nested"]["prior_kind"] == "optimizer_bound_data_dependent"
    assert by_label["nested"]["prior_data_dependent"] is True
    assert by_label["nested"]["evidence_comparable"] is False
    assert by_label["nested"]["bayesian_model_average"] is False


@pytest.mark.parametrize(
    ("basis", "particle_kind", "paradigm"),
    [
        (
            "parametric_bootstrap",
            "parametric_bootstrap_sampling_distribution",
            "frequentist_sampling_distribution",
        ),
        (
            "laplace_hesse",
            "laplace_hesse_parameter_approximation",
            "frequentist_asymptotic",
        ),
    ],
)
def test_predictive_interval_records_actual_homogeneous_contents(
    basis, particle_kind, paradigm
):
    models = [
        {"label": "one", "source_weight": 1.0, "weight": 1.0, "draw_basis": basis}
    ]
    record, _ = predictive_artifact._predictive_statistical_semantics(
        models,
        {"one": SimpleNamespace(fit_semantics=_fit_semantics(), within_model={})},
        _selection_semantics(),
        complete=True,
    )
    assert record["particle_measure_kind"] == particle_kind
    assert record["actual_particle_measure_kind"] == particle_kind
    assert record["actual_particle_basis_homogeneous"] is True
    interval = record["quantities"]["model_interval"]
    assert interval["particle_measure_kind"] == particle_kind
    assert interval["actual_particle_measure_kind"] == particle_kind
    assert interval["inference_paradigm"] == paradigm
    assert interval["contract_permits_heterogeneous_fallback"] is True
    assert record["homogeneous_design_eligible"] is False


def test_predictive_semantics_self_fingerprint_authenticates_on_read():
    record, _ = predictive_artifact._predictive_statistical_semantics(
        [
            {
                "label": "one",
                "source_weight": 1.0,
                "weight": 1.0,
                "draw_basis": "parametric_bootstrap",
            }
        ],
        {"one": SimpleNamespace(fit_semantics=_fit_semantics(), within_model={})},
        _selection_semantics(),
        complete=True,
    )
    assert validate_predictive_semantics(record) == record
    tampered = copy.deepcopy(record)
    tampered["construction_complete"] = False
    with pytest.raises(SemanticsConstructionError) as raised:
        validate_predictive_semantics(tampered)
    assert raised.value.reason_code == "statistical_semantics_fingerprint_mismatch"
    assert read_optional(tampered, PREDICTIVE_SCHEMA) is None


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
    record = scan_delivery._refresh_probability_semantics(
        outcome,
        predictive_payload={
            "predictive_retained_set_fingerprint": retained,
            "statistical_semantics": {"model_weight_kind": "posterior_probability"},
        },
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
            label="two", optimizer_converged=True, cost=10.0, k=2, ndof=20,
            params={"a": 1.0}, fit_semantics=None,
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
    assert [row.label for row in before] == [row.label for row in after] == [
        "one",
        "two",
    ]
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
    assert master_delivery._promotion_candidate_identity([promotion_candidate]) == (
        master_delivery._promotion_candidate_identity(
            [
                {
                    **promotion_candidate,
                    "estimator_kind": "data_likelihood_mle",
                    "selection_eligibility": "eligible",
                    "conditioning_fingerprint": "0" * 64,
                    "shadow_model_selection": {"selection_status": "complete"},
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
        models=[{**model, "statistical_semantics": {"schema": PREDICTIVE_SCHEMA},
                 "shadow_model_selection": {"selection_status": "complete"}}],
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
    }
    sig_before = evidence_update._tracked_authority_signature([observation], **signature_args)
    sig_after = evidence_update._tracked_authority_signature(
        [{**observation, "probability_semantics": {"schema": "ignored"},
          "shadow_model_selection": {"selection_status": "complete"}}],
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

    @dataclass
    class ParsedScientificRecord:
        counts: list[float]

    parsed = ParsedScientificRecord([1.0, 2.0])
    scan_hash = content_hash(parsed)
    parsed.statistical_semantics = {"schema": "ignored"}
    assert content_hash(parsed) == scan_hash

    members = [["scan:1", "det", "content-a"], ["scan:2", "det", "content-b"]]
    master_revision = canonical_hash(members)
    delivery = {"master_revision": master_revision}
    delivery["statistical_semantics"] = {"schema": "ignored"}
    assert delivery["master_revision"] == canonical_hash(members)

    cache_parameters = inspect.signature(
        certification.master_fit_fingerprint
    ).parameters
    assert {
        "fit_selection_semantics",
        "interval_semantics",
        "probability_semantics",
        "shadow_model_selection",
        "statistical_semantics",
    }.isdisjoint(cache_parameters)

    tracking_observation = {
        "gauss_fwhm": 0.8,
        "candidates": [
            {
                "candidate_ref": "c0",
                "energy_meV": 6.0,
                "omega0_err": 0.1,
                "sigma_meV": 0.1,
                "gamma": 0.4,
                "amplitude": 1.0,
                "reliable": True,
            }
        ],
    }
    track_kwargs = {
        "period": 2.0,
        "hypotheses": [
            {
                "name": "optic",
                "zc_energy": {"mean": 6.0, "sigma": 1.0},
                "slope_zc": {"mean": 0.0, "sigma": 8.0},
            }
        ],
        "target_branch": "optic",
    }
    track_before = branch_tracking.track_path(
        [[2.0, 0.0, 0.0]], [tracking_observation], **track_kwargs
    )
    track_after = branch_tracking.track_path(
        [[2.0, 0.0, 0.0]],
        [
            {
                **tracking_observation,
                "probability_semantics": {"schema": "ignored"},
                "shadow_model_selection": {"selection_status": "complete"},
            }
        ],
        **track_kwargs,
    )
    assert track_before["revision"] == track_after["revision"]
    assert track_before["assignments"] == track_after["assignments"]

    calibration = SimpleNamespace(buckets={"100": object()})
    exposure_row = {"effective_exposure": 100.0}
    exposure_before = drive_loop._effective_exposure_bucket(exposure_row, 50)
    exposure_after = drive_loop._effective_exposure_bucket(
        {**exposure_row, "interval_semantics": {"schema": "ignored"}}, 50
    )
    assert exposure_before == exposure_after
    assert drive_loop._calibration_bucket(
        calibration, exposure_before[0]
    ) == drive_loop._calibration_bucket(calibration, exposure_after[0])


# ---------------------------------------------------------------------------
# R13/R34: a failed interval producer returns a nan tuple, not None. Availability
# decided on ``is not None`` therefore published failures as available intervals.
# ---------------------------------------------------------------------------
_NAN3 = (float("nan"), float("nan"), float("nan"))


def _producer(within):
    return errors_mod._interval_semantics_for(
        SimpleNamespace(label="one", fit_semantics=_fit_semantics()), within
    )


def test_a_nan_interval_is_published_unavailable_with_its_failure_reason():
    record = _producer({"freq_interval": _NAN3, "profile_interval": (float("nan"), 5.2)})
    freq = record["quantities"]["freq_interval"]
    assert freq["availability"] is False
    assert freq["failure_reason"] == "bootstrap_interval_unavailable"
    profile = record["quantities"]["profile_interval"]
    assert profile["availability"] is False
    assert profile["failure_reason"] == "profile_interval_unavailable"


def test_a_bound_truncated_profile_side_is_distinguishable_from_an_unavailable_one():
    from isar.analyze.errors.profile import BoundTruncated

    record = _producer({"freq_interval": (1.0, 1.1, 1.2),
                        "profile_interval": (BoundTruncated(), 5.2)})
    profile = record["quantities"]["profile_interval"]
    assert profile["availability"] is False
    assert profile["failure_reason"] == "profile_bound_truncated"
    assert record["quantities"]["freq_interval"]["availability"] is True


def test_a_bayes_run_without_a_prior_fingerprint_degrades_only_its_own_entry(caplog):
    # It used to raise, and _attach_interval_semantics then set the ENTIRE record to None: an
    # optional Bayesian diagnostic erased the valid bootstrap and profile semantics beside it.
    record = _producer({"freq_interval": (1.0, 1.1, 1.2),
                        "profile_interval": (0.9, 1.3),
                        "bayes_interval": _NAN3,
                        "bayes_ok": False,
                        "bayes_fail_reason": "ESS below gate",
                        "bayes_prior_fingerprint": None})
    assert record["quantities"]["freq_interval"]["availability"] is True
    assert record["quantities"]["profile_interval"]["availability"] is True
    bayes = record["quantities"]["bayes_interval"]
    assert bayes["availability"] is False
    assert bayes["failure_reason"] == "missing_bayesian_prior_fingerprint"
    assert "missing_bayesian_prior_fingerprint" in caplog.text


def test_a_non_converged_bayes_run_carries_its_own_failure_reason():
    fingerprint = semantics_fingerprint("test.prior", {"omega0": 5.0})
    record = _producer({"freq_interval": (1.0, 1.1, 1.2),
                        "bayes_interval": _NAN3,
                        "bayes_ok": False,
                        "bayes_fail_reason": "ESS below gate",
                        "bayes_prior_fingerprint": fingerprint})
    bayes = record["quantities"]["bayes_interval"]
    assert bayes["availability"] is False
    assert bayes["failure_reason"] == "ESS below gate"
    assert bayes["prior_fingerprint"] == fingerprint


def test_a_nan_interval_is_never_selected_for_delivery():
    # (nan, nan, nan) is `not None`, so the ladder used to select it, stamp availability=True and
    # hand the stopping rule a nan half-width that no tolerance can ever satisfy.
    fit_semantics = _fit_semantics()
    produced = {"freq_interval": _NAN3, "profile_interval": (0.9, 1.3)}
    record = scan_delivery._outcome_interval_semantics(
        best=SimpleNamespace(fit_semantics=fit_semantics,
                             within_model={"interval_semantics": _producer(produced)}),
        selection=SimpleNamespace(pooled=None, fit_selection_semantics=None),
        interval=_NAN3,
        omega0_profile=(0.9, 1.3),
        hesse_interval=None,
        hesse_trusted=False,
        bayes_interval=None,
        interval_reliable=True,
    )
    assert record["selected_interval_quantity"] == "omega0_profile"
    assert record["quantities"]["interval"]["availability"] is False
    assert record["quantities"]["interval"]["authority_scopes"] == authority_scopes()


def test_every_interval_nan_leaves_nothing_selected():
    fit_semantics = _fit_semantics()
    record = scan_delivery._outcome_interval_semantics(
        best=SimpleNamespace(fit_semantics=fit_semantics,
                             within_model={"interval_semantics": _producer(
                                 {"freq_interval": _NAN3, "profile_interval": _NAN3[:2]})}),
        selection=SimpleNamespace(pooled=None, fit_selection_semantics=None),
        interval=_NAN3,
        omega0_profile=_NAN3[:2],
        hesse_interval=_NAN3[:2],
        hesse_trusted=True,
        bayes_interval=None,
        interval_reliable=True,
    )
    assert record["selected_interval_quantity"] is None
    assert record["selected_interval_certifiable"] is False
    assert not any(item["availability"] for item in record["quantities"].values())

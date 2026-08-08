from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from isar.analyze import loop
from isar.analyze.context import ScanContext
from isar.analyze.phonon import enumerate_phonon_structures
from isar.synth.stat2b.criteria import (
    CandidateEvidence,
    criterion_values,
    evidence_from_results,
    fit_null_candidates,
    mean_poisson_kl,
    mean_poisson_log_score,
    null_hypotheses,
    select_by_criterion,
)


def _candidate(label, cost, k, n=20, converged=True, eligible=True):
    return CandidateEvidence(
        label=label,
        k=k,
        n=n,
        cost=cost,
        optimizer_converged=converged,
        prediction=np.ones(n),
        result=SimpleNamespace(params={}),
        hypothesis=SimpleNamespace(),
        source="test",
        selection_eligible=eligible,
    )


def _context(*, qe=True, center=0.0):
    x = np.linspace(-3.0, 5.0, 33)
    return ScanContext(
        x=x,
        counts=np.full(x.size, 10.0),
        mask=np.ones(x.size, dtype=bool),
        T=100.0,
        center=center,
        gauss_fwhm=1.0,
        lorentz_fwhm=0.0,
        quasielastic_fwhm=(2.0 if qe else None),
        instrument="S30",
        energy_status="needs_zero",
    )


def test_criterion_formulas_and_aicc_infinity():
    values = criterion_values(10.0, 3, 20)
    assert values["aic"] == 16.0
    assert values["aicc"] == pytest.approx(17.5)
    assert values["bic"] == pytest.approx(10.0 + 3.0 * np.log(20))
    assert criterion_values(10.0, 3, 4)["aicc"] == float("inf")


def test_aicc_zero_weights_infinite_member_and_refuses_all_infinite():
    selected = select_by_criterion(
        [_candidate("finite", 10.0, 1, n=5), _candidate("infinite", 1.0, 4, n=5)],
        "aicc",
    )
    assert selected.status == "available"
    assert selected.selected_label == "finite"
    assert {row.label: row.weight for row in selected.rows}["infinite"] == 0.0

    unavailable = select_by_criterion(
        [_candidate("a", 10.0, 4, n=5), _candidate("b", 8.0, 5, n=5)], "aicc"
    )
    assert unavailable.status == "unavailable"
    assert unavailable.reason == "all_infinite_aicc"


def test_complete_family_refuses_missing_or_failed_candidate():
    candidates = [_candidate("a", 1.0, 1)]
    assert (
        select_by_criterion(candidates, "aic", required_labels=("a", "b")).reason
        == "missing_required_candidate"
    )
    failed = [_candidate("a", 1.0, 1), _candidate("b", 2.0, 1, converged=False)]
    assert (
        select_by_criterion(failed, "aic", required_labels=("a", "b")).reason
        == "required_candidate_fit_failed"
    )
    ineligible = [
        _candidate("a", 1.0, 1),
        _candidate("b", 2.0, 1, eligible=False),
    ]
    assert (
        select_by_criterion(
            ineligible, "aic", required_labels=("a", "b")
        ).reason
        == "required_candidate_ineligible"
    )


def test_null_family_obeys_production_gates_and_fits():
    # bg / elastic+bg are production candidates since SCENE-1 PR-a; only the QE-bearing
    # nulls remain campaign-only (fitting the promoted ones here would double-count them).
    ctx = _context(qe=True)
    assert [hyp.label for hyp in null_hypotheses(ctx)] == ["qe+bg", "elastic+qe+bg"]
    evidence = fit_null_candidates(ctx)
    assert [item.label for item in evidence] == ["qe+bg", "elastic+qe+bg"]
    assert all(item.prediction.shape == ctx.x_used.shape for item in evidence)

    off_window = _context(qe=False, center=20.0)
    assert [hyp.label for hyp in null_hypotheses(off_window)] == []


def test_standard_evidence_refuses_missing_fit_selection_semantics():
    ctx = _context()
    hypothesis = enumerate_phonon_structures(ctx)[0]
    result = loop.fit_hypothesis(hypothesis, ctx)
    result.fit_semantics = None
    evidence = evidence_from_results(
        [result], [hypothesis], ctx, source="standard"
    )
    assert evidence[0].selection_eligible is False
    assert select_by_criterion(evidence, "aic").reason == (
        "required_candidate_ineligible"
    )


def test_poisson_kl_and_shared_realized_log_score_formulas():
    truth = np.asarray([0.0, 2.0, 4.0])
    fitted = np.asarray([1.0, 3.0, 4.0])
    expected = np.mean([1.0, 2.0 * np.log(2.0 / 3.0) + 1.0, 0.0])
    assert mean_poisson_kl(truth, fitted) == pytest.approx(expected)

    observed = np.asarray([0.0, 2.0, 4.0])
    expected_log = np.mean(
        observed * np.log(fitted)
        - fitted
        - np.asarray([0.0, np.log(2.0), np.log(24.0)])
    )
    assert mean_poisson_log_score(observed, fitted) == pytest.approx(expected_log)


def test_poisson_kl_is_finite_for_a_zero_fitted_mean_without_ratio_overflow():
    truth = np.asarray([2.0, 4.0])
    fitted = np.asarray([0.0, 0.0])
    tiny = np.finfo(float).tiny
    expected = np.mean(
        truth * (np.log(truth) - np.log(tiny)) + tiny - truth
    )

    with np.errstate(over="raise", divide="raise", invalid="raise"):
        observed = mean_poisson_kl(truth, fitted)

    assert np.isfinite(observed)
    assert observed == pytest.approx(expected)

"""Step-2 modality (§4): the pooled across-model omega0 distribution and the deterministic
fold (unimodal) / fork (multimodal) decision.

The fork is exercised on synthetic samples only. Since the count ladder the selection's survivor
is the winner alone (the one model that receives the expensive errors), so a fork comes from its
own bootstrap; the pooling of several candidates' samples is exercised directly. These fixtures
fabricate the within-model bootstrap samples.
"""
import numpy as np

from isar.analyze import loop, select


def _result(label, k, cost, samples, *, ndof=20, converged=True):
    s = np.asarray(samples, dtype=float)
    params = {f"{label}.p{i}": 0.0 for i in range(k)}
    return loop.StructureFitResult(
        label=label, k=k, ndof=ndof, cost=cost, cost_per_dof=cost / ndof,
        optimizer_converged=converged, covariance_ok=True, profile_ok=True,
        well_constrained=True, analysis_usable=converged,
        params=params, param_errors={n: None for n in params},
        gamma=0.4, gamma_err=0.05, omega0=float(np.median(s)), omega0_err_hesse=0.05,
        message="ok", within_model={"omega0_samples": s},
    )


def _two_clusters(rng, lo=5.0, hi=8.0, n=250, sd=0.1):
    return np.concatenate([rng.normal(lo, sd, n), rng.normal(hi, sd, n)])


def test_two_separated_pooled_samples_fork():
    rng = np.random.default_rng(0)
    pooled = select.pool_across_model([(0.5, rng.normal(5.0, 0.1, 300)),
                                       (0.5, rng.normal(8.0, 0.1, 300))])
    assert pooled.modality == "fork"
    assert pooled.interval is None
    assert pooled.modes is not None and len(pooled.modes) == 2
    locs = sorted(m.location for m in pooled.modes)
    assert abs(locs[0] - 5.0) < 0.3 and abs(locs[1] - 8.0) < 0.3
    assert abs(sum(m.weight for m in pooled.modes) - 1.0) < 1e-9
    assert "either" in pooled.disambiguation.lower()


def test_overlapping_pooled_samples_fold():
    rng = np.random.default_rng(1)
    pooled = select.pool_across_model([(0.5, rng.normal(6.0, 0.15, 300)),
                                       (0.5, rng.normal(6.0, 0.15, 300))])
    assert pooled.modality == "fold"
    assert pooled.modes is None
    lo, med, hi = pooled.interval
    assert lo < med < hi and lo <= 6.0 <= hi


def test_the_survivor_is_the_winner_alone():
    """The expensive errors go to the selected model only (count ladder): a runner-up with an
    equal weight is still scored and flagged ``survives``, but it is not a survivor, and the pool
    holds the winner's samples alone."""
    rng = np.random.default_rng(0)
    a = _result("A", k=5, cost=90.0, samples=rng.normal(5.0, 0.1, 300))   # equal AIC ->
    b = _result("B", k=5, cost=90.0, samples=rng.normal(8.0, 0.1, 300))   # weights 0.5/0.5
    sel = select.select_models([a, b])
    assert sel.survivors == [sel.best] == ["A"]
    assert all(score.survives for score in sel.scores)
    assert sel.modality == "fold"
    assert abs(sel.pooled.interval[1] - 5.0) < 0.1


def test_single_survivor_with_bimodal_bootstrap_forks():
    """The §5.2 cascade: a lone surviving structure whose own bootstrap is double-peaked
    still forks (the modality test runs on the pooled distribution, here = its samples)."""
    rng = np.random.default_rng(2)
    win = _result("win", k=5, cost=80.0, samples=_two_clusters(rng))          # aic 90
    junk = _result("junk", k=4, cost=120.0, samples=rng.normal(5.0, 0.1, 300))  # dAIC ~38, dropped
    sel = select.select_models([win, junk])
    assert sel.survivors == ["win"]
    assert sel.modality == "fork"
    assert len(sel.pooled.modes) == 2


def test_single_clean_survivor_folds():
    """The real-S30 shape (fabricated, fast): one survivor, one cluster -> fold."""
    rng = np.random.default_rng(5)
    win = _result("win", k=5, cost=80.0, samples=rng.normal(6.5, 0.12, 300))
    junk = _result("junk", k=4, cost=120.0, samples=rng.normal(6.5, 0.12, 300))
    sel = select.select_models([win, junk])
    assert sel.survivors == ["win"]
    assert sel.modality == "fold"
    assert sel.pooled.interval[0] <= 6.5 <= sel.pooled.interval[2]


def test_modality_is_deterministic():
    rng = np.random.default_rng(3)
    win = _result("win", k=5, cost=80.0, samples=_two_clusters(rng))
    s1 = select.select_models([win], stamp=False)
    s2 = select.select_models([win], stamp=False)
    assert s1.modality == "fork"
    assert sorted((m.location, m.weight) for m in s1.pooled.modes) == \
        sorted((m.location, m.weight) for m in s2.pooled.modes)

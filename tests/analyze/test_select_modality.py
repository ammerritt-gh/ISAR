"""Step-2 modality (§4): the pooled across-model omega0 distribution and the deterministic
fold (unimodal) / fork (multimodal) decision.

The fork is exercised on synthetic samples only — no second-phonon hypothesis is enumerated
by default (§2), so on real scans there is one survivor and the pool folds. These fixtures
fabricate the survivors' within-model bootstrap samples directly.
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


def test_two_separated_survivors_fork():
    rng = np.random.default_rng(0)
    a = _result("A", k=5, cost=90.0, samples=rng.normal(5.0, 0.1, 300))   # equal AIC ->
    b = _result("B", k=5, cost=90.0, samples=rng.normal(8.0, 0.1, 300))   # weights 0.5/0.5
    sel = select.select_models([a, b])
    assert sel.modality == "fork"
    assert sel.pooled.interval is None
    assert sel.pooled.modes is not None and len(sel.pooled.modes) == 2
    locs = sorted(m.location for m in sel.pooled.modes)
    assert abs(locs[0] - 5.0) < 0.3 and abs(locs[1] - 8.0) < 0.3
    assert abs(sum(m.weight for m in sel.pooled.modes) - 1.0) < 1e-9
    assert "either" in sel.pooled.disambiguation.lower()


def test_overlapping_survivors_fold():
    rng = np.random.default_rng(1)
    a = _result("A", k=5, cost=90.0, samples=rng.normal(6.0, 0.15, 300))
    b = _result("B", k=5, cost=90.0, samples=rng.normal(6.0, 0.15, 300))
    sel = select.select_models([a, b])
    assert sel.modality == "fold"
    assert sel.pooled.modes is None
    lo, med, hi = sel.pooled.interval
    assert lo < med < hi and lo <= 6.0 <= hi


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
    a = _result("A", k=5, cost=90.0, samples=rng.normal(5.0, 0.1, 300))
    b = _result("B", k=5, cost=90.0, samples=rng.normal(8.0, 0.1, 300))
    s1 = select.select_models([a, b], stamp=False)
    s2 = select.select_models([a, b], stamp=False)
    assert sorted((m.location, m.weight) for m in s1.pooled.modes) == \
        sorted((m.location, m.weight) for m in s2.pooled.modes)

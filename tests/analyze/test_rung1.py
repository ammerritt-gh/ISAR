"""Rung 1 of the count ladder (design 2026-10-22, §3.1): every count 0..ceiling with and without
the elastic line, several independent starts per model, the highest-likelihood start kept as the
representative (converged or not), no gate and no early stop, and a per-spectrum record of it all.

Every test names its ceiling: the count is what these tests are about.
"""
from dataclasses import replace

import numpy as np

from isar.analyze import backends, loop, select
from isar.analyze.backends import BackendResult
from isar.analyze.context import ScanContext
from isar.analyze.lineshapes import convolute_voigt_dho
from isar.analyze.phonon import enumerate_phonon_structures


def _ctx(lam, x, seed, **kw):
    counts = np.random.default_rng(seed).poisson(np.clip(lam, 0.0, None)).astype(float)
    return ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=50.0,
                       center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="needs_zero", **kw)


def _dho(x, omega0, gamma, amp):
    return convolute_voigt_dho(x, omega0, 0.0, 1.0, 0.0, gamma, amp, 0.0, 0.0, 50.0,
                               peak="stokes+astokes")


def _one_peak(n=41, seed=3):
    x = np.linspace(-8.0, 16.0, n)
    return _ctx(_dho(x, 5.0, 0.4, 500.0) + 8.0, x, seed)


def test_every_count_to_the_ceiling_is_fitted_with_no_gate():
    """A clean single peak: nothing about it asks for a second mode, and the second mode is
    fitted anyway, with and without the elastic line; the record lists every model with its
    starts, and each representative is its lowest-objective start."""
    ctx = _one_peak()
    out, route = {}, {}
    results = loop.fit_structures_two_stage(ctx, mode_ceiling=2, rung1_out=out, route_out=route)
    labels = [r.label for r in results]
    assert labels == ["stokes+bg", "elastic+stokes+bg", "elastic+bg", "bg",
                      "stokes2+bg", "elastic+stokes2+bg"]
    assert route["multimode_route"] == "count_ladder"
    record = out["rung1"]
    assert record.ceiling == 2
    assert [(m.label, m.count, m.elastic) for m in record.models] == [
        ("stokes+bg", 1, False), ("elastic+stokes+bg", 1, True), ("elastic+bg", 0, True),
        ("bg", 0, False), ("stokes2+bg", 2, False), ("elastic+stokes2+bg", 2, True)]
    for model, result in zip(record.models, results):
        assert model.representative is result
        assert model.starts[model.representative_start].objective == min(
            s.objective for s in model.starts)
        assert model.converged == result.optimizer_converged
    two = record.by_label()["stokes2+bg"]
    assert {"residual", "split", "descriptor"} <= set(two.seeds_by_source)


def _stub_fits(monkeypatch, cost_by_count):
    """Replace the optimizer by a stub that returns each candidate at its seed, with a cost set by
    its mode count — so which counts the ladder fits can be tested without fitting anything."""

    def fake(hyp, ctx, *, backend="iminuit", minos=False, x0=None):
        prob = loop.build_problem(hyp, ctx, x0=x0)
        lo, hi = np.array(prob.bounds).T
        br = BackendResult(np.clip(prob.x0, lo, hi), True, "stub", 1, None, False, {}, None)
        result = loop.result_from_backend(hyp.label, prob.composite, prob.costv, prob.n_used,
                                          prob.k, br)
        result.cost = cost_by_count[len(prob.composite.phonon_indices())]
        return result

    monkeypatch.setattr(loop, "fit_hypothesis", fake)


def test_no_early_stop_a_worse_count_does_not_end_the_ladder(monkeypatch):
    """Count 2 fits worse than count 1 once its two extra parameters are paid for, yet count 3
    is still fitted — and wins. One broad peak sometimes needs two replacements before the larger
    model wins (design §3.1), so a count that looks worse never ends the search."""
    _stub_fits(monkeypatch, {0: 900.0, 1: 100.0, 2: 99.5, 3: 20.0})
    ctx = _one_peak()
    results = loop.fit_structures_two_stage(ctx, mode_ceiling=3)
    by_label = {r.label: r for r in results}
    aic = {label: r.cost + 2 * r.k for label, r in by_label.items()}
    assert aic["stokes2+bg"] > aic["stokes+bg"]               # count 2 is worse than count 1 ...
    assert "stokes3+bg" in by_label                             # ... and count 3 is fitted anyway
    assert select.select_models(results).best == "stokes3+bg"   # ... and wins


def test_split_seeds_find_a_close_doublet_the_residual_seed_misses(monkeypatch):
    """A doublet 0.7 resolution FWHM apart with 2:1 heights, plus a weak far feature: the residual
    seed puts the second mode on the far feature (it may not land within one FWHM of the first),
    and the joint fit stays there. Splitting the one-mode fit's peak resolves the doublet, and
    that start is the representative; without split seeds the ladder keeps the poorer fit."""
    x = np.linspace(-6.0, 14.0, 61)
    lam = (_dho(x, 5.65, 0.15, 400.0) + _dho(x, 6.35, 0.15, 200.0) + _dho(x, 11.0, 0.3, 15.0)
           + 5.0)
    ctx = _ctx(lam, x, 0)
    record = loop._rung1_ladder(ctx, mode_ceiling=2)
    two = record.by_label()["stokes2+bg"]
    residual = next(s for s in two.starts if s.source == "residual")
    best = two.starts[two.representative_start]
    assert best.source == "split"
    assert best.cost < residual.cost - 100.0
    w = sorted(m["omega0"] for m in two.representative.modes)
    assert abs(w[0] - 5.65) < 0.2 and abs(w[1] - 6.35) < 0.2

    monkeypatch.setattr(loop, "SPLIT_SEPARATIONS", ())          # the control: no split seeds
    control = loop._rung1_ladder(ctx, mode_ceiling=2).by_label()["stokes2+bg"]
    assert "split" not in control.seeds_by_source
    assert control.representative.cost > best.cost + 100.0


def test_a_non_converged_representative_is_kept_and_marked(monkeypatch):
    """Starve the optimizer so nothing converges: no count is dropped or skipped for it (no
    converged-only filter decides what is tried), and each representative is marked."""
    monkeypatch.setattr(backends, "_migrad_ncall", lambda npar: 3)
    ctx = _one_peak()
    out = {}
    results = loop.fit_structures_two_stage(ctx, mode_ceiling=2, rung1_out=out)
    assert {"stokes+bg", "stokes2+bg", "elastic+stokes2+bg"} <= {r.label for r in results}
    phonon_models = [m for m in out["rung1"].models if m.count >= 1]
    assert phonon_models and not any(m.converged for m in phonon_models)
    assert all(not s.converged for m in phonon_models for s in m.starts)


def test_an_amplitude_at_the_recipe_ceiling_is_marked():
    """The recipe's amplitude ceiling is a declared constant: a spectrum brighter than it allows
    rails its amplitude at the upper bound, and the record says which parameter."""
    x = np.linspace(-8.0, 16.0, 41)
    ctx = _ctx(_dho(x, 5.0, 0.4, 500.0) + 8.0, x, 3, exposure_scale=np.ones_like(x),
               exposure_reference=20.0, exposure_source="monitor")
    out = {}
    loop.fit_structures_two_stage(ctx, mode_ceiling=1, rung1_out=out)
    model = out["rung1"].by_label()["stokes+bg"]
    assert "phonon.A" in model.at_upper_bound
    nominal = replace(ctx, exposure_scale=None, exposure_reference=None, exposure_source=None)
    loop.fit_structures_two_stage(nominal, mode_ceiling=1, rung1_out=out)
    assert out["rung1"].by_label()["stokes+bg"].at_upper_bound == []


def test_the_record_takes_evidence_and_probability_from_the_selection():
    ctx = _one_peak()
    out = {}
    results = loop.fit_structures_two_stage(ctx, mode_ceiling=1, rung1_out=out)
    hyps = {h.label: h for h in enumerate_phonon_structures(ctx)}
    sel = select.select_models(results, hyps, ctx, stamp=False)
    record = out["rung1"]
    record.apply_selection(sel)
    scored = {s.label: s for s in sel.scores}
    for model in record.models:
        assert model.probability == scored[model.label].posterior_probability
        assert model.log_evidence is scored[model.label].log_evidence   # NaN when pruned
    counts = record.count_probabilities()
    assert set(counts) == {0, 1}
    assert abs(sum(counts.values()) - 1.0) < 1e-9
    assert counts[1] > 0.99


def test_every_start_keeps_the_initial_vector_it_began_from():
    """A reader can state and reproduce a seed: each fresh start keeps its x0 by parameter name,
    the cold grid IC included (resolved in the parent, not left as None), and refitting from the
    kept x0 gives the same cost."""
    x = np.linspace(-6.0, 14.0, 61)
    lam = (_dho(x, 5.65, 0.15, 400.0) + _dho(x, 6.35, 0.15, 200.0) + _dho(x, 11.0, 0.3, 15.0)
           + 5.0)
    ctx = _ctx(lam, x, 0)
    record = loop._rung1_ladder(ctx, mode_ceiling=2)
    for model in record.models:
        names = model.hypothesis.build().param_names()
        for start in model.starts:
            assert list(start.x0) == names, (model.label, start.source)
            assert all(np.isfinite(v) for v in start.x0.values())
    two = record.by_label()["stokes2+bg"]
    one = record.by_label()["stokes+bg"]
    split = next(s for s in two.starts if s.source == "split")
    cold = next(s for s in one.starts if s.source == "residual")
    for model, start in ((two, split), (one, cold)):
        again = loop.fit_hypothesis(model.hypothesis, ctx, x0=start.x0)
        assert np.isclose(again.cost, start.cost, rtol=1e-6)

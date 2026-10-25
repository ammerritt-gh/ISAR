"""Sealed golden gates (S12): held-out real-data scenarios, run only when the owner names it.

This file is a GENERIC runner. Everything scenario-specific — the scan, the candidate
specifications, the seeds, the declared priors, and every expected answer — lives in the
gitignored local ``real_data/golden/<scenario>/scenario.json``. The repository deliberately
carries no expectations: a golden gate is a validation instrument, not a tuning instrument,
and nothing tracked here can leak an answer into development (S12's no-peeking rule).

Sealing is mechanical, twice over: the tests SKIP unless ``ISAR_GOLDEN=1`` is set (so no
ordinary lane, parallel or serial, ever executes them), and, once the switch is set, they FAIL
naming the folder when no local bundle is found (a named run that finds nothing must not read as
a pass). Set the variable only for an owner-named gate run:

    ISAR_GOLDEN=1 python -m pytest proofs/test_golden_scenarios.py -m golden -s

or double-click ``run-isar-proofs.bat golden``.
"""
import json
import os
from pathlib import Path

import numpy as np
import pytest

from isar.analyze import catalogue, evidence, loop, select
from isar.analyze import phonon as PH
from isar.analyze import verdict as V
from isar.analyze.context import ScanContext
from isar.analyze.phonon import enumerate_phonon_multimode, enumerate_phonon_structures
from isar.analyze.structures import Hypothesis
from isar.schema.steering import ParamConstraints

pytestmark = pytest.mark.golden

GOLDEN_ROOT = Path(__file__).resolve().parents[1] / "real_data" / "golden"

if not os.environ.get("ISAR_GOLDEN"):
    pytest.skip("sealed golden gates run only when named (set ISAR_GOLDEN=1) — S12",
                allow_module_level=True)

SCENARIOS = sorted(p for p in GOLDEN_ROOT.glob("*/scenario.json")) if GOLDEN_ROOT.is_dir() else []
if not SCENARIOS:
    pytest.fail(f"ISAR_GOLDEN is set but no golden bundles were found under {GOLDEN_ROOT}",
                pytrace=False)


def _load_counts(path: Path):
    """Rate/error CSV -> effective counts N=(r/s)^2 (pure counting statistics, uniform
    exposure asserted — the only reconstruction golden bundles currently declare)."""
    e, r, s = [], [], []
    with open(path, encoding="utf-8") as f:
        for line in f:
            parts = line.strip().split(",")
            try:
                ei, ri, si = float(parts[0]), float(parts[1]), float(parts[2])
            except (ValueError, IndexError):
                continue
            e.append(ei), r.append(ri), s.append(si)
    e, r, s = np.asarray(e), np.asarray(r), np.asarray(s)
    n = (r / s) ** 2
    t = n / r
    assert float(np.ptp(t) / np.median(t)) < 0.02, "non-uniform exposure: wire exposure_scale"
    return e, n


def _ctx(spec, bundle_dir: Path, *, constraints=None):
    energy, counts = _load_counts(bundle_dir / spec["data_file"])
    c = spec["context"]
    kw = {} if constraints is None else {"constraints": constraints}
    return ScanContext(x=energy, counts=counts, mask=np.ones_like(energy, dtype=bool),
                       T=c["T"], center=c["center"], gauss_fwhm=c["gauss_fwhm"],
                       lorentz_fwhm=c["lorentz_fwhm"], instrument=c["instrument"],
                       energy_status=c["energy_status"],
                       quasielastic_fwhm=c.get("quasielastic_fwhm"), **kw)


def _n_mode_hypothesis(ctx, label: str, n: int):
    base = {h.label: h for h in enumerate_phonon_multimode(ctx)}["stokes2+qe+bg"]
    comps = list(base.components)
    phonons = comps[:2]
    for i in range(3, n + 1):
        phonons = phonons + [PH._phonon_for(ctx, prefix=f"phonon{i}")]
    return Hypothesis(label, (*phonons, *comps[2:]))


def _x0(hyp, ctx, seeds):
    prob = loop.build_problem(hyp, ctx)
    x0, it, cur = {}, iter(seeds), None
    for name in prob.composite.param_names():
        if name.endswith(".omega0"):
            cur = next(it)
            x0[name] = cur[0]
        elif name.endswith(".Gamma"):
            x0[name] = cur[1]
        elif name.endswith(".h") and not name.startswith("elastic."):
            x0[name] = cur[2]
        elif name.endswith(".QE"):
            x0[name] = 1e5
        else:
            x0[name] = 300.0
    return x0


def _run_scenario(spec, bundle_dir: Path):
    ctx = _ctx(spec, bundle_dir)
    results, hyps, per_ctx = [], {}, {}
    blind_sel = None
    for cand in spec["candidates"]:
        if cand["kind"] == "two_stage_enumeration":
            two_stage, _ = loop.fit_structures_two_stage_hinted(ctx, minos=False)
            hyps.update({h.label: h for h in enumerate_phonon_structures(ctx)})
            hyps.update({h.label: h for h in enumerate_phonon_multimode(ctx)})
            for r in two_stage:
                per_ctx[r.label] = ctx
            blind_sel = select.select_models(list(two_stage), dict(hyps), ctx, stamp=False)
            results.extend(two_stage)
        elif cand["kind"] == "n_mode":
            cons = None
            if cand.get("soft_priors"):
                cons = ParamConstraints(
                    soft={k: tuple(v) for k, v in cand["soft_priors"].items()},
                    origins={k: cand.get("prior_origin", "user_declared_golden")
                             for k in cand["soft_priors"]})
            c = _ctx(spec, bundle_dir, constraints=cons) if cons is not None else ctx
            assert loop.data_likelihood_fingerprint(c) == loop.data_likelihood_fingerprint(ctx)
            hyp = _n_mode_hypothesis(c, cand["label"], int(cand["n"]))
            x0 = _x0(hyp, c, cand["ic_seeds"]) if cand.get("ic_seeds") else None
            fit = loop.fit_hypothesis(hyp, c, minos=False, x0=x0)
            hyps[hyp.label], per_ctx[fit.label] = hyp, c
            results.append(fit)
        else:  # pragma: no cover — a new bundle kind must fail loudly, not skip
            raise AssertionError(f"unknown golden candidate kind {cand['kind']!r}")

    es = evidence.EvidenceSet(arena_fingerprint=loop.data_likelihood_fingerprint(ctx))
    for r in results:
        if not r.optimizer_converged:
            continue
        c = per_ctx[r.label]
        prob = loop.build_problem(hyps[r.label], c)
        bundle = catalogue.resolve_priors(prob.composite, c, prob.bounds,
                                          exclude=[prob.free_names[i] for i in prob.fixed_pos])
        for rec in evidence.candidate_set_evidence([(hyps[r.label], r, bundle)], c).records:
            es.add(rec)
    sel = select.select_models(results, hyps, ctx, evidence=es)
    return ctx, results, sel, blind_sel


def _get(mapping, label, what):
    """A claim naming an absent candidate is itself a divergence — report it by name
    (AssertionError so the failure-collection loop keeps going), never a raw KeyError."""
    assert label in mapping, f"claim references {what} {label!r} which is not present " \
                             f"(have: {sorted(mapping)})"
    return mapping[label]


def _check_claim(claim, ctx, results, sel, blind_sel):
    by_r = {r.label: r for r in results}
    by_s = {s.label: s for s in sel.scores}
    kind = claim["type"]
    if kind == "blind_two_stage":
        assert blind_sel is not None
        assert blind_sel.best == claim["expect_winner"], claim["why"]
        vd = V.classify([r for r in results if r.label in
                         {s.label for s in blind_sel.scores}], ctx, blind_sel)
        assert getattr(vd, "verdict", vd) == claim["expect_verdict"], claim["why"]
    elif kind == "winner":
        assert sel.best == claim["expect"], claim["why"]
        win = _get(by_s, sel.best, "scored candidate")
        assert win.posterior_probability >= claim["min_posterior"], claim["why"]
    elif kind == "evidence_order":
        g = _get(by_s, claim["greater"], "scored candidate")
        l = _get(by_s, claim["less"], "scored candidate")
        assert np.isfinite(g.log_evidence) and np.isfinite(l.log_evidence), claim["why"]
        assert g.log_evidence > l.log_evidence, claim["why"]
    elif kind == "mode_near":
        modes = _get(by_r, claim["candidate"], "fitted candidate").modes or []
        assert any(abs(m["omega0"] - claim["omega"]) <= claim["tol"] for m in modes), \
            f"{claim['why']}: no mode within {claim['tol']} of {claim['omega']} in " \
            f"{[round(m['omega0'], 2) for m in modes]}"
    elif kind == "mode_split":
        lo, hi = claim["window"]
        inside = sorted(m["omega0"]
                        for m in (_get(by_r, claim["candidate"], "fitted candidate").modes or [])
                        if lo <= m["omega0"] <= hi)
        assert len(inside) == 2, f"{claim['why']}: expected 2 modes in [{lo},{hi}], got {inside}"
        split = inside[1] - inside[0]
        assert claim["split_min"] <= split <= claim["split_max"], \
            f"{claim['why']}: split {split:.3f} outside [{claim['split_min']}, {claim['split_max']}]"
    elif kind == "all_evidence_cheap":
        escalated = [s.label for s in sel.scores if s.evidence_method == "dynesty"]
        assert len(escalated) <= claim["max_escalations"], \
            f"{claim['why']}: escalated {escalated}"
    else:
        raise AssertionError(f"unknown golden claim type {kind!r}")


@pytest.mark.parametrize("scenario_path", SCENARIOS, ids=lambda p: p.parent.name)
def test_golden_scenario(scenario_path):
    spec = json.loads(scenario_path.read_text(encoding="utf-8"))
    ctx, results, sel, blind_sel = _run_scenario(spec, scenario_path.parent)
    failures = []
    for claim in spec["claims"]:
        try:
            _check_claim(claim, ctx, results, sel, blind_sel)
        except AssertionError as exc:
            failures.append(f"[{claim['type']}] {exc}")
    assert not failures, "golden gate diverged:\n  " + "\n  ".join(failures)

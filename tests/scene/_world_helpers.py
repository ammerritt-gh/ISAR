"""Running the real selection path on a SCENE world, for tests that need genuine evidence.

Lifted verbatim from ``tests/scene/test_worlds_fast.py`` (the ``_scan_ctx`` / ``_run_selection``
pair) so the world tests and the scene tests exercise the SAME path: hypotheses come out of the
fitter, selection runs the Bayesian evidence lane, and ``selection_fallback is None`` is
load-bearing — nothing here may pass on the recorded AIC fallback.
"""
from __future__ import annotations

from dataclasses import replace

import numpy as np

from isar.analyze import loop as _loop, select as _select
from isar.analyze.context import ScanContext
from isar.scene.model import CandidateRow, SceneObservation, observation_fingerprint
from isar.session.scan_delivery import _candidate_rows, _energy_bands
from isar.synth import generate, worlds


def scan_ctx(name: str, h: float) -> ScanContext:
    cfg, truth = worlds.build(name)
    data = generate.generate_world(truth)
    hs = generate.world_h_values(cfg)
    i = int(np.argmin(np.abs(hs - h)))
    x = generate.world_energy_grid(cfg)
    return ScanContext(x=x, counts=data.counts[i].astype(float),
                       mask=np.ones(x.size, dtype=bool), T=truth.T, center=0.0,
                       gauss_fwhm=cfg.gauss_fwhm, lorentz_fwhm=cfg.lorentz_fwhm,
                       instrument="S30", energy_status="needs_zero")


def run_selection(name: str, h: float):
    """``(ctx, results, selection, hypotheses)`` for one world node, on the evidence lane."""
    ctx = scan_ctx(name, h)
    # hypotheses_out, not hand re-enumeration: catalogue variants (H9) exist only because a fit
    # came out a certain way, and each carries the derived context its evidence resolves under —
    # a hand-built hyps dict cannot contain them and select_models refuses the orphan label.
    # The same dict is what the persisted component descriptors are derived from: the enumerated
    # structure is the single authority on what a candidate is made of.
    hyps = {}
    results = _loop.fit_structures_two_stage(ctx, hypotheses_out=hyps)
    sel = _select.select_models(results, hyps, ctx)
    assert sel.selection_fallback is None
    return ctx, results, sel, hyps


def _card():
    from isar.schema import card as card_mod

    policy = card_mod.CardPolicy(
        facility="SYNTH", instrument="S30", format_version="2025", plugin="spec_s30",
        detectors=[card_mod.DetectorSpec("d", 0, "d", True)], energy_column="E",
        monitor_column="M", counting_mode="time", normalize_by="none",
        raw_abscissa_kind="energy", energy_status="needs_zero", measurement_pattern="fscan",
        alignment_pattern="ascan", temperature_rule={}, collation={}, fitting={})
    return card_mod.Card(policy, card_mod.CardState())


def deliver(name: str, h: float) -> dict:
    """One world node through the delivery's fit, ``scan_delivery.fit_one``: rung 1 of the count
    ladder, rung 2 when a trigger fires, and the delivered count outcome
    (``fit["fit_selection_semantics"]["count_ladder"]``). The ceiling is the suite's."""
    from isar.session import scan_delivery

    return scan_delivery.fit_one(scan_ctx(name, h), _card(), prior=None, M=1, seed=1,
                                 errors_tier="cheap")


def scene_observation(name: str, h: float, *, scan_ref: str | None = None,
                      delivered: bool = False) -> SceneObservation:
    """One world node as a scene arena, built from the REAL selection output.

    The candidate table goes through ``isar.session.scan_delivery._candidate_rows`` — the same
    reshaping production persists — so the rows a test scores are the rows a campaign would
    persist, not a hand-made approximation of them. The selection record is complete by
    construction: ``run_selection`` refuses anything but the Bayesian lane. ``delivered``: the
    rows and winner :func:`deliver` persists instead, after the count ladder's rung 2.
    """
    if delivered:
        fit = deliver(name, h)
        ctx = scan_ctx(name, h)
        rows, best = fit["candidates"] or [], fit["best_label"]
    else:
        ctx, results, sel, hyps = run_selection(name, h)
        rows = _candidate_rows(sel.scores, {r.label: r for r in results}, hyps) or []
        best = sel.best
    obs = SceneObservation(
        scan_ref=scan_ref or f"world:{h:.2f}:d",
        q=(float(h), 0.0, 0.0),
        temperature_K=float(ctx.T),
        energy_window=(float(ctx.x.min()), float(ctx.x.max())),
        # Through the production helper, not a hand-made pair: the fixture's coverage must be the
        # coverage a delivery would persist, holes and all.
        energy_bands=tuple(tuple(b) for b in _energy_bands(ctx.x, ctx.mask, ctx.center)),
        gauss_fwhm=float(ctx.gauss_fwhm),
        lorentz_fwhm=None if ctx.lorentz_fwhm is None else float(ctx.lorentz_fwhm),
        # Every node of a synthetic world is generated with the same counting statistics, so the
        # arenas carry equal exposure — which is what the scene's detection-power gate needs to
        # see before an absence at one node may exclude a mode seen at another. Left ``None`` the
        # gate would (correctly) refuse to charge anything anywhere on these worlds.
        effective_exposure=1.0,
        background=None,
        best_label=best,
        candidates=tuple(CandidateRow.from_dict(r)
                         for r in sorted(rows, key=lambda r: str(r["label"]))),
        evidence_comparison_status="complete",
        selection_fallback=None,
        arena_fingerprint=None,
        evidence_sink=None,
        sample=None,
        conditions=(("temperature_K", float(ctx.T)),),
        count_trust="trusted",
        lattice_epoch=None,
        fingerprint="")
    return replace(obs, fingerprint=observation_fingerprint(obs))

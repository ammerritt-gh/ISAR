"""SCENE-5 5.4 challenge battery (plan §4), on the FCC world through the real closed loop.

Slow-lane pytest, real physics throughout: no mocked fits, no mocked selection, no mocked
scene build. Every run is bounded (<= 12 rounds, the harness's own 10-minute wall ceiling) and
timed; wall-clock is printed per run.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from drive._closed_loop import fcc_sample_knowledge, fcc_step, run_closed_loop
from isar.scene.agenda import AGENDA_STORE

ACOUSTIC_HYP = {"acoustic": {"preset": "acoustic", "zc_energy": {"mode": 0.0, "sigma": 0.3},
                             "confidence": 0.8}}


def _decision_signature(run) -> tuple:
    """The observable shape of a closed-loop run's placements: the (node, generator) sequence
    the planner actually chose. Two runs that "place measurably differently" (plan §4.1) must
    diverge somewhere in this sequence."""
    return tuple((round(r.node_h, 6) if r.node_h is not None else None,
                 r.plan.reason) for r in run.rounds)


# ----------------------------------------------------------------------------
# 1. Acoustic-priority: the declared-hypothesis A/B (5.2's A2 obligation)
# ----------------------------------------------------------------------------
@pytest.mark.slow
@pytest.mark.xfail(
    strict=True,
    reason="FINDING (SCENE-5 5.4, the 5.2 A2 obligation): identical worlds/seeds, real "
          "closed-loop physics, WITH vs WITHOUT the declared acoustic hypothesis produce "
          "BYTE-IDENTICAL decision sequences -- no sensitivity at all. Root-caused, not a "
          "harness bug: isar.analyze.catalogue.DEFAULT_CATALOGUE declares NO "
          "parameter_priors on any entry, so _identity_candidates() returns () and every real "
          "fit's Hypothesis.identity is None (isar/analyze/catalogue.py:296-308, H9(e)). "
          "isar.scene.phenomena._resolve_hypothesis only ever matches a NAMED identity "
          "(isar/scene/phenomena.py:240-244) -- with identity always None, the declared "
          "hypotheses mapping this test threads through plan_next_action can never reach a "
          "real-fit-derived belief. The mechanism is fully live at the scoring-engine unit "
          "level with hand-set identities (tests/scene/test_scene_eig.py::"
          "test_declared_hypothesis_changes_the_score) -- this is specifically the gap the "
          "plan's 5.2 advisory named: 'the planner-level hypothesis-sensitivity test is "
          "spy-based ... failure to do so is a finding, not a pass.' Per the plan's own "
          "instruction this is reported, not tuned around: closing it needs an owner decision "
          "on identity-conditioned catalogue priors (SCENE-7's calibration), out of this "
          "slice's scope.")
def test_acoustic_priority_declared_hypothesis_moves_real_placements(tmp_path: Path):
    step = fcc_step(spacing=0.1)
    kwargs = dict(sample_knowledge=fcc_sample_knowledge(), max_rounds=4, step=step)

    t0 = time.time()
    with_hyp = run_closed_loop(tmp_path / "with", "fcc_warm", hypotheses=ACOUSTIC_HYP, **kwargs)
    without_hyp = run_closed_loop(tmp_path / "without", "fcc_warm", hypotheses=None, **kwargs)
    print(f"\nacoustic-priority A/B: with={with_hyp.wall_seconds:.1f}s "
         f"without={without_hyp.wall_seconds:.1f}s")

    assert not with_hyp.wall_ceiling_hit and not without_hyp.wall_ceiling_hit
    assert _decision_signature(with_hyp) != _decision_signature(without_hyp), (
        "the declared acoustic hypothesis must move at least one real placement")


# ----------------------------------------------------------------------------
# 2. Full-range: both phonons found and mapped; classification stays honestly inert
# ----------------------------------------------------------------------------
@pytest.mark.slow
def test_full_range_discovery_finds_and_maps_both_phonons(tmp_path: Path):
    # 6 (of the plan's <=12 budget): measured empirically to already confirm BOTH branches
    # present across several tiles -- per-round EIG scoring cost grows with the observation
    # history, so fewer rounds keeps this run well inside the harness's own wall ceiling.
    step = fcc_step(spacing=0.1)
    t0 = time.time()
    run = run_closed_loop(tmp_path, "fcc_warm", sample_knowledge=fcc_sample_knowledge(),
                          max_rounds=6, step=step)
    print(f"\nfull-range: {len(run.rounds)} rounds, wall={run.wall_seconds:.1f}s")

    assert not run.wall_ceiling_hit
    assert len(run.rounds) >= 1

    # A real mode (n_modes >= 1) was found somewhere below the optic energy and somewhere
    # at/above it: neither branch starved for measurement within the round budget.
    found_omegas = [omega for obs in run.observations
                    for cand in obs.candidates if cand.label == obs.best_label
                    for omega in cand.omegas]
    assert any(0.0 < w < 7.0 for w in found_omegas), "the acoustic branch was never resolved"
    assert any(w >= 7.0 for w in found_omegas), "the optic branch was never resolved"

    # ConfirmPresence answers exist at tiles this run actually measured (both phonons mapped,
    # not just glimpsed) -- classification stays honestly inert (H9(e)): no ClassifyIdentity
    # question ever opens, and the agenda's own derivation note says exactly why.
    report_dir = tmp_path / "report"
    current = AGENDA_STORE.read_current(report_dir)
    assert current is not None
    _pointer, payload = current
    specs_by_id = {s["question_id"]: s for s in payload.get("specs") or ()}
    confirm_answers = [a for a in payload.get("assessments") or ()
                       if a["status"] == "answered"
                       and specs_by_id.get(a["question_id"], {}).get("family") == "ConfirmPresence"]
    assert confirm_answers, "no ConfirmPresence question ever closed"

    assert not any(s.get("family") == "ClassifyIdentity" for s in specs_by_id.values()), (
        "classification must stay inert -- no ClassifyIdentity question may open")
    note_names = [n for n, _detail in map(tuple, payload.get("derivation_notes") or ())]
    assert "classify_identity_inert" in note_names


# ----------------------------------------------------------------------------
# 3. Boundary-convergence + temperature A/B
# ----------------------------------------------------------------------------
@pytest.mark.slow
def test_boundary_convergence_temperature_ab_picks_different_probes(tmp_path: Path):
    step = fcc_step(spacing=0.1)
    kwargs = dict(sample_knowledge=fcc_sample_knowledge(), max_rounds=5, step=step)

    t0 = time.time()
    warm = run_closed_loop(tmp_path / "warm", "fcc_warm", **kwargs)
    cold = run_closed_loop(tmp_path / "cold", "fcc_cold", **kwargs)
    print(f"\nboundary-convergence A/B: warm={warm.wall_seconds:.1f}s cold={cold.wall_seconds:.1f}s")

    assert not warm.wall_ceiling_hit and not cold.wall_ceiling_hit

    # The ranking buys discriminating designs, not repeated re-measurement of one point: not
    # every round lands on the same node.
    warm_nodes = {r.node_h for r in warm.rounds if r.node_h is not None}
    cold_nodes = {r.node_h for r in cold.rounds if r.node_h is not None}
    assert len(warm_nodes) > 1, "warm run re-measured a single point instead of discriminating"
    assert len(cold_nodes) > 1, "cold run re-measured a single point instead of discriminating"

    # No temperature branch anywhere in steering code (D8/D9): the two runs share one step,
    # one sample declaration, one policy -- ONLY the world's own physics (T=300 vs T=5) can
    # move the probe set, through the Bose factor already inside the predictive.
    assert warm_nodes != cold_nodes or _decision_signature(warm) != _decision_signature(cold), (
        "warm and cold boundary runs chose an identical probe set -- the Bose-weighted "
        "-E corroboration probe should distinguish them")

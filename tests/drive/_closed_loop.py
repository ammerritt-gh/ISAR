"""The SCENE-5 5.4 closed-loop harness: real planning, real physics, real scene BUILD.

Per round: (1) ``plan_next_action`` on the current stored scene revision, (2) take the
winning design, (3) look up deterministic synth counts at that node through
``isar.synth.generate`` (the world's whole spectrum — SCENE-5's synth worlds carry no
window/ncount acquisition lever, see the module note below), (4) run the real fit+select
through :func:`run_selection_tolerant` (a fallback-tolerant sibling of
``scene._world_helpers.run_selection`` — a non-Bayesian outcome is recorded exactly as
production records it and the round CONTINUES) then reshape through the production
``_candidate_rows``, (5) append the observation and rebuild the real scene via
``isar.scene.build.build_scene``, stored through the scene store, (6) repeat.

Deliberately test-local (plan §5.4): NOT ``build_and_store(root)`` — workspace measurement
intake (measurements.jsonl/results.csv) is out of scope and already pinned by its own tests.
The claim this harness supports is narrower and exact: real planning, real physics, real
scene BUILD.

**Window/ncount judgment call.** ``isar.synth.generate`` has no per-window or
per-monitor-count regeneration lever — every SCENE world test (``scene._world_helpers``
included) fits the WHOLE generated spectrum at a node, never a windowed subset, and every
node of a world carries the same fixed counting statistics regardless of any design's
``ncount``. This harness follows that same, already-reviewed contract: the winning design's
node selects WHICH world row is fit; its window and ncount are not literal acquisition
inputs, because the synth machinery does not expose them as one. ``effective_exposure`` is
recorded as 1.0 uniformly, matching every other SCENE world fixture.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping

from isar.drive.program_runner import _selected_intent
from isar.drive.steering.cold_start import declare_partition
from isar.drive.steering.planner import SELECTED, PlanResult, plan_next_action
from isar.schema.campaign_program import (
    CampaignProgram, CampaignStep, DiscoverAndMapSpec, program_fingerprint)
from isar.schema.question_policy import resolve_policy
from isar.scene.build import SceneRevision, build_scene
from isar.scene.model import CandidateRow, SampleKnowledge, SceneObservation, observation_fingerprint
from isar.scene.store import SCENE_STORE
from isar.session.scan_delivery import _candidate_rows, _energy_bands

from scene._world_helpers import scan_ctx

from isar.analyze import loop as _loop, select as _select

#: Bounds from the plan's 5.4 acceptance closure: never more rounds, never longer wall time.
MAX_ROUNDS = 12
WALL_CEILING_S = 600.0

#: The FCC step's declared path — H varies, K and L are fixed — so the winning intent's
#: varying coordinate is always ``hkl[0]``. Hardcoded rather than re-derived (the planner's
#: own ``_scoring_axis`` is private machinery this harness need not duplicate): every world
#: and every battery run in this slice shares this one declared path.
_H_AXIS = 0


def fcc_step(step_id: str = "fcc-discover-1", *, spacing: float = 0.1) -> CampaignStep:
    """The one FCC discovery step every closed-loop run plans against.

    ``spacing`` is the candidate q-tile quantum (``maximum_initial_spacing_rlu``) — 0.1 gives
    11 candidate nodes across the declared [2, 3] domain, enough for a multi-round closed
    loop to show real placement movement without the ceremony of a denser lattice.
    """
    spec = DiscoverAndMapSpec(
        q_start=(2.0, 0.0, 0.0), q_end=(3.0, 0.0, 0.0),
        maximum_initial_spacing_rlu=spacing, intrinsic_omega_domain_meV=(0.0, 10.0),
        catalogue_profile="isar.catalogue.default/1",
        resolved_policy=resolve_policy(user_ask={"min_effective_exposure": 1.0}))
    return CampaignStep(id=step_id, kind="discover_and_map", payload=spec)


def fcc_sample_knowledge(dispersion_slope_max: float = 10.0) -> SampleKnowledge:
    """The FCC test sample's declared geometry (plan §5.4): zone period 1 rlu (folds at
    every integer H), only H=2 and H=4 declared as real reflections — H=3 is a fold with NO
    declared reflection, the extinct fold the acoustic branch's boundary maximum sits on.
    """
    return SampleKnowledge(zone_period=1.0, reflections=(2.0, 4.0),
                           dispersion_slope_max=dispersion_slope_max)


def _status_from_selection(sel) -> str:
    """The ``evidence_comparison_status`` a non-Bayesian ``Selection`` earns.

    Mirrors ``isar.workspace_api.scene._evidence_comparison``'s persisted-record mapping,
    applied directly to the in-memory :class:`isar.analyze.select.Selection` this harness
    just produced instead of a ``results.csv`` row (H7: only a completed Bayesian comparison
    with no fallback may score; ``evidence_budget_exhausted`` is a genuine incomplete
    comparison; every other fallback, or no scores at all, is unavailable evidence).
    """
    if not sel.scores:
        return "unavailable"
    fallback = sel.selection_fallback
    if fallback is None:
        return "complete"
    if fallback == "evidence_budget_exhausted":
        return "incomplete"
    return "unavailable"


def run_selection_tolerant(world_name: str, h: float):
    """``(ctx, results, sel, hyps)`` at one world node — real fit, real selection, but
    WITHOUT ``scene._world_helpers.run_selection``'s ``selection_fallback is None`` assertion.

    A non-Bayesian outcome (AIC fallback, budget exhaustion) is a legitimate closed-loop
    round here, not a test failure: the plan's 5.4 contract records it production-faithfully
    and continues.
    """
    ctx = scan_ctx(world_name, h)
    hyps: dict = {}
    results = _loop.fit_structures_two_stage(ctx, hypotheses_out=hyps)
    sel = _select.select_models(results, hyps, ctx)
    return ctx, results, sel, hyps


def _build_observation(*, scan_ref: str, h: float, ctx, sel, rows, status: str,
                       declared, program: CampaignProgram, step: CampaignStep,
                       sample_id: str) -> SceneObservation:
    arena = (getattr(sel.evidence, "arena_fingerprint", None) if status == "complete" else None)
    obs = SceneObservation(
        scan_ref=scan_ref, q=(float(h), 0.0, 0.0), temperature_K=float(ctx.T),
        energy_window=(float(ctx.x.min()), float(ctx.x.max())),
        energy_bands=tuple(tuple(b) for b in _energy_bands(ctx.x, ctx.mask, ctx.center)),
        gauss_fwhm=float(ctx.gauss_fwhm),
        lorentz_fwhm=None if ctx.lorentz_fwhm is None else float(ctx.lorentz_fwhm),
        effective_exposure=1.0,
        background=None, best_label=sel.best,
        candidates=tuple(CandidateRow.from_dict(r)
                         for r in sorted(rows, key=lambda r: str(r["label"]))),
        evidence_comparison_status=status,
        selection_fallback=sel.selection_fallback,
        arena_fingerprint=arena,
        evidence_sink=None,
        sample=sample_id,
        conditions=(("temperature_K", float(ctx.T)),),
        count_trust="trusted",
        lattice_epoch=declared.lattice_epoch,
        fingerprint="",
        topology_role="canonical_path",
        program_fingerprint=program_fingerprint(program),
        program_step=step.id,
        path_fingerprint=declared.path_fingerprint)
    return replace(obs, fingerprint=observation_fingerprint(obs))


@dataclass(frozen=True)
class RoundOutcome:
    index: int
    plan: PlanResult
    node_h: float | None
    selection_fallback: str | None
    evidence_status: str | None
    fit_seconds: float


@dataclass(frozen=True)
class ClosedLoopRun:
    world_name: str
    rounds: tuple[RoundOutcome, ...]
    scene_revisions: tuple[SceneRevision, ...]     # index 0 is the empty round-0 revision
    observations: tuple[SceneObservation, ...]
    wall_seconds: float
    wall_ceiling_hit: bool
    stopped_outcome: str | None


def run_closed_loop(workspace_root: Path, world_name: str, *, sample_knowledge: SampleKnowledge,
                    sample_id: str = "fcc_test_sample", step: CampaignStep | None = None,
                    hypotheses: Mapping[str, dict] | None = None,
                    temperature_K: float | None = None, lattice_epoch: int | None = 1,
                    condition_t_round: float = 5.0, max_rounds: int = MAX_ROUNDS,
                    wall_ceiling_s: float = WALL_CEILING_S) -> ClosedLoopRun:
    """Run the SCENE-5 5.4 closed loop against ``world_name`` for up to ``max_rounds`` rounds.

    Round 0 is an empty-observations ``build_scene`` revision, stored before the loop starts
    (the plan's round-0 clause); the declared partition is minted once, from the harness's
    OWN declarations (never a card), and passed to every ``plan_next_action`` call exactly as
    ``program_runner.py`` passes its one ``declared`` partition across a whole campaign.

    ``workspace_root`` is the workspace directory (production's layout: a ``report/``
    subdirectory the store writes into and ``_selected_intent`` resolves its inventory
    reference against) — NOT the report directory itself, matching every real campaign call
    site (``program_runner.py``'s own ``report_dir`` is always ``<workspace>/report``).
    """
    step = step or fcc_step()
    program = CampaignProgram(steps=(step,))
    report_dir = Path(workspace_root) / "report"
    report_dir.mkdir(parents=True, exist_ok=True)

    conditions = () if temperature_K is None else (("temperature_K", float(temperature_K)),)
    declared = declare_partition(step, sample=sample_id, conditions=conditions,
                                 lattice_epoch=lattice_epoch, condition_t_round=condition_t_round)

    observations: tuple[SceneObservation, ...] = ()
    revision = build_scene(observations, sample=sample_knowledge)
    SCENE_STORE.write_revision(report_dir, revision)
    revisions = [revision]
    rounds: list[RoundOutcome] = []

    started = time.monotonic()
    wall_hit = False
    stopped_outcome: str | None = None
    for i in range(max_rounds):
        if time.monotonic() - started > wall_ceiling_s:
            wall_hit = True
            break
        plan = plan_next_action(report_dir, program, step.id, declared_partition=declared,
                                hypotheses=hypotheses, scene_pin=revision.digest)
        stopped_outcome = plan.outcome
        if plan.outcome != SELECTED:
            rounds.append(RoundOutcome(i, plan, None, None, None, 0.0))
            break

        # A second check AFTER planning (EIG scoring can itself run long as the observation
        # history grows) and BEFORE paying for a real fit — tightens kill-and-report so a slow
        # planning step alone does not additionally spend a whole fit past the ceiling.
        if time.monotonic() - started > wall_ceiling_s:
            wall_hit = True
            break

        intent, _associations = _selected_intent(report_dir, plan)
        h = float(intent.hkl[_H_AXIS])
        t0 = time.monotonic()
        ctx, results, sel, hyps = run_selection_tolerant(world_name, h)
        fit_seconds = time.monotonic() - t0
        status = _status_from_selection(sel)
        rows = _candidate_rows(sel.scores, {r.label: r for r in results}, hyps) or []
        obs = _build_observation(
            scan_ref=f"{world_name}:round{i}:H{h:.3f}", h=h, ctx=ctx, sel=sel, rows=rows,
            status=status, declared=declared, program=program, step=step, sample_id=sample_id)
        observations = observations + (obs,)
        revision = build_scene(observations, sample=sample_knowledge)
        SCENE_STORE.write_revision(report_dir, revision)
        revisions.append(revision)
        rounds.append(RoundOutcome(i, plan, h, sel.selection_fallback, status, fit_seconds))

    wall_seconds = time.monotonic() - started
    return ClosedLoopRun(world_name, tuple(rounds), tuple(revisions), observations,
                         wall_seconds, wall_hit, stopped_outcome)

"""``run_program`` — one campaign-program step, run end to end (SCENE-4.3 C5, D7).

This is the seam the plan calls minimal on purpose: **one function runs ONE step and hands its
caller three things** — the step result, the completion verdict, and the beam directive. There is
no queue, no scheduler, and no multi-step sequencing here; SCENE-6 owns unifying this with
``run_campaign``'s external contract, and the authority flip is 4.4's.

``verdict`` and ``directive`` are structurally separate (D7). A verdict says whether the step's
QUESTIONS are answered; a directive says what the BEAM should do next. Collapsing them — "complete
therefore idle" — is exactly the conflation D7 forbids: a step can be complete while the program
still has work, and a step can end unanswered while the beam must be released.

The discovery lane, per delivery
--------------------------------
``plan_next_action`` → (``selected``) realize the intent → submit through the EXISTING acquisition
path → the delivery folds → the campaign parent builds the scene → the next plan is PINNED to the
digest that build returned. Never ``current.json``: the pin is the authority (D1), so a concurrent
read-only ``isar report --scene`` publish cannot move what this campaign steers from.

The build runs in a SUBPROCESS the parent supervises (owner ruling 2026-08-18). That is not a
performance choice — it is how this module respects the architectural boundary
(``tests/drive/test_boundary.py``): ``isar.drive`` may not import ``isar.session``, and the one
canonical scene build lives there. The seam below is process-NEUTRAL: a future persistent worker
slots in behind :func:`build_scene_revision` without any caller changing.

Stopping
--------
Discovery stop accounting runs EXCLUSIVELY through :func:`~isar.scene.agenda.evaluate_agenda`'s
verdict. There is not one call into ``evaluate_stop`` on this path: that evaluator prices a
targeted objective's per-q tolerance, which a target-free discovery step does not have, and
letting it speak here would invent a stop reason outside the four-reasons law (user stop token /
completion / budget / machine+controller fault). Budget is priced by the SAME evaluator, from the
same ``budget_state`` the acquisition path charges — so "over budget" is one accounting, not two.

:data:`VERDICT_IN_PROGRESS` is not a fifth stop reason. It is ``AgendaVerdict.state`` verbatim: the
step produced nothing more to do on this pass (no eligible action, honest exhaustion, or a request
the instrument declined) while its questions remain open. Whether the CAMPAIGN stops on that is the
caller's decision, not this function's.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, NamedTuple

from isar.drive.acquisition.acquire import _resolve_fwhm
from isar.drive.acquisition.scan_design import (
    BudgetInsufficientForMinimumBatch, InstrumentLimitBelowMinimumBatch,
    InstrumentPointLimitExceeded)
from isar.drive.instrument.tavi_client import TaviError
from isar.drive.objective import ObjectiveSpec
from isar.drive.session_log import AnalysisLimitExceeded, log_event, timed
from isar.drive.steering.cold_start import CANONICAL_PATH, declare_partition
from isar.drive.steering.planner import (
    CONTROLLER_FAULT, SELECTED, PlanResult, plan_next_action)
from isar.drive.steering.realization import DiscoveryWindowUnrealizable, proposal_from_intent
from isar.scene.agenda import AGENDA_STORE, evaluate_agenda
from isar.schema.campaign_program import CampaignProgram, ProgramError
from isar.schema.card import Card
from isar.schema.hypotheses import content_fingerprint
from isar.schema.measurement import current_lattice_epoch

_LOG = logging.getLogger(__name__)

__all__ = [
    "BEAM_HOLD", "BEAM_RELEASE", "MAX_DISCOVERY_DELIVERIES_PER_STEP", "ProgramStepOutcome",
    "SceneBuildFailed", "SceneBuildReport", "StepResult", "StalenessModeRefused",
    "VERDICT_COMPLETE", "VERDICT_IN_PROGRESS", "VERDICT_MACHINE_FAILURE",
    "VERDICT_OPERATOR_STOPPED", "VERDICT_OVER_BUDGET", "WorkspaceUndeclared",
    "build_scene_revision", "run_program",
]

# ---------------------------------------------------------------------------- vocabulary
#: The four-reasons stop law, in this seam's words. Every one of these ends the step.
VERDICT_COMPLETE = "complete"                     # the agenda answered its questions
VERDICT_OVER_BUDGET = "over_budget"               # the declared budget tripped
VERDICT_OPERATOR_STOPPED = "operator_stopped"     # the cooperative stop token appeared
VERDICT_MACHINE_FAILURE = "machine_failure"       # controller fault / failed synchronous build
#: NOT a stop reason — see the module docstring.
VERDICT_IN_PROGRESS = "in_progress"

#: Beam directives. Deliberately a separate axis from the verdict (D7).
BEAM_RELEASE = "release"      # nothing further in this program needs the beam
BEAM_HOLD = "hold"            # a later step still does

#: Runaway backstop, named provisional v1 — the discovery analogue of
#: :data:`isar.drive.loop._ABSOLUTE_ITERATION_CEILING`. The REAL bound is the declared budget,
#: priced by ``evaluate_agenda`` from the same ``budget_state`` the acquisition path charges; this
#: only stops a campaign with NO declared budget from measuring forever. Override per run with
#: ``config["max_deliveries"]``.
MAX_DISCOVERY_DELIVERIES_PER_STEP = 200

#: The subprocess build seam's own timeout, seconds. A scene build measured 7.4–22.7 s in 4.2;
#: this is generous enough that a slow box never trips it and short enough that a hung child is
#: a machine failure within the delivery rather than an overnight stall.
SCENE_BUILD_TIMEOUT_SECONDS = 900.0


class StalenessModeRefused(ValueError):
    """The step's frozen policy asks for a staleness mode this build does not implement."""


class WorkspaceUndeclared(ValueError):
    """A discovery step was asked to run against a workspace with no card.

    Not a gap — a consequence. Cold start is "declared knowledge all the way down" (owner ruling
    ``823502e``), and the card IS where the declarations live: the sample identity, the condition
    setpoint, the ``collation.t_round`` binning, the lattice epoch. Without it there is nothing to
    mint a declared partition FROM, so the honest answer is to say so rather than to invent one.
    """


class SceneBuildFailed(RuntimeError):
    """The supervised scene build did not produce a durable revision. A machine failure.

    Never a stale-scene fallback: steering from the previous revision after a failed build is
    exactly the silent wrong answer the synchronous staleness mode exists to prevent.
    """


# ---------------------------------------------------------------------------- results
@dataclass(frozen=True, slots=True)
class SceneBuildReport:
    """What one supervised build did. ``digest`` is durable whatever ``pointer_flipped`` says."""

    digest: str
    pointer_flipped: bool
    unchanged: bool
    seconds: float


@dataclass(frozen=True)
class StepResult:
    """Everything one step did, in the order it did it.

    Timings live HERE and in the session log, never in ``decisions.jsonl``: that ledger is
    clock-free by contract, because a wall-clock field in it would make same-seed replay produce
    different bytes.
    """

    step_id: str
    kind: str
    deliveries: int = 0
    plan_outcome: str | None = None
    reason: str | None = None
    scene_digests: tuple[str, ...] = ()
    submitted_fingerprints: tuple[str, ...] = ()
    declines: tuple[str, ...] = ()
    build_seconds: tuple[float, ...] = ()
    declared_partition_id: str | None = None
    blocking_questions: tuple[str, ...] = ()
    consultation_requests: tuple[dict, ...] = ()
    observations: list = field(default_factory=list, repr=False)


class ProgramStepOutcome(NamedTuple):
    """``(step result, completion verdict, beam directive)`` — the D7 triple, in that order."""

    result: StepResult
    verdict: str
    directive: str


# ---------------------------------------------------------------------------- the build seam
def build_scene_revision(workspace, *, source: str = "campaign",
                         timeout: float = SCENE_BUILD_TIMEOUT_SECONDS) -> SceneBuildReport:
    """Build ONE scene revision for ``workspace`` in a supervised subprocess.

    The child is ``isar scene-build``, which prints exactly one structured JSON line and exits 0
    when a durable revision exists — INCLUDING the case where a racing publisher won the pointer
    CAS (the blob is content-addressed and already written, so the parent pins its computed digest
    and proceeds; the pointer is not campaign authority). Any other exit, or a line this parent
    cannot parse, is a :class:`SceneBuildFailed` — a machine failure, never a fall back onto
    whatever the pointer currently names.
    """
    command = [sys.executable, "-m", "isar.cli.main", "scene-build",
               "--workspace", str(workspace), "--source", str(source)]
    started = time.perf_counter()
    try:
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise SceneBuildFailed(
            f"scene build did not finish within {timeout:g}s: {error}") from error
    except OSError as error:
        raise SceneBuildFailed(f"could not start the scene build: {error}") from error
    seconds = time.perf_counter() - started
    if completed.returncode != 0:
        raise SceneBuildFailed(
            f"scene build exited {completed.returncode}: "
            f"{(completed.stderr or '').strip()[:400]}")
    lines = [line for line in (completed.stdout or "").splitlines() if line.strip()]
    if not lines:
        raise SceneBuildFailed("scene build exited 0 but printed no report line")
    try:
        report = json.loads(lines[-1])
        digest = str(report["digest"])
        return SceneBuildReport(digest=digest,
                                pointer_flipped=bool(report["pointer_flipped"]),
                                unchanged=bool(report["unchanged"]), seconds=seconds)
    except (ValueError, KeyError, TypeError) as error:
        raise SceneBuildFailed(
            f"scene build report line is unreadable ({error}): {lines[-1][:200]!r}") from error


# ---------------------------------------------------------------------------- step preconditions
def _require_synchronous_staleness(step) -> str:
    """The step's frozen staleness mode, or a loud refusal.

    Only ``synchronous`` is implemented: the parent builds the scene and plans against THAT
    revision, in that order, every delivery. ``fixed_lag`` and ``batch_N`` are real designs with
    real consequences for what a decision is allowed to have seen, and accepting-then-ignoring one
    would silently run a campaign under semantics its program does not declare. The compiler
    already refuses an unsupported mode (``isar.schema.question_policy.validate_resolved_block``);
    this is the run-time half of the same refusal, so a program that reached this build by any
    other route still cannot run under a mode nobody implemented.
    """
    policy = dict(getattr(step.payload, "resolved_policy", None) or {})
    staleness = dict(policy.get("staleness_policy") or {})
    mode = staleness.get("mode")
    if mode != "synchronous":
        raise StalenessModeRefused(
            f"step {step.id!r} declares staleness mode {mode!r} (lag {staleness.get('lag')!r}); "
            "this build implements 'synchronous' only and REFUSES rather than running the "
            "campaign under semantics it does not honour")
    return str(mode)


def _declared_partition(cfg, step):
    """Mint the step's cold-start declared partition from the CARD's declarations (D4).

    Every component is a declaration a human wrote, read through the one ``partition_key`` code
    path: ``sample`` is the campaign's stamped sample identity, ``conditions`` the card's declared
    setpoint, ``condition_t_round`` the card's own collation binning (the same value Stage II and
    the scene builder use), ``lattice_epoch`` the highest declared epoch, and the path the step's
    own declared endpoints. Always passed to the planner: retirement is the planner's job, and
    since C3 the declaration goes inert by itself once the step has produced an observation.
    """
    policy_path = Path(cfg.workspace) / "card.policy.toml"
    if not policy_path.exists():
        raise WorkspaceUndeclared(
            f"discovery step {step.id!r} needs a declared workspace: {policy_path} does not "
            "exist, so there is no sample identity, condition setpoint, binning policy or "
            "lattice epoch to mint the cold-start partition from. Run `isar init` on a probe "
            "scan first.")
    card = Card.load(policy_path, Path(cfg.workspace) / "card.state.json")
    setpoint = dict(card.policy.temperature_rule or {}).get("setpoint")
    conditions = () if setpoint is None else (("temperature_K", float(setpoint)),)
    return declare_partition(
        step, sample=cfg.sample, conditions=conditions,
        lattice_epoch=current_lattice_epoch(getattr(card.state, "lattice_epochs", None) or []),
        condition_t_round=float(dict(card.policy.collation or {}).get("t_round", 5.0)))


def _declared_hypotheses_fingerprint(cfg) -> str | None:
    """The card's declared hypothesis set, fingerprinted — what a discovery request stamps.

    4.2b left ``MeasurementIntent.hypotheses_fingerprint`` ``None`` and said in as many words that
    "the 4.3 driver stamps hypotheses" (``isar.drive.steering.generators``). A discovery step
    declares no steering block of its own, so the second component of
    :func:`~isar.schema.hypotheses.content_fingerprint` is empty — the fingerprint is over the
    DECLARED PHYSICS alone, which is the whole of what a target-free campaign brings to a scan.
    """
    policy_path = Path(cfg.workspace) / "card.policy.toml"
    if not policy_path.exists():
        return None
    from isar.schema.card import load_policy
    return content_fingerprint(list(load_policy(policy_path).hypotheses or []), {})


# ---------------------------------------------------------------------------- the discovery lane
def _agenda_verdict(report_dir: Path, plan: PlanResult, budget_state: dict):
    """``evaluate_agenda`` over the agenda THIS decision published, priced against the budget.

    Read by content address, not through the pointer: the planner already told us which revision
    it wrote, and re-reading ``current.json`` would let a concurrent writer decide this step's
    completion. The planner's own verdict is recomputed here rather than returned by it because
    ``plan_next_action`` deliberately knows nothing about budgets.
    """
    return evaluate_agenda(AGENDA_STORE.read_revision(report_dir, plan.agenda_digest),
                           budget_state=budget_state)


def _selected_intent(report_dir: Path, plan: PlanResult):
    """The winning evaluation's :class:`MeasurementIntent`, rebuilt FROM THE PERSISTED INVENTORY.

    Deliberately not carried out of ``plan_next_action`` in memory. The D1 invariant is that no
    state capable of changing a selected action lives outside a persisted record, and reading the
    winner back out of the artifact the decision addressed makes same-seed replay a property of
    construction rather than of care: a replay reads the identical bytes.

    The intent is rebuilt through :func:`~isar.schema.action_identity.intent_from_mapping` — the
    ONE canonical constructor for a persisted intent map — rather than field by field here. Hand-
    listing the fields is how ``window_center_meV`` was silently dropped and every clamped rival
    probe re-aimed at its window's midpoint; a constructor that validates the whole mapping cannot
    lose a field a caller forgot to copy, and a hand-edited artifact still fails identity below.
    """
    from isar.schema.action_identity import intent_from_mapping

    reference = plan.inventory_ref or {}
    relative = str(reference["path"])
    # The planner writes this path itself, under ``report/``. Checking it rather than positionally
    # dropping the first segment means a reference that is not one refuses instead of resolving to
    # some other file of the workspace.
    if not relative.startswith("report/"):
        raise RuntimeError(
            f"the decision's inventory reference {relative!r} is not a workspace report path; "
            "refusing to read an action from it")
    path = report_dir.parent / relative
    blob = json.loads(path.read_text(encoding="utf-8"))
    rows = [row for row in blob["evaluations"]
            if row.get("scientific_action_fingerprint") == plan.selection_fingerprint]
    if len(rows) != 1:
        raise RuntimeError(
            f"the persisted inventory {path} holds {len(rows)} evaluation(s) for the selected "
            f"fingerprint {plan.selection_fingerprint}; refusing to guess which action was chosen")
    intent = intent_from_mapping(rows[0]["intent"])
    if intent.scientific_action_fingerprint != plan.selection_fingerprint:
        raise RuntimeError(
            f"the persisted intent in {path} does not hash to the fingerprint the decision "
            f"selected ({plan.selection_fingerprint}); refusing to submit it")
    return intent


def _campaign_provenance_block(program: CampaignProgram, step, declared) -> dict:
    """The four C1 fields a discovery request carries to the measurement-arena record (D2/D3).

    ``path_fingerprint`` comes from the declared partition, which computed it from the STEP's own
    endpoints through ``path_geometry_fingerprint`` — one derivation, so the partition a scan is
    declared into and the partition its record lands in cannot disagree.
    """
    from isar.schema.campaign_program import program_fingerprint
    return {"topology_role": CANONICAL_PATH,
            "program_fingerprint": program_fingerprint(program),
            "program_step": step.id,
            "path_fingerprint": declared.path_fingerprint}


def _run_discovery_step(cfg, program: CampaignProgram, step, *, objective: ObjectiveSpec,
                        budget_state: dict, max_deliveries: int) -> tuple[StepResult, str]:
    """One ``discover_and_map`` step, delivery by delivery. Returns ``(result, verdict)``."""
    from isar.drive.loop import _execute_proposal, _operator_stop_requested

    report_dir = Path(cfg.workspace) / "report"
    _require_synchronous_staleness(step)
    declared = _declared_partition(cfg, step)
    hypotheses_fingerprint = _declared_hypotheses_fingerprint(cfg)
    provenance = _campaign_provenance_block(program, step, declared)

    result = StepResult(step_id=step.id, kind=step.kind,
                        declared_partition_id=declared.partition_id)
    observations: list[dict] = []
    consumed: set = set()
    scan_kind: dict = {}
    scan_counter = 0

    def ended(verdict: str, *, reason: str | None = None, agenda=None) -> tuple[StepResult, str]:
        log_event(cfg.log_path, "decide", {
            "event": "program_step_end", "action": "program_step_end", "step_id": step.id,
            "verdict": verdict, "reason": reason, "deliveries": result.deliveries})
        finished = replace(result, reason=reason, observations=observations)
        if agenda is not None:
            finished = replace(finished, blocking_questions=tuple(agenda.blocking),
                               consultation_requests=tuple(agenda.consultation_requests))
        return finished, verdict

    # The step's own opening scene. Built through the same supervised seam as every delivery's, so
    # the first plan is pinned exactly like the rest and this path NEVER reads ``current.json``.
    try:
        build = _build_scene(cfg, iteration=None)
    except SceneBuildFailed as error:
        return ended(VERDICT_MACHINE_FAILURE, reason=f"scene_build_failed: {error}")
    pin = build.digest
    result = replace(result, scene_digests=(pin,), build_seconds=(build.seconds,))

    while True:
        if _operator_stop_requested(cfg, result.deliveries):
            log_event(cfg.log_path, "decide",
                      {"event": "operator_stop", "action": "operator_stop",
                       "iter": result.deliveries, "token": cfg.control_token})
            return ended(VERDICT_OPERATOR_STOPPED, reason=f"token {cfg.control_token}")

        plan = plan_next_action(report_dir, program, step.id,
                                declared_partition=declared, scene_pin=pin)
        result = replace(result, plan_outcome=plan.outcome)
        if plan.outcome == CONTROLLER_FAULT:
            return ended(VERDICT_MACHINE_FAILURE, reason=f"controller_fault: {plan.reason}")

        try:
            agenda = _agenda_verdict(report_dir, plan, budget_state)
        except ValueError as error:
            # The revision store is LOUD about a pinned blob it cannot vouch for — missing,
            # unparseable, or not hashing to the name it is stored under. That is store
            # corruption, which is a machine failure with a recorded reason, not an unhandled
            # exception that reaches the CLI as a bare exit 2 with no verdict attached.
            return ended(VERDICT_MACHINE_FAILURE, reason=f"store_corrupt: {error}")
        if agenda.state == "complete":
            return ended(VERDICT_COMPLETE, agenda=agenda)
        if agenda.state == "incomplete_over_budget":
            return ended(VERDICT_OVER_BUDGET, reason="budget_exhausted", agenda=agenda)
        if plan.outcome != SELECTED:
            # ``agenda_complete`` cannot reach here (the verdict above already caught it); what
            # can is ``no_eligible_action`` / ``no_action_with_honest_exhaustion`` — the step has
            # nothing more to do while its questions stay open. Honest, and not a stop reason.
            return ended(VERDICT_IN_PROGRESS, reason=plan.outcome, agenda=agenda)
        if result.deliveries >= max_deliveries:
            return ended(VERDICT_OVER_BUDGET,
                         reason=f"max_deliveries {max_deliveries} reached with no declared "
                                "budget to trip; the campaign is not converging",
                         agenda=agenda)

        try:
            intent = _selected_intent(report_dir, plan)
        except RuntimeError as error:
            # An artifact that names no single winner, that does not hash to the fingerprint the
            # decision selected, or whose reference is not a report path. Same class as the store
            # corruption above: a recorded machine failure, never a guess at what was chosen.
            return ended(VERDICT_MACHINE_FAILURE, reason=f"inventory_unusable: {error}",
                         agenda=agenda)
        # The PREDICTED energy the action is about, read back off the persisted intent; the
        # midpoint is the fallback only when the action declares no centre. Aiming the resolution
        # query (and, through it, the request) at the midpoint of a clamped window would resolve
        # the instrument somewhere the question is not about.
        centre = (intent.window_center_meV if intent.window_center_meV is not None
                  else 0.5 * (float(intent.energy_lo) + float(intent.energy_hi)))
        try:
            proposal = proposal_from_intent(
                intent, fwhm_meV=_resolve_fwhm(cfg.resolution_cb, *intent.hkl, centre),
                provenance=provenance, hypotheses_fingerprint=hypotheses_fingerprint,
                reason=f"discovery: {step.id}")
        except DiscoveryWindowUnrealizable as error:
            return ended(VERDICT_MACHINE_FAILURE, reason=f"unrealizable_request: {error}",
                         agenda=agenda)
        proposal.acquisition_id = f"{cfg.run_nonce}:{result.deliveries:08d}"
        log_event(cfg.log_path, "propose", {**proposal.to_dict(), "iter": result.deliveries,
                                            "step_id": step.id})

        try:
            rows, scan_counter, declined = _execute_proposal(
                cfg=cfg, objective=objective, proposal=proposal, it=result.deliveries,
                scan_counter=scan_counter, budget_state=budget_state, observations=observations,
                consumed=consumed, scan_kind=scan_kind)
        except (AnalysisLimitExceeded, BudgetInsufficientForMinimumBatch) as error:
            # Both are budget refusals raised inside the acquisition path — the physical budget
            # cannot afford the minimum batch, or the analysis clock is spent. One of the four
            # reasons, priced by the same accounting as the agenda verdict's.
            return ended(VERDICT_OVER_BUDGET, reason=f"budget_exhausted: {error}", agenda=agenda)
        except (InstrumentLimitBelowMinimumBatch, InstrumentPointLimitExceeded) as error:
            # The instrument is working and REFUSED this request. Not a machine failure and not a
            # budget: the same shape as a geometry decline, and C7's durable-blocker producer is
            # what will turn it into evidence and a re-plan.
            return ended(VERDICT_IN_PROGRESS, reason=f"instrument_refused: {error}", agenda=agenda)
        except TaviError as error:
            # Transport. C6 owns the retry/quarantine ladder; until then a lost instrument is a
            # machine failure, which is the honest answer and a resumable one.
            return ended(VERDICT_MACHINE_FAILURE, reason=f"instrument_unreachable: {error}",
                         agenda=agenda)
        submitted = (proposal.meta or {}).get("proposal_fingerprint")
        if declined is not None:
            # C7 owns turning a geometry decline into durable blocker evidence and re-planning
            # against the SAME pinned scene. Until then the scene has not moved, so a re-plan
            # would re-select the identical action forever: end the step and say why. The decline
            # is recorded on the result BEFORE that return — ``StepResult.declines`` is C7's
            # input, and a decline the step ends on is exactly the one it needs.
            result = replace(result, declines=result.declines + (str(declined),))
            return ended(VERDICT_IN_PROGRESS, reason=f"request_declined: {declined}",
                         agenda=agenda)
        result = replace(
            result, deliveries=result.deliveries + 1,
            submitted_fingerprints=result.submitted_fingerprints + ((submitted,) if submitted
                                                                     else ()))
        _LOG.debug("discovery delivery %d folded %d row(s)", result.deliveries, len(rows))

        try:
            build = _build_scene(cfg, iteration=result.deliveries)
        except SceneBuildFailed as error:
            return ended(VERDICT_MACHINE_FAILURE, reason=f"scene_build_failed: {error}")
        pin = build.digest
        result = replace(result, scene_digests=result.scene_digests + (pin,),
                         build_seconds=result.build_seconds + (build.seconds,))


def _build_scene(cfg, *, iteration: int | None) -> SceneBuildReport:
    """The supervised build plus its timing span. Module-global call so tests can swap the seam."""
    with timed(cfg.log_path, "scene_build", iteration=iteration) as metrics:
        report = build_scene_revision(cfg.workspace)
        metrics["digest"] = report.digest
        metrics["pointer_flipped"] = report.pointer_flipped
        metrics["unchanged"] = report.unchanged
    return report


# ---------------------------------------------------------------------------- the seam
def _carrier_objective(step, config: dict) -> ObjectiveSpec:
    """The :class:`ObjectiveSpec` a discovery step submits THROUGH, not the goal it pursues.

    ``_execute_proposal`` — the one submission path, reused rather than reimplemented — takes an
    objective for its budget accounting, its analysis clock, its instrument-limit resolution and
    its pooled-master bookkeeping. A discovery step has no per-q tolerance and no target branch,
    so this carries the step's declared path and the run's declared budget and NOTHING ELSE
    interpretive: ``tolerance_meV`` is 0.0 because nothing on this path reads it as a target (the
    stopping evaluator is never called), and the default target branch is inert without a target.
    """
    payload = step.payload
    return ObjectiveSpec(
        path=[tuple(payload.q_start), tuple(payload.q_end)],
        tolerance_meV=0.0, budget=config.get("budget"),
        tier=str(config.get("tier") or "deterministic"),
        name=f"discover:{step.id}",
        analysis_limit_seconds=config.get("analysis_limit_seconds"),
        meta={"discover_and_map": True, "step_id": step.id})


def _resolve_step(program: CampaignProgram, step_id: str | None):
    if step_id is None:
        if len(program.steps) != 1:
            raise ProgramError(
                f"run_program needs a step id: this program declares {len(program.steps)} steps "
                "and sequencing them lands in SCENE-6")
        return program.steps[0]
    step = next((s for s in program.steps if s.id == step_id), None)
    if step is None:
        raise ProgramError(f"program has no step {step_id!r}")
    return step


def run_program(program: CampaignProgram, step_id: str | None = None,
                config: dict[str, Any] | None = None) -> ProgramStepOutcome:
    """Run ONE program step end to end (D7).

    Returns ``(step result, completion verdict, beam directive)``. ``config`` is the same dict
    ``run_campaign`` takes — ``workspace`` is required; ``budget``, ``tier``, ``control_token``,
    ``seed``, ``tavi_url``, ``client``, ``run_isar``, ``worker`` and ``max_deliveries`` are
    honoured the same way.

    Only ``discover_and_map`` steps route here in this slice. A ``targeted`` step is refused by
    name rather than adapted: its path is ``run_campaign``, unchanged byte for byte, and the CLI
    keeps sending it there.
    """
    from isar.drive.loop import _campaign_config

    config = dict(config or {})
    step = _resolve_step(program, step_id)
    if step.kind != "discover_and_map":
        raise ProgramError(
            f"run_program executes discover_and_map steps; step {step.id!r} is {step.kind!r} — "
            "a targeted step runs through isar.drive.loop.run_campaign unchanged")

    objective = _carrier_objective(step, config)
    cfg = _campaign_config(objective, config)
    budget = dict(objective.budget or {})
    budget_state = {"kind": budget.get("kind"), "amount": budget.get("amount"),
                    "neutrons": 0.0, "seconds": 0.0, "spent": 0.0}
    declared_ceiling = config.get("max_deliveries")
    max_deliveries = int(MAX_DISCOVERY_DELIVERIES_PER_STEP if declared_ceiling is None
                         else declared_ceiling)

    log_event(cfg.log_path, "decide", {
        "event": "program_step_start", "action": "program_step_start", "step_id": step.id,
        "kind": step.kind, "program_steps": [s.id for s in program.steps],
        "budget": objective.budget, "max_deliveries": max_deliveries})
    try:
        result, verdict = _run_discovery_step(
            cfg, program, step, objective=objective, budget_state=budget_state,
            max_deliveries=max_deliveries)
    finally:
        cfg.runner.close()

    # The directive is about the BEAM, not about the questions (D7): a step whose questions are
    # unanswered still releases the beam when nothing after it needs one, and a complete step
    # holds it when a later step does.
    position = next(i for i, s in enumerate(program.steps) if s.id == step.id)
    more_steps_follow = position + 1 < len(program.steps)
    directive = BEAM_HOLD if (more_steps_follow and verdict not in (
        VERDICT_OPERATOR_STOPPED, VERDICT_MACHINE_FAILURE)) else BEAM_RELEASE
    return ProgramStepOutcome(result=result, verdict=verdict, directive=directive)

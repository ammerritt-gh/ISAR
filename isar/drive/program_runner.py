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

The broken-scan lane, three failure classes (C6)
------------------------------------------------
Bad data is never a stop reason. Each class routes somewhere already built:

1. **Local corruption** — a delivery lands but its counts are untrustworthy. The 4.0 count-trust
   lanes already classify that at delivery time, and ``isar.scene.build`` already denies an
   untrusted arena any footprint or coverage credit, so the next plan still sees the question
   unanswered and re-selects the same action: the RETRY is the existing fail-closed behaviour, and
   all this module adds is a BOUND on it (:func:`_broken_scan_retry_limit`, frozen into the
   program). Quarantine is that same record state — nothing here deletes, masks, or rewrites a
   scan. When the bound is spent the action is retired from THIS STEP's re-submission set and a
   ``broken_scan_offramp`` consultation request is recorded for C8 to turn into a prompt. The step
   keeps planning other actions.
2. **Transient request failure** — transport, not geometry. :class:`TaviError` after the client's
   own bounded ``Retry-After`` ladder is spent; it is recorded through the EXISTING refusal
   vocabulary (``classify_rejection`` classes an unmapped verdict TRANSIENT/scope ``none``), which
   suppresses nothing and produces no durable blocker evidence, and the step re-plans against the
   same pin. Nothing is quarantined: no counts were delivered to distrust.
3. **Global machine/beam failure** — controller-side faults already land as
   :data:`VERDICT_MACHINE_FAILURE`; :data:`MAX_CONSECUTIVE_TRANSPORT_FAILURES` is where an
   instrument that will not talk at all stops being called transient. The stop-law pause IS that
   verdict; there is no second pause mechanism.

**Mixed trust across a multi-arena delivery.** One delivery can write several arena records — one
per detector/analyzer — and class 1 counts the delivery as broken only when NOT ONE of them landed
``trusted``. A delivery that wrote NO arena record at all is broken by the same test: no counts
came back, so nothing entered the scene, so the next plan re-selects the identical action, and the
bound is the only thing that ends it. A single dead analyzer beside healthy ones therefore never advances the retry counter
and never offramps: the delivery bought real coverage, the question moves, and the campaign keeps
measuring. That is deliberate and fail-safe in the direction that matters — the failure mode it
refuses is retiring a physically answerable action because one channel of a multi-analyzer
instrument was sick. Its cost is that a persistently dead channel is invisible to this bound; it
shows up as the region it covers never closing, and a human reads the ``count_trust`` lanes in
``measurements.jsonl`` to see why. Per-detector granularity — a bound that can retire the CHANNEL
rather than the action — is future work, and it needs a per-detector action identity this layer
does not have.

The durable-blocker lane (C7, D5)
---------------------------------
A GEOMETRY decline — the instrument saying it literally cannot perform this measurement — is the
one refusal that becomes durable :class:`~isar.schema.generators.QuestionBlockerEvidence`. The
cycle is: emit evidence for every question the declined action served (stamped with the declining
decision's own digest triple) → record the declined ``scientific_action_fingerprint`` → re-plan
against the SAME pin. The planner re-marks matching evaluations REJECTED/STRUCTURAL in the run
record, so producer rule 6 emits the proof and ``apply_exhaustion`` closes the question
unresolvable. Each cycle rejects at least one previously-eligible action, so it terminates;
:data:`MAX_DECLINE_REPLANS_PER_DELIVERY` is a controller-fault backstop, never the mechanism.
"Geometry" here means scope ``geometry`` in ``classify_rejection``'s one map and nothing else, so a
request-scoped refusal — a bad grid at a reachable q — never enters this lane, and neither does a
transport failure (:func:`_is_geometry_decline` fails closed). Both carried state is per SCENE
REVISION and is dropped the moment a delivery moves the scene.

Known deviations
----------------
* The broken-scan bound is per ACTION, not per detector: a delivery with any trusted arena is not
  broken, so a single dead analyzer in a multi-arena delivery never counts (see above).
* **C9 obligation — the operator surface for the consultation channel.** Answering a prompt today
  means hand-writing ``report/control/prompt-<id>.answer.json``. The Operator Contract half — an
  ``isar answer`` subcommand, or the GUI control that writes the same file through
  :func:`isar.drive.consultation.write_answer` — is owed and is NOT in this slice.

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

Read that precisely, because it is easy to over-read (review finding 2026-08-18). It does NOT mean
the step keeps running: ``_run_discovery_step`` returns, ``run_program`` computes a beam directive,
and for the single-step programs this build executes the beam is RELEASED and ``isar drive`` exits
with the incomplete code. So a campaign does end here, and the four-reasons law is not violated by
that — the law governs why a STEP stops, and "there is nothing further I can do while these
questions stay open" is an honest end, not an invented reason. What the law forbids is dressing
that up as completion, as a budget trip, or as a machine failure. The caller-decides sentence
becomes load-bearing when SCENE-6 sequences steps; today the caller is the CLI and its decision is
to stop.
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

from isar.drive.acquisition.acquire import (
    BudgetInsufficientForMinimumBatch, InstrumentLimitBelowMinimumBatch,
    InstrumentPointLimitExceeded, _resolve_fwhm)
from isar.drive.consultation import ConsultationChannel
from isar.drive.instrument.tavi_client import TaviError
from isar.drive.objective import ObjectiveSpec
from isar.drive.session_log import AnalysisLimitExceeded, log_event, timed
from isar.drive.steering.cold_start import CANONICAL_PATH, declare_partition
from isar.drive.steering.planner import (
    CONTROLLER_FAULT, PROGRAM_STORE, SELECTED, PlanResult, plan_next_action, program_revision)
from isar.drive.steering.realization import DiscoveryWindowUnrealizable, proposal_from_intent
from isar.scene.agenda import AGENDA_STORE, evaluate_agenda
from isar.schema.campaign_program import CampaignProgram, ProgramError, program_fingerprint
from isar.schema.card import Card
from isar.schema.consultation import (
    KEEP_SPENDING, RETIRE_ACTION, RETRY_MORE, apply_consultation_overrides,
    consultation_overrides, strip_consultation_overrides)
from isar.scene.store import ConcurrentRevisionWrite
from isar.schema.generators import (
    DISCOVERY_GENERATOR_REGISTRY_VERSION, QuestionActionAssociation, QuestionBlockerEvidence)
from isar.schema.hypotheses import content_fingerprint
from isar.schema.action_identity import physical_action_key
from isar.schema.measurement import MeasurementArena, current_lattice_epoch

_LOG = logging.getLogger(__name__)

__all__ = [
    "BEAM_HOLD", "BEAM_RELEASE", "GEOMETRY_BLOCKER_NAME", "MAX_CONSECUTIVE_TRANSPORT_FAILURES",
    "MAX_DECLINE_REPLANS_PER_DELIVERY",
    "MAX_DISCOVERY_DELIVERIES_PER_STEP", "OFFRAMP_KIND", "ProgramStepOutcome",
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

#: How many CONSECUTIVE transport failures — no delivery in between — turn "the instrument is
#: having a moment" into "the instrument is gone". Named provisional v1, SCENE-7-calibrated. Below
#: it a :class:`TaviError` is transient and the step re-plans; at it the step ends as a machine
#: failure, which is one of the four reasons and a resumable one. The client's own ``Retry-After``
#: ladder (``isar.drive.instrument.tavi_client._RETRYABLE``) has already run inside each attempt —
#: this counts whole failed attempts, and adds no second retry machinery.
MAX_CONSECUTIVE_TRANSPORT_FAILURES = 3

#: The ONE durable blocker this slice produces (D5, owner ruling 2026-08-18). The durable taxonomy
#: has two members; the other one has NO producer in this build, and
#: ``tests/drive/test_decline_feedback.py`` holds the pin that says so.
GEOMETRY_BLOCKER_NAME = "instrument_geometry_infeasible"

#: The decline → re-plan cycle's hard cap, per delivery. Named provisional v1. Each cycle
#: structurally rejects at least one previously-eligible action, so the REAL bound is the
#: inventory's eligible-action count and this only backstops a controller bug that fails to
#: narrow: exceeding it is a machine failure, never the working mechanism.
MAX_DECLINE_REPLANS_PER_DELIVERY = 8

#: The consultation-request kind C8 turns into an operator prompt. One string, so the producer here
#: and the prompt layer there cannot drift.
OFFRAMP_KIND = "broken_scan_offramp"

#: The 4.0 counts-authoritative log, by name. Duplicated as a literal rather than imported for the
#: same reason ``isar.workspace_api.scene`` duplicates it: this layer may not import
#: ``isar.session`` (tests/drive/test_boundary.py), and the file contract is the boundary.
MEASUREMENTS_FILENAME = "measurements.jsonl"

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
    #: How many times a geometry decline forced a re-plan against the SAME pinned scene (C7).
    decline_replans: int = 0
    build_seconds: tuple[float, ...] = ()
    declared_partition_id: str | None = None
    blocking_questions: tuple[str, ...] = ()
    consultation_requests: tuple[dict, ...] = ()
    #: Every prompt this step MINTED from those requests, in issue order (C8).
    prompts: tuple[dict, ...] = ()
    #: Every program fingerprint this step planned under, in order. Longer than one exactly when a
    #: policy-changing consultation answer compiled a new frozen revision.
    program_fingerprints: tuple[str, ...] = ()
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


def _broken_scan_retry_limit(step) -> int:
    """How many times ONE action may be re-submitted after untrusted counts — read off the program.

    Never a bare constant here (owner ruling 2026-08-18): the budget is instrument-varying, so it
    resolves at compile time through the ordinary precedence chain (user ask > card guidelines >
    the named ISAR default of 2, :mod:`isar.schema.question_policy`) and is FROZEN into the step's
    ``resolved_policy`` block, exactly like the staleness mode above. A program that somehow lacks
    it is refused rather than defaulted: a frozen program never acquires a read-time default.
    """
    policy = dict(getattr(step.payload, "resolved_policy", None) or {})
    thresholds = dict(policy.get("thresholds") or {})
    if "broken_scan_retry_limit" not in thresholds:
        raise ProgramError(
            f"step {step.id!r} froze no broken_scan_retry_limit; recompile the campaign with "
            "`isar drive --goal ... --compile-only` rather than running it under a read-time "
            "default nobody declared")
    return int(thresholds["broken_scan_retry_limit"])


def _prompt_timeout_deliveries(step) -> int:
    """How many DELIVERIES an operator prompt stays open before its declared default applies (C8).

    Read off the frozen program for the same reason the retry budget is (and refused the same way
    when absent): it resolves through the ordinary precedence chain at compile time. Counted in
    deliveries rather than seconds so the same seed replays identically — a wall-clock timeout
    would fork a replay the moment the box was slower.
    """
    policy = dict(getattr(step.payload, "resolved_policy", None) or {})
    thresholds = dict(policy.get("thresholds") or {})
    if "prompt_timeout_deliveries" not in thresholds:
        raise ProgramError(
            f"step {step.id!r} froze no prompt_timeout_deliveries; recompile the campaign with "
            "`isar drive --goal ... --compile-only` rather than running it under a read-time "
            "default nobody declared")
    return int(thresholds["prompt_timeout_deliveries"])


def _discovery_monitor_ncount(step) -> int:
    """The exposure ONE discovery survey point counts to — neutrons on the monitor (C10).

    The owner's ruling declares the exposure DIRECTLY in the currency the wire accepts
    (``number_neutrons``), so there is no time-to-counts conversion to make and no module constant
    to drift: it resolves at compile time through the ordinary precedence chain (user ask > card
    guidelines > the named ISAR default of 1e8) and is frozen into the step, refused when absent
    exactly like the two budgets above.
    """
    policy = dict(getattr(step.payload, "resolved_policy", None) or {})
    thresholds = dict(policy.get("thresholds") or {})
    if "discovery_monitor_ncount" not in thresholds:
        raise ProgramError(
            f"step {step.id!r} froze no discovery_monitor_ncount; recompile the campaign with "
            "`isar drive --goal ... --compile-only` rather than running it under a read-time "
            "default nobody declared")
    return int(thresholds["discovery_monitor_ncount"])


def _count_trust_by_scan_ref(report_dir: Path) -> dict[str, str]:
    """``{scan_ref: count_trust}`` off the 4.0 measurement-arena log, last line per ref wins.

    Read as a FACT, not re-derived: the delivery pipeline owns the five-lane count-trust vocabulary
    and this layer only asks which lane a delivered arena landed in. An absent log is an empty map
    (a workspace whose analysis path writes no arena records has nothing to distrust); an unreadable
    LINE refuses, because a partially-read counts ledger would silently look like a clean delivery.

    Every line is validated through :class:`~isar.schema.measurement.MeasurementArena` — the schema
    that owns the record — rather than by picking two keys out of raw JSON, so a record whose
    ``count_trust`` is outside ``COUNT_TRUST_STATES`` (or whose provenance block is self-
    contradictory) refuses here exactly as it does at the scene-layer door, instead of being read as
    a novel trust lane this loop would silently treat as "not trusted". The LOOP is local rather
    than a call into ``isar.workspace_api.scene._read_measurements``: that reader is private, keys
    on a constructed ``Workspace`` facade instead of a report directory, and reaching it imports the
    whole facade package (views, display, config_gates, scene_view, agenda_view) into every campaign
    process for one twelve-line file read. The schema call above is the part that must not be
    duplicated, and it is not.
    """
    path = Path(report_dir) / MEASUREMENTS_FILENAME
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = MeasurementArena.from_dict(json.loads(line))
        except (ValueError, KeyError, TypeError) as error:
            raise ValueError(f"{path} line {lineno} is unreadable ({error})") from error
        out[record.scan_ref] = record.count_trust
    return out


def _resumed_broken_deliveries(log_path, step_id: str) -> dict[str, int]:
    """``{physical_action_key: untrusted deliveries so far}`` off the session log (H21(b)).

    Derived state, never a second durable store — the same posture ``suppressed_actions`` takes on
    the targeted path. The lane already writes a ``broken_scan`` event carrying the physical key
    and the running count on every untrusted delivery, so the count survives a restart without
    anything new being persisted; the MAX per key is the count, because the events are a running
    tally rather than increments. Without this a restart handed every repeatedly-broken action a
    fresh retry budget, and an action that had already spent it went back to costing full exposure
    per pass.
    """
    from isar.drive.session_log import read_log

    out: dict[str, int] = {}
    for event in read_log(log_path):
        payload = event.get("payload") or {}
        if payload.get("event") != "broken_scan" or payload.get("step_id") != step_id:
            continue
        key = payload.get("physical_action_key")
        if isinstance(key, str):
            out[key] = max(out.get(key, 0), int(payload.get("broken_deliveries") or 0))
    return out


def _declared_partition(cfg, step):
    """Mint the step's cold-start declared partition from the CARD's declarations (D4).

    Every component is a declaration a human wrote, read through the one ``partition_key`` code
    path, and every one is read the way the DELIVERY PIPELINE will read it rather than the way
    this process happens to hold it: ``sample`` and ``conditions`` come from the card through
    :func:`_declared_sample` / :func:`_declared_conditions`, which mirror
    ``isar.session.driver._sample_of`` and ``isar.session.scan_delivery._measurement_record``
    and refuse when the card cannot say what the arena will carry; ``condition_t_round`` is the
    card's own collation binning (the same value Stage II and the scene builder use);
    ``lattice_epoch`` is the highest declared epoch; the path is the step's own declared
    endpoints. That mirroring is what makes the module docstring's "lands on the identical key BY
    CONSTRUCTION" true rather than aspirational — a component this process could invent from its
    own configuration would agree with the record only by coincidence.

    Always passed to the planner: retirement is the planner's job, and since C3 the declaration
    goes inert by itself once the step has produced a trusted observation.
    """
    policy_path = Path(cfg.workspace) / "card.policy.toml"
    if not policy_path.exists():
        raise WorkspaceUndeclared(
            f"discovery step {step.id!r} needs a declared workspace: {policy_path} does not "
            "exist, so there is no sample identity, condition setpoint, binning policy or "
            "lattice epoch to mint the cold-start partition from. Run `isar init` on a probe "
            "scan first.")
    card = Card.load(policy_path, Path(cfg.workspace) / "card.state.json")
    return declare_partition(
        step, sample=_declared_sample(card, step), conditions=_declared_conditions(card, step),
        lattice_epoch=current_lattice_epoch(getattr(card.state, "lattice_epochs", None) or []),
        condition_t_round=float(dict(card.policy.collation or {}).get("t_round", 5.0)))


def _declared_sample(card: Card, step) -> str:
    """The sample identity the DELIVERED ARENA will carry, or a refusal (D4).

    ``isar.session.driver._sample_of`` — the one producer of that field — stamps
    ``card.policy.sample["name"]`` when the card names a sample and falls back to the scan
    record's ``experiment_id`` when it does not. Only the first branch is knowable before any scan
    exists, so a card that does not name its sample cannot mint a partition its own measurement
    will join, and minting one anyway is exactly the orphan this module refuses for an unbinnable
    condition. A workspace relying on the ``experiment_id`` fallback has unstable partition
    identity in any case — two scans under different experiment ids already separate.

    Deliberately NOT ``cfg.sample``. That value is what the driver tells the instrument to
    simulate, it is never read by the delivery pipeline, and it carries a synthetic default
    (``isar.drive.loop._campaign_config``) that no card is obliged to match — so a declaration
    built from it agreed with the arena only by coincidence.
    """
    name = (card.policy.sample or {}).get("name")
    if not name:
        raise WorkspaceUndeclared(
            f"discovery step {step.id!r} cannot mint its cold-start partition: the card declares "
            "no [sample] name, so the sample identity the delivered arena will carry is the scan "
            "record's experiment_id — a value no declaration can know before a scan exists. "
            "Declare `name` under [sample] in card.policy.toml.")
    return str(name)


def _declared_conditions(card: Card, step) -> tuple:
    """The condition setpoint the DELIVERED ARENA will carry, or a refusal (D4).

    ``isar.session.scan_delivery._measurement_record`` writes
    ``(("temperature_K", T),)`` when the scan has a temperature and ``()`` when it does not, so a
    declaration has to land in the same one of those two lanes. Three card shapes, three answers,
    and the middle one is the whole reason this function exists:

    * NO ``[temperature_rule]`` — the card declares no temperature dimension, the arena will carry
      none, and ``()`` matches it exactly;
    * a rule declaring a literal ``setpoint`` — the value is known, and the card's own
      ``collation.t_round`` binning is what absorbs the difference between a setpoint and the
      regulation drift around it (that is what the bin is FOR);
    * a rule that names only WHERE to read the setpoint (``setpoint_field`` / ``setpoint_param``,
      as the s30 and in8 templates do) — there IS a temperature dimension and its value lives in a
      scan that has not been taken. Declaring ``()`` here would put the declaration in the
      undeclared-couples-only-with-undeclared lane (H13) while every arena lands in the binned
      lane, so the declared partition could never join its own measurement. Refuse.
    """
    rule = dict(card.policy.temperature_rule or {})
    if not rule:
        return ()
    setpoint = rule.get("setpoint")
    if setpoint is None:
        raise WorkspaceUndeclared(
            f"discovery step {step.id!r} cannot mint its cold-start partition: the card declares "
            f"a [temperature_rule] ({sorted(rule)}) but no literal `setpoint`, so it says a "
            "temperature dimension exists without saying what value the first scan will land on. "
            "Declare `setpoint` under [temperature_rule] in card.policy.toml, or remove the rule "
            "if these measurements carry no temperature.")
    return (("temperature_K", float(setpoint)),)


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


def _declared_hypotheses(cfg) -> dict:
    """The card's declared ``[hypotheses.*]`` mapping (name -> resolved declaration dict) — the
    SAME source :func:`_declared_hypotheses_fingerprint` reads, threaded once (SCENE-5 5.2) so
    the scorer and the name-only fingerprint above can never disagree about what "the declared
    physics" means. Empty when the card declares no policy file, same posture as the fingerprint
    function's ``None``."""
    policy_path = Path(cfg.workspace) / "card.policy.toml"
    if not policy_path.exists():
        return {}
    from isar.schema.card import load_policy
    return dict(load_policy(policy_path).hypotheses or {})


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
    """The winning evaluation's intent AND its question associations, read FROM THE PERSISTED
    INVENTORY. Returns ``(intent, associations)``.

    The associations are what C7 attributes a geometry decline to: they name which agenda questions
    the declined action served and which generator supplied each, which is exactly the
    ``(question_id, supporting_generator_ids)`` a :class:`QuestionBlockerEvidence` needs. Read from
    the same artifact and in the same pass as the intent — the decision's persisted
    ``selection.associations`` carry the identical tuple (``select()`` returns the survivor the
    inventory stored), so there is nothing to be gained by parsing ``decisions.jsonl`` for a second
    copy of a fact this file already holds.

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
    associations = tuple(QuestionActionAssociation.from_dict(a)
                         for a in (rows[0].get("associations") or ()))
    return intent, associations


def _is_geometry_decline(verdict: str) -> bool:
    """Whether a decline verdict is about GEOMETRY — the only class that earns durable evidence.

    Asked of :func:`~isar.drive.loop.classify_rejection`, the ONE canonical map from a refusal
    verdict to its durable consequence, rather than by matching substrings of the TAVI *blocker*
    vocabulary. That earlier match read ``infeasible_points_at_submit`` as geometry because the
    word "infeasible" is in it, which is precisely the confusion ``_REJECTION_RULES`` exists to
    settle — the blocker markers class a BLOCKER STRING for the window-trim path, and the rejection
    rules class a VERDICT for its durable consequence. They are different questions.

    ``_execute_proposal``'s whole decline vocabulary, and the lane each one takes here:

    * ``geometry_unreachable`` — scope ``geometry``. The instrument cannot reach that q at all.
      DURABLE lane: blocker evidence, declined fingerprint, re-plan against the same pin.
    * ``all_points_infeasible`` — scope ``geometry``. Same, refused at submit rather than at
      validation. DURABLE lane.
    * ``infeasible_points_at_submit`` — scope ``request``. A bad GRID at a reachable q; loop.py's
      own comment at the raise site says "the q is reachable — only this exact grid is not — so the
      consequence is scoped to the request ... only this request is suppressed". Minting a durable
      blocker from it would close a question on geometry the instrument never refused, so it takes
      the non-durable lane: no evidence, no declined fingerprint, nothing re-marked STRUCTURAL.
    * ``validation_rejected``, ``insufficient_target_support``,
      ``instrument_point_limit_exceeded`` — scope ``request``. Non-durable lane, same reasoning.
    * ``scan_not_done:<state>`` and anything else unmapped — TRANSIENT, scope ``none``. Non-durable
      lane; ``classify_rejection`` fails open to transient by design.

    Only scope ``geometry`` is durable, so everything else fails CLOSED to "not geometry" — the
    safe direction (binding ruling: transient failures never become durable question blockers).
    """
    from isar.drive.loop import classify_rejection

    return classify_rejection(verdict).scope == "geometry"


def _decline_evidence(plan: PlanResult, associations, verdict: str
                      ) -> tuple[QuestionBlockerEvidence, ...]:
    """One :class:`QuestionBlockerEvidence` per question the declined action served (D5).

    The digest triple is copied from the DECLINING decision's own ``PlanResult`` — the basis agenda
    digest, the scene digest it planned against, and the discovery registry fingerprint. Because the
    re-plan runs against the SAME pinned scene and derivation is deterministic, it reproduces that
    exact triple and ``attach_blocker_evidence`` authenticates the evidence rather than rejecting it
    as stale. Its lifetime is exactly one scene revision: the next DELIVERY moves the scene and the
    caller drops it, because evidence stamped against a revision that no longer exists can only be
    rejected by name. A fresh decline re-emits it against the new revision.
    """
    by_question: dict[str, set[str]] = {}
    for association in associations:
        by_question.setdefault(association.question_id, set()).add(association.generator)
    return tuple(
        QuestionBlockerEvidence(
            basis_agenda_digest=plan.basis_digest, question_id=question_id,
            scene_digest=plan.scene_digest,
            generator_registry_fingerprint=DISCOVERY_GENERATOR_REGISTRY_VERSION,
            blocker_name=GEOMETRY_BLOCKER_NAME,
            supporting_generator_ids=tuple(sorted(generators)),
            reason_codes=(verdict,),
            detail=f"instrument declined {plan.selection_fingerprint} as infeasible: {verdict}")
        for question_id, generators in sorted(by_question.items()))


def _campaign_provenance_block(program: CampaignProgram, step, declared) -> dict:
    """The four C1 fields a discovery request carries to the measurement-arena record (D2/D3).

    ``path_fingerprint`` comes from the declared partition, which computed it from the STEP's own
    endpoints through ``path_geometry_fingerprint`` — one derivation, so the partition a scan is
    declared into and the partition its record lands in cannot disagree.
    """
    return {"topology_role": CANONICAL_PATH,
            "program_fingerprint": program_fingerprint(program),
            "program_step": step.id,
            "path_fingerprint": declared.path_fingerprint}


def _run_discovery_step(cfg, program: CampaignProgram, step, *, objective: ObjectiveSpec,
                        budget_state: dict, max_deliveries: int,
                        resumed: dict | None = None) -> tuple[StepResult, str]:
    """One ``discover_and_map`` step, delivery by delivery. Returns ``(result, verdict)``."""
    from isar.drive.loop import (
        _execute_proposal, _operator_stop_requested, _record_action_rejection)

    report_dir = Path(cfg.workspace) / "report"
    _require_synchronous_staleness(step)
    retry_limit = _broken_scan_retry_limit(step)
    declared = _declared_partition(cfg, step)
    hypotheses_fingerprint = _declared_hypotheses_fingerprint(cfg)
    hypotheses = _declared_hypotheses(cfg)
    provenance = _campaign_provenance_block(program, step, declared)

    # Consultation channel state (C8). The overrides are read off the FROZEN PROGRAM, so a step
    # resumed after an answer starts with the policy that answer compiled — the program is the
    # record, and there is no separate prompt state to reload.
    overrides = consultation_overrides(step)
    keep_open: set[str] = set(overrides["keep_open_questions"])
    retry_budget: dict[str, int] = dict(overrides["retry_budget_by_action"])
    # Settled answers whose recompile has not landed yet (a racing program writer took the pointer
    # between this step's plan and its write). Re-attempted on the next delivery.
    pending_questions: set[str] = set()
    pending_budgets: dict[str, int] = {}
    pending_retired: set[str] = set()
    channel = ConsultationChannel(
        cfg.workspace, cfg.log_path, step_id=step.id,
        timeout_deliveries=_prompt_timeout_deliveries(step),
        policy_defaults=dict(dict(step.payload.resolved_policy).get("prompt_defaults") or {}))

    result = StepResult(step_id=step.id, kind=step.kind,
                        declared_partition_id=declared.partition_id,
                        program_fingerprints=(program_fingerprint(program),))
    resumed = resumed or {"scan_counter": 0, "proposal_counter": 0, "observations": [],
                          "consumed": set(), "scan_kind": {}}
    observations: list[dict] = resumed["observations"]
    consumed: set = resumed["consumed"]
    scan_kind: dict = resumed["scan_kind"]
    scan_counter = resumed["scan_counter"]
    # Monotonic across the whole step, exactly as ``run_campaign`` counts its proposals: a decline
    # or a transport failure is not a delivery, so keying the acquisition id on ``deliveries`` gave
    # the refused proposal and its successor the same id, and every consumer that joins on it
    # (pooled-master evidence, the scan-design provenance join) would have read them as one
    # acquisition. Never reset — the id has to be unique over the step, not per delivery.
    proposal_counter = resumed.get("proposal_counter", 0)
    # Broken-scan lane state (C6). Both are DELIVERY-INDEXED — they advance only when a delivery
    # lands, never on a clock — so a replay of the same deliveries reproduces the same sequence.
    # Both are keyed by PHYSICAL action key, never by scientific action fingerprint: the scene moves
    # on every delivery (an untrusted arena still enters as a synthetic observation), the scientific
    # fingerprint hashes the scene digest, and so a counter keyed on it would mint a fresh key each
    # retry and the bound below would never bind. See
    # :func:`isar.schema.action_identity.physical_action_key`.
    # Both REHYDRATED, so a restart neither re-buys a spent retry budget nor un-retires an action
    # the operator retired (H21(b)). The counter comes off the ``broken_scan`` events the lane
    # already writes — derived state, never a second store — and the retirement off the frozen
    # program, the same route ``retry_budget_by_action`` already took.
    broken_deliveries: dict[str, int] = _resumed_broken_deliveries(cfg.log_path, step.id)
    retired: set[str] = set(overrides["retired_actions"])
    transport_failures = 0                      # consecutive, reset by any delivery
    # Durable-blocker lane state (C7). Keyed by SCIENTIFIC action fingerprint, NOT the physical key
    # C6's retirement uses: a decline re-plans against the SAME pinned scene, so the scientific
    # fingerprint (which hashes the scene digest) is stable across the cycle and IS the identity the
    # evaluations carry. The two suppression channels are deliberately distinct and both visible in
    # the decision basis — retirement narrows selection only, a decline structurally rejects.
    # ``{scientific_action_fingerprint: evidence emitted for it}`` rather than a set beside a tuple:
    # a ``keep_spending`` answer suppresses the EVIDENCE for one question without un-rejecting the
    # action, which needs the two to stay joined.
    declined_actions: dict[str, tuple[QuestionBlockerEvidence, ...]] = {}
    decline_replans = 0                         # per delivery; reset by any delivery

    def ended(verdict: str, *, reason: str | None = None, agenda=None) -> tuple[StepResult, str]:
        log_event(cfg.log_path, "decide", {
            "event": "program_step_end", "action": "program_step_end", "step_id": step.id,
            "verdict": verdict, "reason": reason, "deliveries": result.deliveries})
        finished = replace(result, reason=reason, observations=observations,
                           prompts=channel.issued)
        if agenda is not None:
            # The agenda's own consultation requests come FIRST and the step's offramps after: both
            # are C8's input, and appending rather than replacing is what keeps an offramp recorded
            # on a step that later ends for an unrelated reason.
            finished = replace(finished, blocking_questions=tuple(agenda.blocking),
                               consultation_requests=(tuple(agenda.consultation_requests)
                                                      + result.consultation_requests))
        return finished, verdict

    try:
        trust_seen = _count_trust_by_scan_ref(report_dir)
    except ValueError as error:
        return ended(VERDICT_MACHINE_FAILURE, reason=f"measurements_unreadable: {error}")

    # The step's own opening scene. Built through the same supervised seam as every delivery's, so
    # the first plan is pinned exactly like the rest and this path NEVER reads ``current.json``.
    try:
        build = _build_scene(cfg, iteration=None)
    except SceneBuildFailed as error:
        return ended(VERDICT_MACHINE_FAILURE, reason=f"scene_build_failed: {error}")
    pin = build.digest
    result = replace(result, scene_digests=(pin,), build_seconds=(build.seconds,))

    def apply_resolutions(resolutions) -> str | None:
        """Act on every prompt that stopped being open. Returns a fault reason, or None.

        Only the POLICY-CHANGING options do anything: ``keep_spending`` and ``retry_more`` compile
        a NEW FROZEN PROGRAM REVISION through the ``report/program/`` store under CAS (D6 — no
        in-place mutation anywhere), and every subsequent decision cites its fingerprint because
        ``DecisionBasis`` already embeds the program. The two declared defaults confirm what the
        driver already did, so they recompile nothing.

        The CAS parent is the fingerprint THIS STEP HAS BEEN PLANNING UNDER, never a pointer read
        taken at write time: a fresh read cannot fail, so it would have made the compare-and-swap
        vacuous and hidden exactly the racing program writer it exists to catch. When it does fire,
        the override is NOT applied on this pass — it stays pending and is re-attempted on the next
        delivery under whatever the pointer then names. The delivery loop bounds that; there is no
        retry machinery of its own.
        """
        nonlocal program, step, provenance
        for resolution in resolutions:
            if resolution.option == KEEP_SPENDING:
                pending_questions.add(resolution.prompt.subject)
            elif resolution.option == RETIRE_ACTION:
                # The driver already retired it in memory when the budget ran out; this is what
                # makes that survive a restart (H21(b)).
                pending_retired.add(resolution.prompt.subject)
            elif resolution.option == RETRY_MORE:
                key = resolution.prompt.subject
                # A FRESH budget from where this action already is, so "retry more" always buys at
                # least one more delivery even when the step's frozen limit is 0. Merged upward
                # (the override merge is monotone), so a re-attempt can only ever raise it.
                pending_budgets[key] = max(pending_budgets.get(key, 0),
                                           broken_deliveries.get(key, 0) + max(retry_limit, 1))
                retired.discard(key)
        if not pending_questions and not pending_budgets and not pending_retired:
            return None
        questions, budgets = sorted(pending_questions), dict(pending_budgets)
        retiring = sorted(pending_retired)

        try:
            candidate = apply_consultation_overrides(
                program, step.id, keep_open_questions=questions, retry_budget_by_action=budgets,
                retired_actions=retiring)
            PROGRAM_STORE.write_revision(
                report_dir, program_revision(candidate), source="consultation",
                expected_parent=result.program_fingerprints[-1])
        except ConcurrentRevisionWrite as error:
            log_event(cfg.log_path, "decide", {
                "event": "consultation_recompile_conflict",
                "action": "consultation_recompile_conflict", "step_id": step.id,
                "expected_parent": result.program_fingerprints[-1], "error": str(error),
                "keep_open_questions": questions,
                "retry_budget_by_action": {k: budgets[k] for k in sorted(budgets)},
                "retired_actions": retiring,
                "iter": result.deliveries})
            return None
        except ValueError as error:                          # includes ProgramError
            return f"consultation_recompile_failed: {error}"
        program = candidate
        step = next(s for s in program.steps if s.id == step.id)
        pending_questions.clear()
        pending_budgets.clear()
        pending_retired.clear()
        retired.update(retiring)
        keep_open.update(questions)
        for key, value in budgets.items():          # monotone, exactly as the program merged them
            retry_budget[key] = max(retry_budget.get(key, 0), value)
        provenance = _campaign_provenance_block(program, step, declared)
        # Retroactive: evidence already carried for a now-kept-open question stops being supplied,
        # so the next derivation stops closing that question unresolvable. The DECLINED FINGERPRINT
        # stays — the instrument's refusal is still true, and re-proposing it forever is a loop.
        for fingerprint_, evidence in list(declined_actions.items()):
            declined_actions[fingerprint_] = tuple(
                e for e in evidence if e.question_id not in keep_open)
        fingerprint_now = program_fingerprint(program)
        log_event(cfg.log_path, "decide", {
            "event": "consultation_program_recompiled",
            "action": "consultation_program_recompiled", "step_id": step.id,
            "program_fingerprint": fingerprint_now,
            "parent_program_fingerprint": result.program_fingerprints[-1],
            "keep_open_questions": sorted(keep_open),
            "retry_budget_by_action": {k: retry_budget[k] for k in sorted(retry_budget)},
            "retired_actions": sorted(retired),
            "iter": result.deliveries})
        return None

    def issue_prompts(requests, plan_result) -> None:
        channel.issue(requests, delivery=result.deliveries,
                      program_fingerprint=result.program_fingerprints[-1],
                      agenda_revision=getattr(plan_result, "agenda_digest", None),
                      scene_digest=getattr(plan_result, "scene_digest", None))

    def hard_pause(agenda):
        """The step end an unanswered HARD prompt forces, or None. Hard gate v1 IS the
        operator-requested pause: no second pause mechanism, and the beam is released rather than
        held idle waiting on a human. One reading of ``hard_open()`` per check — reading it twice
        asked the channel a question whose answer could differ between the two calls.

        NO PROMPT KIND IN THIS BUILD SETS ``hard`` (``isar.schema.consultation.PROMPT_KINDS``), so
        this branch is unreachable in production and is exercised only by a test-registered kind —
        which is why ``prompt_kind`` looks the vocabulary up through the module global rather than
        capturing it at import. It is a forward-compat seam WITH a test, not dead code; stating so
        here keeps it from reading as live policy an operator could trigger."""
        hard = channel.hard_open()
        if not hard:
            return None
        return ended(VERDICT_OPERATOR_STOPPED, agenda=agenda,
                     reason="hard_prompt awaiting answer: "
                            + ", ".join(p.prompt_id for p in hard))

    while True:
        if _operator_stop_requested(cfg, result.deliveries):
            log_event(cfg.log_path, "decide",
                      {"event": "operator_stop", "action": "operator_stop",
                       "iter": result.deliveries, "token": cfg.control_token})
            return ended(VERDICT_OPERATOR_STOPPED, reason=f"token {cfg.control_token}")

        # The consultation channel, once per pass and BEFORE the plan: an answer that landed since
        # the last pass must reach the program this decision is taken under, not the one after it.
        fault = apply_resolutions(channel.poll(
            result.deliveries, program_fingerprint=result.program_fingerprints[-1]))
        if fault is not None:
            return ended(VERDICT_MACHINE_FAILURE, reason=fault)
        if program_fingerprint(program) != result.program_fingerprints[-1]:
            result = replace(result,
                             program_fingerprints=result.program_fingerprints
                             + (program_fingerprint(program),))

        try:
            plan = plan_next_action(report_dir, program, step.id,
                                    declared_partition=declared,
                                    hypotheses_fingerprint=hypotheses_fingerprint,
                                    hypotheses=hypotheses,
                                    scene_pin=pin,
                                    retired_physical_actions=frozenset(retired),
                                    blocker_evidence=tuple(e for evidence
                                                           in declined_actions.values()
                                                           for e in evidence),
                                    declined_fingerprints=frozenset(declined_actions))
        except ConcurrentRevisionWrite as error:
            # ``plan_next_action`` lets the agenda/program CAS escape ON PURPOSE — the losing-writer
            # policy belongs to the live caller, and this is the live caller. Uncaught it reached
            # the CLI's blanket handler as a bare exit 2: no ProgramStepOutcome, no
            # ``program_step_end`` record, no beam directive, and no named reason in the log. A
            # recorded machine failure is one of the four stop reasons and a resumable one.
            return ended(VERDICT_MACHINE_FAILURE, reason=f"concurrent_revision_write: {error}")
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
        # The agenda's own consultation requests become prompts here — deduped by stable id, so a
        # request the agenda repeats on every delivery costs exactly one prompt (D6).
        issue_prompts(agenda.consultation_requests, plan)
        # COMPLETION FIRST, then the hard gate: a complete step is complete, and a hard prompt gates
        # CONTINUATION, not completion. Gating completion would have reported a step that answered
        # every one of its questions as an operator pause.
        if agenda.state == "complete":
            return ended(VERDICT_COMPLETE, agenda=agenda)
        paused = hard_pause(agenda)
        if paused is not None:
            return paused
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
            intent, selected_associations = _selected_intent(report_dir, plan)
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
                reason=f"discovery: {step.id}", ncount=_discovery_monitor_ncount(step))
        except DiscoveryWindowUnrealizable as error:
            return ended(VERDICT_MACHINE_FAILURE, reason=f"unrealizable_request: {error}",
                         agenda=agenda)
        proposal.acquisition_id = f"{cfg.run_nonce}:{proposal_counter:08d}"
        proposal_counter += 1
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
            # budget — and NOT geometry either: a point-count limit is a property of the REQUEST
            # (a differently-gridded window at the same q is submittable), so it earns no durable
            # blocker. Ending the step in progress is the honest answer; the geometry lane below
            # is the one that re-plans.
            return ended(VERDICT_IN_PROGRESS, reason=f"instrument_refused: {error}", agenda=agenda)
        except TaviError as error:
            # Class 2 (C6): transport, not geometry and not physics. The client has already spent
            # its own Retry-After ladder inside this attempt, so this counts whole failed attempts
            # and adds no second retry machinery. Recorded through the EXISTING refusal vocabulary,
            # where an unmapped verdict classes TRANSIENT/scope "none" — it suppresses nothing,
            # narrows no action, and produces no durable blocker evidence (binding ruling: transient
            # failures never become durable question blockers). Nothing is quarantined either: no
            # counts arrived to distrust.
            transport_failures += 1
            # ``plan=None`` rests on an invariant of the recorder, not on luck: an UNMAPPED verdict
            # classes scope ``"none"``, which returns before ``plan.next_proposal()`` is reached, so
            # there is no plan object to hand it. Mapping ``instrument_transport_failed`` to a
            # durable scope later must revisit THIS call site — it would dereference the None.
            _record_action_rejection(cfg, None, proposal, "instrument_transport_failed",
                                     {}, {}, {}, it=result.deliveries)
            if transport_failures >= MAX_CONSECUTIVE_TRANSPORT_FAILURES:
                # Class 3: an instrument that will not talk at all, for a bounded number of whole
                # attempts running, is a machine failure — which IS the stop-law pause.
                return ended(VERDICT_MACHINE_FAILURE,
                             reason=f"instrument_unreachable after {transport_failures} "
                                    f"consecutive transport failures: {error}", agenda=agenda)
            _LOG.warning("transient instrument transport failure %d/%d: %s", transport_failures,
                         MAX_CONSECUTIVE_TRANSPORT_FAILURES, error)
            continue
        submitted = (proposal.meta or {}).get("proposal_fingerprint")
        if declined is not None:
            verdict = str(declined)
            result = replace(result, declines=result.declines + (verdict,))
            if not _is_geometry_decline(verdict):
                # Not geometry — no durable blocker may be minted from it (C6's lane, unchanged).
                # The scene has not moved, so a re-plan would re-select the identical action
                # forever: end the step and say why.
                return ended(VERDICT_IN_PROGRESS, reason=f"request_declined: {verdict}",
                             agenda=agenda)
            # The geometry lane (C7/D5). The instrument says it LITERALLY cannot perform this
            # measurement, which is a durable fact about the machine and this evidence basis — so
            # the action is structurally rejected on the re-plan and every question it served gets
            # authenticated evidence backing that rejection. The re-plan runs against the SAME pin:
            # the derivation is deterministic, so it reproduces the digest triple the evidence was
            # stamped with, the evidence attaches, producer rule 6 emits the proof, and
            # apply_exhaustion closes the question unresolvable.
            decline_replans += 1
            if decline_replans > MAX_DECLINE_REPLANS_PER_DELIVERY:
                # A defense, never the mechanism: every cycle rejects at least one previously
                # eligible action, so reaching this means the narrowing is not happening.
                return ended(VERDICT_MACHINE_FAILURE,
                             reason=f"decline_replan_cap: {decline_replans - 1} geometry declines "
                                    f"on one delivery exceeded MAX_DECLINE_REPLANS_PER_DELIVERY "
                                    f"({MAX_DECLINE_REPLANS_PER_DELIVERY}) without the action set "
                                    "narrowing; controller fault",
                             agenda=agenda)
            # Audit parity with the transport arm above: every refusal this step meets appears in
            # the session log's ``action_suppressed`` stream, so a resume and a human reading the
            # log see the same refusal history whatever lane it took. ``plan=None`` for the same
            # reason as the transport arm — there is no planner-side plan object here.
            _record_action_rejection(cfg, None, proposal, verdict, {}, {}, {},
                                     it=result.deliveries)
            # ``keep_open`` is an operator's standing answer that this question may NOT close on a
            # blocker, so no evidence is carried for it — the action stays rejected all the same.
            emitted = tuple(e for e in _decline_evidence(plan, selected_associations, verdict)
                            if e.question_id not in keep_open)
            declined_actions[intent.scientific_action_fingerprint] = emitted
            result = replace(result, decline_replans=result.decline_replans + 1)
            log_event(cfg.log_path, "decide", {
                "event": "durable_blocker_emitted", "action": "durable_blocker_emitted",
                "step_id": step.id, "verdict": verdict,
                "scientific_action_fingerprint": intent.scientific_action_fingerprint,
                "blocker_name": GEOMETRY_BLOCKER_NAME,
                "evidence": [e.to_dict() for e in emitted],
                "decline_replans": result.decline_replans, "iter": result.deliveries})
            continue
        result = replace(
            result, deliveries=result.deliveries + 1,
            submitted_fingerprints=result.submitted_fingerprints + ((submitted,) if submitted
                                                                     else ()))
        transport_failures = 0
        # All three are PER SCENE REVISION, and a delivery moves the scene — so they are cleared
        # here rather than carried. Carrying them was harmless to the OUTCOME (the scientific
        # fingerprint hashes the scene digest, so it stops matching, and the evidence's digest
        # triple goes stale and is refused by name) but not to the LOG: every later decision paid
        # to re-derive evidence that could only ever be rejected, and the agenda picked up a
        # ``blocker_evidence_rejected`` note per expired item that says nothing about this
        # revision. A fresh decline re-emits both against the new revision.
        declined_actions.clear()
        decline_replans = 0
        _LOG.debug("discovery delivery %d folded %d row(s)", result.deliveries, len(rows))

        # ---- class 1: did this delivery's counts survive the gates? ------------------------
        try:
            trust_now = _count_trust_by_scan_ref(report_dir)
        except ValueError as error:
            return ended(VERDICT_MACHINE_FAILURE, reason=f"measurements_unreadable: {error}",
                         agenda=agenda)
        delivered_trust = {ref: state for ref, state in trust_now.items() if ref not in trust_seen}
        trust_seen = trust_now
        # An arena the delivery pipeline recorded but could not trust. The RETRY is already
        # happening by construction — an untrusted arena earns no footprint and no coverage credit
        # (isar.scene.build), so the question stays open and the next plan re-selects this same
        # action — and the record stays exactly where it is: quarantine here means the 4.0 record
        # state, never a deletion, a mask, or a rewrite. All this adds is the bound.
        #
        # A delivery that recorded NO arena at all counts as broken too, and the empty-set case is
        # why this reads ``"trusted" not in ...`` rather than guarding on a non-empty map. Nothing
        # measurable came back, so nothing entered the scene, so the scene digest does not move,
        # so the next plan mints the identical scientific fingerprint and re-selects the identical
        # action — with no counter advancing, that repeats until ``max_deliveries``, at full
        # exposure every pass. Bad data and no data are the same lane here: the action is retried
        # under the same bound and offramps the same way.
        if "trusted" not in delivered_trust.values():
            fingerprint = intent.scientific_action_fingerprint
            key = physical_action_key(intent)
            count = broken_deliveries[key] = broken_deliveries.get(key, 0) + 1
            # A ``retry_more`` answer froze a per-action ceiling into the program; absent one, the
            # step's own frozen budget governs. Never a read-time default either way.
            limit = int(retry_budget.get(key, retry_limit))
            log_event(cfg.log_path, "decide", {
                "event": "broken_scan", "action": "broken_scan", "step_id": step.id,
                "scientific_action_fingerprint": fingerprint, "physical_action_key": key,
                "broken_deliveries": count,
                # Zero arenas and untrusted arenas both reach here; the count is what separates
                # them in the log, where a reader otherwise sees an empty ``scan_refs`` twice.
                "delivered_arenas": len(delivered_trust),
                "retry_limit": limit, "scan_refs": sorted(delivered_trust),
                "count_trust": sorted(set(delivered_trust.values())), "iter": result.deliveries})
            if count > limit:
                # The offramp. The action leaves this step's re-submission set (never the
                # inventory, and never the record), a human is asked, and the step KEEPS GOING —
                # bad data is not one of the four stop reasons, and the planner still has a region
                # to survey.
                retired.add(key)
                request = {"kind": OFFRAMP_KIND, "step_id": step.id,
                           "scientific_action_fingerprint": fingerprint,
                           "physical_action_key": key,
                           "retry_count": count - 1, "broken_deliveries": count,
                           "retry_limit": limit,
                           "region": {"q_start": list(step.payload.q_start),
                                      "q_end": list(step.payload.q_end),
                                      "hkl": list(intent.hkl)},
                           "scan_refs": sorted(delivered_trust)}
                result = replace(result,
                                 consultation_requests=result.consultation_requests + (request,))
                log_event(cfg.log_path, "decide", {"event": OFFRAMP_KIND, "action": OFFRAMP_KIND,
                                                   **request, "iter": result.deliveries})
                issue_prompts((request,), plan)
                paused = hard_pause(agenda)
                if paused is not None:
                    return paused

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


def _adopt_stored_revision(report_dir: Path, program: CampaignProgram,
                           log_path) -> CampaignProgram:
    """The AUTHORED program, or the stored revision that IS it plus the operator's answers (C8).

    The failure this closes: a consultation recompile publishes a new frozen revision, the run
    ends, and the next run is started from the same authored program — which carries none of the
    answers. The recompiled revision would then never be read back, "the program is the record"
    would be false, and a settled answer would be silently discarded.

    One deterministic comparison, no clock and no lineage walk: strip the stored revision's
    ``consultation_overrides`` and fingerprint what is left. Equal to the authored program's
    fingerprint means the stored revision descends from exactly this program and is ADOPTED.
    Unequal means the operator changed the goal — the authored program wins, and its own write
    starts a fresh lineage. No store, an unreadable one, or a stored revision carrying no overrides
    all leave the authored program in force.
    """
    authored = program_fingerprint(program)
    try:
        current = PROGRAM_STORE.read_current(report_dir)
    except ValueError as error:
        log_event(log_path, "decide",
                  {"event": "consultation_overrides_store_unreadable",
                   "action": "consultation_overrides_store_unreadable",
                   "program_fingerprint": authored, "error": str(error)})
        return program
    if current is None:
        return program
    try:
        stored = CampaignProgram.from_dict(current[1])
    except (ProgramError, ValueError) as error:
        log_event(log_path, "decide",
                  {"event": "consultation_overrides_store_unreadable",
                   "action": "consultation_overrides_store_unreadable",
                   "program_fingerprint": authored, "error": str(error)})
        return program

    stored_fingerprint = program_fingerprint(stored)
    if stored_fingerprint == authored:
        return program                                  # the same program; nothing to adopt
    if program_fingerprint(strip_consultation_overrides(stored)) == authored:
        log_event(log_path, "decide",
                  {"event": "consultation_overrides_adopted",
                   "action": "consultation_overrides_adopted",
                   "program_fingerprint": stored_fingerprint,
                   "authored_program_fingerprint": authored})
        return stored
    log_event(log_path, "decide",
              {"event": "consultation_overrides_not_adopted",
               "action": "consultation_overrides_not_adopted",
               "program_fingerprint": stored_fingerprint,
               "authored_program_fingerprint": authored,
               "reason": "the stored revision does not strip to the authored program; the goal "
                         "changed, so the authored program starts a fresh lineage"})
    return program


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
    from isar.drive.loop import _campaign_config, _resume_state

    config = dict(config or {})
    step = _resolve_step(program, step_id)
    if step.kind != "discover_and_map":
        raise ProgramError(
            f"run_program executes discover_and_map steps; step {step.id!r} is {step.kind!r} — "
            "a targeted step runs through isar.drive.loop.run_campaign unchanged")

    objective = _carrier_objective(step, config)
    cfg = _campaign_config(objective, config)
    # The compiled revision is READ BACK before anything plans (C8): a stored revision that is this
    # same program plus an operator's settled answers is adopted, so the answers survive the run
    # they were given in. The objective above is unaffected — an override touches only the step's
    # ``consultation_overrides`` policy section.
    program = _adopt_stored_revision(Path(cfg.workspace) / "report", program, cfg.log_path)
    step = _resolve_step(program, step.id)
    # Running the same program again on the same workspace RESUMES it, exactly as the targeted
    # loop does (owner ruling 2026-08-18) — there is no fresh-vs-resume flag on either path, and
    # two step kinds meaning opposite things by one command is what that ruling refuses.
    # ``_resume_state`` is the same reader ``run_campaign`` uses, over the same
    # ``physical_submission`` events this step's own submissions write (it shares
    # ``_execute_proposal``), so the spend ledger, the scan counter and the folded-row set all come
    # back. That is ALSO what makes reusing the persisted ``.run_nonce`` safe: the nonce is stable
    # so a re-delivered scan replays its own ``Idempotency-Key``, while the counter continues past
    # where the last run stopped, so a NEW request can never collide with an old one's key. The two
    # halves only work together — restoring neither (as this step did) let a second invocation
    # re-emit the first's keys and TAVI's idempotency cache serve an earlier scan as a new
    # measurement.
    resumed = _resume_state(Path(cfg.workspace), cfg.log_path, cfg.calibration)
    budget = dict(objective.budget or {})
    budget_state = {"kind": budget.get("kind"), "amount": budget.get("amount"),
                    "neutrons": resumed["neutrons"], "seconds": resumed["seconds"], "spent": 0.0}
    budget_state["spent"] = (budget_state["neutrons"] if budget_state["kind"] == "neutrons"
                             else budget_state["seconds"]
                             if budget_state["kind"] == "counting_seconds" else 0.0)
    declared_ceiling = config.get("max_deliveries")
    max_deliveries = int(MAX_DISCOVERY_DELIVERIES_PER_STEP if declared_ceiling is None
                         else declared_ceiling)

    log_event(cfg.log_path, "decide", {
        "event": "program_step_start", "action": "program_step_start", "step_id": step.id,
        "kind": step.kind, "program_steps": [s.id for s in program.steps],
        "budget": objective.budget, "max_deliveries": max_deliveries,
        "resumed": {"scan_counter": resumed["scan_counter"],
                    "proposal_counter": resumed.get("proposal_counter", 0),
                    "n_observations": len(resumed["observations"]),
                    "spent": budget_state["spent"]}})
    try:
        result, verdict = _run_discovery_step(
            cfg, program, step, objective=objective, budget_state=budget_state,
            max_deliveries=max_deliveries, resumed=resumed)
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

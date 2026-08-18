"""The consultation channel's schema (SCENE-4.3 C8, D6) — prompts, answers, and the one way an
answer changes policy.

Two kinds exist in v1 and no framework exists at all: ``unresolvable_closure`` (a question the
agenda closed unresolvable — the human may overrule the closure) and ``broken_scan_offramp`` (an
action retired after its broken-scan retry budget ran out — the human may grant more). Anything
else is not a prompt yet, deliberately.

**Prompt identity is DELIVERY-STABLE WITHIN AN EPOCH** (D6, the whole point of this module).
:func:`base_identity` fingerprints the SEMANTIC question — its kind, the step it belongs to, the
subject it is about (a question id, or a scene-invariant physical action key), and the options
offered. The agenda revision and the scene digest ride on the prompt as PROVENANCE and are NEVER
identity components: an agenda-bearing id would re-mint the same open prompt on every delivery (the
agenda revision moves each time) and orphan every answer file a human had already written for it.
That is the failure this design exists to prevent, and ``tests/drive/test_consultation.py`` pins it.

The FULL ``prompt_id`` adds one component the base identity has not got: the **epoch**, the number
of times this base identity has ALREADY been settled (0 on first issue), counted off the persisted
settlement records (:mod:`isar.drive.consultation`). Two failures die on that one component:

* an answer file written for a settlement that already happened names an id no live prompt can
  carry, so a historical answer can never silently re-apply to a re-issued question;
* a subject that CLOSES AGAIN after being settled — the question re-closed unresolvable, the action
  re-offramped — is a genuinely new occurrence and mints epoch+1, so it IS re-asked instead of
  being deduped forever against a decision the operator made about an earlier occurrence.

Within one epoch the id is unchanged across every delivery, which is exactly the D6 guarantee; the
epoch comes off durable records rather than a counter in memory, so a replay reads the same
settlements and reproduces the same ids.

**Timeouts are DELIVERY-COUNTED, never wall-clock** (owner ruling 2026-08-18). A prompt that
elapses on a clock would make the same seed replay differently; a prompt that elapses on deliveries
replays exactly.

**A policy-changing answer compiles a NEW FROZEN PROGRAM REVISION**, it never mutates anything in
place. :func:`apply_consultation_overrides` is the one compiler: it returns a new
:class:`~isar.schema.campaign_program.CampaignProgram` whose step carries the override in its
``resolved_policy`` block's ``consultation_overrides`` section, which the driver then publishes
through the ``report/program/`` store under CAS. Subsequent decisions cite the new
``program_fingerprint`` because ``DecisionBasis`` already embeds it.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping

from .action_identity import canonical_json
from .campaign_program import CampaignProgram, CampaignStep, DiscoverAndMapSpec, ProgramError
from .question_policy import CONSULTATION_OVERRIDES_KEY

PROMPT_SCHEMA = "isar.consultation-prompt/1"
ANSWER_SCHEMA = "isar.consultation-answer/1"

#: The two v1 kinds. Same strings the producers already use (``evaluate_agenda``'s consultation
#: request and ``program_runner.OFFRAMP_KIND``), so the producer and this layer cannot drift.
UNRESOLVABLE_CLOSURE = "unresolvable_closure"
BROKEN_SCAN_OFFRAMP = "broken_scan_offramp"

#: Options. ``accept_close`` is spelled the way the frozen question-policy block already spells it
#: (``isar.schema.question_policy.PROMPT_DEFAULTS``) — a second near-identical spelling of one
#: declared default is exactly the drift this repo keeps paying for.
ACCEPT_CLOSE = "accept_close"
KEEP_SPENDING = "keep_spending"
RETIRE_ACTION = "retire_action"
RETRY_MORE = "retry_more"

#: The options that CHANGE POLICY, and therefore compile a new program revision. The other two are
#: the declared defaults: they confirm what the driver already did, so nothing is recompiled.
POLICY_CHANGING_OPTIONS = frozenset({KEEP_SPENDING, RETRY_MORE})


@dataclass(frozen=True, slots=True)
class PromptKind:
    """One prompt kind's contract.

    ``subject_field`` names the field of the originating consultation request that carries the
    prompt's SEMANTIC subject — the identity component that must stay stable while the agenda moves
    underneath it. ``hard`` is forward-compat only: v1 has no hard-gate mechanism, and a hard prompt
    maps to the operator-requested pause the stop token already implements (the driver ends the step
    ``operator_stopped`` rather than blocking the beam on a human).
    """

    kind: str
    options: tuple[str, ...]
    fallback_default: str
    subject_field: str
    policy_default_key: str | None = None
    hard: bool = False


PROMPT_KINDS: dict[str, PromptKind] = {
    UNRESOLVABLE_CLOSURE: PromptKind(
        kind=UNRESOLVABLE_CLOSURE, options=(ACCEPT_CLOSE, KEEP_SPENDING),
        fallback_default=ACCEPT_CLOSE, subject_field="question_id",
        policy_default_key=UNRESOLVABLE_CLOSURE),
    BROKEN_SCAN_OFFRAMP: PromptKind(
        kind=BROKEN_SCAN_OFFRAMP, options=(RETIRE_ACTION, RETRY_MORE),
        fallback_default=RETIRE_ACTION, subject_field="physical_action_key"),
}


def prompt_kind(name: Any) -> PromptKind | None:
    """The kind's contract, or ``None`` for a kind this build does not prompt on.

    Looked up through the module global (never captured at import) so a test can register a kind —
    the hard-gate case has no v1 producer and would otherwise be untestable.
    """
    return PROMPT_KINDS.get(name) if isinstance(name, str) else None


def base_identity(*, kind: str, step_id: str, subject: str, options) -> str:
    """The SEMANTIC question's fingerprint: sha256 over kind, step, subject, options.

    Four components and no more. Adding the agenda revision or the scene digest here — both of
    which move every delivery — would re-mint the prompt on every pass and orphan the answer file
    the human already wrote. They travel as provenance instead. This is the key the settlement
    records are counted under, so the same question keeps ONE lineage across every re-issue.
    """
    return hashlib.sha256(canonical_json(
        {"kind": kind, "step_id": step_id, "subject": subject,
         "options": list(options)}).encode("utf-8")).hexdigest()[:16]


def prompt_identity(*, kind: str, step_id: str, subject: str, options, epoch: int) -> str:
    """The FULL prompt id: the base identity plus the occurrence's ``epoch``.

    ``epoch`` is the count of settlements this base identity already has (0 on first issue), so the
    id is delivery-stable while the prompt is open and CHANGES the moment the question re-opens
    after a settlement. ``epoch`` is required, never defaulted: a caller that does not know which
    occurrence it is minting cannot be minting the right id.
    """
    return hashlib.sha256(canonical_json(
        {"kind": kind, "step_id": step_id, "subject": subject, "options": list(options),
         "epoch": int(epoch)}).encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class ConsultationPrompt:
    """One open question put to the operator. Immutable; its lifecycle lives in delivery-indexed
    events, never in this record."""

    prompt_id: str
    #: The occurrence-free identity this prompt belongs to, and which occurrence it is.
    base_id: str
    epoch: int
    kind: str
    step_id: str
    subject: str
    options: tuple[str, ...]
    default_option: str
    timeout_deliveries: int
    hard: bool
    expected_program_revision: str
    issued_delivery: int
    #: Provenance, NEVER identity (D6).
    agenda_revision: str | None = None
    scene_digest: str | None = None
    question_id: str | None = None
    detail: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.default_option not in self.options:
            raise ProgramError(
                f"prompt {self.prompt_id} declares default {self.default_option!r}, which is not "
                f"one of its options {list(self.options)}; a default nobody can choose is not a "
                "default")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": PROMPT_SCHEMA, "prompt_id": self.prompt_id, "base_id": self.base_id,
            "epoch": self.epoch, "kind": self.kind,
            "step_id": self.step_id, "subject": self.subject, "options": list(self.options),
            "default": self.default_option, "timeout_deliveries": self.timeout_deliveries,
            "hard": self.hard, "expected_program_revision": self.expected_program_revision,
            "issued_delivery": self.issued_delivery,
            "provenance": {"agenda_revision": self.agenda_revision,
                           "scene_digest": self.scene_digest},
            "question_id": self.question_id,
            "request": dict(self.detail or {}),
        }


# --------------------------------------------------------------------------- overrides
def consultation_overrides(step: CampaignStep) -> dict[str, Any]:
    """The step's frozen override section, or an empty one. Never a read-time default: absent
    genuinely means "no consultation has changed this program's policy"."""
    policy = dict(getattr(step.payload, "resolved_policy", None) or {})
    overrides = dict(policy.get(CONSULTATION_OVERRIDES_KEY) or {})
    return {"keep_open_questions": list(overrides.get("keep_open_questions") or ()),
            "retry_budget_by_action": dict(overrides.get("retry_budget_by_action") or {})}


def apply_consultation_overrides(program: CampaignProgram, step_id: str, *,
                                 keep_open_questions=(),
                                 retry_budget_by_action: Mapping[str, int] | None = None
                                 ) -> CampaignProgram:
    """Compile a NEW program whose ``step_id`` carries these overrides. Nothing is mutated.

    ``keep_open_questions`` is the ``keep_spending`` answer's effect: the driver stops supplying
    durable blocker evidence for that question, so the derivation stops closing it unresolvable and
    the campaign keeps looking for another way to answer it. It deliberately does NOT un-reject the
    declined action — the instrument's "I cannot reach that" is still true, and re-submitting it
    forever is a loop, not a measurement.

    ``retry_budget_by_action`` is the ``retry_more`` answer's effect: a per-physical-action ceiling
    that replaces the step's frozen ``broken_scan_retry_limit`` for that ONE action.

    Both merge into whatever the program already carries (a second answer adds, never replaces),
    and both are emitted sorted so the same answers compile to the same fingerprint. The budget
    merge is MONOTONE — ``max(current, new)``, never last-writer-wins — because every path that
    computes a budget computes it from where the action already is, so a re-answer arriving on a
    later delivery must only ever RAISE the ceiling. Last-writer-wins let a stale recomputation
    silently take budget back off an action the operator had just granted more.
    """
    step = next((s for s in program.steps if s.id == step_id), None)
    if step is None:
        raise ProgramError(f"program has no step {step_id!r}")
    if not isinstance(step.payload, DiscoverAndMapSpec):
        raise ProgramError(
            f"step {step_id!r} is a {step.kind!r} step; consultation overrides are a "
            "discover_and_map policy")

    current = consultation_overrides(step)
    questions = sorted(set(current["keep_open_questions"]) | set(keep_open_questions))
    budgets = {key: int(value) for key, value in current["retry_budget_by_action"].items()}
    for key, value in dict(retry_budget_by_action or {}).items():
        budgets[key] = max(budgets.get(key, 0), int(value))
    section = {"keep_open_questions": questions,
               "retry_budget_by_action": {key: budgets[key] for key in sorted(budgets)}}

    payload = step.payload.to_dict()
    payload["resolved_policy"] = {**payload["resolved_policy"],
                                  CONSULTATION_OVERRIDES_KEY: section}
    replaced = CampaignStep(id=step.id, kind=step.kind,
                            payload=DiscoverAndMapSpec.from_dict(payload, where=step.id))
    return CampaignProgram(
        steps=tuple(replaced if s.id == step_id else s for s in program.steps),
        source_text=program.source_text, source=program.source)


def strip_consultation_overrides(program: CampaignProgram) -> CampaignProgram:
    """``program`` with every step's override section removed — the AUTHORED program it descends
    from.

    The one test for "is this stored revision the operator's answers applied to the program I was
    asked to run?": strip, then compare ``program_fingerprint``. Equal means the stored revision IS
    this program plus answers and may be adopted; unequal means the authored program genuinely
    changed and the stored revision belongs to a different goal. Returns ``program`` itself when
    nothing was carried, so the common case allocates nothing.
    """
    payload = program.to_dict()
    stripped = False
    for step in payload["steps"]:
        policy = step.get("payload", {}).get("resolved_policy")
        if isinstance(policy, dict) and policy.pop(CONSULTATION_OVERRIDES_KEY, None) is not None:
            stripped = True
    return CampaignProgram.from_dict(payload) if stripped else program

"""The consultation channel v1 — prompts out, answers in (SCENE-4.3 C8, D6).

The channel exists so a human can overrule two decisions the driver would otherwise take alone: a
question closed unresolvable, and an action retired after its broken-scan budget ran out. It is
NON-BLOCKING by construction — the beam never idles on a human — so everything below is about what
happens while nobody answers, and about what happens when somebody finally does.

Two levels, deliberately:

* the CHANNEL itself, driven directly, where identity, timeouts, invalid answers and staleness are
  exact and cheap to state;
* the STEP RUNNER, where an answer arriving on a later delivery than the one that issued the prompt
  has to reach the program the next decision is taken under — which is the whole D6 claim.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import isar.drive.program_runner as runner_mod
import isar.schema.consultation as consultation_schema
from isar.drive.consultation import (
    ConsultationChannel, answer_path, settlements_path, write_answer)
from isar.drive.program_runner import VERDICT_OPERATOR_STOPPED, run_program
from isar.drive.steering.planner import PROGRAM_STORE
from isar.schema.campaign_program import CampaignProgram, program_fingerprint
from isar.schema.consultation import (
    ACCEPT_CLOSE, BROKEN_SCAN_OFFRAMP, KEEP_SPENDING, RETIRE_ACTION, RETRY_MORE,
    UNRESOLVABLE_CLOSURE, apply_consultation_overrides, base_identity, prompt_identity)

from drive.test_broken_scan_lane import (
    _two_eligible_actions, config_for, moving_scenes, policy_with, selected_physical_keys,
    trust_runner)
from drive.test_decline_feedback import DeclineFirstTavi, agenda_payload, assessment_for
from drive.test_program_runner import (
    COLD_START_SCENE, DecliningTavi, FakeBuild, decision_lines, discover_step, log_payloads,
    workspace)


# ---------------------------------------------------------------------------- helpers
def channel_for(root: Path, *, timeout: int = 2, defaults=None) -> ConsultationChannel:
    return ConsultationChannel(root, root / "report" / "session_log.jsonl", step_id="discover-1",
                               timeout_deliveries=timeout,
                               policy_defaults=defaults or {UNRESOLVABLE_CLOSURE: ACCEPT_CLOSE})


CLOSURE_REQUEST = {"kind": UNRESOLVABLE_CLOSURE, "question_id": "q-1",
                   "description": "closed unresolvable", "default": ACCEPT_CLOSE}


def prompts_issued(root: Path) -> list[dict]:
    return log_payloads(root, "prompt_issued")


def answer_when(root: Path, inner, *, after_deliveries: int, option: str, index: int = 0):
    """Wrap the analysis stand-in so the operator's answer lands DURING the run.

    The operator does exactly this: reads the prompt id off the feed and writes the answer file.
    ``after_deliveries`` is how many deliveries have folded before the answer appears, so the answer
    always arrives on a LATER delivery than the one that issued the prompt.
    """
    state = {"calls": 0}

    def run_isar(ws, data_paths, **kw):
        inner(ws, data_paths, **kw)
        state["calls"] += 1
        if state["calls"] == after_deliveries:
            issued = prompts_issued(Path(ws))
            if len(issued) > index:
                write_answer(ws, issued[index]["prompt_id"], option, note="operator says so")

    return run_isar


def current_program(root: Path) -> dict:
    _pointer, payload = PROGRAM_STORE.read_current(root / "report")
    return payload


def program_pointer(root: Path) -> dict:
    return json.loads((root / "report" / "program" / "current.json").read_text(encoding="utf-8"))


def assessment_status(root: Path, question_id: str) -> str:
    return assessment_for(agenda_payload(root), question_id)["status"]


def overrides_of(payload: dict, step_id: str = "discover-1") -> dict:
    step = next(s for s in payload["steps"] if s["id"] == step_id)
    return step["payload"]["resolved_policy"].get("consultation_overrides") or {}


# ---------------------------------------------------------------------------- (1) identity
def _identity(subject: str = "q-1", *, epoch: int) -> str:
    return prompt_identity(kind=UNRESOLVABLE_CLOSURE, step_id="discover-1", subject=subject,
                           options=(ACCEPT_CLOSE, KEEP_SPENDING), epoch=epoch)


def test_one_prompt_keeps_its_id_while_the_agenda_revision_moves_under_it(tmp_path):
    """THE D6 acceptance. WITHIN AN EPOCH the id is delivery-stable: the agenda revision and the
    scene digest ride as PROVENANCE, and an id that hashed either would re-mint this prompt on
    every delivery and orphan the answer file a human had already written for the previous one."""
    root = workspace(tmp_path)
    channel = channel_for(root)

    first = channel.issue([CLOSURE_REQUEST], delivery=0, program_fingerprint="pf-0",
                          agenda_revision="agenda-0", scene_digest="scene-0")
    again = channel.issue([CLOSURE_REQUEST], delivery=1, program_fingerprint="pf-0",
                          agenda_revision="agenda-1", scene_digest="scene-1")
    third = channel.issue([CLOSURE_REQUEST], delivery=2, program_fingerprint="pf-0",
                          agenda_revision="agenda-2", scene_digest="scene-2")

    assert len(first) == 1 and again == [] and third == [], "the open prompt was re-issued"
    prompt = first[0]
    assert (prompt.epoch, prompt.prompt_id) == (0, _identity(epoch=0)), \
        "the id is not the semantic question's fingerprint at epoch 0"
    assert prompt.base_id == base_identity(
        kind=UNRESOLVABLE_CLOSURE, step_id="discover-1", subject="q-1",
        options=(ACCEPT_CLOSE, KEEP_SPENDING))
    assert prompt.agenda_revision == "agenda-0" and prompt.scene_digest == "scene-0"
    assert len(prompts_issued(root)) == 1
    # The declared default comes from the FROZEN policy block, not from the request's own wording.
    assert prompt.default_option == ACCEPT_CLOSE


def test_a_settled_subject_re_asks_under_a_new_epoch_and_the_old_answer_cannot_re_apply(tmp_path):
    """The other half of the D6 acceptance (C8 findings 2 and 4).

    Within an epoch a settled prompt is never re-minted — the request the agenda repeats every
    delivery still costs ONE prompt. But a subject that closes AGAIN after being settled is a new
    occurrence, and it mints epoch+1 rather than being deduped forever against a decision the
    operator made about the previous one. Because the id carries the epoch, the answer file written
    for the settled occurrence names an id no live prompt can carry: it can never silently
    re-apply, and it is reported unmatched rather than sitting unread.
    """
    root = workspace(tmp_path)
    channel = channel_for(root, timeout=0)

    first = channel.issue([CLOSURE_REQUEST], delivery=0, program_fingerprint="pf")[0]
    assert first.prompt_id == _identity(epoch=0)
    assert [r.source for r in channel.poll(0)] == ["default"]
    # The operator answers the occurrence that has already settled.
    write_answer(root, first.prompt_id, KEEP_SPENDING)

    second = channel.issue([CLOSURE_REQUEST], delivery=1, program_fingerprint="pf")
    assert [p.prompt_id for p in second] == [_identity(epoch=1)], "the re-closure was not re-asked"
    assert second[0].epoch == 1 and second[0].base_id == first.base_id
    assert channel.issue([CLOSURE_REQUEST], delivery=1, program_fingerprint="pf") == [], \
        "an OPEN prompt was re-minted within its epoch"
    # The historical answer names the epoch-0 id, so the epoch-1 prompt takes its DECLARED DEFAULT
    # instead — the operator's earlier keep_spending never re-applies.
    assert [(r.option, r.source) for r in channel.poll(1)] == [(ACCEPT_CLOSE, "default")], \
        "a historical answer re-applied to a re-issued prompt"

    # ...and it is recorded honestly. It was settled at its own epoch, so what the driver saw is a
    # LATE answer to that settlement (stale), not an unrecognised file.
    stale = log_payloads(root, "stale_answer")
    assert [e["prompt_id"] for e in stale] == [first.prompt_id]
    assert [e["prompt_id"] for e in log_payloads(root, "prompt_default_applied")] == [
        first.prompt_id, second[0].prompt_id]

    # The settlement is DURABLE and clock-free, and a fresh channel reads the same epochs back.
    records = [json.loads(line) for line in
               settlements_path(root).read_text(encoding="utf-8").splitlines()]
    assert [(r["base_id"], r["epoch"], r["resolution"], r["source"]) for r in records] == [
        (first.base_id, 0, ACCEPT_CLOSE, "default"), (first.base_id, 1, ACCEPT_CLOSE, "default")]
    assert not [key for r in records for key in r if key in ("written_at", "ts", "timestamp")]
    resumed = channel_for(root, timeout=0)
    assert [p.prompt_id for p in resumed.issue([CLOSURE_REQUEST], delivery=0,
                                               program_fingerprint="pf")] == [_identity(epoch=2)]


def test_an_answer_file_matching_no_prompt_at_all_is_reported_once(tmp_path):
    """A fat-fingered id must not vanish into the control directory. Once per distinct file
    content, because the directory is re-scanned every delivery."""
    root = workspace(tmp_path)
    channel = channel_for(root, timeout=99)
    channel.issue([CLOSURE_REQUEST], delivery=0, program_fingerprint="pf")

    write_answer(root, "deadbeefdeadbeef", KEEP_SPENDING)
    assert channel.poll(1) == [] and channel.poll(2) == []
    unmatched = log_payloads(root, "prompt_answer_unmatched")
    assert [e["prompt_id"] for e in unmatched] == ["deadbeefdeadbeef"]


# ---------------------------------------------------------------------------- (2) resolution
def test_silence_for_the_declared_timeout_applies_the_default_and_a_late_answer_is_stale(tmp_path):
    """Delivery-counted, never wall-clock: the prompt ages only as deliveries fold. After the
    default is applied an answer is RECORDED and NOT applied — an explicit re-apply is out of v1
    scope, and silently acting on a race the operator cannot see is worse than saying so."""
    root = workspace(tmp_path)
    channel = channel_for(root, timeout=2)
    prompt = channel.issue([CLOSURE_REQUEST], delivery=0, program_fingerprint="pf")[0]

    assert channel.poll(0) == [] and channel.poll(1) == [], "the prompt aged early"
    resolved = channel.poll(2)
    assert [(r.option, r.source) for r in resolved] == [(ACCEPT_CLOSE, "default")]

    write_answer(root, prompt.prompt_id, KEEP_SPENDING)
    assert channel.poll(3) == [], "a post-default answer was applied"
    stale = log_payloads(root, "stale_answer")
    assert [(e["prompt_id"], e["option"], e["applied"]) for e in stale] == [
        (prompt.prompt_id, KEEP_SPENDING, ACCEPT_CLOSE)]
    assert channel.poll(4) == [] and len(log_payloads(root, "stale_answer")) == 1


def test_an_answer_naming_a_non_option_is_recorded_and_the_prompt_stays_open(tmp_path):
    """A typo must not crash a campaign and must not count as an answer either."""
    root = workspace(tmp_path)
    channel = channel_for(root, timeout=99)
    prompt = channel.issue([CLOSURE_REQUEST], delivery=0, program_fingerprint="pf")[0]

    write_answer(root, prompt.prompt_id, "spend_more_maybe")
    assert channel.poll(1) == [] and channel.poll(2) == []
    invalid = log_payloads(root, "prompt_answer_invalid")
    assert len(invalid) == 1, "one typo was reported once per delivery"
    assert invalid[0]["option"] == "spend_more_maybe"
    assert channel.issue([CLOSURE_REQUEST], delivery=2, program_fingerprint="pf") == []

    write_answer(root, prompt.prompt_id, KEEP_SPENDING)
    assert [(r.option, r.source) for r in channel.poll(3)] == [(KEEP_SPENDING, "answer")]
    assert [e["option"] for e in log_payloads(root, "prompt_answered")] == [KEEP_SPENDING]


def test_a_corrupt_answer_file_never_reaches_the_campaign_as_an_answer(tmp_path):
    """Half a file, a foreign schema, or an answer addressed to another prompt: all invalid, none
    fatal, and the prompt stays open for a real answer."""
    root = workspace(tmp_path)
    channel = channel_for(root, timeout=99)
    prompt = channel.issue([CLOSURE_REQUEST], delivery=0, program_fingerprint="pf")[0]
    path = answer_path(root, prompt.prompt_id)
    path.parent.mkdir(parents=True, exist_ok=True)

    path.write_text("{not json", encoding="utf-8")
    assert channel.poll(1) == []
    path.write_text(json.dumps({"schema": "something/9", "prompt_id": prompt.prompt_id,
                                "option": KEEP_SPENDING}), encoding="utf-8")
    assert channel.poll(2) == []
    path.write_text(json.dumps({"schema": "isar.consultation-answer/1", "prompt_id": "other",
                                "option": KEEP_SPENDING}), encoding="utf-8")
    assert channel.poll(3) == []
    assert len(log_payloads(root, "prompt_answer_invalid")) == 3


def test_an_unknown_prompt_kind_is_recorded_rather_than_minted(tmp_path):
    """v1 prompts on exactly two kinds. A third is a producer this build does not consult on — say
    so in the feed rather than inventing a prompt with no options."""
    root = workspace(tmp_path)
    channel = channel_for(root)
    assert channel.issue([{"kind": "sample_melted", "question_id": "q"}], delivery=0,
                         program_fingerprint="pf") == []
    assert channel.issue([{"kind": UNRESOLVABLE_CLOSURE}], delivery=0,
                         program_fingerprint="pf") == []
    assert log_payloads(root, "prompt_kind_unknown") and log_payloads(root,
                                                                     "prompt_subject_missing")


# ---------------------------------------------------------------------------- (3) the step runner
def test_retry_more_answered_on_a_later_delivery_grants_budget_and_re_submits_the_action(
        tmp_path, monkeypatch):
    """The offramp overruled. The prompt is issued when action A retires on delivery 1; the answer
    lands on delivery 2; the pass after it un-retires A, grants it a fresh budget through a NEW
    frozen program revision, and A is submitted again."""
    root = workspace(tmp_path)
    step = discover_step(policy=policy_with(retry_limit=0))     # offramp on the first broken scan
    monkeypatch.setattr(runner_mod, "build_scene_revision", FakeBuild(root, moving_scenes()))
    _two_eligible_actions(monkeypatch, coords=(2.0, 2.9))
    runner = answer_when(root, trust_runner(root, trust="excluded"), after_deliveries=2,
                         option=RETRY_MORE)

    outcome = run_program(CampaignProgram(steps=(step,)), step.id,
                          config_for(root, trust="excluded", max_deliveries=4)
                          | {"run_isar": runner})

    issued = prompts_issued(root)
    assert [p["kind"] for p in issued][:1] == [BROKEN_SCAN_OFFRAMP]
    answered = log_payloads(root, "prompt_answered")
    assert [(e["prompt_id"], e["option"]) for e in answered] == [
        (issued[0]["prompt_id"], RETRY_MORE)]
    # The answer arrived strictly LATER than the delivery that issued the prompt.
    assert answered[0]["iter"] > issued[0]["issued_delivery"]

    retried = issued[0]["subject"]
    keys = selected_physical_keys(root)
    assert keys.count(retried) >= 2, "the un-retired action was never re-submitted"
    assert keys.index(retried) < len(keys) - 1

    budgets = overrides_of(current_program(root))["retry_budget_by_action"]
    assert budgets == {retried: 2}, "the fresh budget did not freeze into the program"
    assert outcome.result.prompts and outcome.result.prompts[0]["prompt_id"] == \
        issued[0]["prompt_id"]

    # --- the policy-changing answer compiled a NEW FROZEN REVISION through the store -----------
    recompiled = log_payloads(root, "consultation_program_recompiled")
    assert len(recompiled) == 1
    original, new = (recompiled[0]["parent_program_fingerprint"],
                     recompiled[0]["program_fingerprint"])
    assert original == program_fingerprint(CampaignProgram(steps=(step,))) != new
    # CAS: the pointer names the new revision and cites the one it replaced as its parent, and the
    # replaced blob is still on disk — nothing was mutated and nothing was deleted.
    pointer = program_pointer(root)
    assert (pointer["digest"], pointer["parent"]) == (new, original)
    assert (root / "report" / "program" / f"{original}.json").is_file()
    # Every decision after the answer cites the new fingerprint; the ones before cite the old.
    cited = [line["decision_basis"]["program_fingerprint"] for line in decision_lines(root)]
    assert cited[0] == original and cited[-1] == new
    assert outcome.result.program_fingerprints == (original, new)


def test_a_closed_question_becomes_an_unresolvable_closure_prompt(tmp_path, monkeypatch):
    """The other v1 kind, from the real producer: a question the geometry lane closed unresolvable
    is put to the operator under a stable id, with the frozen block's declared default.

    STRUCTURAL NOTE (why this stops here): a question closes unresolvable only once NOTHING eligible
    serves it, and in the cold-start world that question is the only one with actions — so the step
    has nothing left to measure and ends on the same pass the prompt is issued. There is no later
    delivery for an answer to arrive on in THIS world; what a ``keep_spending`` answer then buys is
    pinned by the test below, and the "answer on a later delivery compiles a new revision" claim is
    pinned on the offramp kind above, which shares the recompile path exactly.
    """
    root = workspace(tmp_path)
    step = discover_step()
    monkeypatch.setattr(runner_mod, "build_scene_revision", FakeBuild(root, [COLD_START_SCENE]))

    run_program(CampaignProgram(steps=(step,)), step.id,
                config_for(root, client=DecliningTavi(), max_deliveries=3))

    closed = log_payloads(root, "durable_blocker_emitted")[0]["evidence"][0]["question_id"]
    issued = prompts_issued(root)
    assert [p["kind"] for p in issued] == [UNRESOLVABLE_CLOSURE]
    assert issued[0]["subject"] == closed and issued[0]["question_id"] == closed
    assert issued[0]["options"] == [ACCEPT_CLOSE, KEEP_SPENDING]
    assert issued[0]["default"] == ACCEPT_CLOSE
    assert issued[0]["prompt_id"] == prompt_identity(
        kind=UNRESOLVABLE_CLOSURE, step_id=step.id, subject=closed,
        options=(ACCEPT_CLOSE, KEEP_SPENDING), epoch=0)


def test_keep_spending_stops_the_question_closing_without_un_rejecting_the_refused_action(
        tmp_path, monkeypatch):
    """What the answer BUYS, through the real derivation: the driver stops supplying durable blocker
    evidence for that question, so it stays open and the campaign keeps looking for another way to
    answer it — while the action the instrument refused stays structurally rejected, because "I
    cannot reach that q" is still true and re-submitting it forever is a loop, not a measurement.

    Staged the way a resumed campaign meets it: the override already frozen in the program (which is
    exactly what the recompile above writes), so this exercises the READ path production uses.
    """
    def run(name: str, keep_open=()):
        root = workspace(tmp_path / name)
        step = discover_step()
        program = CampaignProgram(steps=(step,))
        if keep_open:
            program = apply_consultation_overrides(program, step.id, keep_open_questions=keep_open)
        monkeypatch.setattr(runner_mod, "build_scene_revision", FakeBuild(root, [COLD_START_SCENE]))
        run_program(program, step.id, config_for(root, client=DecliningTavi(), max_deliveries=3))
        return root

    baseline = run("closes")
    question = log_payloads(baseline, "durable_blocker_emitted")[0]["evidence"][0]["question_id"]
    assert assessment_status(baseline, question) == "unresolvable"

    kept = run("kept", keep_open=(question,))
    assert assessment_status(kept, question) == "open", \
        "the operator's keep_spending answer did not keep the question open"
    assert log_payloads(kept, "durable_blocker_emitted")[0]["evidence"] == []
    assert prompts_issued(kept) == [], "a question that never closed still asked to be consulted"
    # ...and the refused action is still out of the running.
    last = decision_lines(kept)[-1]
    assert last["outcome"] == "no_eligible_action"
    assert last["decision_basis"]["declined_scientific_actions"] == [
        decision_lines(kept)[0]["selection"]["fingerprint"]]


def test_a_hard_prompt_stops_the_step_as_an_operator_pause_rather_than_blocking_the_beam(
        tmp_path, monkeypatch):
    """Hard gate v1 is NOT a new mechanism: a hard prompt with no answer maps to the
    operator-requested pause, so the step ends ``operator_stopped`` and the beam is released. No v1
    kind is hard — the schema carries the flag for forward-compat, and this pins what it does."""
    root = workspace(tmp_path)
    step = discover_step(policy=policy_with(retry_limit=0))
    monkeypatch.setattr(runner_mod, "build_scene_revision", FakeBuild(root, moving_scenes()))
    _two_eligible_actions(monkeypatch, coords=(2.0, 2.9))
    monkeypatch.setitem(consultation_schema.PROMPT_KINDS, BROKEN_SCAN_OFFRAMP,
                        replace(consultation_schema.PROMPT_KINDS[BROKEN_SCAN_OFFRAMP], hard=True))

    outcome = run_program(CampaignProgram(steps=(step,)), step.id,
                          config_for(root, trust="excluded", max_deliveries=4))

    assert outcome.verdict == VERDICT_OPERATOR_STOPPED
    assert "hard_prompt" in outcome.result.reason
    assert outcome.result.deliveries == 1, "the step kept measuring past a hard gate"
    assert [p["hard"] for p in prompts_issued(root)] == [True]


def test_the_consultation_channel_is_replay_deterministic(tmp_path, monkeypatch):
    """Same inputs, same prompts and same decisions. Timeouts are delivery-counted and nothing in a
    prompt record or a decision carries a clock, so a replay reproduces both byte for byte."""
    def run(name: str):
        root = workspace(tmp_path / name)
        step = discover_step(policy=policy_with(retry_limit=0))
        monkeypatch.setattr(runner_mod, "build_scene_revision", FakeBuild(root, moving_scenes()))
        _two_eligible_actions(monkeypatch, coords=(2.0, 2.9))
        runner = answer_when(root, trust_runner(root, trust="excluded"), after_deliveries=2,
                             option=RETRY_MORE)
        run_program(CampaignProgram(steps=(step,)), step.id,
                    config_for(root, trust="excluded", max_deliveries=4) | {"run_isar": runner})
        return ((root / "report" / "decisions.jsonl").read_bytes(), prompts_issued(root),
                log_payloads(root, "prompt_answered"))

    first_decisions, first_prompts, first_answers = run("a")
    second_decisions, second_prompts, second_answers = run("b")

    assert first_prompts and first_prompts == second_prompts
    assert first_answers and first_answers == second_answers
    assert first_decisions == second_decisions
    # No wall clock anywhere in what the channel writes.
    assert not [key for prompt in first_prompts for key in prompt
                if key in ("ts", "written_at", "answered_at", "timestamp")]


def test_the_prompt_timeout_resolves_through_the_precedence_chain(tmp_path):
    """User ask > card guidelines > the named ISAR default of 10 deliveries — frozen into the
    program like every other threshold, never read out of a module at run time."""
    from isar.drive.program_runner import _prompt_timeout_deliveries
    from isar.schema.campaign_program import ProgramError
    from isar.schema.question_policy import resolve_policy
    import pytest

    assert resolve_policy()["thresholds"]["prompt_timeout_deliveries"] == 10
    assert _prompt_timeout_deliveries(discover_step()) == 10
    asked = resolve_policy(user_ask={"prompt_timeout_deliveries": 3})
    assert _prompt_timeout_deliveries(discover_step(policy=asked)) == 3
    assert asked["provenance"]["prompt_timeout_deliveries"]["source"] == "user"

    step = discover_step()
    policy = json.loads(json.dumps(step.payload.resolved_policy))
    policy["thresholds"].pop("prompt_timeout_deliveries")
    object.__setattr__(step.payload, "resolved_policy", policy)
    with pytest.raises(ProgramError):
        _prompt_timeout_deliveries(step)
    with pytest.raises(ValueError):
        resolve_policy(user_ask={"prompt_timeout_deliveries": -1})


def test_the_offramp_default_retires_the_action_exactly_as_c6_already_did(tmp_path, monkeypatch):
    """The declared default changes nothing: silence confirms the retirement C6 already performed,
    so no program revision is compiled and the action stays out of the re-submission set."""
    root = workspace(tmp_path)
    step = discover_step(policy=policy_with(retry_limit=0,
                                            prompt_timeout_deliveries=1))
    monkeypatch.setattr(runner_mod, "build_scene_revision", FakeBuild(root, moving_scenes()))
    _two_eligible_actions(monkeypatch, coords=(2.0, 2.9))

    outcome = run_program(CampaignProgram(steps=(step,)), step.id,
                          config_for(root, trust="excluded", max_deliveries=4))

    defaults = log_payloads(root, "prompt_default_applied")
    assert defaults and defaults[0]["option"] == RETIRE_ACTION
    assert log_payloads(root, "consultation_program_recompiled") == []
    assert len(set(outcome.result.program_fingerprints)) == 1
    retired = defaults[0]["subject"]
    keys = selected_physical_keys(root)
    assert keys.count(retired) == 1, "the retired action was re-submitted after its default"


# ---------------------------------------------------- (4) the compiled revision is read back
def _offramp_run(root: Path, monkeypatch, program, *, answer_after=None, extra_runner=None,
                 max_deliveries=4):
    """One ``run_program`` over the broken-scan world, optionally answering ``retry_more``."""
    monkeypatch.setattr(runner_mod, "build_scene_revision", FakeBuild(root, moving_scenes(24)))
    _two_eligible_actions(monkeypatch, coords=(2.0, 2.9))
    inner = trust_runner(root, trust="excluded")
    runner = (answer_when(root, inner, after_deliveries=answer_after, option=RETRY_MORE)
              if answer_after is not None else inner)
    if extra_runner is not None:
        runner = extra_runner(runner)
    return run_program(program, program.steps[0].id,
                       config_for(root, trust="excluded", max_deliveries=max_deliveries)
                       | {"run_isar": runner})


def test_a_second_run_of_the_same_authored_program_adopts_the_compiled_revision(
        tmp_path, monkeypatch):
    """FIX 1 - "the program is the record" made true. Run 1's ``retry_more`` answer compiles a new
    frozen revision; run 2 is started from the SAME authored program, which carries none of it. The
    driver reads the store back, finds a revision that strips to exactly the authored program, and
    ADOPTS it - so the granted budget survives the run it was granted in.
    """
    root = workspace(tmp_path)
    step = discover_step(policy=policy_with(retry_limit=0))
    authored = CampaignProgram(steps=(step,))
    _offramp_run(root, monkeypatch, authored, answer_after=2)

    compiled = program_pointer(root)["digest"]
    assert compiled != program_fingerprint(authored)
    granted = overrides_of(current_program(root))["retry_budget_by_action"]
    assert granted, "run 1 compiled no budget to adopt"
    before = len(decision_lines(root))

    outcome = _offramp_run(root, monkeypatch, authored)

    adopted = log_payloads(root, "consultation_overrides_adopted")
    assert [(e["program_fingerprint"], e["authored_program_fingerprint"]) for e in adopted] == [
        (compiled, program_fingerprint(authored))]
    assert outcome.result.program_fingerprints[0] == compiled, \
        "run 2 planned under the authored program, not the revision the answer compiled"
    assert {line["decision_basis"]["program_fingerprint"]
            for line in decision_lines(root)[before:]} == {compiled}
    # The budget is HONOURED, not merely present: the offramp bound in force is the granted one
    # and not the step's frozen zero.
    action, budget = next(iter(granted.items()))
    limits = [e["retry_limit"] for e in log_payloads(root, "broken_scan")
              if e["physical_action_key"] == action]
    assert limits[-1] == budget and limits[0] == 0
    # ...and the planner did not revert the pointer to the override-free program.
    assert program_pointer(root)["digest"] == compiled


def test_a_changed_authored_program_starts_a_fresh_lineage_rather_than_adopting(
        tmp_path, monkeypatch):
    """The other half of FIX 1. The stored revision no longer strips to the program being run - the
    operator changed the goal - so it is NOT adopted, the authored program wins, and the refusal is
    recorded rather than silent."""
    root = workspace(tmp_path)
    authored = CampaignProgram(steps=(discover_step(policy=policy_with(retry_limit=0)),))
    _offramp_run(root, monkeypatch, authored, answer_after=2)
    compiled = program_pointer(root)["digest"]

    changed = CampaignProgram(steps=(discover_step(policy=policy_with(retry_limit=3)),))
    assert program_fingerprint(changed) != program_fingerprint(authored)
    before = len(decision_lines(root))
    outcome = _offramp_run(root, monkeypatch, changed)

    assert log_payloads(root, "consultation_overrides_adopted") == []
    refused = log_payloads(root, "consultation_overrides_not_adopted")
    assert [(e["program_fingerprint"], e["authored_program_fingerprint"]) for e in refused] == [
        (compiled, program_fingerprint(changed))]
    assert outcome.result.program_fingerprints[0] == program_fingerprint(changed)
    assert {line["decision_basis"]["program_fingerprint"]
            for line in decision_lines(root)[before:]} == {program_fingerprint(changed)}
    assert overrides_of(current_program(root)) == {}, "an override survived a changed goal"


def test_the_planner_does_not_revert_the_pointer_onto_the_override_free_program(tmp_path):
    """The planner-side half of FIX 1. Its per-plan program write is skipped exactly when the
    pointer already names a CONSULTATION DESCENDANT of the program being written - otherwise the
    override-free program would be published back over the operator's answers on every plan."""
    from isar.drive.steering.planner import _pointer_names_consultation_descendant
    from isar.drive.steering.planner import program_revision

    root = workspace(tmp_path)
    report = root / "report"
    step = discover_step()
    authored = CampaignProgram(steps=(step,))
    PROGRAM_STORE.write_revision(report, program_revision(authored), source="planner")
    assert _pointer_names_consultation_descendant(report, authored) is False

    compiled = apply_consultation_overrides(authored, step.id, keep_open_questions=("q-1",))
    PROGRAM_STORE.write_revision(report, program_revision(compiled), source="consultation")
    assert _pointer_names_consultation_descendant(report, authored) is True
    assert _pointer_names_consultation_descendant(report, compiled) is False, \
        "a revision is not its own consultation descendant"
    other = CampaignProgram(steps=(discover_step(policy=policy_with(retry_limit=3)),))
    assert _pointer_names_consultation_descendant(report, other) is False


def test_a_racing_program_write_surfaces_as_a_conflict_and_the_override_lands_next_delivery(
        tmp_path, monkeypatch):
    """FIX 3 - the compare-and-swap is honest. The recompile's expected parent is the fingerprint
    the step has been PLANNING UNDER, never a pointer read taken at write time (which cannot fail,
    and made the CAS vacuous). A writer that moved the pointer in between is therefore surfaced:
    the conflict is recorded, nothing is silently orphaned, and the override is re-attempted on the
    next delivery under the pointer as it then stands.
    """
    from isar.drive.steering.planner import program_revision

    root = workspace(tmp_path)
    authored = CampaignProgram(steps=(discover_step(policy=policy_with(retry_limit=0)),))
    racer = CampaignProgram(steps=(discover_step(policy=policy_with(retry_limit=7)),))

    def racing(inner):
        state = {"calls": 0}

        def run_isar(ws, data_paths, **kw):
            inner(ws, data_paths, **kw)
            state["calls"] += 1
            if state["calls"] == 2:          # the same delivery the operator's answer lands on
                PROGRAM_STORE.write_revision(Path(ws) / "report", program_revision(racer),
                                             source="another-writer")
        return run_isar

    _offramp_run(root, monkeypatch, authored, answer_after=2, extra_runner=racing,
                 max_deliveries=6)

    conflicts = log_payloads(root, "consultation_recompile_conflict")
    assert len(conflicts) == 1, "the racing writer was not surfaced"
    assert conflicts[0]["expected_parent"] == program_fingerprint(authored)
    # Nothing was applied on the conflicting pass, and the next delivery landed it cleanly.
    recompiled = log_payloads(root, "consultation_program_recompiled")
    assert len(recompiled) == 1 and recompiled[0]["iter"] > conflicts[0]["iter"]
    assert overrides_of(current_program(root))["retry_budget_by_action"], \
        "the override never landed after the conflict"


# ---------------------------------------------------------------------- (5) merge and stray files
def test_a_re_answer_can_only_raise_a_retry_budget(tmp_path):
    """FIX 7 - the override merge is MONOTONE. Every path computes a budget from where the action
    already is, so a later answer that recomputes a smaller number must not take budget back off an
    action the operator had just granted more of."""
    program = CampaignProgram(steps=(discover_step(),))
    once = apply_consultation_overrides(program, "discover-1", retry_budget_by_action={"a": 5})
    lowered = apply_consultation_overrides(once, "discover-1", retry_budget_by_action={"a": 2})
    raised = apply_consultation_overrides(once, "discover-1",
                                          retry_budget_by_action={"a": 9, "b": 1})

    assert overrides_of(once.to_dict())["retry_budget_by_action"] == {"a": 5}
    assert overrides_of(lowered.to_dict())["retry_budget_by_action"] == {"a": 5}
    assert overrides_of(raised.to_dict())["retry_budget_by_action"] == {"a": 9, "b": 1}


def test_a_corrupt_file_after_the_default_is_invalid_and_leaves_the_stale_slot_alone(tmp_path):
    """FIX 9 - a corrupt file is not a stale answer. Recording it as one (with a null option) both
    misnamed it and consumed the ONE stale report, so the real late answer the operator wrote over
    it afterwards was never reported at all."""
    root = workspace(tmp_path)
    channel = channel_for(root, timeout=0)
    prompt = channel.issue([CLOSURE_REQUEST], delivery=0, program_fingerprint="pf")[0]
    assert [r.source for r in channel.poll(0)] == ["default"]

    path = answer_path(root, prompt.prompt_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    channel.poll(1)
    assert [e["error"][:8] for e in log_payloads(root, "prompt_answer_invalid")] == ["not JSON"]
    assert log_payloads(root, "stale_answer") == [], "a corrupt file was reported as an answer"

    write_answer(root, prompt.prompt_id, KEEP_SPENDING)
    channel.poll(2)
    assert [(e["prompt_id"], e["option"]) for e in log_payloads(root, "stale_answer")] == [
        (prompt.prompt_id, KEEP_SPENDING)]

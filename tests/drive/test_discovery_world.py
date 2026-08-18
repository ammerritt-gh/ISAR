"""The discovery WORLD, end to end (SCENE-4.3 C9 — the plan's §Exit acceptance lane).

C4-C8 each pinned their own mechanism on a world of its own; this file owns the two §Exit claims
that are about the whole campaign rather than about any one mechanism:

* **the canonical world** — a cold workspace with nothing but declarations reaches an anchored
  first scan, keeps measuring across several deliveries, and ends on an honest verdict, with a
  complete DecisionBasis on EVERY decision and the campaign-provenance block on EVERY submission.
  There is no bootstrap special case anywhere on that path: the first scan is planned by the same
  `plan_next_action` call as the fifth, from a partition minted through the same `partition_key`.
* **same-seed full-campaign replay** — two complete `run_program` executions over identical inputs
  produce the identical ACTION SEQUENCE and a byte-identical `decisions.jsonl`. This is the
  sharpest determinism claim in the slice, and it is deliberately taken over a MULTI-delivery
  campaign: the lane-local replay tests (broken scan, consultation) each pin their own counters,
  and a single-delivery replay would pin almost nothing.

The other four §Exit worlds are already owned elsewhere and are NOT duplicated here: the blank
world and the synchronous-build-failure fault in `test_program_runner.py`, defaults-taken prompts
in `test_consultation.py`, and the broken-scan quarantine/retry/offramp lane in
`test_broken_scan_lane.py`.
"""
from __future__ import annotations

import json
from pathlib import Path

import isar.drive.program_runner as runner_mod
from isar.drive.program_runner import (
    VERDICT_COMPLETE, VERDICT_IN_PROGRESS, VERDICT_MACHINE_FAILURE, VERDICT_OVER_BUDGET,
    run_program)
from isar.schema.campaign_program import CampaignProgram, program_fingerprint
from isar.schema.measurement import CAMPAIGN_PROVENANCE_FIELDS

from drive.test_broken_scan_lane import config_for, moving_scenes
from drive.test_program_runner import (
    FakeBuild, MockTavi, decision_lines, discover_step, log_payloads, workspace)


#: Every key `_decision_basis_common` writes unconditionally. A decision missing one of these is
#: a decision that cannot be re-checked later, which is the whole reason the record exists.
BASIS_KEYS = frozenset({"program_fingerprint", "step_id", "resolved_policy_fingerprint",
                        "generator_registry", "scheduler_policy", "locality", "placement",
                        "staleness_policy", "scene_digest", "agenda_digest", "basis_digest"})

DELIVERIES = 3


def canonical_world(root: Path, monkeypatch, client=None, **extra):
    """The cold-start world: declarations only, a healthy instrument, trusted deliveries.

    The scene payload MOVES every delivery (distinct digests, same content) exactly as production's
    does — a fixture that republishes one payload would let a stale-pin bug pass.
    """
    step = discover_step()
    monkeypatch.setattr(runner_mod, "build_scene_revision",
                        FakeBuild(root, moving_scenes(DELIVERIES + 2)))
    program = CampaignProgram(steps=(step,))
    return program, step, run_program(
        program, step.id,
        config_for(root, trust="trusted", client=client or MockTavi(),
                   max_deliveries=DELIVERIES, **extra))


# ------------------------------------------------------------------ (a) the canonical world
def test_the_canonical_world_runs_from_declarations_to_an_honest_verdict(tmp_path, monkeypatch):
    root = workspace(tmp_path)
    client = MockTavi()
    program, step, outcome = canonical_world(root, monkeypatch, client=client)

    # It measured, and it ended on one of the four reasons or the honest in-progress state — never
    # a fault, and never silence.
    assert outcome.verdict in (VERDICT_COMPLETE, VERDICT_IN_PROGRESS, VERDICT_OVER_BUDGET)
    assert outcome.verdict != VERDICT_MACHINE_FAILURE
    assert outcome.result.deliveries == DELIVERIES
    assert len(client.bodies) == DELIVERIES

    # The FIRST scan comes off the declared partition's anchor, not a bootstrap path and not the
    # region midpoint (2.5 for the 2.0->3.0 step): cold start is declared knowledge all the way
    # down, so the declaration is what aims the first request.
    first = client.bodies[0]["parameters"]
    assert (first["H"], first["K"], first["L"]) == (2.0, 0.0, 0.0)
    assert outcome.result.declared_partition_id

    # Provenance on EVERY submission, not just the first: an attempt that cannot name its program
    # step cannot be partitioned by path later.
    submissions = [p for p in log_payloads(root, "run") if p.get("job_id")]
    assert len(submissions) == DELIVERIES
    for block in submissions:
        assert set(CAMPAIGN_PROVENANCE_FIELDS) <= set(block)
        assert block["topology_role"] == "canonical_path"
        assert block["program_fingerprint"] == program_fingerprint(program)
        assert block["program_step"] == step.id
        assert block["path_fingerprint"]

    # A complete DecisionBasis on EVERY decision, and the declared partition embedded on the ones
    # taken while the declaration was still live.
    lines = decision_lines(root)
    assert len(lines) >= DELIVERIES
    for line in lines:
        assert BASIS_KEYS <= set(line["decision_basis"])
        assert line["decision_basis"]["program_fingerprint"] == program_fingerprint(program)
    declared = lines[0]["decision_basis"]["declared_partition"]
    assert declared["step_id"] == step.id
    assert declared["partition_id"] == outcome.result.declared_partition_id

    # Question-driven throughout: every selection is supplied by a question's own generator, and
    # each cites the scene revision it was planned against.
    # One selection per delivery, plus the one the delivery ceiling cut short: the step plans the
    # next action before it learns it has run out of deliveries, which is the honest order.
    selected = [line for line in lines if line["outcome"] == "selected"]
    assert len(selected) == DELIVERIES + 1
    for line in selected:
        assert line["selection"]["supplying_association"]["question_id"]
        assert line["decision_basis"]["scene_digest"]
    assert len({line["decision_basis"]["scene_digest"] for line in selected}) == len(selected), \
        "two decisions were planned against the same scene revision — a pin did not move"


# ------------------------------------------------------------------ (c) same-seed replay
def test_a_same_seed_full_campaign_replays_action_for_action_and_byte_for_byte(
        tmp_path, monkeypatch):
    """The sharpest determinism claim in the slice: identical inputs, identical campaign.

    Both halves are asserted because either alone can hide a defect — identical decision bytes with
    a different submitted grid would mean the realization layer had drifted from the record, and an
    identical action sequence with different decision bytes would mean the ledger had acquired
    something the campaign's behaviour does not depend on (a clock, a path, a counter).
    """
    def campaign(name: str) -> tuple[list, bytes]:
        root = workspace(tmp_path / name)
        client = MockTavi()
        canonical_world(root, monkeypatch, client=client)
        return (client.bodies, (root / "report" / "decisions.jsonl").read_bytes())

    first_actions, first_decisions = campaign("a")
    second_actions, second_decisions = campaign("b")

    assert first_actions, "the replay world submitted nothing to compare"
    assert len(first_actions) == DELIVERIES
    assert [body["parameters"] for body in first_actions] \
        == [body["parameters"] for body in second_actions]
    assert first_decisions == second_decisions
    # Guard the guard: the bytes really do carry one parseable decision per delivery, so byte
    # equality is a claim about a campaign rather than about an empty file.
    records = [json.loads(line) for line in first_decisions.splitlines() if line.strip()]
    assert len(records) >= DELIVERIES

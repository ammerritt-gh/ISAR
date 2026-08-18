"""The broken-scan lane on the discovery step-runner (SCENE-4.3 C6).

Three failure classes, three routes, and none of them stops the campaign for bad data:

1. LOCAL CORRUPTION — the delivery lands but the 4.0 count-trust lanes distrust its counts. The
   retry is the EXISTING fail-closed behaviour (an untrusted arena earns no footprint and no
   coverage credit, pinned in ``tests/scene/test_build.py`` — ``non_scoring`` with reason
   ``count_untrusted``), so the question stays open and the same action is re-selected; C6 only
   BOUNDS that, then offramps to a consultation request and keeps planning.
2. TRANSIENT REQUEST FAILURE — transport, recorded through the existing refusal vocabulary,
   suppressing nothing and quarantining nothing.
3. GLOBAL MACHINE FAILURE — the same transport failure, repeated past a bounded threshold.

The wire is mocked exactly as in ``test_program_runner.py`` (whose builders these reuse): the
analysis stand-in additionally writes the counts-authoritative ``report/measurements.jsonl``, which
is the only thing that decides trust here — counts are authoritative, fits are disposable, so a
delivered fit row beside an untrusted record is the record's call, not the row's.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

import isar.drive.program_runner as runner_mod
from isar.drive.instrument.tavi_client import TaviError
from isar.drive.program_runner import (
    MAX_CONSECUTIVE_TRANSPORT_FAILURES, OFFRAMP_KIND, VERDICT_MACHINE_FAILURE, run_program)
from isar.schema.campaign_program import CampaignProgram, ProgramError
from isar.schema.measurement import COUNT_TRUST_STATES
from isar.schema.question_policy import resolve_policy

from drive.test_program_runner import (
    COLD_START_SCENE, FakeBuild, MockTavi, decision_lines, discover_step, log_payloads, workspace)


# ---------------------------------------------------------------------------- builders
def policy_with(retry_limit: int | None = None, **extra):
    ask = {"min_effective_exposure": 1.0, **extra}
    if retry_limit is not None:
        ask["broken_scan_retry_limit"] = retry_limit
    return resolve_policy(user_ask=ask)


def trust_runner(root: Path, *, trust: str):
    """The analysis stand-in, plus the counts-authoritative record every delivery now writes.

    ``trust`` is the ``count_trust`` lane each new arena lands in. ``"excluded"`` is a real terminal
    gate outcome from the 4.0 vocabulary — nothing here invents a new state, and nothing deletes or
    masks anything: the record IS the quarantine.
    """
    assert trust in COUNT_TRUST_STATES
    seen: dict[str, int] = {}

    def run_isar(ws, data_paths, **kw):
        data_dir = Path(data_paths[0])
        report = Path(ws) / "report"
        report.mkdir(parents=True, exist_ok=True)
        rows = []
        for path in sorted(data_dir.glob("*.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            params = document.get("launch", {}).get("parameters", {})
            if not params:
                continue
            new = path.stem not in seen
            seen.setdefault(path.stem, len(seen) + 1)
            index = seen[path.stem]
            rows.append({"scan": index, "detector": "det",
                         "h": params["H"], "k": params["K"], "l": params["L"],
                         "status": "fitted", "verdict": "resolved", "disposition": "resolved",
                         "confidence": 0.9, "omega0": 4.0, "interval_lo": 3.7,
                         "interval_hi": 4.3, "interval_reliable": True, "reasons": "resolved"})
            if new:
                record = {
                    "schema": "isar.measurement-arena/1", "scan_ref": f"mock:{index}:det",
                    "scan_id": f"mock:{index}:det", "scan_number": index, "detector": "det",
                    "sample": "tavi", "conditions": [["temperature_K", 300.0]],
                    "q": [params["H"], params["K"], params["L"]], "lattice_epoch": 1,
                    "energy_window": None, "energy_bands": None, "effective_exposure": 5.0,
                    "exposure_provenance": None, "count_trust": trust, "reasons": ["mock"],
                    "written_at": "2026-08-18T00:00:00+00:00", "content_hash": f"c{index}",
                    "program_fingerprint": None, "program_step": None, "topology_role": None,
                    "path_fingerprint": None}
                with open(report / "measurements.jsonl", "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record) + "\n")
        columns = ["scan", "detector", "h", "k", "l", "status", "verdict", "disposition",
                   "confidence", "omega0", "interval_lo", "interval_hi", "interval_reliable",
                   "reasons"]
        with open(report / "results.csv", "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            for row in rows:
                writer.writerow({c: row.get(c) for c in columns})

    return run_isar


class UnreachableTavi(MockTavi):
    """Transport is down: the submission never reaches the instrument. Not geometry, not physics."""

    def __init__(self, fail_first: int | None = None):
        super().__init__()
        self.fail_first = fail_first
        self.attempts = 0

    def submit_scan(self, body, idempotency_key=None):
        self.attempts += 1
        if self.fail_first is None or self.attempts <= self.fail_first:
            raise TaviError(status=503, message="instrument gateway unreachable")
        return super().submit_scan(body, idempotency_key=idempotency_key)


def config_for(root: Path, *, trust: str = "trusted", client=None, **extra) -> dict:
    return {"workspace": str(root), "client": client or MockTavi(),
            "run_isar": trust_runner(root, trust=trust), "sample": "tavi", "seed": 0, **extra}


def _two_eligible_actions(monkeypatch, coords=(2.0, 2.9)):
    """Two eligible survivors instead of the survey's one, so "the step plans a DIFFERENT action"
    is observable at all. Same synthetic generator ``tests/drive/test_planner.py`` already uses."""
    from drive.test_planner import _two_action_generator
    import isar.drive.steering.readiness as readiness_module
    from isar.drive.steering.readiness import GENERATOR_FUNCTIONS

    patched = dict(GENERATOR_FUNCTIONS)
    patched["opening_book_survey"] = _two_action_generator(coords)
    monkeypatch.setattr(readiness_module, "GENERATOR_FUNCTIONS", patched)


def selected_fingerprints(root: Path) -> list[str]:
    return [line["selection"]["fingerprint"] for line in decision_lines(root)
            if line["outcome"] == "selected"]


def moving_scenes(n: int = 12) -> list[dict]:
    """``n`` DISTINCT scene payloads — what production actually publishes.

    The real scene moves on every delivery (an untrusted arena still enters the scene as a synthetic
    observation), so a fixture that republishes one payload hides the failure this file's key test is
    about. The nonce differs only in a field no derivation reads, so what moves is the DIGEST and
    nothing else about the world the planner sees.
    """
    return [dict(COLD_START_SCENE, scene_nonce=i) for i in range(n)]


def selected_physical_keys(root: Path) -> list[str]:
    """The PHYSICAL action key of every selected decision, resolved through its own inventory blob."""
    from isar.schema.action_identity import intent_from_mapping, physical_action_key

    keys = []
    for line in decision_lines(root):
        if line["outcome"] != "selected":
            continue
        blob = json.loads((root / line["inventory_ref"]["path"]).read_text(encoding="utf-8"))
        row = next(r for r in blob["evaluations"]
                   if r["scientific_action_fingerprint"] == line["selection"]["fingerprint"])
        keys.append(physical_action_key(intent_from_mapping(row["intent"])))
    return keys


# ---------------------------------------------------------------------------- (1) local corruption
def test_an_untrusted_delivery_earns_no_coverage_and_the_same_action_is_resubmitted(
        tmp_path, monkeypatch):
    """The retry, and what makes it a retry: the SAME physical action (same scientific action
    fingerprint) is submitted again, because the untrusted arena bought no coverage credit and left
    the question exactly as open as it was. The record itself is untouched — masked, never deleted.
    """
    root = workspace(tmp_path)
    step = discover_step(policy=policy_with(retry_limit=2))
    monkeypatch.setattr(runner_mod, "build_scene_revision", FakeBuild(root, [COLD_START_SCENE]))

    outcome = run_program(CampaignProgram(steps=(step,)), step.id,
                          config_for(root, trust="excluded", max_deliveries=2))

    chosen = selected_fingerprints(root)
    assert len(chosen) >= 2, "the broken delivery was not retried"
    assert chosen[0] == chosen[1], "the retry submitted a DIFFERENT action"

    broken = log_payloads(root, "broken_scan")
    assert [event["broken_deliveries"] for event in broken] == [1, 2]
    assert broken[0]["scientific_action_fingerprint"] == chosen[0]
    assert broken[0]["count_trust"] == ["excluded"]
    assert broken[0]["retry_limit"] == 2

    # Quarantine is the 4.0 record state: every attempted arena is still on disk, still untrusted.
    records = [json.loads(line) for line in
               (root / "report" / "measurements.jsonl").read_text(encoding="utf-8").splitlines()]
    assert records and all(r["count_trust"] == "excluded" for r in records)
    assert len(records) == outcome.result.deliveries
    assert outcome.result.consultation_requests == ()      # budget not spent yet: no offramp


def test_the_exhausted_retry_budget_offramps_and_the_step_keeps_planning(tmp_path, monkeypatch):
    """The offramp: a recorded consultation request (C8 turns it into a prompt), the action retired
    from THIS step's re-submission set, and the step continuing — bad data is not a stop reason."""
    root = workspace(tmp_path)
    step = discover_step(policy=policy_with(retry_limit=0))   # offramp on the first broken delivery
    monkeypatch.setattr(runner_mod, "build_scene_revision", FakeBuild(root, [COLD_START_SCENE]))
    _two_eligible_actions(monkeypatch)

    outcome = run_program(CampaignProgram(steps=(step,)), step.id,
                          config_for(root, trust="excluded", max_deliveries=3))

    requests = [r for r in outcome.result.consultation_requests if r["kind"] == OFFRAMP_KIND]
    assert requests, "no offramp was recorded"
    first = requests[0]
    assert first["retry_count"] == 0 and first["retry_limit"] == 0
    assert first["region"]["q_start"] == list(step.payload.q_start)
    assert first["scan_refs"], "the offramp names no arena"

    chosen = selected_fingerprints(root)
    assert len(chosen) >= 2, "the step stopped instead of planning past the broken action"
    assert chosen[1] != first["scientific_action_fingerprint"], \
        "the retired action was re-proposed after its offramp"
    # The step ran on; it never ended on the broken scan.
    assert outcome.verdict != VERDICT_MACHINE_FAILURE
    assert outcome.result.deliveries >= 2


def test_the_retired_action_stays_in_the_inventory_and_produces_no_durable_evidence(
        tmp_path, monkeypatch):
    """A broken detector is not a statement about the physics: the offramp narrows the SELECTION
    input only. The evaluation is still in the persisted inventory, and nothing durable was
    emitted about the question."""
    root = workspace(tmp_path)
    step = discover_step(policy=policy_with(retry_limit=0))
    monkeypatch.setattr(runner_mod, "build_scene_revision", FakeBuild(root, [COLD_START_SCENE]))
    _two_eligible_actions(monkeypatch)

    outcome = run_program(CampaignProgram(steps=(step,)), step.id,
                          config_for(root, trust="excluded", max_deliveries=3))

    retired = outcome.result.consultation_requests[0]["scientific_action_fingerprint"]
    later = [line for line in decision_lines(root) if line["outcome"] == "selected"][-1]
    assert later["selection"]["fingerprint"] != retired, "the fixture never planned past it"
    blob = json.loads((root / later["inventory_ref"]["path"]).read_text(encoding="utf-8"))
    fingerprints = {row["scientific_action_fingerprint"] for row in blob["evaluations"]}
    assert retired in fingerprints, "the offramp deleted evidence instead of narrowing selection"
    assert not [line for line in decision_lines(root) if line["outcome"] == "controller_fault"]


def test_the_bound_binds_and_the_retirement_holds_while_the_scene_moves_every_delivery(
        tmp_path, monkeypatch):
    """The failure every other test in this file misses: in production the scene MOVES on each
    delivery, and the scientific action fingerprint hashes the scene digest. Keyed on that, the
    counter would mint a fresh key each retry, the bound would never bind, and the retirement filter
    would never match — a bounded retry that silently retries forever. Keyed on the scene-invariant
    physical action key, the SAME physical action broken twice under two different scene revisions
    reaches its limit and retires, and the third plan (under a third digest) still excludes it."""
    root = workspace(tmp_path)
    step = discover_step(policy=policy_with(retry_limit=1))     # retire on the second broken scan
    monkeypatch.setattr(runner_mod, "build_scene_revision", FakeBuild(root, moving_scenes()))
    _two_eligible_actions(monkeypatch, coords=(2.0,))           # exactly ONE physical action exists

    outcome = run_program(CampaignProgram(steps=(step,)), step.id,
                          config_for(root, trust="excluded", max_deliveries=3))

    # The premise: the scene genuinely moved, so the scientific identity of one physical action did
    # too. Without that this test would pass for the wrong reason.
    decisions = decision_lines(root)
    cited = [line["decision_basis"]["scene_digest"] for line in decisions]
    assert len(set(cited)) == len(cited) >= 3, "the fixture republished one scene"

    broken = log_payloads(root, "broken_scan")
    assert len(broken) == 2
    assert broken[0]["physical_action_key"] == broken[1]["physical_action_key"]
    assert (broken[0]["scientific_action_fingerprint"]
            != broken[1]["scientific_action_fingerprint"]), "the scene did not move between retries"
    assert [event["broken_deliveries"] for event in broken] == [1, 2]

    requests = [r for r in outcome.result.consultation_requests if r["kind"] == OFFRAMP_KIND]
    assert requests, "the retry bound never bound: no offramp under a moving scene"
    retired = requests[0]["physical_action_key"]
    assert selected_physical_keys(root) == [retired, retired], \
        "the two broken deliveries were not one physical action"

    # The third plan, under a THIRD scene digest: the only action there is is the retired one, so an
    # exclusion that keyed on scientific identity would have re-selected it here.
    third = decisions[2]
    assert third["outcome"] == "no_eligible_action"
    assert third["decision_basis"]["retired_physical_actions"] == [retired]
    assert outcome.result.deliveries == 2


# ---------------------------------------------------------------------------- (2) transient
def test_a_transport_failure_quarantines_nothing_and_leaves_no_durable_evidence(
        tmp_path, monkeypatch):
    """Class 2. The existing refusal vocabulary classes an unmapped verdict TRANSIENT/scope
    ``none`` — it suppresses nothing — and the step re-plans and delivers on the next attempt."""
    root = workspace(tmp_path)
    step = discover_step()
    monkeypatch.setattr(runner_mod, "build_scene_revision", FakeBuild(root, [COLD_START_SCENE]))
    client = UnreachableTavi(fail_first=1)

    outcome = run_program(CampaignProgram(steps=(step,)), step.id,
                          config_for(root, client=client, max_deliveries=1))

    suppressed = log_payloads(root, "action_suppressed")
    assert suppressed, "the transport failure was not recorded at all"
    assert suppressed[0]["rejection_class"] == "transient"
    assert suppressed[0]["scope"] == "none"
    assert outcome.verdict != VERDICT_MACHINE_FAILURE
    assert outcome.result.deliveries == 1, "the step never recovered from a transient failure"
    assert log_payloads(root, "broken_scan") == [], "a transport failure quarantined a scan"


def test_repeated_total_transport_failure_escalates_to_a_machine_failure(tmp_path, monkeypatch):
    """Class 3. Transient has a bound: an instrument that will not talk for
    ``MAX_CONSECUTIVE_TRANSPORT_FAILURES`` whole attempts running is a machine failure, which IS
    the stop-law pause — no new pause machinery."""
    root = workspace(tmp_path)
    step = discover_step()
    monkeypatch.setattr(runner_mod, "build_scene_revision", FakeBuild(root, [COLD_START_SCENE]))
    client = UnreachableTavi()

    outcome = run_program(CampaignProgram(steps=(step,)), step.id,
                          config_for(root, client=client, max_deliveries=10))

    assert outcome.verdict == VERDICT_MACHINE_FAILURE
    assert "instrument_unreachable" in outcome.result.reason
    assert f"{MAX_CONSECUTIVE_TRANSPORT_FAILURES} consecutive" in outcome.result.reason
    assert outcome.result.deliveries == 0


# ---------------------------------------------------------------------------- (3) the budget
def test_the_retry_limit_resolves_through_the_precedence_chain(tmp_path):
    """User ask > card guidelines > the named ISAR default, and the resolved value is FROZEN into
    the program — never read out of this module at run time."""
    from isar.drive.program_runner import _broken_scan_retry_limit

    assert resolve_policy()["thresholds"]["broken_scan_retry_limit"] == 2       # named default
    assert resolve_policy()["provenance"]["broken_scan_retry_limit"] == {
        "source": "default", "authority": "target"}

    carded = resolve_policy(card_guidelines={"broken_scan_retry_limit": 1})
    assert carded["thresholds"]["broken_scan_retry_limit"] == 1
    assert carded["provenance"]["broken_scan_retry_limit"]["source"] == "card"

    asked = resolve_policy(user_ask={"broken_scan_retry_limit": 5},
                           card_guidelines={"broken_scan_retry_limit": 1})
    assert asked["thresholds"]["broken_scan_retry_limit"] == 5
    assert asked["provenance"]["broken_scan_retry_limit"]["source"] == "user"

    # The runner reads the PROGRAM's frozen value, not the module default.
    assert _broken_scan_retry_limit(discover_step(policy=asked)) == 5
    assert _broken_scan_retry_limit(discover_step()) == 2
    # Zero is a legitimate declaration ("offramp on the first broken scan"); a fraction is not.
    assert resolve_policy(user_ask={"broken_scan_retry_limit": 0})[
        "thresholds"]["broken_scan_retry_limit"] == 0
    with pytest.raises(ValueError):
        resolve_policy(user_ask={"broken_scan_retry_limit": 1.5})
    with pytest.raises(ValueError):
        resolve_policy(user_ask={"broken_scan_retry_limit": -1})


def test_a_program_frozen_without_the_retry_budget_refuses(tmp_path):
    """A frozen program never acquires a read-time default — including this one."""
    from isar.drive.program_runner import _broken_scan_retry_limit

    step = discover_step()
    policy = json.loads(json.dumps(step.payload.resolved_policy))
    policy["thresholds"].pop("broken_scan_retry_limit")
    object.__setattr__(step.payload, "resolved_policy", policy)
    with pytest.raises(ProgramError):
        _broken_scan_retry_limit(step)


# ---------------------------------------------------------------------------- (4) replay
def test_the_broken_scan_lane_is_replay_deterministic(tmp_path, monkeypatch):
    """Same inputs, same decisions and same retry sequence: the counters are delivery-indexed, so
    a replay reproduces them. Byte identity of ``decisions.jsonl`` is the test — a wall-clock field
    anywhere on this path would fork it."""
    def run(name: str) -> tuple[bytes, list]:
        root = workspace(tmp_path / name)
        step = discover_step(policy=policy_with(retry_limit=1))
        monkeypatch.setattr(runner_mod, "build_scene_revision", FakeBuild(root, [COLD_START_SCENE]))
        run_program(CampaignProgram(steps=(step,)), step.id,
                    config_for(root, trust="excluded", max_deliveries=3))
        return ((root / "report" / "decisions.jsonl").read_bytes(),
                [(e["scientific_action_fingerprint"], e["broken_deliveries"])
                 for e in log_payloads(root, "broken_scan")])

    first_decisions, first_retries = run("a")
    second_decisions, second_retries = run("b")

    assert first_retries and first_retries == second_retries
    assert [count for _, count in first_retries][:2] == [1, 2]
    assert first_decisions == second_decisions


def test_a_restart_does_not_hand_a_broken_action_a_fresh_retry_budget(tmp_path, monkeypatch):
    """``broken_deliveries`` is rebuilt from the ``broken_scan`` events the lane already writes
    (H21(e)) — derived state, never a second store.

    Held only in memory, a restart reset the counter, so an action that had already spent its
    budget went back to costing full exposure for another `retry_limit` deliveries every time the
    campaign was restarted. Nothing new is persisted for this: the count rides events the lane was
    already emitting.
    """
    from isar.drive.program_runner import _resumed_broken_deliveries

    root = workspace(tmp_path)
    step = discover_step(policy=policy_with(retry_limit=1))
    program = CampaignProgram(steps=(step,))
    monkeypatch.setattr(runner_mod, "build_scene_revision", FakeBuild(root, moving_scenes()))

    run_program(program, step.id, config_for(root, trust="excluded", max_deliveries=2))

    broken = [p for p in log_payloads(root, "decide") if p.get("event") == "broken_scan"]
    assert broken, "the world recorded no broken delivery to resume from"
    resumed = _resumed_broken_deliveries(root / "report" / "session_log.jsonl", step.id)
    assert resumed, "a restart would have handed every broken action a fresh budget"
    for event in broken:
        assert resumed[event["physical_action_key"]] >= event["broken_deliveries"]

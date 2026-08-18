"""``run_program`` — one discovery step, end to end (SCENE-4.3 C5, D7).

One scenario per acceptance item in the plan's C5 bullet: the seam's triple, a question-driven
delivery that reaches a real submission, a blank world that terminates honestly, a failed
synchronous build that becomes a controller fault and never steers from the older scene, the
concurrent-pointer-write non-fault, and both staleness arms. The CLI's exit vocabulary — including
the exit-7 gate that stayed shut for a step kind with no runner — is pinned in
``tests/cli/test_drive_cmd.py``, not here.

The scene-build SUBPROCESS is the one seam these tests stand in for: spawning a real interpreter
per delivery costs seconds, and what the fake preserves is exactly the contract the parent depends
on — a digest to pin, or a :class:`SceneBuildFailed`. One test
(``tests/scene/test_cli_scene_build.py``) runs the real child so the fake cannot drift from it.
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

import isar.drive.loop as loop_mod
import isar.drive.program_runner as runner_mod
from isar.drive.program_runner import (
    BEAM_HOLD, BEAM_RELEASE, ProgramStepOutcome, SceneBuildFailed, SceneBuildReport, StepResult,
    StalenessModeRefused, VERDICT_COMPLETE, VERDICT_IN_PROGRESS, VERDICT_MACHINE_FAILURE,
    VERDICT_OPERATOR_STOPPED, VERDICT_OVER_BUDGET, run_program)
from isar.drive.steering.realization import DISCOVERY_NCOUNT_PROVISIONAL
from isar.schema.campaign_program import (
    CampaignProgram, CampaignStep, DiscoverAndMapSpec, ProgramError, program_fingerprint)
from isar.schema.measurement import CAMPAIGN_PROVENANCE_FIELDS
from isar.schema.question_policy import resolve_policy
from isar.scene.model import canonical_bytes, digest_bytes
from isar.scene.store import SCENE_STORE

from synthetic_ws import write_policy, write_session, write_state


# ---------------------------------------------------------------------------- builders
@dataclass(frozen=True, slots=True)
class _SceneRevision:
    canonical: bytes
    digest: str

    @classmethod
    def of(cls, payload: dict) -> "_SceneRevision":
        blob = canonical_bytes(payload)
        return cls(canonical=blob, digest=digest_bytes(blob))


def scene(sample_knowledge=None, t_round=5.0):
    """A workspace with no measurement: declarations only — the cold-start world."""
    return {"observations": [], "footprints": [], "clusters": [], "non_scoring": [],
            "sample_knowledge": sample_knowledge or {}, "condition_t_round": t_round}


COLD_START_SCENE = scene(sample_knowledge={"zone_period": 1.0, "lattice_epoch": 1})
#: A scene with no declared zone geometry at all — the blank-knowledge world.
BLANK_SCENE = scene()


def discover_step(step_id="discover-1", q_start=(2.0, 0.0, 0.0), q_end=(3.0, 0.0, 0.0),
                  spacing=1.0, domain=(0.0, 10.0), policy=None):
    spec = DiscoverAndMapSpec(
        q_start=q_start, q_end=q_end, maximum_initial_spacing_rlu=spacing,
        intrinsic_omega_domain_meV=domain, catalogue_profile="isar.catalogue.default/1",
        resolved_policy=policy or resolve_policy(user_ask={"min_effective_exposure": 1.0}))
    return CampaignStep(id=step_id, kind="discover_and_map", payload=spec)


def workspace(tmp_path: Path) -> Path:
    """An INITIALIZED workspace: a card with the declarations cold start mints its partition from.

    A discovery step refuses a card-less workspace by name (``WorkspaceUndeclared``) — cold start
    is declared knowledge all the way down, and the card is where the declarations live.
    """
    root = tmp_path / "ws"
    (root / "report").mkdir(parents=True)
    (root / "data").mkdir(parents=True)
    write_policy(root)
    write_state(root, lattice_epochs=[{"version": 1}])
    write_session(root)
    return root


def write_scene(root: Path, payload: dict) -> str:
    return SCENE_STORE.write_revision(root / "report", _SceneRevision.of(payload)).digest


def decision_lines(root: Path) -> list[dict]:
    path = root / "report" / "decisions.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def log_payloads(root: Path, action: str) -> list[dict]:
    path = root / "report" / "session_log.jsonl"
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        payload = event.get("payload") or {}
        if payload.get("action") == action or event.get("kind") == action:
            out.append(payload)
    return out


# ---------------------------------------------------------------------------- fake seams
class FakeBuild:
    """Stands in for the supervised ``isar scene-build`` child.

    ``payloads`` is the scene each successive build publishes; the last one repeats. ``fail_after``
    makes the Nth build raise, which is the synchronous-failure fixture.
    """

    def __init__(self, root: Path, payloads, *, fail_after=None, pointer_flipped=True):
        self.root = root
        self.payloads = list(payloads)
        self.fail_after = fail_after
        self.pointer_flipped = pointer_flipped
        self.calls = 0
        self.digests: list[str] = []

    def __call__(self, workspace_root, *, source="campaign", timeout=None):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise SceneBuildFailed("scene build exited 3: adapter refused the workspace")
        payload = self.payloads[min(self.calls - 1, len(self.payloads) - 1)]
        digest = write_scene(self.root, payload)
        self.digests.append(digest)
        return SceneBuildReport(digest=digest, pointer_flipped=self.pointer_flipped,
                                unchanged=False, seconds=0.01)


class MockTavi:
    """The scripted instrument: everything queues, every scan completes, one Gaussian in view."""

    def __init__(self):
        self.jobs: dict[str, dict] = {}
        self.n = 0
        self.bodies: list[dict] = []

    def validate(self, body):
        return {"would_queue": True, "blockers": [], "eta": {"estimated_seconds": 5.0}}

    def submit_scan(self, body, idempotency_key=None):
        self.n += 1
        job_id = f"j-{self.n}"
        self.jobs[job_id] = body
        self.bodies.append(body)
        return {"job_id": job_id, "state": "queued", "eta": {"estimated_seconds": 5.0}}

    def wait_scan(self, job_id, timeout=0.0, **kw):
        return {"job_id": job_id, "state": "done"}

    def get_scan_data(self, job_id):
        params = self.jobs[job_id]["parameters"]
        _, lo, hi, step = params["scan_command1"].split()
        lo, hi, step = float(lo), float(hi), float(step)
        n = int(round((hi - lo) / step)) + 1
        grid = [lo + i * step for i in range(n)]
        counts = [1.0 + 100.0 * pow(2.718281828, -((v - 4.0) / 0.5) ** 2) for v in grid]
        return {"job_id": job_id, "state": "done",
                "result": {"counts": counts, "scan_values_1": grid},
                "launch": {"parameters": params}}

    def get_resolution(self, h, k, l, deltaE):
        return {"ok": True, "bragg": {"dE": 0.3}}


class DecliningTavi(MockTavi):
    """Geometry refuses every request — the decline lane C7 will turn into blocker evidence."""

    def validate(self, body):
        return {"would_queue": False, "blockers": ["q_unreachable_geometry"],
                "requested_points": 10, "feasible_points": 0}


def mock_runner(root: Path):
    """The analysis boundary stand-in: writes the minimum ``results.csv`` the loop folds."""
    seen: dict[str, int] = {}

    def run_isar(ws, data_paths, **kw):
        data_dir = Path(data_paths[0])
        rows = []
        for path in sorted(data_dir.glob("*.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            params = document.get("launch", {}).get("parameters", {})
            if not params:
                continue
            seen.setdefault(path.stem, len(seen) + 1)
            rows.append({"scan": seen[path.stem], "detector": "det",
                         "h": params["H"], "k": params["K"], "l": params["L"],
                         "status": "fitted", "verdict": "resolved", "disposition": "resolved",
                         "confidence": 0.9, "omega0": 4.0, "interval_lo": 3.7,
                         "interval_hi": 4.3, "interval_reliable": True, "reasons": "resolved"})
        report = Path(ws) / "report"
        report.mkdir(parents=True, exist_ok=True)
        columns = ["scan", "detector", "h", "k", "l", "status", "verdict", "disposition",
                   "confidence", "omega0", "interval_lo", "interval_hi", "interval_reliable",
                   "reasons"]
        with open(report / "results.csv", "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            for row in rows:
                writer.writerow({c: row.get(c) for c in columns})

    return run_isar


@dataclass(frozen=True, slots=True)
class _StubVerdict:
    """The shape ``_run_discovery_step`` reads off an ``AgendaVerdict``, held at incomplete."""

    state: str = "incomplete"
    blocking: tuple = ()
    consultation_requests: tuple = ()


def _refuse_evaluate_stop(*args, **kwargs):
    """The four-reasons pin: discovery stop accounting is the AgendaVerdict's, and nobody else's."""
    raise AssertionError("the discovery path called evaluate_stop")


def config_for(root: Path, client=None, **extra) -> dict:
    return {"workspace": str(root), "client": client or MockTavi(),
            "run_isar": mock_runner(root), "sample": "tavi", "seed": 0, **extra}


# ---------------------------------------------------------------------------- (1) the seam
def test_the_seam_returns_a_step_result_a_verdict_and_a_beam_directive(tmp_path, monkeypatch):
    """D7: one function, one step, three values — and the verdict is NOT the directive."""
    root = workspace(tmp_path)
    step = discover_step()
    monkeypatch.setattr(runner_mod, "build_scene_revision",
                        FakeBuild(root, [BLANK_SCENE], fail_after=0))
    outcome = run_program(CampaignProgram(steps=(step,)), step.id, config_for(root))

    assert isinstance(outcome, ProgramStepOutcome)
    assert (outcome.result, outcome.verdict, outcome.directive) == tuple(outcome)
    assert isinstance(outcome.result, StepResult)
    assert outcome.result.step_id == step.id
    assert outcome.verdict == VERDICT_MACHINE_FAILURE      # the fixture failed the opening build
    assert outcome.directive == BEAM_RELEASE


def test_the_beam_directive_is_a_separate_axis_from_the_verdict(tmp_path, monkeypatch):
    """A step that ends unanswered still HOLDS the beam when a later step needs one — the
    structural separation D7 asks for. Collapsing the two would release the beam here."""
    root = workspace(tmp_path)
    first, second = discover_step("discover-1"), discover_step("discover-2")
    monkeypatch.setattr(runner_mod, "build_scene_revision", FakeBuild(root, [BLANK_SCENE]))
    outcome = run_program(CampaignProgram(steps=(first, second)), first.id,
                          config_for(root, max_deliveries=0))

    assert outcome.verdict != VERDICT_COMPLETE
    assert outcome.directive == BEAM_HOLD


# ---------------------------------------------------------------------------- (2) end to end
def test_a_question_driven_delivery_submits_a_provenance_stamped_grid_snapped_request(
        tmp_path, monkeypatch):
    """The canonical flow: declared partition -> plan -> selected -> submission.

    Asserts every link the plan names: the DecisionBasis carries the declared partition's full key
    components, the wire request carries the C1 campaign-provenance block, the energy step is the
    nice-number value at the instrument FWHM, and the exposure is the named discovery default.

    It also carries the four-reasons pin, because this is the test that exercises a real
    submission and delivery: ``evaluate_stop`` prices a TARGETED objective's per-q tolerance, and
    a discovery step has none, so a single call into it anywhere on this path would be inventing a
    stop reason. Pinning it on a world that never submits would leave the delivery half unpinned.
    """
    root = workspace(tmp_path)
    step = discover_step()
    program = CampaignProgram(steps=(step,))
    client = MockTavi()
    build = FakeBuild(root, [COLD_START_SCENE])
    monkeypatch.setattr(runner_mod, "build_scene_revision", build)
    monkeypatch.setattr(loop_mod, "evaluate_stop", _refuse_evaluate_stop)

    outcome = run_program(program, step.id, config_for(root, client=client, max_deliveries=1))

    assert client.bodies, "the discovery step submitted nothing"
    parameters = client.bodies[0]["parameters"]
    # FWHM 0.3 (the mock's bragg.dE) snaps to 0.25, and the grid is zero-anchored.
    _, lo, hi, snapped = parameters["scan_command1"].split()
    assert float(snapped) == 0.25
    assert abs(float(lo) / 0.25 - round(float(lo) / 0.25)) < 1e-9
    assert parameters["number_neutrons"] == DISCOVERY_NCOUNT_PROVISIONAL

    # The campaign-provenance block reached the durable submission record.
    run_events = log_payloads(root, "run")
    submitted = [p for p in run_events if p.get("job_id")]
    assert submitted, "no run event recorded"
    block = submitted[0]
    assert block["topology_role"] == "canonical_path"
    assert block["program_fingerprint"] == program_fingerprint(program)
    assert block["program_step"] == step.id
    assert block["path_fingerprint"]
    assert set(CAMPAIGN_PROVENANCE_FIELDS) <= set(block)

    # And the decision that chose it cites the declared partition by its full key components.
    selected = [d for d in decision_lines(root) if d["outcome"] == "selected"]
    assert selected, "no selected decision was persisted"
    declared = selected[0]["decision_basis"]["declared_partition"]
    assert declared["step_id"] == step.id
    assert declared["sample"] == "tavi"
    assert declared["conditions"] == [["temperature_K", 300.0]]
    assert declared["condition_t_round"] == 5.0
    assert declared["partition_id"] == outcome.result.declared_partition_id
    # Clock-free by contract: a timestamp here would fork same-seed replay bytes.
    assert not any("second" in key or "written_at" in key or "time" in key
                   for line in decision_lines(root) for key in line["decision_basis"])
    # The timings live in the session log instead.
    assert any(p.get("stage") == "scene_build" for p in log_payloads(root, "timing"))
    assert outcome.result.build_seconds


def test_the_next_plan_is_pinned_to_the_digest_the_build_returned(tmp_path, monkeypatch):
    """Steering consumes the JUST-BUILT revision, by content address — never ``current.json``."""
    root = workspace(tmp_path)
    step = discover_step()
    build = FakeBuild(root, [COLD_START_SCENE])
    monkeypatch.setattr(runner_mod, "build_scene_revision", build)

    outcome = run_program(CampaignProgram(steps=(step,)), step.id,
                          config_for(root, max_deliveries=1))

    cited = [d["decision_basis"]["scene_digest"] for d in decision_lines(root)]
    assert cited, "no decision recorded"
    assert set(cited) <= set(build.digests)
    assert outcome.result.scene_digests[0] == build.digests[0]


# ---------------------------------------------------------------------------- (3) blank world
def test_a_blank_world_terminates_honestly_and_never_asks_the_targeted_stop_evaluator(
        tmp_path, monkeypatch):
    """A world with no declared zone geometry still terminates with a real verdict and a reason,
    rather than looping or faulting. (The ``evaluate_stop`` pin lives on the delivery test above,
    where a submission actually happens — here nothing is ever submitted to price.)"""
    root = workspace(tmp_path)
    step = discover_step()
    monkeypatch.setattr(runner_mod, "build_scene_revision", FakeBuild(root, [BLANK_SCENE]))

    outcome = run_program(CampaignProgram(steps=(step,)), step.id,
                          config_for(root, max_deliveries=1))

    assert outcome.verdict in (VERDICT_COMPLETE, VERDICT_IN_PROGRESS, VERDICT_OVER_BUDGET)
    assert outcome.result.reason is not None
    assert outcome.directive == BEAM_RELEASE


# ---------------------------------------------------------------------------- (4) build failure
def test_a_failed_synchronous_build_is_a_controller_fault_and_never_steers_from_the_old_scene(
        tmp_path, monkeypatch):
    """The whole point of synchronous staleness: no decision may cite a scene older than the
    delivery that just landed. The fixture fails the build AFTER the first delivery; the campaign
    must stop as a machine failure, not re-plan against the previous digest."""
    root = workspace(tmp_path)
    step = discover_step()
    build = FakeBuild(root, [COLD_START_SCENE], fail_after=1)
    monkeypatch.setattr(runner_mod, "build_scene_revision", build)

    outcome = run_program(CampaignProgram(steps=(step,)), step.id, config_for(root))

    assert outcome.verdict == VERDICT_MACHINE_FAILURE
    assert "scene_build_failed" in outcome.result.reason
    # Exactly one scene was ever built, so exactly one digest may be cited — a re-plan on the
    # stale pin would show a SECOND decision citing build.digests[0] after the delivery.
    cited = [d["decision_basis"]["scene_digest"] for d in decision_lines(root)]
    assert cited == [build.digests[0]]
    assert outcome.result.deliveries == 1


def test_a_pointer_flip_lost_to_a_concurrent_publisher_is_not_a_fault(tmp_path, monkeypatch):
    """``ConcurrentSceneWrite``'s window: the blob is durable before the CAS check, so the child
    reports its computed digest with ``pointer_flipped=false`` and exits 0. The campaign steers
    from that digest and does not stop — making a read-only ``isar report --scene`` able to fault a
    live campaign is exactly the false machine failure the stop law forbids."""
    root = workspace(tmp_path)
    step = discover_step()
    build = FakeBuild(root, [COLD_START_SCENE], pointer_flipped=False)
    monkeypatch.setattr(runner_mod, "build_scene_revision", build)
    # A rival publisher owns the pointer: it names a revision this campaign never built.
    rival = write_scene(root, scene(sample_knowledge={"zone_period": 4.0}))

    outcome = run_program(CampaignProgram(steps=(step,)), step.id,
                          config_for(root, max_deliveries=1))

    assert outcome.verdict != VERDICT_MACHINE_FAILURE
    cited = {d["decision_basis"]["scene_digest"] for d in decision_lines(root)}
    assert rival not in cited, "the campaign steered from the rival publisher's pointer"
    assert cited <= set(build.digests)


# ---------------------------------------------------------------------------- (5) staleness
def test_the_synchronous_staleness_arm_proceeds(tmp_path, monkeypatch):
    root = workspace(tmp_path)
    step = discover_step()
    assert step.payload.resolved_policy["staleness_policy"] == {"mode": "synchronous", "lag": 0}
    monkeypatch.setattr(runner_mod, "build_scene_revision", FakeBuild(root, [COLD_START_SCENE]))

    outcome = run_program(CampaignProgram(steps=(step,)), step.id,
                          config_for(root, max_deliveries=1))
    assert outcome.verdict != VERDICT_MACHINE_FAILURE
    assert decision_lines(root), "the synchronous arm planned nothing"


def test_any_other_staleness_mode_refuses_loudly_at_step_start(tmp_path, monkeypatch):
    """Accepted-and-ignored is the failure this prevents: a fixed-lag program that quietly ran
    synchronously would have decisions its own contract says it could not have made."""
    root = workspace(tmp_path)
    step = discover_step()
    policy = dict(step.payload.resolved_policy)
    policy["staleness_policy"] = {"mode": "fixed_lag", "lag": 1}
    object.__setattr__(step.payload, "resolved_policy", policy)
    build = FakeBuild(root, [COLD_START_SCENE])
    monkeypatch.setattr(runner_mod, "build_scene_revision", build)

    with pytest.raises(StalenessModeRefused) as excinfo:
        run_program(CampaignProgram(steps=(step,)), step.id, config_for(root))
    assert "fixed_lag" in str(excinfo.value)
    assert build.calls == 0, "the refusal must precede any work"


def test_the_compiler_already_refuses_an_unsupported_staleness_mode(tmp_path):
    """The other half of the same refusal: a program declaring one cannot be frozen at all."""
    policy = dict(resolve_policy())
    policy["staleness_policy"] = {"mode": "batch_N", "lag": 4}
    with pytest.raises(ProgramError) as excinfo:
        discover_step(policy=policy)
    assert "batch_N" in str(excinfo.value)


# ---------------------------------------------------------------------------- (6) operator stop
def test_the_operator_stop_token_ends_the_step_before_it_plans(tmp_path, monkeypatch):
    root = workspace(tmp_path)
    step = discover_step()
    monkeypatch.setattr(runner_mod, "build_scene_revision", FakeBuild(root, [COLD_START_SCENE]))
    control = root / "report" / "control"
    control.mkdir(parents=True, exist_ok=True)
    (control / "stop-tok.json").write_text("{}", encoding="utf-8")

    outcome = run_program(CampaignProgram(steps=(step,)), step.id,
                          config_for(root, control_token="tok"))

    assert outcome.verdict == VERDICT_OPERATOR_STOPPED
    assert decision_lines(root) == []
    assert outcome.directive == BEAM_RELEASE


# ---------------------------------------------------------------------------- (7) budget
def test_the_delivery_ceiling_reports_over_budget_rather_than_measuring_forever(
        tmp_path, monkeypatch):
    root = workspace(tmp_path)
    step = discover_step()
    monkeypatch.setattr(runner_mod, "build_scene_revision", FakeBuild(root, [COLD_START_SCENE]))

    outcome = run_program(CampaignProgram(steps=(step,)), step.id,
                          config_for(root, max_deliveries=0))

    assert outcome.verdict == VERDICT_OVER_BUDGET
    assert "max_deliveries 0" in outcome.result.reason


def test_a_declared_budget_is_priced_by_the_agenda_verdict_not_by_a_second_accounting(
        tmp_path, monkeypatch):
    """The SAME ``budget_state`` the acquisition path charges is what ``evaluate_agenda`` prices —
    one accounting, not a second one bolted onto the discovery loop. A zero budget is spent before
    the first delivery, so the verdict trips at the plan, with nothing submitted."""
    root = workspace(tmp_path)
    step = discover_step()
    client = MockTavi()
    monkeypatch.setattr(runner_mod, "build_scene_revision", FakeBuild(root, [COLD_START_SCENE]))

    outcome = run_program(
        CampaignProgram(steps=(step,)), step.id,
        config_for(root, client=client, budget={"kind": "neutrons", "amount": 0.0},
                   max_deliveries=5))

    assert outcome.verdict == VERDICT_OVER_BUDGET
    assert outcome.result.reason == "budget_exhausted"
    assert outcome.result.deliveries == 0
    assert client.bodies == []


def test_a_budget_that_cannot_afford_the_minimum_batch_ends_the_step_over_budget(
        tmp_path, monkeypatch):
    """The acquisition path's own budget refusal reaches the seam as a BUDGET reason, never as an
    unhandled exception out of ``run_program``."""
    root = workspace(tmp_path)
    step = discover_step()
    monkeypatch.setattr(runner_mod, "build_scene_revision", FakeBuild(root, [COLD_START_SCENE]))

    outcome = run_program(
        CampaignProgram(steps=(step,)), step.id,
        config_for(root, budget={"kind": "neutrons", "amount": 1.0}, max_deliveries=5))

    assert outcome.verdict == VERDICT_OVER_BUDGET
    assert "budget_insufficient_for_minimum_batch" in outcome.result.reason


# ---------------------------------------------------------------------------- (8) declines
def test_a_declined_request_ends_the_step_honestly_rather_than_re_proposing_forever(
        tmp_path, monkeypatch):
    """C7 owns turning a geometry decline into durable blocker evidence and a re-plan against the
    same pinned scene. Until then the scene has not moved, so a re-plan would re-select the
    identical action — the step ends and says which request was refused."""
    root = workspace(tmp_path)
    step = discover_step()
    monkeypatch.setattr(runner_mod, "build_scene_revision", FakeBuild(root, [COLD_START_SCENE]))

    outcome = run_program(CampaignProgram(steps=(step,)), step.id,
                          config_for(root, client=DecliningTavi(), max_deliveries=5))

    assert outcome.verdict == VERDICT_IN_PROGRESS
    assert outcome.result.reason.startswith("request_declined:")
    # And the decline is ON the result, not only in the reason string: ``StepResult.declines`` is
    # what C7's durable-blocker producer reads, and the decline a step ENDS on is the one it needs.
    assert len(outcome.result.declines) == 1
    assert outcome.result.declines[0] in outcome.result.reason


# ---------------------------------------------------------------------------- (8b) store corruption
def test_an_unloadable_pinned_agenda_revision_is_a_machine_failure_naming_the_digest(
        tmp_path, monkeypatch):
    """The revision store is LOUD about a blob it cannot vouch for. That loudness has to land as a
    verdict with a reason, not as a bare exception the CLI renders as an exit 2 with no step
    result — a resume needs to know the step died on store corruption."""
    root = workspace(tmp_path)
    step = discover_step()
    monkeypatch.setattr(runner_mod, "build_scene_revision", FakeBuild(root, [COLD_START_SCENE]))

    real_plan = runner_mod.plan_next_action
    corrupted: list[str] = []

    def plan_then_corrupt(report_dir, *args, **kwargs):
        plan = real_plan(report_dir, *args, **kwargs)
        # Overwrite the agenda revision this decision just published, in place: the bytes no
        # longer hash to the name they are stored under.
        blob = Path(report_dir) / "agenda" / f"{plan.agenda_digest}.json"
        blob.write_text('{"tampered": true}', encoding="utf-8")
        corrupted.append(plan.agenda_digest)
        return plan

    monkeypatch.setattr(runner_mod, "plan_next_action", plan_then_corrupt)
    outcome = run_program(CampaignProgram(steps=(step,)), step.id,
                          config_for(root, max_deliveries=1))

    assert corrupted, "the fixture never reached a plan to corrupt"
    assert outcome.verdict == VERDICT_MACHINE_FAILURE
    assert outcome.result.reason.startswith("store_corrupt:")
    assert corrupted[0] in outcome.result.reason
    assert outcome.result.deliveries == 0


# ---------------------------------------------------------------------------- (8c) the prediction
def test_an_off_center_window_center_survives_persistence_and_aims_the_request(
        tmp_path, monkeypatch):
    """A clamped rival probe is ABOUT its predicted energy, not the midpoint of what survived the
    clamp. That centre has to reach the wire through the persisted inventory — the seam rebuilds
    the intent from disk, so a field the writer drops or the reader forgets re-aims both the
    resolution query and the request at the wrong energy."""
    from isar.drive.acquisition.inventory import (
        ActionEligibility, ActionEvaluation, ActionInventory, MeasurementIntent)
    from isar.drive.steering.planner import PlanResult, _write_inventory
    from isar.schema.action_identity import intent_from_mapping

    root = workspace(tmp_path)
    step = discover_step()
    # The window is clamped into the identity domain [0, 10]; the prediction sits well off its
    # midpoint (5.0), so "aimed at the midpoint" and "aimed at the prediction" cannot be confused.
    intent = MeasurementIntent(
        action_class="identity_discriminator", hkl=(2.5, 0.0, 0.0),
        energy_lo=0.0, energy_hi=10.0, window_family="two_branch",
        target_branch_role="acoustic", rival_branch_role="optic",
        evidence_basis_fingerprint="rev1", hypotheses_fingerprint=None,
        campaign_slice_fingerprint=None, window_center_meV=8.0)
    inventory = ActionInventory(
        evaluations=[ActionEvaluation(intent=intent, generator="g",
                                      eligibility=ActionEligibility.ELIGIBLE, policy_rank=0.0)],
        reports=[], inventory_version="isar.action-inventory/2",
        generator_registry_fingerprint="test", generator_registry_manifest=())

    # (a) the writer keeps it, and (c) the canonical constructor gives the same intent back.
    persisted = inventory.to_full_dict()["evaluations"][0]["intent"]
    assert persisted["window_center_meV"] == 8.0
    assert intent_from_mapping(persisted) == intent

    reference = _write_inventory(root / "report", inventory)
    fingerprint = intent.scientific_action_fingerprint

    class RecordingTavi(MockTavi):
        """Records the energy every resolution query was asked about."""

        def __init__(self):
            super().__init__()
            self.resolution_at: list[float] = []

        def get_resolution(self, h, k, l, deltaE):
            self.resolution_at.append(float(deltaE))
            return super().get_resolution(h, k, l, deltaE)

    client = RecordingTavi()
    monkeypatch.setattr(runner_mod, "build_scene_revision", FakeBuild(root, [COLD_START_SCENE]))
    monkeypatch.setattr(runner_mod, "plan_next_action", lambda *a, **kw: PlanResult(
        outcome="selected", step_id=step.id, scene_digest=None, basis_digest=None,
        agenda_digest=None, program_fingerprint=None, selection_fingerprint=fingerprint,
        inventory_ref=reference, run_records=()))
    # The agenda verdict has nothing to read here (the plan above wrote no agenda revision); this
    # test is about the intent round trip, so the verdict is held at "incomplete".
    monkeypatch.setattr(runner_mod, "_agenda_verdict", lambda *a, **kw: _StubVerdict())

    outcome = run_program(CampaignProgram(steps=(step,)), step.id,
                          config_for(root, client=client, max_deliveries=1))

    assert outcome.verdict != VERDICT_MACHINE_FAILURE, outcome.result.reason
    # (d) the instrument resolution was resolved AT the prediction, not at the window midpoint.
    assert client.resolution_at and client.resolution_at[0] == 8.0
    # ... and the request the ledger records is about that same energy.
    proposed = log_payloads(root, "propose")
    assert proposed and proposed[0]["e_pred"] == 8.0


# ---------------------------------------------------------------------------- (9) refusals
def test_a_targeted_step_is_refused_by_name_rather_than_adapted(tmp_path):
    program = CampaignProgram(steps=(CampaignStep(id="t1", kind="targeted", payload={}),))
    with pytest.raises(ProgramError) as excinfo:
        run_program(program, "t1", {"workspace": str(tmp_path)})
    assert "run_campaign" in str(excinfo.value)


def test_a_card_less_workspace_refuses_by_name(tmp_path):
    """Cold start is declared knowledge all the way down: with no card there is no sample, no
    condition setpoint, no binning policy and no epoch to mint a declared partition from."""
    from isar.drive.program_runner import WorkspaceUndeclared

    root = tmp_path / "bare"
    (root / "report").mkdir(parents=True)
    step = discover_step()
    with pytest.raises(WorkspaceUndeclared):
        run_program(CampaignProgram(steps=(step,)), step.id, config_for(root))

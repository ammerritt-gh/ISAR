"""The agenda READ surface (``isar.workspace_api.agenda_view``) over a stored revision (PR 4.2b
C8 / B7): absence graceful, corruption loud, and the four-comparand tri-state freshness verdict.

Every workspace here is built through real stores in ``tmp_path`` — the scene/agenda stores via
``isar.scene.*``, the program store + full decision pipeline via the real
``isar.drive.steering.planner.plan_next_action`` for the end-to-end "fresh" case (tests aren't
bound by ``workspace_api``'s own import ban, so they may reach for the real writers directly).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from isar.schema.campaign_program import CampaignProgram, CampaignStep, DiscoverAndMapSpec
from isar.schema.generators import DISCOVERY_GENERATOR_REGISTRY_VERSION
from isar.schema.question_policy import resolve_policy
from isar.scene.agenda import AGENDA_STORE, build_and_store_agenda
from isar.scene.model import canonical_bytes, digest_bytes
from isar.scene.store import SCENE_STORE

from isar.drive.steering.planner import PROGRAM_STORE, _ProgramRevision, plan_next_action
from isar.workspace_api import Workspace
from isar.workspace_api.agenda_view import _AGENDA_STORE, _PROGRAM_STORE


# ----------------------------------------------------------------------------
# Builders
# ----------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _SceneRevision:
    canonical: bytes
    digest: str

    @classmethod
    def of(cls, payload: dict) -> "_SceneRevision":
        blob = canonical_bytes(payload)
        return cls(canonical=blob, digest=digest_bytes(blob))


def scene(footprints=(), observations=(), sample_knowledge=None):
    return {"observations": list(observations), "footprints": list(footprints), "clusters": [],
            "non_scoring": [], "sample_knowledge": sample_knowledge or {},
            "condition_t_round": None}


def write_scene(report_dir: Path, payload: dict) -> None:
    SCENE_STORE.write_revision(report_dir, _SceneRevision.of(payload))


def discover_step(step_id="discover-1", q_start=(2.0, 0.0, 0.0), q_end=(3.0, 0.0, 0.0),
                  spacing=0.5, exposure_floor=1.0):
    policy = resolve_policy(user_ask={"min_effective_exposure": exposure_floor})
    step_payload = DiscoverAndMapSpec(
        q_start=q_start, q_end=q_end, maximum_initial_spacing_rlu=spacing,
        intrinsic_omega_domain_meV=(0.0, 10.0), catalogue_profile="isar.catalogue.default/1",
        resolved_policy=policy)
    return CampaignStep(id=step_id, kind="discover_and_map", payload=step_payload)


def program_with(step) -> CampaignProgram:
    return CampaignProgram(steps=(step,))


def write_program(report_dir: Path, program: CampaignProgram) -> None:
    PROGRAM_STORE.write_revision(report_dir, _ProgramRevision.of(program))


def write_agenda(report_dir: Path, scene_payload: dict, step, *, program_fp="prog-1",
                 registry_fp=DISCOVERY_GENERATOR_REGISTRY_VERSION, expected_parent=None):
    step_payload = step.payload.to_dict()
    return build_and_store_agenda(
        report_dir, scene_payload, step_payload, step_id=step.id,
        program_fingerprint=program_fp, generator_registry_fingerprint=registry_fp,
        expected_parent=expected_parent, source="test")


# ----------------------------------------------------------------------------
# Sanity: the re-instantiated store bindings mirror their drive/scene originals exactly
# ----------------------------------------------------------------------------
def test_local_store_bindings_match_the_drive_and_scene_originals():
    assert (_AGENDA_STORE.dir_name, _AGENDA_STORE.pointer_schema, _AGENDA_STORE.artifact_label) \
        == (AGENDA_STORE.dir_name, AGENDA_STORE.pointer_schema, AGENDA_STORE.artifact_label)
    assert (_PROGRAM_STORE.dir_name, _PROGRAM_STORE.pointer_schema,
           _PROGRAM_STORE.artifact_label) == (PROGRAM_STORE.dir_name, PROGRAM_STORE.pointer_schema,
                                              PROGRAM_STORE.artifact_label)


def test_agenda_schema_matches_the_real_module():
    from isar.scene.agenda import AGENDA_SCHEMA
    from isar.workspace_api.agenda_view import AGENDA_SCHEMA as LOCAL_SCHEMA
    assert LOCAL_SCHEMA == AGENDA_SCHEMA


# ----------------------------------------------------------------------------
# Absence and no-agenda
# ----------------------------------------------------------------------------
def test_no_agenda_at_all_is_none(tmp_path):
    ws = Workspace(tmp_path)
    assert ws.agenda is None


# ----------------------------------------------------------------------------
# End-to-end fresh path, through the real planner
# ----------------------------------------------------------------------------
def test_fresh_path_after_a_real_plan_next_action_run(tmp_path):
    step = discover_step()
    program = program_with(step)
    write_scene(tmp_path / "report", scene())
    result = plan_next_action(tmp_path / "report", program, "discover-1")

    ws = Workspace(tmp_path)
    assert ws.agenda is not None
    assert ws.agenda.digest == result.agenda_digest
    report = ws.agenda.freshness()
    assert report.verdict == "fresh"
    assert report.stale_reasons == ()
    assert report.unknown_reasons == ()


# ----------------------------------------------------------------------------
# Each comparand forced stale independently, from its OWN persisted source
# ----------------------------------------------------------------------------
def test_scene_alone_going_stale_never_reads_as_fresh(tmp_path):
    report_dir = tmp_path / "report"
    step = discover_step()
    program = program_with(step)
    write_scene(report_dir, scene())
    write_program(report_dir, program)
    write_agenda(report_dir, scene(), step, program_fp=_program_fp(program))

    # A NEW scene revision moves the scene store's pointer without touching the agenda.
    write_scene(report_dir, scene(footprints=[{"scan_ref": "e:1:d", "axis_coordinate": 2.1,
                                              "partition_id": "a" * 64, "effective_exposure": 5.0,
                                              "bands": []}]))
    ws = Workspace(tmp_path)
    report = ws.agenda.freshness()
    assert report.verdict == "stale"
    assert report.stale_reasons == ("scene",)


def _program_fp(program: CampaignProgram) -> str:
    from isar.schema.campaign_program import program_fingerprint
    return program_fingerprint(program)


def test_program_recompile_names_program_and_a_policy_change_names_both(tmp_path):
    report_dir = tmp_path / "report"
    baseline_step = discover_step(spacing=0.5)
    baseline_program = program_with(baseline_step)
    write_scene(report_dir, scene())
    write_program(report_dir, baseline_program)
    write_agenda(report_dir, scene(), baseline_step, program_fp=_program_fp(baseline_program))

    # A program-only change: different geometry, SAME resolved_policy content.
    geometry_step = discover_step(spacing=0.25)
    write_program(report_dir, program_with(geometry_step))
    ws = Workspace(tmp_path)
    ws.refresh()
    report = ws.agenda.freshness()
    assert report.verdict == "stale"
    assert report.stale_reasons == ("program",)

    # A policy-carrying change: resolved_policy is EMBEDDED in the program, so a threshold
    # change necessarily moves program_fingerprint too -- both are named, never policy alone.
    threshold_step = discover_step(spacing=0.5, exposure_floor=2.0)
    write_program(report_dir, program_with(threshold_step))
    ws.refresh()
    report2 = ws.agenda.freshness()
    assert report2.verdict == "stale"
    assert set(report2.stale_reasons) == {"program", "policy"}


def test_foreign_registry_fingerprint_names_registry(tmp_path):
    report_dir = tmp_path / "report"
    step = discover_step()
    program = program_with(step)
    write_scene(report_dir, scene())
    write_program(report_dir, program)
    write_agenda(report_dir, scene(), step, program_fp=_program_fp(program),
                registry_fp="isar.generator-registry/1:foreign000000")

    ws = Workspace(tmp_path)
    report = ws.agenda.freshness()
    assert report.verdict == "stale"
    assert report.stale_reasons == ("registry",)


# ----------------------------------------------------------------------------
# Unrecoverable comparands -> unknown, never fresh
# ----------------------------------------------------------------------------
def test_pre_program_workspace_with_matching_scene_is_unknown_never_fresh(tmp_path):
    report_dir = tmp_path / "report"
    step = discover_step()
    write_scene(report_dir, scene())
    # No program store written at all.
    write_agenda(report_dir, scene(), step, program_fp="whatever-unverifiable")

    ws = Workspace(tmp_path)
    report = ws.agenda.freshness()
    assert report.verdict == "unknown"
    assert report.stale_reasons == ()
    assert set(report.unknown_reasons) == {"program", "policy"}


def test_null_registry_fingerprint_is_unknown():
    from isar.workspace_api.agenda_view import compute_freshness
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        report_dir = Path(td) / "report"
        step = discover_step()
        program = program_with(step)
        write_scene(report_dir, scene())
        write_program(report_dir, program)
        write_agenda(report_dir, scene(), step, program_fp=_program_fp(program),
                    registry_fp=None)
        ws = Workspace(Path(td))
        report = ws.agenda.freshness()
        assert report.verdict == "unknown"
        assert report.unknown_reasons == ("registry",)


# ----------------------------------------------------------------------------
# Corruption is loud
# ----------------------------------------------------------------------------
def test_a_damaged_agenda_blob_raises(tmp_path):
    report_dir = tmp_path / "report"
    step = discover_step()
    write_scene(report_dir, scene())
    result = write_agenda(report_dir, scene(), step)
    blob_path = report_dir / "agenda" / f"{result.digest}.json"
    blob_path.write_text('{"schema": "isar.agenda-revision/1", "specs": "not-a-list"}',
                         encoding="utf-8")

    ws = Workspace(tmp_path)
    with pytest.raises(ValueError):
        _ = ws.agenda


def test_a_damaged_program_pointer_raises_from_freshness(tmp_path):
    report_dir = tmp_path / "report"
    step = discover_step()
    program = program_with(step)
    write_scene(report_dir, scene())
    write_program(report_dir, program)
    write_agenda(report_dir, scene(), step, program_fp=_program_fp(program))

    (report_dir / "program" / "current.json").write_text(
        json.dumps({"schema": "not-the-right-schema", "digest": "0" * 64, "parent": None,
                   "written_at": "x"}),
        encoding="utf-8")

    ws = Workspace(tmp_path)
    assert ws.agenda is not None                 # the AGENDA store itself is undamaged
    with pytest.raises(ValueError):
        ws.agenda.freshness()                     # but freshness must read the program store


# ----------------------------------------------------------------------------
# Transitions, in order
# ----------------------------------------------------------------------------
def test_transitions_reader_returns_recorded_transitions_in_order(tmp_path):
    report_dir = tmp_path / "report"
    step = discover_step(spacing=0.5)
    write_scene(report_dir, scene())
    write_agenda(report_dir, scene(), step)

    # A genuinely different scene re-derives a different agenda -> a second transition line.
    changed = scene(footprints=[{"scan_ref": "e:1:d", "axis_coordinate": 2.1,
                                 "partition_id": "a" * 64, "effective_exposure": 5.0,
                                 "bands": []}])
    write_scene(report_dir, changed)
    write_agenda(report_dir, changed, step,
                expected_parent=AGENDA_STORE.pointer_digest(report_dir))

    ws = Workspace(tmp_path)
    from isar.workspace_api.agenda_view import read_transitions
    from isar.scene.agenda import read_transitions as real_read_transitions
    recorded = read_transitions(ws)
    expected = real_read_transitions(report_dir)
    assert recorded == expected
    assert len(recorded) == 2                     # one per build_and_store_agenda call

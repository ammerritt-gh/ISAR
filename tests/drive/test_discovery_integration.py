"""The cold-start boundary, end to end through the REAL scene builder (review finding 2026-08-18).

Every other discovery world in this tree replaces the scene build with ``FakeBuild`` and hands the
planner scene payloads the test itself wrote. That is the right shape for testing the CONTROLLER —
it makes the world deterministic and cheap — but it means no test crosses the boundary where a
submitted request becomes a measurement-arena record and that record becomes a scene. Three of the
six identity defects the 4.3 review found lived exactly on that boundary and survived a full green
suite:

* the declaration minted its ``sample`` from the campaign config while the delivered arena took the
  card's, so the declared partition could never join its own measurement;
* a pooled master inherited no ``program_step``, so suppressing a member erased the proof that the
  step had measured and UN-RETIRED the declaration;
* the agenda fanned questions into partitions the step could not write into.

This world closes the gap. The scene build is the REAL
:func:`isar.session.scene_hook.build_and_store`, run in-process — the subprocess seam itself is
pinned by ``tests/scene/test_cli_scene_build.py``, and paying a process per delivery here would buy
nothing this file is about. The delivery stand-in writes a genuine
:class:`~isar.schema.measurement.MeasurementArena` whose campaign-provenance block is read back from
the acquisition record THE DRIVER persisted and whose sample comes from
``isar.session.driver._sample_of`` — never hand-assembled, because hand-assembling both sides is
precisely what let the original defect pass.
"""
from __future__ import annotations

import json
from pathlib import Path

import isar.drive.program_runner as runner_mod
from isar.drive.program_runner import SceneBuildReport, run_program
from isar.schema.campaign_program import CampaignProgram
from isar.schema.card import Card
from isar.schema.measurement import MeasurementArena, campaign_provenance
from isar.scene.build import _partition_key
from isar.scene.questions import step_observed
from isar.scene.store import SCENE_STORE
from isar.session.driver import _sample_of
from isar.workspace_api.scene import _synthetic_observation

from drive.test_program_runner import MockTavi, config_for, discover_step, workspace
from synthetic_ws import FULL_COLUMNS, write_results

T_ROUND = 5.0
SETPOINT = 300.0


def _candidate(label="phonon+bg"):
    """The CURRENT delivery contract's candidate row — component descriptors and mode-interval
    projections included, because the adapter refuses a stale one rather than degrading (H1)."""
    return {"label": label, "k": 5, "n_modes": 1, "log_evidence": -12.5,
            "evidence_method": "nested", "posterior_probability": 0.7, "usable": True,
            "identity": None, "variant": None, "omegas": [4.0],
            "components": [{"kind": "mode", "slot": 0}],
            "constraint_origin": "none", "mode_intervals": [None]}


class _Rec:
    """The one field ``_sample_of`` reads when the card names no sample. Deliberately NOT the
    card's name, so the assertion below fails if the arena ever stops preferring the card."""

    experiment_id = "experiment-id-that-is-not-the-sample-name"


def real_scene_build(root: Path):
    """``build_scene_revision``'s contract, satisfied by the real canonical build."""
    def build(workspace_dir, *, source: str = "campaign", timeout: float = 0.0):
        from isar.session import scene_hook
        result = scene_hook.build_and_store(workspace_dir, source=source)
        return SceneBuildReport(digest=result.digest, pointer_flipped=not result.unchanged,
                                unchanged=result.unchanged, seconds=0.0)
    return build


def delivering_runner(root: Path):
    """Write what a real delivery writes: a fitted ``results.csv`` row AND its arena record.

    The arena's campaign provenance is read off the acquisition record the DRIVER persisted for
    this submission — the same block ``isar.session.scan_delivery`` stamps — so the two sides of
    the partition key are produced independently here exactly as they are in production.
    """
    state = {"n": 0}

    def run_isar(ws, data_paths, **kw):
        ws = Path(ws)
        card = Card.load(ws / "card.policy.toml", ws / "card.state.json")
        acquisitions = list(getattr(card.state, "acquisition_provenance", None) or [])
        if not acquisitions:
            return
        state["n"] += 1
        n = state["n"]
        scan_ref = f"tavi:{n}:det"
        chash = f"ch-{n}"
        sample = _sample_of(_Rec(), card)
        rows = [{
            "scan": n, "detector": "det", "scan_id": scan_ref, "sample": sample, "T": SETPOINT,
            "h": 2.0 + 0.1 * n, "k": 0.0, "l": 0.0, "status": "fitted", "verdict": "resolved",
            "disposition": "resolved", "confidence": 0.9, "omega0": 4.0, "interval_lo": 3.7,
            "interval_hi": 4.3, "interval_reliable": True, "reasons": "resolved",
            "best_model": "phonon+bg", "gauss_fwhm": 1.2, "lorentz_fwhm": 0.0,
            "energy_lo": 0.0, "energy_hi": 10.0, "energy_bands": json.dumps([[0.0, 10.0]]),
            "effective_exposure": 1000.0, "evidence_sink": "canonical_master",
            "amplitudes": json.dumps({"bg.BG": 3.0, "phonon.A": 20.0}),
            "candidates": json.dumps([_candidate()]), "content_hash": chash,
        }]
        write_results(ws, rows, FULL_COLUMNS)
        record = MeasurementArena(
            scan_ref=scan_ref, scan_id=scan_ref, scan_number=n, detector="det", sample=sample,
            conditions=(("temperature_K", SETPOINT),), q=(2.0 + 0.1 * n, 0.0, 0.0),
            lattice_epoch=1, energy_window=(0.0, 10.0), energy_bands=((0.0, 10.0),),
            effective_exposure=1000.0, exposure_provenance=None, count_trust="trusted",
            reasons=(), written_at="2026-08-18T00:00:00+00:00", content_hash=chash,
            **campaign_provenance(acquisitions[-1]))
        with open(ws / "report" / "measurements.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record.to_dict(), sort_keys=True) + "\n")

    return run_isar


def _run(tmp_path, monkeypatch, *, max_deliveries=1):
    root = workspace(tmp_path)
    step = discover_step()
    monkeypatch.setattr(runner_mod, "build_scene_revision", real_scene_build(root))
    outcome = run_program(
        CampaignProgram(steps=(step,)), step.id,
        config_for(root, client=MockTavi(), max_deliveries=max_deliveries,
                   run_isar=delivering_runner(root)))
    return root, step, outcome


def _scene(root: Path) -> dict:
    current = SCENE_STORE.read_current(root / "report")
    assert current is not None, "no scene revision was built"
    return current[1]


def test_the_declared_partition_is_the_partition_the_first_measurement_lands_in(tmp_path,
                                                                                monkeypatch):
    """The claim ``isar.drive.steering.cold_start`` makes in its first paragraph — "a resolved
    declaration's first measurement lands on the identical key BY CONSTRUCTION" — asserted across
    the real boundary rather than between two hand-written copies of the same literals.

    It was FALSE on every real workspace until this review: the declaration read ``cfg.sample``,
    whose CLI default is a synthetic name no card is obliged to match, and its conditions came from
    a card key two of the four card templates do not carry.
    """
    root, _, outcome = _run(tmp_path, monkeypatch)

    assert outcome.result.deliveries == 1, outcome.result.reason
    scene = _scene(root)
    observations = scene.get("observations") or ()
    assert observations, "the delivery produced no scene observation"

    partitions = {_partition_key(_synthetic_observation(MeasurementArena.from_dict(record)),
                                 T_ROUND)
                  for record in _arena_records(root)}
    assert len(partitions) == 1
    from isar.scene.model import partition_id
    assert partition_id(partitions.pop()) == outcome.result.declared_partition_id, (
        "the first measurement landed in a different partition than the one the campaign "
        "declared it would — the cold-start declaration cannot join its own measurement")


def test_the_declaration_retires_once_its_own_step_has_really_measured(tmp_path, monkeypatch):
    """And the retirement test reads TRUE against a scene the real builder produced — the path and
    step on the arena are the ones the driver stamped, not ones the fixture chose."""
    root, step, outcome = _run(tmp_path, monkeypatch)
    scene = _scene(root)
    record = MeasurementArena.from_dict(_arena_records(root)[0])

    assert record.program_step == step.id
    assert record.topology_role == "canonical_path"
    assert step_observed(scene, step.id, record.path_fingerprint), (
        "a real trusted delivery from this step did not retire its own declaration")
    # ...and a namesake step on another path does not.
    assert not step_observed(scene, step.id, "some-other-path")


def _arena_records(root: Path) -> list[dict]:
    path = root / "report" / "measurements.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]

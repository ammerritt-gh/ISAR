"""P2-M (PR #32 response wave, W6): does a NEW scan_ref pooled over an underexposed member clear
the old footprint's ``VerifyCompleteness`` exposure blocker, through the REAL production path?

The opening book's ``underexposed_cell`` unit test closes the predicate by mutating the old
footprint in place — never proven against the real join. This module builds workspace-shaped
inputs (results.csv rows + measurements.jsonl, the shared ``tests/scene`` synthetic-workspace
fixture), runs them through the real adapter (``scene_observations``) and the real scorer
(``build_scene``), and evaluates ``completeness_relative`` against the resulting payload —
exactly the pipeline ``isar drive`` runs, never a hand-built payload.
"""
import json

from isar.scene.build import build_scene
from isar.scene.predicates import evaluate_predicate
from isar.scene.questions import PredicateDeclaration, QuestionSpec, QuestionSubject
from isar.schema.measurement import MeasurementArena
from isar.workspace_api.scene import scene_observations
from scene.test_workspace_api_scene import _candidate, _row, _ws

FLOOR = 500.0


def _completeness_spec(partition_id):
    return QuestionSpec(
        family="VerifyCompleteness", claim_kind="completeness_relative",
        subject=QuestionSubject(kind="campaign", partition_id=partition_id,
                                frame=(("catalogue_version", "v1"),)),
        predicate=PredicateDeclaration(kind="completeness_relative", version=1,
                                       parameters={"min_effective_exposure": FLOOR}))


def _member_rows():
    """Member A underexposed (below FLOOR), member B adequate; same (sample, T, epoch)."""
    cands = [_candidate("phonon+bg")]
    row_a = _row(1, candidates=cands, h=2.0, effective_exposure=0.1)
    row_b = _row(2, candidates=cands, h=2.1, scan_id="tavi:2:det", effective_exposure=1000.0)
    return row_a, row_b


def _payload(ws):
    return build_scene(scene_observations(ws)).payload


def test_pre_supersession_the_scene_carries_both_footprints_and_stays_open(tmp_path):
    row_a, row_b = _member_rows()
    ws = _ws(tmp_path / "pre", [row_a, row_b])
    payload = _payload(ws)

    refs = {fp["scan_ref"] for fp in payload["footprints"]}
    assert refs == {"tavi:1:det", "tavi:2:det"}
    partition_id = payload["footprints"][0]["partition_id"]
    assert all(fp["partition_id"] == partition_id for fp in payload["footprints"])

    assessment = evaluate_predicate(_completeness_spec(partition_id), payload)
    assert assessment.status == "open"
    assert [b.name for b in assessment.blockers] == ["detection_authority_unavailable"]
    # Attributed to the underexposed member A, not the adequate member B.
    assert "tavi:1:det" in assessment.blockers[0].detail
    assert "tavi:2:det" not in assessment.blockers[0].detail


def test_a_canonical_pooled_master_clears_the_exposure_blocker(tmp_path):
    row_a, row_b = _member_rows()
    master = _row(9_000_001, candidates=[_candidate("phonon+bg")],
                  scan_id="master:tavi_2.0,0,0_200", pooled=True, n_members=2,
                  members="tavi:1:det;tavi:2:det", effective_exposure=1000.0)
    ws = _ws(tmp_path / "post", [row_a, row_b, master])
    payload = _payload(ws)

    refs = {fp["scan_ref"] for fp in payload["footprints"]}
    assert refs == {"master:tavi_2.0,0,0_200:det"}, (
        "the members' old footprints must not survive supersession")
    obs_refs = {o["scan_ref"] for o in payload["observations"]}
    assert "tavi:1:det" not in obs_refs and "tavi:2:det" not in obs_refs
    non_scoring_refs = {n["scan_ref"] for n in payload["non_scoring"]}
    assert "tavi:1:det" not in non_scoring_refs and "tavi:2:det" not in non_scoring_refs

    master_fp = payload["footprints"][0]
    assert master_fp["effective_exposure"] >= FLOOR
    partition_id = master_fp["partition_id"]

    assessment = evaluate_predicate(_completeness_spec(partition_id), payload)
    assert assessment.status == "answered"
    assert dict(assessment.answer) == {"complete": True}


def test_mismatched_provenance_leaves_the_honest_failure_mode_honest(tmp_path):
    """Same master, but member B's measurement record disagrees on lattice_epoch: no
    supersession, both members' footprints (and the exposure blocker) persist, and the master
    scores nothing under the current frame."""
    row_a, row_b = _member_rows()
    master = _row(9_000_001, candidates=[_candidate("phonon+bg")],
                  scan_id="master:tavi_2.0,0,0_200", pooled=True, n_members=2,
                  members="tavi:1:det;tavi:2:det", effective_exposure=1000.0)
    root = tmp_path / "mismatch"
    ws = _ws(root, [row_a, row_b, master])

    # Overwrite the measurements log with mismatched lattice_epoch on member B — the shared
    # fixture helper always writes ``lattice_epoch=None`` for every row, so the mismatch this
    # arm needs is written directly here, against the real MeasurementArena schema.
    def record(row, epoch):
        return MeasurementArena(
            scan_ref=row["scan_id"], scan_id=row["scan_id"], scan_number=row["scan"],
            detector=row["detector"], sample=row["sample"],
            conditions=(("temperature_K", float(row["T"])),),
            q=(row["h"], row["k"], row["l"]), lattice_epoch=epoch,
            energy_window=(row["energy_lo"], row["energy_hi"]),
            energy_bands=((row["energy_lo"], row["energy_hi"]),),
            effective_exposure=row["effective_exposure"], exposure_provenance=None,
            count_trust="trusted", reasons=(),
            written_at="2026-01-01T00:00:00+00:00", content_hash=row["content_hash"])

    lines = [json.dumps(record(row_a, 0).to_dict(), sort_keys=True),
             json.dumps(record(row_b, 1).to_dict(), sort_keys=True)]
    (root / "report" / "measurements.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    ws.refresh()

    payload = _payload(ws)
    refs = {fp["scan_ref"] for fp in payload["footprints"]}
    assert refs == {"tavi:1:det", "tavi:2:det"}, "an incompatible master must not erase its members"

    partition_id = next(fp["partition_id"] for fp in payload["footprints"]
                        if fp["scan_ref"] == "tavi:1:det")
    assessment = evaluate_predicate(_completeness_spec(partition_id), payload)
    assert assessment.status == "open"
    assert [b.name for b in assessment.blockers] == ["detection_authority_unavailable"]
    assert "tavi:1:det" in assessment.blockers[0].detail

    # The master itself carries the candidate table but does not supersede/score under this frame.
    master_obs = next(o for o in payload["observations"] if o["scan_ref"].startswith("master:"))
    assert master_obs["count_trust"] == "excluded"

"""The scene READ surface (``isar.workspace_api.scene_view``) over a stored revision.

Every workspace here is stamped, built and stored through the real path — the adapter builds
observations off persisted rows, ``build_scene`` scores them, ``write_revision`` publishes them —
so what these tests read back is a revision nothing in the test wrote by hand.
"""
import json

import pytest

from isar.scene.build import build_scene
from isar.scene.store import write_revision
from isar.workspace_api import Workspace, hypothesis_map, scene_revision
from isar.workspace_api.scene import sample_knowledge, scene_observations
from scene.test_workspace_api_scene import COLUMNS, _candidate, _row, _selection, _ws

# A row the params-only evaluator can render: a calibration zero (so ``resolution_context`` exists)
# plus ω₀/Γ and stored amplitudes (so the pure path builds a manifest without any counts).
EVALUABLE = {"zero": 0.0, "omega0": 4.0, "gamma": 0.5}


def _stored(ws):
    """Build and publish the scene of ``ws`` exactly as the driver would."""
    revision = build_scene(scene_observations(ws), sample=sample_knowledge(ws))
    return write_revision(ws.root / "report", revision)


def _workspace(tmp_path, rows):
    ws = _ws(tmp_path, rows, COLUMNS)
    result = _stored(ws)
    return ws, result


# ---------------------------------------------------------------------------
# scene_revision: absence is silent, corruption is loud
# ---------------------------------------------------------------------------
def test_absent_scene_reads_as_none_without_a_warning(tmp_path):
    ws = _ws(tmp_path, [_row(1, candidates=[_candidate("phonon+bg")])])
    assert scene_revision(ws) is None
    assert ws.scene is None
    assert hypothesis_map(ws) is None
    assert ws.warnings == []


def test_stored_revision_round_trips_with_its_pointer(tmp_path):
    ws, result = _workspace(tmp_path, [_row(1, candidates=[_candidate("phonon+bg")], **EVALUABLE)])
    view = scene_revision(ws)

    assert view.digest == result.digest and view.parent is None
    assert view.written_at
    assert view.payload["schema"] == "isar.scene-revision/1"
    assert [o["scan_ref"] for o in view.observations] == ["tavi:1:det"]
    assert view.clusters and view.sample_knowledge["zone_period"] is None
    # ws.scene is the same read, cached on the workspace.
    assert ws.scene == view


def test_a_second_revision_records_the_first_as_its_parent(tmp_path):
    ws, first = _workspace(tmp_path, [_row(1, candidates=[_candidate("phonon+bg")])])
    second = _ws(tmp_path / "b", [_row(1, candidates=[_candidate("phonon+bg", log_evidence=-9.0)])])
    write_revision(ws.root / "report", build_scene(scene_observations(second)))

    ws.refresh()
    assert ws.scene.parent == first.digest


def test_a_corrupt_blob_raises_rather_than_reading_as_no_scene(tmp_path):
    ws, result = _workspace(tmp_path, [_row(1, candidates=[_candidate("phonon+bg")])])
    payload = json.loads(result.path.read_text(encoding="utf-8"))
    payload["catalogue_version"] = "tampered"
    result.path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="does not match its digest"):
        scene_revision(ws)
    with pytest.raises(ValueError):
        ws.scene
    with pytest.raises(ValueError):
        hypothesis_map(ws)


def test_a_missing_blob_under_a_live_pointer_raises(tmp_path):
    ws, result = _workspace(tmp_path, [_row(1, candidates=[_candidate("phonon+bg")])])
    result.path.unlink()
    with pytest.raises(ValueError, match="missing revision"):
        scene_revision(ws)


# ---------------------------------------------------------------------------
# hypothesis_map
# ---------------------------------------------------------------------------
def test_winner_curve_is_the_evaluator_on_the_same_grid(tmp_path):
    ws, _ = _workspace(tmp_path, [_row(1, candidates=[_candidate("phonon+bg")], **EVALUABLE)])
    entry, = hypothesis_map(ws, x_points=9).entries

    assert entry.scan_ref == "tavi:1:det" and entry.q == (2.0, 0.0, 0.0)
    assert entry.winner_label == "phonon+bg" and entry.delivered_label == "phonon+bg"
    assert entry.curve_absent_reason is None
    curve = entry.winner_curve
    assert curve.pure is True and len(curve.x) == 9
    # The window is (-5, 10): the grid spans it, negative ω included (the model renders both
    # sides itself — nothing here invents an anti-Stokes curve).
    assert curve.x[0] == -5.0 and curve.x[-1] == 10.0
    direct = ws.evaluate_fit(ws.results[0], x_eval=list(curve.x))
    assert curve.total == tuple(float(v) for v in direct.total)


def test_a_row_that_cannot_be_evaluated_records_why_and_no_curve(tmp_path):
    # No calibration zero -> no resolution context -> no pure path; no data/ file -> no fallback.
    ws, _ = _workspace(tmp_path, [_row(1, candidates=[_candidate("phonon+bg")])])
    entry, = hypothesis_map(ws).entries
    assert entry.winner_curve is None
    assert "evaluation refused" in entry.curve_absent_reason


def test_no_curve_when_the_scene_winner_is_not_the_delivered_model(tmp_path):
    cands = [_candidate("phonon+bg", log_evidence=-20.0),
             _candidate("2phonon+bg", n_modes=2, omegas=[4.0, 6.0], log_evidence=-10.0)]
    ws, _ = _workspace(tmp_path, [_row(1, candidates=cands, **EVALUABLE)])
    entry, = hypothesis_map(ws).entries

    assert entry.winner_label == "2phonon+bg" and entry.delivered_label == "phonon+bg"
    assert entry.winner_curve is None
    assert "not the delivered model" in entry.curve_absent_reason


def test_mode_markers_carry_every_alternative_at_its_recorded_mass(tmp_path):
    cands = [_candidate("phonon+bg", log_evidence=-12.5),
             _candidate("2phonon+bg", n_modes=2, omegas=[4.0, 6.0], log_evidence=-13.0)]
    ws, _ = _workspace(tmp_path, [_row(1, candidates=cands, **EVALUABLE)])
    entry, = hypothesis_map(ws).entries

    assert [(m.omega, m.label) for m in entry.mode_markers] == [
        (4.0, "2phonon+bg"), (4.0, "phonon+bg"), (6.0, "2phonon+bg")]
    weights = {m.label: m.weight for m in entry.mode_markers}
    marginal = scene_revision(ws).clusters[0]["marginals"]["label"]["tavi:1:det"]
    assert weights == pytest.approx(marginal)          # read off the revision, never recomputed
    assert sum(weights.values()) == pytest.approx(1.0)
    # The loser is present at its own mass, not dropped for being the loser.
    assert 0.0 < weights["2phonon+bg"] < weights["phonon+bg"]


def test_an_unplaceable_mode_contributes_no_marker(tmp_path):
    cands = [_candidate("2phonon+bg", n_modes=2, omegas=[4.0, None])]
    ws, _ = _workspace(tmp_path, [_row(1, candidates=cands, **EVALUABLE)])
    entry, = hypothesis_map(ws).entries
    assert [m.omega for m in entry.mode_markers] == [4.0]


def test_a_window_that_misses_the_elastic_line_leaves_a_gap(tmp_path):
    ws, _ = _workspace(tmp_path, [_row(1, candidates=[_candidate("phonon+bg")],
                                       energy_lo=2.0, energy_hi=10.0, **EVALUABLE)])
    gap, = hypothesis_map(ws).gaps
    assert (gap.scan_ref, gap.lo, gap.hi, gap.reason) == ("tavi:1:det", 0.0, 2.0, "uncovered")


def test_a_covered_window_leaves_no_gap(tmp_path):
    ws, _ = _workspace(tmp_path, [_row(1, candidates=[_candidate("phonon+bg")], **EVALUABLE)])
    assert hypothesis_map(ws).gaps == ()


def test_a_non_scoring_arena_is_a_whole_arena_gap_with_its_reason(tmp_path):
    rows = [_row(1, candidates=[_candidate("phonon+bg")], **EVALUABLE),
            _row(2, candidates=[_candidate("phonon+bg")], h=9.0,
                 fit_selection_semantics=_selection(method="none"), **EVALUABLE)]
    ws, _ = _workspace(tmp_path, rows)
    result = hypothesis_map(ws)

    assert [e.scan_ref for e in result.entries] == ["tavi:1:det"]
    assert [(g.scan_ref, g.lo, g.hi, g.reason) for g in result.gaps] == [
        ("tavi:2:det", None, None, "no_evidence")]


def test_visibility_flags_are_the_clusters_own_rows_for_that_arena(tmp_path):
    """Two adjacent arenas (Δh = 0.1 < Q_ADJACENCY) cluster, so the revision records neighbour
    reports for the leading alternative — and each entry gets its own arena's rows, verbatim."""
    cands = [_candidate("phonon+bg")]
    ws, _ = _workspace(tmp_path, [_row(1, candidates=cands, **EVALUABLE),
                                  _row(2, candidates=cands, h=2.1, **EVALUABLE)])
    first, second = hypothesis_map(ws).entries

    recorded = scene_revision(ws).clusters[0]["visibility"]
    assert recorded, "expected the cluster to record neighbour reports"
    for entry in (first, second):
        assert entry.visibility_flags
        assert all(row["scan_ref"] == entry.scan_ref for row in entry.visibility_flags)
    assert list(first.visibility_flags) + list(second.visibility_flags) == sorted(
        recorded, key=lambda r: r["scan_ref"])


def test_two_reads_of_one_revision_are_identical(tmp_path):
    cands = [_candidate("phonon+bg"), _candidate("2phonon+bg", n_modes=2, omegas=[4.0, 6.0])]
    ws, _ = _workspace(tmp_path, [_row(1, candidates=cands, **EVALUABLE),
                                  _row(2, candidates=cands, h=2.1, **EVALUABLE)])
    assert hypothesis_map(ws) == hypothesis_map(ws)


def test_x_points_below_two_is_refused(tmp_path):
    ws, _ = _workspace(tmp_path, [_row(1, candidates=[_candidate("phonon+bg")], **EVALUABLE)])
    with pytest.raises(ValueError, match="x_points"):
        hypothesis_map(ws, x_points=1)

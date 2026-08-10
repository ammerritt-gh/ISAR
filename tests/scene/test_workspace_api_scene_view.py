"""The scene READ surface (``isar.workspace_api.scene_view``) over a stored revision.

Every workspace here is stamped, built and stored through the real path — the adapter builds
observations off persisted rows, ``build_scene`` scores them, ``write_revision`` publishes them —
so what these tests read back is a revision nothing in the test wrote by hand.
"""
import json
from types import SimpleNamespace

import pytest

from isar.scene import build as scene_build
from isar.scene.build import SceneRevision, build_scene
from isar.scene.store import write_revision
from isar.session import scene_hook
from isar.workspace_api import Workspace, hypothesis_map, scene_revision
from isar.workspace_api.scene import sample_knowledge, scene_observations
from scene.test_workspace_api_scene import COLUMNS, _candidate, _row, _selection, _ws
from synthetic_ws import stamp_ws

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
    # The footprint is built from the KEPT RUNS, not the outer window, so the runs are what has
    # to miss the elastic line for the gap to be real.
    ws, _ = _workspace(tmp_path, [_row(1, candidates=[_candidate("phonon+bg")],
                                       energy_lo=2.0, energy_hi=10.0,
                                       energy_bands=json.dumps([[2.0, 10.0]]), **EVALUABLE)])
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
    """Two arenas at consecutive path ordinals cluster, so the revision records neighbour
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


# ---------------------------------------------------------------------------
# The interpreting reader gates the payload it interprets (review finding F)
# ---------------------------------------------------------------------------
def _publish(ws, payload):
    """Publish an arbitrary payload as the current revision. The store checks integrity, not
    meaning — which is exactly why the reader has to check meaning."""
    return write_revision(ws.root / "report", SceneRevision.of(payload))


@pytest.mark.parametrize("payload, message", [
    ({}, "declares schema None"),
    ({"schema": "isar.scene-revision/2", "observations": [], "clusters": [],
      "footprints": [], "non_scoring": []}, "declares schema 'isar.scene-revision/2'"),
    ({"schema": "isar.scene-revision/1"}, "carries no usable"),
    ({"schema": "isar.scene-revision/1", "observations": [], "clusters": [],
      "footprints": {}, "non_scoring": []}, "carries no usable footprints"),
])
def test_a_blob_this_reader_cannot_interpret_refuses_instead_of_reading_as_empty(
        tmp_path, payload, message):
    """"The scene claims nothing here" and "this reader cannot read the scene" mean opposite
    things to an operator, and a `{}` or a future revision must never quietly become the first."""
    ws, _ = _workspace(tmp_path, [_row(1, candidates=[_candidate("phonon+bg")], **EVALUABLE)])
    _publish(ws, payload)
    ws.refresh()
    with pytest.raises(ValueError, match=message):
        scene_revision(ws)
    with pytest.raises(ValueError, match=message):
        hypothesis_map(ws)


# ---------------------------------------------------------------------------
# The view is not mutable shared state (review finding G)
# ---------------------------------------------------------------------------
def test_mutating_a_returned_payload_changes_nothing(tmp_path):
    ws, result = _workspace(tmp_path, [_row(1, candidates=[_candidate("phonon+bg")], **EVALUABLE)])
    view = scene_revision(ws)

    payload = view.payload
    payload["clusters"] = "vandalised"
    payload["observations"].append({"scan_ref": "invented"})

    assert view.payload["clusters"] == scene_revision(ws).payload["clusters"]
    assert [o["scan_ref"] for o in view.observations] == ["tavi:1:det"]
    # ...and the content still matches the digest it is stored under.
    assert result.path.read_bytes() == view.canonical


# ---------------------------------------------------------------------------
# Source binding (review finding H)
# ---------------------------------------------------------------------------
def _restamp(tmp_path, rows):
    """Move the workspace on UNDER a stored revision, exactly as a later delivery would."""
    stamp_ws(tmp_path, rows=rows, columns=COLUMNS)
    return Workspace.open(tmp_path)


def test_a_refitted_row_makes_the_view_stale_and_withholds_its_curve(tmp_path):
    """The store can vouch that a blob matches its digest; only the fingerprints can say the
    revision is still about these rows. Without this, a scene frozen three deliveries ago renders
    as the current belief over current data."""
    ws, _ = _workspace(tmp_path, [_row(1, candidates=[_candidate("phonon+bg")], **EVALUABLE)])
    assert scene_revision(ws).stale is False
    assert hypothesis_map(ws).entries[0].winner_curve is not None

    moved = _restamp(tmp_path, [_row(1, candidates=[_candidate("phonon+bg", log_evidence=-3.0)],
                                     **EVALUABLE)])
    assert scene_revision(moved).stale is True
    entry, = hypothesis_map(moved).entries
    assert entry.winner_curve is None and entry.curve_absent_reason == "source_mismatch"
    assert hypothesis_map(moved).stale is True


def test_an_arena_the_revision_never_saw_makes_the_view_stale(tmp_path):
    ws, _ = _workspace(tmp_path, [_row(1, candidates=[_candidate("phonon+bg")], **EVALUABLE)])
    grown = _restamp(tmp_path, [_row(1, candidates=[_candidate("phonon+bg")], **EVALUABLE),
                                _row(2, candidates=[_candidate("phonon+bg")], h=2.1, **EVALUABLE)])
    assert scene_revision(grown).stale is True
    # The arena the revision DID see still fingerprints as its source, so its curve stands.
    entry, = hypothesis_map(grown).entries
    assert entry.scan_ref == "tavi:1:det" and entry.winner_curve is not None


def test_a_workspace_the_adapter_can_no_longer_read_is_stale_not_unchanged(tmp_path):
    ws, _ = _workspace(tmp_path, [_row(1, candidates=[_candidate("phonon+bg")], **EVALUABLE)])
    # A results.csv with no candidates column at all: the adapter refuses it outright.
    stamp_ws(tmp_path, rows=[_row(1, candidates=[_candidate("phonon+bg")], **EVALUABLE)],
             columns=[c for c in COLUMNS if c != "candidates"])
    broken = Workspace.open(tmp_path)

    assert scene_revision(broken).stale is True          # unconfirmable is not confirmed
    assert hypothesis_map(broken).entries[0].curve_absent_reason == "source_mismatch"


def test_a_curve_the_evaluator_would_reconstruct_from_counts_is_withheld(tmp_path, monkeypatch):
    """Hard rule 7: re-solving amplitudes from re-parsed counts is fitting, and a read surface
    never fits. The evaluator already flags which path it took — render only the pure one."""
    ws, _ = _workspace(tmp_path, [_row(1, candidates=[_candidate("phonon+bg")], **EVALUABLE)])
    pure = ws.evaluate_fit(ws.results[0], x_eval=[0.0, 1.0])
    monkeypatch.setattr(type(ws), "evaluate_fit",
                        lambda self, fit, **kw: SimpleNamespace(
                            x=pure.x, total=pure.total, components=pure.components, pure=False))

    entry, = hypothesis_map(ws).entries
    assert entry.winner_curve is None and entry.curve_absent_reason == "no_pure_model"


# ---------------------------------------------------------------------------
# A failed observe pass is visible (review finding I)
# ---------------------------------------------------------------------------
def test_a_failed_rebuild_leaves_the_old_pointer_a_stale_view_and_a_recorded_reason(
        tmp_path, monkeypatch):
    """The regression the finding names: delivery N publishes, delivery N+1's scene build fails,
    and the workspace must not present N's scene as the current explanation of N+1's rows."""
    ws, first = _workspace(tmp_path, [_row(1, candidates=[_candidate("phonon+bg")], **EVALUABLE)])
    assert scene_revision(ws).last_attempt is None       # nothing recorded by the direct store call

    moved = _restamp(tmp_path, [_row(1, candidates=[_candidate("phonon+bg", log_evidence=-3.0)],
                                     **EVALUABLE)])
    monkeypatch.setattr(scene_build, "build_scene", _boom)
    with pytest.raises(RuntimeError):
        scene_hook.build_and_store(moved.root)

    view = scene_revision(moved)
    assert view.digest == first.digest                   # the pointer never moved
    assert view.stale is True                            # ...and the rows did
    assert view.last_attempt["status"] == "failed"
    assert "scene scoring exploded" in view.last_attempt["reason"]


def _boom(*_a, **_kw):
    raise RuntimeError("scene scoring exploded")


def test_a_successful_hook_records_the_digest_it_published(tmp_path):
    ws = _ws(tmp_path, [_row(1, candidates=[_candidate("phonon+bg")], **EVALUABLE)])
    result = scene_hook.build_and_store(ws.root)
    view = scene_revision(Workspace.open(tmp_path))
    assert view.last_attempt["status"] == "ok"
    assert view.last_attempt["digest"] == result.digest == view.digest


# ---------------------------------------------------------------------------
# Cluster authority (review finding J)
# ---------------------------------------------------------------------------
def _stored_with(ws, **kwargs):
    write_revision(ws.root / "report", build_scene(scene_observations(ws), **kwargs))
    ws.refresh()
    return hypothesis_map(ws)


TWO_CANDIDATES = [_candidate("phonon+bg", log_evidence=-12.5),
                  _candidate("2phonon+bg", n_modes=2, omegas=[4.0, 6.0], log_evidence=-13.0)]


def test_an_exact_cluster_projects_as_an_ordinary_winner(tmp_path):
    ws = _ws(tmp_path, [_row(1, candidates=TWO_CANDIDATES, **EVALUABLE)])
    entry, = _stored_with(ws, factors=[]).entries
    assert entry.cluster_authority == "exact"
    assert entry.log_discarded_mass_bound is None and entry.conditional_on_retained_set is False
    assert entry.winner_curve is not None


def test_a_bounded_truncation_exposes_its_bound_and_keeps_its_curve(tmp_path):
    third = _candidate("bg", n_modes=0, omegas=[], components=[], log_evidence=-14.0)
    ws = _ws(tmp_path, [_row(1, candidates=[*TWO_CANDIDATES, third], **EVALUABLE)])
    entry, = _stored_with(ws, factors=[], alt_cap=2).entries
    assert entry.cluster_authority == "bounded"
    # One dropped alternative, lowest KEPT base -13.0 against a leader at -12.5, no scoring
    # factor: ln 1 + (-13.0 + 12.5).
    assert entry.log_discarded_mass_bound == pytest.approx(-0.5, rel=1e-12)
    assert entry.conditional_on_retained_set is False and entry.winner_curve is not None


def test_an_unresolved_cluster_withholds_its_curve_and_labels_its_masses(tmp_path):
    """An unbounded truncation leaves conditional masses over the retained set, not cluster
    posteriors. The numbers stay — they are what the scene computed — and say what they are."""
    ws = _ws(tmp_path, [_row(1, candidates=TWO_CANDIDATES, **EVALUABLE)])
    # The LIVE factor registry declares no score bound, so a cap makes the truncation unbounded.
    entry, = _stored_with(ws, alt_cap=1).entries

    assert entry.cluster_authority == "unresolved"
    assert entry.log_discarded_mass_bound is None
    assert entry.conditional_on_retained_set is True
    assert entry.winner_curve is None and entry.curve_absent_reason == "cluster_unresolved"
    assert entry.mode_markers, "the conditional masses are kept, only labelled"


def test_a_damaged_cluster_record_fails_safe_rather_than_reading_as_exact(tmp_path):
    """Truncated, no bound, neither unbounded flag — a combination the builder cannot emit. An
    entry that cannot prove it enumerated everything must not project as one that did."""
    ws = _ws(tmp_path, [_row(1, candidates=TWO_CANDIDATES, **EVALUABLE)])
    payload = build_scene(scene_observations(ws), factors=[]).payload
    payload["clusters"][0].update(truncated=True, log_discarded_mass_bound=None,
                                  truncation_unbounded=False, linkage_truncated=False)
    _publish(ws, payload)
    ws.refresh()

    entry, = hypothesis_map(ws).entries
    assert entry.cluster_authority == "unresolved"
    assert entry.winner_curve is None and entry.curve_absent_reason == "cluster_unresolved"


def test_a_linkage_truncated_cluster_is_unresolved_too(tmp_path, monkeypatch):
    """The other unresolved lane: dropped linkage readings that no base ordering bounds."""
    cands = [_candidate("2phonon+bg", n_modes=2, omegas=[4.0, 6.0],
                        components=[{"kind": "mode", "slot": 0}, {"kind": "mode", "slot": 1}])]
    ws = _ws(tmp_path, [_row(1, candidates=cands, **EVALUABLE),
                        _row(2, candidates=cands, h=2.1, **EVALUABLE)])
    monkeypatch.setattr(scene_build, "MAX_LINKAGE_VARIANTS", 1)
    entries = _stored_with(ws).entries

    assert {e.cluster_authority for e in entries} == {"unresolved"}
    assert all(e.curve_absent_reason == "cluster_unresolved" for e in entries)
    assert all(e.conditional_on_retained_set for e in entries)

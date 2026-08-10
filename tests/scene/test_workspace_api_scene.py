"""The scene adapter (``isar.workspace_api.scene``) over a stamped synthetic workspace."""
import json
import logging

import pytest

from isar.workspace_api import Workspace
from isar.workspace_api.scene import scene_observations
from synthetic_ws import FULL_COLUMNS, stamp_ws

# The adapter reads persisted columns the shared stamper's base header predates.
COLUMNS = FULL_COLUMNS + ["energy_lo", "energy_hi", "evidence_sink", "effective_exposure"]


def _candidate(label, **kw):
    row = {"label": label, "k": 5, "n_modes": 1, "log_evidence": -12.5,
           "evidence_method": "nested", "posterior_probability": 0.7, "usable": True,
           "identity": None, "variant": None, "omegas": [4.0]}
    row.update(kw)
    return row


def _row(scan, *, candidates, scan_id=None, **kw):
    row = {
        "scan": scan, "detector": "det", "scan_id": scan_id or f"tavi:{scan}:det",
        "sample": "tavi", "T": 200.0, "h": 2.0, "k": 0.0, "l": 0.0, "status": "fitted",
        "verdict": "resolved", "best_model": "phonon+bg", "gauss_fwhm": 1.2, "lorentz_fwhm": 0.0,
        "energy_lo": -5.0, "energy_hi": 10.0, "effective_exposure": 1000.0,
        "evidence_sink": "canonical_master",
        "amplitudes": json.dumps({"bg.BG": 3.0, "phonon.A": 20.0}),
        "candidates": candidates if isinstance(candidates, str) else json.dumps(candidates),
    }
    row.update(kw)
    return row


def _ws(root, rows):
    root.mkdir(parents=True, exist_ok=True)
    stamp_ws(root, rows=rows, columns=COLUMNS)
    return Workspace.open(root)


def test_row_becomes_an_observation_with_parsed_candidates(tmp_path):
    cands = [_candidate("phonon+bg"),
             _candidate("2phonon+bg", n_modes=2, omegas=[4.0, None]),
             _candidate("bg", n_modes=0, omegas=[], usable=False, posterior_probability=None)]
    obs, = scene_observations(_ws(tmp_path, [_row(1, candidates=cands)]))

    assert obs.scan_ref == "tavi:1:det"
    assert obs.q == (2.0, 0.0, 0.0)
    assert obs.temperature_K == 200.0
    assert obs.energy_window == (-5.0, 10.0)
    assert (obs.gauss_fwhm, obs.lorentz_fwhm) == (1.2, 0.0)
    assert obs.effective_exposure == 1000.0
    assert obs.background == 3.0
    assert obs.best_label == "phonon+bg"
    assert len(obs.fingerprint) == 64
    # sorted by label, not stamped order
    assert [c.label for c in obs.candidates] == ["2phonon+bg", "bg", "phonon+bg"]
    by_label = {c.label: c for c in obs.candidates}
    assert by_label["2phonon+bg"].omegas == (4.0, None)     # None slot survives the round trip
    assert by_label["bg"].omegas == ()                      # [] null stays empty, not (None,)
    assert by_label["bg"].usable is False                   # unusable rows are kept as evidence


def test_unplaceable_window_and_missing_q_degrade_to_none(tmp_path):
    ws = _ws(tmp_path, [_row(1, candidates=[_candidate("phonon+bg")],
                             energy_lo=None, energy_hi=10.0, h=None, k=None, l=None)])
    obs, = scene_observations(ws)
    assert obs.energy_window is None and obs.q is None


def test_deterministic_across_calls_and_candidate_permutation(tmp_path):
    cands = [_candidate("phonon+bg"), _candidate("bg", n_modes=0, omegas=[])]
    first = scene_observations(_ws(tmp_path, [_row(1, candidates=cands)]))
    assert [o.to_dict() for o in first] == [
        o.to_dict() for o in scene_observations(_ws(tmp_path, [_row(1, candidates=cands)]))]

    permuted = scene_observations(_ws(tmp_path / "b", [_row(1, candidates=list(reversed(cands)))]))
    assert [o.fingerprint for o in permuted] == [o.fingerprint for o in first]


def test_master_supersedes_its_member_scans(tmp_path):
    cands = [_candidate("phonon+bg")]
    master = _row(9_000_001, candidates=cands, scan_id="master:tavi_2.0,0,0_300",
                  pooled=True, n_members=2, members="1;2")
    refs = [o.scan_ref for o in scene_observations(
        _ws(tmp_path, [_row(1, candidates=cands), _row(2, candidates=cands),
                       _row(3, candidates=cands), master]))]
    # Members 1 and 2 fitted the same neutrons the master pooled; scan 3 is its own arena.
    assert refs == ["master:tavi_2.0,0,0_300:det", "tavi:3:det"]


def test_bare_member_label_does_not_suppress_another_detector(tmp_path):
    """A master pooled ONE detector's group; a bare member label carries no detector, so it must
    not take out the same scan number's arena on a detector the master never pooled."""
    cands = [_candidate("phonon+bg")]
    master = _row(9_000_001, candidates=cands, scan_id="master:tavi_2.0,0,0_300",
                  detector="det1", pooled=True, n_members=1, members="1")
    refs = [o.scan_ref for o in scene_observations(_ws(tmp_path, [
        _row(1, candidates=cands, detector="det1", scan_id="tavi:1:det1"),
        _row(1, candidates=cands, detector="det2", scan_id="tavi:1:det2"),
        master]))]
    assert refs == ["master:tavi_2.0,0,0_300:det1", "tavi:1:det2"]


def test_candidateless_master_supersedes_nothing(tmp_path):
    cands = [_candidate("phonon+bg")]
    master = _row(9_000_001, candidates=None, scan_id="master:tavi_2.0,0,0_300",
                  pooled=True, n_members=1, members="1")
    refs = [o.scan_ref for o in scene_observations(
        _ws(tmp_path, [_row(1, candidates=cands), master]))]
    assert refs == ["tavi:1:det"]


def test_non_canonical_sink_keeps_the_arena_but_scores_nothing(tmp_path):
    ws = _ws(tmp_path, [_row(1, candidates=[_candidate("phonon+bg")],
                             evidence_sink="auxiliary_track_node")])
    obs, = scene_observations(ws)
    assert obs.candidates == ()
    assert obs.energy_window == (-5.0, 10.0)      # still footprint-bearing


@pytest.mark.parametrize("cell", ["{not json", json.dumps([{"k": 5}])])
def test_malformed_candidates_yield_no_observation_and_log(tmp_path, caplog, cell):
    with caplog.at_level(logging.WARNING, logger="isar.workspace_api.scene"):
        assert scene_observations(_ws(tmp_path, [_row(1, candidates=cell)])) == []
    assert any("tavi:1:det" in r.getMessage() for r in caplog.records)


def test_rows_that_never_had_a_fit_are_silent(tmp_path, caplog):
    """A real campaign carries many held/context/excluded ledger rows (written detectorless).
    They yield no observation, and must not drown the malformed-cell warning in noise."""
    held = {"scan": 7, "scan_id": "tavi:7:", "status": "held_for_calibration",
            "reasons": "no energy zero"}
    with caplog.at_level(logging.DEBUG, logger="isar.workspace_api.scene"):
        assert scene_observations(_ws(tmp_path, [held])) == []
    assert [r.levelname for r in caplog.records] == ["DEBUG"]

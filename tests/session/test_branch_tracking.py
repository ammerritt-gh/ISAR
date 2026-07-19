from __future__ import annotations

import math
from types import SimpleNamespace

from isar.schema.card import CardState
from isar.session.branch_tracking import anchor_index, track_path
from isar.session.driver import (
    _apply_tracked_branch,
    _clear_tracked_selection,
    _mode_candidates,
    _select_mode,
)
from isar.session.state import ScanEntry, SessionState


HYPOTHESES = [
    {"name": "optic", "zc_energy": {"mean": 6.0, "sigma": 1.0},
     "slope_zc": {"mean": 0.0, "sigma": 8.0}},
    {"name": "acoustic", "zc_energy": {"mean": 0.0, "sigma": 0.2},
     "slope_zc": {"mean": 12.0, "sigma": 12.0}},
]


def _candidate(ref, energy, error=0.12):
    return {"candidate_ref": ref, "energy_meV": energy, "omega0_err": error,
            "sigma_meV": error, "gamma": 0.4, "amplitude": 1.0, "reliable": True}


def test_anchor_is_nearest_reduced_zone_centre_with_path_order_tie_break():
    path = [[3.0, 0, 0], [2.0, 0, 0], [4.0, 0, 0]]
    assert anchor_index(path, 2.0) == 1


def test_broad_zc_prior_confirms_peak_at_its_mean_without_phantom_new_branch():
    hypotheses = [
        {"name": "optic", "zc_energy": {"mean": 6.0, "sigma": 3.0},
         "slope_zc": {"mean": 0.0, "sigma": 8.0}},
        HYPOTHESES[1],
    ]
    candidate = _candidate("zc:optic", 5.956, error=0.031)
    candidate["reliable"] = False  # interval certification is deliberately separate from identity
    track = track_path([[2.0, 0.0, 0.0]], [{"candidates": [candidate]}], period=2.0,
                       hypotheses=hypotheses, assignment_entropy_stop_nats=0.2)
    assignment = track["assignments"][0]
    assert assignment["track_state"] == "confirmed"
    assert assignment["target_probability"] > 0.95


def test_two_separated_modes_have_clear_conditional_identity_at_adjacent_q():
    hypotheses = [
        {"name": "optic", "zc_energy": {"mean": 6.0, "sigma": 3.0},
         "slope_zc": {"mean": 0.0, "sigma": 8.0}},
        HYPOTHESES[1],
    ]
    observations = [
        {"candidates": [_candidate("zc:o", 5.896, error=0.03)]},
        {"candidates": [_candidate("q1:a", 0.463, error=0.011),
                         _candidate("q1:o", 6.200, error=0.047)]},
        {"candidates": [_candidate("q2:a", 0.946, error=0.144)]},
    ]
    for row in observations:
        for candidate in row["candidates"]:
            candidate["reliable"] = False
    track = track_path([[2.0, 0.0, 0.0], [2.05, 0.0, 0.0], [2.1, 0.0, 0.0]],
                       observations, period=2.0, hypotheses=hypotheses,
                       assignment_entropy_stop_nats=0.2)
    adjacent = track["assignments"][1]
    assert adjacent["target_probability"] >= 0.85
    assert adjacent["identity_entropy"] <= 0.2
    assert adjacent["track_state"] == "confirmed"


def test_joint_tracker_keeps_optic_when_acoustic_is_closer_to_six_mev():
    path = [[2.0 + 0.05 * index, 0.0, 0.0] for index in range(21)]
    observations = []
    for index in range(len(path)):
        r = 0.05 * index
        acoustic = min(6.05 * r, 6.05)
        optic = 6.0 + 2.1 * r
        observations.append({"candidates": [
            _candidate(f"q{index}:lower", acoustic), _candidate(f"q{index}:upper", optic)]})
    # The upper OP1-like candidate is farther from 6 meV at high q, but continuous with the ZC optic.
    observations[13]["candidates"] = [
        _candidate("h265:acoustic", 5.055), _candidate("h265:optic", 7.774)]
    observations[15]["candidates"] = [
        _candidate("h275:acoustic", 5.571), _candidate("h275:optic", 7.919)]
    observations[18]["candidates"] = [
        _candidate("h290:acoustic", 5.856), _candidate("h290:optic", 7.930)]
    observations[20]["candidates"] = [
        _candidate("h300:acoustic", 6.027), _candidate("h300:optic", 8.117)]

    track = track_path(path, observations, period=2.0, hypotheses=HYPOTHESES,
                       assignment_entropy_stop_nats=0.7)

    expected = {13: 7.774, 15: 7.919, 18: 7.930, 20: 8.117}
    for index, energy in expected.items():
        row = track["assignments"][index]
        candidate = row["candidates"][row["selected_candidate_index"]]
        assert math.isclose(candidate["energy_meV"], energy)
        assert row["optic_candidate_index"] != row["acoustic_candidate_index"]


def test_missing_q_stops_confirmed_segments_and_allows_only_one_bridge():
    path = [[2.0 + 0.05 * index, 0.0, 0.0] for index in range(8)]
    observations = []
    for index in range(len(path)):
        candidates = [] if index == 3 else [
            _candidate(f"q{index}:a", 0.6 * index),
            _candidate(f"q{index}:o", 6.0 + 0.1 * index)]
        observations.append({"candidates": candidates})

    track = track_path(path, observations, period=2.0, hypotheses=HYPOTHESES,
                       assignment_entropy_stop_nats=1.0, maximum_bridge_steps=1)

    states = [row["track_state"] for row in track["assignments"]]
    assert states[3] == "missing"
    assert states[4] == "provisional"
    assert all(state == "blocked" for state in states[5:])
    optic = next(item for item in track["tracks"] if item["branch_id"] == "optic")
    assert not any(len(segment["points"]) > 1 and
                   segment["points"][0]["hkl"][0] < 2.15 < segment["points"][-1]["hkl"][0]
                   for segment in optic["segments"])


def test_new_neighboring_evidence_can_reassign_an_earlier_candidate():
    path = [[2.0, 0, 0], [2.1, 0, 0], [2.2, 0, 0]]
    first = [
        {"candidates": [_candidate("a0", 0.1), _candidate("o0", 6.0)]},
        {"candidates": [_candidate("x1", 4.2), _candidate("y1", 6.4)]},
        {"candidates": []},
    ]
    before = track_path(path, first, period=2.0, hypotheses=HYPOTHESES,
                        assignment_entropy_stop_nats=1.0)
    second = [first[0], first[1],
              {"candidates": [_candidate("a2", 2.3), _candidate("o2", 6.7)]}]
    after = track_path(path, second, period=2.0, hypotheses=HYPOTHESES, previous=before,
                       assignment_entropy_stop_nats=1.0)
    assert after["revision"] == before["revision"] + 1
    assert all(row["optic_candidate_index"] != row["acoustic_candidate_index"]
               for row in after["assignments"] if row["optic_candidate_index"] is not None
               and row["acoustic_candidate_index"] is not None)
    assert isinstance(after["reassignments"], list)


def test_selecting_second_mode_uses_its_gamma_and_error_not_primary_interval():
    outcome = {
        "omega0": 5.0, "omega0_err": 0.1, "gamma": 0.3,
        "interval": [4.8, 5.2], "omega0_profile": [4.7, 5.3],
        "hesse_interval": [4.9, 5.1], "bayes_interval": [4.8, 5.0, 5.2],
        "interval_reliable": True, "pooled": True, "n_members": 8,
        "fit_kind": "unconstrained", "best_label": "stokes2+bg", "verdict": "resolved",
        "modality": "fold", "modes": [
            {"omega0": 5.0, "omega0_err": 0.1, "gamma": 0.3, "gamma_err": 0.04,
             "amplitude": 10.0},
            {"omega0": 7.8, "omega0_err": 0.22, "gamma": 0.9, "gamma_err": 0.08,
             "amplitude": 6.0},
        ],
    }
    candidates = _mode_candidates("master:q", "det0", outcome)
    outcome["branch_candidates"] = candidates
    assignment = {"track_state": "confirmed", "assignment_entropy": 0.01}
    candidates[1]["probabilities"] = {"optic": 0.98, "acoustic": 0.01, "new": 0.01}

    _select_mode(outcome, candidates[1], 1, assignment, revision=3)

    assert outcome["omega0"] == 7.8
    assert outcome["omega0_err"] == 0.22
    assert outcome["gamma"] == 0.9
    assert outcome["interval"] is None
    assert outcome["omega0_profile"] is None
    assert outcome["bayes_interval"] is None
    assert outcome["hesse_interval"] == [7.58, 8.02]

    carried = _mode_candidates("master:q", "det0", outcome)
    assert carried[0]["interval"] == [4.8, 5.2]
    assert carried[0]["profile_interval"] == [4.7, 5.3]
    assert carried[0]["bayes_interval"] == [4.8, 5.0, 5.2]
    assert carried[1]["interval"] is None


def test_missing_revision_clears_stale_scalar_selection_but_preserves_candidates():
    outcome = {
        "omega0": 7.8, "omega0_err": 0.2, "gamma": 0.8,
        "interval": [7.5, 8.1], "omega0_profile": [7.4, 8.2],
        "hesse_interval": [7.6, 8.0], "bayes_interval": [7.5, 7.8, 8.1],
        "interval_reliable": True, "branch_id": "optic", "selected_candidate_index": 1,
        "branch_candidates": [_candidate("a", 5.0), _candidate("o", 7.8)],
    }
    _clear_tracked_selection(
        outcome, {"track_state": "missing", "assignment_entropy": math.inf}, revision=4)
    assert outcome["track_state"] == "missing" and outcome["track_revision"] == 4
    assert outcome["selected_candidate_index"] is None and outcome["branch_id"] is None
    assert outcome["omega0"] is None and outcome["interval"] is None
    assert len(outcome["branch_candidates"]) == 2


def test_session_wiring_recomputes_after_masters_and_rebuilds_connected_prior():
    path = [[2.0, 0.0, 0.0], [2.1, 0.0, 0.0]]
    state = CardState(
        campaign_target={"branch": "optic", "label": "optic", "selector": "tracked_branch",
                         "target_energy_meV": None, "energy_search_meV": [-2.0, 12.0],
                         "path": path},
        hypothesis_snapshot={"hypotheses": HYPOTHESES, "target_hypothesis": "optic",
                             "steering": {"assignment_entropy_stop_nats": 1.0,
                                          "maximum_bridge_steps": 1}})
    card = SimpleNamespace(state=state, policy=SimpleNamespace(
        dispersion={"zone_period": 2.0}, collation={"t_round": 5.0}))
    session = SessionState()
    for index, (acoustic, optic) in enumerate(((0.1, 6.0), (1.2, 6.35))):
        outcome = {
            "detector_id": "det0", "q": path[index], "T": 200.0, "sample": "sample",
            "omega0": acoustic, "omega0_err": 0.1, "gamma": 0.2, "interval": [acoustic - .2, acoustic + .2],
            "omega0_profile": None, "hesse_interval": [acoustic - .1, acoustic + .1],
            "bayes_interval": None, "interval_reliable": True, "pooled": True, "n_members": 2,
            "fit_kind": "unconstrained", "best_label": "stokes2+bg", "verdict": "resolved",
            "modality": "fold", "modes": [
                {"omega0": acoustic, "omega0_err": 0.1, "gamma": 0.2,
                 "gamma_err": 0.03, "amplitude": 5.0},
                {"omega0": optic, "omega0_err": 0.12, "gamma": 0.7,
                 "gamma_err": 0.05, "amplitude": 4.0},
            ],
        }
        entry = ScanEntry(scan_id=f"master:q{index}", scan_number=9_000_000 + index,
                          scan_type="energy", status="fitted", detectors={"det0": outcome})
        session.put(entry)

    track = _apply_tracked_branch(session, card, t_round=5.0)

    assert track is state.branch_tracks
    assert session.scans["master:q1"].detectors["det0"]["omega0"] == 6.35
    assert session.scans["master:q1"].detectors["det0"]["gamma"] == 0.7
    assert all(point["branch_id"] in {"optic", "acoustic"} for point in state.dispersion_prior)
    assert {point["branch_id"] for point in state.dispersion_prior} == {"optic", "acoustic"}

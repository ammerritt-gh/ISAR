from __future__ import annotations

import math
from types import SimpleNamespace

from isar.schema.card import CardState
from isar.session.branch_tracking import anchor_index, track_path
from isar.session.driver import (
    _apply_tracked_branch,
    _clear_tracked_selection,
    _materialize_branch_candidates,
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


def test_unlabeled_local_lowest_candidate_does_not_define_acoustic_anchor():
    path = [[2.0, 0, 0], [2.05, 0, 0], [2.1, 0, 0], [2.15, 0, 0]]
    observations = [
        {"gauss_fwhm": 0.8, "candidates": [_candidate("a0", 0.1)]},
        {"gauss_fwhm": 0.8, "candidates": [_candidate("a1", 0.6)]},
        {"gauss_fwhm": 0.8, "candidates": [_candidate("a2", 1.1)]},
        {"gauss_fwhm": 0.8, "candidates": [_candidate("a3", 1.7)]},
    ]
    assert anchor_index(path, 2.0, target_branch="acoustic", observations=observations) == 0


def test_first_acoustic_anchor_uses_only_observed_hypothesis_resolvable_seed():
    path = [[2.0, 0, 0], [2.05, 0, 0], [2.1, 0, 0], [2.15, 0, 0]]
    observations = [{"candidates": []}, {"candidates": []}, {"candidates": []},
                    {"gauss_fwhm": 0.8, "candidates": [_candidate("seed", 1.4),
                                                          _candidate("optic", 6.4)]}]
    assert anchor_index(
        path, 2.0, target_branch="acoustic", observations=observations,
        acoustic_hypothesis=HYPOTHESES[1]) == 3


def test_persisted_acoustic_anchor_does_not_drift_to_new_nearer_q_candidate():
    path = [[2.0, 0, 0], [2.05, 0, 0], [2.1, 0, 0], [2.15, 0, 0]]
    first_observations = [{"candidates": []}, {"candidates": []}, {"candidates": []},
                          {"gauss_fwhm": 0.8,
                           "candidates": [_candidate("seed:a", 1.4),
                                          _candidate("seed:o", 6.4)]}]
    before = track_path(path, first_observations, period=2.0, hypotheses=HYPOTHESES,
                        target_branch="acoustic", assignment_entropy_stop_nats=1.0)
    assert before["anchor_index"] == 3
    assert before["acquisition_seed_index"] == 3
    assert before["reduced_q_origin_index"] == 0

    after = track_path(
        path,
        [{"gauss_fwhm": 0.8, "candidates": [_candidate("zc:optic", 5.98)]},
         {"candidates": []},
         {"gauss_fwhm": 0.8, "candidates": [_candidate("q10:a", 0.95),
                                               _candidate("q10:o", 6.2)]},
         first_observations[3]],
        period=2.0, hypotheses=HYPOTHESES, target_branch="acoustic", previous=before,
        assignment_entropy_stop_nats=1.0)
    assert after["anchor_index"] == 3
    zc = after["assignments"][0]
    assert zc["track_state"] != "confirmed"


def test_acoustic_transition_geometry_is_measured_from_physical_zone_centre():
    path = [[2.0, 0, 0], [2.05, 0, 0], [2.1, 0, 0], [2.15, 0, 0]]
    observations = [
        {"gauss_fwhm": 0.8, "candidates": [_candidate("zc:o", 6.0)]},
        {"gauss_fwhm": 0.8, "candidates": [_candidate("q05:a", 0.5),
                                             _candidate("q05:o", 6.1)]},
        {"gauss_fwhm": 0.8, "candidates": [_candidate("q10:a", 1.0),
                                             _candidate("q10:o", 6.2)]},
        {"gauss_fwhm": 0.8, "candidates": [_candidate("q15:a", 1.5),
                                             _candidate("q15:o", 6.3)]},
    ]
    track = track_path(path, observations, period=2.0, hypotheses=HYPOTHESES,
                       target_branch="acoustic", previous={"target_branch": "acoustic",
                                                            "anchor_index": 3})
    assert [row["acoustic_candidate_index"] for row in track["assignments"]] == [None, 0, 0, 0]


def test_acoustic_transition_geometry_has_physical_sign_on_both_sides_of_zone_centre():
    path = [[1.9, 0, 0], [1.95, 0, 0], [2.0, 0, 0], [2.05, 0, 0], [2.1, 0, 0]]
    observations = [
        {"gauss_fwhm": 0.8, "candidates": [_candidate("lm:a", 1.0),
                                               _candidate("lm:o", 6.2)]},
        {"gauss_fwhm": 0.8, "candidates": [_candidate("ln:a", 0.5),
                                               _candidate("ln:o", 6.1)]},
        {"gauss_fwhm": 0.8, "candidates": [_candidate("zc:o", 6.0)]},
        {"gauss_fwhm": 0.8, "candidates": [_candidate("rn:a", 0.5),
                                               _candidate("rn:o", 6.1)]},
        {"gauss_fwhm": 0.8, "candidates": [_candidate("rm:a", 1.0),
                                               _candidate("rm:o", 6.2)]},
    ]
    track = track_path(
        path, observations, period=2.0, hypotheses=HYPOTHESES,
        target_branch="acoustic",
        previous={"target_branch": "acoustic", "acquisition_seed_index": 4})
    assert track["reduced_q_origin_index"] == 2
    assert [row["acoustic_candidate_index"] for row in track["assignments"]] == [0, 0, None, 0, 0]


def test_acoustic_tracking_selects_low_connected_branch_and_labels_it_target():
    path = [[2.0, 0, 0], [2.1, 0, 0], [2.2, 0, 0]]
    observations = [
        {"gauss_fwhm": 0.8, "candidates": [_candidate("a0", 0.1), _candidate("o0", 6.0)]},
        {"gauss_fwhm": 0.8, "candidates": [_candidate("a1", 1.2), _candidate("o1", 6.2)]},
        {"gauss_fwhm": 0.8, "candidates": [_candidate("a2", 2.4), _candidate("o2", 6.4)]},
    ]
    track = track_path(path, observations, period=2.0, hypotheses=HYPOTHESES,
                       target_branch="acoustic", assignment_entropy_stop_nats=1.0)
    assert track["target_branch"] == "acoustic" and track["anchor_index"] == 1
    assert track["assignments"][1]["selected_candidate_index"] == 0
    acoustic = next(item for item in track["tracks"] if item["branch_id"] == "acoustic")
    optic = next(item for item in track["tracks"] if item["branch_id"] == "optic")
    assert acoustic["role"] == "target" and optic["role"] == "rival"


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


def test_hypothesis_master_candidates_persist_before_target_selection():
    outcome = {
        "detector_id": "det0", "omega0": 2.7, "omega0_err": 0.12,
        "gamma": 0.4, "interval": [2.5, 2.9], "interval_reliable": True,
        "modes": [
            {"omega0": 2.7, "omega0_err": 0.12, "gamma": 0.4, "amplitude": 10.0},
            {"omega0": 6.85, "omega0_err": 0.18, "gamma": 0.7, "amplitude": 8.0},
        ],
    }
    session = SessionState()
    session.put(ScanEntry(scan_id="master:q", scan_number=9_000_001, scan_type="energy",
                          status="fitted", detectors={"det0": outcome}))
    _materialize_branch_candidates(session)
    candidates = outcome["branch_candidates"]
    assert [candidate["energy_meV"] for candidate in candidates] == [2.7, 6.85]


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


def test_ambiguous_optic_like_mode_is_evidence_not_acoustic_delivery():
    path = [[2.0, 0.0, 0.0]]
    state = CardState(
        campaign_target={"branch": "acoustic", "label": "acoustic",
                         "selector": "tracked_branch", "target_energy_meV": None,
                         "energy_search_meV": [-2.0, 7.0], "path": path},
        hypothesis_snapshot={"hypotheses": HYPOTHESES, "target_hypothesis": "acoustic",
                             "steering": {"assignment_entropy_stop_nats": 0.2,
                                          "maximum_bridge_steps": 1}})
    card = SimpleNamespace(state=state, policy=SimpleNamespace(
        dispersion={"zone_period": 2.0}, collation={"t_round": 5.0}))
    outcome = {
        "detector_id": "det0", "q": path[0], "T": 200.0, "sample": "sample",
        "omega0": 5.98, "omega0_err": 0.03, "gamma": 0.3,
        "interval": [5.92, 6.04], "hesse_interval": [5.95, 6.01],
        "interval_reliable": True, "pooled": True, "n_members": 3,
        "fit_kind": "unconstrained", "best_label": "stokes+bg", "verdict": "resolved",
        "modality": "fold", "gauss_fwhm": 0.8,
        "modes": [{"omega0": 5.98, "omega0_err": 0.03, "gamma": 0.3,
                   "gamma_err": 0.02, "amplitude": 10.0}],
    }
    session = SessionState()
    session.put(ScanEntry(scan_id="master:q0", scan_number=9_000_000, scan_type="energy",
                          status="fitted", detectors={"det0": outcome}))

    track = _apply_tracked_branch(session, card, t_round=5.0)

    assert track["assignments"][0]["track_state"] == "ambiguous"
    assert outcome["branch_candidates"][0]["probabilities"]["acoustic"] < 0.15
    assert outcome["omega0"] is None and outcome["branch_id"] is None
    assert outcome["selected_candidate_index"] is None


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

    revision = track["revision"]
    # A new physical/provisional fit is display evidence only. Without a changed pooled authority it
    # must neither rerun joint assignment nor advance the durable track revision.
    provisional = {
        "detector_id": "det0", "q": path[1], "T": 200.0, "sample": "sample",
        "omega0": 3.2, "omega0_err": 0.05, "gamma": 0.2,
        "interval_reliable": True, "pooled": False, "best_label": "stokes+bg",
        "verdict": "resolved", "modality": "fold",
        "modes": [{"omega0": 3.2, "omega0_err": 0.05, "gamma": 0.2,
                   "amplitude": 2.0}],
    }
    session.put(ScanEntry(scan_id="physical:provisional", scan_number=17,
                          scan_type="energy", status="fitted", detectors={"det0": provisional}))
    unchanged = _apply_tracked_branch(session, card, t_round=5.0)
    assert unchanged["revision"] == revision
    assert unchanged["authoritative_evidence_signature"] == track[
        "authoritative_evidence_signature"]

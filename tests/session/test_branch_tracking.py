from __future__ import annotations

import math
from types import SimpleNamespace

from isar.schema.card import CardState
from isar.session.branch_tracking import (
    _lowest_energy_log_likelihood,
    anchor_index,
    track_path,
)
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


def test_ap3_connected_trajectory_keeps_acoustic_character_to_zone_boundary():
    hypotheses = [
        {"name": "optic", "zc_energy": {"mean": 3.5, "sigma": 1.75},
         "slope_zc": {"mean": 0.0, "sigma": 8.0}, "confidence": 0.6},
        {"name": "acoustic", "zc_energy": {"mean": 0.0, "sigma": 0.2},
         "slope_zc": {"mean": 12.0, "sigma": 12.0}, "confidence": 0.8},
    ]
    path = [[2.0 + 0.05 * index, 0.0, 0.0] for index in range(21)]
    observations = [{"candidates": []} for _ in path]
    observations[0] = {"gauss_fwhm": 0.8,
                       "candidates": [_candidate("zc:unrelated", 5.8)]}
    for index, energy in ((1, 0.45), (2, 0.87), (3, 1.38), (4, 1.80)):
        observations[index] = {"candidates": [_candidate(f"a{index}", energy)]}
    observations[3]["candidates"].append(_candidate("o3", 6.4))
    for index in range(5, 21):
        observations[index] = {
            "candidates": [_candidate(f"a{index}", 1.8 + 0.255 * (index - 4))]}

    track = track_path(
        path, observations, period=2.0, hypotheses=hypotheses,
        target_branch="acoustic",
        previous={"target_branch": "acoustic", "acquisition_seed_index": 3},
        assignment_entropy_stop_nats=1.0)

    for index in range(1, 21):
        row = track["assignments"][index]
        assert row["acoustic_candidate_index"] == 0
        assert row["candidates"][0]["trajectory_identity"] == "acoustic"
    assert track["assignments"][3]["optic_candidate_index"] == 1
    assert track["assignments"][3]["candidates"][1]["trajectory_identity"] == "optic"
    origin = track["assignments"][0]
    assert origin["track_state"] == "inferred_unresolved"
    assert origin["structural_endpoint_status"] == "inferred_unresolved"
    assert origin["acoustic_candidate_index"] is None
    assert origin["target_delivery_eligible"] is False
    assert origin["precision_coverage_eligible"] is False
    assert origin["candidates"][0]["trajectory_identity"] == "optic"
    assert track["inferred_endpoint_indices"] == [0]


def test_authoritative_trajectory_reverses_stale_candidate_character_prior():
    path = [[2.05, 0.0, 0.0], [2.1, 0.0, 0.0],
            [2.15, 0.0, 0.0], [2.2, 0.0, 0.0]]
    observations = []
    for index, energy in enumerate((0.45, 0.87, 1.38, 1.80)):
        candidate = _candidate(f"stale{index}", energy)
        candidate["probabilities"] = {"optic": 0.99, "acoustic": 0.005, "new": 0.005}
        candidate["trajectory_identity"] = "optic"
        observations.append({"candidates": [candidate]})

    track = track_path(path, observations, period=2.0, hypotheses=HYPOTHESES,
                       target_branch="acoustic", assignment_entropy_stop_nats=1.0)

    assert all(row["candidates"][0]["trajectory_identity"] == "acoustic"
               for row in track["assignments"])
    assert all(row["candidates"][0]["probabilities"]["acoustic"] > 0.85
               for row in track["assignments"])


def test_lone_candidate_contributes_no_lowest_energy_ordering_evidence():
    assert _lowest_energy_log_likelihood(0, [_candidate("only", 0.45)]) == 0.0


def test_fitted_gamma_acoustic_candidate_is_not_recast_as_structural_endpoint():
    path = [[2.0 + 0.05 * index, 0.0, 0.0] for index in range(4)]
    observations = [{"gauss_fwhm": 0.8, "candidates": [_candidate("a0", 0.1)]}]
    observations.extend({"candidates": [_candidate(f"a{index}", energy)]}
                        for index, energy in enumerate((0.45, 0.87, 1.38), start=1))

    track = track_path(path, observations, period=2.0, hypotheses=HYPOTHESES,
                       target_branch="acoustic", assignment_entropy_stop_nats=1.0)

    assert track["assignments"][0]["acoustic_candidate_index"] == 0
    assert track["assignments"][0].get("structural_endpoint_status") is None
    assert track["inferred_endpoint_indices"] == []


def test_structural_gamma_inference_requires_declared_reduced_zone_period():
    path = [[2.05 + 0.05 * index, 0.0, 0.0] for index in range(4)]
    observations = [{"candidates": []}]
    observations.extend({"candidates": [_candidate(f"a{index}", energy)]}
                        for index, energy in enumerate((0.45, 0.87, 1.38), start=1))

    track = track_path(path, observations, period=None, hypotheses=HYPOTHESES,
                       target_branch="acoustic", assignment_entropy_stop_nats=1.0)

    assert track["assignments"][0].get("structural_endpoint_status") is None
    assert track["inferred_endpoint_indices"] == []


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


def test_inferred_gamma_endpoint_never_delivers_or_seeds_dispersion_prior():
    path = [[2.0 + 0.05 * index, 0.0, 0.0] for index in range(5)]
    state = CardState(
        campaign_target={"branch": "acoustic", "label": "acoustic",
                         "selector": "tracked_branch", "target_energy_meV": None,
                         "energy_search_meV": [-2.0, 7.0], "path": path},
        hypothesis_snapshot={"hypotheses": HYPOTHESES, "target_hypothesis": "acoustic",
                             "steering": {"assignment_entropy_stop_nats": 1.0,
                                          "maximum_bridge_steps": 1}})
    card = SimpleNamespace(state=state, policy=SimpleNamespace(
        dispersion={"zone_period": 2.0}, collation={"t_round": 5.0}))
    session = SessionState()
    for index, energy in enumerate((5.8, 0.45, 0.87, 1.38, 1.80)):
        outcome = {
            "detector_id": "det0", "q": path[index], "T": 200.0, "sample": "sample",
            "omega0": energy, "omega0_err": 0.08, "gamma": 0.2,
            "interval": [energy - 0.16, energy + 0.16], "interval_reliable": True,
            "pooled": True, "n_members": 2, "pool_consistent": True,
            "fit_kind": "unconstrained", "best_label": "stokes+bg",
            "verdict": "resolved", "modality": "fold", "gauss_fwhm": 0.8,
            "modes": [{"omega0": energy, "omega0_err": 0.08, "gamma": 0.2,
                       "gamma_err": 0.03, "amplitude": 5.0}],
        }
        session.put(ScanEntry(scan_id=f"master:q{index}", scan_number=9_000_000 + index,
                              scan_type="energy", status="fitted",
                              detectors={"det0": outcome}))

    track = _apply_tracked_branch(session, card, t_round=5.0)
    origin = session.scans["master:q0"].detectors["det0"]

    assert track["assignments"][0]["track_state"] == "inferred_unresolved"
    assert origin["omega0"] is None and origin["branch_id"] is None
    assert origin["selected_candidate_index"] is None
    assert all(point.get("q") != path[0] for point in state.dispersion_prior)


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


# ------------------------------------------------------- gap-spanning character invariant (OP9)
# Regression: the character-handoff penalty was charged on EVERY edge, so a branch that was merely
# undetected at one q (an expected outcome at 5-10 counts, not physics) was treated as evidence
# against that branch's identity at the PREVIOUS q. In OP9 this dropped P(optic) at q=2.75 from
# 0.92 to 0.67, below the 0.85 confirm threshold, blocking the identity frontier — which then denied
# the following q the exposure that would have resolved their second peak. Self-reinforcing.
#
# The invariant must still forbid a genuine acoustic<->optic character flip. It must NOT forbid a
# branch being temporarily undetected. These are different propositions; the fix separates them.

def _two_branch_path(n=8, step=0.05, start=2.4):
    return [[start + i * step, 0.0, 0.0] for i in range(n)]


def _obs(rows):
    """rows: list of lists of energies (one list per q); [] means nothing resolved there."""
    return [{"candidates": [_candidate(f"c{i}:{j}", e) for j, e in enumerate(energies)]}
            for i, energies in enumerate(rows)]


# Real OP9 numbers (campaign 2026-07-21). Acoustic rises toward a near-flat optic; at q=2.80 the
# fitter resolved only ONE mode at 6.14, 1.6 meV below the optic it had just tracked at 7.76.
# That proximity is what made the handoff penalty bite -- it scales with how close the surviving
# candidate sits to the vanished branch, so a wide gap is harmless and a near one is fatal.
OP9_HYPOTHESES = [
    {"name": "optic", "zc_energy": {"mean": 6.0, "sigma": 3.0},
     "slope_zc": {"mean": 0.0, "sigma": 8.0}, "confidence": 0.6},
    {"name": "acoustic", "zc_energy": {"mean": 0.0, "sigma": 0.2},
     "slope_zc": {"mean": 12.0, "sigma": 12.0}, "confidence": 0.8},
]
# Verbatim from the OP9 campaign log (2026-07-21): (energy_meV, sigma_meV, reliable) per q.
# Recorded exactly rather than idealised -- the sigmas span two orders of magnitude and every
# candidate carried reliable=False, and the failure does not reproduce without both.
OP9_ROWS = [
    [(5.7939, 0.33915, False)],                                    # q=2.00
    [(6.0589, 0.00345, False)],                                    # q=2.05
    [(0.8918, 0.06775, False), (6.3328, 0.02042, False)],          # q=2.10
    [(1.3208, 0.07464, False), (6.6903, 0.03128, False)],          # q=2.15
    [(1.8564, 0.04759, False), (6.5382, 0.09834, False)],          # q=2.20
    [(2.2020, 0.04561, False), (6.9092, 0.00284, False)],          # q=2.25
    [(6.7953, 0.00584, False)],                                    # q=2.30
    [(3.0651, 0.00665, False), (7.2150, 0.01216, False)],          # q=2.35
    [(3.4246, 0.00978, False), (6.9274, 0.00338, False)],          # q=2.40
    [(3.8547, 0.02710, False), (7.3000, 0.01203, False)],          # q=2.45
    [(4.1589, 0.00660, False), (7.1096, 0.00372, False)],          # q=2.50
    [(4.3123, 0.16520, False), (7.4396, 0.03917, False)],          # q=2.55
    [(5.0678, 0.26286, False), (7.8453, 0.01504, False)],          # q=2.60
    [(5.2140, 0.01386, False), (7.3879, 0.26002, False)],          # q=2.65
    [(5.3498, 0.20314, False), (7.7840, 0.01038, False)],          # q=2.70
    [(5.5411, 0.04211, False), (7.7620, 0.04820, False)],          # q=2.75  <-- blocked here
    [(6.1413, 0.36429, False)],                                    # q=2.80  <-- second mode lost
    [(6.1112, 0.00703, False), (8.1123, 0.00627, False)],          # q=2.85
    [(8.4373, 0.02009, False)],                                    # q=2.90
    [(6.2730, 0.01446, False)],                                    # q=2.95
    [(7.8586, 0.29894, False)],                                    # q=3.00
]
OP9_PATH = [[2.00 + i * 0.05, 0.0, 0.0] for i in range(len(OP9_ROWS))]
OP9_Q275_INDEX = 15


def _op9_obs():
    return [{"candidates": [
                {"candidate_ref": f"c{i}:{j}", "energy_meV": e, "omega0_err": sig,
                 "sigma_meV": sig, "gamma": 0.4, "amplitude": 1.0, "reliable": rel}
                for j, (e, sig, rel) in enumerate(row)]}
            for i, row in enumerate(OP9_ROWS)]


def test_single_q_dropout_does_not_destroy_identity_at_the_neighbour():
    """OP9 regression: P(optic) at q=2.75 must clear the 0.85 confirm threshold.

    Recorded 0.67 at HEAD (blocking the frontier); 0.92 with the penalty disabled, matching the
    OP8 run of the identical campaign that reached ``met``.
    """
    result = track_path(OP9_PATH, _op9_obs(), period=2.0, hypotheses=OP9_HYPOTHESES,
                        target_branch="optic")
    at_275 = result["assignments"][OP9_Q275_INDEX]["candidates"]
    optic = max(at_275, key=lambda c: c["probabilities"]["optic"])
    assert optic["energy_meV"] == 7.7620
    assert optic["probabilities"]["optic"] > 0.85, (
        f'a one-q detection gap must not push the previous q below the confirm threshold; '
        f'got {optic["probabilities"]["optic"]:.3f}')


def test_genuine_character_flip_across_a_gap_is_still_penalised():
    """The invariant survives: swapping character across a gap must remain heavily disfavoured."""
    path = _two_branch_path(n=6)
    # Optic ~7.8 and acoustic ~4.x on both sides of a fully-unresolved q at index 3. If the tracker
    # were free to swap, the 7.8 mode after the gap could be relabelled acoustic.
    rows = [[4.0, 7.8], [4.2, 7.8], [4.4, 7.8], [], [4.8, 7.8], [5.0, 7.8]]
    result = track_path(path, _obs(rows), period=2.0, hypotheses=HYPOTHESES,
                        target_branch="optic")
    after_gap = result["assignments"][4]["candidates"]
    high = max(after_gap, key=lambda c: c["energy_meV"])
    assert high["energy_meV"] == 7.8
    assert high["probabilities"]["optic"] > high["probabilities"]["acoustic"], (
        "character must not flip across an all-missing state")


def test_multi_step_gap_does_not_let_character_flip():
    """Anti-evasion: a longer gap must not become a cheap way around the invariant."""
    path = _two_branch_path(n=8)
    rows = [[4.0, 7.8], [4.2, 7.8], [4.4, 7.8], [], [], [], [5.2, 7.8], [5.4, 7.8]]
    result = track_path(path, _obs(rows), period=2.0, hypotheses=HYPOTHESES,
                        target_branch="optic")
    after = result["assignments"][6]["candidates"]
    high = max(after, key=lambda c: c["energy_meV"])
    assert high["probabilities"]["optic"] > high["probabilities"]["acoustic"]

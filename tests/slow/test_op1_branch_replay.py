from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from isar.schema.card import Card
from isar.session.branch_tracking import track_path
from isar.session.driver import _apply_tracked_branch
from isar.session.state import SessionState


@pytest.mark.slow
def test_op1_pooled_modes_replay_as_optic_and_acoustic_tracks(tmp_path):
    source = Path("test_workspaces/OP1/session.json")
    if not source.exists():
        pytest.skip("local OP1 campaign workspace is unavailable")
    replay = tmp_path / "OP1-session.json"
    shutil.copy2(source, replay)
    session = SessionState.load(replay)
    path = [[2.0 + 0.05 * index, 0.0, 0.0] for index in range(21)]
    choices: dict[int, tuple[int, list[dict]]] = {}
    for entry in session.scans.values():
        for outcome in entry.detectors.values():
            if not outcome.get("pooled") or not outcome.get("modes") or not outcome.get("q"):
                continue
            index = round((float(outcome["q"][0]) - 2.0) / 0.05)
            if not 0 <= index < len(path):
                continue
            candidates = [{
                "candidate_ref": f"{entry.scan_id}:mode:{mode_index}",
                "energy_meV": mode["omega0"], "omega0_err": mode.get("omega0_err"),
                "sigma_meV": mode.get("omega0_err"), "gamma": mode.get("gamma"),
                "amplitude": mode.get("amplitude"), "reliable": True,
            } for mode_index, mode in enumerate(outcome["modes"])]
            choice = (int(outcome.get("n_members") or 0), candidates)
            if index not in choices or choice[0] > choices[index][0]:
                choices[index] = choice
    observations = [{"candidates": choices.get(index, (0, []))[1]}
                    for index in range(len(path))]
    hypotheses = [
        {"name": "optic", "zc_energy": {"mean": 6.0, "sigma": 3.0},
         "slope_zc": {"mean": 0.0, "sigma": 8.0}},
        {"name": "acoustic", "zc_energy": {"mean": 0.0, "sigma": 0.2},
         "slope_zc": {"mean": 12.0, "sigma": 12.0}},
    ]

    track = track_path(path, observations, period=2.0, hypotheses=hypotheses,
                       assignment_entropy_stop_nats=0.7)

    expected = {13: 7.774, 15: 7.919, 18: 7.930, 20: 8.117}
    for index, energy in expected.items():
        row = track["assignments"][index]
        selected = row["candidates"][row["selected_candidate_index"]]["energy_meV"]
        assert selected == pytest.approx(energy, abs=0.02)
        assert row["optic_candidate_index"] != row["acoustic_candidate_index"]

    optic = next(item for item in track["tracks"] if item["branch_id"] == "optic")
    for segment in optic["segments"]:
        indices = [round((point["hkl"][0] - 2.0) / 0.05) for point in segment["points"]]
        assert all(right - left == 1 for left, right in zip(indices, indices[1:]))

    # Exercise the real ledger/state wiring in memory. The historical workspace remains untouched;
    # only its copied session and an in-memory Card receive the new objective semantics.
    card = Card.load(source.parent / "card.policy.toml", source.parent / "card.state.json")
    card.state.campaign_target = {
        "branch": "optic", "label": "optic phonon around 6 meV",
        "selector": "tracked_branch", "target_energy_meV": None,
        "energy_search_meV": [-2.0, 12.0], "path": path,
    }
    card.state.hypothesis_snapshot = {
        "hypotheses": hypotheses, "target_hypothesis": "optic",
        "steering": {"assignment_entropy_stop_nats": 0.7, "maximum_bridge_steps": 1},
    }
    integrated = _apply_tracked_branch(session, card, t_round=5.0)
    assert integrated is card.state.branch_tracks
    selected_by_h = {}
    for entry in session.scans.values():
        for outcome in entry.detectors.values():
            if outcome.get("pooled") and outcome.get("selected_candidate_index") is not None:
                selected_by_h[round(float(outcome["q"][0]), 2)] = float(outcome["omega0"])
    for index, energy in expected.items():
        assert selected_by_h[round(path[index][0], 2)] == pytest.approx(energy, abs=0.02)

from __future__ import annotations

from isar.drive.acquisition.acquire import _select_target, propose_scan, tracked_anchor_index
from isar.drive.acquisition.model import fit
from isar.drive.objective import ObjectiveSpec, shipped_hypothesis_snapshot
from isar.drive.supervisor.stopping import GridQ


class _Model:
    def predict(self, q):
        return 6.0, 1.0


def _grid():
    return [GridQ(index=index, hkl=(2.0 + 0.05 * index, 0.0, 0.0), q=0.05 * index,
                  tolerance=0.5) for index in range(7)]


def _pick(grid):
    return _select_target(grid, _Model(), tracked_branch=True, maximum_bridge_steps=1)


def _authoritative_evidence(point, sequence):
    point.evidence_updates.append(dict(point.evidence_master or {}))
    point.evidence_update_sequences.append(sequence)


def test_tracked_opening_is_anchor_then_strictly_adjacent():
    grid = _grid()
    pick, kind, _reason, _score = _pick(grid)
    assert (pick.index, kind) == (0, "quicklook")

    grid[0].n_quicklook = 1
    grid[0].track_state = "confirmed"
    pick, kind, _reason, _score = _pick(grid)
    assert (pick.index, kind) == (1, "quicklook")


def test_ambiguous_frontier_gets_one_bridge_then_is_revisited():
    grid = _grid()
    grid[0].n_quicklook = 1
    grid[0].track_state = "confirmed"
    grid[1].n_quicklook = 1
    grid[1].track_state = "ambiguous"
    grid[1].evidence_master = {
        "diagnostic_halfwidth": 0.1,
        "branch_candidates": [{"energy_meV": 1.0, "sigma_meV": 0.1}],
    }

    pick, kind, reason, _score = _pick(grid)
    assert (pick.index, kind) == (2, "quicklook")
    assert "bridge" in reason

    grid[2].n_quicklook = 1
    grid[2].track_state = "provisional"
    grid[2].evidence_master = {
        "branch_candidates": [{"energy_meV": 1.2, "sigma_meV": 0.03}],
    }
    _authoritative_evidence(grid[2], 10)
    pick, kind, reason, _score = _pick(grid)
    assert (pick.index, kind) == (1, "production")
    assert "revisit" in reason


def test_ambiguous_frontier_does_not_revisit_after_visited_bridge_without_fitted_evidence():
    grid = _grid()
    grid[0].n_quicklook = 1
    grid[0].track_state = "confirmed"
    grid[1].n_quicklook = 1
    grid[1].track_state = "ambiguous"
    grid[1].evidence_master = {
        "diagnostic_halfwidth": 0.1,
        "branch_candidates": [{"energy_meV": 1.0, "sigma_meV": 0.1}],
    }
    grid[2].n_quicklook = 1
    pick, _kind, _reason, _score = _pick(grid)
    assert pick is None or pick.index != 1


def test_tracked_opening_never_precision_polishes_a_confirmed_point_ahead_of_coverage():
    grid = _grid()
    grid[0].n_quicklook = 1
    grid[0].n_production = 2
    grid[0].track_state = "confirmed"
    grid[0].master = {"candidate_state": "provisional", "diagnostic_halfwidth": 2.0}

    pick, kind, _reason, _score = _pick(grid)
    assert (pick.index, kind) == (1, "quicklook")


def test_scan_declines_cannot_block_master_authoritative_zone_centre():
    grid = _grid()
    grid[0].n_quicklook = 1
    grid[0].n_decline = 3
    pick, kind, reason, score = _pick(grid)
    assert (pick.index, kind) == (0, "production")
    assert score is not None
    assert "not yet statistically adequate" in reason


def test_persisted_reduced_zone_anchor_can_be_inside_the_path():
    grid = _grid()
    pick, kind, _reason, _score = _select_target(
        grid, _Model(), tracked_branch=True, maximum_bridge_steps=1, track_anchor_index=3)
    assert (pick.index, kind) == (3, "quicklook")
    grid[3].n_quicklook = 1
    grid[3].track_state = "confirmed"
    pick, kind, _reason, _score = _select_target(
        grid, _Model(), tracked_branch=True, maximum_bridge_steps=1, track_anchor_index=3)
    assert (pick.index, kind) == (2, "quicklook")


def test_first_proposal_computes_interior_reduced_zone_anchor_before_tracks_exist():
    path = [(1.8, 0.0, 0.0), (1.9, 0.0, 0.0), (2.0, 0.0, 0.0),
            (2.1, 0.0, 0.0), (2.2, 0.0, 0.0)]
    assert tracked_anchor_index(path, period=2.0) == 2
    objective = ObjectiveSpec(
        path=path, tolerance_meV=0.5, budget={"kind": "neutrons", "amount": 1e12},
        target_branch="optic", target_selector="tracked_branch",
        **shipped_hypothesis_snapshot("optic", (-2.0, 12.0)))
    proposal = propose_scan(objective, _Model(), [], resolution_info=0.8,
                            track_zone_period=2.0)
    assert proposal is not None and proposal.h == 2.0


def test_acoustic_opening_uses_nearest_predicted_resolvable_anchor():
    class AcousticModel:
        def predict(self, q):
            return 10.0 * q, 0.5

    path = [(2.0, 0.0, 0.0), (2.05, 0.0, 0.0), (2.1, 0.0, 0.0),
            (2.15, 0.0, 0.0)]
    assert tracked_anchor_index(path, period=2.0, target_branch="acoustic",
                                model=AcousticModel(), resolution_info=0.8) == 2


def test_acoustic_opening_uses_highest_energy_option_when_none_is_predicted_resolvable():
    class UnresolvableAcousticModel:
        def predict(self, q):
            return 0.1 * q, 0.5

    path = [(2.0, 0.0, 0.0), (2.05, 0.0, 0.0), (2.1, 0.0, 0.0),
            (2.15, 0.0, 0.0)]
    assert tracked_anchor_index(
        path, period=2.0, target_branch="acoustic",
        model=UnresolvableAcousticModel(), resolution_info=0.8) == 3


def test_unresolvable_acoustic_fallback_uses_path_order_for_symmetric_energy_tie():
    class UnresolvableAcousticModel:
        def predict(self, q):
            return 0.0, 0.5

    path = [(1.8, 0.0, 0.0), (1.9, 0.0, 0.0), (2.0, 0.0, 0.0),
            (2.1, 0.0, 0.0), (2.2, 0.0, 0.0)]
    assert tracked_anchor_index(
        path, period=2.0, target_branch="acoustic",
        model=UnresolvableAcousticModel(), resolution_info=0.8) == 0


def test_cold_acoustic_campaign_uses_hypothesis_to_avoid_zone_centre_and_far_endpoint():
    path = [(2.0, 0.0, 0.0), (2.05, 0.0, 0.0), (2.1, 0.0, 0.0),
            (2.15, 0.0, 0.0)]
    objective = ObjectiveSpec(
        path=path, tolerance_meV=0.5, budget={"kind": "neutrons", "amount": 1e12},
        target_branch="acoustic", target_selector="tracked_branch",
        **shipped_hypothesis_snapshot("acoustic", (-2.0, 7.0)))
    proposal = propose_scan(objective, fit([]), [], resolution_info=0.8,
                            track_zone_period=2.0)
    assert proposal is not None and proposal.h == 2.1


def test_anchor_strategy_ceiling_switches_to_adjacent_differentiation_probe():
    grid = _grid()
    grid[0].n_quicklook = 1
    grid[0].n_production = grid[0].max_production_batches
    grid[0].track_state = "ambiguous"
    pick, kind, reason, score = _pick(grid)
    assert (pick.index, kind) == (1, "quicklook")
    assert "strategy ceiling" in reason
    assert score.purpose == "differentiate"


def _ambiguous_anchor(grid, *, precision=0.0189650075206503):
    anchor = grid[3]
    anchor.tolerance = 0.05
    anchor.n_quicklook = 1
    anchor.n_production = 1
    anchor.track_state = "ambiguous"
    anchor.evidence_master = {
        "candidate_state": "ambiguous",
        "target_branch": "acoustic",
        "branch_posterior": {"acoustic": 0.8617, "optic": 0.0998, "new": 0.0385},
        "assignment_entropy": "0.4836",
        "diagnostic_halfwidth": precision,
        "branch_candidates": [
            {"energy_meV": 1.4111, "sigma_meV": precision},
            {"energy_meV": 6.4200, "sigma_meV": precision / 2.0},
        ],
    }
    return anchor


def test_adequate_ambiguous_acoustic_anchor_immediately_moves_to_best_adjacent_probe():
    grid = _grid()
    _ambiguous_anchor(grid)

    def classify(point):
        return {2: 0.2, 4: 0.7}.get(point.index, 0.0)

    pick, kind, reason, score = _select_target(
        grid, _Model(), tracked_branch=True, maximum_bridge_steps=1,
        track_anchor_index=3, classify=classify)

    assert (pick.index, kind) == (4, "quicklook")
    assert "neighboring identity evidence" in reason
    assert score.purpose == "differentiate"
    assert score.evidence_transition == {
        "source_hkl": [2.15, 0.0, 0.0],
        "selected_probe_hkl": [2.2, 0.0, 0.0],
        "source_q": 0.15000000000000002,
        "selected_probe_q": 0.2,
        "candidate_count": 2,
        "candidate_precision_meV": 0.0189650075206503,
        "tolerance_meV": 0.05,
        "branch_probability": 0.8617,
        "assignment_entropy": 0.4836,
        "adequate": True,
        "classification_information": 0.7,
        "transition_reason": "adequate_candidates_ambiguous_identity",
    }


def test_adequate_ambiguous_anchor_uses_path_order_to_break_classification_tie():
    grid = _grid()
    _ambiguous_anchor(grid)
    pick, kind, _reason, _score = _select_target(
        grid, _Model(), tracked_branch=True, track_anchor_index=3,
        classify=lambda _point: 0.4)
    assert (pick.index, kind) == (2, "quicklook")


def test_ap1_ambiguous_two_mode_state_uses_global_branch_information_for_breadth():
    grid = _grid()
    anchor = _ambiguous_anchor(grid)
    for neighbor in (grid[2], grid[4]):
        neighbor.n_quicklook = 1
        neighbor.track_state = "ambiguous"
        neighbor.evidence_master = {
            "branch_candidates": [
                {"energy_meV": 1.0, "sigma_meV": 0.05},
                {"energy_meV": 6.2, "sigma_meV": 0.05},
            ],
        }
        _authoritative_evidence(neighbor, 4)
    # No neighboring revision is newer than the anchor's last production, so another same-q scan is
    # forbidden. Of the remaining survey points, q-index 6 has the highest branch information.
    anchor.n_production = 1
    anchor.production_sequences = [10]
    pick, kind, reason, _score = _select_target(
        grid, _Model(), tracked_branch=True, maximum_bridge_steps=1,
        track_anchor_index=3, classify=lambda point: float(point.index))
    assert (pick.index, kind) == (6, "quicklook")
    assert "broaden" in reason


def test_statistics_limited_ambiguous_anchor_may_receive_more_same_q_exposure():
    grid = _grid()
    _ambiguous_anchor(grid, precision=0.8)
    pick, kind, reason, score = _select_target(
        grid, _Model(), tracked_branch=True, track_anchor_index=3,
        classify=lambda _point: 1.0)
    assert (pick.index, kind) == (3, "production")
    assert "not yet statistically adequate" in reason
    assert score.purpose == "hypothesis_check"


def test_unquantified_rival_candidate_keeps_anchor_statistics_limited():
    grid = _grid()
    anchor = _ambiguous_anchor(grid)
    anchor.evidence_master["branch_candidates"][1].pop("sigma_meV")
    pick, kind, reason, _score = _select_target(
        grid, _Model(), tracked_branch=True, track_anchor_index=3,
        classify=lambda _point: 1.0)
    assert (pick.index, kind) == (3, "production")
    assert "not yet statistically adequate" in reason


def test_ambiguous_anchor_revisit_requires_neighboring_evidence_and_obeys_bridge_allowance():
    grid = _grid()
    anchor = _ambiguous_anchor(grid)
    for neighbor in (grid[2], grid[4]):
        neighbor.n_quicklook = 1
        neighbor.track_state = "provisional"
        neighbor.evidence_master = {
            "branch_candidates": [{"energy_meV": 1.2, "sigma_meV": 0.03}],
        }
        _authoritative_evidence(neighbor, 10 + neighbor.index)
    anchor.n_production = 0
    pick, kind, reason, _score = _select_target(
        grid, _Model(), tracked_branch=True, maximum_bridge_steps=1,
        track_anchor_index=3)
    assert (pick.index, kind) == (3, "production")
    assert "using neighboring identity evidence" in reason

    anchor.n_production = 1
    anchor.production_sequences = [20]
    anchor.identity_revisit_sequences = [20]
    pick, _kind, _reason, _score = _select_target(
        grid, _Model(), tracked_branch=True, maximum_bridge_steps=1,
        track_anchor_index=3)
    assert pick is None or pick.index != 3


def test_ambiguous_anchor_does_not_revisit_after_neighbor_scan_without_fitted_evidence():
    grid = _grid()
    anchor = _ambiguous_anchor(grid)
    grid[2].n_quicklook = 1
    grid[4].n_quicklook = 1
    grid[2].evidence_master = {}
    grid[4].evidence_master = {}
    anchor.n_production = 0
    pick, _kind, _reason, _score = _select_target(
        grid, _Model(), tracked_branch=True, maximum_bridge_steps=1,
        track_anchor_index=3)
    assert pick is None or pick.index != 3


def test_ambiguous_anchor_does_not_revisit_from_inconsistent_neighbor_master():
    grid = _grid()
    anchor = _ambiguous_anchor(grid)
    anchor.n_production = 1
    anchor.production_sequences = [5]
    neighbor = grid[2]
    neighbor.n_quicklook = 1
    neighbor.track_state = "provisional"
    neighbor.evidence_master = {
        "branch_candidates": [{"energy_meV": 1.2, "sigma_meV": 0.03}],
        "pool_consistent": False,
    }
    _authoritative_evidence(neighbor, 10)

    pick, _kind, _reason, _score = _select_target(
        grid, _Model(), tracked_branch=True, maximum_bridge_steps=1,
        track_anchor_index=3)

    assert pick is None or pick.index != 3


def test_ap2_half_mev_sequence_broadens_after_one_middle_point_identity_revisit():
    grid = _grid()[:4]
    assert all(point.tolerance == 0.5 for point in grid)
    proposals = []

    pick, kind, _reason, _score = _select_target(
        grid, _Model(), tracked_branch=True, maximum_bridge_steps=1,
        track_anchor_index=3)
    proposals.append((pick.hkl[0], kind))
    assert proposals[-1] == (2.15, "quicklook")

    grid[3].n_quicklook = 1                       # H=2.15 acquisition seed
    grid[3].track_state = "confirmed"
    grid[3].evidence_master = {
        "branch_candidates": [{"energy_meV": 1.4, "sigma_meV": 0.08}],
    }
    _authoritative_evidence(grid[3], 2)

    pick, kind, _reason, _score = _select_target(
        grid, _Model(), tracked_branch=True, maximum_bridge_steps=1,
        track_anchor_index=3)
    proposals.append((pick.hkl[0], kind))
    assert proposals[-1] == (2.10, "quicklook")

    middle = grid[2]                              # H=2.10 ambiguous target frontier
    middle.n_quicklook = 1
    middle.track_state = "ambiguous"
    middle.evidence_master = {
        "diagnostic_halfwidth": 0.08,
        "branch_candidates": [
            {"energy_meV": 0.95, "sigma_meV": 0.08},
            {"energy_meV": 6.2, "sigma_meV": 0.05},
        ],
    }
    _authoritative_evidence(middle, 4)

    pick, kind, _reason, _score = _select_target(
        grid, _Model(), tracked_branch=True, maximum_bridge_steps=1,
        track_anchor_index=3)
    proposals.append((pick.hkl[0], kind))
    assert proposals[-1] == (2.05, "quicklook")

    grid[1].n_quicklook = 1                       # H=2.05 neighboring evidence
    grid[1].track_state = "provisional"
    grid[1].evidence_master = {
        "branch_candidates": [
            {"energy_meV": 0.48, "sigma_meV": 0.07},
            {"energy_meV": 6.08, "sigma_meV": 0.05},
        ],
    }
    _authoritative_evidence(grid[1], 6)

    pick, kind, reason, score = _select_target(
        grid, _Model(), tracked_branch=True, maximum_bridge_steps=1,
        track_anchor_index=3)
    proposals.append((pick.hkl[0], kind))
    assert (pick.index, kind) == (2, "production")
    assert "revisit" in reason
    assert score.purpose == "differentiate"
    assert score.evidence_transition["authoritative_evidence_revision"]

    # The H=2.10 revisit consumes the bridge allowance. Unchanged identity evidence must broaden
    # to H=2.00, rather than emitting scans 2..13 at the middle point.
    middle.n_production = 1
    middle.production_sequences.append(7)
    middle.identity_revisit_sequences.append(7)
    _authoritative_evidence(middle, 8)
    pick, kind, reason, _score = _select_target(
        grid, _Model(), tracked_branch=True, maximum_bridge_steps=1,
        track_anchor_index=3)
    proposals.append((pick.hkl[0], kind))
    assert (pick.index, kind) == (0, "quicklook")
    assert "broaden" in reason
    assert proposals == [
        (2.15, "quicklook"), (2.10, "quicklook"), (2.05, "quicklook"),
        (2.10, "production"), (2.00, "quicklook"),
    ]

from __future__ import annotations

from isar.drive.acquisition.acquire import (
    MAX_RECONNECT_PRODUCTIONS,
    _classification_link_diagnostics,
    _select_target,
    propose_scan,
    tracked_anchor_index,
)
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


def _converged_master(hkl, hw=0.3, *, pool_consistent=True):
    return {"hkl": hkl, "omega0": 3.0, "halfwidth": hw, "usable": True,
            "reliable": True, "certifiable": True, "disposition": "resolved",
            "pool_consistent": pool_consistent, "n_members": 2, "members": ["1", "2"]}


def test_pool_inconsistent_point_is_re_acquired_not_abandoned():
    """AP8 Q1: once the identity cascade is fixed, a confirmed interior point whose pooled master is
    ``pool_consistent=False`` is an ordinary unconverged precision target — it has no certifiable
    interval (deficit +inf) and one production so far — so the tracked selector must pick it for
    another production rather than leaving the frontier with no action (which stopped AP8)."""
    grid = _grid()
    for index, g in enumerate(grid):
        g.n_quicklook = 1
        g.n_production = 1
        g.track_state = "confirmed"
        g.master = _converged_master(g.hkl, pool_consistent=(index != 3))
    # Every point certifies except index 3, whose members disagree (pool_consistent=False).
    assert not grid[3].converged and grid[3].cert_halfwidth is None
    assert all(grid[i].converged for i in range(len(grid)) if i != 3)

    pick, kind, _reason, score = _pick(grid)
    assert pick is not None, "a pool-inconsistent frontier must remain actionable"
    assert (pick.index, kind) == (3, "production")
    assert score is not None


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


def test_unmeasured_inferred_gamma_is_overlaid_and_not_proposed():
    path = [(2.0, 0.0, 0.0), (2.05, 0.0, 0.0)]
    objective = ObjectiveSpec(
        path=path, tolerance_meV=0.5, budget={"kind": "neutrons", "amount": 1e12},
        target_branch="acoustic", target_selector="tracked_branch",
        **shipped_hypothesis_snapshot("acoustic", (-2.0, 12.0)))
    branch_track = {
        "revision": 4,
        "acquisition_seed_index": 0,
        "assignments": [{
            "path_index": 0,
            "track_state": "inferred_unresolved",
            "structural_endpoint_status": "inferred_unresolved",
            "is_reduced_q_origin": True,
        }],
    }

    proposal = propose_scan(
        objective, _Model(), [], resolution_info=0.8, track_zone_period=2.0,
        track_anchor_index=0, branch_track=branch_track)

    assert proposal is not None and proposal.h == 2.05


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


def test_inferred_gamma_does_not_block_measured_point_precision_work():
    grid = _grid()[:2]
    grid[0].n_quicklook = 1
    grid[0].track_state = "inferred_unresolved"
    grid[0].structural_endpoint_status = "inferred_unresolved"
    grid[0].is_reduced_q_origin = True
    grid[0].evidence_master = {"track_state": "inferred_unresolved",
                               "branch_candidates": [{"energy_meV": 5.8}]}
    grid[1].n_quicklook = 1
    grid[1].track_state = "confirmed"
    grid[1].master = {"usable": True, "omega0": 0.45, "halfwidth": 0.8,
                      "reliable": True, "certifiable": True,
                      "candidate_state": "confirmed"}
    grid[1].evidence_master = dict(grid[1].master)

    pick, kind, _reason, _score = _select_target(
        grid, _Model(), tracked_branch=True, track_anchor_index=1)

    assert (pick.index, kind) == (1, "production")


def test_inferred_gamma_anchor_advances_without_rescanning_endpoint():
    grid = _grid()[:2]
    grid[0].n_quicklook = 1
    grid[0].track_state = "inferred_unresolved"
    grid[0].structural_endpoint_status = "inferred_unresolved"
    grid[0].is_reduced_q_origin = True
    grid[0].evidence_master = {"track_state": "inferred_unresolved",
                               "structural_endpoint_status": "inferred_unresolved",
                               "is_reduced_q_origin": True}

    pick, kind, _reason, _score = _select_target(
        grid, _Model(), tracked_branch=True, track_anchor_index=0)

    assert (pick.index, kind) == (1, "quicklook")


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
        # Precision adequacy split from identity adequacy (OP10).  This fixture sets no tracker
        # identity fields, so identity reads unknown — and unknown is never treated as clear.
        "precision_adequate": True,
        "target_probability": None,
        "identity_entropy": None,
        "local_identity_clear": False,
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


def _ap3_broadening_grid():
    grid = [GridQ(index=index, hkl=(2.0 + 0.05 * index, 0.0, 0.0),
                  q=0.05 * index, tolerance=0.5) for index in range(21)]
    anchor = _ambiguous_anchor(grid)
    anchor.production_sequences = [10]
    for index in (0, 1, 2, 4):
        grid[index].n_quicklook = 1
        grid[index].track_state = "ambiguous"
        grid[index].evidence_master = {
            "branch_candidates": [{"energy_meV": 0.45 * index, "sigma_meV": 0.08}],
        }
    return grid


def test_ap3_broadening_discounts_zone_boundary_across_unmeasured_interval():
    grid = _ap3_broadening_grid()

    def classify(point):
        if point.index == 5:
            return 0.04
        if point.index == 20:
            return 0.089
        return 0.01

    pick, kind, reason, score = _select_target(
        grid, _Model(), tracked_branch=True, maximum_bridge_steps=1,
        track_anchor_index=3, classify=classify)

    assert (pick.index, kind) == (5, "quicklook")
    assert "broaden" in reason
    diagnostics = score.evidence_transition
    assert diagnostics["classification_information"] == 0.04
    assert diagnostics["expected_identity_information"] < 0.04
    assert diagnostics["unmeasured_gap_steps"] == 1
    assert diagnostics["propagated_association_uncertainty"] > 0.0
    assert score.classification_eig == diagnostics["expected_identity_information"]


def test_distant_probe_can_still_win_when_identity_information_is_sufficient():
    grid = _ap3_broadening_grid()

    pick, kind, _reason, score = _select_target(
        grid, _Model(), tracked_branch=True, maximum_bridge_steps=1,
        track_anchor_index=3,
        classify=lambda point: 0.25 if point.index == 20 else 0.04)

    assert (pick.index, kind) == (20, "quicklook")
    assert score.evidence_transition["unmeasured_gap_steps"] == 16
    assert score.evidence_transition["expected_identity_information"] > 0.04


def test_link_uncertainty_is_symmetric_on_both_sides_of_source():
    grid = [GridQ(index=index, hkl=(2.0 + 0.05 * index, 0.0, 0.0),
                  q=0.05 * index, tolerance=0.5) for index in range(7)]
    for index in (1, 2, 3, 4, 5):
        grid[index].n_quicklook = 1

    left = _classification_link_diagnostics(grid, 3, 0, 0.04)
    right = _classification_link_diagnostics(grid, 3, 6, 0.04)

    assert left["unmeasured_gap_steps"] == right["unmeasured_gap_steps"] == 1
    assert left["expected_identity_information"] == right["expected_identity_information"]


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


# --- OP10: a precise but identity-ambiguous frontier must keep counting ----------------------
#
# OP10 stopped `incomplete` at path index 15 (H=2.75) with n_production=1 of an authorized 12,
# exposure_saturated False and a 10e9 per-scan ceiling untouched.  The frontier carried two fitted
# candidates whose centres were precise (0.129 meV against a 0.5 meV tolerance) while the branch
# assignment stayed undecided.  Precision adequacy was read as identity adequacy, which suppressed
# same-q production, and the only same-q route left was capped by ``maximum_bridge_steps``.  Once
# broadening ran out of unvisited q the controller returned no proposal at all.


def _op10_frontier(grid, *, local_identity_clear=False, n_production=1):
    """Index 1 as OP10's blocked frontier: centres precise, branch identity undecided."""
    grid[0].n_quicklook = 1
    grid[0].track_state = "confirmed"
    frontier = grid[1]
    frontier.n_quicklook = 1
    frontier.n_production = n_production
    frontier.production_sequences = [20] * n_production
    frontier.identity_revisit_sequences = [20] * n_production
    frontier.track_state = "ambiguous"
    frontier.target_probability = 0.95 if local_identity_clear else 0.62
    frontier.identity_entropy = 0.05 if local_identity_clear else 0.66
    frontier.local_identity_clear = local_identity_clear
    frontier.evidence_master = {
        "target_branch": "optic",
        "diagnostic_halfwidth": 0.1287,
        "branch_candidates": [
            {"energy_meV": 7.5787, "sigma_meV": 0.1287},
            {"energy_meV": 6.9012, "sigma_meV": 0.1287},
        ],
    }
    return frontier


def _visit_rest(grid, start):
    """Spend every remaining classification route, leaving broadening nothing to pick."""
    for point in grid[start:]:
        point.n_quicklook = 1
        point.track_state = "blocked"


def test_op10_ambiguous_frontier_keeps_counting_once_broadening_is_exhausted():
    grid = _grid()
    frontier = _op10_frontier(grid)
    _visit_rest(grid, 2)

    pick, kind, reason, _score = _select_target(
        grid, _Model(), tracked_branch=True, maximum_bridge_steps=1)
    assert pick is not None, "a frontier with same-q budget left must not stop the campaign"
    assert (pick.index, kind) == (frontier.index, "production")
    assert "identity" in reason


def test_op10_frontier_buys_adjacent_evidence_before_counting_itself():
    """Neighbour-first ordering survives: cheap discrimination outranks brute-force dwell."""
    grid = _grid()
    frontier = _op10_frontier(grid)
    # grid[2] unvisited -> the provisional bridge is still available.
    pick, kind, _reason, _score = _select_target(
        grid, _Model(), tracked_branch=True, maximum_bridge_steps=1)
    assert (pick.index, kind) == (2, "quicklook")
    assert pick.index != frontier.index


def test_op10_frontier_broadens_before_falling_back_to_same_q():
    grid = _grid()
    _op10_frontier(grid)
    grid[2].n_quicklook = 1
    grid[2].track_state = "blocked"
    # grid[3:] stay unvisited, so an informative broadening probe still exists.
    pick, kind, reason, _score = _select_target(
        grid, _Model(), tracked_branch=True, maximum_bridge_steps=1,
        classify=lambda _point: 1.0)
    assert kind == "quicklook"
    assert "broaden" in reason


def test_single_candidate_can_still_be_identity_ambiguous():
    """Candidate count is diagnostic, not a criterion: one mode can be acoustic or optic."""
    grid = _grid()
    frontier = _op10_frontier(grid)
    frontier.evidence_master["branch_candidates"] = [
        {"energy_meV": 7.5787, "sigma_meV": 0.1287},
    ]
    _visit_rest(grid, 2)

    pick, kind, _reason, _score = _select_target(
        grid, _Model(), tracked_branch=True, maximum_bridge_steps=1)
    assert (pick.index, kind) == (frontier.index, "production")


def test_two_candidates_with_decisive_identity_are_not_counted_again():
    """Two separated candidates with one-to-one ownership need no identity production."""
    grid = _grid()
    _op10_frontier(grid, local_identity_clear=True)
    _visit_rest(grid, 2)

    pick, _kind, _reason, _score = _select_target(
        grid, _Model(), tracked_branch=True, maximum_bridge_steps=1)
    assert pick is None or pick.index != 1


def test_locally_clear_but_disconnected_frontier_is_not_counted_again():
    """A decisive local posterior behind a path gap is a connectivity problem, not a count problem."""
    grid = _grid()
    frontier = _op10_frontier(grid, local_identity_clear=True)
    frontier.track_state = "blocked"
    _visit_rest(grid, 2)

    pick, _kind, _reason, _score = _select_target(
        grid, _Model(), tracked_branch=True, maximum_bridge_steps=1)
    assert pick is None or pick.index != 1


# --- OP10 part two: the frontier was blocked by a gap in the *rival* trajectory ---------------
#
# At H=2.75 the two candidates were 5.53 and 7.72 meV — 2.18 meV apart, ~22 sigma, already cleanly
# resolved as peaks.  The ambiguity was in the labels, and P(optic)=0.752 sat just under the 0.85
# confirm gate because the acoustic branch had not been fitted at 2.60/2.65/2.70, breaking the
# continuity evidence that would assign 5.53 to acoustic.  Those q had only ever had a 10M
# quicklook.  Counting the frontier harder cannot fix that; counting the *gap* can.


def _rival_gap_grid():
    """Confirmed run carrying two modes, then three single-mode q, then a two-mode frontier."""
    grid = [GridQ(index=i, hkl=(2.0 + 0.05 * i, 0.0, 0.0), q=0.05 * i, tolerance=0.5,
                  max_production_batches=12) for i in range(6)]
    two_mode = [{"energy_meV": 4.55, "sigma_meV": 0.05}, {"energy_meV": 7.61, "sigma_meV": 0.05}]
    one_mode = [{"energy_meV": 7.88, "sigma_meV": 0.05}]
    for index in (0, 1):
        grid[index].n_quicklook = 1
        grid[index].track_state = "confirmed"
        grid[index].evidence_master = {"branch_candidates": list(two_mode)}
    for index in (2, 3, 4):                      # the rival gap: quicklooked only, one mode fitted
        grid[index].n_quicklook = 1
        grid[index].track_state = "confirmed"
        grid[index].evidence_master = {"branch_candidates": list(one_mode)}
    frontier = grid[5]
    frontier.n_quicklook = 1
    frontier.n_production = 1
    frontier.production_sequences = [20]
    frontier.track_state = "ambiguous"
    frontier.local_identity_clear = False
    frontier.target_probability = 0.752
    frontier.identity_entropy = 0.545
    frontier.evidence_master = {
        "branch_candidates": [{"energy_meV": 5.53, "sigma_meV": 0.065},
                              {"energy_meV": 7.72, "sigma_meV": 0.097}],
    }
    return grid, frontier


def test_blocked_frontier_counts_the_rival_gap_before_counting_itself():
    grid, frontier = _rival_gap_grid()
    pick, kind, reason, _score = _select_target(
        grid, _Model(), tracked_branch=True, maximum_bridge_steps=1)
    assert kind == "production"
    assert pick.index != frontier.index, "counting the frontier cannot restore rival continuity"
    assert pick.index == 4, "the nearest gap q to the frontier is the one that reconnects it"
    assert "rival" in reason


def test_rival_gap_is_skipped_once_it_resolves_the_second_mode():
    """With the rival trajectory whole again, the frontier is back to its own same-q work."""
    grid, frontier = _rival_gap_grid()
    for index in (2, 3, 4):
        grid[index].evidence_master = {"branch_candidates": [
            {"energy_meV": 4.9, "sigma_meV": 0.05}, {"energy_meV": 7.88, "sigma_meV": 0.05}]}

    pick, kind, _reason, _score = _select_target(
        grid, _Model(), tracked_branch=True, maximum_bridge_steps=1)
    assert (pick.index, kind) == (frontier.index, "production")


def test_rival_gap_that_never_resolves_yields_the_beam_instead_of_looping():
    """A reconnect that fails to raise the mode count must not be re-picked indefinitely.

    The gap q is under-resolved precisely because it was only quicklooked, so its production count
    records how often this rung has already tried.  Without the cap, a gap whose rival mode never
    appears would be re-selected up to its own 12-production ceiling — with geometric escalation,
    billions of counts spent proving the same negative.
    """
    grid, frontier = _rival_gap_grid()
    gap_indices = {2, 3, 4}
    picks = []
    for _ in range(12):
        pick, kind, _reason, _score = _select_target(
            grid, _Model(), tracked_branch=True, maximum_bridge_steps=1)
        picks.append((pick.index, kind))
        pick.n_production += 1                  # the scan lands but the rival mode stays unfitted
        pick.production_sequences.append(100 + len(picks))

    # Each gap q is a different place the rival evidence could live, so trying each is legitimate —
    # what must not happen is any single q absorbing the beam, or the frontier never getting it.
    for index in gap_indices:
        assert grid[index].n_production <= MAX_RECONNECT_PRODUCTIONS, (
            f"gap q {index} exceeded its reconnect allowance: {picks}")
    assert picks[0][0] == 4, "the nearest gap q is tried first"
    assert picks[-1][0] == frontier.index, f"the frontier must end up with the beam: {picks}"
    gap_spend = sum(1 for index, _kind in picks if index in gap_indices)
    assert gap_spend <= MAX_RECONNECT_PRODUCTIONS * len(gap_indices)


def test_rival_gap_respects_the_gap_points_own_production_ceiling():
    grid, frontier = _rival_gap_grid()
    for index in (2, 3, 4):
        grid[index].n_production = 12               # every gap q already spent its own budget
    pick, kind, _reason, _score = _select_target(
        grid, _Model(), tracked_branch=True, maximum_bridge_steps=1)
    assert (pick.index, kind) == (frontier.index, "production")


def test_missing_frontier_identity_also_earns_the_same_q_fallback():
    """``_frontier_states`` labels the frontier ``missing`` when no candidate was selected."""
    grid = _grid()
    frontier = _op10_frontier(grid)
    frontier.track_state = "missing"
    _visit_rest(grid, 2)

    pick, kind, _reason, _score = _select_target(
        grid, _Model(), tracked_branch=True, maximum_bridge_steps=1)
    assert (pick.index, kind) == (frontier.index, "production")


def test_frontier_identity_saturated_still_earns_a_precision_scan_when_centres_are_coarse():
    """O27 at the frontier (Blocker #4). Identity saturation says counting cannot settle the branch
    *label* here; it says nothing about a *precision* scan, which asks the different question "is the
    centre measured finely enough". When the candidate centre is still coarse and production +
    exposure allowance remain, that precision production must stay eligible even though the point is
    ``identity_saturated`` — the same conflation O27 removed at the anchor, retained at the frontier."""
    grid = _grid()
    grid[0].n_quicklook = 1
    grid[0].track_state = "confirmed"
    frontier = grid[1]
    frontier.n_quicklook = 1
    frontier.n_production = 1
    frontier.max_production_batches = 12
    frontier.production_sequences = [20]
    frontier.track_state = "ambiguous"
    frontier.local_identity_clear = False
    # A single coarse candidate: sigma 0.9 meV against a 0.5 meV tolerance is precision-INADEQUATE.
    frontier.evidence_master = {
        "target_branch": "optic",
        "branch_candidates": [{"energy_meV": 7.5, "sigma_meV": 0.9}],
    }
    # Identity has stopped responding to a fourfold exposure increase → saturated (so ``stuck``).
    frontier.identity_updates = [
        {"effective_exposure": 25e6, "target_probability": 0.75, "identity_entropy": 0.56},
        {"effective_exposure": 50e6, "target_probability": 0.751, "identity_entropy": 0.559},
        {"effective_exposure": 100e6, "target_probability": 0.752, "identity_entropy": 0.558},
    ]
    assert frontier.identity_saturated and frontier.stuck
    # A newer authoritative revision → same-q evidence advanced, so a precision scan is still justified.
    _authoritative_evidence(frontier, 21)
    _visit_rest(grid, 2)   # no unvisited bridge, no broadening, no rival-gap reconnect

    pick, kind, _reason, _score = _select_target(
        grid, _Model(), tracked_branch=True, maximum_bridge_steps=1)
    assert pick is not None, (
        "a coarse frontier centre with production and exposure allowance left must not stop the "
        "campaign merely because the identity label is saturated")
    assert (pick.index, kind) == (frontier.index, "production")


def test_identity_saturation_needs_both_metrics_and_a_real_exposure_increase():
    """A partial or cheap identity history is not evidence that counting cannot help."""
    frontier = GridQ(index=1, hkl=(2.05, 0.0, 0.0), q=0.05, tolerance=0.5,
                     track_state="ambiguous", local_identity_clear=False)
    stalled = [{"effective_exposure": 25e6, "target_probability": 0.75, "identity_entropy": 0.56},
               {"effective_exposure": 50e6, "target_probability": 0.751, "identity_entropy": 0.559},
               {"effective_exposure": 100e6, "target_probability": 0.752, "identity_entropy": 0.558}]
    frontier.identity_updates = [dict(u) for u in stalled]
    assert frontier.identity_saturated

    # Only one metric available → unknown, not saturated.
    partial = [{k: v for k, v in u.items() if k != "target_probability"} for u in stalled]
    frontier.identity_updates = partial
    assert not frontier.identity_saturated

    # Exposure barely moved → the window proves nothing about counting.
    flat = [dict(u, effective_exposure=25e6) for u in stalled]
    frontier.identity_updates = flat
    assert not frontier.identity_saturated

    # Identity actually improving → keep counting.
    improving = [dict(stalled[0]),
                 dict(stalled[1], target_probability=0.85, identity_entropy=0.40),
                 dict(stalled[2], target_probability=0.93, identity_entropy=0.22)]
    frontier.identity_updates = improving
    assert not frontier.identity_saturated


def _stalled_identity_series():
    """Three comparable revisions: exposure up 4x, neither identity metric moving."""
    return [{"effective_exposure": 25e6, "target_probability": 0.75, "identity_entropy": 0.56},
            {"effective_exposure": 50e6, "target_probability": 0.751, "identity_entropy": 0.559},
            {"effective_exposure": 100e6, "target_probability": 0.752, "identity_entropy": 0.558}]


def _identity_frontier():
    return GridQ(index=1, hkl=(2.05, 0.0, 0.0), q=0.05, tolerance=0.5,
                 track_state="ambiguous", local_identity_clear=False)


def test_identity_saturation_holds_within_one_stable_basis():
    """The positive case with a basis present: a stable candidate set and calibration across the
    window still saturates when the metrics stall under growing exposure."""
    frontier = _identity_frontier()
    frontier.identity_updates = [
        dict(u, branch_candidates=[{"energy_meV": 5.75}, {"energy_meV": 7.11}],
             calibration_version="cal-1")
        for u in _stalled_identity_series()]
    assert frontier.identity_saturated
    assert not frontier.identity_basis_changed


def test_a_changed_candidate_set_is_not_identity_saturation():
    """Blocker #3. If a second candidate appears (or one disappears) inside the window, the last
    revisions answer a different candidate-ownership question than the first, so worsening/stalled
    metrics across them are not evidence that counting has stopped paying."""
    frontier = _identity_frontier()
    series = _stalled_identity_series()
    frontier.identity_updates = [
        dict(series[0], branch_candidates=[{"energy_meV": 5.75}]),
        dict(series[1], branch_candidates=[{"energy_meV": 5.75}]),
        # A second candidate appeared: this is a different scientific question, not stalled counting.
        dict(series[2], branch_candidates=[{"energy_meV": 5.75}, {"energy_meV": 7.11}]),
    ]
    assert not frontier.identity_saturated
    assert frontier.identity_basis_changed


def test_a_recalibration_mid_window_is_not_identity_saturation():
    """A calibration change makes the older revisions incomparable to the newest — the label may be
    settling under the new calibration, so counting is not proven wasted."""
    frontier = _identity_frontier()
    series = _stalled_identity_series()
    frontier.identity_updates = [
        dict(series[0], calibration_version="cal-1"),
        dict(series[1], calibration_version="cal-1"),
        dict(series[2], calibration_version="cal-2"),
    ]
    assert not frontier.identity_saturated
    assert frontier.identity_basis_changed


def test_entirely_replaced_candidate_sets_are_not_one_comparable_question():
    """The review reproduction. The basis recorded only the candidate *count*, so three windows whose
    two candidates were entirely different modes each time still read as one comparable question and
    saturated -- exactly the phantom the basis exists to prevent, just one level down."""
    frontier = _identity_frontier()
    series = _stalled_identity_series()
    frontier.identity_updates = [
        dict(series[0], branch_candidates=[{"energy_meV": 1.0}, {"energy_meV": 2.0}]),
        dict(series[1], branch_candidates=[{"energy_meV": 9.0}, {"energy_meV": 12.0}]),
        dict(series[2], branch_candidates=[{"energy_meV": 20.0}, {"energy_meV": 25.0}]),
    ]
    assert not frontier.identity_saturated
    assert frontier.identity_basis_changed


def test_refit_jitter_below_tolerance_stays_comparable():
    """The counterpart the basis must not over-fire on: the same two modes re-fit slightly differently
    are the same question. A quantized key would have reset the window at whichever bin edge the
    energies happened to straddle; matched movement against the q's own tolerance has no edges."""
    frontier = _identity_frontier()
    series = _stalled_identity_series()
    jitter = (0.0, 0.03, -0.04)
    frontier.identity_updates = [
        dict(update, branch_candidates=[{"energy_meV": 5.75 + shift},
                                        {"energy_meV": 7.11 - shift}])
        for update, shift in zip(series, jitter)]
    assert frontier.identity_saturated
    assert not frontier.identity_basis_changed


def test_candidates_straddling_a_bin_edge_are_still_comparable():
    """Directly pins the reason for a matched predicate over absolute quantization: two revisions
    0.02 meV apart must never be incomparable merely because a bin boundary fell between them."""
    frontier = _identity_frontier()
    series = _stalled_identity_series()
    frontier.identity_updates = [
        dict(series[0], branch_candidates=[{"energy_meV": 4.999}]),
        dict(series[1], branch_candidates=[{"energy_meV": 5.001}]),
        dict(series[2], branch_candidates=[{"energy_meV": 5.010}]),
    ]
    assert not frontier.identity_basis_changed


def test_a_target_selection_flip_resets_the_window_as_its_own_epoch():
    """A selection flip is a changed *answer*, not a changed candidate set. It must still reset the
    saturation window -- the newest revisions are about a different target -- but as an explicit
    identity epoch rather than by quietly redefining what a candidate-basis change means."""
    frontier = _identity_frontier()
    series = _stalled_identity_series()
    candidates = [{"energy_meV": 5.75}, {"energy_meV": 7.11}]
    frontier.identity_updates = [
        dict(series[0], branch_candidates=candidates, identity_epoch=3),
        dict(series[1], branch_candidates=candidates, identity_epoch=3),
        dict(series[2], branch_candidates=candidates, identity_epoch=4),
    ]
    assert not frontier.identity_saturated
    assert frontier.identity_basis_changed


def test_a_hypothesis_or_slice_change_is_a_different_question():
    """Both fields were declared in the basis and never written by the live producer, so they proved
    nothing. Pinned here so the producer wiring cannot regress silently."""
    frontier = _identity_frontier()
    series = _stalled_identity_series()
    for field in ("hypotheses_fingerprint", "campaign_slice_fingerprint"):
        frontier.identity_updates = [
            dict(series[0], **{field: "a"}),
            dict(series[1], **{field: "a"}),
            dict(series[2], **{field: "b"}),
        ]
        assert not frontier.identity_saturated, field
        assert frontier.identity_basis_changed, field


def test_same_q_budget_is_the_production_ceiling_not_the_bridge_allowance():
    grid = _grid()
    frontier = _op10_frontier(grid, n_production=11)
    frontier.max_production_batches = 12
    _visit_rest(grid, 2)

    pick, kind, _reason, _score = _select_target(
        grid, _Model(), tracked_branch=True, maximum_bridge_steps=1)
    assert (pick.index, kind) == (frontier.index, "production"), (
        "eleven same-q productions against a ceiling of twelve must leave one more")

    frontier.n_production = 12
    frontier.production_sequences = [20] * 12
    pick, _kind, _reason, _score = _select_target(
        grid, _Model(), tracked_branch=True, maximum_bridge_steps=1)
    assert pick is None or pick.index != frontier.index


def test_the_identity_epoch_follows_the_selected_mode_not_its_id_or_index():
    """The epoch has to survive re-pooling. ``candidate_ref`` is re-minted when a master gains a
    member and ``selected_candidate_index`` follows the table's order, so keying the selection on
    either would report a flip on an unchanged selection and reset the saturation window forever."""
    from isar.drive.loop import _identity_epoch, _target_selection_rank

    def row(selected, energies, refs, epoch=None):
        out = {"kind": "pooled_master_update", "hkl": [2.9, 0.0, 0.0],
               "selected_candidate_index": selected,
               "branch_candidates": [{"energy_meV": e, "candidate_ref": r}
                                     for e, r in zip(energies, refs)]}
        if epoch is not None:
            out["identity_epoch"] = epoch
        return out

    # Same mode selected, but re-pooled: new refs, and the table came back in the other order.
    before = row(0, [5.75, 7.11], ["s1:det:mode:0", "s1:det:mode:1"], epoch=2)
    after = row(1, [7.11, 5.75], ["s2:det:mode:0", "s2:det:mode:1"])
    assert _target_selection_rank(before) == _target_selection_rank(after) == 0
    assert _identity_epoch(after, [before]) == 2, "an unchanged selection must not advance the epoch"

    # A genuine flip to the other mode does advance it.
    flipped = row(0, [7.11, 5.75], ["s2:det:mode:0", "s2:det:mode:1"])
    assert _identity_epoch(flipped, [before]) == 3

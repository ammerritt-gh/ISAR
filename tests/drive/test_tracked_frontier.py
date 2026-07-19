from __future__ import annotations

from isar.drive.acquisition.acquire import _select_target, propose_scan, tracked_anchor_index
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

    pick, kind, reason, _score = _pick(grid)
    assert (pick.index, kind) == (2, "quicklook")
    assert "bridge" in reason

    grid[2].n_quicklook = 1
    grid[2].track_state = "provisional"
    pick, kind, reason, _score = _pick(grid)
    assert (pick.index, kind) == (1, "production")
    assert "revisit" in reason


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
    assert "not yet confirmed" in reason


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

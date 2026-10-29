"""q-links and curvature from provider output (C-q-provider into C-graph), and the reasons a missing q part gives
in the preview (C-preview). Workspaces are generated at test time; every number is made up (hard rule 10)."""
from __future__ import annotations

from pathlib import Path

import pytest

import isar
import intake_fixtures as fx
from bl43_fixture import ANALYZERS
from inter_fixtures import build_inter_workspace, scan_keys
from inter_q_fixtures import epoch, set_epochs
from inter_synthetic import DETS, build, point, scan_nodes
from isar.intake.gpi import check_gpi
from isar.session.inter_spectra.layout import BL43_GRID
from isar.session.inter_spectra.preview import preview_anchored, preview_simultaneous
from isar.session.inter_spectra.q_provider import q_coordinates
from isar.session.inter_spectra.rings import plan_anchored
from isar.session.inter_spectra.schedule import plan_simultaneous
from isar.session.workspace import Workspace

REPO = Path(__file__).resolve().parents[3]
N = 28
SET = "CsV3Sb5_100K_0p5_0p5_3_sum"            # the set name the inter fixture's analyzer files carry
NUMBERS = ANALYZERS[:N]


def _key(scan, det):
    return f"{scan}:{det}"


def test_import_root_is_the_worktree():
    assert Path(isar.__file__).resolve().parents[1] == REPO


@pytest.fixture
def grid_root(tmp_path):
    """One scan of 28 analyzers whose gpi hkl sit on the BL43 grid: row r, column c -> (0.5 + 0.1c, 0.5 + 0.1r, 3)."""
    root = build_inter_workspace(tmp_path, temperatures=(100,), setups=("0p5_0p5_3",), n_analyzers=N, fits=False)
    incoming = root.parent / "incoming"
    q_nm = {f"a{n:02d}": 20.0 + n / 10 for n in NUMBERS}      # the Q the fixture's analyzer files state
    hkl = {d: (0.5 + 0.1 * c, 0.5 + 0.1 * r, 3.0) for r, row in enumerate(BL43_GRID.rows) for c, d in enumerate(row)}
    fx.write_gpi(incoming, SET, q_nm=q_nm, hkl=hkl)
    card, _session = Workspace.open(root).load()
    assert [e.accepted for e in check_gpi(root, card, [incoming], by="import").scans.values()] == [True]
    return root


def _ring_of_a09(root):
    """The two-anchor example of C-graph: anchors a03 and a41 of one scan, on the provider's coordinates."""
    scan = scan_keys(root)[0]
    qc = q_coordinates(root, [_key(scan, d) for d in DETS])
    g = build(scan_nodes(scan), coords=qc.points)
    return g, plan_anchored(g, [_key(scan, "a03"), _key(scan, "a41")], [], []), scan


def test_two_anchors_meet_where_the_grid_says_on_the_gpi_hkl(grid_root):
    g, plan, scan = _ring_of_a09(grid_root)
    assert g.metric == {scan: "hkl"}
    assert plan.ring_of[_key(scan, "a09")] == 3 and plan.fronts[_key(scan, "a09")] == (_key(scan, "a41"),)


def test_the_same_anchors_meet_where_the_grid_says_under_a_lattice_epoch(grid_root):
    set_epochs(grid_root, epoch(1, a=4.0, c=7.0, gamma=120.0))
    g, plan, scan = _ring_of_a09(grid_root)
    assert g.metric == {scan: "lattice"}
    assert plan.ring_of[_key(scan, "a09")] == 3 and plan.fronts[_key(scan, "a09")] == (_key(scan, "a41"),)


# ---------------------------------------------------------------------------
# the reason a missing q part gives, through the preview (synthetic coordinates)
# ---------------------------------------------------------------------------
def test_an_instrument_without_an_analyzer_layout_says_so_in_the_preview():
    nodes = scan_nodes(dets=("a03", "a04"))
    coords = {"E:10:a03": point((0, 0, 0)), "E:10:a04": point((0.1, 0, 0))}
    g = build(nodes, coords=coords, grid=None)
    pv = preview_anchored(g, plan_anchored(g, ["E:10:a03"], [], []), "q")
    assert not pv.can_start and "no analyzer layout" in pv.blocked


def test_accepted_hkl_with_no_adjacent_partner_says_so_in_the_preview():
    nodes = scan_nodes(dets=("a03", "a25"))                  # rows 0 and 2: not adjacent on the grid
    coords = {"E:10:a03": point((0, 0, 0)), "E:10:a25": point((0.2, 0, 0))}
    g = build(nodes, coords=coords)
    pv = preview_anchored(g, plan_anchored(g, ["E:10:a03"], [], []), "q")
    assert not pv.can_start and "adjacent partner" in pv.blocked


def test_a_missing_hkl_says_so_in_the_preview_and_the_qT_note_names_it():
    nodes = scan_nodes(dets=("a03", "a04"))
    coords = {"E:10:a03": point((0, 0, 0))}                  # a04 has no accepted hkl
    g = build(nodes, coords=coords)
    pv = preview_anchored(g, plan_anchored(g, ["E:10:a03"], [], []), "q")
    assert "accepted hkl" in pv.blocked
    pv_qT = preview_anchored(g, plan_anchored(g, ["E:10:a03"], [], []), "qT")
    assert "q-links unavailable: no analyzer of the scan has an accepted hkl; spreading in T only" in pv_qT.notes


def test_two_q_linked_spectra_with_no_curvature_say_so_in_a_simultaneous_preview():
    nodes = scan_nodes(dets=("a03", "a04"))
    coords = {"E:10:a03": point((0, 0, 0)), "E:10:a04": point((0.1, 0, 0))}
    g = build(nodes, coords=coords, kinds=("q", "T"))
    pv = preview_simultaneous(g, plan_simultaneous(g, ()))
    assert not pv.can_start and "q-linked set has no curvature term (two spectra)" in pv.blocked


def test_a_simultaneous_spectrum_without_hkl_says_so_in_the_preview():
    nodes = scan_nodes(dets=("a03", "a04"))
    coords = {"E:10:a03": point((0, 0, 0))}
    g = build(nodes, coords=coords, kinds=("q", "T"))
    pv = preview_simultaneous(g, plan_simultaneous(g, ()))
    assert not pv.can_start and "no accepted hkl" in pv.blocked

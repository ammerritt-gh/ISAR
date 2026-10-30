"""The analyzer array's Qt-free read (C-array-view) and the BL43LXU layout it reads from the shipped card.

Every number is made up (hard rule 10): hkl (0.5 + 0.1c, 0.5 + 0.1r, 3.0) on the grid, as the graph tests use.
The real-read case builds its workspace at test time.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import intake_fixtures as fx
from bl43_fixture import ANALYZERS
from inter_fixtures import build_inter_workspace, scan_keys
from isar.intake.gpi import check_gpi
from isar.schema import card as card_mod
from isar.session.inter_spectra.layout import BL43_GRID, grid_for
from isar.session.inter_spectra.q_provider import q_coordinates
from isar.session.workspace import Workspace as SessionWorkspace
from isar.workspace_api import Workspace
from isar.workspace_api.analyzer_array import (AnalyzerLayout, StepSummary, array_view, layout_for)
from isar.workspace_api.scan_list import Fact, ScanListSnapshot, ScanSetRow, SpectrumRow, Status, scan_list

BEAMLINE = tuple(name for row in BL43_GRID.rows for name in row)      # row A first, position 1 first
OK = Status("ok", True)
N = 28
SET = "CsV3Sb5_100K_0p5_0p5_3_sum"
NUMBERS = ANALYZERS[:N]


def _policy():
    return card_mod.load_policy(card_mod.SHIPPED_FORMATS["sp8_bl43"])


def _grid_hkl(beam):
    r, c = BL43_GRID.position(beam)
    return (0.5 + 0.1 * c, 0.5 + 0.1 * r, 3.0)


def _spectrum(beam, hkl):
    q = (Fact(hkl, "rlu", "gpi", "file") if hkl is not None
         else Fact(None, "rlu", "missing", "missing", "no hkl in the file"))
    return SpectrumRow(key=f"e:13:{beam}", set_key="e:13", analyzer=beam, q=q, parsed=OK, fast=OK, deep=OK,
                       file_name=None, file_path=None, n_points=None, q_abs=Fact(20.0, "nm⁻¹", "file", "file"))


def _spectra(drop=(), no_hkl=()):
    return [_spectrum(b, None if b in no_hkl else _grid_hkl(b)) for b in BEAMLINE if b not in drop]


def _view(spectra, policy=None, scans=()):
    row = ScanSetRow(key="e:13", label="13", experiment_id="e", scan_numbers=(13,), summed=False, n_merged=None,
                     temperature=Fact(100.0, "K", "file", "file"), slit=Fact(None, None, "missing", "missing"),
                     spectra=tuple(spectra))
    ws = SimpleNamespace(scans=list(scans), card=SimpleNamespace(policy=policy or _policy()))
    return array_view(ScanListSnapshot(sets=(row,)), "e:13", ws)


def test_the_shipped_card_carries_the_one_bl43_layout_and_validates():
    policy = _policy()
    choice = layout_for(policy)
    assert choice.how == "the only BL43LXU layout"
    assert choice.layout == AnalyzerLayout(id="BL43LXU-7x4", rows=BL43_GRID.rows)
    assert card_mod.Card(policy, card_mod.CardState()).validate() == []


def test_grid_for_bl43_is_the_card_layout_and_other_plugins_have_none():
    assert grid_for("sp8_bl43") == BL43_GRID
    assert grid_for("spec_s30") is None


def test_the_pinned_positions_seen_from_the_sample():
    view = _view(_spectra())
    cells = {c.beamline: c for c in view.cells}
    # (internal name, screen row, screen column); screen row 0 is the top, column 0 the left, seen from the sample
    assert (cells["a03"].internal, cells["a03"].screen_row, cells["a03"].screen_col) == ("A1", 3, 6)   # bottom right
    assert (cells["a09"].internal, cells["a09"].screen_row, cells["a09"].screen_col) == ("A7", 3, 0)   # bottom left
    assert (cells["a41"].internal, cells["a41"].screen_row, cells["a41"].screen_col) == ("D7", 0, 0)   # top left
    assert cells["a06"].internal == "A4" and cells["a06"].key == "e:13:a06"
    assert [c.beamline for c in view.cells[:2]] == ["a03", "a04"]     # layout order: row A, position 1 first


def test_a_full_scan_steps_are_the_grid_steps():
    view = _view(_spectra())
    assert (view.in_plane.used, view.in_plane.possible) == (24, 24)
    assert (view.out_of_plane.used, view.out_of_plane.possible) == (21, 21)
    assert view.in_plane.mean == pytest.approx((0.1, 0.0, 0.0))
    assert view.out_of_plane.mean == pytest.approx((0.0, 0.1, 0.0))
    first = view.in_plane.pairs[0]
    assert (first.a, first.b) == ("A1", "A2") and first.delta == pytest.approx((0.1, 0.0, 0.0))


def test_an_old_six_column_scan_draws_the_missing_column_as_no_file():
    view = _view(_spectra(drop=("a09", "a20", "a31", "a41")))
    gone = [c for c in view.cells if c.key is None]
    assert {c.beamline for c in gone} == {"a09", "a20", "a31", "a41"}
    assert all(c.hkl.value is None and c.hkl.reason and c.q_abs.value is None for c in gone)
    assert (view.in_plane.used, view.in_plane.possible) == (20, 24)
    assert (view.out_of_plane.used, view.out_of_plane.possible) == (18, 21)
    assert view.unplaced == ()


def test_one_missing_hkl_drops_only_the_pairs_it_is_in():
    view = _view(_spectra(no_hkl=("a06",)))
    assert (view.in_plane.used, view.out_of_plane.used) == (22, 20)     # (A3,A4), (A4,A5); (A4,B4)
    a06 = view.cell_for("e:13:a06")
    assert a06.hkl.value is None and a06.hkl.reason


def test_no_usable_pair_gives_no_mean():
    view = _view(_spectra(no_hkl=BEAMLINE))
    assert view.in_plane == StepSummary(None, 0, 24, ())
    assert view.out_of_plane == StepSummary(None, 0, 21, ())


def test_an_instrument_without_a_layout_has_no_cells_and_lists_every_spectrum():
    s30 = SimpleNamespace(instrument="S30", analyzer_layouts=[])
    view = _view([_spectrum("ana1", (1.0, 0.0, 0.0)), _spectrum("ana2", (1.0, 1.0, 0.0))], policy=s30)
    assert view.layout is None and view.cells == ()
    assert view.how == "no analyzer layout for S30"
    assert view.unplaced == ("e:13:ana1", "e:13:ana2")
    assert view.in_plane == StepSummary(None, 0, 0, ()) and view.out_of_plane == StepSummary(None, 0, 0, ())


def test_a_spectrum_outside_the_layout_is_listed_and_two_layouts_guess_nothing():
    view = _view(_spectra() + [_spectrum("a99", (1.0, 1.0, 1.0))])
    assert view.unplaced == ("e:13:a99",)
    entry = _policy().analyzer_layouts[0]
    two = SimpleNamespace(instrument="BL43LXU", analyzer_layouts=[entry, dict(entry, id="other")])
    guessed = _view(_spectra(), policy=two)
    assert guessed.layout is None and guessed.how == "layout unknown" and guessed.cells == ()
    assert len(guessed.unplaced) == N


def test_the_header_names_the_scan_and_says_what_is_missing():
    scans = [SimpleNamespace(scan_id="e:13:a06",
                             provenance={"scan_facts": {"hkl_nominal": {"value": [0.5, 0.5, 3.0], "source": "filename"}}})]
    h = _view(_spectra(), scans=scans).header
    assert (h.experiment_id, h.scan_label, h.scan_numbers, h.n_spectra) == ("e", "13", (13,), N)
    assert h.temperature == Fact(100.0, "K", "file", "file")
    assert h.hkl_nominal == Fact((0.5, 0.5, 3.0), "rlu", "filename", "file")
    assert h.sample == Fact(None, None, "missing", "missing", "no sample name in the card")
    named = SimpleNamespace(instrument="BL43LXU", analyzer_layouts=_policy().analyzer_layouts, sample={"name": "CsV3Sb5"})
    assert _view(_spectra(), policy=named).header.sample == Fact("CsV3Sb5", None, "card", "card")
    assert _view(_spectra()).header.hkl_nominal.value is None
    assert array_view(ScanListSnapshot(), "e:9", SimpleNamespace(scans=[])) is None


@pytest.fixture
def grid_root(tmp_path):
    """One scan of 28 analyzers whose gpi hkl sit on the BL43 grid, as the graph tests build it."""
    root = build_inter_workspace(tmp_path, temperatures=(100,), setups=("0p5_0p5_3",), n_analyzers=N, fits=False)
    incoming = root.parent / "incoming"
    q_nm = {f"a{n:02d}": 20.0 + n / 10 for n in NUMBERS}
    hkl = {d: _grid_hkl(d) for d in BEAMLINE}
    fx.write_gpi(incoming, SET, q_nm=q_nm, hkl=hkl)
    card, _session = SessionWorkspace.open(root).load()
    assert [e.accepted for e in check_gpi(root, card, [incoming], by="import").scans.values()] == [True]
    return root


def test_real_read_cells_carry_the_providers_accepted_hkl(grid_root):
    ws = Workspace.open(grid_root)
    scan = scan_keys(grid_root)[0]
    view = array_view(scan_list(ws), scan, ws)
    assert view.layout.id == "BL43LXU-7x4" and len(view.cells) == N
    qc = q_coordinates(grid_root, [c.key for c in view.cells])
    for cell in view.cells:
        assert cell.key in qc.points, cell.key
        assert cell.hkl.value == pytest.approx(qc.points[cell.key].hkl)
    assert (view.in_plane.used, view.out_of_plane.used) == (24, 21)
    assert view.in_plane.mean == pytest.approx((0.1, 0.0, 0.0))
    assert view.out_of_plane.mean == pytest.approx((0.0, 0.1, 0.0))

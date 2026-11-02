"""The analyzer path, read (C-path): stops placed at cumulative distance, the fit in use drawn per stop, link names and
markers only while they name that fit, and the plain reasons a path is not available.

Every number is made up (hard rule 10): the q points are supplied by a fake provider and the workspace is generated
at test time. Prevents: a stop drawn from the wrong fit after a pick, a broad mode's marker placed at its omega0
instead of E*, a stale or other-fit link drawn as current, and a picked analyzer of another scan silently plotted.
"""
from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path

import pytest

import isar.workspace_api.analyzer_path as apath
from inter_fixtures import build_inter_workspace, spectrum_keys
from isar.intake import gpi, inputs, inter_values, scans as intake_scans, store
from isar.intake.inter_spectra import read_user
from isar.schema.card import load_state
from isar.session import writer
from isar.session.inter_spectra import records
from isar.session.inter_spectra.q_provider import QCoordinates, QPoint
from isar.workspace_api.analyzer_path import analyzer_path
from isar.workspace_api.inter_spectra import LINK_WORDS, LinkView, MarkerView, UnitLinks, inter_overview
from isar.workspace_api.scan_list import scan_list
from isar.workspace_api.spectra import spectrum_bundles
from isar.workspace_api.workspace import Workspace

B1 = "b000000000001"
F1 = "f00000000a001"
LINK_AT = "2026-11-02T10:00:00+00:00"


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    return build_inter_workspace(tmp_path_factory.mktemp("path"), temperatures=(100, 200), setups=("0p5_0p5_3",),
                                 n_analyzers=3)


@pytest.fixture
def root(base, tmp_path):
    return Path(shutil.copytree(base, tmp_path / "ws"))


@pytest.fixture
def q_points(monkeypatch):
    """Replace the graph's q provider: one point per requested key, from ``carts`` (default: 0.1 apart along a)."""
    carts: dict[str, tuple[float, float, float]] = {}

    def fake(root, keys):
        points = {k: QPoint(hkl=(0.5, 0.5, 3.0), source="gpi",
                            cart=carts.get(k, (0.1 * i, 0.0, 0.0)), epoch="lattice epoch v1")
                  for i, k in enumerate(keys)}
        return QCoordinates(points=points, reasons={}, lattices={})

    monkeypatch.setattr(apath, "q_coordinates", fake)
    return carts


def _snapshot(root):
    ws = Workspace.open(root)
    return ws, scan_list(ws)


def _accept_refit(root, unit: str, fit: str) -> None:
    """One accepted refit of ``unit`` through the real batch writer, fitted on the inputs now (as test_link_view does)."""
    read = store.read_all(root, store.ENTRIES)
    now = records.inputs_now_from(intake_scans.read_scans(root), inputs.index_entries(read.entries, lines=read.lines),
                                  load_state(root / "card.state.json").tabulated_calibration, [unit],
                                  gpi=gpi.entries_in_force(root), data_dir=root / "data")[unit]

    def payload(event, body):
        return {"schema": records.SCHEMA_SAVE, "unit": f"inter:{B1}:batch", "pass": "inter", "batch": B1,
                "event": event, event: body}

    start = {"batch_kind": "anchored", "scope": {"direction": "q", "members": [unit]}, "anchors": [], "rings": {},
             "seams": [], "settings": {"version": 1, "values": {}}, "preview": "t", "fixed": {}, "unreached": {},
             "terms": [], "metric": {}, "core": "t", "inputs_at": 0, "intake_seq": read_user(root).max_seq, "epochs": {}}
    refit = {"fit": fit, "unit": unit, "status": "accepted", "reason": None, "seed": "fast", "inputs": now,
             "conditioned_on": [], "flag": None, "why_changed": None,
             "payload": {"came_in": {}, "came_out": {"errors": "conditional"},
                         "overlay": {"n_modes": 2, "omega0": 3.5, "best_label": "2 modes", "interval": [3.4, 3.6],
                                     "modes": [{"omega0": 3.5, "omega0_err": 0.1, "gamma": 0.4, "amplitude": 50.0},
                                               {"omega0": 7.5, "omega0_err": 0.2, "gamma": 0.6, "amplitude": 20.0}]}}}
    stage = {"stage": "ring", "index": 1, "round": None, "refits": [refit], "seams": [],
             "progress": {"done": 1, "failed": 0, "not_reached": 0, "fixed": 0, "total": 1}, "not_reached": {}}
    out = writer.save_inter(root, [payload("start", start), payload("stage", stage)])
    assert all(isinstance(v, dict) for v in out), out


def _energies(stop) -> list[float]:
    return [m.energy for m in stop.modes]


def test_the_worked_example_places_the_stops_at_cumulative_distance(root, q_points):
    ws, snap = _snapshot(root)
    k = spectrum_keys(root)[:3]
    q_points.update({k[0]: (0.0, 0.0, 0.0), k[1]: (0.1, 0.0, 0.0), k[2]: (0.1, 0.2, 0.0)})
    view = analyzer_path(snap, ws, k)
    assert (view.ok, view.metric, view.unit, view.reason) == (True, "lattice", "Å⁻¹", None)
    assert [s.key for s in view.stops] == k                             # the path is the click order
    assert [s.x for s in view.stops] == pytest.approx([0.0, 0.1, 0.3])
    assert [s.beamline for s in view.stops] == [key.rsplit(":", 1)[1] for key in k]
    assert view.set_key == k[0].rsplit(":", 1)[0] and view.skipped == ()


def test_fewer_than_two_spectra_is_a_plain_reason_and_no_path(root, q_points):
    ws, snap = _snapshot(root)
    k = spectrum_keys(root)[0]
    view = analyzer_path(snap, ws, [k, k])                               # a duplicate is not a second analyzer
    assert (view.ok, view.stops) == (False, ())
    assert view.reason == "pick at least two analyzers of one scan (1 picked)"


def test_keys_of_another_scan_are_skipped_and_named(root, q_points):
    ws, snap = _snapshot(root)
    keys = spectrum_keys(root)
    same_scan, other = keys[:2], keys[3]                                 # keys[3] is the first analyzer of the next scan
    view = analyzer_path(snap, ws, [same_scan[0], other, same_scan[1]])
    assert [s.key for s in view.stops] == same_scan
    assert view.skipped == (other,)


def test_a_refit_pick_is_drawn_with_its_own_modes(root, q_points):
    k0, k1 = spectrum_keys(root)[:2]
    _accept_refit(root, k0, F1)
    assert inter_values.pick_fit(root, k0, F1).ok
    ws, snap = _snapshot(root)
    rows = inter_overview(root).by_key                                  # the fit in use, as the view model reads it
    assert rows[k0].fit_id == F1 and k1 not in rows                     # no row: the Fast solo
    solo = spectrum_bundles(ws, [snap.spectrum(k1)])[0].summaries["fast"]
    view = analyzer_path(snap, ws, [k0, k1], fit_choice={k0: F1})
    assert _energies(view.stops[0]) == [3.5, 7.5]                       # the refit's modes, not the solo's
    assert _energies(view.stops[1]) == [m.energy.value for m in solo.modes]


def test_the_path_draws_only_the_fit_it_is_given(root, q_points):
    k0, k1 = spectrum_keys(root)[:2]
    _accept_refit(root, k0, F1)
    assert inter_values.pick_fit(root, k0, F1).ok
    ws, snap = _snapshot(root)
    solo = spectrum_bundles(ws, [snap.spectrum(k0)])[0].summaries["fast"]
    view = analyzer_path(snap, ws, [k0, k1])                            # no choice: the Fast solo, though F1 is picked
    assert _energies(view.stops[0]) == [m.energy.value for m in solo.modes]


def test_a_broad_mode_is_marked_at_its_peak_energy_not_its_omega0(root, q_points):
    ws, snap = _snapshot(root)
    k0, k1 = spectrum_keys(root)[:2]
    omega0 = [m.energy.value for m in spectrum_bundles(ws, [snap.spectrum(k0)])[0].summaries["fast"].modes]
    mark = MarkerView("unlinked", 3, 0, None, LINK_WORDS["unlinked"], "Mode 1 follows no neighbour", "warn")
    ul = UnitLinks(unit=k0, link_id="l1", at=LINK_AT, stale=False, stale_text=None, current=True, fit_id="fast",
                   names=(None,), peak_E=(omega0[0] - 1.0,), unresolved=((),), settled=(False,), markers=(mark,),
                   changes=(), ties=())
    links = LinkView("r", {k0: ul}, {})
    view = analyzer_path(snap, ws, [k0, k1], links=links)
    mode = view.stops[0].modes[0]
    assert (mode.energy, mode.peak_E) == (pytest.approx(omega0[0]), pytest.approx(omega0[0] - 1.0))
    assert mode.name is None
    assert view.stops[1].modes[0].peak_E is None and view.links_at == LINK_AT and not view.stale


def test_names_are_drawn_only_while_they_name_the_fit_in_use(root, q_points):
    ws, snap = _snapshot(root)
    k0, k1 = spectrum_keys(root)[:2]
    e0 = spectrum_bundles(ws, [snap.spectrum(k0)])[0].summaries["fast"].modes[0].energy.value
    mark = MarkerView("unlinked", 3, 0, None, LINK_WORDS["unlinked"], "no link", "warn")
    ul = UnitLinks(unit=k0, link_id="l1", at=LINK_AT, stale=False, stale_text=None, current=True, fit_id="fast",
                   names=("B1",), peak_E=(e0,), unresolved=((),), settled=(True,), markers=(mark,),
                   changes=(), ties=())
    stale = replace(ul, stale=True, stale_text=LINK_WORDS["stale"], markers=())   # its names stay, its markers go
    other_fit = replace(ul, fit_id=F1)                                             # the stop draws the fast fit
    cases = {"current": (ul, False, "B1", e0), "stale": (stale, True, "B1", None),
             "other fit": (other_fit, False, None, None)}
    for name, (entry, is_stale, expect_name, expect_peak) in cases.items():
        view = analyzer_path(snap, ws, [k0, k1], links=LinkView("r", {k0: entry}, {}))
        mode = view.stops[0].modes[0]
        assert (mode.name, mode.peak_E, view.stale) == (expect_name, expect_peak, is_stale), name
    assert view.stale_text is None
    assert analyzer_path(snap, ws, [k0, k1], links=LinkView("r", {k0: stale}, {})).stale_text == LINK_WORDS["stale"]


def test_a_picked_refit_that_cannot_be_drawn_is_noted_and_its_names_are_not_drawn(root, q_points, monkeypatch):
    ws, snap = _snapshot(root)
    k0, k1 = spectrum_keys(root)[:2]
    _accept_refit(root, k0, F1)
    assert inter_values.pick_fit(root, k0, F1).ok
    monkeypatch.setattr("isar.workspace_api.spectra.fit_views", lambda w, fits: {u: (None, "Refit") for u in fits})
    e0 = spectrum_bundles(ws, [snap.spectrum(k0)])[0].summaries["fast"].modes[0].energy.value
    mark = MarkerView("unlinked", 3, 0, None, LINK_WORDS["unlinked"], "no link", "warn")
    ul = UnitLinks(unit=k0, link_id="l1", at=LINK_AT, stale=False, stale_text=None, current=True, fit_id=F1,
                   names=("B1",), peak_E=(e0,), unresolved=((),), settled=(True,), markers=(mark,),
                   changes=(), ties=())
    view = analyzer_path(snap, ws, [k0, k1], links=LinkView("r", {k0: ul}, {}), fit_choice={k0: F1})
    stop = view.stops[0]
    assert stop.note == f"fit {F1} cannot be drawn; showing the Fast solo fit, without phonon names"
    assert _energies(stop) == _energies(analyzer_path(snap, ws, [k0, k1]).stops[0])   # the solo's modes
    assert all(m.name is None and m.peak_E is None for m in stop.modes)
    assert view.stops[1].note is None

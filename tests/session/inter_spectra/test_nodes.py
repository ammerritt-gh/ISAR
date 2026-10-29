"""Spectrum nodes, series keys and temperatures from a BL43-shaped workspace (C-keys).

The workspace is generated at test time (tests/inter_fixtures.py): no real data is read.
"""
from __future__ import annotations

import shutil

import pytest

from inter_fixtures import build_inter_workspace
from isar.intake.values import save_value
from isar.session.inter_spectra.nodes import read_nodes, series_key
from isar.session.workspace import Workspace


@pytest.fixture(scope="module")
def ws3(tmp_path_factory):
    """Two q settings x three temperatures x three analyzers, stamped with fast solo fits."""
    return build_inter_workspace(tmp_path_factory.mktemp("nodes") / "root", n_analyzers=3)


def _copy(ws3, tmp_path):
    ws = tmp_path / "ws"
    shutil.copytree(ws3, ws)
    return ws


def test_two_series_ordered_by_temperature(ws3):
    ns = read_nodes(ws3)
    assert len(ns.nodes) == 2 * 3 * 3
    assert len(ns.series) == 2
    for scans in ns.series.values():
        assert [ns.nodes[f"{s}:a03"].T for s in scans] == [100.0, 150.0, 200.0]
    assert all(n.series is not None and n.eligible and n.reason is None for n in ns.nodes.values())
    assert ns.repeats == ()


def test_user_temperature_moves_the_scan_in_its_series(ws3, tmp_path):
    ws = _copy(ws3, tmp_path)
    ns = read_nodes(ws)
    series = next(iter(ns.series))
    scans = ns.series[series]
    first = scans[0]                                   # the 100 K scan
    save_value(ws, "temperature", 175.0, [first])
    moved = read_nodes(ws)
    assert moved.nodes[f"{first}:a03"].T == 175.0
    assert moved.series[series].index(first) == 1      # 150 K, 175 K (the user's), 200 K


def test_repeated_temperature_is_listed(ws3, tmp_path):
    ws = _copy(ws3, tmp_path)
    scans = next(iter(read_nodes(ws).series.values()))
    save_value(ws, "temperature", 100.0, [scans[2]])   # the 200 K scan now reads 100 K: a repeat slot
    ns = read_nodes(ws)
    assert ns.nodes[f"{scans[2]}:a03"].T == 100.0
    assert len(ns.repeats) == 1 and "100 K holds 2 scans" in ns.repeats[0]


def test_ledger_state_gives_the_reason(ws3, tmp_path):
    ws = _copy(ws3, tmp_path)
    scans = next(iter(read_nodes(ws).series.values()))
    gone, held = scans[0] + ":a03", scans[1] + ":a03"
    card, session = Workspace.open(ws).load()
    del session.scans[gone]
    session.scans[held].status = "held_for_calibration"
    session.scans[held].reasons = ["waiting for a calibration"]
    Workspace.open(ws).save(card, session)
    ns = read_nodes(ws)
    assert (ns.nodes[gone].eligible, ns.nodes[gone].reason) == (False, "not fitted yet")
    assert (ns.nodes[held].eligible, ns.nodes[held].reason) == (False, "waiting for a calibration")


def test_series_key_is_the_q_setting_and_nothing_else():
    hkl = {"facts": {"hkl_nominal": {"value": [0.5, 0.5, 3.0]}}, "analyzers": {}}
    assert series_key(hkl, sample=None, hkl_step=0.05) == "|q:10,10,60@0.05"
    assert series_key(hkl, sample="CsV3Sb5", hkl_step=0.05) == "CsV3Sb5|q:10,10,60@0.05"
    other = {"facts": {"hkl_nominal": {"value": [0.3, 0.3, 2.0]}}, "analyzers": {}}
    assert series_key(other, sample=None, hkl_step=0.05) != series_key(hkl, sample=None, hkl_step=0.05)


def test_series_key_falls_back_to_the_analyzer_q_then_to_none():
    with_q = {"facts": {}, "analyzers": {"a03": {"q": [1.0, 0, 0]}, "a04": {"q": [1.1, 0, 0]}}}
    key = series_key(with_q, sample=None, hkl_step=0.05)
    assert key.startswith("aq:") and len(key) == len("aq:") + 16
    assert series_key({"facts": {}, "analyzers": {"a03": {"q": [1.0, 0, 0]}, "a04": {"q": None}}},
                      sample=None, hkl_step=0.05) is None
    assert series_key({"facts": {}, "analyzers": {}}, sample=None, hkl_step=0.05) is None

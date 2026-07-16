"""ScansViewModel × ScanFilter integration — the orthogonal filter over the shared model.

Confirms the filter shrinks the VISIBLE rows/auto-groups without touching the structural predicates
(tabs), persists through the GuiStore, and keeps the shared selection sane under narrowing.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from isar_gui.gui_store import GuiStore
from isar_gui.scan_filter import ScanFilter, parse_query
from isar_gui.viewmodels.scans import ScansViewModel


def _sv(exp, num, det, *, status="ingested", T=300.0, hkl=(1.0, 0.0, 0.0),
        source=r"D:\d\scan.dat", scan_type="measurement", B=None):
    prov = {}
    if B is not None:
        prov["B"] = B
    return SimpleNamespace(
        scan_id=f"{exp}:{num}:{det}", experiment_id=exp, scan_number=num, scan_type=scan_type,
        status=status, source_file=source, T=T, hkl=list(hkl) if hkl is not None else None,
        provenance=prov, detectors={det: SimpleNamespace(detector_id=det)})


def _ws(scan_views):
    return SimpleNamespace(scans=list(scan_views))


def _mixed():
    # Three analyzers × two temperatures (100 K, 300 K) = six units.
    out = []
    for det in ("ana1", "ana2", "ana9"):
        for num, T in ((10, 100.0), (20, 300.0)):
            out.append(_sv("s30", num, det, T=T, hkl=(1.5, 0.0, 0.0)))
    return out


def test_filter_shrinks_visible_rows(qtbot):
    vm = ScansViewModel(_ws(_mixed()))
    assert len(vm.rows()) == 6
    vm.set_filter(parse_query("ana:ana9")[0])
    keys = {u.key for u in vm.rows()}
    assert keys == {"s30:10:ana9", "s30:20:ana9"}


def test_filter_is_orthogonal_to_grouping(qtbot):
    # The council's core ask: group by T, but show only analyzer 9 — the T groups keep BOTH temps,
    # each now holding only the ana9 unit.
    vm = ScansViewModel(_ws(_mixed()))
    vm.set_filter(parse_query("ana:ana9")[0])
    t_groups = vm.auto_groups("T")
    assert len(t_groups) == 2                       # both 100 K and 300 K groups survive
    for g in t_groups:
        assert all(k.endswith(":ana9") for k in g.scan_keys)
        assert len(g.scan_keys) == 1


def test_empty_group_vanishes_under_filter(qtbot):
    vm = ScansViewModel(_ws(_mixed()))
    assert len(vm.auto_groups("ana")) == 3
    vm.set_filter(parse_query("ana:ana9")[0])
    assert len(vm.auto_groups("ana")) == 1          # ana1/ana2 groups gone, no member survives


def test_tabs_stay_stable_under_filter(qtbot):
    # multi_detector / has_field read _all_rows, so narrowing to one analyzer must NOT collapse the
    # workspace to "single detector" (which would drop the Analyzer tab).
    vm = ScansViewModel(_ws(_mixed()))
    assert vm.multi_detector is True
    vm.set_filter(parse_query("ana:ana9")[0])
    assert vm.multi_detector is True


def test_temperature_filter(qtbot):
    vm = ScansViewModel(_ws(_mixed()))
    vm.set_filter(parse_query("T>200")[0])
    assert all(u.T == 300.0 for u in vm.rows())
    assert len(vm.rows()) == 3


def test_clear_filter_restores(qtbot):
    vm = ScansViewModel(_ws(_mixed()))
    vm.set_filter(parse_query("ana:ana9")[0])
    assert len(vm.rows()) == 2
    vm.clear_filter()
    assert len(vm.rows()) == 6


def test_filtered_count(qtbot):
    vm = ScansViewModel(_ws(_mixed()))
    vm.set_filter(parse_query("ana:ana9")[0])
    assert vm.filtered_count() == (2, 6)


def test_apply_query_returns_error_but_still_filters(qtbot):
    vm = ScansViewModel(_ws(_mixed()))
    err = vm.apply_query("foo:bar ana:ana9")
    assert err is not None
    assert len(vm.rows()) == 2                       # good token still applied


def test_filter_changed_signal_fires(qtbot):
    vm = ScansViewModel(_ws(_mixed()))
    with qtbot.waitSignal(vm.filter_changed, timeout=1000):
        vm.set_filter(parse_query("ana:ana9")[0])


def test_option_providers(qtbot):
    vm = ScansViewModel(_ws(_mixed()))
    assert vm.available_analyzers() == ["ana1", "ana2", "ana9"]
    assert vm.t_extent() == (100.0, 300.0)


def test_filter_persists_through_store(qtbot, tmp_path):
    store = GuiStore.load(tmp_path)
    vm = ScansViewModel(_ws(_mixed()), store=store)
    with qtbot.waitSignal(vm.store_dirty, timeout=1000):
        vm.set_filter(parse_query("ana:ana9 T>200")[0])
    store.save()

    # A fresh store + VM hydrates the persisted filter on bind.
    reloaded = GuiStore.load(tmp_path)
    assert reloaded.scan_filter() == {"analyzers": ["ana9"], "t_min": 200.0,
                                      "t_min_exclusive": True}   # strict `>` survives persistence
    vm2 = ScansViewModel(_ws(_mixed()))
    vm2.set_store(reloaded)
    assert vm2.filter().analyzers == frozenset({"ana9"})
    assert {u.key for u in vm2.rows()} == {"s30:20:ana9"}


def test_selection_survives_compatible_filter(qtbot):
    vm = ScansViewModel(_ws(_mixed()))
    vm.select_keys(["s30:10:ana9"])
    vm.set_filter(parse_query("ana:ana9")[0])
    assert vm.selected_keys() == ["s30:10:ana9"]     # still visible, still selected


def test_selection_of_filtered_out_unit_drops(qtbot):
    vm = ScansViewModel(_ws(_mixed()))
    vm.select_keys(["s30:10:ana1"])
    vm.set_filter(parse_query("ana:ana9")[0])
    assert vm.selected_keys() == []                   # ana1 filtered out, selection gone

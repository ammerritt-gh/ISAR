"""CenterHost tests (GUI Pass 3, M1-2): the central view switcher + its MainWindow integration.

Covers page registration/switching, the auto-follow clearing rules (user click / show_view(user=True)
clear it; a programmatic show_view does not), glw_for, refresh-on-show, the send-to-group routing
(routes to the right pane AND switches the page), and a dispose()+rebind that leaves no dangling
view_changed receiver on the window.
"""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")

from PySide6.QtWidgets import QWidget

from isar.workspace_api import Workspace
from isar_gui.main_window import MainWindow
from isar_gui.session import Session
from isar_gui.views.center_host import CenterHost


class _FakePage(QWidget):
    """A stand-in page: counts refresh() calls and exposes a sentinel _glw like the real panes."""

    def __init__(self):
        super().__init__()
        self.refreshed = 0
        self._glw = object()

    def refresh(self):
        self.refreshed += 1


class _NoGlwPage(QWidget):
    """A page with neither refresh() nor _glw (the Scan/Sandbox shape has no arm/glw contract)."""


def _host(qtbot, *keys):
    host = CenterHost()
    qtbot.addWidget(host)
    pages = {}
    for k in keys:
        p = _FakePage()
        host.add_page(k, k.title(), p)
        pages[k] = p
    return host, pages


# --------------------------------------------------------------------------- registration + switching
def test_first_page_is_current_and_pages_registered(qtbot):
    host, pages = _host(qtbot, "scan", "waterfall", "plane")
    assert host.current_key == "scan"                 # first added page becomes current
    assert host.pane("waterfall") is pages["waterfall"]
    assert host.auto_follow is True                   # default: auto-follow on


def test_show_view_switches_and_emits(qtbot):
    host, pages = _host(qtbot, "scan", "plane")
    seen = []
    host.view_changed.connect(seen.append)
    host.show_view("plane")
    assert host.current_key == "plane"
    assert seen == ["plane"]
    # Re-showing the same page does not re-emit (no spurious change).
    host.show_view("plane")
    assert seen == ["plane"]


def test_unknown_key_raises_keyerror(qtbot):
    host, _ = _host(qtbot, "scan")
    with pytest.raises(KeyError):
        host.show_view("nope")
    with pytest.raises(KeyError):
        host.pane("nope")
    with pytest.raises(KeyError):
        host.glw_for("nope")


# --------------------------------------------------------------------------- auto-follow rules
def test_switcher_click_clears_auto_follow(qtbot):
    host, _ = _host(qtbot, "scan", "plane")
    assert host.auto_follow is True
    host._buttons["plane"].click()                    # a genuine user press
    assert host.current_key == "plane"
    assert host.auto_follow is False


def test_show_view_user_true_clears_auto_follow(qtbot):
    host, _ = _host(qtbot, "scan", "plane")
    host.show_view("plane", user=True)
    assert host.auto_follow is False


def test_programmatic_show_view_keeps_auto_follow(qtbot):
    host, _ = _host(qtbot, "scan", "plane")
    host.show_view("plane")                           # user defaults False
    assert host.auto_follow is True                   # a non-user switch must not clear it


def test_auto_button_toggle_syncs_flag(qtbot):
    host, _ = _host(qtbot, "scan")
    host._auto_btn.click()                            # user unchecks Auto
    assert host.auto_follow is False
    host.auto_follow = True                           # programmatic set re-checks the button
    assert host._auto_btn.isChecked() is True


# --------------------------------------------------------------------------- glw_for + refresh-on-show
def test_glw_for_returns_page_glw(qtbot):
    host, pages = _host(qtbot, "scan", "plane")
    assert host.glw_for("plane") is pages["plane"]._glw


def test_glw_for_none_when_page_has_no_glw(qtbot):
    host = CenterHost()
    qtbot.addWidget(host)
    host.add_page("scan", "Scan", _NoGlwPage())
    assert host.glw_for("scan") is None


def test_hidden_page_gets_refresh_on_show(qtbot):
    host, pages = _host(qtbot, "scan", "plane")
    before = pages["plane"].refreshed
    host.show_view("plane")                           # a hidden pyqtgraph page needs a re-render
    assert pages["plane"].refreshed == before + 1


# --------------------------------------------------------------------------- dispose
def test_dispose_clears_pages(qtbot):
    host, _ = _host(qtbot, "scan", "plane")
    host.dispose()
    assert host.current_key is None
    with pytest.raises(KeyError):
        host.pane("scan")


# --------------------------------------------------------------------------- MainWindow integration
_X = np.linspace(-2.0, 8.0, 21)


def _counts(center: float) -> list:
    return list((100.0 + 500.0 * np.exp(-((_X - center) ** 2) / 2.0)).round())


def _ws_2t(make_ws) -> Workspace:
    scans, rows, data = {}, [], []
    for scan, h, T, om in [(1, 2.0, 200.0, 4.0), (2, 2.1, 200.0, 5.0),
                           (3, 2.0, 400.0, 4.2), (4, 2.1, 400.0, 5.1)]:
        oc = {"detector_id": "det", "verdict": "resolved", "disposition": "resolved",
              "confidence": 0.9, "omega0": om, "gamma": 1.0, "q": [h, 0.0, 0.0], "T": T,
              "sample": "tavi", "assignment_entropy": 0.0,
              "calib": {"center": 0.0, "gauss_fwhm": 1.2, "lorentz_fwhm": 0.0},
              "interval": [om - 0.4, om + 0.4], "interval_reliable": True}
        sid = f"tavi:{scan}:det"
        scans[sid] = {"scan_id": sid, "scan_number": scan, "scan_type": "measurement",
                      "status": "fitted", "detectors": {"det": oc}, "provenance": {"M": 200}}
        rows.append({"scan": scan, "detector": "det", "sample": "tavi", "T": T,
                     "h": h, "k": 0.0, "l": 0.0, "status": "fitted"})
        data.append({"scan_number": scan, "energy": list(_X), "counts": _counts(om)})
    return Workspace.open(make_ws(scans=scans, rows=rows, data_files=data, state={}))


_COLD = ["tavi:1:det", "tavi:2:det"]      # the two T=200 units


def test_send_group_routes_to_pane_and_switches_view(qtbot, make_ws):
    w = MainWindow(_ws_2t(make_ws), session=Session(), watch=False)
    qtbot.addWidget(w)
    # A single-T group sent to the dispersion plane: the pane restricts AND the view switches to it.
    w._on_send_group(_COLD, "dispersion", "Cold")
    assert w._center_host.current_key == "dispersion"
    assert w.dispersion_vm.restriction_active
    assert w._center_host.pane("dispersion")._grp_count == len(_COLD)
    # "heatmap" is the "plane" page key.
    w._on_send_group(_COLD, "heatmap", "Cold")
    assert w._center_host.current_key == "plane"
    assert w.heatmap_vm.restriction_active


def test_send_group_refused_multi_t_does_not_switch(qtbot, make_ws):
    w = MainWindow(_ws_2t(make_ws), session=Session(), watch=False)
    qtbot.addWidget(w)
    w._center_host.show_view("scan")
    w._on_send_group(["tavi:1:det", "tavi:3:det"], "dispersion", "Both")   # multi-T -> refused
    assert w._center_host.current_key == "scan"                            # view unchanged
    assert not w.dispersion_vm.restriction_active


def test_fit_requested_switches_to_sandbox(qtbot, make_ws):
    w = MainWindow(_ws_2t(make_ws), session=Session(), watch=False)
    qtbot.addWidget(w)
    w._center_host.show_view("scan")
    w._on_fit_requested("tavi:1:det")
    assert w._center_host.current_key == "sandbox"


def test_auto_follow_routes_inspect_to_scan_only_when_on(qtbot, make_ws):
    w = MainWindow(_ws_2t(make_ws), session=Session(), watch=False)
    qtbot.addWidget(w)
    # Auto on (default): a plot-point pick routes the inspector AND raises the Scan page.
    w._center_host.show_view("plane")
    assert w._center_host.auto_follow is True
    w._inspect_fit(w._ws.results[0])
    assert w._center_host.current_key == "scan"
    # Auto off: the operator's chosen view is left untouched by an inspect request.
    w._center_host.show_view("plane", user=True)
    assert w._center_host.auto_follow is False
    w._inspect_fit(w._ws.results[1])
    assert w._center_host.current_key == "plane"


def test_rebind_disposes_host_and_drops_view_changed_receiver(qtbot, make_ws):
    """Binding a second workspace disposes the first host and disconnects its view_changed from the
    window, so a stale emit cannot mutate the session (no dangling receiver)."""
    w = MainWindow(_ws_2t(make_ws), session=Session(), watch=False)
    qtbot.addWidget(w)
    old_host = w._center_host
    assert old_host is not None

    root_b = make_ws(rows=[{"scan": 1, "detector": "det", "status": "fitted", "omega0": 5.0}])
    assert w.load_workspace(str(root_b)) is True

    new_host = w._center_host
    assert new_host is not None and new_host is not old_host
    assert old_host.current_key is None                    # disposed
    # The new host is live and persists its view; the stale host's emit must NOT reach the window.
    w.session_view_before = dict(w._session.view_state)
    old_host.view_changed.emit("plane")                    # would set center_view if still connected
    assert w._session.view_state.get("center_view") != "plane"
    # The new host still routes normally.
    new_host.show_view("plane")
    assert w._session.view_state["center_view"] == "plane"


def test_center_view_state_persists_and_restores(qtbot, make_ws):
    session = Session()
    w = MainWindow(_ws_2t(make_ws), session=session, watch=False)
    qtbot.addWidget(w)
    w._center_host.show_view("plane", user=True)            # clears auto-follow, switches view
    w._snapshot_session()
    assert session.view_state["center_view"] == "plane"
    assert session.view_state["center_auto_follow"] is False

    # A fresh window on the same session restores the persisted view + auto-follow.
    w2 = MainWindow(_ws_2t(make_ws), session=session, watch=False)
    qtbot.addWidget(w2)
    assert w2._center_host.current_key == "plane"
    assert w2._center_host.auto_follow is False

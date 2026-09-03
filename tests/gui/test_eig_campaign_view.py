"""GUI acceptance for the EIG campaign: what the room actually sees.

The fixture under ``fixtures/eig_campaign`` is a real short-path campaign run live against TAVI
(``map the acoustic phonon from (2,0,0) to (2.3,0,0)``, 19 scans, terminal ``met``), stripped to the
files the GUI reads: the two card files, ``results.csv`` with its five per-fit audit blobs emptied,
``session_log.jsonl`` with the kinds the dock hides already dropped, ``measurements.jsonl``, and four
ingest records. Nothing the feed or the dispersion view reads was altered, so these assertions are
against what the loop really wrote.

Offscreen Qt with the real widgets (``tests/gui/conftest.py`` pins ``QT_QPA_PLATFORM``): the
headless engine has passed while the real GUI was broken for a day, so this acceptance drives the
widgets, not only the view models.
"""
from __future__ import annotations

import json
import pathlib

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")

from isar.plotspec.spec import Glyph, Role, State
from isar.workspace_api import Workspace
from isar_gui.viewmodels.campaign import CampaignViewModel
from isar_gui.viewmodels.dispersion import DispersionViewModel
from isar_gui.views.campaign_dock import CampaignDock
from isar_gui.views.dispersion_pane import DispersionPane

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "eig_campaign"


class _NoOps:
    """The coordinator surface CampaignDock touches, doing nothing."""

    def __init__(self):
        from PySide6.QtCore import QObject, Signal

        class _Sig(QObject):
            drive_started = Signal()
            drive_finished = Signal(int, bool)
            activity_changed = Signal(object)
            drive_compiled = Signal(object, str)
            drive_compile_failed = Signal(str)

        self._sig = _Sig()
        for name in ("drive_started", "drive_finished", "activity_changed",
                     "drive_compiled", "drive_compile_failed"):
            setattr(self, name, getattr(self._sig, name))

    def is_running_campaign(self):
        return False

    def is_busy(self):
        return False

    def is_stop_pending(self):
        return False

    def drive_options(self):
        return None


@pytest.fixture
def campaign_ws():
    return Workspace.open(FIXTURE)


@pytest.fixture
def feed(campaign_ws):
    """Every line the campaign dock would show, in order, rendered by the real renderer."""
    return [item.text for item in CampaignViewModel(campaign_ws).feed_snapshot()]


def _eig_lines(feed):
    return [text for text in feed if text.startswith("EIG")]


# --------------------------------------------------------------------------- the campaign feed
def test_the_feed_renders_the_planner_decision_lines(feed):
    """The line an operator reads to know why this scan and not another: where, what window, how
    many points at what exposure, what that cost, and how much of the prize is in."""
    lines = _eig_lines(feed)
    assert lines, "a campaign that planned 19 scans must show its planning"

    chosen = next(text for text in lines if "→ H=" in text)
    assert "meV," in chosen                       # the chosen window
    assert " pts × " in chosen               # points x count
    assert " n, price " in chosen                 # the dose AND the price it was ranked on
    assert "nats/1e6 n" in chosen                 # information per neutron
    assert "prize " in chosen and " within ±" in chosen


def test_the_feed_shows_the_intensity_map_when_it_changes(feed):
    """It is the same three numbers most iterations; a feed that repeats them teaches an operator to
    stop reading the line, so it is said on change only."""
    lines = _eig_lines(feed)
    with_map = [text for text in lines if "intensity map:" in text]
    assert with_map, "the map is announced at least once"
    assert len(with_map) < len(lines), "and not on every line"
    assert "/1e+07" in with_map[0]
    assert "elastic" in with_map[0] and "bg" in with_map[0]


def test_the_identity_clause_appears_only_for_a_measured_point(campaign_ws):
    """A point held back by identity says so beside its width. A point with no candidate yet is not
    held back by identity -- it has no identity to be unsure of -- and saying "identified 0.00" on
    every opening scan trains the operator to ignore the clause exactly where it matters.
    """
    from isar_gui.log_render import render_event

    saw_unmeasured = False
    for record in campaign_ws.session_events():
        if record.get("kind") != "eig_plan":
            continue
        payload = record.get("payload") or {}
        chosen = payload.get("chosen")
        if not isinstance(chosen, dict):
            continue
        point = next((p for p in (payload.get("per_point") or [])
                      if p.get("hkl") == chosen.get("hkl")), None)
        if point is None:
            continue
        text = render_event(record).text
        if not point.get("n_candidates"):
            saw_unmeasured = True
            assert "identified" not in text, text
        elif float(point.get("identified_mass") or 1.0) < 0.9:
            assert "but identified" in text, text
    assert saw_unmeasured, "this campaign opens on unmeasured points; the fixture should show some"


def test_the_feed_ends_on_the_prize_and_the_terminal(feed):
    """The last thing the room sees: the planner says the map is at the bar, and the campaign says
    it stopped ``met`` on the certified intervals."""
    assert feed[-2].startswith("EIG: hypothesis map at ")
    assert feed[-2].rstrip().endswith("— done  [iter 23]")
    assert feed[-1].startswith("Campaign stopped: met")
    assert "on the certified intervals" in feed[-1]


def test_the_dock_shows_the_feed_with_the_real_widget(qtbot, campaign_ws):
    """The whole point of offscreen Qt: the lines reach the actual list widget, not just a VM."""
    # The dock seeds itself from the view model in __init__; nothing is pushed in by hand.
    dock = CampaignDock(CampaignViewModel(campaign_ws), _NoOps(), store=None, llm_env=dict)
    qtbot.addWidget(dock)
    dock.show()

    # The feed is a QTreeWidget and newest-first: read the top-level rows off the real widget.
    root = dock._feed.invisibleRootItem()
    shown = [root.child(i).text(0) for i in range(root.childCount())]
    assert shown, "the dock seeded no rows from a 313-event campaign"
    assert any("EIG → " in text for text in shown)
    assert any("intensity map:" in text for text in shown)
    assert any("Campaign stopped: met" in text for text in shown)


# --------------------------------------------------------------------------- the dispersion view
def test_the_dispersion_view_draws_the_measured_points_and_the_belief_band(campaign_ws):
    """What the campaign measured, with the bars it certified, under the branch belief it steered
    by -- the picture that shows the map being filled in."""
    spec = DispersionViewModel(campaign_ws).spec
    assert spec.projection is not None and spec.projection.plane.startswith("(H, 0, 0)")

    pooled = [s for s in spec.series if s.state is State.POOLED]
    assert pooled, "the campaign published pooled masters and they must be drawn"
    assert all(s.errors is not None for s in pooled), "a published point is drawn with its interval"
    certified = [s for s in pooled if s.glyph is Glyph.MASTER_CERTIFIED]
    assert certified and len(certified[0].x) >= 3

    branch = [s for s in spec.series if s.role is Role.BRANCH]
    assert branch, "the acoustic belief the planner steered by must be on the plot"
    assert any("coustic" in s.label for s in branch)
    assert len(branch[0].x) >= 3


def test_the_belief_band_comes_from_the_workspaces_own_branch_belief_models(campaign_ws):
    """The band is the persisted hypothesis map, not a refit: pull the rows out and the branch the
    view drew is one of them."""
    # Read the persisted rows straight off the card: that IS the claim being made.
    state = json.loads((FIXTURE / "card.state.json").read_text(encoding="utf-8"))
    rows = state.get("branch_belief_models") or []
    assert rows, "the fixture carries the belief rows the planner read"
    assert {str(row.get("branch_id")) for row in rows} >= {"acoustic"}

    spec = DispersionViewModel(campaign_ws).spec
    branch = [s for s in spec.series if s.role is Role.BRANCH]
    assert branch and any("coustic" in s.label for s in branch)


def test_the_pane_builds_with_the_real_widget(qtbot, campaign_ws):
    """Offscreen, through the actual pyqtgraph pane rather than the spec alone."""
    pane = DispersionPane(DispersionViewModel(campaign_ws))
    qtbot.addWidget(pane)
    pane.show()
    assert pane.isVisible()

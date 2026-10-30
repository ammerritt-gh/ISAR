"""The Processing dock's inter-spectra box: the pure rules table, then the real widgets (offscreen): the anchors list,
the adds and removes as deltas, the direction's preview, the greyed Start buttons and their exact requests. The
InterState and the queue controller are fakes; the scan list is the real view model over the BL43 sample."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

import bl43_fixture
from bl43_fixture import KEY_A
from gui.inter_fakes import FakeInterState, make_overview, make_preview
from gui.inter_helpers import assert_described
from isar.workspace_api.inter_spectra import BatchRequest
from isar_gui.selection import identity
from isar_gui.viewmodels.inter_batch import (ADD_OFF, CANNOT_START, NO_ANCHORS, NO_EXPERIMENT, REMOVE_OFF,
                                             PREVIEW_FAILED, TOO_FEW_SELECTED, WORKING, Control, anchored_request,
                                             box_state, simultaneous_request, wanted_requests)
from isar_gui.viewmodels.scan_list import ScanListViewModel
from isar_gui.views.inter_batch_box import InterBatchBox

A = {n: f"{KEY_A}:a{n:02d}" for n in (3, 4, 5, 6)}
CAMPAIGN = "a campaign is running in this experiment"


def _state(**overrides):
    ok = make_preview(can_start=True, line="2 anchors · spread in q · 56 spectra · 6 rings", notes=("a note",))
    base = dict(bound=True, blocked=None, n_anchors=1, n_selected=2, n_addable=1, n_selected_anchors=0,
                anchored=ok, simultaneous=ok, note=None)
    return box_state(**{**base, **overrides})


# ---- the rules, as a table: no Qt --------------------------------------------------------------
def test_the_first_reason_that_holds_says_why_start_is_greyed():
    assert _state().anchored == Control(True, None) and _state().simultaneous == Control(True, None)
    assert _state(bound=False).anchored.reason == NO_EXPERIMENT
    assert _state(bound=False).simultaneous.reason == NO_EXPERIMENT
    assert _state(blocked=CAMPAIGN).anchored.reason == CAMPAIGN
    assert _state(n_anchors=0, anchored=None).anchored.reason == NO_ANCHORS
    assert _state(n_selected=1, simultaneous=None).simultaneous.reason == TOO_FEW_SELECTED
    assert _state(anchored=None).anchored.reason == WORKING
    refused = make_preview(can_start=False, reason="member spec1:42:a03 is held by a running item")
    assert _state(anchored=refused).anchored.reason == refused.reason
    assert _state(simultaneous=make_preview(can_start=False, reason=None)).simultaneous.reason == CANNOT_START


def test_the_lines_are_the_preview_verbatim_and_the_adds_and_removes_say_why():
    assert _state().anchored_line == "2 anchors · spread in q · 56 spectra · 6 rings\na note"
    assert _state(anchored=None).anchored_line == ""
    assert _state(bound=False).add.reason == NO_EXPERIMENT
    assert _state(n_addable=0).add == Control(False, ADD_OFF)
    assert _state(n_selected_anchors=0).remove == Control(False, REMOVE_OFF)
    assert _state(note="the read failed").note == "the read failed"


def test_a_preview_is_asked_for_only_when_its_precondition_holds():
    assert wanted_requests(bound=False, direction="q", anchors=(A[3],), selection=(A[3], A[4])) == ()
    assert wanted_requests(bound=True, direction="T", anchors=(), selection=(A[3],)) == ()
    assert wanted_requests(bound=True, direction="T", anchors=(A[3],), selection=(A[3], A[4])) == (
        anchored_request("T", (A[3],)), simultaneous_request((A[3], A[4])))


# ---- the real widgets ----------------------------------------------------------------------------
class FakeQueue(QObject):
    """The queue controller's data side: ``view()`` with ``bound`` and ``blocked``, and ``changed``."""
    changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.bound = True
        self.blocked: str | None = None

    def view(self):
        return SimpleNamespace(bound=self.bound, blocked=self.blocked)


def _publish(inter, *, revision, anchors=()) -> None:
    inter.overview = make_overview(anchors=anchors, revision=revision)
    inter.changed.emit()


@pytest.fixture
def sample(bl43_ws_readonly):
    return bl43_fixture.sample_snapshot(bl43_ws_readonly)


@pytest.fixture
def made(qtbot, monkeypatch, sample):
    """The box, open, over the sample scan list, with a fake InterState and a fake queue controller."""
    monkeypatch.setattr("isar_gui.viewmodels.scan_list.scan_list", lambda _ws, **_reads: sample)
    vm = ScanListViewModel(SimpleNamespace())
    inter = FakeInterState()
    controller = FakeQueue()
    vm.set_inter(inter)
    box = InterBatchBox()
    qtbot.addWidget(box)
    box.set_inter(inter, vm)
    box.set_controller(controller)
    box.show()
    box._set_expanded(True)
    QApplication.processEvents()
    return box, vm, inter, controller


def _set_calls(inter) -> list[tuple]:
    return [c for c in inter.calls if c[0] == "set_anchors"]


def test_the_anchors_list_names_each_anchor_and_repaints_when_the_anchors_change(made):
    box, vm, inter, _ = made
    _publish(inter, revision="2", anchors=(A[3],))
    ident = identity(vm.snapshot, A[3])
    assert [box._anchors.item(i).text() for i in range(box._anchors.count())] == [
        f"{ident.analyzer} · {ident.set_label} · {ident.t}"]
    _publish(inter, revision="3", anchors=(A[3], "unlisted:1:a01"))
    assert box._anchors.item(1).text() == "unlisted:1:a01 (not in the scan list)"


def test_add_writes_only_the_scans_that_are_not_anchors_yet(made):
    box, vm, inter, _ = made
    _publish(inter, revision="2", anchors=(A[3],))
    vm.select_keys((A[3], A[4]))
    box._add.click()
    assert _set_calls(inter) == [("set_anchors", ((A[4],), True, None))]


def test_remove_writes_the_selected_anchors_and_is_greyed_until_one_is_selected(made):
    box, _, inter, _ = made
    _publish(inter, revision="2", anchors=(A[3], A[4]))
    assert not box._remove.isEnabled() and box._remove.toolTip() == REMOVE_OFF
    box._anchors.item(1).setSelected(True)
    assert box._remove.isEnabled()
    box._remove.click()
    assert _set_calls(inter) == [("set_anchors", ((A[4],), False, None))]


def test_a_direction_change_asks_for_the_anchored_preview_of_that_direction(made):
    box, _, inter, _ = made
    _publish(inter, revision="2", anchors=(A[3],))
    box._radios["T"].setChecked(True)
    last = [c for c in inter.calls if c[0] == "request_preview"][-1]
    assert last == ("request_preview", (BatchRequest(kind="anchored", direction="T", anchors=(A[3],), selection=()),))


def test_a_greyed_start_says_why_in_its_text_and_its_tooltip(made):
    box, vm, inter, controller = made
    reason = box._anchored_reason
    controller.bound = False
    controller.changed.emit()
    assert not box._start_anchored.isEnabled() and reason.text() == NO_EXPERIMENT
    assert box._start_anchored.toolTip() == NO_EXPERIMENT and not reason.isHidden()
    controller.bound = True
    controller.blocked = CAMPAIGN
    controller.changed.emit()
    assert reason.text() == CAMPAIGN and box._start_anchored.toolTip() == CAMPAIGN
    controller.blocked = None
    controller.changed.emit()
    assert reason.text() == NO_ANCHORS
    _publish(inter, revision="2", anchors=(A[3],))
    assert reason.text() == WORKING and not box._start_anchored.isEnabled()


def test_an_enabled_anchored_start_emits_exactly_the_request_it_previewed(made):
    box, _, inter, _ = made
    _publish(inter, revision="2", anchors=(A[3],))
    req = anchored_request("q", (A[3],))
    inter.previews[req] = make_preview(can_start=True, line="1 anchor · spread in q · 4 spectra · 2 rings")
    inter.preview_ready.emit(req)
    assert box._anchored_line.text() == "1 anchor · spread in q · 4 spectra · 2 rings"
    sent = []
    box.batch_requested.connect(sent.append)
    box._start_anchored.click()
    assert sent == [req] and box._start_anchored.isEnabled()


def test_a_failed_preview_read_is_shown_beside_start_and_says_why(made):
    box, _, inter, _ = made
    _publish(inter, revision="2", anchors=(A[3],))
    req = anchored_request("q", (A[3],))
    inter.preview_errors[req] = "preview: ValueError: the fit log is unreadable"
    inter.preview_failed.emit(req)
    reason = box._anchored_reason.text()
    assert reason.startswith(PREVIEW_FAILED) and "the fit log is unreadable" in reason and WORKING not in reason
    assert not box._start_anchored.isEnabled() and box._start_anchored.toolTip() == reason
    asked = [c for c in inter.calls if c[0] == "request_preview"]
    inter.preview_failed.emit(req)                  # a refresh does not ask for the failed request again
    assert [c for c in inter.calls if c[0] == "request_preview"] == asked


def test_a_simultaneous_batch_needs_two_selected_spectra_and_sends_the_selection(made):
    box, vm, inter, _ = made
    vm.select_keys((A[3],))
    assert not box._start_simultaneous.isEnabled()
    assert box._simultaneous_reason.text() == TOO_FEW_SELECTED
    vm.select_keys((A[3], A[4]))
    req = simultaneous_request(vm.selected_spectra())
    inter.previews[req] = make_preview(can_start=True, members=vm.selected_spectra())
    inter.preview_ready.emit(req)
    sent = []
    box.batch_requested.connect(sent.append)
    box._start_simultaneous.click()
    assert sent == [req] and sent[0].selection == vm.selected_spectra()


def test_the_box_is_open_by_default_and_a_fold_holds_when_anchors_appear(made, qtbot):
    box, _, inter, _ = made
    fresh = InterBatchBox()
    qtbot.addWidget(fresh)
    assert fresh._expanded                                    # open with no anchors, before any window state
    box._toggle.click()                                       # the user folds it
    assert not box._expanded
    _publish(inter, revision="2", anchors=(A[3],))
    assert not box._expanded                                  # publishing anchors does not reopen it
    box._toggle.click()
    assert box._expanded


def test_the_direction_and_the_fold_are_restored_for_each_experiment(made, qtbot, tmp_path):
    box, vm, inter, _ = made
    a, b = tmp_path / "a", tmp_path / "b"
    box._radios["qT"].setChecked(True)
    box._toggle.click()                                       # folded in experiment a
    session = SimpleNamespace(view_state={})
    box.save_view_state(session, a)
    assert session.view_state["inter_box"] == {str(a.resolve()): {"folded": True, "direction": "qT"}}
    again = InterBatchBox()
    qtbot.addWidget(again)
    again.set_inter(inter, vm)
    again.load_view_state(session, b)                         # experiment b has no entry: expanded, the default
    assert again._radios["q"].isChecked() and again._expanded
    again.load_view_state(session, a)
    assert again._radios["qT"].isChecked() and not again._expanded


def test_an_unkeyed_fold_from_before_the_per_experiment_key_is_ignored(made, qtbot, tmp_path):
    box, vm, inter, _ = made
    old = SimpleNamespace(view_state={"inter_box": {"folded": True, "direction": "T"}})   # written before the key
    again = InterBatchBox()
    qtbot.addWidget(again)
    again.set_inter(inter, vm)
    again.load_view_state(old, tmp_path / "a")
    assert again._expanded and again._radios["q"].isChecked()


def test_every_control_in_the_box_is_described(made):
    box, _, inter, _ = made
    _publish(inter, revision="2", anchors=(A[3],))
    assert_described(box)

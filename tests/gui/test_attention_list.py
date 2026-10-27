"""The needs-attention list (S2U2): the backend's groups, ticked-scan entry, the user's own groups.

Offscreen Qt with the real widgets over the S2 fixture in its ``with_t=False`` variant (sets A and B, and
the single scan, all lack a temperature) with ``off_integer`` on set B's a03 (its counts may not be raw).
Everything the backend decides (a group existing, closing, losing a member) is awaited through
``intake.worker_changed``; every save goes through the real intake writers into the copied workspace.
"""
from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QCheckBox, QWidget

import bl43_fixture
import bl43_intake_fixture
from isar.workspace_api import AttentionGroup, AttentionItem, Workspace
from isar_gui.main_window import MainWindow
from isar_gui.session import Session
from isar_gui.viewmodels.intake import IntakeState
from isar_gui.views.attention_list import (_BODY_MAX, USER_COUNTS, USER_SLIT, USER_TEMPERATURE,
                                           AttentionList)
from isar_gui.views.value_entry import ValueEntry

REPO = Path(__file__).resolve().parents[2]
LAPTOP = (1108, 851)
WAIT_MS = 15000
A, B = bl43_intake_fixture.KEYS_NO_T
SINGLE = bl43_fixture.KEY_SINGLE
B03 = f"{B}:a03"
A05 = f"{A}:a05"
BUSY = "the intake store is busy (another ISAR process is saving); try again"


@pytest.fixture(scope="module")
def att_built(tmp_path_factory):
    return bl43_intake_fixture.build_intake_workspace(
        tmp_path_factory.mktemp("att"), with_t=False, off_integer=(f"{bl43_fixture.STEM_B_NO_T}_a03",))


@pytest.fixture(scope="module")
def att_big(tmp_path_factory):
    return bl43_intake_fixture.build_intake_workspace(tmp_path_factory.mktemp("attbig"), 28, with_t=False)


@pytest.fixture
def att_ws(att_built, tmp_path):
    """A private copy (its manifest and curve check still name the original ``incoming/``)."""
    copy = tmp_path / "att"
    shutil.copytree(att_built.parent, copy)
    return copy / att_built.name


def _make(qtbot, root, *, wait=True, width=330) -> tuple[IntakeState, AttentionList]:
    intake = IntakeState()
    lst = AttentionList(intake)
    qtbot.addWidget(lst)
    lst.resize(width, 420)
    lst.show()
    intake.bind(root)
    if wait:
        _worker(qtbot, intake)
    return intake, lst


def _worker(qtbot, intake) -> None:
    """Wait for the next worker result (the backend's groups); a new request is made by a reload."""
    with qtbot.waitSignal(intake.worker_changed, timeout=WAIT_MS):
        pass


def _reload_and_wait(qtbot, intake) -> None:
    with qtbot.waitSignal(intake.worker_changed, timeout=WAIT_MS):
        intake.reload()


def _group(lst, key):
    return lst._groups[key]


def _open(lst, key):
    g = _group(lst, key)
    if key not in lst._open:
        g.toggle.click()
    return g


def _box(group, member) -> QCheckBox:
    for box in group.findChildren(QCheckBox):
        if box.property("member") == member:
            return box
    raise AssertionError(f"no box for {member}: {[b.property('member') for b in group.findChildren(QCheckBox)]}")


def _tick(group, *members) -> None:
    for m in members:
        _box(group, m).click()


def _members(lst, key) -> list[str]:
    return [r.id for r in _group(lst, key).spec.rows]


def _lines(root) -> list[dict]:
    path = Path(root) / "intake" / "entries.jsonl"
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()] if path.exists() else []


def _type(entry: ValueEntry, *texts, reason=None) -> None:
    for field, text in zip(entry._fields, texts):
        field.setText(text)
    if reason is not None:
        entry._reason.setText(reason)


def _y(top, widget) -> int:
    return widget.mapTo(top, QPoint(0, 0)).y()


def _kinds(att) -> list[str]:
    return [g.kind for g in att.groups]


def test_the_worktree_is_the_import_root():
    import isar_gui
    print("isar_gui imported from", isar_gui.__file__)
    assert Path(isar_gui.__file__).resolve().is_relative_to(REPO)


# ---- the header and the backend's groups ----------------------------------------------------------
def test_the_header_reads_checking_then_the_groups_as_the_backend_gives_them(qtbot, att_ws):
    intake, lst = _make(qtbot, att_ws, wait=False)
    assert lst._header.text() == "Needs attention · checking…" and intake.attention is None
    assert not lst._scroll.isVisible()
    _worker(qtbot, intake)
    att = intake.attention
    assert att.evaluated and "temperature" in _kinds(att) and "counts_unaccepted" in _kinds(att)
    assert lst._header.text() == f"Needs attention · {len(att.groups)}"
    backend = [k for k in _kinds(att)]
    assert [k for k in lst._groups if k in backend] == backend           # the backend's order, kept
    assert list(lst._groups)[:len(backend)] == backend
    temp = next(g for g in att.groups if g.kind == "temperature")
    group = _group(lst, "temperature")
    assert group.title.text() == temp.summary == "Temperature: 3 scans"
    assert set(_members(lst, "temperature")) == {i.scan for i in temp.items} == {A, B, SINGLE}
    assert "Affects fit" in group.quiet.text() and "to resolve: enter the value" in group.quiet.text()
    assert group.mark.text() == "fallback in use" and group.mark.isVisible()
    assert lst.is_expanded()                                  # the first time this experiment shows gaps
    assert lst._scroll.isVisible()


def test_not_evaluated_and_nothing_headers(qtbot, att_ws):
    intake, lst = _make(qtbot, att_ws)
    att = intake.attention
    intake._attention = dataclasses.replace(att, evaluated=False, note="readiness is computed for BL43LXU workspaces only",
                                            groups=())
    intake.worker_changed.emit()
    assert lst._header.text() == "Needs attention"
    assert lst._header.toolTip() == "readiness is computed for BL43LXU workspaces only"
    assert not lst._scroll.isVisible() and not lst._header.isEnabled()
    intake._attention = dataclasses.replace(att, groups=())
    intake.worker_changed.emit()
    assert lst._header.text() == "Needs attention · nothing" and "color" not in lst._header.styleSheet()


def test_unbound_the_list_takes_no_room(qtbot):
    intake, lst = _make(qtbot, None, wait=False)
    assert lst._header.isHidden() and lst._scroll.isHidden()


def test_expansion_is_the_users_after_the_first_time(qtbot, att_ws):
    intake = IntakeState()
    lst = AttentionList(intake)
    qtbot.addWidget(lst)
    seen = []
    lst.expanded_changed.connect(seen.append)
    with qtbot.waitSignal(intake.worker_changed, timeout=WAIT_MS):
        intake.bind(att_ws)
    assert lst.is_expanded() and seen == [True]
    lst._header.click()                                          # the user collapses it
    assert not lst.is_expanded() and seen == [True, False]
    _reload_and_wait(qtbot, intake)
    assert not lst.is_expanded() and seen == [True, False]        # a re-read does not open it again
    # a restored arrangement counts as the user's choice
    other = AttentionList(intake)
    qtbot.addWidget(other)
    other.set_expanded(False)
    intake.worker_changed.emit()
    assert not other.is_expanded()


def test_the_first_gap_rule_is_per_experiment(qtbot, att_ws, tmp_path):
    second = tmp_path / "second"
    shutil.copytree(att_ws.parent, second)
    intake, lst = _make(qtbot, att_ws)
    assert lst.is_expanded()
    lst._header.click()                                          # collapsed in the first experiment
    with qtbot.waitSignal(intake.worker_changed, timeout=WAIT_MS):
        intake.bind(second / att_ws.name)
    assert lst.is_expanded()                                     # the second's first gaps open it all the same
    lst._header.click()
    with qtbot.waitSignal(intake.worker_changed, timeout=WAIT_MS):
        intake.bind(att_ws)
    assert not lst.is_expanded()                                 # the first already showed its gaps: the choice holds


# ---- scope and rows --------------------------------------------------------------------------------
def test_scan_set_rows_expand_to_plain_analyzer_lines_and_analyzer_rows_are_tri_state(qtbot, att_ws):
    intake, lst = _make(qtbot, att_ws)
    group = _open(lst, "temperature")
    assert [b.property("member") for b in group.findChildren(QCheckBox)
            if b.objectName() == "attention_row_box"] == _members(lst, "temperature")
    text = lambda: [lab.text() for lab in group.rows_widget.findChildren(QWidget) if hasattr(lab, "wordWrap")]
    assert not any("a05 ·" in t for t in text())
    group.findChildren(QWidget, "attention_scan_toggle")[0].click()       # open a scan set: its analyzers, no boxes
    assert any(t.startswith("a03 ·") for t in text())
    assert len(group.findChildren(QCheckBox)) == len(_members(lst, "temperature")) + 1     # + "Select all"

    unit = _open(lst, "q_missing")
    assert unit.foot is not None and unit.foot.entry is None            # Mark dead, but no value entry
    assert unit.findChildren(ValueEntry) == []
    scan_box = _box(unit, B)
    assert scan_box.isTristate()
    unit.findChildren(QWidget, "attention_scan_toggle")[0].click()
    unit = _group(lst, "q_missing")
    scan_box = _box(unit, B)
    scan_box.click()                                                     # ticks every listed analyzer of B
    ids = lst.ticked("q_missing")
    assert ids and all(i.startswith(f"{B}:") for i in ids) and set(ids) == {
        r.id for r in unit.spec.rows if r.parent == B}
    assert scan_box.checkState() == Qt.CheckState.Checked
    _box(unit, ids[0]).click()                                           # untick one: the scan row is partial
    assert scan_box.checkState() == Qt.CheckState.PartiallyChecked
    assert unit.foot.isVisible() and f"{len(ids) - 1} analyzers in 1 scan set" in unit.foot.preview.full_text()


def test_a_constructed_damaged_lines_group_renders_plain_rows_with_their_reasons(qtbot, att_ws):
    intake, lst = _make(qtbot, att_ws)
    reasons = ["entries.jsonl line 3 skipped: not JSON", "settings.jsonl line 1 skipped: bad record"]
    items = tuple(AttentionItem(scan=None, analyzer=None, file=str(att_ws / "intake" / "entries.jsonl"),
                                reason=r, fallback=None, running=False, values_disagree=False, values_changed=())
                  for r in reasons)
    group = AttentionGroup(kind="damaged_lines", title="Intake lines skipped", summary="Intake lines skipped: 2 lines",
                           uses=("read",), state="unreadable", action="tell_operator", can_start=None,
                           fallback_in_use=False, n_scans=0, analyzers=(), items=items)
    att = intake.attention
    intake._attention = dataclasses.replace(att, groups=att.groups + (group,))
    intake.worker_changed.emit()
    g = _open(lst, "damaged_lines")
    assert g.foot is None and g.findChildren(QCheckBox) == []
    shown = [w.text() for w in g.rows_widget.findChildren(QWidget) if hasattr(w, "wordWrap") and w.text()]
    assert shown == reasons
    assert "to resolve: tell the operator" in g.quiet.text()


def test_the_q_group_shows_its_members_and_what_resolves_it_with_no_value_entry(qtbot, att_ws):
    intake, lst = _make(qtbot, att_ws)
    g = _open(lst, "q_missing")
    assert g.spec.fact is None and g.findChildren(ValueEntry) == []
    assert g.quiet.text().startswith("Affects steer")
    # the other kinds that are not value gaps carry no entry either
    for key in ("calibration_pending",):
        assert _group(lst, key).foot is None and _group(lst, key).findChildren(ValueEntry) == []


# ---- entering a value ---------------------------------------------------------------------------------
def test_a_temperature_is_saved_once_on_exactly_the_ticked_scan_sets(qtbot, att_ws):
    intake, lst = _make(qtbot, att_ws)
    group = _open(lst, "temperature")
    foot = group.foot
    assert foot.isHidden()                                                # a foot only with ticks
    _tick(group, A, B)
    assert foot.isVisible()
    _type(foot.entry, "100", reason="sample thermometer")
    preview = foot.preview.full_text()
    assert preview.startswith("Applies to 2 scan sets: ") and len(preview.split(": ", 1)[1].split(", ")) == 2
    assert foot.buttons["save"].isEnabled()
    before = len(_lines(att_ws))
    with qtbot.waitSignal(intake.worker_changed, timeout=WAIT_MS):
        foot.buttons["save"].click()
    lines = _lines(att_ws)[before:]
    assert len(lines) == 1 and lines[0]["kind"] == "value" and lines[0]["fact"] == "temperature"
    assert sorted(lines[0]["targets"]) == sorted([A, B]) and lines[0]["reason"] == "sample thermometer"
    for row in Workspace(att_ws).fact_rows([A, B]):
        if row.fact == "temperature":
            assert row.used_value == 100.0 and row.used_source == "user" and row.file_source == "missing"
    assert lst.ticked("temperature") == [] and foot.isHidden()             # ticks and entry cleared
    assert foot.entry.value() is None
    assert _members(lst, "temperature") == [SINGLE]                        # the gap closed only where it was filled
    assert _group(lst, "temperature").title.text() == "Temperature: 1 scan"
    mine = _group(lst, USER_TEMPERATURE)
    assert mine.title.text() == "Your temperature values: 2 scan sets"
    assert set(_members(lst, USER_TEMPERATURE)) == {A, B}
    assert all("100 K (yours, sample thermometer)" in r.text for r in mine.spec.rows)
    assert lst._header.text() == f"Needs attention · {len(intake.attention.groups)}"      # theirs are not counted


def test_a_refused_value_shows_the_backend_reason_and_keeps_the_ticks_and_text(qtbot, att_ws):
    intake, lst = _make(qtbot, att_ws)
    group = _open(lst, "temperature")
    _tick(group, A)
    _type(group.foot.entry, "-5", reason="typo")
    log = []
    intake.saved.connect(log.append)
    entries = att_ws / "intake" / "entries.jsonl"
    before = entries.read_bytes() if entries.exists() else b""
    group.foot.buttons["save"].click()
    assert len(log) == 1 and not log[0].startswith("Saved")
    assert (entries.read_bytes() if entries.exists() else b"") == before
    group = _group(lst, "temperature")
    assert group.foot.entry._status.text() == log[0] and group.foot.entry._status.isVisible()
    assert lst.ticked("temperature") == [A]
    assert group.foot.entry._fields[0].text() == "-5" and group.foot.entry.reason() == "typo"
    assert _box(group, A).isChecked()


def test_save_is_disabled_without_a_parseable_value_and_says_why(qtbot, att_ws):
    intake, lst = _make(qtbot, att_ws)
    group = _open(lst, "temperature")
    _tick(group, A)
    save = group.foot.buttons["save"]
    assert not save.isEnabled() and "temperature" in save.toolTip()
    _type(group.foot.entry, "abc")
    assert not save.isEnabled() and save.toolTip() == "Not a number"
    _type(group.foot.entry, "12")
    assert save.isEnabled()
    _tick(group, A)                                                        # untick: nothing to save on
    assert group.foot.isHidden()


def test_a_slit_is_entered_as_two_numbers_and_withdrawn_separately_from_the_temperature(qtbot, att_ws):
    intake, lst = _make(qtbot, att_ws)
    t = _open(lst, "temperature")
    _tick(t, A)
    _type(t.foot.entry, "50")
    with qtbot.waitSignal(intake.worker_changed, timeout=WAIT_MS):
        t.foot.buttons["save"].click()
    s = _open(lst, "slit")
    assert s.foot.entry.fact == "slit" and len(s.foot.entry._fields) == 2
    _tick(s, A)
    _type(s.foot.entry, "60", "60")
    with qtbot.waitSignal(intake.worker_changed, timeout=WAIT_MS):
        s.foot.buttons["save"].click()
    mine = _group(lst, USER_SLIT)
    assert mine.title.text() == "Your slit values: 1 scan set" and _members(lst, USER_SLIT) == [A]
    assert "mm (yours)" in mine.spec.rows[0].text
    assert _group(lst, USER_TEMPERATURE).title.text() == "Your temperature values: 1 scan set"
    mine = _open(lst, USER_SLIT)
    _tick(mine, A)
    before = len(_lines(att_ws))
    mine.foot.reason_edit.setText("wrong slit")
    with qtbot.waitSignal(intake.worker_changed, timeout=WAIT_MS):
        mine.foot.buttons["file_value"].click()
    new = _lines(att_ws)[before:]
    assert len(new) == 1 and new[0]["kind"] == "file_value" and new[0]["fact"] == "slit" and new[0]["targets"] == [A]
    assert new[0]["reason"] == "wrong slit"
    assert USER_SLIT not in lst._groups and USER_TEMPERATURE in lst._groups   # the temperature value stays


def test_use_the_files_value_withdraws_one_scan_set_and_reopens_its_gap(qtbot, att_ws):
    intake, lst = _make(qtbot, att_ws)
    t = _open(lst, "temperature")
    _tick(t, A, B)
    _type(t.foot.entry, "100")
    with qtbot.waitSignal(intake.worker_changed, timeout=WAIT_MS):
        t.foot.buttons["save"].click()
    mine = _open(lst, USER_TEMPERATURE)
    _tick(mine, A)
    assert mine.foot.preview.full_text().startswith("Applies to 1 scan set: ")
    before = len(_lines(att_ws))
    with qtbot.waitSignal(intake.worker_changed, timeout=WAIT_MS):
        mine.foot.buttons["file_value"].click()
    new = _lines(att_ws)[before:]
    assert len(new) == 1 and new[0]["kind"] == "file_value" and new[0]["targets"] == [A]
    assert set(_members(lst, "temperature")) == {A, SINGLE}                # A's gap re-opened
    assert _members(lst, USER_TEMPERATURE) == [B]
    assert lst.ticked(USER_TEMPERATURE) == []


def test_a_held_store_lock_shows_the_busy_reason_under_the_entry_with_the_ticks_kept(qtbot, att_ws):
    intake, lst = _make(qtbot, att_ws)
    group = _open(lst, "temperature")
    _tick(group, A)
    _type(group.foot.entry, "100")
    code = ("import sys, time\nfrom pathlib import Path\nsys.path.insert(0, sys.argv[1])\n"
            "from isar.intake import store\nwith store._locked(Path(sys.argv[2])):\n"
            "    print('locked', flush=True)\n    time.sleep(3)\n")
    child = subprocess.Popen([sys.executable, "-c", code, str(REPO), str(att_ws)], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "locked"
        group.foot.buttons["save"].click()
    finally:
        child.wait(timeout=30)
    group = _group(lst, "temperature")
    assert group.foot.entry._status.text() == BUSY
    assert lst.ticked("temperature") == [A] and group.foot.entry._fields[0].text() == "100"
    with qtbot.waitSignal(intake.worker_changed, timeout=WAIT_MS):
        group.foot.buttons["save"].click()                                  # the same save, now free
    assert lst.ticked("temperature") == [] and USER_TEMPERATURE in lst._groups


# ---- conditions ---------------------------------------------------------------------------------------
def test_mark_dead_then_in_use_from_the_declared_group(qtbot, att_ws):
    intake, lst = _make(qtbot, att_ws)
    g = _open(lst, "q_missing")
    assert A05 in _members(lst, "q_missing")
    lst._open_scans.setdefault("q_missing", set()).add(A)                   # open set A's analyzers
    lst._refresh_group("q_missing")
    g = _group(lst, "q_missing")
    _tick(g, A05)
    g.foot.reason_edit.setText("detector fault")
    before = len(_lines(att_ws))
    with qtbot.waitSignal(intake.worker_changed, timeout=WAIT_MS):
        g.foot.buttons["dead"].click()
    new = _lines(att_ws)[before:]
    assert len(new) == 1 and new[0]["kind"] == "condition" and new[0]["condition"] == "dead"
    assert new[0]["declared"] is True and new[0]["targets"] == [A05] and new[0]["reason"] == "detector fault"
    assert "declared" in lst._groups and _members(lst, "declared") == [A05]
    d = _open(lst, "declared")
    lst._open_scans.setdefault("declared", set()).add(A)
    lst._refresh_group("declared")
    d = _group(lst, "declared")
    assert set(d.foot.buttons) == {"in_use"} and "to resolve: reverse the declaration" in d.quiet.text()
    _tick(d, A05)
    with qtbot.waitSignal(intake.worker_changed, timeout=WAIT_MS):
        d.foot.buttons["in_use"].click()
    again = _lines(att_ws)[before + 1:]
    assert len(again) == 1 and again[0]["declared"] is False and again[0]["targets"] == [A05]
    assert "declared" not in lst._groups


def test_counts_accepted_and_withdrawn(qtbot, att_ws):
    intake, lst = _make(qtbot, att_ws)
    g = _open(lst, "counts_unaccepted")
    assert set(g.foot.buttons) == {"accept", "dead"}                        # Mark dead, and Counts accepted
    assert "accept the counts, or correct the data" in g.quiet.text()
    assert _members(lst, "counts_unaccepted") == [B03]
    lst._open_scans.setdefault("counts_unaccepted", set()).add(B)
    lst._refresh_group("counts_unaccepted")
    g = _group(lst, "counts_unaccepted")
    _tick(g, B03)
    g.foot.reason_edit.setText("checked by hand")
    before = len(_lines(att_ws))
    with qtbot.waitSignal(intake.worker_changed, timeout=WAIT_MS):
        g.foot.buttons["accept"].click()
    new = _lines(att_ws)[before:]
    assert len(new) == 1 and new[0]["condition"] == "counts_accepted" and new[0]["declared"] is True
    assert new[0]["targets"] == [B03] and new[0]["reason"] == "checked by hand"
    assert "counts_unaccepted" not in lst._groups
    mine = _group(lst, USER_COUNTS)
    assert mine.title.text() == "Your accepted counts: 1 analyzer" and _members(lst, USER_COUNTS) == [B03]
    assert "a03 · " in mine.spec.rows[0].text and "(accepted, checked by hand)" in mine.spec.rows[0].text
    assert lst._header.text() == f"Needs attention · {len(intake.attention.groups)}"       # not counted
    lst._open.add(USER_COUNTS)
    lst._open_scans.setdefault(USER_COUNTS, set()).add(B)
    lst._refresh_group(USER_COUNTS)
    mine = _group(lst, USER_COUNTS)
    _tick(mine, B03)
    with qtbot.waitSignal(intake.worker_changed, timeout=WAIT_MS):
        mine.foot.buttons["withdraw"].click()
    last = _lines(att_ws)[-1]
    assert last["condition"] == "counts_accepted" and last["declared"] is False and last["targets"] == [B03]
    assert USER_COUNTS not in lst._groups and "counts_unaccepted" in lst._groups      # the gap is back


# ---- re-reads keep the user's place --------------------------------------------------------------------
def test_a_background_reread_keeps_ticks_text_and_expansion_and_drops_members_that_left(qtbot, att_ws):
    intake, lst = _make(qtbot, att_ws)
    group = _open(lst, "temperature")
    _tick(group, A, SINGLE)
    _type(group.foot.entry, "12.5", reason="draft")
    _reload_and_wait(qtbot, intake)
    group = _group(lst, "temperature")
    assert "temperature" in lst._open and lst.ticked("temperature") == [A, SINGLE]
    assert group.foot.entry._fields[0].text() == "12.5" and group.foot.entry.reason() == "draft"
    assert _box(group, A).isChecked() and _box(group, SINGLE).isChecked() and not _box(group, B).isChecked()
    # the backend now says SINGLE no longer has the gap: it is unticked and the preview follows
    att = intake.attention
    shrunk = tuple(dataclasses.replace(g, items=tuple(i for i in g.items if i.scan != SINGLE)) if g.kind == "temperature"
                   else g for g in att.groups)
    intake._attention = dataclasses.replace(att, groups=shrunk)
    intake.worker_changed.emit()
    group = _group(lst, "temperature")
    assert lst.ticked("temperature") == [A] and set(_members(lst, "temperature")) == {A, B}
    assert group.foot.preview.full_text().startswith("Applies to 1 scan set: ")
    assert group.foot.entry._fields[0].text() == "12.5"


def test_reveal_expands_the_list_and_the_group_and_scrolls_to_it(qtbot, att_ws):
    intake, lst = _make(qtbot, att_ws)
    lst._header.click()
    assert not lst.is_expanded()
    for key in ("temperature", "slit", "calibration_pending"):                # make the content overflow the area
        _open(lst, key)
    for key in list(lst._groups):
        _open(lst, key)
    lst.resize(330, 420)
    qtbot.wait(30)
    bar = lst._scroll.verticalScrollBar()
    assert bar.maximum() > 0 and bar.value() == 0
    last = list(lst._groups)[-1]
    lst.reveal(last)
    qtbot.waitUntil(lambda: bar.value() > 0, timeout=WAIT_MS)
    assert lst.is_expanded() and last in lst._open
    view = lst._scroll.viewport()
    group = _group(lst, last)
    top = group.mapTo(view, QPoint(0, 0)).y()
    assert 0 <= top < view.height(), (top, view.height())
    lst.reveal("slit")
    qtbot.waitUntil(lambda: lst._scroll.viewport().rect().contains(
        _group(lst, "slit").mapTo(lst._scroll.viewport(), QPoint(0, 0))), timeout=WAIT_MS)
    # a key whose group has not arrived yet waits for it
    intake2, lst2 = _make(qtbot, att_ws, wait=False)
    lst2.reveal("temperature")
    assert lst2.is_expanded() and "temperature" not in lst2._open
    _worker(qtbot, intake2)
    assert "temperature" in lst2._open


def test_the_scroll_place_survives_a_reread(qtbot, att_ws):
    intake, lst = _make(qtbot, att_ws)
    for key in list(lst._groups):
        _open(lst, key)
    bar = lst._scroll.verticalScrollBar()
    assert bar.maximum() > 20
    bar.setValue(20)
    _reload_and_wait(qtbot, intake)
    assert bar.value() == 20


# ---- height: the scan list keeps its minimum -------------------------------------------------------------
def _window(qtbot, monkeypatch, size=LAPTOP) -> MainWindow:
    monkeypatch.setattr(MainWindow, "_available_size", lambda self: size)
    w = MainWindow(None, session=Session(), watch=False)
    qtbot.addWidget(w)
    w.resize(*size)
    w.show()
    qtbot.waitExposed(w)
    return w


def test_with_56_analyzers_expanded_the_scan_list_keeps_its_minimum_and_nothing_overlaps(qtbot, monkeypatch, att_big):
    w = _window(qtbot, monkeypatch)
    assert w.load_workspace(str(att_big))
    qtbot.waitUntil(lambda: w.intake.attention is not None, timeout=WAIT_MS)
    lst = w.attention_list
    assert lst.is_expanded()
    group = _open(lst, "temperature")
    scans = [r.id for r in group.spec.rows]
    for scan in scans:                                                      # every scan set open to its 28 analyzers
        lst._open_scans.setdefault("temperature", set()).add(scan)
    lst._refresh_group("temperature")
    group = _group(lst, "temperature")
    _tick(group, *scans)
    for key in list(lst._groups):
        if key != "temperature":
            _open(lst, key)
    qtbot.wait(80)
    view = lst._scroll
    content = lst._content
    assert content.height() > view.viewport().height() + 100               # it overflows, so it must scroll
    assert view.height() <= _BODY_MAX and view.verticalScrollBar().maximum() > 0
    top = lambda m: m.mapTo(w, QPoint(0, 0)).y()
    rects = {m.objectName(): (top(m), top(m) + m.height()) for m in
             (w.experiment_strip, w.setup_section, lst, w.files_strip, w.scan_list)}
    print("left column spans at 1108x851:", rects)
    order = [w.experiment_strip, w.setup_section, lst, w.files_strip, w.scan_list]
    for upper, lower in zip(order, order[1:]):                              # no overlap, in order
        assert top(upper) + upper.height() <= top(lower), (upper.objectName(), lower.objectName(), rects)
    assert w.scan_list.height() >= 400, rects                               # S1's minimum
    tree = w.scan_list._view
    assert tree.viewport().height() // tree.sizeHintForRow(0) >= 10         # S1's 10-row rule
    foot = group.foot
    assert foot.isVisible()                                                 # the entry is reachable by scrolling
    view.ensureWidgetVisible(foot.buttons["save"])
    assert view.viewport().rect().contains(foot.buttons["save"].mapTo(view.viewport(), QPoint(0, 0)))
    assert lst.width() <= w.left_column_dock.width()

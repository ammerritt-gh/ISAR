"""The inspector's fact table (S2U3): file value, yours, used, per use readiness, and the row actions.

Offscreen Qt with the real widgets over the S2 fixture (sets A and B at 97 K from the header, the Ba122
single scan at 26 K from its name) with ``off_integer`` on set B's a03 (its counts may not be raw). Every
read is the backend's own through ``IntakeState``; every save goes through the real intake writers into a
private copy of the workspace. Dialogs are driven by replacing ``FactTable._run`` (the one place a dialog
is executed), so no modal loop runs.
"""
from __future__ import annotations

import json
import shutil
from itertools import combinations
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QRect
from PySide6.QtWidgets import (QAbstractItemView, QAbstractSpinBox, QLabel, QLineEdit, QPlainTextEdit,
                               QPushButton, QTextEdit, QWidget)

import bl43_fixture
import bl43_intake_fixture
import intake_fixtures as fx
from isar.session import ingest
from isar.workspace_api import Workspace, needs_attention
from isar_gui.main_window import MainWindow
from isar_gui.session import Session
from isar_gui.theme import TONE_COLOR, Tone
from isar_gui.viewmodels.intake import IntakeState
from isar_gui.viewmodels.scan_list import ScanListViewModel
from isar_gui.views.fact_table import NO_HKL, NO_VALUE_OF_YOURS, FactTable, _Cell, _ReasonDialog
from isar_gui.views.scan_inspector import ScanInspectorPanel
from isar_gui.views.value_entry import SetValueDialog

REPO = Path(__file__).resolve().parents[2]
LAPTOP = (1108, 851)
WAIT_MS = 15000
A, B, SINGLE = bl43_fixture.KEY_A, bl43_fixture.KEY_B, bl43_fixture.KEY_SINGLE
A03, A05, B03, S06 = f"{A}:a03", f"{A}:a05", f"{B}:a03", f"{SINGLE}:a06"
WARN = TONE_COLOR[Tone.WARN]
BUSY = "an export is running"


@pytest.fixture(scope="module")
def ft_built(tmp_path_factory):
    return bl43_intake_fixture.build_intake_workspace(
        tmp_path_factory.mktemp("ft"), off_integer=(f"{bl43_fixture.STEM_B}_a03",))


@pytest.fixture
def ft_ws(ft_built, tmp_path):
    """A private copy (its manifest and curve check still name the original ``incoming/``)."""
    copy = tmp_path / "ft"
    shutil.copytree(ft_built.parent, copy)
    return copy / ft_built.name


class Rig:
    def __init__(self, qtbot, root, key=None, width=300, wait=True):
        self.qtbot, self.root = qtbot, root
        self.intake = IntakeState()
        self.vm = ScanListViewModel(Workspace.open(root))
        self.vm.set_intake(self.intake)         # as the window wires it: each intake signal re-reads the list,
        self.intake.changed.connect(lambda: self.vm.on_workspace_changed(None))      # which re-renders the inspector
        self.intake.worker_changed.connect(lambda: self.vm.on_workspace_changed(None))
        self.panel = ScanInspectorPanel(self.vm)
        qtbot.addWidget(self.panel)
        self.panel.set_intake(self.intake)
        self.panel.resize(width, 900)
        self.panel.show()
        self.table: FactTable = self.panel._fact_table
        self.intake.bind(root)
        if wait:
            self.worker()
        if key is not None:
            self.vm.select(key)

    def worker(self):
        with self.qtbot.waitSignal(self.intake.worker_changed, timeout=WAIT_MS):
            pass

    def reload_and_wait(self):
        with self.qtbot.waitSignal(self.intake.worker_changed, timeout=WAIT_MS):
            self.intake.reload()

    def cell(self, fact: str, col: str) -> _Cell:
        found = self.table.findChild(_Cell, f"fact_{fact}_{col}")
        assert found is not None, f"no {fact}/{col} cell: {[c.objectName() for c in self.table.findChildren(_Cell)]}"
        return found

    def text(self, fact: str, col: str) -> str:
        return self.cell(fact, col).text()

    def buttons(self) -> dict[str, QPushButton]:
        return {b.objectName().removeprefix("fact_action_"): b for b in self.table.findChildren(QPushButton)
                if b.objectName().startswith("fact_action_")}

    def titles(self) -> list[str]:
        return [c.objectName().removeprefix("fact_title_") for c in self.table.findChildren(QLabel)
                if c.objectName().startswith("fact_title_")]

    def readiness(self) -> list[str]:
        return [c.text() for c in self.table.findChildren(_Cell) if c.objectName() == "fact_readiness"]

    def type_value(self, monkeypatch, fact, *texts, reason=None):
        """Click *Set my value...* and type into the real dialog; returns the dialog after it ran."""
        seen = []

        def run(table, dialog):
            assert isinstance(dialog, SetValueDialog)
            for field, text in zip(dialog.entry._fields, texts):
                field.setText(text)
            if reason is not None:
                dialog.entry._reason.setText(reason)
            seen.append(dialog)
            dialog._save.click()
            return dialog.result()
        monkeypatch.setattr(FactTable, "_run", run)
        self.buttons()[f"set_{fact}"].click()
        return seen[0]

    def reason_action(self, monkeypatch, button, reason=None, accept=True):
        """Click ``button``; the real reason dialog gets ``reason`` and its action button pressed."""
        seen = []

        def run(table, dialog):
            assert isinstance(dialog, _ReasonDialog)
            if reason is not None:
                dialog.reason_edit.setText(reason)
            seen.append(dialog)
            if accept:
                dialog.action_button.click()
            return dialog.result()
        monkeypatch.setattr(FactTable, "_run", run)
        self.buttons()[button].click()
        return seen[0]


def _lines(root) -> list[dict]:
    path = Path(root) / "intake" / "entries.jsonl"
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()] if path.exists() else []


def _rows(rig, scan, **kw):
    return [r for r in Workspace(rig.root).fact_rows([scan]) if all(getattr(r, k) == v for k, v in kw.items())]


def test_the_worktree_is_the_import_root():
    import isar_gui
    print("isar_gui imported from", isar_gui.__file__)
    assert Path(isar_gui.__file__).resolve().is_relative_to(REPO)


# ---- the file's value, yours, used --------------------------------------------------------------------------
def test_the_file_value_is_shown_with_the_source_the_read_gives(qtbot, ft_ws):
    rig = Rig(qtbot, ft_ws, A03)
    source = _rows(rig, A, fact="temperature")[0].file_source
    assert source == "header"
    assert rig.text("temperature", "files") == "97 K\nfrom the header"          # the read's word, in words
    assert rig.text("temperature", "yours") == "" and rig.text("temperature", "used") == "97 K\nfile"
    assert rig.titles()[:2] == ["temperature", "slit"]
    # the slit is missing: the fallback is named, not a number
    assert rig.text("slit", "files") == "—\nmissing"
    assert rig.text("slit", "used") == "—\nno slit yet: it is chosen from the elastic lines when the analysis starts"
    assert rig.cell("temperature", "used").styleSheet() == ""


def test_the_table_draws_once_per_intake_signal_and_not_while_the_inspector_is_hidden(qtbot, monkeypatch, ft_ws):
    w = _window(qtbot, monkeypatch, ft_ws)
    w.scan_list_vm.inspect(A03)
    qtbot.wait(50)
    draws = []
    real = FactTable._render
    monkeypatch.setattr(FactTable, "_render", lambda self: (draws.append(1), real(self))[1])
    w.intake.changed.emit()
    w.intake.worker_changed.emit()
    assert len(draws) == 2                                   # the inspector's render is the one trigger of each
    w.scan_inspector.hide()
    draws.clear()
    w.intake.changed.emit()
    w.intake.worker_changed.emit()
    assert draws == []                                       # hidden: the inspector defers, and so does the table
    w.scan_inspector.show()
    assert len(draws) == 1 and w.scan_inspector._fact_table.has_facts


def test_set_my_value_supersedes_and_use_the_file_value_returns(qtbot, monkeypatch, ft_ws):
    rig = Rig(qtbot, ft_ws, A03)
    assert not rig.buttons()["use_file_temperature"].isEnabled()
    assert rig.buttons()["use_file_temperature"].toolTip() == NO_VALUE_OF_YOURS
    note = rig.table.findChild(_Cell, "fact_action_note_use_file_temperature")      # the reason, as visible text
    assert note is not None and note.text() == NO_VALUE_OF_YOURS
    qtbot.waitUntil(lambda: note.isVisible(), timeout=2000)                       # shown, not just built
    before = len(_lines(ft_ws))
    dialog = rig.type_value(monkeypatch, "temperature", "100", reason="offset")
    assert dialog.result() == 1 and "Whole scan set".lower() in dialog._applies.text().lower()
    assert f"{bl43_fixture.STEM_A} (4 analyzers)" in dialog._applies.text()
    assert rig.text("temperature", "files") == "97 K\nfrom the header"                      # the file's value is untouched
    assert rig.text("temperature", "yours") == "100 K\noffset"
    assert rig.text("temperature", "used") == "100 K\nyours"
    for col in ("yours", "used"):                                                 # a disagreement, in the warning colour
        assert WARN in rig.cell("temperature", col).styleSheet()
        tip = rig.cell("temperature", col).toolTip()
        assert "100 K" in tip and "97 K" in tip
    assert rig.cell("temperature", "files").styleSheet() == ""
    rig.type_value(monkeypatch, "temperature", "101")                             # a typo is corrected by a new value
    assert rig.text("temperature", "used") == "101 K\nyours" and rig.text("temperature", "yours") == "101 K"
    new = _lines(ft_ws)[before:]
    assert [(x["kind"], x["value"]) for x in new] == [("value", 100.0), ("value", 101.0)]   # both stay on record
    assert all(x["targets"] == [A] for x in new)
    rig.reason_action(monkeypatch, "use_file_temperature", "file was right")
    assert rig.text("temperature", "used") == "97 K\nfile" and rig.text("temperature", "yours") == ""
    assert rig.cell("temperature", "used").styleSheet() == ""
    assert len(_lines(ft_ws)) == before + 3
    assert not rig.buttons()["use_file_temperature"].isEnabled()


def test_a_value_applies_to_the_whole_set_and_another_set_shows_its_own_rows(qtbot, monkeypatch, ft_ws):
    rig = Rig(qtbot, ft_ws, A03)
    rig.type_value(monkeypatch, "slit", "60", "60")
    assert rig.text("slit", "used") == "60x60 mm\nyours"
    rig.vm.select(f"{A}:a04")                                                      # the same set: the same value
    assert rig.text("slit", "used") == "60x60 mm\nyours"
    rig.vm.select(B03)
    assert rig.text("slit", "yours") == "" and rig.text("slit", "used") == "—\nno slit yet: it is chosen from the elastic lines when the analysis starts"
    assert rig.text("temperature", "used") == "97 K\nfile"
    assert f"{bl43_fixture.STEM_B} (4 analyzers)" in rig.table.findChild(_Cell, "fact_applies_to").text()
    assert rig.table.findChild(_Cell, "fact_applies_to").toolTip().startswith("Applies to the whole scan set")
    assert "whole scan set" in rig.table.findChild(QLabel, "fact_title_temperature").toolTip().lower()


def test_a_refused_save_shows_its_reason_in_the_dialog_and_stays_open(qtbot, monkeypatch, ft_ws):
    rig = Rig(qtbot, ft_ws, A03)
    rig.intake.set_busy_reason(BUSY)
    before = _lines(ft_ws)
    dialog = rig.type_value(monkeypatch, "temperature", "100")
    assert dialog.result() == 0 and dialog.entry._status.text() == BUSY and not dialog.entry._status.isHidden()
    dialog = rig.reason_action(monkeypatch, "mark_dead", "x")
    assert dialog.result() == 0 and dialog.status.text() == BUSY and not dialog.status.isHidden()
    assert _lines(ft_ws) == before                                                 # nothing written
    rig.intake.set_busy_reason(None)
    dialog = rig.reason_action(monkeypatch, "mark_dead", "x")
    assert dialog.result() == 1


# ---- display-only rows ------------------------------------------------------------------------------------------
def test_q_counts_and_the_other_rows_have_no_action_and_the_files_column_nothing_editable(qtbot, ft_ws):
    rig = Rig(qtbot, ft_ws, A03)
    assert rig.titles() == ["temperature", "slit", "q", "counts", "analyzer"]
    # no hkl in set A's files: the file's |Q| in nm^-1 with its source, and the hover that says what is missing
    q = rig.vm.snapshot.spectrum(A03).q                    # the scan list's own |Q|, read from the same files
    assert rig.text("q", "files") == f"{q.value:g} nm⁻¹\n(|Q|, header)" and q.unit == "nm⁻¹"
    assert rig.cell("q", "files").toolTip() == NO_HKL == "no hkl in the files: campaign planning needs it"
    assert rig.cell("q", "used").toolTip() == NO_HKL
    assert rig.text("counts", "files") == "N = (y/e)², 60 bins tested, none off an integer"
    assert rig.cell("counts", "files").toolTip() == "family _i, level none"
    assert rig.text("counts", "yours") == ""
    assert rig.cell("q", "files").toolTip() != "" and "Comes from the files only" in rig.table.findChild(
        QLabel, "fact_title_q").toolTip()
    # only the temperature, slit and analyzer rows own buttons
    assert set(rig.buttons()) == {"set_temperature", "use_file_temperature", "set_slit", "use_file_slit", "mark_dead"}
    for editable in (QLineEdit, QPlainTextEdit, QTextEdit, QAbstractSpinBox, QAbstractItemView):
        assert not rig.table.findChildren(editable)
    assert all(type(c) is _Cell for c in rig.table.findChildren(QLabel) if c.objectName().startswith("fact_")
               and not c.objectName().startswith("fact_title_"))


def test_an_hkl_in_the_files_is_shown_in_rlu_with_its_source(qtbot, ft_ws):
    rig = Rig(qtbot, ft_ws, S06)
    assert rig.text("q", "files") == "[4, 0.075, 0] rlu\nfrom the file name"
    assert rig.text("q", "used") == "[4, 0.075, 0] rlu\nfile"
    assert rig.text("temperature", "files") == "26 K\nfrom the file name"
    assert rig.cell("q", "files").toolTip() == "Value read from the file name"


def test_a_q_missing_everywhere_says_so(qtbot, ft_ws):
    rig = Rig(qtbot, ft_ws, A05)                           # set A's a05 has no #Q= line either
    assert rig.text("q", "files").startswith("—") and rig.cell("q", "files").toolTip() == NO_HKL


def test_an_hkl_from_an_accepted_gpi_file_is_shown_as_used_from_it_not_as_no_hkl(qtbot, tmp_path):
    ws, data = fx.bl43_workspace(tmp_path, stems=("A",), analyzers=("a03", "a05"))
    q = {"a03": 10.0, "a05": 11.0}                         # generated, as the data files' #Q= trailers
    fx.write_scan(data, stem=f"{fx.STEM}_A", analyzers=("a03", "a05"), seed=1, energy=fx.ENERGY_120, q_nm=q)
    fx.write_gpi(data, f"{fx.STEM}_A", q_nm=q, hkl={"a03": (0.5, 0.5, 3.0), "a05": (0.5, 0.5, 3.5)})
    card, session = ws.load()
    ingest.ingest_batch(card, session, [data], workspace_root=ws.root)
    ws.save(card, session)
    scan = sorted({r.scan for r in Workspace(ws.root).fact_rows()})[0]
    rig = Rig(qtbot, ws.root, f"{scan}:a03")
    assert rig.text("q", "used") == "[0.5, 0.5, 3] rlu\nfrom the gpi file"
    assert rig.cell("q", "used").toolTip() != NO_HKL
    assert rig.text("q", "files") == "—\nmissing"           # the file gave no HKL; the gpi file did


def test_the_analyzer_row_marks_dead_then_in_use_from_the_conditions_read(qtbot, monkeypatch, ft_ws):
    rig = Rig(qtbot, ft_ws, A03)
    assert rig.text("analyzer", "used") == "in use" and rig.text("analyzer", "yours") == ""
    before = len(_lines(ft_ws))
    rig.reason_action(monkeypatch, "mark_dead", "no counts")
    new = _lines(ft_ws)[before:]
    assert len(new) == 1 and new[0]["kind"] == "condition" and new[0]["condition"] == "dead"
    assert new[0]["declared"] is True and new[0]["targets"] == [A03] and new[0]["reason"] == "no counts"
    assert rig.text("analyzer", "yours") == "declared dead\nno counts" and rig.text("analyzer", "used") == "not used"
    assert set(rig.buttons()) >= {"in_use"} and "mark_dead" not in rig.buttons()
    rig.reason_action(monkeypatch, "in_use")
    again = _lines(ft_ws)[before + 1:]
    assert len(again) == 1 and again[0]["declared"] is False and again[0]["targets"] == [A03]
    assert rig.text("analyzer", "used") == "in use" and rig.text("analyzer", "yours") == ""


# ---- counts accepted (the operator-approved conditional block) ------------------------------------------------
def test_accept_counts_lifts_the_hold_and_withdraw_brings_it_back(qtbot, monkeypatch, ft_ws):
    rig = Rig(qtbot, ft_ws, B03)
    assert rig.text("counts", "files") == "N = (y/e)², 60 bins tested, 20 off (largest deviation 0.49)"
    assert rig.cell("counts", "files").toolTip() == "family _i, level warning"
    assert rig.text("counts", "yours") == "" and "accept_counts" in rig.buttons()
    assert "withdraw_counts" not in rig.buttons()
    assert any(x.startswith("Calibrate: held") and "counts may not be raw" in x for x in rig.readiness())
    before = len(_lines(ft_ws))
    with qtbot.waitSignal(rig.intake.worker_changed, timeout=WAIT_MS):
        rig.reason_action(monkeypatch, "accept_counts", "checked by hand")
    new = _lines(ft_ws)[before:]
    assert len(new) == 1 and new[0]["condition"] == "counts_accepted" and new[0]["declared"] is True
    assert new[0]["targets"] == [B03] and new[0]["reason"] == "checked by hand"
    assert rig.text("counts", "yours") == "accepted\nchecked by hand"
    assert rig.text("counts", "used") == "as in the file"                          # accepting changes no count
    assert not any(x.startswith("Calibrate: held") for x in rig.readiness())
    assert "withdraw_counts" in rig.buttons() and "accept_counts" not in rig.buttons()
    with qtbot.waitSignal(rig.intake.worker_changed, timeout=WAIT_MS):
        rig.reason_action(monkeypatch, "withdraw_counts")
    last = _lines(ft_ws)[-1]
    assert len(_lines(ft_ws)) == before + 2 and last["declared"] is False and last["targets"] == [B03]
    assert rig.text("counts", "yours") == "" and "accept_counts" in rig.buttons()
    assert any(x.startswith("Calibrate: held") for x in rig.readiness())


def test_counts_that_look_raw_offer_no_counts_action(qtbot, ft_ws):
    rig = Rig(qtbot, ft_ws, A03)
    assert "accept_counts" not in rig.buttons() and "withdraw_counts" not in rig.buttons()


# ---- readiness ---------------------------------------------------------------------------------------------------
def test_readiness_reads_checking_then_matches_the_backend(qtbot, ft_ws):
    rig = Rig(qtbot, ft_ws, A03, wait=False)
    rig.vm.select(A03)
    assert rig.intake.attention is None and rig.readiness() == ["Readiness: checking…"]
    rig.worker()
    attention = needs_attention(ft_ws)
    uses = attention.scans[A].analyzers["a03"].uses
    lines = rig.readiness()
    assert len(lines) == len(uses) == 5
    for line, (use, st) in zip(lines, uses.items()):
        name = {"reduce": "Reduce / export", "steer": "Campaign planning"}.get(use, use.capitalize())
        assert line.startswith(f"{name}: {st.state}")
        if st.reason:
            assert line.endswith(f"— {st.reason}")
    assert "Campaign planning: held — no Q in the files (only |Q|)" in lines


def test_a_zero_temperature_fallback_is_named_in_the_fit_line(qtbot, tmp_path):
    root = bl43_intake_fixture.build_intake_workspace(tmp_path, with_t=False)
    rig = Rig(qtbot, root, f"{bl43_intake_fixture.KEYS_NO_T[0]}:a03")
    assert rig.text("temperature", "used") == "—\nno temperature known: the fit assumes T → 0 (no thermal population)"
    assert rig.text("temperature", "files") == "—\nmissing"


# ---- the panel -----------------------------------------------------------------------------------------------------
def test_a_pooled_row_shows_no_table_and_the_stage_one_lines_stay(qtbot, tmp_path, monkeypatch):
    root = bl43_fixture.pooled_workspace(tmp_path)
    rig = Rig(qtbot, root, None)
    key = next(iter(rig.vm.snapshot.pooled)).key
    rig.vm.select(key)
    assert rig.table.isHidden() and not rig.table.has_facts
    labels = {rig.panel._facts.itemAt(i, rig.panel._facts.ItemRole.LabelRole).widget().full_text()
              for i in range(rig.panel._facts.rowCount())}
    assert {"Temperature", "Q"} <= labels


def test_the_table_replaces_the_stage_one_lines_only_while_it_has_facts(qtbot, ft_ws):
    rig = Rig(qtbot, ft_ws, A03)

    def labels():
        form = rig.panel._facts
        return {form.itemAt(i, form.ItemRole.LabelRole).widget().full_text() for i in range(form.rowCount())}
    assert rig.table.has_facts and not labels() & {"Temperature", "Q", "Slit"}
    assert {"File", "Scan numbers", "Analyzer", "Points"} <= labels()
    rig.vm.select(None)
    assert rig.table.isHidden()
    rig.intake.bind(None)                                   # nothing read: the table has nothing, the lines return
    rig.vm.select(A03)
    assert not rig.table.has_facts and {"Temperature", "Q", "Slit"} <= labels()


# ---- geometry ------------------------------------------------------------------------------------------------------
def _leaves(table: FactTable):
    out = []
    for w in table.findChildren(QWidget):
        if w.isVisibleTo(table) and isinstance(w, (QLabel, QPushButton)):
            out.append(w)
    return out


def _rect(panel, w) -> QRect:
    return QRect(w.mapTo(panel, w.rect().topLeft()), w.size())


def _assert_no_overlap(panel, table, inside: QRect):
    leaves = _leaves(table)
    assert len(leaves) > 12
    rects = {id(w): _rect(panel, w) for w in leaves}
    for w in leaves:
        r = rects[id(w)]
        assert inside.contains(r), (w.objectName(), w.text(), r, inside)
        if isinstance(w, QLabel) and w.wordWrap():                                  # the wrapped text fits its box
            assert r.height() >= w.heightForWidth(r.width()), (w.objectName(), w.text())
    for a, b in combinations(leaves, 2):
        assert not rects[id(a)].intersects(rects[id(b)]), (a.objectName(), a.text(), b.objectName(), b.text())
    scroll = panel._scroll
    shown = QRect(scroll.viewport().mapTo(panel, scroll.viewport().rect().topLeft()), scroll.viewport().size())
    others = [w for w in panel.findChildren(QLabel) if w.isVisibleTo(panel) and not table.isAncestorOf(w)]
    assert len(others) >= 8                                                         # the form's, the sections', the header's
    for w in others:                                                                # the inspector's other labels
        for t in leaves:
            mine = rects[id(t)]
            if not scroll.isAncestorOf(w):
                mine = mine.intersected(shown)                                      # the scroll area clips the table
            assert not _rect(panel, w).intersects(mine), (w.objectName(), w.text(), t.objectName(), t.text())


def _window(qtbot, monkeypatch, root, size=LAPTOP):
    monkeypatch.setattr(MainWindow, "_available_size", lambda self: size)
    w = MainWindow(None, session=Session(), watch=False)
    qtbot.addWidget(w)
    w.resize(*size)
    w.show()
    qtbot.waitExposed(w)
    assert w.load_workspace(str(root))
    qtbot.waitUntil(lambda: w.intake.attention is not None, timeout=WAIT_MS)
    return w


def test_nothing_overlaps_in_the_open_inspector_at_the_laptop_size(qtbot, monkeypatch, ft_ws):
    w = _window(qtbot, monkeypatch, ft_ws)
    panel = w.scan_inspector
    w.scan_list_vm.inspect(B03)
    qtbot.wait(100)
    table = panel._fact_table
    assert not panel.isHidden() and table.has_facts and panel.width() >= 220
    # the Count-origin row is the longest text: B03 also offers the counts action, so every button is shown
    assert {"set_temperature", "use_file_temperature", "set_slit", "use_file_slit", "mark_dead",
            "accept_counts"} <= {b.objectName().removeprefix("fact_action_") for b in table.findChildren(QPushButton)}
    scroll = panel._scroll
    viewport = QRect(scroll.viewport().mapTo(panel, scroll.viewport().rect().topLeft()), scroll.viewport().size())
    content = panel._scroll.widget()
    assert content.width() <= scroll.viewport().width()                             # nothing pushes the rows wider
    inside = QRect(viewport.left(), -10_000, viewport.width(), 20_000)              # inside the inspector's width
    _assert_no_overlap(panel, table, inside)
    print("inspector", panel.width(), "x", panel.height(), "table", table.width(), "x", table.height())
    for key in (A03, S06):
        w.scan_list_vm.inspect(key)
        qtbot.wait(50)
        _assert_no_overlap(panel, table, inside)
    assert panel.width() >= 220 and w.width() <= LAPTOP[0] + 1


def test_a_value_saved_in_the_window_reaches_the_table(qtbot, monkeypatch, ft_ws):
    w = _window(qtbot, monkeypatch, ft_ws)
    w.scan_list_vm.inspect(A03)
    qtbot.wait(50)
    rig = Rig.__new__(Rig)
    rig.qtbot, rig.root, rig.intake, rig.table, rig.panel = qtbot, ft_ws, w.intake, w.scan_inspector._fact_table, w.scan_inspector
    rig.type_value(monkeypatch, "temperature", "100", reason="offset")
    assert rig.text("temperature", "used") == "100 K\nyours"
    assert rig.text("temperature", "yours") == "100 K\noffset"
    assert [x["value"] for x in _lines(ft_ws) if x["kind"] == "value"] == [100.0]

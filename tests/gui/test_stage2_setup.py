"""Stage two's foundation: the IntakeState holder, the setup section, Locate…, Details…, and the wires.

Offscreen Qt with the real widgets over the S2 fixture (``tests/bl43_intake_fixture.py``: the S1
BL43LXU workspace with its import record, curve check and one complete fast-pass run), generated at
test time. Render cases the fixture cannot reach cheaply set constructed values of the backend's own
dataclasses on IntakeState's reads, because the setup section only displays what the backend returns.
Every engine read the worker makes is awaited through ``intake.worker_changed``.
"""
from __future__ import annotations

import dataclasses
import inspect
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

COLLECTOR_UNDER_TEST = True          # tests/gui/conftest.py keeps the collector off for other tests

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QDialog, QFileDialog, QPushButton

import bl43_fixture
import bl43_intake_fixture
from isar.intake import fit_inputs as intake_fit_inputs
from isar.intake import settings as intake_settings
from isar.intake import values as intake_values
from isar.workspace_api import FileNotRead
from isar_gui import main_window as mw_mod
from isar_gui.main_window import MainWindow
from isar_gui.session import Session
from isar_gui.viewmodels.intake import IntakeState, SaveOutcome
from isar_gui.viewmodels.scan_list import ScanListViewModel
from isar_gui.views.attention_list import AttentionList
from isar_gui.views.details_dialog import FIELDS, DetailsDialog
from isar_gui.views.experiment_strip import ExperimentStrip
from isar_gui.views.fact_table import FactTable
from isar_gui.views.processing_panel import ProcessingPanel
from isar_gui.views.readiness_line import ReadinessLine
from isar_gui.views.scan_inspector import ScanInspectorPanel
from isar_gui.views.setup_section import NOTHING_READ, STALE_NOTE, SetupSection, locate_resolution_folder
from isar_gui.views.value_entry import NOT_SAVED, SetValueDialog, ValueEntry

REPO = Path(__file__).resolve().parents[2]
LAPTOP = (1108, 851)
A, B = bl43_fixture.KEY_A, bl43_fixture.KEY_B
A03 = f"{A}:a03"
WAIT_MS = 15000


# ---- the fixture, built once per module; tests that write take a copy -------------------------------
@pytest.fixture(scope="module")
def s2_built(tmp_path_factory):
    return bl43_intake_fixture.build_intake_workspace(tmp_path_factory.mktemp("s2"),
                                                       off_integer=(f"{bl43_fixture.STEM_B}_a03",))


@pytest.fixture(scope="module")
def s2_no_curves(tmp_path_factory):
    return bl43_intake_fixture.build_intake_workspace(tmp_path_factory.mktemp("s2nc"), curves=False)


@pytest.fixture(scope="module")
def s2_no_t(tmp_path_factory):
    return bl43_intake_fixture.build_intake_workspace(tmp_path_factory.mktemp("s2nt"), with_t=False)


@pytest.fixture
def s2_ws(s2_built, tmp_path):
    """A private copy (its manifest and curve check still name the original ``incoming/``)."""
    copy = tmp_path / "s2"
    shutil.copytree(s2_built.parent, copy)
    return copy / s2_built.name


def _plain(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


def _bound(qtbot, root) -> IntakeState:
    intake = IntakeState()
    with qtbot.waitSignal(intake.worker_changed, timeout=WAIT_MS):
        intake.bind(root)
    return intake


def _window(qtbot, monkeypatch, size=LAPTOP, session=None) -> MainWindow:
    monkeypatch.setattr(MainWindow, "_available_size", lambda self: size)
    w = MainWindow(None, session=session or Session(), watch=False)
    qtbot.addWidget(w)
    w.resize(*size)
    w.show()
    qtbot.waitExposed(w)
    return w


def _pick_folder(monkeypatch, folder) -> None:
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: str(folder))


def _y(w, widget) -> int:
    return widget.mapTo(w, QPoint(0, 0)).y()


def test_the_worktree_is_the_import_root():
    import isar_gui
    print("isar_gui imported from", isar_gui.__file__)
    assert Path(isar_gui.__file__).resolve().is_relative_to(REPO)


# ---- the fixture's own checks ------------------------------------------------------------------------
def test_the_fixture_writes_the_intake_layer_and_its_variants(s2_built, s2_no_curves):
    report = s2_built / "report"
    assert (report / "intake_scans.json").is_file() and (report / "curves.json").is_file()
    from isar.workspace_api import Workspace, import_summary, needs_attention
    assert import_summary(s2_built).curves.source == "discovered"
    assert import_summary(s2_no_curves).curves.n_curves == 0
    kinds = [g.kind for g in needs_attention(s2_built).groups]
    assert "counts_unaccepted" in kinds              # U7: every third bin off reaches the warning level
    fits = {u.unit: u for u in Workspace(s2_built).fit_inputs()}
    assert set(fits) == set(bl43_intake_fixture.FITTED)
    assert all(u.fast.state == "finished" and not u.fast.changes for u in fits.values())   # fresh = current


def test_the_no_temperature_variant_has_no_temperature_anywhere(s2_no_t):
    incoming = s2_no_t.parent / "incoming"
    names = [f.name for f in incoming.iterdir() if f.is_file()]
    assert not [n for n in names if re.search(r"\d+K_|_T\d+K", n)]          # no temperature token in a stem
    assert not [f for f in incoming.iterdir() if f.is_file() and "#T=" in f.read_text(encoding="utf-8")]
    from isar.workspace_api import Workspace
    rows = [r for r in Workspace(s2_no_t).fact_rows() if r.fact == "temperature"]
    assert {r.scan for r in rows} >= set(bl43_intake_fixture.KEYS_NO_T)     # the exposed keys are the real ones
    assert all(r.used_source == "missing" and r.file_source == "missing" for r in rows)
    assert {u.unit for u in Workspace(s2_no_t).fit_inputs()} == set(bl43_intake_fixture.FITTED_NO_T)


# ---- IntakeState: its worked examples ----------------------------------------------------------------
def test_example1_bind_reads_on_both_threads(qtbot, s2_built):
    intake = IntakeState()
    seen = []
    intake.changed.connect(lambda: seen.append("changed"))
    intake.worker_changed.connect(lambda: seen.append("worker"))
    with qtbot.waitSignal(intake.worker_changed, timeout=WAIT_MS):
        intake.bind(s2_built)
        assert seen == ["changed"]
        assert intake.attention is None and intake.fit_inputs is None
        assert None not in (intake.fact_rows, intake.conditions, intake.summary, intake.settings)
    qtbot.wait(150)
    assert seen == ["changed", "worker"]
    assert intake.attention.evaluated and intake.fit_inputs is not None and intake.error is None


def test_example2_a_refused_value_shows_the_backend_reason_and_writes_nothing(qtbot, s2_ws):
    intake = _bound(qtbot, s2_ws)
    entries = s2_ws / "intake" / "entries.jsonl"
    before = entries.read_bytes() if entries.exists() else None
    with qtbot.waitSignal(intake.changed, timeout=WAIT_MS):
        outcome = intake.save_value("temperature", [A], -5.0, None)
    assert not outcome.ok
    assert outcome.message == "temperature must be a finite number greater than 0 K (got -5.0)"
    assert outcome.message == outcome.result.reason
    assert (entries.read_bytes() if entries.exists() else None) == before


def test_example3_every_save_is_refused_while_an_export_runs(qtbot, monkeypatch, s2_ws):
    intake = _bound(qtbot, s2_ws)
    called = []
    for name in ("save_value", "use_file_value", "save_condition"):
        real = getattr(intake_values, name)
        monkeypatch.setattr(intake_values, name, lambda *a, _n=name, _r=real, **k: (called.append(_n), _r(*a, **k))[1])
    for name in ("save_experiment_metadata", "save_resolution_folder"):
        monkeypatch.setattr(intake_settings, name, lambda *a, _n=name, **k: called.append(_n))
    logged = []
    intake.saved.connect(logged.append)
    intake.set_busy_reason("an export is running")
    outcomes = [intake.save_value("temperature", [A], 100.0, None),
                intake.use_file_value("temperature", [A], None),
                intake.set_dead([A03], True, None),
                intake.save_details({"title": "x"}),
                intake.save_resolution_folder(s2_ws)]
    assert all(not o.ok and o.message == "an export is running" and o.result is None for o in outcomes)
    assert called == [] and logged == ["an export is running"] * 5
    intake.set_busy_reason(None)
    assert intake.save_value("temperature", [A], 100.0, "offset").ok
    assert called == ["save_value"]
    assert intake.save_value("temperature", [A], 100.0, None).message == "Saved: 100 K on 1 scan set"


_HOLD_LOCK = """
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from isar.intake import store
with store._locked(Path(sys.argv[2])):
    print("locked", flush=True)
    time.sleep(float(sys.argv[3]))
"""


def test_example4_a_busy_store_is_shown_and_the_same_save_succeeds_after(qtbot, s2_ws):
    intake = _bound(qtbot, s2_ws)
    logged = []
    intake.saved.connect(logged.append)
    child = subprocess.Popen([sys.executable, "-c", _HOLD_LOCK, str(REPO), str(s2_ws), "3"],
                             stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "locked"
        outcome = intake.save_value("temperature", [A], 100.0, None)
    finally:
        child.wait(timeout=30)
    busy = "the intake store is busy (another ISAR process is saving); try again"
    assert not outcome.ok and outcome.message == busy and logged == [busy]
    assert intake.save_value("temperature", [A], 100.0, None).ok


def test_example5_an_unbind_drops_the_worker_result_in_flight(qtbot, s2_built):
    intake = IntakeState()
    worker = []
    intake.worker_changed.connect(lambda: worker.append(1))
    intake.bind(s2_built)
    intake.bind(None)
    qtbot.waitUntil(lambda: not intake._active and intake._queue.empty(), timeout=WAIT_MS)
    qtbot.wait(120)                           # one more poll
    assert worker == [] and intake.attention is None and intake.fit_inputs is None
    assert intake.fact_rows is None and intake.summary is None


_KILLED_RUN = """
import sys
from pathlib import Path
sys.path[:0] = [sys.argv[1], sys.argv[1] + "/tests"]
import bl43_intake_fixture
print(bl43_intake_fixture.write_run(Path(sys.argv[2]), [sys.argv[3]], end=False))
"""


def test_example6_a_run_whose_process_ended_without_its_end_line_never_counts(qtbot, s2_ws):
    first = next(ln["run"] for ln in intake_fit_inputs.read_log(s2_ws) if ln.get("event") == "start")
    done = subprocess.run([sys.executable, "-c", _KILLED_RUN, str(REPO), str(s2_ws), A03],
                          capture_output=True, text=True, timeout=120, check=True)
    killed = done.stdout.strip().splitlines()[-1]
    intake = _bound(qtbot, s2_ws)
    fast = {u.unit: u for u in intake.fit_inputs}[A03].fast
    assert fast.run == first != killed and fast.state == "finished"
    # The other direction: the same lines written by a live process count (its run is still running).
    live = bl43_intake_fixture.write_run(s2_ws, [A03], end=False)
    with qtbot.waitSignal(intake.worker_changed, timeout=WAIT_MS):
        intake.reload()
    assert {u.unit: u for u in intake.fit_inputs}[A03].fast.run == live


def test_a_worker_failure_is_an_error_and_empty_marks(qtbot, monkeypatch, s2_built):
    import isar_gui.viewmodels.intake as intake_mod
    monkeypatch.setattr(intake_mod, "needs_attention", lambda root: 1 / 0)
    intake = _bound(qtbot, s2_built)
    assert intake.attention is None and intake.fit_inputs is None
    assert "ZeroDivisionError" in intake.error


def test_no_collection_runs_on_the_intake_worker_and_the_collector_comes_back(qtbot, s2_built):
    """A cyclic collection on the worker could finalize Qt objects off the GUI thread (the suite's hang:
    the worker parked right after re-enabling the collector, at its first allocation)."""
    import gc
    import threading
    collected_on = []

    def note(phase, _info):
        if phase == "start":
            collected_on.append(threading.current_thread().name)
    gc.callbacks.append(note)
    try:
        for _ in range(3):
            intake = _bound(qtbot, s2_built)
            qtbot.waitUntil(lambda: not any(t.name == "isar-gui-intake" for t in threading.enumerate()),
                            timeout=WAIT_MS)
            qtbot.waitUntil(gc.isenabled, timeout=WAIT_MS)       # resumed on the GUI thread once the worker ended
            assert intake.attention is not None
    finally:
        gc.callbacks.remove(note)
    assert "isar-gui-intake" not in collected_on


# ---- the value entry ---------------------------------------------------------------------------------
def test_value_entry_parses_and_the_dialog_saves_or_stays_open(qtbot, s2_ws):
    entry = ValueEntry("temperature")
    qtbot.addWidget(entry)
    entry._fields[0].setText("97,5")
    assert entry.value() is None and entry.parse_error() == "Not a number (use a point: 97.5)"
    entry._fields[0].setText("97.5")
    assert entry.value() == 97.5 and entry._status.text() == NOT_SAVED

    intake = _bound(qtbot, s2_ws)
    dialog = SetValueDialog(intake, "slit", [A], f"Applies to the whole scan set {bl43_fixture.STEM_A} (4 analyzers)")
    qtbot.addWidget(dialog)
    assert not dialog._save.isEnabled()                    # empty: nothing to save
    dialog.entry._fields[0].setText("60")
    dialog.entry._fields[1].setText("-1")
    dialog._save.click()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert dialog.entry._status.text().startswith("slit must be two finite positive numbers")
    dialog.entry._fields[1].setText("60")
    assert dialog.entry.value() == (60.0, 60.0)
    dialog._save.click()
    assert dialog.result() == QDialog.DialogCode.Accepted
    slit = next(r for r in intake.fact_rows if r.scan == A and r.fact == "slit")
    assert slit.user_value == "60x60" and slit.used_source == "user"


# ---- the setup section -------------------------------------------------------------------------------
def _section(qtbot, intake) -> SetupSection:
    section = SetupSection(intake)
    qtbot.addWidget(section)
    section.resize(340, 200)
    section.show()
    return section


def test_setup_section_reads_what_was_read_and_where_the_curves_were_found(qtbot, s2_built):
    intake = _bound(qtbot, s2_built)
    section = _section(qtbot, intake)
    assert _plain(section._summary.text()) == ("3 scan sets · 9 analyzers read · 1 incomplete set · "
                                               "3 files not read")
    rf = s2_built.parent / "incoming" / "rf"
    assert section._resolution.full_text() == f"Resolution curves: {rf.resolve()} (found beside the data)"
    assert "Last checked" in section._resolution.toolTip()
    assert section._locate.isVisible() and section._problem.isHidden() and section._stale.isHidden()
    section.toggle_list("not_read")                          # the link opens the list in place
    rows = [section._list.item(i).text() for i in range(section._list.count())]
    assert section._list.isVisible() and any(r.startswith("notes.txt: not recognised") for r in rows)
    section.toggle_list("incomplete")
    assert [section._list.item(i).text() for i in range(section._list.count())] == [
        f"{bl43_fixture.STEM_B}: missing a07 (has 1 columns, expected energy, intensity, error)"]
    section.toggle_list("incomplete")
    assert section._list.isHidden()


def test_setup_section_not_found_with_the_no_curves_fixture(qtbot, s2_no_curves):
    section = _section(qtbot, _bound(qtbot, s2_no_curves))
    assert section._resolution.full_text() == "Resolution curves: not found"   # the shipped card names no folder
    assert "#b7791f" in section._resolution.styleSheet()
    assert section._problem.full_text() == "No resolution curves yet. Choose the folder that holds them."
    assert "no resolution folder is saved" in section._problem.toolTip()           # the backend's text stays on hover


def test_setup_section_shows_constructed_backend_states(qtbot, s2_ws, tmp_path):
    intake = _bound(qtbot, s2_ws)
    section = _section(qtbot, intake)
    real = intake.summary
    other = tmp_path / "later" / "rf"
    other.mkdir(parents=True)
    curves = dataclasses.replace(real.curves, problem="resolution library unavailable (gone)", check_stale=True,
                                 other_folders=(type("O", (), {"path": str(other), "beside": str(other.parent)})(),))
    intake._summary = dataclasses.replace(real, curves=curves, record_problem="import record unreadable (bad JSON)")
    section._render()
    assert section._problem.full_text() == "resolution library unavailable (gone)"
    assert section._stale.full_text() == STALE_NOTE
    assert section._record_problem.full_text() == "import record unreadable (bad JSON)"   # the folder holds a card
    use = [b for b in section.findChildren(QPushButton) if b.objectName() == "setup_use_other"]
    assert len(use) == 1
    use[0].click()                                             # saves that path, and re-reads
    saved = intake_settings.current_settings(s2_ws).resolution_folder
    assert saved.path == str(other.resolve()) and saved.source == "user"
    assert section._resolution.full_text() == f"Resolution curves: {other.resolve()} (chosen by you)"

    not_bl43 = "not recognised as BL43LXU data"     # a folder whose files are none of them BL43 data
    intake._summary = dataclasses.replace(
        real, n_scans=0, n_analyzers=0, incomplete_sets=(), record_problem=None,
        files_not_read=(FileNotRead("C:/x/a.txt", "unrecognised", not_bl43, None, None),
                        FileNotRead("C:/x/b.txt", "unrecognised", not_bl43, None, None)))
    section._render()
    assert _plain(section._summary.text()) == ("0 scan sets · 0 analyzers read · 0 incomplete sets · "
                                               "2 files not read")


def _unread(n: int, prefix: str = "") -> tuple:
    """``n`` files not read, each in its own folder with the same name (``notes.txt``)."""
    return tuple(FileNotRead(f"C:/data/{prefix}{i:02d}/notes.txt", "unrecognised", "not recognised", None, None)
                 for i in range(n))


def test_an_opened_unread_list_keeps_its_place_across_a_re_read(qtbot, s2_ws):
    intake = _bound(qtbot, s2_ws)
    section = _section(qtbot, intake)
    intake._summary = dataclasses.replace(intake.summary, files_not_read=_unread(12))
    section._render()
    section.toggle_list("not_read")
    bar = section._list.verticalScrollBar()
    assert section._list.count() == 12 and bar.maximum() > 0
    bar.setValue(3)                                              # inside the range before and after the rebuild
    place = bar.value()
    assert place == 3
    section._list.item(3).setData(Qt.ItemDataRole.UserRole, "kept")
    intake.changed.emit()                                       # a re-read that found the same files
    assert section._open_list == "not_read" and section._list.isVisible()
    assert bar.value() == place and section._list.item(3).data(Qt.ItemDataRole.UserRole) == "kept"
    intake._summary = dataclasses.replace(intake.summary, files_not_read=_unread(12) + _unread(1, "z"))
    intake.changed.emit()                                       # a new file arrives: rebuilt, place kept
    assert section._list.count() == 13 and section._open_list == "not_read"
    assert bar.value() == place
    section.toggle_list("incomplete")                           # another list starts at the top
    assert bar.value() == 0


def test_each_unread_file_shows_its_full_path_on_hover(qtbot, s2_ws):
    intake = _bound(qtbot, s2_ws)
    section = _section(qtbot, intake)
    intake._summary = dataclasses.replace(intake.summary, files_not_read=_unread(2))
    section._render()
    section.toggle_list("not_read")
    rows = [(section._list.item(i).text(), section._list.item(i).toolTip()) for i in range(2)]
    assert rows[0][0] == rows[1][0] == "notes.txt: not recognised"              # the rows alone are twins
    assert rows[0][1] == "C:/data/00/notes.txt\nnot recognised"
    assert rows[1][1] == "C:/data/01/notes.txt\nnot recognised"


def test_an_unreadable_record_in_an_experiment_with_a_card_is_shown_not_nothing_read(qtbot, s2_ws):
    intake = _bound(qtbot, s2_ws)
    section = _section(qtbot, intake)
    intake._summary = dataclasses.replace(
        intake.summary, n_scans=0, n_analyzers=0, incomplete_sets=(), files_not_read=(),
        record_problem="import record unreadable (bad JSON)")       # what the backend returns for it
    section._render()
    assert section._summary.text() == "import record unreadable (bad JSON)"
    assert "#b7791f" in section._summary.styleSheet() and section._record_problem.isHidden()


def test_record_problem_is_not_shown_without_a_card(qtbot, tmp_path):
    folder = tmp_path / "just_created"
    folder.mkdir()
    intake = _bound(qtbot, folder)
    assert intake.summary.record_problem.startswith("not an ISAR workspace")
    section = _section(qtbot, intake)
    assert _plain(section._summary.text()) == NOTHING_READ and section._record_problem.isHidden()
    assert section._locate.isVisible()
    assert section._locate.full_text() == "Choose curves folder…"


def test_no_experiment_reads_nothing_read_yet_without_locate(qtbot):
    intake = IntakeState()
    intake.bind(None)
    section = _section(qtbot, intake)
    assert section._summary.text() == NOTHING_READ and section._locate.isHidden()


# ---- Locate… -----------------------------------------------------------------------------------------
def test_locate_saves_the_users_folder_and_it_survives_reopening(qtbot, monkeypatch, s2_ws, tmp_path):
    chosen = tmp_path / "curves"
    chosen.mkdir()
    w = _window(qtbot, monkeypatch)
    assert w.load_workspace(str(s2_ws))
    qtbot.waitUntil(lambda: w.intake.attention is not None, timeout=WAIT_MS)
    _pick_folder(monkeypatch, chosen)
    w.setup_section._locate.click()
    saved = intake_settings.current_settings(s2_ws).resolution_folder
    assert saved.path == str(chosen.resolve()) and saved.source == "user"
    assert w.setup_section._resolution.full_text() == f"Resolution curves: {chosen.resolve()} (chosen by you)"
    assert w.setup_section._stale.full_text() == STALE_NOTE           # not checked until the next import
    assert f"Saved: resolution folder {chosen.resolve()}" in w.log_dock._view.toPlainText()
    w.intake.bind(None)
    w.intake.bind(s2_ws)                                          # reopen
    assert w.intake.settings.resolution_folder.path == str(chosen.resolve())


def test_locate_onto_a_missing_folder_shows_the_reason_and_writes_nothing(qtbot, monkeypatch, s2_ws, tmp_path):
    w = _window(qtbot, monkeypatch)
    assert w.load_workspace(str(s2_ws))
    settings_file = s2_ws / "intake" / "settings.jsonl"
    before = settings_file.read_bytes()
    missing = tmp_path / "nope"
    _pick_folder(monkeypatch, missing)
    w.setup_section._locate.click()
    assert w.setup_section._outcome.full_text() == f"resolution folder {missing} does not exist"
    assert settings_file.read_bytes() == before
    assert f"resolution folder {missing} does not exist" in w.log_dock._view.toPlainText()


# ---- Details… ----------------------------------------------------------------------------------------
def test_details_round_trip_keeps_every_other_key(qtbot, s2_ws):
    assert intake_settings.save_experiment_metadata(s2_ws, {"beamline_note": "keep me"}).ok
    intake = _bound(qtbot, s2_ws)
    typed = {"title": "CDW phonons", "proposal": "2026B1234", "sample": "CsV3Sb5", "people": "A, B",
             "dates": "2026-11-02 to 2026-11-06", "notes": "line one\nline two"}
    dialog = DetailsDialog(intake, s2_ws.name, s2_ws)
    qtbot.addWidget(dialog)
    for key, value in typed.items():
        field = dialog._fields[key]
        field.setPlainText(value) if key == "notes" else field.setText(value)
    dialog.save()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert intake_settings.current_settings(s2_ws).metadata == {"beamline_note": "keep me", **typed}

    again = DetailsDialog(intake, s2_ws.name, s2_ws)
    qtbot.addWidget(again)
    assert again.fields() == typed and [k for k, _l in FIELDS] == list(typed)
    assert again._folder.isReadOnly() and again._folder.text() == str(s2_ws)
    again._fields["title"].setText("")
    again.save()
    assert intake_settings.current_settings(s2_ws).metadata == {"beamline_note": "keep me", **typed, "title": ""}


def test_a_refused_details_save_keeps_the_dialog_open_with_the_reason(qtbot, s2_ws):
    intake = _bound(qtbot, s2_ws)
    intake.set_busy_reason("an export is running")
    dialog = DetailsDialog(intake, s2_ws.name, s2_ws)
    qtbot.addWidget(dialog)
    dialog.save()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert dialog._message.text() == "an export is running" and not dialog._message.isHidden()


# ---- a just-created experiment: nothing read, no card, no bridge ----------------------------------
def test_a_just_created_experiment_saves_and_reads_back_then_imports(qtbot, monkeypatch, tmp_path):
    chosen = tmp_path / "curves"
    chosen.mkdir()
    w = _window(qtbot, monkeypatch)
    w.new_workspace()
    root = w._wsm.root()
    assert w._wsm.state() == "pending" and not (root / "card.policy.toml").exists() and w._bridge is None
    assert w.intake.root == root and w.setup_section._summary.text() == NOTHING_READ
    assert w.setup_section._locate.isVisible() and w.experiment_strip._details.isVisible()

    _pick_folder(monkeypatch, chosen)
    w.setup_section._locate.click()
    assert w.setup_section._resolution.full_text() == f"Resolution curves: {chosen.resolve()} (chosen by you)"

    def fill_and_save(dialog):
        dialog._fields["sample"].setText("CsV3Sb5")
        dialog.save()
        return dialog.result()
    monkeypatch.setattr(DetailsDialog, "exec", fill_and_save)
    w.experiment_strip._details.click()
    assert intake_settings.current_settings(root).metadata["sample"] == "CsV3Sb5"
    assert w.intake.settings.metadata["sample"] == "CsV3Sb5"

    data = bl43_fixture.write_bl43_files(tmp_path / "data")
    from isar.cli.main import main as isar_main
    assert isar_main(["init", str(data), "--out", str(root)]) == 0       # the intake/ folder does not block it
    assert isar_main(["ingest", str(data), "--workspace", str(root)]) == 0
    from isar.workspace_api import import_summary
    summary = import_summary(root)
    assert summary.n_scans == 3 and summary.curves.folder == str(chosen.resolve())


# ---- the window: watching off, the reload set, process ends, the export guard ----------------------
def test_a_window_without_a_bridge_binds_saves_and_rereads(qtbot, monkeypatch, s2_ws):
    def no_bridge(*_a, **_k):
        raise AssertionError("stage two must not start or call a watch bridge")
    monkeypatch.setattr(mw_mod, "WatchBridge", no_bridge)
    w = _window(qtbot, monkeypatch)
    assert w.load_workspace(str(s2_ws)) and w._bridge is None and w.intake.root == s2_ws
    outcome = w.intake.save_value("temperature", [A], 100.0, "offset")
    assert outcome.ok and outcome.message == "Saved: 100 K on 1 scan set"
    row = next(r for r in w.intake.fact_rows if r.scan == A and r.fact == "temperature")
    assert row.user_value == 100.0 and row.user_reason == "offset" and row.used_source == "user"
    assert "Saved: 100 K on 1 scan set" in w.log_dock._view.toPlainText()


def test_watch_events_and_process_ends_reread_the_intake(qtbot, monkeypatch, s2_built):
    w = _window(qtbot, monkeypatch)
    assert w.load_workspace(str(s2_built))
    for event in ("curves", "intake_entries", "intake_settings", "fit_inputs", "snapshot"):
        with qtbot.waitSignal(w.intake.changed, timeout=2000):
            w._on_workspace_changed({event})
    reloads = []
    monkeypatch.setattr(w.intake, "reload", lambda: reloads.append(1))
    w._on_workspace_changed({"events"})                     # not an intake input: no re-read
    assert reloads == []
    w._on_ingest_failed(1, "cancelled")                      # as a Cancel delivers it
    w._on_drive_finished(4, True)
    assert reloads == [1, 1]
    w._on_drive_finished(0, False)                           # another experiment's campaign: the bind re-read already
    assert reloads == [1, 1]


def test_one_watcher_tick_rebuilds_the_scan_list_once_now_and_once_for_the_worker(qtbot, monkeypatch, s2_built):
    w = _window(qtbot, monkeypatch)
    assert w.load_workspace(str(s2_built))
    qtbot.waitUntil(lambda: w.intake.attention is not None and not w.intake._active, timeout=WAIT_MS)
    rebuilds = []
    real = ScanListViewModel._reread
    monkeypatch.setattr(ScanListViewModel, "_reread", lambda self: (rebuilds.append(1), real(self))[1])
    w._on_workspace_changed({"snapshot", "results", "card", "card_state"})
    assert len(rebuilds) == 1                              # the intake's re-read; no earlier one on stale reads
    qtbot.waitUntil(lambda: len(rebuilds) == 2 and not w.intake._active, timeout=WAIT_MS)
    assert len(rebuilds) == 2                              # then the worker's answer, and nothing more


def test_the_export_guard_follows_the_export_controller(qtbot, monkeypatch, s2_built):
    w = _window(qtbot, monkeypatch)
    assert w.load_workspace(str(s2_built))
    w._export.started.emit()
    assert w.intake.busy_reason() == "an export is running"
    assert w.intake.save_details({"title": "x"}).message == "an export is running"
    w._export.failed.emit("cancelled", "")
    assert w.intake.busy_reason() is None
    w._export.started.emit()
    w._export.finished.emit(s2_built)
    assert w.intake.busy_reason() is None


# ---- the mounts and every wire ------------------------------------------------------------------------
def test_the_new_mounts_have_their_final_signatures():
    for cls, params in ((IntakeState, ["self", "parent"]), (SetupSection, ["self", "intake", "parent"]),
                        (DetailsDialog, ["self", "intake", "name", "folder", "parent"]),
                        (AttentionList, ["self", "intake", "labels", "parent"]), (FactTable, ["self", "intake", "parent"]),
                        (ReadinessLine, ["self", "intake", "labels", "parent"]), (ValueEntry, ["self", "fact", "parent"]),
                        (SetValueDialog, ["self", "intake", "fact", "targets", "applies_to", "parent"])):
        assert list(inspect.signature(cls.__init__).parameters) == params, cls.__name__
    for cls, members in ((IntakeState, ("changed", "worker_changed", "saved", "bind", "reload", "root", "fact_rows",
                                        "conditions", "summary", "settings", "attention", "fit_inputs", "error",
                                        "busy_reason", "set_busy_reason", "save_value", "use_file_value",
                                        "set_dead", "set_counts_accepted", "save_details",
                                        "save_resolution_folder")),
                         (SetupSection, ("locate_requested",)), (DetailsDialog, ("rename_requested",)),
                         (ExperimentStrip, ("details_requested",)), (ProcessingPanel, ("set_readiness_widget",)),
                         (AttentionList, ("expanded_changed", "reveal", "is_expanded", "set_expanded")),
                         (FactTable, ("set_unit",)), (ScanInspectorPanel, ("set_intake",)),
                         (ScanListViewModel, ("set_intake",)),
                         (ReadinessLine, ("reveal_gap", "locate_requested", "run_requested")),
                         (ValueEntry, ("edited", "value", "reason", "parse_error", "set_message", "clear"))):
        for name in members:
            assert hasattr(cls, name), (cls.__name__, name)
    assert list(inspect.signature(locate_resolution_folder).parameters) == ["parent", "intake"]
    assert [f.name for f in dataclasses.fields(SaveOutcome)] == ["ok", "message", "result"]


def test_counts_accepted_is_declared_and_withdrawn(qtbot, s2_ws):
    intake = _bound(qtbot, s2_ws)
    first = intake.set_counts_accepted([A03], True, "checked by hand")
    assert first.ok and first.message == "Saved: counts accepted on 1 analyzer"
    rows = [(c.scan, c.analyzer, c.user_reason) for c in intake.conditions if c.condition == "counts_accepted"]
    assert rows == [(A, "a03", "checked by hand")]
    assert intake.set_counts_accepted([A03], False, None).ok
    assert not [c for c in intake.conditions if c.condition == "counts_accepted"]


def test_every_wire_of_the_mount_table_fires(qtbot, monkeypatch, s2_built):
    reveals, runs, renames, locates, details = [], [], [], [], []
    monkeypatch.setattr(AttentionList, "reveal", lambda self, key: reveals.append(key))
    monkeypatch.setattr(MainWindow, "start_analysis", lambda self: runs.append(1))
    monkeypatch.setattr(mw_mod, "locate_resolution_folder",
                        lambda parent, intake: locates.append(intake) or SaveOutcome(False, "refused here", None))
    w = _window(qtbot, monkeypatch)
    assert w.load_workspace(str(s2_built))
    monkeypatch.setattr(w, "rename_experiment", lambda: renames.append(1))

    assert w.scan_list_vm._intake is w.intake and w.scan_inspector._intake is w.intake
    assert w.processing_panel.isAncestorOf(w.readiness_line)
    layout = w.processing_panel._layout
    assert layout.indexOf(w.readiness_line) == layout.indexOf(w.processing_panel._run) - 1

    w.readiness_line.reveal_gap.emit("temperature")
    w.readiness_line.run_requested.emit()
    assert reveals == ["temperature"] and runs == [1]
    w.setup_section.locate_requested.emit()
    w.readiness_line.locate_requested.emit()
    assert locates == [w.intake, w.intake] and w.setup_section._outcome.full_text() == "refused here"

    def open_dialog(dialog):
        details.append((dialog._name.full_text(), dialog._folder.text()))
        dialog.rename_requested.emit()
        return 0
    monkeypatch.setattr(DetailsDialog, "exec", open_dialog)
    w.experiment_strip._details.click()
    w.experiment_strip.show_created_note("folder")
    note_details = [b for b in w.experiment_strip.findChildren(QPushButton)
                    if b.objectName() == "experiment_note_details"]
    note_details[0].click()
    assert details == [(s2_built.name, str(s2_built))] * 2 and renames == [1, 1]

    rereads = []
    monkeypatch.setattr(w.scan_list_vm, "on_workspace_changed", lambda changed=None: rereads.append(changed))
    w.intake.changed.emit()
    w.intake.worker_changed.emit()
    assert rereads == [None, None]
    w.intake.saved.emit("Saved: a line for the Log")
    assert "Saved: a line for the Log" in w.log_dock._view.toPlainText()

    w.attention_list.set_expanded(True)
    session = w._snapshot_session()
    assert session.view_state["attention_list"] == {"expanded": True}
    again = _window(qtbot, monkeypatch, session=session)
    assert again.attention_list.is_expanded()


def test_the_attention_list_and_readiness_line_read_no_scan_list_of_their_own():
    for name in ("attention_list", "readiness_line"):
        src = (REPO / "isar_gui" / "views" / f"{name}.py").read_text(encoding="utf-8")
        assert "scan_list(" not in src and "Workspace(" not in src and "_NO_SUMMARY" not in src, name


def test_the_scan_list_re_reads_before_the_panels_that_take_its_labels(qtbot, monkeypatch, s2_built):
    w = _window(qtbot, monkeypatch)
    assert w.load_workspace(str(s2_built))
    order = []
    real = ScanListViewModel._reread
    monkeypatch.setattr(ScanListViewModel, "_reread", lambda self: (order.append("list"), real(self))[1])
    real_rebuild = w.attention_list._rebuild
    monkeypatch.setattr(w.attention_list, "_rebuild", lambda: (order.append("attention"), real_rebuild())[1])
    w.intake.changed.emit()
    assert order == ["list", "attention"]                      # a label is read from the fresh list, never a stale one
    key = w.scan_list_vm.snapshot.sets[0].key
    assert w._scan_label(key) == w.scan_list_vm.snapshot.sets[0].label != key
    assert w._scan_label("no such scan set") == "no such scan set"


def test_the_left_column_order_in_every_state(qtbot, monkeypatch, s2_built):
    w = _window(qtbot, monkeypatch)
    order = [w.experiment_strip, w.setup_section, w.attention_list, w.files_strip, w.scan_list]
    column = w.experiment_strip.parentWidget().layout()
    for state in ("none", "ready"):
        if state == "ready":
            assert w.load_workspace(str(s2_built))
        qtbot.wait(30)
        assert [column.indexOf(m) for m in order] == [0, 1, 2, 3, 4], state
        ys = [_y(w, m) for m in order]
        assert ys == sorted(ys) and ys[0] < ys[1] < ys[3] < ys[4], (state, ys)


def test_long_paths_in_the_setup_section_elide_and_widen_nothing(qtbot, monkeypatch, s2_built):
    w = _window(qtbot, monkeypatch)
    assert w.load_workspace(str(s2_built))
    qtbot.wait(30)
    before = w.left_column_dock.width()
    long = "D:\\beamtime\\" + "CsV3Sb5_97K_0p5_0p5_3_rep_sum_" * 6 + "rf"
    summary = w.intake.summary
    w.intake._summary = dataclasses.replace(summary, curves=dataclasses.replace(
        summary.curves, folder=long, problem=f"resolution library unavailable ([Errno 2] {long})"))
    w.setup_section._render()
    qtbot.wait(30)
    section = w.setup_section
    assert w.left_column_dock.width() == before
    assert section._resolution.toolTip().startswith(f"Resolution curves: {long} (found beside the data)")
    assert section._problem.toolTip().endswith(f"{long})")
    inside = section.rect()
    for child in (section._summary, section._resolution, section._locate, section._problem):
        rect = child.geometry()
        assert inside.contains(rect), (child.objectName(), rect, inside)


def test_the_left_column_heights_at_1108x851_with_data_and_the_attention_list_open(qtbot, monkeypatch, s2_built):
    w = _window(qtbot, monkeypatch)
    assert w.load_workspace(str(s2_built))
    w.attention_list.set_expanded(True)
    qtbot.waitUntil(lambda: w.intake.attention is not None, timeout=WAIT_MS)
    qtbot.wait(50)
    heights = {m.objectName(): m.height() for m in (w.experiment_strip, w.setup_section, w.attention_list,
                                                    w.files_strip, w.scan_list)}
    print("left column heights at 1108x851:", heights)
    bottoms = [_y(w, m) + m.height() for m in (w.experiment_strip, w.setup_section, w.files_strip)]
    assert all(b <= _y(w, w.scan_list) for b in bottoms)            # nothing overlaps the scan list
    assert w.scan_list.height() >= 400, heights                     # S1's minimum
    tree = w.scan_list._view
    assert tree.viewport().height() // tree.sizeHintForRow(0) >= 10   # S1's 10-row rule

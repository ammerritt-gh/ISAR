"""The experiment as a real folder, through the real window: drop-to-create, New, Open, Rename,
Save copy as, and what the experiment strip and the Output bar say.

Offscreen Qt with the real widgets. Every experiment folder is created under the per-test
``ISAR_EXPERIMENTS_ROOT`` (tests/gui/conftest.py). Engine commands never run: the ingest controller's
``ProcessRunner`` is replaced by a fake that records argv and completes on demand.
"""
from __future__ import annotations

import importlib.util
import json
import os
import time
from datetime import date, datetime
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from synthetic_ws import write_session, write_state

from isar.workspace_api import Workspace
from isar_gui import ingest_controller, main_window
from isar_gui.gui_store import GuiStore
from isar_gui.main_window import MainWindow
from isar_gui.session import Session
from isar_gui.views.output_bar import OutputBar
from isar_gui.views.rename_dialog import REFIT_LINE, RenameDialog

_ROWS = [{"scan": 1, "detector": "det", "status": "fitted", "omega0": 5.0}]
_IDLE = {"kind": None, "queued": 0, "cancelling": False}


# ---- helpers -------------------------------------------------------------------------------
class _Sig:
    def __init__(self):
        self._cbs = []

    def connect(self, cb):
        self._cbs.append(cb)

    def disconnect(self, cb=None):
        if cb is None:
            self._cbs.clear()
        elif cb in self._cbs:
            self._cbs.remove(cb)

    def emit(self, *a):
        for cb in list(self._cbs):
            cb(*a)


class _Runner:
    """ProcessRunner stand-in for the ingest controller: records argv, completes on demand."""

    instances: list = []

    def __init__(self, *_a, **_k):
        self.line = _Sig()
        self.finished = _Sig()
        self.argv = None
        self.running = False
        _Runner.instances.append(self)

    def start(self, argv, **_k):
        self.argv = [str(a) for a in argv]
        self.running = True

    def is_running(self):
        return self.running

    def terminate(self):
        self.running = False

    kill = kill_tree = terminate

    def deleteLater(self):
        pass

    def complete(self, code, lines=()):
        self.running = False
        for ln in lines:
            self.line.emit(ln)
        self.finished.emit(code)


@pytest.fixture
def runner(monkeypatch):
    _Runner.instances = []
    monkeypatch.setattr(ingest_controller, "ProcessRunner", _Runner)
    return _Runner


class _Forbidden:
    """Stands in for a dialog class: any use fails the test."""

    def __init__(self, name):
        self._name = name

    def __getattr__(self, attr):
        raise AssertionError(f"{self._name}.{attr} was used: this path must ask nothing")

    def __call__(self, *a, **k):
        raise AssertionError(f"{self._name} was constructed: this path must ask nothing")


def _no_dialogs(monkeypatch):
    monkeypatch.setattr(main_window, "QFileDialog", _Forbidden("QFileDialog"))
    monkeypatch.setattr(main_window, "QMessageBox", _Forbidden("QMessageBox"))


def _window(qtbot, ws=None, session=None, **kw):
    kw.setdefault("watch", False)
    w = MainWindow(ws, session=session or Session(), **kw)
    qtbot.addWidget(w)
    return w


def _ready(make_ws, **parts):
    parts.setdefault("rows", _ROWS)
    return Workspace.open(make_ws(**parts))


def _log_text(w) -> str:
    return w.log_dock._view.toPlainText()


def _ingest_ok_line(root) -> str:
    return json.dumps({"workspace": str(root), "files": [],
                       "scans": {"added": [], "updated": [], "unchanged": []},
                       "deduped": [], "gaps": []})


def _answer_rename(monkeypatch, name):
    monkeypatch.setattr(RenameDialog, "ask", lambda root, ws=None, parent=None: name)


def _rename_must_not_ask(monkeypatch):
    def ask(*_a, **_k):
        raise AssertionError("the Rename dialog opened although Rename was refused")
    monkeypatch.setattr(RenameDialog, "ask", ask)


def _backend() -> str:
    """The watch backend production picks: watchdog when importable (it holds a directory handle)."""
    return "watchdog" if importlib.util.find_spec("watchdog") else "poll"


# ---- drop-to-create, New, Open -----------------------------------------------------------------
def test_a_drop_with_no_experiment_creates_a_named_folder_and_reads_into_it(
        qtbot, monkeypatch, tmp_path, runner):
    _no_dialogs(monkeypatch)
    data = tmp_path / "beam" / "leTacon_SP8_CsV3Sb5"
    data.mkdir(parents=True)
    w = _window(qtbot)

    w._on_paths_dropped([str(data)])
    root = tmp_path / "experiments" / "leTacon_SP8_CsV3Sb5"
    assert root.is_dir() and w._wsm.root() == root and w._wsm.state() == "pending"
    assert w.windowTitle() == "ISAR — leTacon_SP8_CsV3Sb5"
    init = runner.instances[0].argv
    assert init[init.index("isar") + 1] == "init" and str(root) in init
    note = w.experiment_strip._note
    assert not note.isHidden()
    assert w.experiment_strip._note_text.text() == (
        "Experiment created and named after the folder you dropped.")

    runner.instances[0].complete(0)
    ingest = runner.instances[1].argv
    assert ingest[ingest.index("isar") + 1] == "ingest" and str(root) in ingest
    runner.instances[1].complete(1, ["error: nothing parsed"])   # end the job; no fake card exists
    assert not w._operations.is_busy()

    from PySide6.QtWidgets import QToolButton
    w.experiment_strip.findChild(QToolButton, "experiment_note_close").click()
    assert note.isHidden()


def test_loose_files_name_the_experiment_after_their_common_name(qtbot, monkeypatch, tmp_path, runner):
    _no_dialogs(monkeypatch)
    folder = tmp_path / "beam"
    folder.mkdir()
    files = [folder / f"CsV3Sb5_97K_0p5_0p5_3_sum_a0{i}.dat" for i in (3, 4)]
    w = _window(qtbot)
    w._on_paths_dropped([str(f) for f in files])
    assert w._wsm.root() == tmp_path / "experiments" / "CsV3Sb5_97K_0p5_0p5_3_sum"
    assert w.experiment_strip._note_text.text() == (
        "Experiment created and named after the files you dropped.")
    runner.instances[-1].complete(1)


def test_a_dropped_experiment_opens_only_when_none_is_open(qtbot, monkeypatch, make_ws):
    first, second = make_ws(rows=_ROWS), make_ws(rows=_ROWS)
    w = _window(qtbot)
    w._on_paths_dropped([str(first)])
    assert w._wsm.state() == "ready" and w._wsm.root() == first

    requested = []
    monkeypatch.setattr(w._ingest_controller, "request_ingest", lambda p: requested.append(list(p)))
    w._on_paths_dropped([str(second)])
    assert w._wsm.root() == first                        # read into the open one, never switched
    assert requested == [[str(second)]]


def test_new_experiment_twice_gives_suffixed_folders_and_no_note(qtbot, tmp_path):
    w = _window(qtbot)
    w.new_workspace()
    first = w._wsm.root()
    w.new_workspace()
    second = w._wsm.root()
    name = f"Experiment_{date.today():%Y-%m-%d}"
    assert (first, second) == (tmp_path / "experiments" / name,
                               tmp_path / "experiments" / f"{name}-2")
    assert first.is_dir() and second.is_dir()
    assert w.experiment_strip._note.isHidden()           # a default name needs no note


def test_new_experiment_reports_a_folder_that_cannot_be_made(qtbot, tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("x", encoding="utf-8", newline="\n")
    monkeypatch.setenv("ISAR_EXPERIMENTS_ROOT", str(blocker / "ISAR"))
    w = _window(qtbot)
    w.new_workspace()
    assert w._wsm.state() == "none"
    assert "Could not create the experiment folder" in w.statusBar().currentMessage()
    assert "Could not create the experiment folder" in _log_text(w)


# ---- Rename ------------------------------------------------------------------------------------
def test_rename_is_refused_while_an_engine_command_or_an_export_runs(qtbot, monkeypatch, make_ws):
    ws = _ready(make_ws)
    root = Path(ws.root)
    w = _window(qtbot, ws)
    _rename_must_not_ask(monkeypatch)

    monkeypatch.setattr(w._operations, "is_busy", lambda: True)
    w._activity_kind = "ingest"
    w.rename_experiment()
    assert "files are being read into the experiment" in w.statusBar().currentMessage()
    assert "Rename refused: files are being read into the experiment" in _log_text(w)

    monkeypatch.setattr(w._operations, "is_busy", lambda: False)
    w._activity_kind = None
    monkeypatch.setattr(w._export, "is_running", lambda: True)
    w.rename_experiment()
    assert "an export is running" in w.statusBar().currentMessage()
    assert root.is_dir() and w._wsm.root() == root and w.windowTitle() == f"ISAR — {root.name}"


def test_rename_is_refused_when_the_experiment_holds_its_own_source_files(qtbot, monkeypatch, make_ws):
    root = make_ws(rows=_ROWS)
    write_session(root, files={str(root / "raw" / "x.dat"): {"format": "x"}})
    w = _window(qtbot, Workspace.open(root))
    _rename_must_not_ask(monkeypatch)
    w.rename_experiment()
    reason = "this experiment holds its own source files; rename would leave ISAR unable to find them"
    assert reason in w.statusBar().currentMessage() and reason in _log_text(w)
    assert root.is_dir() and w._wsm.root() == root and w.windowTitle() == f"ISAR — {root.name}"


def test_rename_of_a_watched_experiment_moves_folder_binding_recents_and_run(
        qtbot, monkeypatch, make_ws):
    other = make_ws(rows=_ROWS)
    ws = _ready(make_ws)
    old = Path(ws.root)
    session = Session(recent_workspaces=[str(other), str(old)],
                      view_state={"run": {str(old): {"data": ["D:/beam"], "opts": {}}}})
    w = _window(qtbot, ws, session, watch=True,
                watch_kwargs={"backend": _backend(), "poll_interval_s": 0.05, "debounce_ms": 50})
    assert w._bridge is not None
    _answer_rename(monkeypatch, "leTacon run 2")

    w.rename_experiment()
    new = old.parent / "leTacon run 2"
    assert new.is_dir() and not old.exists()
    assert w._wsm.state() == "ready" and str(w._wsm.root()) == str(new)
    assert w.windowTitle() == "ISAR — leTacon run 2"
    assert w.experiment_strip._name.full_text() == "leTacon run 2"
    assert w._bridge is not None and str(w._bridge._ws.root) == str(new)   # watching the new root
    assert session.recent_workspaces == [str(other), str(new)]
    assert set(session.view_state["run"]) == {str(new)}
    assert w._rerun_action.isEnabled()                   # the remembered run followed the folder
    assert f"Renamed experiment {old.name} to leTacon run 2 ({new})" in _log_text(w)


def test_a_failed_rename_leaves_everything_as_it_was_and_watches_again(qtbot, monkeypatch, make_ws):
    import isar_gui.workspace_manager as wm

    ws = _ready(make_ws)
    old = Path(ws.root)
    session = Session(recent_workspaces=[str(old)])
    w = _window(qtbot, ws, session, watch=True,
                watch_kwargs={"backend": "poll", "poll_interval_s": 0.05, "debounce_ms": 50})
    bridge_before = w._bridge
    _answer_rename(monkeypatch, "renamed")

    def refuse(_src, _dst):
        raise PermissionError(5, "Access is denied")
    monkeypatch.setattr(wm.os, "rename", refuse)
    w.rename_experiment()
    assert old.is_dir() and not (old.parent / "renamed").exists()
    assert w._wsm.root() == old and w.windowTitle() == f"ISAR — {old.name}"
    assert session.recent_workspaces == [str(old)]
    assert "could not rename" in w.statusBar().currentMessage()
    assert w._bridge is not None and w._bridge is not bridge_before   # stopped, then started again


def test_a_rename_whose_new_folder_cannot_be_opened_is_undone(qtbot, monkeypatch, make_ws):
    ws = _ready(make_ws)
    old = Path(ws.root)
    new = old.parent / "renamed"
    w = _window(qtbot, ws, watch=True,
                watch_kwargs={"backend": "poll", "poll_interval_s": 0.05, "debounce_ms": 50})
    _answer_rename(monkeypatch, "renamed")
    real_open = w._wsm.open

    def open_all_but_new(path):
        if Path(path) == new:
            w._wsm._last_error = "the card would not parse"
            return False
        return real_open(path)
    monkeypatch.setattr(w._wsm, "open", open_all_but_new)

    w.rename_experiment()
    assert old.is_dir() and not new.exists()
    assert w._wsm.state() == "ready" and w._wsm.root() == old
    assert w._bridge is not None and str(w._bridge._ws.root) == str(old)
    message = w.statusBar().currentMessage()
    assert "the rename was undone" in message and "the card would not parse" in message


def test_a_rename_that_cannot_be_undone_says_where_the_folder_is(qtbot, monkeypatch, make_ws):
    import isar_gui.workspace_manager as wm

    ws = _ready(make_ws)
    old = Path(ws.root)
    new = old.parent / "renamed"
    w = _window(qtbot, ws)
    _answer_rename(monkeypatch, "renamed")
    real_open, real_rename = w._wsm.open, os.rename
    monkeypatch.setattr(w._wsm, "open", lambda path: False if Path(path) == new else real_open(path))
    calls = []

    def rename_once(src, dst):
        calls.append((src, dst))
        if len(calls) > 1:
            raise PermissionError(5, "Access is denied")
        real_rename(src, dst)
    monkeypatch.setattr(wm.os, "rename", rename_once)

    w.rename_experiment()
    assert new.is_dir() and not old.exists()
    assert w._wsm.state() == "none" and w.windowTitle() == "ISAR — no experiment"
    assert str(new) in w.statusBar().currentMessage() and str(new) in _log_text(w)
    assert "Renamed experiment" not in _log_text(w)


def test_rename_dialog_cleans_the_name_and_warns_of_a_refit_only_with_fits(make_ws, qtbot):
    from PySide6.QtWidgets import QDialogButtonBox

    def stamped(rows):
        root = make_ws(rows=rows)
        write_state(root, resolution_versions=[
            {"version": 1, "method": "tabulated_elastic", "folder": str(root / "rf")}])
        return root

    root = stamped(_ROWS)
    (root.parent / "taken").mkdir()
    dialog = RenameDialog(root, Workspace.open(root))
    qtbot.addWidget(dialog)
    assert not dialog._refit.isHidden() and dialog._refit.text() == REFIT_LINE
    dialog._field.setText("run: 3/4 <T>")
    assert dialog._folder.text() == f"Folder: {root.parent / 'run_ 3_4 _T'}"
    ok = dialog._buttons.button(QDialogButtonBox.StandardButton.Ok)
    assert ok.isEnabled()
    dialog._field.setText("taken")
    assert not ok.isEnabled() and "already exists" in dialog._reason.text()
    dialog._field.setText("???")
    assert not ok.isEnabled() and dialog._reason.text() == "enter a name with letters or digits"

    unfitted = stamped([])
    dialog = RenameDialog(unfitted, Workspace.open(unfitted))
    qtbot.addWidget(dialog)
    assert dialog._refit.isHidden()


# ---- Save copy as ----------------------------------------------------------------------------
def _choose_copy_folder(monkeypatch, dest):
    from PySide6.QtWidgets import QFileDialog

    class _Dialog:
        Option = QFileDialog.Option

        @staticmethod
        def getSaveFileName(*_a, **_k):
            return str(dest), ""

    monkeypatch.setattr(main_window, "QFileDialog", _Dialog)


def test_after_save_copy_as_another_drop_goes_to_the_original(
        qtbot, monkeypatch, make_ws, tmp_path, runner):
    shown = []
    real_show = main_window.FilesStrip.show_arrival
    monkeypatch.setattr(main_window.FilesStrip, "show_arrival", lambda self, report: (
        shown.append(Path(self._files_vm.workspace.root)), real_show(self, report)))
    ws = _ready(make_ws)
    root = Path(ws.root)
    w = _window(qtbot, ws)
    dest = tmp_path / "copies" / "kept"
    _choose_copy_folder(monkeypatch, dest)

    t0 = time.perf_counter()
    w.save_copy_as()
    elapsed = time.perf_counter() - t0
    assert (dest / "card.policy.toml").is_file(), w.statusBar().currentMessage()
    copied = (dest / "session.json").read_bytes()
    assert w._wsm.root() == root                         # the original stays the active experiment
    message = w.output_bar._message.full_text()
    assert f"Copy saved to {dest}" in message and f"new data keeps going to {root.name}" in message
    assert w.output_bar._message_action.text() == "Open copy"

    data = tmp_path / "beam" / "a06.dat"
    w._on_paths_dropped([str(data)])
    argv = runner.instances[-1].argv
    assert argv[argv.index("--workspace") + 1] == str(root)
    assert w.windowTitle() == f"ISAR — {root.name}"
    assert w.experiment_strip._name.full_text() == root.name
    runner.instances[-1].complete(0, [_ingest_ok_line(root)])
    assert shown == [root]                               # the arrival names the original
    assert (dest / "session.json").read_bytes() == copied
    print(f"save copy of a stamped experiment: {elapsed:.3f} s")


def test_the_copy_opens_with_watching_paused_and_the_original_keeps_watching(
        qtbot, monkeypatch, make_ws, tmp_path):
    ws = _ready(make_ws)
    root = Path(ws.root)
    store = GuiStore.load(root)
    store.add_watched(str(tmp_path / "beam"))
    store.save()
    session = Session()
    w = _window(qtbot, Workspace.open(root), session)
    dest = tmp_path / "kept"
    _choose_copy_folder(monkeypatch, dest)
    w.save_copy_as()
    assert GuiStore.load(dest).watch_paused() is True
    assert GuiStore.load(root).watch_paused() is False

    w.output_bar._message_action.click()                 # Open copy
    assert w._wsm.root() == dest and w.files_vm.watch_paused() is True


def test_a_failed_copy_says_why_in_the_error_style(qtbot, monkeypatch, make_ws, tmp_path):
    ws = _ready(make_ws)
    w = _window(qtbot, ws)
    dest = tmp_path / "full"
    dest.mkdir()
    (dest / "x").write_text("x", encoding="utf-8", newline="\n")
    _choose_copy_folder(monkeypatch, dest)
    w.save_copy_as()
    message = w.output_bar._message
    assert "Copy not saved: destination is not empty" in message.full_text()
    assert "color" in message.styleSheet()
    assert w._wsm.root() == Path(ws.root)


# ---- the Output bar and the experiment strip ---------------------------------------------------
def test_output_bar_texts(qtbot, monkeypatch, tmp_path):
    bar = OutputBar()
    qtbot.addWidget(bar)
    assert bar._status.full_text() == "No experiment, so nothing is being written."
    root = tmp_path / "leTacon_SP8_CsV3Sb5"
    bar.set_experiment(root)
    assert bar._status.full_text() == (
        "Results are written to …\\leTacon_SP8_CsV3Sb5 as they arrive · nothing written yet")
    assert str(root) in bar._status.toolTip()
    bar.note_written(datetime.now().replace(hour=6, minute=41, second=30))
    assert bar._status.full_text().endswith("· last written 06:41:30")
    bar.note_written(datetime(2026, 1, 2, 3, 4, 5))
    assert bar._status.full_text().endswith("· last written 2026-01-02 03:04:05")

    bar.show_copy_done(tmp_path / "kept")
    assert bar._message.full_text() == (
        f"Copy saved to {tmp_path / 'kept'} · new data keeps going to leTacon_SP8_CsV3Sb5")
    opened = []
    bar.open_copy_requested.connect(opened.append)
    bar._message_action.click()
    assert opened == [tmp_path / "kept"]

    bar.show_export_progress("12 of 28 spectra")
    assert bar._message.full_text() == "Exporting… 12 of 28 spectra" and bar._message_action.isHidden()

    urls = []
    monkeypatch.setattr("isar_gui.views.output_bar.QDesktopServices.openUrl", urls.append)
    export = tmp_path / "report" / "export-1"
    bar.show_export_done(export)
    assert bar._message.full_text() == f"Export written to {export}"
    assert bar._message_action.text() == "Open folder"
    bar._message_action.click()
    assert [u.toLocalFile() for u in urls] == [export.as_posix()]

    bar.show_export_failed("the engine stopped", r"D:\exp\report\gui-runs\export.log")
    assert bar._message.full_text() == "Export failed: the engine stopped"
    assert bar._log_path.text() == r"Log: D:\exp\report\gui-runs\export.log"
    assert not bar._log_path.isHidden() and bar._message_action.isHidden()
    bar.show_export_failed("no stored results", "")
    assert bar._log_path.isHidden()

    bar.set_experiment(None)
    assert bar._message.isHidden() and bar._status.full_text().startswith("No experiment")


def test_the_availability_rule_on_the_real_output_bar(qtbot, make_ws):
    w = _window(qtbot)
    bar = w.output_bar

    def shown():
        return bar._save_copy.isEnabled(), bar._export.isEnabled(), bar._reason.text()

    assert shown() == (False, False, "need an experiment")
    w.new_workspace()
    assert shown() == (False, False, "no files read into this experiment yet")
    assert w.experiment_strip._busy.isHidden()
    assert w.load_workspace(make_ws(rows=_ROWS))
    assert shown() == (True, True, "")
    for kind, reason in (("ingest", "files are being read into the experiment"),
                         ("run", "analysis is writing to the experiment"),
                         ("export", "an export is running")):
        w._on_operation_activity_changed({**_IDLE, "kind": kind})
        assert shown() == (False, False, reason), kind
        assert w.experiment_strip._busy.text() == reason and not w.experiment_strip._new.isEnabled()
        w._on_operation_activity_changed(_IDLE)
        assert shown() == (True, True, ""), kind


def test_each_activity_kind_has_its_own_word(qtbot, make_ws):
    w = _window(qtbot, _ready(make_ws))
    for kind, word in (("ingest", "Ingesting"), ("run", "Analysing"), ("export", "Exporting"),
                       ("drive", "Campaign running"), ("compile", "Compiling campaign")):
        w._on_operation_activity_changed({**_IDLE, "kind": kind, "queued": 1})
        assert w._operation_status.text() == f"{word} + 1 queued", kind
    w._on_operation_activity_changed({**_IDLE, "kind": "export", "cancelling": True})
    assert w._operation_status.text() == "Cancelling"
    w._on_operation_activity_changed(_IDLE)
    assert w._operation_status.text() == ""


def test_last_written_on_bind_is_the_session_files_time(qtbot, make_ws):
    root = make_ws(rows=_ROWS)
    stamp = time.time() + 120                            # newer than every other watched file
    os.utime(root / "session.json", (stamp, stamp))
    w = _window(qtbot, Workspace.open(root))
    assert f"last written {datetime.fromtimestamp(stamp):%H:%M:%S}" in w.output_bar._status.full_text()


def test_a_missing_source_folder_is_said_in_the_strip_and_the_log(qtbot, make_ws, tmp_path):
    gone = tmp_path / "beam" / "data"
    root = make_ws(rows=_ROWS, files={str(gone / f"a0{i}.dat"): {"format": "x"} for i in (3, 4)})
    w = _window(qtbot, Workspace.open(root))
    warning = w.experiment_strip._warning
    assert not warning.isHidden()
    assert warning.text() == (f"Source data folder not found: {gone} (2 files) — stored scans still "
                              "show; reading or re-running them needs it")
    assert str(gone) in warning.toolTip()
    assert f"Source data folder not found: {gone} (2 files)" in _log_text(w)

    w.load_workspace(make_ws(rows=_ROWS))               # an experiment whose data is all there
    assert warning.isHidden()

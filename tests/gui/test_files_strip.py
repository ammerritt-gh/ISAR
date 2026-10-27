"""FilesStrip tests -- the drop zone and the one line, the Watch dialog, and the arrival tint.

Offscreen Qt with the real widgets. Watched folders live in a real GuiStore under ``tmp_path``; the
watcher is driven by hand (``poll()``), never started. File dialogs are stubbed at the module seam.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QMimeData, QUrl

from isar_gui.commands import GuiIngestReport
from isar_gui.folder_watcher import FolderWatcher, WatchStatus
from isar_gui.gui_store import GuiStore
from isar_gui.viewmodels.files import FilesViewModel
from isar_gui.views import files_strip as strip_mod
from isar_gui.views import watch_dialog as dialog_mod
from isar_gui.views.files_strip import FilesStrip
from isar_gui.views.watch_dialog import WatchDialog


class _FakeDrop:
    def __init__(self, paths):
        md = QMimeData()
        md.setUrls([QUrl.fromLocalFile(p) for p in paths])
        self._md = md

    def mimeData(self):
        return self._md

    def acceptProposedAction(self):
        pass


def _file(name, scan_ids=("exp:1",)):
    return SimpleNamespace(path=rf"D:\beam\{name}", size=1, mtime=1.0, classification="measurement",
                           n_scans=len(scan_ids), scan_ids=tuple(scan_ids), error=None)


def _vm(tmp_path, files=(), store=True):
    ws = SimpleNamespace(files=list(files), last_ingest=None, root=tmp_path / "exp_a")
    return FilesViewModel(ws, store=GuiStore.load(tmp_path) if store else None)


def _strip(qtbot, vm, experiment_open=True):
    strip = FilesStrip()
    qtbot.addWidget(strip)
    strip.bind(vm, experiment_open)
    return strip


def test_drop_zone_without_an_experiment_or_files(qtbot, tmp_path):
    strip = _strip(qtbot, _vm(tmp_path, store=False), experiment_open=False)
    assert not strip._zone.isHidden() and strip._line.isHidden()
    assert strip._title.text() == "Drop data files or a folder here"
    assert "creates the experiment" in strip._note.text()

    strip = _strip(qtbot, _vm(tmp_path), experiment_open=True)           # open, but nothing read yet
    assert not strip._zone.isHidden() and strip._line.isHidden()
    assert strip._note.text() == "Files you drop are read into this experiment."


def test_one_line_counts_files_and_distinct_scan_sets(qtbot, tmp_path):
    files = [_file("CsV_a03", ("exp:1842",)), _file("CsV_a04", ("exp:1842",)), _file("Ba_a01", ("exp:58",))]
    strip = _strip(qtbot, _vm(tmp_path, files))
    assert strip._zone.isHidden() and not strip._line.isHidden()
    assert strip._line_text.full_text() == "3 files · 2 scan sets · drop more here"
    assert strip._watch.isEnabled()


def test_watch_is_disabled_with_the_reason_without_a_store(qtbot, tmp_path):
    strip = _strip(qtbot, _vm(tmp_path, [_file("a.dat")], store=False))
    assert not strip._watch.isEnabled()
    assert strip._line_text.full_text().endswith("needs an experiment with data")


def test_watch_suffixes_and_attention(qtbot, tmp_path):
    vm = _vm(tmp_path,[_file("a.dat")])
    strip = _strip(qtbot, vm)
    vm.add_watch(r"D:\beam\numors")
    assert strip._line_text.full_text().endswith(r"· watching D:\beam\numors")

    vm.add_watch(r"D:\beam\more")
    assert strip._line_text.full_text().endswith("· watching 2 folders")

    strip.set_watch_status(WatchStatus(paused=True))
    assert strip._line_text.full_text().endswith("· watching paused")
    assert strip._attention.isHidden()

    strip.set_watch_status(WatchStatus(retrying=(r"D:\beam\bad.dat",)))
    assert not strip._attention.isHidden()
    assert strip._attention_word.text() == "watch needs attention"
    assert strip._attention_word.toolTip() == "Retrying 1 file(s) that could not be parsed"


def test_drops_and_add_choices_leave_as_native_absolute_paths(qtbot, tmp_path, monkeypatch):
    chosen = [str(tmp_path / "a.dat"), str(tmp_path / "b.dat")]
    folder = str(tmp_path / "numors")
    monkeypatch.setattr(strip_mod, "QFileDialog", SimpleNamespace(
        getOpenFileNames=lambda *a, **k: (chosen, ""), getExistingDirectory=lambda *a, **k: folder))
    strip = _strip(qtbot, _vm(tmp_path, store=False), experiment_open=False)
    got = []
    strip.paths_dropped.connect(got.append)

    strip._add_files.click()
    strip._add_folder.click()
    strip.dropEvent(_FakeDrop([chosen[0]]))
    assert got == [chosen, [folder], [str(Path(chosen[0]))]]

    got.clear()
    strip.bind(_vm(tmp_path, [_file("a.dat")]), True)           # the one-line form's Add… menu
    add_files, add_folder = strip._add.menu().actions()
    add_files.trigger()
    add_folder.trigger()
    assert got == [chosen, [folder]]


def test_watch_dialog_add_remove_and_pause_reach_the_store(qtbot, tmp_path, monkeypatch):
    folder = str(tmp_path / "numors")
    monkeypatch.setattr(dialog_mod, "QFileDialog",
                        SimpleNamespace(getExistingDirectory=lambda *a, **k: folder))
    vm = _vm(tmp_path,[_file("a.dat")])
    strip = _strip(qtbot, vm)
    changes = []
    vm.watch_config_changed.connect(lambda: changes.append(1))

    strip._open_watch()
    dialog = strip._watch_dialog
    assert isinstance(dialog, WatchDialog) and dialog.isVisible()
    dialog._add()
    assert vm.watched() == [folder] and dialog._list.count() == 1 and changes == [1]

    dialog._pause.setChecked(True)
    assert vm.watch_paused() is True and len(changes) == 2

    dialog._list.item(0).setSelected(True)
    dialog._remove()
    assert vm.watched() == [] and changes == [1, 1, 1]


def test_watch_dialog_remove_stops_every_selected_folder(qtbot, tmp_path):
    vm = _vm(tmp_path, [_file("a.dat")])
    vm.add_watch(r"D:\beam\numors")
    vm.add_watch(r"D:\beam\more")
    strip = _strip(qtbot, vm)
    strip._open_watch()
    dialog = strip._watch_dialog
    dialog._list.selectAll()
    dialog._remove()                               # each removal re-renders the list under the loop
    assert vm.watched() == [] and dialog._list.count() == 0


def test_last_check_line_follows_a_hand_driven_poll(qtbot, tmp_path):
    folder = tmp_path / "data"
    folder.mkdir()
    vm = _vm(tmp_path,[_file("a.dat")])
    vm.add_watch(str(folder))
    watcher = FolderWatcher(poll_ms=5, stable_polls=2)
    watcher.set_folders([str(folder)])
    strip = _strip(qtbot, vm)
    watcher.status_changed.connect(strip.set_watch_status)
    strip._open_watch()
    dialog = strip._watch_dialog
    assert dialog._last.text() == "No new files found since watching started"

    (folder / "s.dat").write_bytes(b"done")
    watcher.poll()
    watcher.poll()                                 # settles on the second sweep
    assert dialog._last.text().startswith("Last check that found files: ")
    assert dialog._last.text().endswith("· 1 new file")


def test_rebinding_to_another_viewmodel_closes_the_dialog(qtbot, tmp_path):
    strip = _strip(qtbot, _vm(tmp_path,[_file("a.dat")]))
    strip._open_watch()
    dialog = strip._watch_dialog
    strip.bind(_vm(tmp_path / "other", [_file("b.dat")], store=False), True)
    assert strip._watch_dialog is None and not dialog.isVisible()


def test_show_arrival_tints_the_strip_and_logs_what_arrived(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(strip_mod, "_HIGHLIGHT_MS", 20)
    strip = _strip(qtbot, _vm(tmp_path,[_file("a.dat")]))
    lines = []
    strip.log_message.connect(lines.append)

    strip.show_arrival(GuiIngestReport())               # no files: nothing happens
    assert lines == [] and strip.styleSheet() == ""

    report = GuiIngestReport(
        files=({"path": r"D:\beam\a.dat", "error": None}, {"path": r"D:\beam\b.dat", "error": "boom"}),
        added=("exp:1",), last_ingest={"logical_scan_count": 2, "analyzer_unit_count": 5})
    strip.show_arrival(report)
    assert "#files_strip" in strip.styleSheet()
    assert lines == ["Read 2 file(s) into exp_a: 2 scan set(s), 5 analyzer(s); 1 not read"]
    qtbot.waitUntil(lambda: strip.styleSheet() == "", timeout=2000)

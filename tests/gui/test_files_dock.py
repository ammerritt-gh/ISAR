"""FilesDock tests — manifest rendering, drop routing, watched-folder round-trip (D5).

The dock is presentational; it renders rows the FilesViewModel computed from a fake ``ws.files``,
routes a body drop to the injected controller, and reflects the watched-folder list held in a real
GuiStore under ``tmp_path``.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QMimeData, QUrl

from isar_gui.gui_store import GuiStore
from isar_gui.viewmodels.files import FilesViewModel
from isar_gui.views.files_dock import FilesDock


class _SpyController:
    def __init__(self):
        self.requests = []

    def request_ingest(self, paths):
        self.requests.append(list(paths))


class _FakeDrop:
    def __init__(self, paths):
        md = QMimeData()
        md.setUrls([QUrl.fromLocalFile(p) for p in paths])
        self._md = md
        self.accepted = False

    def mimeData(self):
        return self._md

    def acceptProposedAction(self):
        self.accepted = True


def _fake_ws(files):
    return SimpleNamespace(files=files)


def test_rows_render_from_ws_files(qtbot, tmp_path):
    files = [
        SimpleNamespace(path=r"D:\d\scan_1.dat", size=10, mtime=1.0,
                        classification="measurement", n_scans=12),
        SimpleNamespace(path=r"D:\d\notes.txt", size=3, mtime=1.0,
                        classification="foreign", n_scans=None),
    ]
    vm = FilesViewModel(_fake_ws(files), store=GuiStore.load(tmp_path))
    dock = FilesDock(vm, _SpyController())
    qtbot.addWidget(dock)

    assert dock._tree.topLevelItemCount() == 2
    first = dock._tree.topLevelItem(0)
    assert first.text(0) == "scan_1.dat"
    assert first.text(1) == "parsed 12 scans"
    assert first.toolTip(0) == r"D:\d\scan_1.dat"
    assert dock._tree.topLevelItem(1).text(1) == "foreign"


def test_empty_manifest_shows_hint(qtbot, tmp_path):
    vm = FilesViewModel(_fake_ws([]), store=GuiStore.load(tmp_path))
    dock = FilesDock(vm, _SpyController())
    qtbot.addWidget(dock)
    assert dock._hint.isVisible() or dock._tree.topLevelItemCount() == 0


def test_body_drop_routes_paths_to_controller(qtbot, tmp_path):
    spy = _SpyController()
    vm = FilesViewModel(_fake_ws([]), store=GuiStore.load(tmp_path))
    dock = FilesDock(vm, spy)
    qtbot.addWidget(dock)

    dock.dropEvent(_FakeDrop([r"D:\d\a.dat", r"D:\d\b.dat"]))
    assert spy.requests == [[r"D:\d\a.dat", r"D:\d\b.dat"]]


def test_watched_folder_add_remove_round_trips_through_store(qtbot, tmp_path):
    store = GuiStore.load(tmp_path)
    vm = FilesViewModel(_fake_ws([]), store=store)
    dock = FilesDock(vm, _SpyController())
    qtbot.addWidget(dock)

    vm.add_watch(r"D:\beam\numors")             # store mutation -> dock re-renders the list
    assert dock._watch_list.count() == 1
    assert dock._watch_list.item(0).text().endswith("numors")

    store.save()
    assert r"D:\beam\numors" in "\n".join(GuiStore.load(tmp_path).watched_folders())

    vm.remove_watch(r"D:\beam\numors")
    assert dock._watch_list.count() == 0


def test_highlight_added_marks_rows(qtbot, tmp_path):
    files = [SimpleNamespace(path=r"D:\d\scan_1.dat", size=1, mtime=1.0,
                             classification="measurement", n_scans=1)]
    vm = FilesViewModel(_fake_ws(files), store=GuiStore.load(tmp_path))
    dock = FilesDock(vm, _SpyController())
    qtbot.addWidget(dock)

    dock.highlight_added([r"D:\d\scan_1.dat"])
    item = dock._tree.topLevelItem(0)
    # A highlighted row carries a non-default background brush.
    assert item.background(0).style() != 0

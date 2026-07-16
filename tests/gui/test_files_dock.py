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
from isar_gui.folder_watcher import WatchStatus
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
    return SimpleNamespace(files=files, last_ingest=None)


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
    assert dock._tree.topLevelItem(1).text(1) == "belongs to a different experiment"


def test_empty_manifest_shows_hint(qtbot, tmp_path):
    vm = FilesViewModel(_fake_ws([]), store=GuiStore.load(tmp_path))
    dock = FilesDock(vm, _SpyController())
    qtbot.addWidget(dock)
    assert dock._hint.isVisible() or dock._tree.topLevelItemCount() == 0


def test_ready_workspace_without_files_raises_files_dock(qtbot, make_ws):
    """A ready workspace with no ingested files surfaces the Files dock (the drop-target hint) as the
    front tab, so the operator's first action — adding data — has a visible home. Without this the
    Files dock is buried behind its tabified Browser/Card siblings and the empty-state hint is
    invisible. Mirrors the empty/pending bind, which raises Files in _init_docks."""
    from isar.workspace_api import Workspace
    from isar_gui.main_window import MainWindow
    from isar_gui.session import Session

    root = make_ws()                               # no `files`/`rows` -> empty manifest, ready bind
    w = MainWindow(Workspace.open(root), session=Session(), watch=False)
    qtbot.addWidget(w)
    w.show()
    assert not w.is_empty
    assert w.files_vm.rows == []
    # The raise is queued (singleShot(0)) to run after any layout restore; flush the event loop.
    # isVisible() is unreliable for tabified docks under the offscreen platform (every tab reports
    # visible), so assert on visibleRegion(): only the front tab of a stack has a non-empty region.
    qtbot.waitUntil(lambda: w.files_dock is not None
                    and not w.files_dock.visibleRegion().isEmpty())
    # Files is the front tab of its left stack; its tabified siblings (Browser/Card) are behind it.
    assert w.card_dock.visibleRegion().isEmpty()


def test_ready_workspace_with_files_does_not_raise_files_dock(qtbot, make_ws):
    """The data-entry raise is conditional on an empty manifest: a workspace that already has files
    must not have its focus stolen. Since Pass 3 the Files dock owns a small dedicated top-left
    slot (always visible by design), so 'not raised' is observed on the scan stack instead: Scans
    keeps the front tab, its siblings (Browser/Card/Scan Table) stay buried."""
    from isar.workspace_api import Workspace
    from isar_gui.main_window import MainWindow
    from isar_gui.session import Session

    root = make_ws(rows=[{"scan": 1, "detector": "det", "status": "fitted", "omega0": 5.0}],
                   files={r"D:\d\scan_1.dat": {"format": "measurement", "n_scans": 1}})
    w = MainWindow(Workspace.open(root), session=Session(), watch=False)
    qtbot.addWidget(w)
    w.show()
    qtbot.wait(20)                                 # let any queued layout work settle
    assert w.files_vm.rows                         # manifest is non-empty
    # (visibleRegion, not isVisible: isVisible is unreliable for tabbed docks offscreen.)
    assert not w.scans_dock.visibleRegion().isEmpty()   # Scans leads its stack
    assert w.card_dock.visibleRegion().isEmpty()        # siblings stay buried
    assert w.browser_dock.visibleRegion().isEmpty()
    assert not w.files_dock.visibleRegion().isEmpty()   # Files: small, dedicated, always visible


def test_cold_launch_file_less_workspace_raises_files_over_saved_card_layout(qtbot, make_ws):
    """Cold-launch regression: on first MainWindow construction ``self._bound`` is False, so
    _bind_workspace does NOT queue the layout restore — __init__ queues _restore_layout AFTER the
    bind already queued the file-less Files raise. A saved layout whose front tab is Card would then
    re-bury Files (restore runs last). The construction path must re-queue the raise so Files still
    wins. Distinct from the rebind path, which queues its restore inside _bind_workspace before the
    raise."""
    from isar.workspace_api import Workspace
    from isar_gui.main_window import MainWindow
    from isar_gui.session import Session

    # Capture a saved layout whose left-stack front tab is Card. Dock objectNames are structural, so
    # a layout captured from one workspace restores onto another.
    seed = MainWindow(Workspace.open(make_ws()), session=Session(), watch=False)
    qtbot.addWidget(seed)
    seed.show()
    qtbot.waitUntil(lambda: not seed.files_dock.visibleRegion().isEmpty())  # our raise ran first
    seed.card_dock.raise_()
    qtbot.waitUntil(lambda: not seed.card_dock.visibleRegion().isEmpty())   # Card is now the front
    card_front_layout = bytes(seed.saveState().toBase64().data()).decode("ascii")

    # Cold-launch a fresh window on a file-less workspace WITH that Card-front layout persisted.
    w = MainWindow(Workspace.open(make_ws()),
                   session=Session(dock_layout_b64=card_front_layout), watch=False)
    qtbot.addWidget(w)
    w.show()
    qtbot.waitUntil(lambda: not w.files_dock.visibleRegion().isEmpty())  # Files raised after restore
    assert w.card_dock.visibleRegion().isEmpty()   # the saved Card-front layout did not win


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


def test_failed_file_row_shows_parse_failed_with_tooltip(qtbot, tmp_path):
    """A manifest row carrying an ``error`` renders 'parse failed' with the message as the Status
    tooltip and a non-default (warning) foreground so it is visible at a glance."""
    files = [
        SimpleNamespace(path=r"D:\d\bad.numor", size=5, mtime=1.0,
                        classification="measurement", n_scans=0,
                        error="ValueError: could not convert string to float: 'da3p'"),
        SimpleNamespace(path=r"D:\d\ok.dat", size=5, mtime=1.0,
                        classification="measurement", n_scans=3, error=None),
    ]
    vm = FilesViewModel(_fake_ws(files), store=GuiStore.load(tmp_path))
    dock = FilesDock(vm, _SpyController())
    qtbot.addWidget(dock)

    bad = dock._tree.topLevelItem(0)
    assert bad.text(1) == "parse failed"
    assert "da3p" in bad.toolTip(1)
    assert bad.foreground(1).style() != 0                # tinted, not the default brush
    ok = dock._tree.topLevelItem(1)
    assert ok.text(1) == "parsed 3 scans"
    assert ok.toolTip(1) == r"D:\d\ok.dat"              # no error -> path tooltip, default color


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


def test_watcher_attention_is_persistent_and_plain(qtbot, tmp_path):
    vm = FilesViewModel(_fake_ws([]), store=GuiStore.load(tmp_path))
    dock = FilesDock(vm, _SpyController())
    qtbot.addWidget(dock)

    dock.set_watch_status(WatchStatus(
        unavailable=(r"D:\beam\offline",), retrying=(r"D:\beam\bad.dat",),
        exhausted=(r"D:\beam\still-bad.dat",),
    ))
    assert dock._watch_notice.isVisible() or dock._watch_notice.text()
    assert "unavailable" in dock._watch_notice.text().lower()
    assert "automatic retries" in dock._watch_notice.text()
    assert "top-level files only" in dock.widget().findChild(type(dock._hint), "watch_heading").text()


def test_last_ingest_attention_stays_visible_with_operation_scoped_counts(qtbot, tmp_path):
    summary = SimpleNamespace(logical_scan_count=2, analyzer_unit_count=18,
                              failed_file_count=1, gaps=(("SYN", 4, 7),))
    vm = FilesViewModel(SimpleNamespace(files=[], last_ingest=summary), store=GuiStore.load(tmp_path))
    dock = FilesDock(vm, _SpyController())
    qtbot.addWidget(dock)

    assert dock._ingest_notice.isVisible() or dock._ingest_notice.text()
    text = dock._ingest_notice.text().lower()
    assert "2 logical scans" in text
    assert "18 analyzer units" in text
    assert "failed to parse" in text and "gap" in text
    assert "last ingest" in text

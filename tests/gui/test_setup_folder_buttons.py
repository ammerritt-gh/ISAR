"""The setup panel's two folder buttons (a check or a cross, the path and the reason on hover and to a reader), and
the Add data button, which must never be cut.

Offscreen Qt with the real widgets over a generated BL43 workspace (``bl43_fixture.build_bl43_gpi_workspace``).
Renders go to ``tmp_path``; the operator's copy in the main checkout is made by hand, never by a test.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QFileDialog

import bl43_fixture
from isar.intake import settings as intake_settings
from isar.workspace_api.intake_view import FolderStatus
from isar_gui.gui_store import GuiStore
from isar_gui.viewmodels.files import FilesViewModel
from isar_gui.viewmodels.intake import IntakeState
from isar_gui.views import setup_section as setup_mod
from isar_gui.views.files_strip import FilesStrip
from isar_gui.views.setup_section import SetupSection, locate_resolution_folder

REPO = Path(__file__).resolve().parents[2]
WAIT_MS = 15000
MIN_WIDTH = 280                     # the left column's minimum (main_window._LEFT_MIN)


def test_the_worktree_is_the_import_root():
    import isar_gui
    print("isar_gui imported from", isar_gui.__file__)
    assert Path(isar_gui.__file__).resolve().is_relative_to(REPO)


def _bound(qtbot, root) -> IntakeState:
    intake = IntakeState()
    with qtbot.waitSignal(intake.worker_changed, timeout=WAIT_MS):
        intake.bind(root)
    return intake


def _section(qtbot, intake, width=340) -> SetupSection:
    section = SetupSection(intake)
    qtbot.addWidget(section)
    section.resize(width, 200)
    section.show()
    qtbot.waitExposed(section)
    return section


def test_both_folders_are_good_and_say_where_they_are(qtbot, tmp_path):
    root = bl43_fixture.build_bl43_gpi_workspace(tmp_path, gpi="accepted")
    section = _section(qtbot, _bound(qtbot, root))
    rf, gpi = section._locate, section._gpi_locate
    assert rf.isVisible() and gpi.isVisible()
    assert rf.accessibleName() == "Resolution curves folder: good"
    assert gpi.accessibleName() == "gpi files folder: good"
    assert not rf.icon().isNull() and not gpi.icon().isNull()
    assert rf.toolTip().startswith("Resolution curves (rf)…: good\n")
    assert f"{(root / 'rf').resolve()}\nchosen by you" in rf.toolTip()
    assert gpi.toolTip().startswith("gpi files…: good\n") and "found beside the data" in gpi.toolTip()


def test_a_missing_curve_crosses_the_rf_folder_and_counts_the_analyzers(qtbot, tmp_path):
    root = bl43_fixture.build_bl43_gpi_workspace(tmp_path, drop_curve="a04")
    section = _section(qtbot, _bound(qtbot, root))
    assert section._locate.accessibleName() == "Resolution curves folder: not good"
    assert section._locate.accessibleDescription() == "1 analyzer has no resolution curve"
    assert section._locate.toolTip().startswith("Resolution curves (rf)…: not good\n1 analyzer has no resolution curve")


def test_a_rejected_gpi_file_crosses_the_gpi_button_with_its_reason(qtbot, tmp_path):
    root = bl43_fixture.build_bl43_gpi_workspace(tmp_path, gpi="rejected")
    section = _section(qtbot, _bound(qtbot, root))
    assert section._gpi_locate.accessibleName() == "gpi files folder: not good"
    assert "not used" in section._gpi_locate.accessibleDescription()
    assert "not used" in section._gpi_locate.toolTip()


def test_a_fresh_bl43_experiment_with_nothing_imported_crosses_both_folders_with_the_reason(qtbot, tmp_path):
    import intake_fixtures as fx
    ws, _data = fx.bl43_workspace(tmp_path, stems=())        # the folder saved, nothing imported yet
    intake = _bound(qtbot, ws.root)
    assert not intake.attention.evaluated                    # the readiness is never judged before an import
    section = _section(qtbot, intake)
    rf, gpi = section._locate, section._gpi_locate
    assert rf.accessibleName() == "Resolution curves folder: not good" and rf.accessibleDescription()
    assert not rf.icon().isNull() and not gpi.icon().isNull()
    assert rf.toolTip().startswith("Resolution curves (rf)…: not good\n")
    assert gpi.accessibleName() == "gpi files folder: not good"
    assert gpi.accessibleDescription() == "no gpi folder is chosen"


def test_the_two_gpi_crosses_without_a_needs_attention_item_say_why_on_hover_and_to_a_reader(
        qtbot, monkeypatch, tmp_path):
    root = bl43_fixture.build_bl43_gpi_workspace(tmp_path, gpi="none")
    section = _section(qtbot, _bound(qtbot, root))
    gpi = section._gpi_locate
    assert gpi.accessibleName() == "gpi files folder: not good"
    assert gpi.accessibleDescription() == "no gpi folder is chosen"
    assert gpi.toolTip().startswith("gpi files…: not good\nno gpi folder is chosen")
    # a folder chosen that holds no file for these scans: the facade judges it, the panel only shows the reason
    other = FolderStatus(False, str(tmp_path / "gpi"), "chosen by you", "no gpi file matches the imported scans",
                         "gpi files folder: not good")
    monkeypatch.setattr(setup_mod, "folder_status", lambda *_a: (None, other))
    section._render()
    assert gpi.accessibleDescription() == "no gpi file matches the imported scans"
    assert "no gpi file matches the imported scans" in gpi.toolTip()


def test_a_card_that_reads_no_bl43_files_shows_neither_folder_button(qtbot, tmp_path):
    root = bl43_fixture.build_bl43_gpi_workspace(tmp_path)
    intake = _bound(qtbot, root)
    intake._summary = dataclasses.replace(intake.summary, curves=None)
    section = _section(qtbot, intake)
    assert section._locate.isHidden() and section._gpi_locate.isHidden()


def test_repointing_a_folder_twice_saves_each_as_the_users_and_leaves_the_other_alone(qtbot, monkeypatch, tmp_path):
    root = bl43_fixture.build_bl43_gpi_workspace(tmp_path)
    intake = _bound(qtbot, root)
    section = _section(qtbot, intake)
    section.locate_requested.connect(lambda: locate_resolution_folder(section, intake))
    gpi_before = section._gpi_locate.accessibleName()
    for name in ("first", "second"):
        chosen = tmp_path / name
        chosen.mkdir()
        monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *_a, _c=chosen, **_k: str(_c))
        with qtbot.waitSignal(intake.worker_changed, timeout=WAIT_MS):
            section._locate.click()
        saved = intake_settings.current_settings(root).resolution_folder
        assert (saved.path, saved.source) == (str(chosen.resolve()), "user")
        assert str(chosen.resolve()) in section._locate.toolTip()
    assert section._gpi_locate.accessibleName() == gpi_before


def test_at_the_minimum_width_the_buttons_share_a_row_and_keep_their_words(qtbot, tmp_path):
    root = bl43_fixture.build_bl43_gpi_workspace(tmp_path)
    section = _section(qtbot, _bound(qtbot, root), width=MIN_WIDTH)
    rf, gpi = section._locate, section._gpi_locate
    assert abs(rf.y() - gpi.y()) <= 3 and rf.x() < gpi.x()
    for button in (rf, gpi):
        assert button.text() == button.full_text()
        assert section.rect().contains(button.geometry())
    assert section.minimumSizeHint().width() <= MIN_WIDTH


def test_a_hostile_long_path_stays_on_hover_and_widens_nothing(qtbot, tmp_path):
    root = bl43_fixture.build_bl43_gpi_workspace(tmp_path)
    intake = _bound(qtbot, root)
    long = str(tmp_path / ("a_very_long_curve_folder_name_" * 8))
    summary = intake.summary
    intake._summary = dataclasses.replace(summary, curves=dataclasses.replace(summary.curves, folder=long,
                                                                              check_stale=True))
    section = _section(qtbot, intake, width=MIN_WIDTH)
    section._render()
    assert long in section._locate.toolTip()
    assert section._locate.text() == section._locate.full_text() == "Resolution curves (rf)…"
    assert section.minimumSizeHint().width() <= MIN_WIDTH


def test_the_panel_renders_to_a_png(qtbot, tmp_path):
    root = bl43_fixture.build_bl43_gpi_workspace(tmp_path)
    section = _section(qtbot, _bound(qtbot, root))
    out = tmp_path / "setup-panel.png"
    assert section.grab().save(str(out)) and out.stat().st_size > 0


def _strip(qtbot, tmp_path) -> FilesStrip:
    f = SimpleNamespace(path=r"D:\beam\a.dat", size=1, mtime=1.0, classification="measurement",
                        n_scans=1, scan_ids=("exp:1",), error=None)
    ws = SimpleNamespace(files=[f], last_ingest=None, root=tmp_path / "exp_a")
    strip = FilesStrip()
    qtbot.addWidget(strip)
    strip.bind(FilesViewModel(ws, store=GuiStore.load(tmp_path)), True)
    strip.resize(MIN_WIDTH, 200)
    strip.show()
    qtbot.waitExposed(strip)
    return strip


def _label_left(widget) -> int:
    """The leftmost column where the button's text is drawn: the button grabbed with its text and without it
    (same size), so the frame, bevel and any menu indicator cancel out."""
    text = widget.text()
    widget.setFixedWidth(widget.width())            # clearing the text must not change the size
    drawn = widget.grab().toImage()
    widget.setText("")
    bare = widget.grab().toImage()
    widget.setText(text)
    return min(x for x in range(drawn.width()) for y in range(drawn.height() // 3, 2 * drawn.height() // 3)
               if drawn.pixel(x, y) != bare.pixel(x, y))


# the default test font, and the Windows style at a large Segoe UI, where a full row cut the label
@pytest.mark.parametrize("family, point_size, style", [("Sans Serif", 9, "fusion"), ("Segoe UI", 13, "windows11")])
def test_add_data_keeps_its_whole_text_at_the_minimum_width(qtbot, tmp_path, family, point_size, style):
    app = QApplication.instance()
    old_font, old_style = app.font(), app.style().name()
    app.setStyle(style)
    app.setFont(QFont(family, point_size))
    try:
        strip = _strip(qtbot, tmp_path)
        add = strip._add
        assert add.text() == add.full_text() == "Add data ▾"
        # the drawn text: a centred label moves half of any extra width; a label cut at its left edge does not
        left_narrow = _label_left(add)
        add.setFixedWidth(add.width() + 40)
        left_wide = _label_left(add)
        assert left_wide - left_narrow == pytest.approx(20, abs=2)
    finally:
        app.setFont(old_font)
        app.setStyle(old_style)

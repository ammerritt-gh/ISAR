"""Helpers for the Analyzer array tests (a plain module, not a test file).

``make_array_view`` builds the read layer's ``ArrayView`` by hand for the BL43LXU numbering, so positions, labels
and the delta-hkl words are asserted without a workspace. Every hkl is made up (hard rule 10): the default is
``(0.5 + 0.1 p, 0.5 + 0.1 r, 3.0)`` for in-plane position ``p`` (0 = a03) in row ``r`` (0 = the scattering plane).
``make_env`` / ``open_dock`` build the real view model (and dock) over a generated workspace.
"""
from __future__ import annotations

from types import SimpleNamespace

from isar.schema.analyzer_layout import AnalyzerLayout
from isar.workspace_api import Workspace
from isar.workspace_api.analyzer_array import (ArrayCell, ArrayHeader, ArrayView, _neighbour_pairs, _summary)
from isar.workspace_api.inter_spectra import SpectrumInter
from isar.workspace_api.scan_list import Fact
from isar_gui.viewmodels.analyzer_array import (ArrayState, AnalyzerArrayViewModel, CellState, CellPlot)
from isar_gui.viewmodels.scan_list import ScanListViewModel
from isar_gui.viewmodels.scans_page import ScansPageViewModel
from isar_gui.views.analyzer_array_dock import AnalyzerArrayDock

ROWS = (("a03", "a04", "a05", "a06", "a07", "a08", "a09"),
        ("a14", "a15", "a16", "a17", "a18", "a19", "a20"),
        ("a25", "a26", "a27", "a28", "a29", "a30", "a31"),
        ("a35", "a36", "a37", "a38", "a39", "a40", "a41"))
LAYOUT = AnalyzerLayout("BL43LXU-7x4", ROWS)
NO_FILE = "no file for this analyzer in this scan"


def rlu(h: float, k: float, l: float) -> Fact:
    return Fact((h, k, l), "rlu", "header", "file")


def made_up_hkl(r: int, p: int) -> tuple[float, float, float]:
    return 0.5 + 0.1 * p, 0.5 + 0.1 * r, 3.0


def make_array_view(set_key: str, present=None, *, hkl=made_up_hkl, no_hkl=(), scan_label="13",
                    experiment_id="exp1", unplaced=(), layout: AnalyzerLayout | None = LAYOUT) -> ArrayView:
    """The 4 x 7 BL43LXU view of ``set_key``. ``present``: the beamline names that have a file (None: all);
    ``no_hkl``: names whose hkl is missing."""
    cells, values = [], {}
    for r, names in enumerate(ROWS):
        for p, name in enumerate(names):
            has = present is None or name in present
            key = f"{set_key}:{name}" if has else None
            fact = rlu(*hkl(r, p)) if has and name not in no_hkl else Fact(None, "rlu", "missing", "missing", NO_FILE)
            if name not in no_hkl:        # the steps are the whole array's, whatever files this scan has
                values[name] = hkl(r, p)
            q_abs = (Fact(round(20.0 + p / 10 + r, 3), "nm⁻¹", "header", "file") if has
                     else Fact(None, None, "missing", "missing", NO_FILE))
            cells.append(ArrayCell(internal=layout.internal_name(name), beamline=name, row=r, pos=p,
                                   screen_row=layout.n_rows - 1 - r, screen_col=layout.n_cols - 1 - p,
                                   key=key, hkl=fact, q_abs=q_abs))
    in_pairs, out_pairs = _neighbour_pairs(layout)
    header = ArrayHeader(instrument="BL43LXU", experiment_id=experiment_id, scan_label=scan_label,
                         scan_numbers=(13,), sample=Fact("Sr2RuO4", None, "card", "card"),
                         temperature=Fact(97.0, "K", "header", "file"), hkl_nominal=rlu(0.5, 0.5, 3.0),
                         n_spectra=sum(c.key is not None for c in cells))
    return ArrayView(set_key, header, layout, "the only BL43LXU layout", tuple(cells), tuple(unplaced),
                     _summary(layout, in_pairs, values), _summary(layout, out_pairs, values))


def inter_row(key: str, fit_id: str = "fast", **overrides) -> SpectrumInter:
    fields = dict(key=key, fit_id=fit_id, in_use=fit_id != "fast", source="batch" if fit_id != "fast" else "fast",
                  label="Batch 1 · ring 2" if fit_id != "fast" else "Fast solo", flag=None, locked=False,
                  locked_fit_id=None, lock_stale=False, stale=False, stale_text=None)
    return SpectrumInter(**{**fields, **overrides})


def make_env(ws_root, *, inter=None, active=True) -> SimpleNamespace:
    """A real workspace's scan list, Scan(s) view model and Analyzer array view model, wired as the window does."""
    ws = Workspace.open(ws_root)
    scan_list = ScanListViewModel(ws)
    page = ScansPageViewModel(ws, scan_list)
    vm = AnalyzerArrayViewModel(ws, scan_list, page.reference_for)
    if inter is not None:
        scan_list.set_inter(inter)
        page.set_inter(inter)
        vm.set_inter(inter)
    vm.set_active(active)
    return SimpleNamespace(ws=ws, scan_list=scan_list, page=page, vm=vm, selection=scan_list.selection)


def open_dock(qtbot, ws_root, *, inter=None, size=(1060, 800)) -> SimpleNamespace:
    """``make_env`` plus a shown dock (its own top-level window), as a test sees it."""
    env = make_env(ws_root, inter=inter, active=False)
    env.dock = AnalyzerArrayDock(env.vm)
    qtbot.addWidget(env.dock)
    env.dock.resize(*size)
    env.dock.show()
    qtbot.waitExposed(env.dock)
    return env


def hostile_state(*, rows: int = 4, cols: int = 7) -> ArrayState:
    """A grid state with the worst strings the design lets through: 90-character names, long hkl and |Q|, a 160
    character absence reason, non-ASCII text, and "not zeroed" in every cell. Nothing in it is a measured value."""
    long90 = "Ünïcode-Sr2RuO4-" + "x" * 74
    cells = []
    for r, names in enumerate(ROWS[:rows]):
        for p, name in enumerate(names[:cols]):
            cells.append(CellState(
                beamline=name, internal=LAYOUT.internal_name(name), screen_row=rows - 1 - r,
                screen_col=cols - 1 - p, key=f"{long90}:13:{name}", hkl="-123.456, -123.456, -123.456",
                hkl_tip="-123.456, -123.456, -123.456 r.l.u. (from the file's own words)",
                q="|Q| 12345.67 nm⁻¹", q_tip="|Q| 12345.67 nm⁻¹ (header)", selected=(p + r) % 3 == 0,
                anchor=p == 1, lock=p == 2, absence="not zeroed", absence_tip="why " * 40, plot=None,
                y_range=None, x_range=None, fit_label=None))
    return ArrayState(
        body="grid", title=f"{long90} · scan 13 · {long90} · 97 K · hkl 0.50, 0.50, 3.00", title_tip="tip " * 30,
        selection_note="Showing scan 13 — 12 scans selected, only one is shown",
        layout_line="Layout BL43LXU-7x4 (the only BL43LXU layout)",
        unplaced_line="3 spectra not in layout BL43LXU-7x4: a97, a98, a99", unplaced_tip="a97\na98\na99",
        scale="normalised", y_mode="shared", limit_text="123456789", limit_placeholder="largest shown (123456789)",
        limit_unit="Counts per 10⁵ monitor", limit_enabled=True, limit_reason=None,
        x_label="In plane — 2θ increases ←   Δhkl ≈ (-123.456, -123.456, -123.456) per analyzer (from 20 of 24 pairs)",
        x_tip="A1 → A2  (0.1, 0, 0)", y_label="Out of plane ↑   Δhkl ≈ (-123.456, -123.456, -123.456) per row",
        y_tip="A1 → B1  (0, 0.1, 0)", caption="x: energy transfer (meV), the same in every cell · y: Counts, linear",
        n_rows=rows, n_cols=cols, cells=tuple(cells), batch_line="Anchored batch · ring 2 · 9 of 24 done " * 3)

"""pyqtgraph backend for :class:`isar.plotspec.spec.PlotSpec` — the GUI-side counterpart of
``isar.plotspec.backends.mpl`` (design ruling G8). The *semantics* live in the spec layer; this
module only resolves the same tokens the mpl backend resolves, into pyqtgraph visuals, so a view
authored as a spec renders with the same meaning in the app as it does in the report.

Token -> visual choices mirror ``backends/mpl.py`` in meaning (not pixels): tier colour overrides
role colour; the certifying tier is visually primary (heavier line / more opaque band); DATA carries
points + error bars; a SANDBOX result is dashed; an UNRELIABLE point is a hollow warning glyph with
NO whiskers; markers are deduplicated in the legend by ``(kind, label)`` exactly as mpl does; the
projection legend is an always-visible, ViewBox-anchored declaration that data autorange cannot
occlude (G9).

Qt/pyqtgraph are imported only inside this module (isar_gui is Qt land); ``isar.plotspec`` stays
pure. Nothing here writes to a workspace.
"""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui

from isar.plotspec.spec import MarkerKind, PlotSpec, Role, State, Tier, entropy_rgb

# Matplotlib default colour cycle (C0..C9) as concrete hex, so tier/role colours mean the same thing
# as they do in the mpl backend. Greys mirror mpl's "0.4"/"0.3"/"0.25" string levels.
_C = {
    "C0": "#1f77b4", "C1": "#ff7f0e", "C2": "#2ca02c", "C3": "#d62728", "C4": "#9467bd",
    "C5": "#8c564b", "C6": "#e377c2", "C7": "#7f7f7f", "C8": "#bcbd22", "C9": "#17becf",
}
_GREY_04 = "#666666"   # mpl "0.4"
_GREY_03 = "#4d4d4d"   # mpl "0.3"
_GREY_025 = "#404040"  # mpl "0.25"

_ROLE_COLOR = {
    Role.DATA: _C["C0"], Role.FIT: _C["C1"], Role.COMPONENT: _C["C3"], Role.RESIDUAL: _GREY_04,
    Role.PRIOR: _C["C4"], Role.BRANCH: _C["C2"], Role.SANDBOX: _C["C5"],
}
_TIER_COLOR = {
    Tier.HESSE: _C["C7"], Tier.PROFILE: _C["C8"], Tier.BOOTSTRAP: _C["C9"], Tier.BAYES: _C["C6"],
}


# ---------------------------------------------------------------------------
# Label derivation — byte-for-byte mirror of backends/mpl.py so the legend entry
# *set* is identical across backends (the parity pin).
# ---------------------------------------------------------------------------
def _series_color(s) -> str:
    if s.tier is not None:
        return _TIER_COLOR.get(s.tier, _C["C0"])
    return _ROLE_COLOR.get(s.role, _C["C0"])


def _band_color(b) -> str:
    if b.tier is not None:
        return _TIER_COLOR.get(b.tier, _ROLE_COLOR.get(b.role, _C["C0"]))
    return _ROLE_COLOR.get(b.role, _C["C0"])


def _series_label(s) -> str | None:
    if s.label is not None:
        return s.label
    if s.tier is not None:
        return f"{s.tier.value}{' (certifying)' if s.certifying else ''}"
    return None


def _band_label(b) -> str | None:
    if b.label is not None:
        return b.label
    if b.tier is not None:
        return f"{b.tier.value} band{' (certifying)' if b.certifying else ''}"
    return None


def _color(spec_color) -> QtGui.QColor:
    return pg.mkColor(spec_color)


# ---------------------------------------------------------------------------
# Rendered-spec handle
# ---------------------------------------------------------------------------
class RenderedSpec:
    """Handle to the pyqtgraph items produced by one :func:`render_into` call.

    ``items`` are every created pg item (data items + overlays). :meth:`remove` detaches them all
    from the plot; re-rendering the same ``PlotItem`` via :func:`render_into` calls this first, so a
    live view can re-render without leaking items.
    """

    def __init__(self, plot_item):
        self._plot_item = plot_item
        self._data_items: list = []   # added via plot_item.addItem -> removeItem
        self._overlays: list = []     # ViewBox-parented widgets -> scene removal
        self.legend = None            # pg.LegendItem or None
        self.projection_label = None  # pg.LabelItem anchored to the ViewBox, or None
        self.legend_names: set[str] = set()

    def _add_data(self, item) -> None:
        self._plot_item.addItem(item)
        self._data_items.append(item)

    def _add_overlay(self, item) -> None:
        self._overlays.append(item)

    @property
    def items(self) -> list:
        overlays = list(self._overlays)
        if self.legend is not None:
            overlays.append(self.legend)
        return self._data_items + overlays

    def remove(self) -> None:
        for it in self._data_items:
            self._plot_item.removeItem(it)
        self._data_items = []
        for it in self._overlays:
            _detach(it)
        self._overlays = []
        if self.legend is not None:
            _detach(self.legend)
            self.legend = None
        self.projection_label = None
        self.legend_names = set()


def _detach(item) -> None:
    """Detach a ViewBox-parented graphics widget (legend / label) from the scene."""
    scene = item.scene()
    if scene is not None:
        scene.removeItem(item)
    else:
        item.setParentItem(None)


# ---------------------------------------------------------------------------
# Element drawers
# ---------------------------------------------------------------------------
def _pen(color, width, dashed=False) -> QtGui.QPen:
    style = QtCore.Qt.PenStyle.DashLine if dashed else QtCore.Qt.PenStyle.SolidLine
    return pg.mkPen(color=color, width=width, style=style)


def _draw_series(rs: RenderedSpec, s, legend_entries: list) -> None:
    color = _series_color(s)
    width = 2.2 if s.certifying else 1.0
    z = 6 if s.certifying else 2
    # A pooled (combine-before-divide) master gets a distinct SQUARE glyph so it stays visually
    # separable from per-scan member points even in the degenerate no-entropy case (mirrors mpl).
    symbol = "s" if s.state is State.POOLED else "o"
    label = _series_label(s)
    x = np.asarray(s.x, dtype=float)
    y = np.asarray(s.y, dtype=float)

    if s.role is Role.SANDBOX:
        # Sandbox results are ephemeral and unmistakably marked (G7): dashed.
        item = pg.PlotDataItem(x, y, pen=_pen(color, width, dashed=True))
        item.setZValue(z)
        rs._add_data(item)
        if label is not None:
            legend_entries.append((item, label))
    elif s.yerr is not None and s.color_values is not None:
        # Per-point honesty colour channel (e.g. assignment entropy): neutral whiskers + a scatter
        # whose brushes are per-point calm->alarm (mirrors the mpl backend).
        yerr = np.asarray(s.yerr, dtype=float)
        rgb = entropy_rgb(s.color_values, s.color_norm)
        brushes = [pg.mkBrush(int(round(r * 255)), int(round(g * 255)), int(round(b * 255)))
                   for r, g, b in rgb]
        err = pg.ErrorBarItem(x=x, y=y, top=yerr, bottom=yerr, pen=_pen(_GREY_04, width), beam=0.0)
        err.setZValue(z)
        scat = pg.ScatterPlotItem(x=x, y=y, symbol=symbol, size=10, brush=brushes,
                                  pen=pg.mkPen(_GREY_03, width=0.5))
        scat.setZValue(z + 1)
        rs._add_data(err)
        rs._add_data(scat)
        if label is not None:
            legend_entries.append((scat, label))
    elif s.yerr is not None:
        yerr = np.asarray(s.yerr, dtype=float)
        line = pg.PlotDataItem(
            x, y, pen=_pen(color, width), symbol=symbol, symbolSize=5,
            symbolBrush=color, symbolPen=color,
        )
        line.setZValue(z)
        err = pg.ErrorBarItem(x=x, y=y, top=yerr, bottom=yerr, pen=_pen(color, width), beam=0.0)
        err.setZValue(z)
        rs._add_data(err)
        rs._add_data(line)
        if label is not None:
            legend_entries.append((line, label))
    else:
        item = pg.PlotDataItem(x, y, pen=_pen(color, width))
        item.setZValue(z)
        rs._add_data(item)
        if label is not None:
            legend_entries.append((item, label))


def _draw_band(rs: RenderedSpec, b, legend_entries: list) -> None:
    color = _band_color(b)
    alpha = 0.35 if b.certifying else 0.15  # certifying tier is opaque; others recede
    x = np.asarray(b.x, dtype=float)
    lo = np.asarray(b.lo, dtype=float)
    hi = np.asarray(b.hi, dtype=float)
    brush = pg.mkBrush(QtGui.QColor(_color(color).red(), _color(color).green(),
                                    _color(color).blue(), int(round(alpha * 255))))
    # FillBetweenItem needs two boundary curves; keep them penless so only the fill shows. The fill
    # item holds references to them, so they are not garbage-collected.
    c_lo = pg.PlotDataItem(x, lo, pen=None)
    c_hi = pg.PlotDataItem(x, hi, pen=None)
    fill = pg.FillBetweenItem(c_lo, c_hi, brush=brush)
    fill.setZValue(-10)  # bands sit under series/markers
    rs._add_data(fill)
    label = _band_label(b)
    if label is not None:
        # Legend swatch: a filled proxy PlotDataItem (not added to the plot) whose ItemSample draws a
        # coloured fill rectangle. Only the name is parity-checked; the swatch is cosmetic.
        proxy = pg.PlotDataItem(pen=_pen(color, 1.0), fillLevel=0.0, fillBrush=brush)
        legend_entries.append((proxy, label))


# Marker kind -> (symbol, pen colour, brush colour|None hollow, size). Mirrors the mpl scatter
# choices: UNRELIABLE/WARNING = hollow red diamond; verdict glyphs get distinct symbols.
def _marker_style(kind: MarkerKind):
    if kind is MarkerKind.MASKED:
        return "x", "red", None, 10
    if kind is MarkerKind.COSMIC_RAY:
        return "o", "darkorange", None, 14
    if kind is MarkerKind.WARNING:
        return "d", "red", None, 12
    if kind is MarkerKind.VERDICT_RESOLVED:
        return "o", "green", "green", 11
    if kind is MarkerKind.VERDICT_FLAGGED:
        return "t", "orange", "orange", 12
    if kind is MarkerKind.VERDICT_HELD:
        return "s", _GREY_03, None, 11
    return "o", _GREY_03, _GREY_03, 9


def _draw_marker(rs: RenderedSpec, m, label, legend_entries: list) -> None:
    symbol, pen_c, brush_c, size = _marker_style(m.kind)
    brush = None if brush_c is None else pg.mkBrush(brush_c)
    scatter = pg.ScatterPlotItem(
        x=[float(m.x)], y=[float(m.y)], symbol=symbol, size=size,
        pen=pg.mkPen(pen_c, width=1.6), brush=brush,
    )
    scatter.setZValue(6)
    rs._add_data(scatter)
    if label is not None:
        legend_entries.append((scatter, label))


def _strip_levels(strips):
    """Shared (vmin, vmax) across every strip's finite values (one colour scale for the whole plane),
    or ``None`` when nothing is finite. Mirrors ``backends/mpl._strip_levels``."""
    vals = []
    for s in strips:
        v = np.asarray(s.values, dtype=float)
        v = v[np.isfinite(v)]
        if v.size:
            vals.append(v)
    if not vals:
        return None
    allv = np.concatenate(vals)
    return float(np.min(allv)), float(np.max(allv))


def _strip_lut():
    """The viridis lookup table shared by every strip ImageItem (same colour meaning as the mpl
    backend's ``cmap="viridis"``)."""
    return pg.colormap.get("viridis").getLookupTable(nPts=256)


def _draw_strip(rs: RenderedSpec, s, levels, lut) -> None:
    """One MEASURED column as a single ``pg.ImageItem`` placed with ``setRect`` over THIS scan's E
    extent only. Each strip is its own item, so the empty space between strips (a gap in the
    sampling) is never covered — gaps stay gaps, nothing is interpolated across them."""
    y = np.asarray(s.y, dtype=float)
    values = np.asarray(s.values, dtype=float)
    if y.size == 0 or values.size == 0:
        return
    x0 = float(s.x_center) - float(s.width) / 2.0
    y0, y1 = float(np.min(y)), float(np.max(y))
    img = pg.ImageItem(values.reshape(1, -1))  # (1 in x, N in y): one column spanning the E extent
    if levels is not None:
        img.setLevels(list(levels))
    img.setLookupTable(lut)
    img.setRect(QtCore.QRectF(x0, y0, float(s.width), y1 - y0))
    img.setZValue(-20)  # measured plane sits under bands/series/markers
    rs._add_data(img)


def _draw_annotation(rs: RenderedSpec, a) -> None:
    if a.xy is not None:
        color = "red" if a.role is State.UNRELIABLE else _GREY_025
        text = pg.TextItem(a.text, color=color, anchor=(0, 1))
        text.setPos(float(a.xy[0]), float(a.xy[1]))
        rs._add_data(text)
    else:
        # Un-anchored notes (exclusion counts, etc.) -> ViewBox bottom-right corner.
        lbl = pg.LabelItem(a.text, color=_GREY_025, size="7pt")
        lbl.setParentItem(rs._plot_item.getViewBox())
        lbl.anchor(itemPos=(1, 1), parentPos=(1, 1), offset=(-8, -8))
        rs._add_overlay(lbl)


def _draw_projection(rs: RenderedSpec, pj) -> None:
    # The always-on 4D-honesty declaration (G9): anchored to the ViewBox top-left corner so data
    # autorange can never occlude it (it is NOT in data coordinates).
    text = f"plane {pj.plane}<br>integrating {pj.integration}"
    lbl = pg.LabelItem(text, color=_GREY_025, size="7pt", justify="left")
    lbl.setParentItem(rs._plot_item.getViewBox())
    lbl.anchor(itemPos=(0, 0), parentPos=(0, 0), offset=(8, 8))
    rs._add_overlay(lbl)
    rs.projection_label = lbl


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def render_into(plot_item: "pg.PlotItem", spec: PlotSpec) -> RenderedSpec:
    """Render ``spec`` into ``plot_item``, returning a :class:`RenderedSpec` handle.

    Any items from a previous :func:`render_into` on this ``plot_item`` are removed first (tracked
    per-PlotItem), so re-rendering for a live update replaces rather than accumulates. Never calls
    ``plot_item.clear()`` — items added by other code are left untouched.
    """
    previous = getattr(plot_item, "_isar_rendered", None)
    if previous is not None:
        previous.remove()

    rs = RenderedSpec(plot_item)
    legend_entries: list = []

    if spec.strips:  # measured columns underneath everything (the plane background)
        levels = _strip_levels(spec.strips)
        lut = _strip_lut()
        for st in spec.strips:
            _draw_strip(rs, st, levels, lut)

    for b in spec.bands:
        _draw_band(rs, b, legend_entries)
    for s in spec.series:
        _draw_series(rs, s, legend_entries)

    seen_marker_keys: set = set()  # (kind, label) already legended — one entry per group, like mpl
    for m in spec.markers:
        key = (m.kind, m.label)
        label = m.label if (m.label is not None and key not in seen_marker_keys) else None
        seen_marker_keys.add(key)
        _draw_marker(rs, m, label, legend_entries)

    for a in spec.annotations:
        _draw_annotation(rs, a)
    if spec.projection is not None:
        _draw_projection(rs, spec.projection)

    if legend_entries:
        legend = pg.LegendItem(offset=None)
        legend.setParentItem(plot_item.getViewBox())
        legend.anchor(itemPos=(1, 0), parentPos=(1, 0), offset=(-8, 8))
        for sample, name in legend_entries:
            legend.addItem(sample, name)
            rs.legend_names.add(name)
        rs.legend = legend

    plot_item.setLabel("bottom", spec.xlabel)
    plot_item.setLabel("left", spec.ylabel)
    plot_item.setTitle(spec.title)

    plot_item._isar_rendered = rs
    return rs

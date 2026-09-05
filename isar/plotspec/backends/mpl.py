"""Matplotlib (Agg) backend: resolve the semantic tokens in a :class:`~isar.plotspec.spec.PlotSpec`
into pixels and return PNG bytes. The token -> style mapping lives *here* (the spec stays pure); the
pyqtgraph backend (later) will resolve the same tokens to the same meanings.

Mirrors the report idiom (`matplotlib.use("Agg")`, `_png_bytes`); the tiny helper is replicated
rather than imported so plotspec does not depend on `isar.report`."""
from __future__ import annotations

import io

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402

import numpy as np  # noqa: E402

from isar.plotspec.spec import (  # noqa: E402
    Glyph,
    MarkerKind,
    PlotSpec,
    Role,
    State,
    Tier,
    entropy_rgb,
)

# Role -> line colour. DATA sits on matplotlib's C0; fit/components follow the report palette.
_ROLE_COLOR = {
    Role.DATA: "C0", Role.FIT: "C1", Role.COMPONENT: "C3", Role.RESIDUAL: "0.4",
    Role.PRIOR: "0.55", Role.BRANCH: "0.35", Role.SANDBOX: "C5",
}
# Interval-ladder tiers get distinct, stable colours so a side-by-side ladder is legible.
_TIER_COLOR = {
    Tier.HESSE: "C7", Tier.PROFILE: "C8", Tier.BOOTSTRAP: "C9", Tier.BAYES: "C6",
}


def _png_bytes(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
    return buf.getvalue()


def _series_color(s) -> str:
    styles = set((s.style or "").split())
    if "muted" in styles:
        return "0.6"
    if s.state is State.POOLED:
        if "verdict_resolved" in styles:
            return "green"
        if "verdict_flagged" in styles:
            return "darkorange"
        if "verdict_held" in styles:
            return "0.35"
    if s.tier is not None:
        return _TIER_COLOR.get(s.tier, "C0")
    return _ROLE_COLOR.get(s.role, "C0")


_GLYPH_STYLE = {
    Glyph.INDIVIDUAL_VALID: ("o", 32, "green", "green", 1.0, 6),
    Glyph.INDIVIDUAL_UNCERTIFIED: ("o", 30, "0.55", "none", 1.2, 6),
    Glyph.INDIVIDUAL_UNRELIABLE: ("D", 42, "red", "none", 1.4, 6),
    Glyph.MASTER_TARGET: ("o", 82, "green", "none", 1.8, 9),
    Glyph.MASTER_AMBIGUOUS: ("D", 88, "darkorange", "none", 1.8, 9),
    Glyph.MASTER_MISMATCH: ("D", 88, "red", "none", 1.8, 9),
    Glyph.MASTER_CERTIFIED: ("s", 92, "C0", "C0", 2.4, 10),
    Glyph.MASTER_UNASSESSED: ("s", 86, "0.5", "none", 1.8, 9),
    Glyph.UNSELECTED_CANDIDATE: ("o", 28, "0.6", "none", 1.0, 5),
}


def _errors(s):
    if s.errors is None:
        if s.yerr is None:
            return None, None
        return None, np.asarray(s.yerr, dtype=float)
    e = s.errors
    xerr = None
    yerr = None
    if e.left is not None or e.right is not None:
        left = np.zeros_like(s.x, dtype=float) if e.left is None else np.asarray(e.left, dtype=float)
        right = np.zeros_like(s.x, dtype=float) if e.right is None else np.asarray(e.right, dtype=float)
        xerr = np.vstack((left, right))
    if e.bottom is not None or e.top is not None:
        bottom = np.zeros_like(s.y, dtype=float) if e.bottom is None else np.asarray(e.bottom, dtype=float)
        top = np.zeros_like(s.y, dtype=float) if e.top is None else np.asarray(e.top, dtype=float)
        yerr = np.vstack((bottom, top))
    return xerr, yerr


def _draw_series(ax, s) -> None:
    color = _series_color(s)
    styles = set((s.style or "").split())
    # Certifying tier is visually primary (heavier, on top); non-certifying tiers recede.
    lw = 2.2 if s.certifying else 1.0
    z = 6 if s.certifying else 2
    # A pooled (combine-before-divide) master gets a distinct SQUARE glyph so it stays visually
    # separable from per-scan member points even in the degenerate no-entropy case (both backends).
    marker = "s" if s.state is State.POOLED else "o"
    if s.state is State.POOLED:
        lw = max(lw, 1.8)
        z = 9
    elif "muted" in styles:
        z = 1
    dashed = bool(styles & {"rival", "provisional", "dashed"})
    hollow = "provisional" in styles
    label = s.label
    if s.tier is not None and label is None:
        label = f"{s.tier.value}{' (certifying)' if s.certifying else ''}"
    xerr, yerr = _errors(s)
    has_errors = xerr is not None or yerr is not None
    if s.glyph is not None:
        marker, size, edge, face, glw, gz = _GLYPH_STYLE[s.glyph]
        if has_errors:
            ax.errorbar(s.x, s.y, xerr=xerr, yerr=yerr, fmt="none", ecolor=edge,
                        lw=max(lw, 1.0), capsize=2, zorder=gz)
        if s.color_values is not None:
            rgb = entropy_rgb(s.color_values, s.color_norm)
            edgecolors = rgb
            facecolors = rgb if face != "none" else "none"
        else:
            edgecolors, facecolors = edge, face
        ax.scatter(s.x, s.y, s=size, marker=marker, edgecolors=edgecolors,
                   facecolors=facecolors, linewidths=glw, zorder=gz + 1, label=label)
    elif s.role is Role.SANDBOX or (not has_errors and dashed):
        # Sandbox results are ephemeral and unmistakably marked (G7): dashed.
        ax.plot(s.x, s.y, ls="--", lw=lw, color=color, zorder=z, label=label)
    elif has_errors and s.color_values is not None:
        # Per-point honesty colour channel (e.g. assignment entropy): neutral whiskers, and each
        # point coloured calm->alarm by its own value (the legend note explains the encoding).
        rgb = entropy_rgb(s.color_values, s.color_norm)
        ax.errorbar(s.x, s.y, xerr=xerr, yerr=yerr, fmt="none",
                    ecolor="0.5", lw=lw, zorder=z)
        edge = color if s.state is State.POOLED else "0.2"
        size = 72 if s.state is State.POOLED else (24 if "muted" in styles else 36)
        kwargs = ({"facecolors": "none"} if hollow else {"c": rgb})
        ax.scatter(s.x, s.y, s=size, marker=marker, edgecolors=edge, linewidths=1.2,
                   zorder=z + 1, label=label, **kwargs)
    elif has_errors:
        size = 7 if s.state is State.POOLED else (3 if "muted" in styles else 4)
        ax.errorbar(s.x, s.y, xerr=xerr, yerr=yerr, fmt=marker, ms=size, lw=lw, color=color,
                    markerfacecolor=("none" if hollow else color), linestyle="none",
                    zorder=z, label=label)
    else:
        if "muted" in styles:
            ax.scatter(s.x, s.y, marker=marker, s=18, facecolors="none", edgecolors=color,
                       linewidths=0.8, zorder=z, label=label)
        else:
            ax.plot(s.x, s.y, lw=lw, color=color, zorder=z, label=label)


def _draw_band(ax, b) -> None:
    color = _TIER_COLOR.get(b.tier, _ROLE_COLOR.get(b.role, "C0")) if b.tier else \
        _ROLE_COLOR.get(b.role, "C0")
    alpha = 0.35 if b.certifying else 0.15
    label = b.label
    if b.tier is not None and label is None:
        label = f"{b.tier.value} band{' (certifying)' if b.certifying else ''}"
    ax.fill_between(b.x, b.lo, b.hi, color=color, alpha=alpha, zorder=1, label=label)


# Marker kind -> matplotlib scatter style. UNRELIABLE points render as a hollow warning glyph with
# NO whiskers (the spec layer already forbids whiskers on them). ``label`` is forwarded to the
# scatter for the legend; the caller deduplicates so only the first marker of each (kind, label)
# carries a label (the builder emits one Marker per point).
def _draw_marker(ax, m, label=None) -> None:
    if m.kind is MarkerKind.MASKED:
        ax.scatter([m.x], [m.y], marker="o", s=45, facecolors="none", edgecolors="0.5",
                   linewidths=1.4, zorder=5, label=label)
    elif m.kind is MarkerKind.COSMIC_RAY:
        ax.scatter([m.x], [m.y], s=90, facecolors="none", edgecolors="darkorange",
                   linewidths=1.5, zorder=4, label=label)
    elif m.kind is MarkerKind.WARNING:
        ax.scatter([m.x], [m.y], marker="D", s=70, facecolors="none", edgecolors="red",
                   linewidths=1.6, zorder=6, label=label)
    elif m.kind is MarkerKind.POOLED_WARNING:
        ax.scatter([m.x], [m.y], marker="s", s=85, facecolors="none", edgecolors="red",
                   linewidths=1.8, zorder=9, label=label)
    elif m.kind is MarkerKind.POOLED_UNCERTIFIED:
        ax.scatter([m.x], [m.y], marker="s", s=85, facecolors="none", edgecolors="0.35",
                   linewidths=1.8, zorder=9, label=label)
    elif m.kind is MarkerKind.AMBIGUOUS_CANDIDATE:
        ax.scatter([m.x], [m.y], marker="o", s=55, facecolors="none", edgecolors="0.45",
                   linewidths=1.4, zorder=7, label=label)
    elif m.kind is MarkerKind.VERDICT_RESOLVED:
        ax.scatter([m.x], [m.y], marker="o", s=60, color="green", zorder=6, label=label)
    elif m.kind is MarkerKind.VERDICT_FLAGGED:
        ax.scatter([m.x], [m.y], marker="^", s=70, color="orange", zorder=6, label=label)
    elif m.kind is MarkerKind.VERDICT_HELD:
        ax.scatter([m.x], [m.y], marker="s", s=60, facecolors="none", edgecolors="0.3", zorder=6,
                   label=label)
    else:
        ax.scatter([m.x], [m.y], marker="o", s=40, color="0.3", zorder=6, label=label)


def _draw_annotation(ax, a) -> None:
    if a.xy is not None:
        color = "red" if a.role is State.UNRELIABLE else "0.2"
        ax.annotate(a.text, xy=a.xy, fontsize=7, color=color,
                    xytext=(4, 4), textcoords="offset points")
    else:
        # Un-anchored notes (e.g. exclusion counts) go in a corner text box.
        ax.text(0.99, 0.02, a.text, transform=ax.transAxes, ha="right", va="bottom",
                fontsize=7, color="0.2")


def _strip_levels(strips):
    """Shared (vmin, vmax) across every strip's finite values, so all columns share one colour scale
    (a strip's brightness is comparable across the plane). ``None`` when no finite value exists."""
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


def _draw_strip(ax, s, levels, colormap) -> None:
    """One MEASURED column as a single imshow rectangle spanning THIS scan's E extent only. Each
    strip is its own artist, so the empty space between strips (a gap in the sampling) stays empty —
    no artist bridges the gap and nothing is interpolated across it."""
    y = np.asarray(s.y, dtype=float)
    values = np.asarray(s.values, dtype=float)
    if y.size == 0 or values.size == 0:
        return
    x0 = float(s.x_center) - float(s.width) / 2.0
    x1 = float(s.x_center) + float(s.width) / 2.0
    y0, y1 = float(np.min(y)), float(np.max(y))
    vmin, vmax = (levels if levels is not None else (None, None))
    ax.imshow(values.reshape(-1, 1), extent=(x0, x1, y0, y1), origin="lower", aspect="auto",
              interpolation="none", cmap=colormap, vmin=vmin, vmax=vmax, zorder=0)


def _draw_region(ax, region) -> None:
    """A double outline remains visible over both dark and light strip pixels."""
    xy = (region.x_min, region.y_min)
    width = region.x_max - region.x_min
    height = region.y_max - region.y_min
    ax.add_patch(Rectangle(xy, width, height, fill=False, edgecolor="white",
                           linewidth=4.0, zorder=1.2))
    ax.add_patch(Rectangle(xy, width, height, fill=False, edgecolor="black",
                           linewidth=1.5, zorder=1.3, label=region.label))


def _draw_projection(ax, pj) -> None:
    # The always-on 4D-honesty declaration, as a persistent text box (G9).
    ax.text(0.01, 0.99, f"plane {pj.plane}\nintegrating {pj.integration}", transform=ax.transAxes,
            ha="left", va="top", fontsize=6.5, color="0.25",
            bbox=dict(boxstyle="round", fc="white", ec="0.7", alpha=0.85))


def _render_onto(ax, spec: PlotSpec) -> None:
    """Draw ``spec`` onto an existing Axes (the render core; :func:`render_png` wraps it)."""
    if spec.strips:  # measured columns sit underneath everything (the plane background)
        levels = (tuple(float(v) for v in spec.strip_levels) if spec.strip_levels is not None
                  else _strip_levels(spec.strips))
        for st in spec.strips:
            _draw_strip(ax, st, levels, spec.strip_colormap)
    for region in spec.regions:
        _draw_region(ax, region)
    for b in spec.bands:
        _draw_band(ax, b)
    for s in spec.series:
        _draw_series(ax, s)
    seen_marker_keys = set()  # (kind, label) already in the legend — dedup one entry per group
    for m in spec.markers:
        key = (m.kind, m.label)
        label = m.label if (m.label is not None and key not in seen_marker_keys) else None
        seen_marker_keys.add(key)
        _draw_marker(ax, m, label=label)
    for a in spec.annotations:
        _draw_annotation(ax, a)
    if spec.projection is not None:
        _draw_projection(ax, spec.projection)
    ax.set_xlabel(spec.xlabel)
    ax.set_ylabel(spec.ylabel)
    ax.set_title(spec.title, fontsize=9)
    _, labels = ax.get_legend_handles_labels()
    if labels:
        ax.legend(fontsize=7, framealpha=0.9)


def render_png(spec: PlotSpec) -> bytes:
    """Render ``spec`` to PNG bytes (Agg).

    Owns the figure lifecycle: whatever raises during rendering, ``tight_layout``, or the PNG
    encode, the figure is closed in the ``finally`` so a failed render never leaks a live figure.
    """
    fig, ax = plt.subplots(figsize=(6.0, 3.4))
    try:
        _render_onto(ax, spec)
        fig.tight_layout()
        return _png_bytes(fig)
    finally:
        plt.close(fig)

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

import numpy as np  # noqa: E402

from isar.plotspec.spec import (  # noqa: E402
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
    Role.PRIOR: "C4", Role.BRANCH: "C2", Role.SANDBOX: "C5",
}
# Interval-ladder tiers get distinct, stable colours so a side-by-side ladder is legible.
_TIER_COLOR = {
    Tier.HESSE: "C7", Tier.PROFILE: "C8", Tier.BOOTSTRAP: "C9", Tier.BAYES: "C6",
}


def _png_bytes(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def _series_color(s) -> str:
    if s.tier is not None:
        return _TIER_COLOR.get(s.tier, "C0")
    return _ROLE_COLOR.get(s.role, "C0")


def _draw_series(ax, s) -> None:
    color = _series_color(s)
    # Certifying tier is visually primary (heavier, on top); non-certifying tiers recede.
    lw = 2.2 if s.certifying else 1.0
    z = 6 if s.certifying else 2
    # A pooled (combine-before-divide) master gets a distinct SQUARE glyph so it stays visually
    # separable from per-scan member points even in the degenerate no-entropy case (both backends).
    marker = "s" if s.state is State.POOLED else "o"
    label = s.label
    if s.tier is not None and label is None:
        label = f"{s.tier.value}{' (certifying)' if s.certifying else ''}"
    if s.role is Role.SANDBOX:
        # Sandbox results are ephemeral and unmistakably marked (G7): dashed.
        ax.plot(s.x, s.y, ls="--", lw=lw, color=color, zorder=z, label=label)
    elif s.yerr is not None and s.color_values is not None:
        # Per-point honesty colour channel (e.g. assignment entropy): neutral whiskers, and each
        # point coloured calm->alarm by its own value (the legend note explains the encoding).
        rgb = entropy_rgb(s.color_values, s.color_norm)
        ax.errorbar(s.x, s.y, yerr=s.yerr, fmt="none", ecolor="0.5", lw=lw, zorder=z)
        ax.scatter(s.x, s.y, c=rgb, s=36, marker=marker, edgecolors="0.2", linewidths=0.5,
                   zorder=z + 1, label=label)
    elif s.yerr is not None:
        ax.errorbar(s.x, s.y, yerr=s.yerr, fmt=f"{marker}-", ms=3, lw=lw, color=color,
                    zorder=z, label=label)
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
        ax.scatter([m.x], [m.y], marker="x", color="red", zorder=5, label=label)
    elif m.kind is MarkerKind.COSMIC_RAY:
        ax.scatter([m.x], [m.y], s=90, facecolors="none", edgecolors="darkorange",
                   linewidths=1.5, zorder=4, label=label)
    elif m.kind is MarkerKind.WARNING:
        ax.scatter([m.x], [m.y], marker="D", s=70, facecolors="none", edgecolors="red",
                   linewidths=1.6, zorder=6, label=label)
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


def _draw_strip(ax, s, levels) -> None:
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
              interpolation="none", cmap="viridis", vmin=vmin, vmax=vmax, zorder=0)


def _draw_projection(ax, pj) -> None:
    # The always-on 4D-honesty declaration, as a persistent text box (G9).
    ax.text(0.01, 0.99, f"plane {pj.plane}\nintegrating {pj.integration}", transform=ax.transAxes,
            ha="left", va="top", fontsize=6.5, color="0.25",
            bbox=dict(boxstyle="round", fc="white", ec="0.7", alpha=0.85))


def _render_onto(ax, spec: PlotSpec) -> None:
    """Draw ``spec`` onto an existing Axes (the render core; :func:`render_png` wraps it)."""
    if spec.strips:  # measured columns sit underneath everything (the plane background)
        levels = _strip_levels(spec.strips)
        for st in spec.strips:
            _draw_strip(ax, st, levels)
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
    """Render ``spec`` to PNG bytes (Agg)."""
    fig, ax = plt.subplots(figsize=(6.0, 3.4))
    _render_onto(ax, spec)
    fig.tight_layout()
    return _png_bytes(fig)

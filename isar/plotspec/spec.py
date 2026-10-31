"""Pure plot specifications: declarative descriptions of a figure (series, bands, markers,
annotations) plus the **semantic style tokens** that both backends resolve into pixels (G8). The
semantics live *here*, once, so the matplotlib (reports) and pyqtgraph (GUI, later) backends can
never disagree about what an unreliable interval or a pooled master looks like.

Stdlib + numpy only — no matplotlib, no Qt, no isar.session/drive/analyze. A spec is a value: its
``to_dict()`` is JSON-serializable (numpy arrays serialize as lists, tokens as their string value)
and is the contract golden-snapshot tests pin.

Binding rendering semantics (inherited from docs/ARCHITECTURE.md §12 via design/gui.md §7.3), enforced here so no
backend can violate them:

- An **unreliable** interval (``interval_reliable=False``) is NEVER a numeric range: it may not be
  a :class:`Band`, and a :class:`Series`/`Band` tagged :data:`State.UNRELIABLE` may carry no
  whiskers (``yerr``). Constructing such an object raises ``ValueError``. The honest rendering is a
  warning :class:`Marker` + an ``"(unreliable)"`` :class:`Annotation` — see :func:`mark_unreliable`.
- **Pooled** masters carry :data:`State.POOLED` and enumerate their members in ``PlotSpec.meta``.
- **Interval-ladder** comparison: tiers rendered side by side are labeled by their :class:`Tier`
  token, and **exactly one** may be ``certifying=True`` (0 or 1 allowed; more is rejected).
- A :class:`Strip` is **measured** data — one column of intensity at a plane position (the growing
  ``(q, E)`` plane, design/gui.md item 2, G9 4D-honesty). Renderers MUST NOT interpolate *between*
  strips: each strip is drawn as its own rectangle over its own measured ``E`` extent, and the empty
  space between strips (a gap in the plane sampling) stays empty — a gap is shown as a gap, never
  filled. Interpolation *within* a strip's own measured points is a rendering choice (conservative:
  nearest, no smoothing across the gap).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import numpy as np


# Scan(s) identity and component palettes, shared by both renderers.
DATA_NEUTRAL = "#404040"
GUIDE_COLOR = "#999999"
TRACE_COLORS = ("#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
                "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf")
SYMBOLS = ("o", "s", "t", "d", "p", "h", "star", "t1", "t2", "t3")
CURVE_COLORS = {"total": "#222222", "elastic": "#7f7f7f",
                "quasielastic": "#8c564b", "background": "#999999",
                **{f"mode{i + 1}": color for i, color in enumerate(TRACE_COLORS[:6])}}


# ---------------------------------------------------------------------------
# Semantic tokens (str-valued so they serialize as themselves)
# ---------------------------------------------------------------------------
class Role(str, Enum):
    """What a series/band *is* — drives colour and draw order, never the physics."""
    DATA = "data"
    FIT = "fit"
    COMPONENT = "component"
    RESIDUAL = "residual"
    PRIOR = "prior"
    BRANCH = "branch"
    SANDBOX = "sandbox"


class Tier(str, Enum):
    """The interval-ladder tier a band/series came from (labeled side by side; one certifies)."""
    HESSE = "hesse"
    PROFILE = "profile"
    BOOTSTRAP = "bootstrap"
    BAYES = "bayes"


class State(str, Enum):
    """Cross-cutting condition of a point/series (honesty channel), orthogonal to :class:`Role`."""
    UNRELIABLE = "unreliable"          # interval_reliable=False — never a range, never whiskers
    POOLED = "pooled"                  # a combine-before-divide master; members in meta
    ENTROPY = "entropy"                # continuous colouring by assignment entropy
    VERDICT_RESOLVED = "verdict_resolved"
    VERDICT_FLAGGED = "verdict_flagged"
    VERDICT_HELD = "verdict_held"


class MarkerKind(str, Enum):
    """Single-glyph markers (verdict glyphs, warnings, shown-not-hidden points)."""
    WARNING = "warning"                # hollow warning glyph (e.g. unreliable point)
    POOLED_UNCERTIFIED = "pooled_uncertified"  # neutral hollow square: no certifying interval
    POOLED_WARNING = "pooled_warning"  # red hollow square: explicitly unreliable pooled result
    AMBIGUOUS_CANDIDATE = "ambiguous_candidate"  # open mode candidate; never target delivery
    MASKED = "masked"                  # masked point (QC or manual), shown not deleted (grey x)
    COSMIC_RAY = "cosmic_ray"          # cosmic-ray-cleaned point, ringed
    VERDICT_RESOLVED = "verdict_resolved"
    VERDICT_FLAGGED = "verdict_flagged"
    VERDICT_HELD = "verdict_held"


class Glyph(str, Enum):
    """One composed fit-display glyph. Renderers map these tokens; builders own classification."""
    INDIVIDUAL_VALID = "individual_valid"
    INDIVIDUAL_UNCERTIFIED = "individual_uncertified"
    INDIVIDUAL_UNRELIABLE = "individual_unreliable"
    MASTER_TARGET = "master_target"
    MASTER_AMBIGUOUS = "master_ambiguous"
    MASTER_MISMATCH = "master_mismatch"
    MASTER_CERTIFIED = "master_certified"
    MASTER_UNASSESSED = "master_unassessed"
    UNSELECTED_CANDIDATE = "unselected_candidate"


class RegionKind(str, Enum):
    """Semantic plot-region overlays."""
    LATEST_ACQUISITION = "latest_acquisition"


def _arr(a) -> list | None:
    """numpy array (or sequence) -> JSON list; ``None`` passes through."""
    if a is None:
        return None
    return np.asarray(a).tolist()


def _tok(t) -> str | None:
    """Token enum -> its string value; ``None`` passes through."""
    return None if t is None else str(t.value if isinstance(t, Enum) else t)


# The assignment-entropy colour ramp (State.ENTROPY): 0 -> calm, the reference (ln 2, "undecided")
# -> alarm. Defined once here so both backends colour identically (semantics live in the spec).
LN2 = float(np.log(2.0))
_CALM_RGB = (0.13, 0.47, 0.71)     # muted blue
_ALARM_RGB = (0.84, 0.15, 0.16)    # red


def entropy_rgb(values, norm: float | None = None) -> np.ndarray:
    """Map per-point ``values`` (e.g. assignment entropy) to ``(N, 3)`` RGB in ``[0, 1]``, linearly
    from calm at 0 to alarm at ``norm`` (default :data:`LN2`, the undecided reference). Non-finite /
    ``None`` entries read as calm (0). Clipped outside ``[0, norm]``."""
    v = np.asarray([np.nan if x is None else float(x) for x in values], dtype=float)
    n = float(norm) if norm else LN2
    t = np.clip(np.where(np.isfinite(v), v, 0.0) / (n if n > 0 else 1.0), 0.0, 1.0)
    calm = np.asarray(_CALM_RGB, dtype=float)
    alarm = np.asarray(_ALARM_RGB, dtype=float)
    return calm[None, :] * (1.0 - t[:, None]) + alarm[None, :] * t[:, None]


# ---------------------------------------------------------------------------
# Spec primitives
# ---------------------------------------------------------------------------
@dataclass
class ErrorBars:
    """Directional per-point errors. Missing directions are not drawn."""
    left: np.ndarray | None = None
    right: np.ndarray | None = None
    bottom: np.ndarray | None = None
    top: np.ndarray | None = None

    def __post_init__(self):
        present = [np.asarray(v, dtype=float) for v in
                   (self.left, self.right, self.bottom, self.top) if v is not None]
        if present and any(v.shape != present[0].shape for v in present[1:]):
            raise ValueError("all ErrorBars directions must have the same shape")
        if any(np.any(v < 0.0) for v in present):
            raise ValueError("ErrorBars spans must be non-negative")

    def validate_length(self, shape) -> None:
        for name in ("left", "right", "bottom", "top"):
            value = getattr(self, name)
            if value is not None and np.asarray(value).shape != shape:
                raise ValueError(f"ErrorBars.{name} and Series points must have the same shape")

    def to_dict(self) -> dict:
        return {name: _arr(getattr(self, name))
                for name in ("left", "right", "bottom", "top")}


@dataclass
class Series:
    """Line/scatter data. ``tier``/``certifying`` place it on the interval ladder; ``state`` carries
    the honesty channel (an :data:`State.UNRELIABLE` series may not have whiskers)."""
    x: np.ndarray
    y: np.ndarray
    yerr: np.ndarray | None = None
    errors: ErrorBars | None = None
    label: str | None = None
    role: Role = Role.DATA
    style: str | None = None
    tier: Tier | None = None
    certifying: bool = False
    state: State | None = None
    # Optional per-point colour channel (honesty overlay, e.g. State.ENTROPY): ``color_values`` is one
    # scalar per point, mapped calm->alarm through :func:`entropy_rgb` at reference ``color_norm``
    # (default ln 2). Both backends colour each marker by it; when None the series takes its role/tier
    # colour. Only a scatter (``yerr`` present, DATA-like) is per-point coloured.
    color_values: np.ndarray | None = None
    color_norm: float | None = None
    glyph: Glyph | None = None
    color: str | None = None
    symbol: str | None = None
    legend_group: str | None = None
    legend_bold: bool = False
    tooltip: str | None = None

    def __post_init__(self):
        if self.symbol is not None and self.symbol not in SYMBOLS:
            raise ValueError(f"unknown series symbol: {self.symbol}")
        if self.yerr is not None and self.errors is not None:
            raise ValueError("Series.yerr and Series.errors are mutually exclusive")
        if self.errors is not None:
            self.errors.validate_length(np.asarray(self.x).shape)
        if self.state is State.UNRELIABLE and (self.yerr is not None or self.errors is not None):
            raise ValueError("an UNRELIABLE series may not carry whiskers; "
                             "use mark_unreliable() instead")

    def to_dict(self) -> dict:
        result = {"x": _arr(self.x), "y": _arr(self.y), "yerr": _arr(self.yerr),
                "errors": None if self.errors is None else self.errors.to_dict(),
                "label": self.label, "role": _tok(self.role), "style": self.style,
                "tier": _tok(self.tier), "certifying": bool(self.certifying),
                "state": _tok(self.state), "color_values": _arr(self.color_values),
                "color_norm": None if self.color_norm is None else float(self.color_norm),
                "glyph": _tok(self.glyph)}
        for name in ("color", "symbol", "legend_group"):
            if getattr(self, name) is not None:
                result[name] = getattr(self, name)
        if self.legend_bold:
            result["legend_bold"] = True
        return result


@dataclass
class GuideLine:
    """An axis-aligned reference spanning the viewport, excluded from legends.

    ``include_in_bounds`` includes only its fixed coordinate in autoscaling, never its extent.

    ``axis="x"`` fixes x (vertical); ``axis="y"`` fixes y (horizontal).
    """
    axis: str
    value: float
    color: str = GUIDE_COLOR
    style: str = "dotted"
    include_in_bounds: bool = False

    def __post_init__(self):
        if self.axis not in {"x", "y"}:
            raise ValueError("guide axis must be x or y")

    def to_dict(self) -> dict:
        result = {"axis": self.axis, "value": float(self.value), "color": self.color, "style": self.style}
        if self.include_in_bounds:
            result["include_in_bounds"] = True
        return result


@dataclass
class Band:
    """Filled interval band [lo, hi] over x. Never used for an unreliable interval."""
    x: np.ndarray
    lo: np.ndarray
    hi: np.ndarray
    label: str | None = None
    role: Role = Role.FIT
    tier: Tier | None = None
    certifying: bool = False
    state: State | None = None

    def __post_init__(self):
        if self.state is State.UNRELIABLE:
            raise ValueError("an unreliable interval may not be a Band; render it as an "
                             "'(unreliable)' annotation + warning marker (mark_unreliable())")

    def to_dict(self) -> dict:
        return {"x": _arr(self.x), "lo": _arr(self.lo), "hi": _arr(self.hi),
                "label": self.label, "role": _tok(self.role), "tier": _tok(self.tier),
                "certifying": bool(self.certifying), "state": _tok(self.state)}


@dataclass
class Marker:
    """A single glyph at (x, y): verdict glyphs, warning markers, shown-not-hidden points."""
    x: float
    y: float
    kind: MarkerKind
    label: str | None = None

    def to_dict(self) -> dict:
        return {"x": float(self.x), "y": float(self.y), "kind": _tok(self.kind),
                "label": self.label}


@dataclass
class Annotation:
    """Free text (e.g. ``"(unreliable)"``, exclusion counts), optionally anchored at ``xy``."""
    text: str
    xy: tuple | None = None
    role: State | None = None
    anchor: tuple | None = None
    tooltip: str | None = None
    bold: bool = False

    def to_dict(self) -> dict:
        result = {"text": self.text, "xy": (None if self.xy is None else [float(v) for v in self.xy]),
                "role": _tok(self.role)}
        if self.anchor is not None:
            result["anchor"] = list(self.anchor)
        if self.tooltip is not None:
            result["tooltip"] = self.tooltip
        if self.bold:
            result["bold"] = True
        return result


@dataclass
class Strip:
    """One MEASURED column of intensity at a plane position ``x_center`` (the growing ``(q, E)``
    plane): ``values`` sampled at ``y`` (the scan's own energy axis), drawn as a rectangle of width
    ``width`` centred on ``x_center`` spanning that scan's measured ``E`` range only. ``meta`` carries
    provenance (e.g. ``scan_id``). Renderers never interpolate between strips — gaps stay gaps."""
    x_center: float
    width: float
    y: np.ndarray
    values: np.ndarray
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"x_center": float(self.x_center), "width": float(self.width),
                "y": _arr(self.y), "values": _arr(self.values), "meta": dict(self.meta)}


@dataclass
class RegionBox:
    """Unfilled semantic rectangle drawn above strips and below fit points."""
    x_min: float
    x_max: float
    y_min: float
    y_max: float
    kind: RegionKind
    label: str | None = None

    def __post_init__(self):
        if self.x_max < self.x_min or self.y_max < self.y_min:
            raise ValueError("RegionBox maxima must not be below minima")

    def to_dict(self) -> dict:
        return {
            "x_min": float(self.x_min), "x_max": float(self.x_max),
            "y_min": float(self.y_min), "y_max": float(self.y_max),
            "kind": _tok(self.kind), "label": self.label,
        }


@dataclass
class ProjectionLegend:
    """The always-on 4D-honesty declaration (G9): which plane is shown and over what window it is
    integrated, e.g. plane=``"(H, 1, 0) x E"``, integration=``"|L| < 0.02, |K-1| < 0.02"``."""
    plane: str
    integration: str

    def to_dict(self) -> dict:
        return {"plane": self.plane, "integration": self.integration}


@dataclass
class PlotSpec:
    """A whole figure. ``meta`` is a free JSON-serializable dict (pooled masters enumerate their
    members here). Interval-ladder invariant enforced on construction: at most one certifying tier."""
    title: str
    xlabel: str
    ylabel: str
    series: list[Series] = field(default_factory=list)
    bands: list[Band] = field(default_factory=list)
    markers: list[Marker] = field(default_factory=list)
    annotations: list[Annotation] = field(default_factory=list)
    strips: list[Strip] = field(default_factory=list)
    regions: list[RegionBox] = field(default_factory=list)
    projection: ProjectionLegend | None = None
    value_label: str | None = None   # colorbar meaning for strips, e.g. "counts/monitor"
    strip_colormap: str = "viridis"
    #: The colour scale ``(vmin, vmax)`` every strip shares. ``None`` means the renderers span the
    #: finite min..max of all strip values. The spec owns it (not the renderer) so both backends
    #: and the colourbar agree, and so the producer can set it from the SIGNAL it wants visible: a
    #: plane whose one Bragg-elastic cell is 2000x every phonon cell paints the phonons black on a
    #: min..max scale. Cells above ``vmax`` saturate; they are never hidden.
    strip_levels: tuple[float, float] | None = None
    meta: dict = field(default_factory=dict)
    guides: list[GuideLine] = field(default_factory=list)

    def __post_init__(self):
        self._check_certifying()

    def _check_certifying(self):
        n = sum(1 for o in (*self.series, *self.bands) if getattr(o, "certifying", False))
        if n > 1:
            raise ValueError(f"exactly one interval-ladder tier may be certifying; found {n}")

    def to_dict(self) -> dict:
        result = {
            "title": self.title, "xlabel": self.xlabel, "ylabel": self.ylabel,
            "series": [s.to_dict() for s in self.series],
            "bands": [b.to_dict() for b in self.bands],
            "markers": [m.to_dict() for m in self.markers],
            "annotations": [a.to_dict() for a in self.annotations],
            "strips": [s.to_dict() for s in self.strips],
            "regions": [r.to_dict() for r in self.regions],
            "projection": None if self.projection is None else self.projection.to_dict(),
            "value_label": self.value_label,
            "strip_colormap": self.strip_colormap,
            "strip_levels": (None if self.strip_levels is None
                             else [float(self.strip_levels[0]), float(self.strip_levels[1])]),
            "meta": self.meta,
        }
        if self.guides:
            result["guides"] = [g.to_dict() for g in self.guides]
        return result


# ---------------------------------------------------------------------------
# Helper for the binding unreliable-interval semantics
# ---------------------------------------------------------------------------
def mark_unreliable(spec: PlotSpec, x: float, y: float, text: str = "(unreliable)") -> PlotSpec:
    """Represent an unreliable point the only sanctioned way: a warning :class:`Marker` plus an
    ``"(unreliable)"`` :class:`Annotation` — never a band, never whiskers. Mutates and returns
    ``spec``."""
    spec.markers.append(Marker(x=x, y=y, kind=MarkerKind.WARNING, label=text))
    spec.annotations.append(Annotation(text=text, xy=(x, y), role=State.UNRELIABLE))
    return spec

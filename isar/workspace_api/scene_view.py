"""Reading a STORED scene revision back out of a workspace (SCENE-3 PR 3c).

The counterpart of :mod:`isar.workspace_api.scene`, and deliberately a separate module. That one
is the scene layer's *input* door: persisted fit rows in, ``SceneObservation`` DTOs out, and it is
banned from importing ``isar.scene.store`` at all (``tests/scene/test_import_ban.py``) so a builder
input can never be contaminated by an already-stored revision. This is the *output* door: the
stored revision back out, for a reader (notebook now, GUI later). Keeping them apart is what lets
the input ban stay absolute while this side reads the store.

Read-only and re-fit-free like the rest of the facade (hard rule 7). The one curve source is
:func:`isar.workspace_api.evaluate.evaluate_model` through ``ws.evaluate_fit`` — the delivered
model as it was persisted, evaluated on a chosen grid; nothing here re-fits, re-collates or writes.

**Absence is graceful, corruption is loud** (H1). A workspace with no scene is ordinary and reads
as ``None``. A pointer naming a missing blob, an unparseable blob, or a blob that does not hash to
its own digest is machine-state damage, and the store's ``ValueError`` propagates to the caller
rather than being swallowed into ``ws.warnings`` — the operator has to see it.

**No second model of the payload.** ``SceneRevisionView`` exposes the payload dict; it does not
mirror it in typed fields (H12(e), one representation layer). The hypothesis map IS a derived
product — a projection for a renderer — and states only what the revision already decided:
posterior mass comes from the cluster marginals, visibility from the cluster's recorded reports,
coverage from the recorded footprints. Nothing here re-derives belief.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from isar.scene.store import read_current

from .scene import _scan_ref

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# The stored revision
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SceneRevisionView:
    """One stored scene revision: its pointer metadata plus the payload it names.

    ``payload`` is the revision content verbatim — the same dict the builder digested — and it is
    the contract. The three accessors below are conveniences over its top-level keys, not a typed
    re-modelling of them.
    """

    digest: str
    parent: str | None
    written_at: str | None
    payload: dict

    @property
    def clusters(self) -> list:
        return self.payload.get("clusters") or []

    @property
    def observations(self) -> list:
        return self.payload.get("observations") or []

    @property
    def sample_knowledge(self) -> dict:
        return self.payload.get("sample_knowledge") or {}


def scene_revision(ws) -> SceneRevisionView | None:
    """The scene revision ``ws`` currently points at, or ``None`` if it has none.

    Raises ``ValueError`` on a damaged store (see the module docstring): a corrupt revision is
    never degraded into "no scene", because the two mean opposite things to an operator.
    """
    # Private only in the sense of "not part of the facade's public surface"; this module is
    # inside the facade, and the report directory's location is exactly what it needs.
    current = read_current(ws._report_dir)
    if current is None:
        return None
    pointer, payload = current
    return SceneRevisionView(digest=str(pointer["digest"]), parent=pointer.get("parent"),
                             written_at=pointer.get("written_at"), payload=payload)


# ---------------------------------------------------------------------------
# The hypothesis map
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Curve:
    """A delivered model curve on a chosen grid.

    ``x`` is the SIGNED calibrated energy grid of the observation's window (meV) — the only signed
    axis in this module. A :class:`ModeMarker` at intrinsic ω corresponds to peaks at ``±ω`` here,
    both rendered by the model itself (the Stokes/anti-Stokes tie lives inside the component
    layer), so a renderer overlays markers by folding, never by drawing one side.

    ``pure`` is the evaluator's own flag: True when it rebuilt from stored amplitudes, False when
    it re-solved amplitudes from re-parsed counts.
    """

    x: tuple[float, ...]
    total: tuple[float, ...]
    pure: bool


@dataclass(frozen=True, slots=True)
class ModeMarker:
    """One candidate's mode at ``omega``, carrying that candidate's cluster posterior mass.

    ``omega`` is INTRINSIC (unsigned) mode energy in meV, the scale ``CandidateRow.omegas`` and the
    footprint bands use — not a position on a signed curve grid; see :class:`Curve`.

    ``weight`` is the arena's marginal P(label) as the revision recorded it — every candidate that
    took part in the cluster's enumeration gets markers, not only the winner, so a renderer can
    draw the alternates at the weight the scene actually gives them. Never recomputed here.
    """

    label: str
    omega: float
    weight: float


@dataclass(frozen=True, slots=True)
class HypothesisEntry:
    """What the scene expects at one active observation arena."""

    scan_ref: str
    q: tuple[float, float, float] | None
    # The label the cluster's leading alternative assigns here, and the label the workspace row
    # actually delivered a fitted model for. They usually agree; when they do not, no curve is
    # drawn (there are no stored amplitudes for a candidate that was not the delivered fit).
    winner_label: str | None
    delivered_label: str | None
    winner_curve: Curve | None
    curve_absent_reason: str | None
    mode_markers: tuple[ModeMarker, ...]
    # The cluster's recorded neighbour reports for this arena (leading alternative), verbatim:
    # per chosen mode, the visibility/link-visibility state at each adjacent arena.
    visibility_flags: tuple[dict, ...]


@dataclass(frozen=True, slots=True)
class GapEntry:
    """A stretch of intrinsic ``|ω|`` that carries no update, and why.

    ``lo``/``hi`` are ``None`` for a whole-arena gap — a non-scoring arena, or one whose energy
    window could not be placed at all. A gap is honest absence: a renderer draws nothing there,
    never zero intensity.
    """

    scan_ref: str
    lo: float | None
    hi: float | None
    reason: str


@dataclass(frozen=True, slots=True)
class HypothesisMap:
    """The renderable two-map product of one revision: beliefs, and where there are none."""

    digest: str
    entries: tuple[HypothesisEntry, ...]
    gaps: tuple[GapEntry, ...]


def hypothesis_map(ws, *, x_points: int = 200) -> HypothesisMap | None:
    """Project ``ws``'s stored scene revision into renderable data. ``None`` if no scene.

    One entry per SCORING arena and one or more gap rows for everything the scene does not claim.

    THREE axes, deliberately not unified — each part states what it knows and nothing more:
    ``ModeMarker.omega`` and ``GapEntry`` bounds are intrinsic (unsigned) ``|ω|``, which is what
    the revision itself records; ``Curve.x`` is the SIGNED energy window of the observation,
    because that is the axis the delivered model was fitted on. Folding a marker onto the curve is
    the renderer's ±ω overlay, and doing it here would either discard the curve's anti-Stokes side
    or invent a sign for a mode that has none.
    Deterministic: same revision + same workspace ⇒ identical structure, with every sequence in
    sorted order.
    """
    if x_points < 2:
        raise ValueError(f"x_points must be at least 2, got {x_points}")
    revision = scene_revision(ws)
    if revision is None:
        return None
    payload = revision.payload
    footprints = {f["scan_ref"]: f for f in payload.get("footprints") or ()}
    candidates = {o["scan_ref"]: (o.get("candidates") or [])
                  for o in payload.get("observations") or ()}
    rows = _rows_by_scan_ref(ws)

    entries, gaps = [], []
    for cluster in revision.clusters:
        alternatives = cluster.get("alternatives") or []
        # The same leader rule the builder used when it recorded ``visibility`` (build._leader),
        # so the flags below describe the alternative named here and not a different one.
        leader = max(alternatives, key=lambda e: e["posterior"]) if alternatives else None
        chosen = dict(tuple(pair) for pair in (leader or {}).get("assignment") or ())
        marginals = (cluster.get("marginals") or {}).get("label") or {}
        for ref in sorted(cluster.get("scan_refs") or ()):
            footprint = footprints.get(ref) or {}
            entries.append(_entry(ws, ref, footprint, chosen.get(ref), rows.get(ref),
                                  marginals.get(ref) or {}, candidates.get(ref) or [],
                                  cluster.get("visibility") or [], x_points))
            gaps.extend(_coverage_gaps(ref, footprint.get("bands") or []))

    for row in payload.get("non_scoring") or ():
        # A whole arena the scene declined to score: the footprint says we looked, the revision
        # says nothing was concluded, and the reason travels with the gap.
        gaps.append(GapEntry(scan_ref=row["scan_ref"], lo=None, hi=None,
                             reason=str(row.get("reason"))))

    return HypothesisMap(
        digest=revision.digest,
        entries=tuple(sorted(entries, key=lambda e: e.scan_ref)),
        gaps=tuple(sorted(gaps, key=lambda g: (g.scan_ref, g.lo if g.lo is not None else -1.0))))


def _rows_by_scan_ref(ws) -> dict:
    """The workspace's delivered fits keyed the way the scene keys arenas.

    Same ``_scan_ref`` the builder's input adapter used, so a revision's ``scan_ref`` finds the row
    it was built from; a second key derivation here would silently fail to match pooled masters.
    """
    out = {}
    for fit in getattr(ws, "results", ()) or ():
        ref = _scan_ref(fit)
        if ref is not None:
            out.setdefault(ref, fit)
    return out


def _entry(ws, ref, footprint, winner_label, fit, marginal, candidate_rows, visibility,
           x_points) -> HypothesisEntry:
    curve, reason = _curve(ws, ref, winner_label, fit, x_points)
    return HypothesisEntry(
        scan_ref=ref,
        q=None if footprint.get("q") is None else tuple(float(v) for v in footprint["q"]),
        winner_label=winner_label,
        delivered_label=None if fit is None else fit.best_label,
        winner_curve=curve,
        curve_absent_reason=reason,
        mode_markers=_markers(marginal, candidate_rows),
        visibility_flags=tuple(dict(v) for v in visibility if v.get("scan_ref") == ref))


def _markers(marginal: dict, candidate_rows) -> tuple[ModeMarker, ...]:
    """Weighted mode markers for every candidate the cluster enumerated at this arena.

    A candidate with an unplaceable mode (``omega`` is ``None``) contributes no marker for it —
    there is no position to draw — while its other modes still do; the candidate row itself stays
    in the revision either way.
    """
    omegas = {str(c["label"]): (c.get("omegas") or []) for c in candidate_rows}
    markers = [ModeMarker(label=label, omega=float(w), weight=float(weight))
               for label, weight in marginal.items()
               for w in omegas.get(label, ()) if w is not None]
    return tuple(sorted(markers, key=lambda m: (m.omega, m.label)))


def _curve(ws, ref, winner_label, fit, x_points):
    """``(Curve, None)`` or ``(None, reason)`` — never a guessed curve.

    The evaluator renders the model the row DELIVERED. When the scene's leading alternative names
    a different candidate there are no stored amplitudes for it, so the honest answer is no curve
    plus the reason: drawing the delivered model under the winner's name would attribute one
    hypothesis's intensity to another.
    """
    if fit is None:
        return None, "no delivered row for this arena in results.csv"
    if winner_label is not None and fit.best_label is not None and winner_label != fit.best_label:
        return None, (f"leading alternative {winner_label!r} is not the delivered model "
                      f"{fit.best_label!r}; no stored amplitudes for it")
    window = fit.energy_window
    if window is None or len(window) != 2 or any(v is None for v in window):
        return None, "no placeable energy window"
    lo, hi = float(window[0]), float(window[1])
    step = (hi - lo) / (x_points - 1)
    grid = [lo + step * i for i in range(x_points)]
    try:
        result = ws.evaluate_fit(fit, x_eval=grid)
    except ValueError as exc:                      # includes evaluate.UnsupportedFitView
        log.warning("hypothesis map: %s has no evaluable curve (%s)", ref, exc)
        return None, f"evaluation refused: {exc}"
    return Curve(x=tuple(float(v) for v in result.x),
                 total=tuple(float(v) for v in result.total),
                 pure=bool(result.pure)), None


def _coverage_gaps(ref, bands) -> list:
    """The parts of ``[0, max band hi]`` this arena's footprint does NOT cover.

    The footprint is the signed energy window folded onto intrinsic ``|ω|``, so a window that
    never reaches the elastic line (say 2…10 meV) leaves 0…2 uncovered on both sides — a real
    coverage hole a renderer must not paint over. No bands at all means the window could not be
    placed, which is a whole-arena gap: there is not even an axis extent to complement within.
    """
    spans = sorted((float(b["lo"]), float(b["hi"])) for b in bands)
    if not spans:
        return [GapEntry(scan_ref=ref, lo=None, hi=None, reason="unplaceable_energy_window")]
    gaps, cursor = [], 0.0
    for lo, hi in spans:
        if lo > cursor:
            gaps.append(GapEntry(scan_ref=ref, lo=cursor, hi=lo, reason="uncovered"))
        cursor = max(cursor, hi)
    return gaps

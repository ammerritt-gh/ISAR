"""HeatmapViewModel — view-ready state for the C3 growing (q, E) plane (Task 11, A-5).

design/gui.md item 2: measured spectra render as STRIPS (one measured column per q: the pooled master's
ΣN per common reference monitor where the card collates by merge, else one column per scan), gaps
stay gaps, and the declared projection is always on screen (G9 4D-honesty). The honesty overlays ride on top:
the certifying-tier ω̂ whisker (a warning marker where no tier certifies), the branch-model curve,
per-point assignment-entropy colouring, verdict glyphs, and a visible count of the scans excluded by
the projection window.

ALL derivation lives here; :class:`~isar_gui.views.heatmap_pane.HeatmapPane` only renders the emitted
:class:`~isar.plotspec.spec.PlotSpec` and forwards clicks. isar_gui import contract: workspace_api /
plotspec / PySide6 only — no matplotlib, no isar.session/analyze/drive.

Projection declaration (the plane): among the fitted, non-pooled scans at the viewed temperature we
pick the VARYING q component as the one least concentrated at a single modal value (so a lone
out-of-window scan can never masquerade as the scan axis), fix the other two at their modal values,
and integrate within the card's ``hkl_round_step`` of those modal values. Scans outside that window
are EXCLUDED from the plane and counted — never silently dropped.
"""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import Signal

from isar.plotspec import spec as ps
from isar.workspace_api import fit_display, latest_completed_acquisition
from isar_gui.viewmodels import certify, projection
from isar_gui.viewmodels.base import ViewModel

_LN2 = float(np.log(2.0))
_AXIS = projection.AXIS
_VERDICT_MARKER = {
    "resolved": ps.MarkerKind.VERDICT_RESOLVED,
    "flagged": ps.MarkerKind.VERDICT_FLAGGED,
    "held": ps.MarkerKind.VERDICT_HELD,
}
_T_ROUND = 5.0   # K bin for matching branch-model / prior points to the viewed temperature
_STATE_KEY = "heatmap"
_DISPLAY_STATE_KEY = "fit_display"


def _g(v) -> str:
    return f"{v:g}"


class HeatmapViewModel(ViewModel):
    """Assembles the C3 (q, E) plane spec from the workspace; emits ``spec_changed`` on every
    rebuild and ``point_activated`` (a :class:`~isar.workspace_api.FitView`) when a ω̂ point is
    picked. Temperature is a VM state (one T at a time); :meth:`set_temperature` switches it."""

    #: Emitted after every :meth:`refresh`/rebuild (including temperature switches).
    spec_changed = Signal()
    #: Emitted with the selected ``FitView`` when a ω̂ point is activated (MainWindow routes it to
    #: the inspector).
    point_activated = Signal(object)

    #: Group-drop target kind (plan Part 2A). One displayed T, so a multi-T group is refused.
    TARGET = "heatmap"

    def __init__(self, ws, parent=None):
        super().__init__(ws, parent)
        self._temperature: float | None = None
        self._restriction: set | None = None   # None = all fitted scans; else covered unit ids
        self._restriction_stale = False        # set when a refresh auto-cleared a now-stale restriction
        self._spec: ps.PlotSpec | None = None
        self._points: list = []          # [(x, omega0, FitView)] for click hit-testing
        self._excluded_count = 0
        self._temperatures: list = []
        self._projection: ps.ProjectionLegend | None = None
        self._color_by_entropy = False
        self._exposure_reference: float | None = None   # set per rebuild when master columns draw
        self._rebuild()

    # ---- public state -----------------------------------------------------
    @property
    def spec(self) -> ps.PlotSpec | None:
        return self._spec

    @property
    def excluded_count(self) -> int:
        return self._excluded_count

    @property
    def temperatures(self) -> list:
        return list(self._temperatures)

    @property
    def temperature(self) -> float | None:
        return self._temperature

    @property
    def projection(self) -> ps.ProjectionLegend | None:
        return self._projection

    @property
    def color_by_entropy(self) -> bool:
        return self._color_by_entropy

    # ---- slots ------------------------------------------------------------
    def refresh(self) -> None:
        """Rebuild the spec from the (already-refreshed) workspace and emit ``spec_changed``. Cheap
        and idempotent — the WatchBridge wiring in the next task calls this after ``ws.refresh()``."""
        self._rebuild()
        self.spec_changed.emit()

    def on_workspace_changed(self, changed=None) -> None:
        self._rebuild()
        self.spec_changed.emit()
        super().on_workspace_changed(changed)

    def set_temperature(self, T) -> None:
        """Switch the viewed temperature (one T at a time) and rebuild."""
        self._temperature = None if T is None else round(float(T), 6)
        self.refresh()

    def set_color_by_entropy(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self._color_by_entropy:
            return
        self._color_by_entropy = enabled
        self.refresh()

    # ---- group-drop restriction (plan Part 2A) ------------------------------
    def set_scan_restriction(self, keys) -> None:
        """Restrict the plane to a manual group's covered unit ids; ``None`` restores all fitted
        scans. Pure view filter — no re-fit, no workspace write (G10)."""
        self._restriction = {str(k) for k in keys} if keys is not None else None
        self.refresh()

    @property
    def restriction_active(self) -> bool:
        return self._restriction is not None

    def take_restriction_stale(self) -> bool:
        """True once after a refresh auto-cleared a now-stale restriction (consumed by the dock)."""
        stale, self._restriction_stale = self._restriction_stale, False
        return stale

    def can_accept(self, scan_ids) -> tuple[bool, str]:
        """``(ok, reason)`` for dropping ``scan_ids`` here — refuses a group spanning >1 temperature."""
        from isar_gui import group_targets
        return group_targets.accepts(self.TARGET, self._ws.results, scan_ids)

    def activate_at(self, x, y, tol: float = 0.2) -> None:
        """Emit ``point_activated`` for the ω̂ point nearest ``(x, y)`` (in scale-normalised data
        units) when within ``tol``. The dock calls this on a plot click."""
        fv = self._nearest(x, y, tol)
        if fv is not None:
            self.point_activated.emit(fv)

    def _nearest(self, x, y, tol):
        return projection.nearest_point(self._points, x, y, tol)

    # ---- view-state round trip (G10: view state only, never the workspace) --
    def save_view_state(self, session) -> None:
        session.view_state[_STATE_KEY] = {"temperature": self._temperature}
        session.view_state[_DISPLAY_STATE_KEY] = {
            "color_by_entropy": self._color_by_entropy}

    def load_view_state(self, session) -> None:
        st = (getattr(session, "view_state", None) or {}).get(_STATE_KEY)
        if isinstance(st, dict):
            t = st.get("temperature")
            self._temperature = None if t is None else float(t)
        shared = (getattr(session, "view_state", None) or {}).get(_DISPLAY_STATE_KEY) or {}
        self._color_by_entropy = bool(shared.get("color_by_entropy", False))
        self.refresh()

    # ---- assembly ---------------------------------------------------------
    def _hkl_round_step(self) -> float:
        pol = getattr(self._ws.card, "policy", None)
        return float(getattr(pol, "hkl_round_step", None) or 0.05)

    def _value_label(self) -> str:
        reference = getattr(self, "_exposure_reference", None)
        if reference:
            return f"counts per {reference:g} monitor"
        pol = getattr(self._ws.card, "policy", None)
        mode = getattr(pol, "counting_mode", None)
        return f"counts/{mode}" if mode else "counts"

    def _candidates(self) -> list:
        """Fitted results carrying a full Q and a temperature (the plane's population), narrowed to
        the active group restriction when one is set (plan Part 2A). Auto-clears a now-stale
        restriction (all ids gone on this refresh) so a stale banner never lingers over an empty plane."""
        cands, stale = projection.restrict_or_clear(
            projection.candidates(self._ws.results), self._restriction)
        if stale:
            self._restriction = None
            self._restriction_stale = True
        return cands

    def _rebuild(self) -> None:
        step = self._hkl_round_step()
        cands = self._candidates()
        self._temperatures = sorted({round(fv.T, 6) for fv in cands})
        if self._temperature is None or self._temperature not in self._temperatures:
            self._temperature = projection.modal_T(cands)

        at_t = [fv for fv in cands if self._temperature is not None
                and abs(fv.T - self._temperature) < 1e-6]

        if not at_t:
            self._spec = self._empty_spec()
            self._points, self._excluded_count, self._projection = [], 0, None
            return

        varying, modal = projection.choose_axes(at_t, step)
        fixed = [i for i in range(3) if i != varying]
        in_window, excluded = projection.split_window(at_t, varying, modal, step)
        self._excluded_count = len(excluded)

        self._spec = self._build_spec(in_window, varying, modal, fixed, step)
        self._projection = self._spec.projection

    def _empty_spec(self) -> ps.PlotSpec:
        return ps.PlotSpec(title="(q, E) plane — no fitted scans", xlabel="q (rlu)",
                           ylabel="energy transfer (meV)", value_label=self._value_label())

    # ---- the spec ---------------------------------------------------------
    def _build_spec(self, fits, varying, modal, fixed, step) -> ps.PlotSpec:
        strips, strip_fits = self._strips(fits, varying, step)
        series, markers, annotations, points, entropy = self._branch_first_overlays(fits, varying)
        branch = self._branch_series(fits, varying, modal, step)
        if branch:
            series[0:0] = branch
        self._points = points

        projection = self._projection_legend(varying, modal, fixed, step)
        if self._excluded_count > 0:
            annotations.append(ps.Annotation(
                text=f"{self._excluded_count} scans outside this projection", xy=None))
        strip_levels, clip_note = self._inelastic_levels(strips, fits)
        if clip_note:
            annotations.append(ps.Annotation(text=clip_note, xy=None))

        axis = _AXIS[varying]
        T = self._temperature
        bm = getattr(self._ws, "branch_model", None)
        track_payload = getattr(bm, "branch_tracks", None) if bm is not None else None
        title = f"(q, E) plane · {projection.plane}" + (f" · T={_g(T)} K" if T is not None else "")
        meta = {"varying": varying, "modal": modal, "temperature": T,
                "excluded_count": self._excluded_count,
                "omega_hat_entropy_all": entropy,
                "scan_ids": [s.meta.get("scan_id") for s in strips]}
        if isinstance(track_payload, dict) and track_payload:
            meta["branch_track_revision"] = track_payload.get("revision")
            meta["branch_frontiers"] = track_payload.get("frontiers")
        regions = self._latest_regions(strips, fits)
        return ps.PlotSpec(
            title=title, xlabel=f"{axis} (rlu)", ylabel="energy transfer (meV)",
            series=series, markers=markers, annotations=annotations, strips=strips,
            regions=regions,
            projection=projection, value_label=self._value_label(),
            strip_levels=strip_levels,
            meta=meta,
        )

    #: The elastic line is excluded from the colour scale out to this many resolution FWHMs of
    #: E = 0. The line is resolution-limited, so 1.5 FWHM covers it with its wings; a phonon that
    #: close to zero is inside the elastic line anyway.
    ELASTIC_EXCLUSION_FWHM = 1.5

    @classmethod
    def _inelastic_levels(cls, strips, fits):
        """``((vmin, vmax), note)`` for the plane's colour scale, set by the INELASTIC signal.

        The scan axis is calibrated energy transfer, so E = 0 is the elastic line wherever a column
        reaches it. At a Bragg position that line is thousands of counts against phonons of ten,
        and a min..max scale then shows nothing but the line (the operator's report, 2026-09-05).
        Levels come from the cells with ``|E| > ELASTIC_EXCLUSION_FWHM x`` the plane's resolution
        FWHM; cells above ``vmax`` saturate and a note says so. ``(None, None)`` when no column
        reaches the elastic band or nothing is finite — the renderers then span min..max as before.
        The operator can still drag the colourbar; this is only the default the plane opens with.
        """
        fwhm = [float(f.gauss_fwhm) for f in fits
                if getattr(f, "gauss_fwhm", None) is not None and float(f.gauss_fwhm) > 0]
        exclusion = cls.ELASTIC_EXCLUSION_FWHM * (float(np.median(fwhm)) if fwhm else 1.0)
        inelastic, everything, reaches = [], [], False
        for s in strips:
            y = np.asarray(s.y, float)
            v = np.asarray(s.values, float)
            keep = np.isfinite(v)
            if not keep.any():
                continue
            everything.append(v[keep])
            outside = keep & (np.abs(y) > exclusion)
            reaches = reaches or bool((keep & ~outside).any())
            if outside.any():
                inelastic.append(v[outside])
        if not reaches or not inelastic:
            return None, None
        allv = np.concatenate(everything)
        inel = np.concatenate(inelastic)
        vmin, vmax = float(np.min(allv)), float(np.max(inel))
        if vmax <= vmin or float(np.max(allv)) <= vmax:
            return None, None            # nothing brighter than the inelastic signal: no clip
        return (vmin, vmax), (f"colour scale set by |E| > {exclusion:.2g} meV; "
                              "the elastic line saturates")

    def _strips(self, fits, varying, step):
        """One MEASURED column per in-window q position.

        Where the card collates by merge the column is the pooled MASTER — the collated group's ΣN,
        normalised to one plane-wide reference monitor so columns pooling different member counts
        are comparable. The master is located through its members, never through its fit, so a
        withheld or failed master fit still shows its data (:func:`projection.pooled_masters`).
        Only a q with no persisted master at all (separate collation, or a workspace predating the
        pooled-spectrum artifact) draws one column per member scan; without this rule the member
        columns of one q stack at the same x and the most recent scan silently covers the rest.
        Width = 0.8 x the median spacing between distinct varying-q positions (cosmetic; never
        overlaps)."""
        rows = []
        masters = projection.pooled_masters(self._ws, fits)
        reference = projection.common_exposure_reference([m.spectrum for m in masters])
        self._exposure_reference = reference
        covered = set()
        for m in masters:
            covered.update(fv.unit_id for fv in m.members)
            values, _yerr = projection.counts_per_reference(m.spectrum, reference)
            if not np.isfinite(values).any():
                continue
            rows.append((float(m.members[0].q[varying]), np.asarray(m.spectrum.x, float), values, {
                "scan_id": m.scan_number, "unit_id": m.scan_id, "pooled": True,
                "acquisition_id": getattr(m.master, "acquisition_id", None),
                "member_unit_ids": list(m.member_ids),
                "member_acquisition_ids": [fv.acquisition_id for fv in m.members
                                           if fv.acquisition_id is not None],
                "exposure_reference": reference}))
        for fv in fits:
            if fv.pooled or fv.unit_id in covered:
                continue
            energy, counts, _yerr = self._ws.scan_data(fv.scan_number)
            if energy is None or counts is None or len(energy) == 0:
                continue
            rows.append((float(fv.q[varying]), np.asarray(energy, float),
                         np.asarray(counts, float),
                         {"scan_id": fv.scan_number, "unit_id": fv.unit_id,
                          "acquisition_id": fv.acquisition_id}))
        if not rows:
            return [], []
        xs = sorted({round(x, 6) for x, *_ in rows})
        if len(xs) >= 2:
            spacing = float(np.median(np.diff(xs)))
        else:
            spacing = 2.0 * step
        width = 0.8 * spacing if spacing > 0 else 0.8 * (2.0 * step)
        strips = [ps.Strip(x_center=x, width=width, y=e, values=c, meta=meta)
                  for (x, e, c, meta) in rows]
        return strips, rows

    @staticmethod
    def _strip_covers(strip, *, acquisition_id=None, unit_id=None) -> bool:
        """Whether a column stands for this acquisition/unit — directly, or as a master pooling it."""
        meta = strip.meta
        if acquisition_id is not None:
            return (meta.get("acquisition_id") == acquisition_id
                    or acquisition_id in (meta.get("member_acquisition_ids") or ()))
        return (meta.get("unit_id") == unit_id
                or unit_id in (meta.get("member_unit_ids") or ()))

    def _latest_regions(self, strips, fits) -> list[ps.RegionBox]:
        events = list(self._ws.session_events()) if hasattr(self._ws, "session_events") else []
        latest = latest_completed_acquisition(events)
        selected = []
        if latest.detailed_events_present:
            if latest.acquisition_id is None:
                return []
            selected = [strip for strip in strips
                        if self._strip_covers(strip, acquisition_id=latest.acquisition_id)]
        else:
            physical = [fit for fit in getattr(self._ws, "results", ()) if not fit.pooled]
            if not physical:
                return []
            fallback = physical[-1]
            if fallback.acquisition_id is not None:
                selected = [strip for strip in strips
                            if self._strip_covers(strip, acquisition_id=fallback.acquisition_id)]
            else:
                selected = [
                    strip for strip in strips
                    if (fallback.unit_id is not None
                        and self._strip_covers(strip, unit_id=fallback.unit_id))
                    or (fallback.unit_id is None
                        and strip.meta.get("scan_id") == fallback.scan_number)]
        if not selected:
            return []
        return [ps.RegionBox(
            x_min=min(strip.x_center - strip.width / 2.0 for strip in selected),
            x_max=max(strip.x_center + strip.width / 2.0 for strip in selected),
            y_min=min(float(np.min(strip.y)) for strip in selected),
            y_max=max(float(np.max(strip.y)) for strip in selected),
            kind=ps.RegionKind.LATEST_ACQUISITION,
            label="latest completed acquisition")]

    def _branch_first_overlays(self, fits, varying):
        """Build the shared ω̂ overlay, including additive unselected branch candidates."""
        from isar.plotspec import builders

        overlay_points = []
        hit_points = []
        entropy = []
        assessments = getattr(self._ws, "master_assessments", ())
        branch_tracks = getattr(getattr(self._ws, "branch_model", None), "branch_tracks", None)
        for fv in fits:
            x = float(fv.q[varying])
            candidates = projection.unselected_candidate_points(fv, x)
            if fv.omega0 is None:
                overlay_points.extend({
                    **candidate, "display_state": "unselected_candidate"}
                    for candidate in candidates)
                continue
            display = fit_display(
                fv, assessments, branch_tracks=branch_tracks)
            y = float(fv.omega0)
            if display.assessment is not None \
                    and getattr(display.assessment, "omega0", None) is not None:
                y = float(display.assessment.omega0)
            hit_points.append((x, y, fv))
            ent = fv.assignment_entropy
            ent = None if ent is None else float(ent)
            entropy.append(ent)
            overlay_points.append({
                "q": x, "omega": y, "entropy": ent,
                "interval": (None if display.interval is None else
                             (display.interval.lo, display.interval.hi)),
                "display_state": display.state.value,
            })
            overlay_points.extend({
                **candidate, "display_state": "unselected_candidate"}
                for candidate in candidates)
        overlay = builders.dispersion_spec(
            points=overlay_points,
            projection=ps.ProjectionLegend(plane="overlay", integration="overlay"),
            color_by_entropy=self._color_by_entropy)
        return overlay.series, overlay.markers, overlay.annotations, hit_points, entropy

    def _overlays(self, fits, varying):
        """ω̂ scatter (whisker only where a tier certifies), warning markers for uncertified ω̂,
        verdict glyphs, and the per-point assignment-entropy channel (kept in meta; 0→calm, ln2→
        undecided). Returns (series, markers, annotations, points, entropy)."""
        rel_x, rel_y, rel_ent = [], [], []
        pool_x, pool_y, pool_err, pool_ent, pool_n = [], [], [], [], []
        markers: list[ps.Marker] = []
        annotations: list[ps.Annotation] = []
        points: list = []
        entropy: list = []
        any_unreliable = False
        for fv in fits:
            if fv.omega0 is None:
                continue
            x = float(fv.q[varying])
            y = float(fv.omega0)
            points.append((x, y, fv))
            ent = fv.assignment_entropy
            ent = None if ent is None else float(ent)
            entropy.append(ent)
            cert = certify.certifying_interval(fv)
            genuinely_unreliable = bool(
                fv.interval_reliable is False or
                (fv.pooled and (fv.pool_consistent is False or fv.modality == "fork")))
            certifying = bool(cert is not None and not genuinely_unreliable
                              and fv.track_state not in {
                                  "ambiguous", "missing", "provisional", "blocked"})
            half = (abs(float(cert[-1]) - float(cert[0])) / 2.0
                    if certifying and len(cert) >= 2 else None)
            if fv.pooled:
                # The delivered per-q master: shown on its OWN whiskered scatter (its own interval,
                # certify law), marked POOLED, so it is visible AND distinct from the member points.
                if half is not None:
                    pool_x.append(x)
                    pool_y.append(y)
                    pool_err.append(half)
                    pool_ent.append(ent)
                    pool_n.append(fv.n_members)
                elif genuinely_unreliable:
                    any_unreliable = True
                    markers.append(ps.Marker(x=x, y=y, kind=ps.MarkerKind.POOLED_WARNING,
                                             label="pooled master (unreliable)"))
                else:
                    markers.append(ps.Marker(x=x, y=y, kind=ps.MarkerKind.POOLED_UNCERTIFIED,
                                             label="pooled master (not certifying)"))
            else:
                rel_x.append(x)
                rel_y.append(y)
                rel_ent.append(ent)

        # The entropy colour channel only rides on the certifying-ω̂ scatter (the whiskered points);
        # attach it — and the legend note explaining it — ONLY when at least one such point carries an
        # entropy value, so the note never asserts an encoding that isn't on screen.
        colored = bool(rel_x) and any(e is not None for e in rel_ent)
        series: list[ps.Series] = []
        if rel_x:
            series.append(ps.Series(
                x=np.array(rel_x), y=np.array(rel_y),
                label="individual scan fits (provenance)", role=ps.Role.DATA,
                style="muted",
                state=(ps.State.ENTROPY if colored else None),
                color_values=(list(rel_ent) if colored else None),
                color_norm=(ps.LN2 if colored else None)))
        if pool_x:
            p_colored = any(e is not None for e in pool_ent)  # masters: entropy n/a unless present
            ns = [n for n in pool_n if n is not None]
            if len(pool_x) == 1 and ns:
                plabel = f"pooled master ({int(ns[0])})"
            elif ns:
                plabel = f"pooled masters ({len(pool_x)})"
            else:
                plabel = "pooled master"
            series.append(ps.Series(
                x=np.array(pool_x), y=np.array(pool_y), yerr=np.array(pool_err),
                label=plabel, role=ps.Role.DATA, state=ps.State.POOLED,
                color_values=(list(pool_ent) if p_colored else None),
                color_norm=(ps.LN2 if p_colored else None)))
            colored = colored or p_colored
        if any_unreliable:
            # One legend-area note, NOT one per point.
            annotations.append(ps.Annotation(text="(unreliable)", xy=None,
                                             role=ps.State.UNRELIABLE))
        if colored:
            annotations.append(ps.Annotation(
                text="color = assignment entropy (ln 2 = undecided)", xy=None))
        return series, markers, annotations, points, entropy

    def _branch_series(self, fits, varying, modal, step):
        """Segmented target/rival tracks, with the legacy dispersion prior as fallback."""
        from isar.plotspec import builders

        bm = getattr(self._ws, "branch_model", None)
        sample = projection.modal_sample(fits)
        tracks = projection.branch_track_overlays(
            getattr(bm, "branch_tracks", None) if bm is not None else None,
            varying, sample=sample, T=self._temperature, t_round=_T_ROUND,
            modal=modal, step=step)
        if tracks:
            overlay = builders.dispersion_spec(
                points=[], branches=tracks,
                projection=ps.ProjectionLegend(plane="overlay", integration="overlay"))
            return [s for s in overlay.series if s.role is ps.Role.BRANCH]
        pts = list(getattr(bm, "dispersion_prior", None) or []) if bm is not None else []
        curve = projection.prior_curve(pts, varying, sample=sample,
                                       T=self._temperature, t_round=_T_ROUND)
        if curve is None:
            return []
        xs, ws = curve
        return [ps.Series(x=xs, y=ws, label="branch model", role=ps.Role.BRANCH)]

    def _projection_legend(self, varying, modal, fixed, step) -> ps.ProjectionLegend:
        return projection.projection_legend(varying, modal, step)

"""Spec builders: turn read-side records into :class:`~isar.plotspec.spec.PlotSpec` objects. This is
the seam the report figures and the GUI viewmodels share (G8) — read-only accessors in, a pure spec
out. Only the spectrum/fit overlay is migrated for now; the rest of `isar/report/figures.py`
migrates lazily.

Imports only numpy + the record accessors it reads (`isar.schema.axes`); never matplotlib, Qt,
`isar.report`, `isar.drive`, or `isar.analyze` (importing report here would cycle — report figures
delegate to this module)."""
from __future__ import annotations

import numpy as np

from isar.plotspec import spec as ps
from isar.schema import axes


def _rejected_points(sp) -> np.ndarray:
    """Point indices with at least one cosmic-ray-rejected miniscan pass (kept for the ring glyph)."""
    if sp.miniscan_reject is None or sp.miniscan_counts is None:
        return np.zeros(0, dtype=int)
    rejected = sp.miniscan_reject & ~np.isnan(sp.miniscan_counts)
    return np.nonzero(rejected.any(axis=1))[0]


def spectrum_spec(record, det_id: str) -> ps.PlotSpec:
    """The spectrum/fit overlay for one analyzer as a spec: kept points as a DATA series with
    Poisson whiskers, masked points as MASKED markers (shown, not deleted), cosmic-ray-cleaned
    points as COSMIC_RAY rings. Fixed-Q, so the projection legend declares the integration."""
    sp = record.spectra[det_id]
    energy = axes.scanned_axes(record)[0].values
    mask = sp.mask.astype(bool)
    yerr = np.sqrt(np.clip(sp.variance, 0.0, None))

    series = [ps.Series(x=energy[mask], y=sp.intensity[mask], yerr=yerr[mask],
                        label="kept (+/-sqrt N)", role=ps.Role.DATA)]
    markers = [ps.Marker(x=float(e), y=float(v), kind=ps.MarkerKind.MASKED,
                         label="masked (shown, not deleted)")
               for e, v in zip(energy[~mask], sp.intensity[~mask])]
    markers += [ps.Marker(x=float(energy[i]), y=float(sp.intensity[i]),
                          kind=ps.MarkerKind.COSMIC_RAY, label="cosmic-ray cleaned")
                for i in _rejected_points(sp)]

    q = [round(float(v), 3) for v in sp.q]
    projection = ps.ProjectionLegend(plane="E", integration=f"Q = {q} (fixed)")
    return ps.PlotSpec(
        title=f"scan {record.scan_number}  {det_id}   Q={q}",
        xlabel="energy transfer (meV)",
        ylabel="counts (raw; Poisson sqrt N errors)",
        series=series, markers=markers, projection=projection,
        meta={"scan_number": record.scan_number, "det_id": det_id, "q": q},
    )


# ---------------------------------------------------------------------------
# Fit inspector (C2) builders — plain-data in, PlotSpec out (no workspace_api here).
# ---------------------------------------------------------------------------
def _interval_at(x, y, x0):
    """Curve value at ``x0`` by linear interpolation (for placing the ω₀ marker), or the peak of
    ``y`` when ``x0`` is missing/out of range or the grid is empty."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    if y.size == 0:
        return 0.0
    if x0 is None or x.size == 0 or x0 < float(np.min(x)) or x0 > float(np.max(x)):
        return float(np.max(y))
    return float(np.interp(float(x0), x, y))


def fit_overlay_spec(*, x, counts, yerr, x_fit, total, components,
                     omega0=None, interval=None, interval_reliable=True,
                     scan_number=None, det_id=None, q=None, pooled=False,
                     members=None, n_members=None, title=None,
                     xlabel="energy transfer (meV)",
                     ylabel="counts (raw; Poisson sqrt N errors)"):
    """The C2 data+fit overlay as a :class:`~isar.plotspec.spec.PlotSpec`.

    Plain data only (no workspace_api import): measured ``(x, counts, yerr)``; the delivered curve
    ``(x_fit, total)`` and its ``components`` (a list of ``(name, values)`` over ``x_fit``); ω₀ with
    its interval + reliability flag; pooled/members provenance for the honesty channel.

    Rendering discipline (binding): a **pooled master** NEVER draws a raw-data overlay — the true
    pooled spectrum is not persisted, so drawing any single member's counts under a "pooled" title
    would mislead. A pooled spec is curve-only and carries a "member data not shown" annotation; its
    members are still enumerated in ``meta``. The ω₀ marker shows an interval whisker ONLY when the
    interval is reliable — an unreliable ω₀ is the sanctioned warning-marker + ``"(unreliable)"``
    annotation (via :func:`~isar.plotspec.spec.mark_unreliable`), never a numeric whisker.
    """
    series: list[ps.Series] = []
    annotations: list[ps.Annotation] = []
    # Honesty law: a pooled master shows no member counts (curve-only + a note). Per-scan fits draw
    # their measured data with Poisson whiskers.
    if pooled:
        n = n_members if n_members else "?"
        annotations.append(ps.Annotation(
            text=f"pooled master — member data not shown ({n} members)", xy=None,
            role=ps.State.POOLED))
    elif counts is not None:
        x = np.asarray(x, dtype=float)
        counts = np.asarray(counts, dtype=float)
        yerr = None if yerr is None else np.asarray(yerr, dtype=float)
        series.append(ps.Series(x=x, y=counts, yerr=yerr, label="data (+/-sqrt N)",
                                role=ps.Role.DATA))

    if x_fit is not None and total is not None:
        xf = np.asarray(x_fit, dtype=float)
        fit_label = f"pooled fit ({n_members} members)" if pooled and n_members else "fit"
        series.append(ps.Series(x=xf, y=np.asarray(total, dtype=float), label=fit_label,
                                role=ps.Role.FIT))
        for name, values in (components or []):
            series.append(ps.Series(x=xf, y=np.asarray(values, dtype=float), label=str(name),
                                    role=ps.Role.COMPONENT))

    markers: list[ps.Marker] = []
    # ω₀ marker — reliable interval => a whisker; unreliable => warning-marker + annotation only.
    grid_x = x_fit if (x_fit is not None and total is not None) else x
    grid_y = total if (x_fit is not None and total is not None) else counts
    if omega0 is not None and grid_y is not None:
        y0 = _interval_at(grid_x, grid_y, omega0)
        if interval_reliable and interval is not None and len(interval) >= 2:
            lo, hi = float(interval[0]), float(interval[-1])
            half = abs(hi - lo) / 2.0
            series.append(ps.Series(x=np.array([float(omega0)]), y=np.array([y0]),
                                    yerr=np.array([half]), label="ω₀ (interval)", role=ps.Role.FIT))
        else:
            markers.append(ps.Marker(x=float(omega0), y=y0, kind=ps.MarkerKind.WARNING,
                                     label="ω₀ (unreliable)"))
            annotations.append(ps.Annotation(text="(unreliable)", xy=(float(omega0), y0),
                                              role=ps.State.UNRELIABLE))

    projection = None
    if q is not None:
        qr = [round(float(v), 3) for v in q]
        projection = ps.ProjectionLegend(plane="E", integration=f"Q = {qr} (fixed)")

    if title is None:
        base = f"scan {scan_number}" if scan_number is not None else "fit"
        if det_id:
            base += f"  {det_id}"
        title = f"pooled — {base}" if pooled else base

    return ps.PlotSpec(
        title=title, xlabel=xlabel, ylabel=ylabel,
        series=series, markers=markers, annotations=annotations, projection=projection,
        meta={"scan_number": scan_number, "det_id": det_id, "q": None if q is None else list(q),
              "pooled": bool(pooled), "n_members": n_members,
              "members": list(members) if members else None,
              "omega0": None if omega0 is None else float(omega0),
              "interval": None if interval is None else [float(v) for v in interval],
              "interval_reliable": bool(interval_reliable)},
    )


_VERDICT_MARKER = {
    "resolved": ps.MarkerKind.VERDICT_RESOLVED,
    "flagged": ps.MarkerKind.VERDICT_FLAGGED,
    "held": ps.MarkerKind.VERDICT_HELD,
}


def dispersion_spec(*, points, projection, prior=None, branches=None,
                    off_curve=None, color_norm=None,
                    title="dispersion ω̂(q)", xlabel="q (rlu)",
                    ylabel="energy transfer (meV)", value_label=None, meta=None):
    """The C5 dispersion view (ω̂ vs q) as a :class:`~isar.plotspec.spec.PlotSpec` — the heatmap's
    1-D sibling, same honesty law, plain data only (no workspace_api).

    ``points`` is a list of per-ω̂ dicts: ``q`` and ``omega`` (required); ``yerr`` — the certifying
    half-width (symmetric, from the interval ladder) or ``None`` when NO tier certifies; ``entropy``
    (assignment entropy or None); ``verdict`` (resolved/flagged/held or None); ``pooled`` (bool) and
    ``n_members`` for a combine-before-divide master.

    Binding rendering discipline:

    * a point with a ``yerr`` is a certifying ω̂ — it joins the DATA scatter with that whisker; a
      point with ``yerr is None`` is uncertified — a WARNING :class:`~isar.plotspec.spec.Marker`
      and a SINGLE ``"(unreliable)"`` note, never a numeric whisker;
    * the assignment-entropy colour channel rides ONLY on the whiskered scatter (values aligned to
      that subset) and its legend note appears ONLY when at least one whiskered point carries an
      entropy value — the note never asserts an encoding that isn't on screen;
    * a POOLED master is the delivered per-q answer: it is drawn on its own DATA scatter tagged
      :data:`~isar.plotspec.spec.State.POOLED` and labeled ``"pooled (N)"``, carrying a whisker per
      the same certify law (a master owns its own interval; ``yerr is None`` → a warning marker), so
      it is visible AND distinguishable from the per-scan member points, which stay shown;
    * ``prior`` (``(xs, ys)``) draws as a :data:`~isar.plotspec.spec.Role.PRIOR` curve; ``branches``
      (list of ``(label, xs, ys)``) as :data:`~isar.plotspec.spec.Role.BRANCH` curves — both optional;
    * ``off_curve`` (list of ``{q, omega, n_sigma}``) annotates points that sit >kσ off the prior;
    * the :class:`~isar.plotspec.spec.ProjectionLegend` is MANDATORY (G9 4D-honesty) — ``None`` raises.
    """
    if projection is None:
        raise ValueError("dispersion_spec requires a ProjectionLegend (declared projection "
                         "is always on screen; G9 4D-honesty)")

    series: list[ps.Series] = []
    markers: list[ps.Marker] = []
    annotations: list[ps.Annotation] = []

    rel_x, rel_y, rel_err, rel_ent = [], [], [], []
    pool_x, pool_y, pool_err, pool_ent, pool_n = [], [], [], [], []
    any_unreliable = False
    for p in points or []:
        x = float(p["q"])
        y = float(p["omega"])
        ent = p.get("entropy")
        ent = None if ent is None else float(ent)
        yerr = p.get("yerr")
        if p.get("pooled"):
            # Pooled master: its OWN interval (not a member's) whiskers it, per certify law; no
            # certifying interval → the same warning-marker form as any uncertified ω̂.
            if yerr is not None:
                pool_x.append(x)
                pool_y.append(y)
                pool_err.append(float(yerr))
                pool_ent.append(ent)
                pool_n.append(p.get("n_members"))
            else:
                any_unreliable = True
                markers.append(ps.Marker(x=x, y=y, kind=ps.MarkerKind.WARNING,
                                         label="pooled ω̂ (unreliable)"))
        elif yerr is not None:
            rel_x.append(x)
            rel_y.append(y)
            rel_err.append(float(yerr))
            rel_ent.append(ent)
        else:
            any_unreliable = True
            markers.append(ps.Marker(x=x, y=y, kind=ps.MarkerKind.WARNING,
                                     label="ω̂ (unreliable)"))
        kind = _VERDICT_MARKER.get((p.get("verdict") or "").lower())
        if kind is not None:
            markers.append(ps.Marker(x=x, y=y, kind=kind, label=f"verdict: {p['verdict']}"))

    # Prior + branch overlays first (drawn under the scatter).
    if prior is not None:
        px, py = prior
        series.append(ps.Series(x=np.asarray(px, float), y=np.asarray(py, float),
                                label="dispersion prior", role=ps.Role.PRIOR))
    for label, bx, by in (branches or []):
        series.append(ps.Series(x=np.asarray(bx, float), y=np.asarray(by, float),
                                label=str(label), role=ps.Role.BRANCH))

    # The entropy colour channel rides ONLY on the certifying (whiskered) scatter; attach it — and
    # its legend note — ONLY when at least one such point carries an entropy value.
    colored = bool(rel_x) and any(e is not None for e in rel_ent)
    if rel_x:
        series.append(ps.Series(
            x=np.asarray(rel_x, float), y=np.asarray(rel_y, float), yerr=np.asarray(rel_err, float),
            label="ω̂ (certifying interval)", role=ps.Role.DATA,
            state=(ps.State.ENTROPY if colored else None),
            color_values=(list(rel_ent) if colored else None),
            color_norm=((color_norm if color_norm is not None else ps.LN2) if colored else None)))
    if pool_x:
        # Masters usually carry no assignment entropy (n/a); colour the pooled scatter only when at
        # least one master actually has one — same truth-gating as the member channel.
        p_colored = any(e is not None for e in pool_ent)
        ns = [n for n in pool_n if n is not None]
        if len(pool_x) == 1 and ns:
            plabel = f"pooled ({int(ns[0])})"
        elif ns:
            plabel = f"pooled masters ({len(pool_x)})"
        else:
            plabel = "pooled master"
        series.append(ps.Series(
            x=np.asarray(pool_x, float), y=np.asarray(pool_y, float),
            yerr=np.asarray(pool_err, float), label=plabel, role=ps.Role.DATA, state=ps.State.POOLED,
            color_values=(list(pool_ent) if p_colored else None),
            color_norm=((color_norm if color_norm is not None else ps.LN2) if p_colored else None)))
        colored = colored or p_colored

    if any_unreliable:
        annotations.append(ps.Annotation(text="(unreliable)", xy=None, role=ps.State.UNRELIABLE))
    if colored:
        annotations.append(ps.Annotation(
            text="color = assignment entropy (ln 2 = undecided)", xy=None))

    for oc in (off_curve or []):
        nσ = oc.get("n_sigma")
        note = "off prior" if nσ is None else f"off prior ({float(nσ):.1f}σ)"
        annotations.append(ps.Annotation(text=note, xy=(float(oc["q"]), float(oc["omega"]))))

    return ps.PlotSpec(
        title=title, xlabel=xlabel, ylabel=ylabel,
        series=series, markers=markers, annotations=annotations,
        projection=projection, value_label=value_label, meta=dict(meta or {}))


def waterfall_spec(*, traces, offset_step, q=None, axis_label=None,
                   title="waterfall", xlabel="energy transfer (meV)",
                   ylabel="counts (offset per trace)", meta=None) -> ps.PlotSpec:
    """The C4 waterfall view: one DATA+FIT trace pair per ``traces`` entry, stacked with a vertical
    offset (``i * offset_step`` for the i-th trace, 0-indexed) — plain data only, no workspace_api.

    ``traces`` is a list of per-trace dicts: ``label`` (e.g. ``"T=12 K"``), ``x``/``counts``/``yerr``
    (the measured spectrum; ``counts`` is ``None`` for a pooled trace), ``curve_x``/``curve_total``
    (the delivered fit curve), ``pooled`` (bool), ``n_members``.

    The offset is a pure VIEW-STATE display device (G10: never written to the workspace/card) — so
    it is ALWAYS declared on screen via an Annotation (``"offset +<step> per trace"``), the honesty
    rule for stacked plots. ``yerr`` is preserved at its true magnitude (NOT shifted) since it is a
    property of the data, not of the display offset.

    A **pooled** trace mirrors the Task-10/``fit_overlay_spec`` honesty rule verbatim: its true
    pooled spectrum is not persisted, so it draws NO data series (curve-only); its fit-curve label
    carries ``"pooled (N) — member data not shown"`` so the missing counts are never silently implied
    to be a real measured trace.

    ``q``/``axis_label`` (optional) add the fixed-q honesty declaration as a :class:`ProjectionLegend`
    (this view fixes q and varies T — the ``dispersion_spec`` mandatory-projection rule doesn't apply
    here since q, not a plane, is what's fixed, but the fixed value is still worth declaring)."""
    series: list[ps.Series] = []
    annotations: list[ps.Annotation] = []

    for i, tr in enumerate(traces or []):
        offset = i * float(offset_step)
        label = tr.get("label") or f"trace {i}"
        pooled = bool(tr.get("pooled"))
        n_members = tr.get("n_members")

        if pooled:
            annotations.append(ps.Annotation(
                text=f"{label}: pooled ({n_members or '?'}) — member data not shown",
                xy=None, role=ps.State.POOLED))
        elif tr.get("counts") is not None:
            x = np.asarray(tr["x"], dtype=float)
            y = np.asarray(tr["counts"], dtype=float) + offset
            yerr = None if tr.get("yerr") is None else np.asarray(tr["yerr"], dtype=float)
            series.append(ps.Series(x=x, y=y, yerr=yerr, label=label, role=ps.Role.DATA))

        if tr.get("curve_x") is not None and tr.get("curve_total") is not None:
            xf = np.asarray(tr["curve_x"], dtype=float)
            yf = np.asarray(tr["curve_total"], dtype=float) + offset
            fit_label = (f"{label}: pooled ({n_members or '?'}) — member data not shown"
                        if pooled else label)
            series.append(ps.Series(x=xf, y=yf, label=fit_label, role=ps.Role.FIT))

    annotations.append(ps.Annotation(text=f"offset +{float(offset_step):g} per trace", xy=None))

    projection = None
    if q is not None:
        axis = axis_label or "q"
        projection = ps.ProjectionLegend(plane="E (per T)", integration=f"{axis}={float(q):g} (fixed)")

    return ps.PlotSpec(
        title=title, xlabel=xlabel, ylabel=ylabel,
        series=series, annotations=annotations, projection=projection,
        meta=dict(meta or {}),
    )


def residuals_spec(*, x, residuals, yerr=None, xlabel="energy transfer (meV)",
                   ylabel="data - fit (counts)", title="residuals"):
    """Residuals (measured - delivered curve, on the measured grid) as a RESIDUAL-role PlotSpec.

    A single :data:`Role.RESIDUAL` series over the data grid; ``yerr`` (Poisson whiskers) optional.
    The zero line is implicit — no separate series, so the spec stays minimal and golden-stable.
    """
    x = np.asarray(x, dtype=float)
    r = np.asarray(residuals, dtype=float)
    yerr = None if yerr is None else np.asarray(yerr, dtype=float)
    return ps.PlotSpec(
        title=title, xlabel=xlabel, ylabel=ylabel,
        series=[ps.Series(x=x, y=r, yerr=yerr, label="residual", role=ps.Role.RESIDUAL)],
    )

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

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

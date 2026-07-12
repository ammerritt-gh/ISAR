"""Matplotlib figures for the report (D6: static PNG, `Agg` backend — the `isar/synth/demo.py` idiom).

Every figure obeys "shown, not hidden" (§7.4): masked points are drawn (red x), cosmic-ray-cleaned
points are ringed, and `miniscan_figure` is the per-pass drill-down — the individual acquisition
passes at a rejected point with the rejected one(s) in red — so the human sees exactly what was
removed and how it compared to the survivors. Each function returns PNG bytes (for inline HTML or a
file).
"""
from __future__ import annotations

import io

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from isar.plotspec.backends import mpl as plotspec_mpl  # noqa: E402
from isar.plotspec.builders import spectrum_spec  # noqa: E402
from isar.report import model as _model  # noqa: E402  (id helpers only; no cycle — model has no figures)
from isar.schema import axes  # noqa: E402


def _png_bytes(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def _rejected_points(sp) -> np.ndarray:
    if sp.miniscan_reject is None or sp.miniscan_counts is None:
        return np.zeros(0, dtype=int)
    rejected = sp.miniscan_reject & ~np.isnan(sp.miniscan_counts)
    return np.nonzero(rejected.any(axis=1))[0]


def spectrum_figure(record, det_id: str) -> bytes:
    """Rate vs energy for one analyzer: kept points, masked points (red x), cosmic-ray-cleaned rings.

    Pilot migration (G8): built as a shared :class:`~isar.plotspec.spec.PlotSpec` and rendered by the
    matplotlib backend, so the GUI and the report draw this from one definition."""
    return plotspec_mpl.render_png(spectrum_spec(record, det_id))


def miniscan_figure(record, det_id: str, point: int) -> bytes:
    """The per-pass view at one point: every acquisition pass, rejected ones in red vs the survivors."""
    sp = record.spectra[det_id]
    counts = sp.miniscan_counts[point]
    valid = ~np.isnan(counts)
    reject = (sp.miniscan_reject[point] if sp.miniscan_reject is not None
              else np.zeros(len(counts), dtype=bool))
    idx = np.arange(len(counts))
    energy = axes.scanned_axes(record)[0].values[point]

    fig, ax = plt.subplots(figsize=(5.2, 3.0))
    kept = valid & ~reject
    rej = valid & reject
    ax.bar(idx[kept], counts[kept], color="C0", label="kept passes")
    if np.any(rej):
        ax.bar(idx[rej], counts[rej], color="red", label="rejected (cosmic ray)")
    if np.any(kept):
        ax.axhline(float(np.mean(counts[kept])), color="0.4", ls="--", lw=1, label="kept mean")
    ax.set_xlabel("miniscan pass")
    ax.set_ylabel("counts")
    ax.set_title(f"scan {record.scan_number}  {det_id}  point {point}  (E={energy:.3f} meV)",
                 fontsize=9)
    ax.legend(fontsize=7)
    fig.tight_layout()
    return _png_bytes(fig)


def merged_figure(merged) -> bytes:
    """The combine-before-divide result: summed raw counts vs energy (±√ΣN), masked points shown."""
    energy = np.asarray(merged.energy, dtype="float64")
    intensity = np.asarray(merged.intensity, dtype="float64")
    yerr = np.sqrt(np.clip(np.asarray(merged.variance, dtype="float64"), 0.0, None))
    mask = np.asarray(merged.mask, dtype=bool)

    fig, ax = plt.subplots(figsize=(6.0, 3.4))
    ax.errorbar(energy[mask], intensity[mask], yerr=yerr[mask], fmt="o-", ms=3, lw=1, color="C2",
                label=f"merged ΣN ({merged.n_members} scans)")
    if np.any(~mask):
        ax.scatter(energy[~mask], intensity[~mask], marker="x", color="red", zorder=5,
                   label="masked (shown, not deleted)")
    ax.set_xlabel("energy transfer (meV)")
    ax.set_ylabel("counts (raw ΣN; Poisson √N)")
    ax.set_title(f"merged spectrum — {merged.n_members} scans combined before divide", fontsize=9)
    ax.legend(fontsize=7, framealpha=0.9)
    fig.tight_layout()
    return _png_bytes(fig)


def coverage_figure(collation) -> bytes:
    """Which Q points were measured along the dominant-varying component, with gaps shaded red."""
    cov = collation.coverage
    vals = np.asarray(cov.values, dtype="float64")
    fig, ax = plt.subplots(figsize=(6.0, 1.8))
    if len(vals):
        ax.scatter(vals, np.zeros_like(vals), marker="|", s=240, color="C0")
    for lo, hi in cov.gaps:
        ax.axvspan(lo, hi, color="red", alpha=0.2)
    ax.set_yticks([])
    ax.set_xlabel(f"Q {cov.component} (rlu) — measured points; red span = coverage gap")
    fig.tight_layout()
    return _png_bytes(fig)


def report_figures(records, collation) -> dict:
    """The standard PNG set for a report, keyed by the shared figure ids: the coverage map, one
    spectrum per measurement analyzer, and a per-pass miniscan figure for each cosmic-ray point."""
    figs = {_model.FIG_COVERAGE: coverage_figure(collation)}
    for rec in records:
        if rec.scan_type != "measurement":
            continue
        for det_id, sp in rec.spectra.items():
            figs[_model.fig_spectrum(rec.scan_number, det_id)] = spectrum_figure(rec, det_id)
            for i in _rejected_points(sp):
                figs[_model.fig_miniscan(rec.scan_number, det_id, int(i))] = \
                    miniscan_figure(rec, det_id, int(i))
    if getattr(collation, "merge", "separate") == "merge":
        for i, g in enumerate(gg for gg in collation.groups if gg.merged is not None):
            figs[_model.fig_merged(i)] = merged_figure(g.merged)
    return figs

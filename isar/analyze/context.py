"""ScanContext — the immutable per-fit context for one scan/detector.

Built from a Phase-2 ``ScanRecord`` + experiment ``Card`` (plus the per-scan resolution,
measured from the elastic line for IXS or Cooper-Nathans for TAS). It carries the QC mask
but **never applies it** (P5 / §0.4): the loop fits ``x_used`` only; the dropped points
remain in the record. Components read ``gauss_fwhm/lorentz_fwhm/center/T``; the structure
gate reads ``energy_status`` and ``window``.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from isar.schema import axes
from isar.schema.steering import ParamConstraints


@dataclass(frozen=True)
class ExposureSelection:
    """The single usable normalization vector selected for one spectrum."""

    absolute: np.ndarray | None
    scale: np.ndarray | None
    source: str | None
    reference: float | None


def normalized_exposure(exposure, mask=None) -> tuple[np.ndarray | None, float | None]:
    """Return ``(relative exposure, positive kept median)`` without altering observed data.

    Invalid exposure remains represented as zero in the relative vector; callers must mask those
    points.  ``None`` means that no usable absolute exposure was supplied and selects the legacy
    uniform-mean path.
    """
    values = np.asarray(exposure, dtype=float)
    keep = np.ones(values.shape, dtype=bool) if mask is None else np.asarray(mask, dtype=bool)
    valid = keep & np.isfinite(values) & (values > 0)
    if not valid.any():
        return None, None
    reference = float(np.median(values[valid]))
    scale = np.zeros(values.shape, dtype=float)
    scale[np.isfinite(values) & (values > 0)] = values[np.isfinite(values) & (values > 0)] / reference
    return scale, reference


def select_exposure(normalization, shape, mask=None) -> ExposureSelection:
    """Select monitor, then time, only when it can normalize the requested spectrum.

    A malformed or wholly unusable monitor must not suppress a valid time vector.  The returned
    absolute vector is the same vector whose relative scale is consumed by fitting, so diagnostics
    cannot silently report a different exposure source.
    """
    expected = tuple(shape)
    if normalization is None:
        return ExposureSelection(None, None, None, None)
    for source in ("monitor", "time"):
        candidate = getattr(normalization, source, None)
        if candidate is None:
            continue
        try:
            absolute = np.asarray(candidate, dtype=float)
        except (TypeError, ValueError):
            continue
        if absolute.shape != expected:
            continue
        try:
            scale, reference = normalized_exposure(absolute, mask)
        except (TypeError, ValueError):
            continue
        if scale is not None:
            return ExposureSelection(absolute, scale, source, reference)
    return ExposureSelection(None, None, None, None)


def effective_exposure(x, exposure, mask, *, target_center: float,
                       resolution_fwhm: float, submitted_ncount: int | None = None) -> dict:
    """Deterministic peak-local absolute exposure used by fitting diagnostics and steering.

    The local half-width is one resolution FWHM, never smaller than the median kept energy spacing.
    A kept-window median is an explicit fallback; submitted ncount is used only when no monitor/time
    vector exists.  The returned provenance makes those cases distinguishable during replay.
    """
    energy = np.asarray(x, dtype=float)
    keep = np.asarray(mask, dtype=bool)
    finite_x = keep & np.isfinite(energy)
    spacing = 0.0
    ordered = np.sort(energy[finite_x])
    if ordered.size > 1:
        diffs = np.diff(ordered)
        diffs = diffs[np.isfinite(diffs) & (diffs > 0)]
        if diffs.size:
            spacing = float(np.median(diffs))
    half_width = max(float(resolution_fwhm), spacing, 1e-9)
    if exposure is not None:
        values = np.asarray(exposure, dtype=float)
        if values.shape != energy.shape:
            raise ValueError("absolute exposure must have the same shape as x")
        valid = keep & np.isfinite(values) & (values > 0)
        local = valid & (np.abs(energy - float(target_center)) <= half_width)
        if local.any():
            return {"value": float(np.median(values[local])), "source": "target_window",
                    "fallback": False, "center": float(target_center), "half_width": half_width}
        if valid.any():
            return {"value": float(np.median(values[valid])), "source": "kept_window",
                    "fallback": True, "center": float(target_center), "half_width": half_width}
    if submitted_ncount is not None and int(submitted_ncount) > 0:
        return {"value": float(submitted_ncount), "source": "submitted_ncount",
                "fallback": True, "center": float(target_center), "half_width": half_width}
    return {"value": None, "source": "unavailable", "fallback": True,
            "center": float(target_center), "half_width": half_width}


@dataclass(frozen=True)
class ScanContext:
    x: np.ndarray            # energy transfer (meV), all points
    counts: np.ndarray       # raw counts, all points
    mask: np.ndarray         # bool, True = keep (honored, never applied)
    T: float                 # temperature (K)
    center: float            # elastic-line / energy-zero position (meV)
    gauss_fwhm: float        # Voigt resolution — Gaussian FWHM (meV)
    lorentz_fwhm: float      # Voigt resolution — Lorentzian FWHM (meV)
    instrument: str          # "S30" (IXS) | "IN8" (TAS) …
    energy_status: str       # "absolute" | "needs_zero" | "needs_conversion_and_zero"
    prior_omega: float | None = None  # running dispersion-prior ω(Q), if any (None at step 1)
    acoustic_omega: float | None = None  # branch-assignment acoustic ω₀(q) hint (Phase A): which mode
    #                                     of a multi-mode fit is the acoustic QoI. None ⇒ fall back to
    #                                     prior_omega / largest-amplitude (branch-agnostic, unchanged).
    quasielastic_fwhm: float | None = None  # broad quasi-elastic Lorentzian FWHM (meV); None ⇒ no QE
    #                                         component admissible (§8 TAS card default; unset on IXS)
    constraints: ParamConstraints | None = None  # fit-time soft/hard param constraints (Phase 2+;
    #                                              None ⇒ unconstrained. Nothing reads it yet.)
    exposure_scale: np.ndarray | None = None  # relative per-point exposure.  It multiplies only the
    # model mean; observed counts and their Poisson variance are never rescaled.  None is the legacy
    # uniform-exposure path and deliberately avoids an extra multiply in model evaluation.
    exposure_reference: float | None = None  # the ABSOLUTE exposure ``exposure_scale`` is relative
    # to -- exactly the reference :func:`normalized_exposure` returned for this fit (median kept
    # monitor).  A fitted amplitude therefore means "counts per ``exposure_reference``", which is
    # the amplitude basis a predictive artifact must publish.  It is carried, never recomputed: the
    # median is a rank statistic, so recomputing it on a different kept set can shift the declared
    # basis (EIG-0 §4.5).  None wherever ``exposure_scale`` is None.

    @property
    def x_used(self) -> np.ndarray:
        return np.asarray(self.x, dtype=float)[np.asarray(self.mask, dtype=bool)]

    @property
    def counts_used(self) -> np.ndarray:
        return np.asarray(self.counts, dtype=float)[np.asarray(self.mask, dtype=bool)]

    @property
    def exposure_used(self) -> np.ndarray | None:
        if self.exposure_scale is None:
            return None
        return np.asarray(self.exposure_scale, dtype=float)[np.asarray(self.mask, dtype=bool)]

    def exposure_for(self, x) -> np.ndarray | None:
        """Relative exposure aligned to ``x``; ``None`` preserves the exact legacy path.

        Fit callers use either the full or masked acquisition grid.  Plot/reconstruction callers may
        request a denser grid, for which linear interpolation is the least-surprising representation
        of the recorded monitor profile.  This affects only the predicted raw-count mean.
        """
        if self.exposure_scale is None:
            return None
        target = np.asarray(x, dtype=float)
        full_x = np.asarray(self.x, dtype=float)
        scale = np.asarray(self.exposure_scale, dtype=float)
        if scale.shape != full_x.shape:
            raise ValueError("exposure_scale must have the same shape as x")
        if target.shape == full_x.shape and np.array_equal(target, full_x):
            return scale
        used_x = self.x_used
        if target.shape == used_x.shape and np.array_equal(target, used_x):
            return self.exposure_used
        order = np.argsort(full_x)
        return np.interp(target, full_x[order], scale[order], left=scale[order][0],
                         right=scale[order][-1])

    @property
    def window(self) -> tuple[float, float]:
        xu = self.x_used
        return (float(xu.min()), float(xu.max()))

    @classmethod
    def from_scan(cls, record, detector_id, card, *, center, gauss_fwhm, lorentz_fwhm,
                  prior_omega=None) -> "ScanContext":
        """Extract a context from a ScanRecord's detector spectrum + the card.

        ``center``/``gauss_fwhm``/``lorentz_fwhm`` are the per-scan resolution (measured
        from the elastic line for IXS; Cooper-Nathans for TAS) supplied by the caller.
        """
        energy = axes.scanned_axes(record)[0].values
        sp = record.spectra[detector_id]
        exposure_scale = None
        norm = getattr(record, "normalization", None)
        # Detector expectation follows incident exposure.  Prefer a usable recorded monitor even on
        # time-counted instruments (where it captures flux drift); seconds are the deterministic
        # fallback when monitor is absent or malformed.
        selected = select_exposure(norm, np.asarray(energy).shape, sp.mask)
        exposure_scale = selected.scale
        T = float(np.median(np.asarray(record.temperature.setpoint, dtype=float)))
        return cls(
            x=np.asarray(energy, dtype=float),
            counts=np.asarray(sp.intensity, dtype=float),
            mask=(np.asarray(sp.mask, dtype=bool) if exposure_scale is None else
                  np.asarray(sp.mask, dtype=bool) & (exposure_scale > 0)),
            T=T,
            center=float(center),
            gauss_fwhm=float(gauss_fwhm),
            lorentz_fwhm=float(lorentz_fwhm),
            instrument=card.policy.instrument,
            energy_status=record.energy_calibration.status,
            prior_omega=prior_omega,
            quasielastic_fwhm=card.policy.quasielastic_fwhm,
            exposure_scale=exposure_scale,
            exposure_reference=selected.reference,
        )

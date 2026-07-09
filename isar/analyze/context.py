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

    @property
    def x_used(self) -> np.ndarray:
        return np.asarray(self.x, dtype=float)[np.asarray(self.mask, dtype=bool)]

    @property
    def counts_used(self) -> np.ndarray:
        return np.asarray(self.counts, dtype=float)[np.asarray(self.mask, dtype=bool)]

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
        T = float(np.median(np.asarray(record.temperature.setpoint, dtype=float)))
        return cls(
            x=np.asarray(energy, dtype=float),
            counts=np.asarray(sp.intensity, dtype=float),
            mask=np.asarray(sp.mask, dtype=bool),
            T=T,
            center=float(center),
            gauss_fwhm=float(gauss_fwhm),
            lorentz_fwhm=float(lorentz_fwhm),
            instrument=card.policy.instrument,
            energy_status=record.energy_calibration.status,
            prior_omega=prior_omega,
            quasielastic_fwhm=card.policy.quasielastic_fwhm,
        )

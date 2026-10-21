"""Tabulated resolution curves: a measured energy-resolution function per detector and setting.

Shared and instrument-agnostic (any card can switch it on): an instrument's reader turns its own
resolution files into :class:`TabulatedResolution` objects and collects them in a
:class:`ResolutionLibrary` keyed by ``(detector_id, setting)`` (BL43LXU: analyzer and slit). The
fit seam (:func:`isar.analyze.lineshapes.convolute_voigt_dho` and the components) convolves with
the curve in place of the Voigt profile when the fit context carries one.

The table is used as given: normalised to unit area on its own grid, linearly interpolated, and
zero outside its tabulated range. No tail is invented; :meth:`TabulatedResolution.covers` tells a
caller whether a needed range lies inside the table. Qt-free, scipy-free.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass(frozen=True, eq=False)
class TabulatedResolution:
    """One measured resolution curve. ``energy`` (meV, strictly ascending) and ``values`` are the
    table; ``values`` is rescaled to unit area (trapezoid on ``energy``) at construction.

    Identity-hashed (``eq=False``) so a curve can key a per-fit cache without hashing its arrays.
    """

    energy: np.ndarray = field(repr=False)
    values: np.ndarray = field(repr=False)
    detector_id: str
    setting: str | None = None
    source: str = ""
    facts: dict = field(default_factory=dict, repr=False)   # what the instrument's own files say of it
    fwhm: float = field(init=False)

    def __post_init__(self):
        e = np.array(self.energy, dtype=float)
        v = np.array(self.values, dtype=float)
        if e.ndim != 1 or e.shape != v.shape or e.size < 3:
            raise ValueError(f"resolution table for {self.detector_id!r} needs matching 1-D "
                             f"energy and value columns of at least 3 points")
        if not (np.all(np.isfinite(e)) and np.all(np.isfinite(v))):
            raise ValueError(f"resolution table for {self.detector_id!r} has non-finite entries")
        if np.any(np.diff(e) <= 0):
            raise ValueError(f"resolution table for {self.detector_id!r} energy is not ascending")
        area = float(np.trapezoid(v, e))
        if not area > 0:
            raise ValueError(f"resolution table for {self.detector_id!r} has no positive area")
        v = v / area
        e.flags.writeable = False
        v.flags.writeable = False
        object.__setattr__(self, "energy", e)
        object.__setattr__(self, "values", v)
        object.__setattr__(self, "fwhm", _fwhm(e, v, self.detector_id))
        object.__setattr__(self, "_kernels", {})

    @property
    def range(self) -> tuple[float, float]:
        return float(self.energy[0]), float(self.energy[-1])

    def profile(self, x) -> np.ndarray:
        """Unit-area curve at ``x`` (meV): linear interpolation, zero outside the table."""
        return np.interp(np.asarray(x, dtype=float), self.energy, self.values,
                         left=0.0, right=0.0)

    def covers(self, lo: float, hi: float) -> bool:
        """Whether ``[lo, hi]`` lies inside the tabulated range (no extrapolated tail needed)."""
        e0, e1 = self.range
        return e0 <= float(lo) and float(hi) <= e1

    def kernel(self, dx: float) -> tuple[np.ndarray, int]:
        """The curve on nodes ``k*dx`` inside the table's own range, renormalised so that
        ``sum(kernel) * dx == 1``, and the index of the ``k = 0`` node (the curve's zero energy).

        The kernel is never padded beyond the table, and its centre is explicit, so a convolution
        that honours the returned index has no window-dependent centring offset. Cached per ``dx``.
        """
        dx = float(dx)
        cached = self._kernels.get(dx)
        if cached is not None:
            return cached
        e0, e1 = self.range
        # The 1e-9 guard keeps an endpoint that is a dx multiple up to float rounding.
        k_lo = int(np.ceil(e0 / dx - 1e-9))
        k_hi = int(np.floor(e1 / dx + 1e-9))
        if not (k_lo <= 0 <= k_hi):
            raise ValueError(f"resolution table for {self.detector_id!r} does not contain 0 meV")
        nodes = np.clip(np.arange(k_lo, k_hi + 1) * dx, e0, e1)
        kern = self.profile(nodes)
        total = float(kern.sum()) * dx
        if not total > 0:
            raise ValueError(f"resolution table for {self.detector_id!r} vanishes on a {dx} meV grid")
        kern = kern / total
        kern.flags.writeable = False
        out = (kern, -k_lo)
        self._kernels[dx] = out
        return out


def _fwhm(energy: np.ndarray, values: np.ndarray, label: str) -> float:
    """Full width at half maximum, each crossing found separately by linear interpolation
    between the samples bounding the contiguous above-half region around the maximum."""
    i_max = int(np.argmax(values))
    half = 0.5 * float(values[i_max])
    below_left = np.nonzero(values[:i_max] < half)[0]
    below_right = np.nonzero(values[i_max + 1:] < half)[0]
    if below_left.size == 0 or below_right.size == 0:
        raise ValueError(f"resolution table for {label!r} does not fall to half maximum "
                         f"on both sides")
    i = int(below_left[-1])           # values[i] < half <= values[i + 1]
    j = i_max + 1 + int(below_right[0])  # values[j - 1] >= half > values[j]
    left = energy[i] + (half - values[i]) * (energy[i + 1] - energy[i]) / (values[i + 1] - values[i])
    right = energy[j - 1] + (values[j - 1] - half) * (energy[j] - energy[j - 1]) / (
        values[j - 1] - values[j])
    return float(right - left)


class ResolutionLibrary:
    """The available curves, keyed by ``(detector_id, setting)``. A second curve for a key
    already present is refused by name, never silently replaced."""

    def __init__(self, curves=()):
        self._curves: dict[tuple[str, str | None], TabulatedResolution] = {}
        for curve in curves:
            key = (curve.detector_id, curve.setting)
            if key in self._curves:
                raise ValueError(f"two resolution curves for detector {key[0]!r} at setting "
                                 f"{key[1]!r}: {self._curves[key].source!r} and {curve.source!r}")
            self._curves[key] = curve

    def __len__(self) -> int:
        return len(self._curves)

    def settings(self) -> list[str | None]:
        """Every setting with at least one curve, sorted (``None`` first)."""
        return sorted({s for _, s in self._curves}, key=lambda s: (s is not None, s or ""))

    def get(self, detector_id: str, setting: str | None) -> TabulatedResolution | None:
        """The curve for this detector at this setting, or ``None`` when the library has none."""
        return self._curves.get((detector_id, setting))

"""Qt-free spectrum DTOs shared by the Scan(s) page and fit inspector."""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np

from .scan_list import PooledRow, SpectrumRow, Status

Scale = Literal["raw", "normalised"]
PassName = Literal["fast", "deep"]
Role = Literal["mode", "elastic", "quasielastic", "background"]

FINE_POINTS = 400
CACHE_SIZE = 64

NOT_PARSED = "not_parsed"
POOLED = "pooled"
NO_ENERGY_ZERO = "no_energy_zero"
NO_MONITOR = "no_monitor"
MONITOR_NOT_STORED = "monitor_not_stored"
NO_PASS = "no_pass"
CURVE_UNAVAILABLE = "curve_unavailable"
DEEP_DOES_NOT_REBUILD = "deep_does_not_rebuild"

OUTDATED_TEXT = "outdated: values changed since fit"
NOT_JUDGED_TEXT = "currentness not recorded"
ENERGY_HEADING = "Energy E₀ (meV, undamped DHO energy)"
LINEWIDTH_HEADING = "Linewidth Γ (meV, DHO damping, HWHM)"
AMPLITUDE_HEADING = "Amplitude h (counts per point at the spectrum's reference exposure)"


@dataclass(frozen=True)
class NotDrawn:
    code: str
    text: str


@dataclass(frozen=True)
class Measured:
    value: float | None
    lo: float | None
    hi: float | None
    kind: str | None


@dataclass(frozen=True)
class ModeRow:
    number: int
    prefix: str
    energy: Measured
    linewidth: Measured
    amplitude: Measured


@dataclass(frozen=True)
class PassSummary:
    pass_name: PassName
    present: bool
    state: str
    state_reason: str | None
    label: str | None
    trusted: bool | None
    trust_reason: str | None
    current: bool | None
    outdated_text: str | None
    unjudged_text: str | None
    changed: tuple[tuple[str, str, str], ...]
    modes: tuple[ModeRow, ...]


@dataclass(frozen=True)
class CurveComponent:
    role: Role
    mode_number: int | None
    name: str
    prefix: str
    values: np.ndarray


@dataclass(frozen=True)
class PassCurves:
    total: np.ndarray
    components: tuple[CurveComponent, ...]
    residuals: np.ndarray


@dataclass(frozen=True)
class ScaledSpectrum:
    key: str
    scale: Scale
    R: float | None
    y_unit: str
    not_drawn: NotDrawn | None
    x: np.ndarray
    y: np.ndarray
    yerr: np.ndarray
    kept: np.ndarray
    fine_x: np.ndarray
    passes: Mapping[PassName, PassCurves | NotDrawn]


@dataclass(frozen=True)
class SpectrumBundle:
    key: str
    revision: str
    not_drawn: NotDrawn | None
    summaries: Mapping[PassName, PassSummary]
    energy: np.ndarray
    counts: np.ndarray
    variance: np.ndarray
    kept: np.ndarray
    monitor: np.ndarray | None
    exposure_stored: bool
    exposure_source: str | None
    exposure_scale: np.ndarray | None
    m_ref: float | None
    fine_energy: np.ndarray

    def scaled(self, scale: Scale, R: float | None = None) -> ScaledSpectrum:
        raise NotImplementedError("spectrum scaling is provided by the Scan(s) read unit")


class SpectraCache:
    """Bounded cache shell; S1U2 supplies bundle lookup and revision eviction."""

    def __init__(self, maxsize: int = CACHE_SIZE):
        self.maxsize = maxsize
        self._cache: OrderedDict[str, SpectrumBundle] = OrderedDict()


def spectrum_bundles(ws, rows: Sequence[SpectrumRow | PooledRow], *,
                     cache: SpectraCache | None = None) -> tuple[SpectrumBundle, ...]:
    raise NotImplementedError("spectrum reads are provided by the Scan(s) read unit")


def normalisation_reference(ws, keys: Iterable[str]) -> float | None:
    raise NotImplementedError("normalisation reads are provided by the Scan(s) read unit")


def nearest_power_of_ten(value: float) -> float:
    raise NotImplementedError("normalisation helpers are provided by the Scan(s) read unit")


def normalised_points(counts, variance, monitor, R: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(drawable, y, yerr); U2 implements the stored-monitor calculation."""
    raise NotImplementedError("normalisation helpers are provided by the Scan(s) read unit")

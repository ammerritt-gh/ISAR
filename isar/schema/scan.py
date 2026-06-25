"""The normalized scan record (§5) — one `ScanRecord` per physical scan, holding N spectra.

A `ScanRecord` is shared scan metadata + a signal block of N per-detector spectra + QC, addressed
by the identity key `(experiment_id, scan_number, detector_id)` (§3). It is the canonical store
(D1, §5.7): one JSON file per physical scan, arrays inline, NeXus-aligned field names.

Decisions baked in here: **no zero-count variance floor** (D3 — variance is stored honestly, 0 for
0 counts, convention recorded as `VARIANCE_CONVENTION`); **both monitor and time always stored**
regardless of counting mode (§5.3); the three-state energy-calibration `status` replacing the old
`energy_absolute` bool (§5.3, DESIGN §8 refinement).
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field

import numpy as np

from isar.schema import _json
from isar.schema.axes import ScanAxis  # noqa: F401  (used in ScanRecord type hints)

SCHEMA_VERSION = "isar.scan/1"
VARIANCE_CONVENTION = "none"  # D3: no zero-count floor

# Controlled vocabularies (bare strings — Phase 1 idiom keeps (de)serialization trivial).
SCAN_TYPES = ("measurement", "alignment", "calibration", "environment")
SCAN_TYPE_SOURCES = ("declared", "inferred")
COUNTING_MODES = ("monitor", "time")
ENERGY_STATUSES = ("absolute", "needs_zero", "needs_conversion_and_zero")
RAW_ABSCISSA_KINDS = ("energy", "temperature", "angle")


# ---------------------------------------------------------------------------
# Flags
# ---------------------------------------------------------------------------
@dataclass
class Flag:
    type: str
    severity: str
    reason: str


@dataclass
class PointFlag:
    index: int
    type: str
    reason: str


# ---------------------------------------------------------------------------
# Per-spectrum signal block (§5.3)
# ---------------------------------------------------------------------------
@dataclass
class Spectrum:
    detector_id: str
    q: list[float]                     # this spectrum's actual Q (D2: from #V4-#V12 where logged)
    q_source: str                      # "reported" | "card_offset"
    intensity: np.ndarray              # the signal: RAW COUNTS (combined, post-rejection); monitor
                                       # is stored, not divided in — normalize downstream if you wish
    variance: np.ndarray               # honest Poisson variance = counts (σ=√N), 0 for 0 counts (D3)
    mask: np.ndarray                   # True=keep; NEVER applied to intensity (§0.4)
    point_flags: list[PointFlag]
    spectrum_flags: list[Flag]
    resolution: dict | None = None     # per-spectrum; None at reduction, set in Phase 3
    # Raw-S30 miniscan ensemble (None unless ingested from raw .spec): the full per-pass counts
    # kept so cosmic-ray rejection is reversible (§0.4) — we mark passes, never delete them.
    miniscan_counts: np.ndarray | None = None      # [nP, n_mini] per-pass raw counts
    miniscan_reject: np.ndarray | None = None      # [nP, n_mini] bool, True=rejected pass
    miniscan_reject_source: str | None = None      # "sigma_clip" | "poisson"

    _ARRAY_FIELDS = {
        "intensity": "float64", "variance": "float64", "mask": "bool",
        "miniscan_counts": "float64", "miniscan_reject": "bool",
    }


# ---------------------------------------------------------------------------
# Normalization, temperature, environment, geometry, calibration (§5.3-5.4)
# ---------------------------------------------------------------------------
@dataclass
class Normalization:
    counting_mode: str                 # "monitor" | "time"
    monitor: np.ndarray                # both always stored regardless of mode (§5.3); kept-sum for raw
    time: np.ndarray
    preset: float
    monitor_correction: bool = False
    # Raw-S30 per-miniscan monitor/time, shared across the 9 analyzers (ICO-C is one column). The
    # reduced monitor[nP]/time[nP] above are the kept-sum denominators (combine-before-divide, §7.5).
    miniscan_monitor: np.ndarray | None = None     # [nP, n_mini]
    miniscan_time: np.ndarray | None = None        # [nP, n_mini]

    _ARRAY_FIELDS = {
        "monitor": "float64", "time": "float64",
        "miniscan_monitor": "float64", "miniscan_time": "float64",
    }


@dataclass
class Sensor:
    name: str
    values: np.ndarray
    valid_mask: np.ndarray
    role: str = "monitored"

    _ARRAY_FIELDS = {"values": "float64", "valid_mask": "bool"}


@dataclass
class Temperature:
    setpoint: np.ndarray
    sensors: dict[str, Sensor]
    valid_mask: np.ndarray
    snap_applied: bool = False

    _ARRAY_FIELDS = {"setpoint": "float64", "valid_mask": "bool"}


@dataclass
class EnvField:
    name: str
    setpoint: np.ndarray
    sensors: dict[str, Sensor]
    valid_mask: np.ndarray

    _ARRAY_FIELDS = {"setpoint": "float64", "valid_mask": "bool"}


@dataclass
class EnvLog:
    name: str
    values: np.ndarray
    units: str
    setpoint: float | None
    sensor_id: str | None
    role: str
    valid_mask: np.ndarray

    _ARRAY_FIELDS = {"values": "float64", "valid_mask": "bool"}


@dataclass
class Environment:
    field: EnvField | None = None
    logs: dict[str, EnvLog] = dc_field(default_factory=dict)


@dataclass
class Geometry:
    fixed_mode: str | None = None      # "ki" | "kf"
    fixed_value: float | None = None
    mono: dict | None = None
    ana: dict | None = None
    collimation: list | None = None
    filter: str | None = None


@dataclass
class EnergyCalibration:
    raw_abscissa_kind: str             # "energy" | "temperature" | "angle"
    transform: dict | None
    elastic_zero_offset: float | None  # null at reduction; the zero-fit is Phase 3
    status: str                        # "absolute" | "needs_zero" | "needs_conversion_and_zero"


@dataclass
class Source:
    file: str
    byte_range: list | None
    line_range: list | None
    plugin: str
    plugin_version: str
    format_version: str
    schema_version: str


# ---------------------------------------------------------------------------
# The record (§5.1)
# ---------------------------------------------------------------------------
@dataclass
class ScanRecord:
    experiment_id: str
    scan_number: int
    scan_type: str
    scan_type_source: str
    scan_axes: list[ScanAxis]
    spectra: dict[str, Spectrum]       # keyed by detector_id → the (exp,scan,det) identity slice
    normalization: Normalization
    temperature: Temperature
    environment: Environment
    geometry: Geometry | None
    energy_calibration: EnergyCalibration
    scan_flags: list[Flag]
    raw_header: dict
    source: Source

    def to_dict(self) -> dict:
        return _json.to_jsonable(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ScanRecord":
        return _json.from_dataclass(cls, d)

    def spectrum_key(self, detector_id: str) -> tuple[str, int, str]:
        """The §3 composite identity key selecting one spectrum out of this record."""
        return (self.experiment_id, self.scan_number, detector_id)


def records_close(a: ScanRecord, b: ScanRecord, *, rtol: float = 1e-7, atol: float = 1e-9) -> bool:
    """Array-aware deep equality (the comparison every JSON/round-trip test uses, §5.7)."""
    return _json.objects_close(a, b, rtol=rtol, atol=atol)

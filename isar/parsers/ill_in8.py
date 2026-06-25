"""IN8 (ILL TAS) parser — a thin card-driven layer over the shared numor reader (§6).

IN8 is a single-detector thermal triple-axis spectrometer. A `bs qh` numor is a constant-Q energy
scan (one `det0` measurement: scanned EN, constant Q from the command); a `sc a3` numor is a motor
alignment scan (one scanned angle axis). The stored signal is RAW COUNTS (`CNTS`); the monitor (`M1`)
is stored, not divided in — flux normalization stays a reversible downstream choice (raw counts state
the Poisson error plainly). Identity is keyed on `EXPNO` (shared across the separate numor files) so
collation groups scans across files; the numor's `FILE_` is the scan_number.

Fixed-kf geometry makes the energy axis absolute (no elastic-zero fit here — that is Phase 3). The
temperature setpoint is the header `RT` and the sample sensor is the `TT` column, so the QC
sensor-divergence check catches the disconnected sensor (055177: TT≈299 vs RT=221.57).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from isar.parsers import numor_base, registry
from isar.schema import axes, scan
from isar.schema.card import Card

name = "ill_in8"
version = "0.1.0"

_CARD_PATH = Path(__file__).parent / "cards" / "in8.policy.toml"


def card_template() -> Card:
    """The bundled IN8 card (policy from the shipped TOML; fresh, empty state)."""
    return Card.load(_CARD_PATH)


def matches(path: str | Path) -> bool:
    try:
        return registry.detect(path)[:2] == ("ILL", "IN8")
    except (ValueError, OSError):
        return False


# ---------------------------------------------------------------------------
# COMND parsing
# ---------------------------------------------------------------------------
def _counting(comnd: str):
    """(counting_mode, preset) from the COMND `mn <M>` (monitor) or `ti <T>` (time) token."""
    toks = comnd.split()
    if "mn" in toks:
        return "monitor", float(toks[toks.index("mn") + 1])
    if "ti" in toks:
        return "time", float(toks[toks.index("ti") + 1])
    return "monitor", 0.0


def _bs_q_and_deltas(comnd: str):
    """For `bs qh h k l E dqh dh dk dl dE …`: return ((h,k,l), [dh,dk,dl,dE] or None)."""
    toks = comnd.split()
    q = (float(toks[2]), float(toks[3]), float(toks[4]))
    dtok = "d" + toks[1]                         # "qh" -> "dqh"
    deltas = None
    if dtok in toks:
        j = toks.index(dtok)
        deltas = [float(toks[j + k]) for k in range(1, 5)]
    return q, deltas


# ---------------------------------------------------------------------------
# Shared record pieces
# ---------------------------------------------------------------------------
def _col(block, nm):
    i = numor_base.column_index(block.column_labels, nm)
    return block.data[:, i] if i >= 0 else None


def _temperature(block, pol, nP) -> scan.Temperature:
    rule = pol.temperature_rule or {}
    sp = numor_base.param(block, rule.get("setpoint_param", "RT"))
    setpoint = np.full(nP, float(sp) if sp is not None else float("nan"))
    sensors = {}
    for nm in rule.get("sensor_columns", []):
        vals = _col(block, nm)
        if vals is not None:
            sensors[nm] = scan.Sensor(nm, np.asarray(vals, dtype="float64"),
                                      np.ones(nP, dtype=bool), "monitored")
    return scan.Temperature(setpoint, sensors, np.ones(nP, dtype=bool), False)


def _geometry(block) -> scan.Geometry:
    fx = numor_base.param(block, "FX")
    fixed_mode = {2.0: "kf", 1.0: "ki"}.get(fx)
    return scan.Geometry(
        fixed_mode=fixed_mode, fixed_value=numor_base.param(block, "KFIX"),
        mono={"d_spacing": numor_base.param(block, "DM")},
        ana={"d_spacing": numor_base.param(block, "DA")},
        collimation=None, filter=None)


def _raw_header(block) -> dict:
    return {"keywords": dict(block.keywords), "params": dict(block.params),
            "columns": list(block.column_labels)}


def _source(block, fname, pol) -> scan.Source:
    return scan.Source(
        file=fname, byte_range=None, line_range=list(block.line_range),
        plugin=name, plugin_version=version,
        format_version=pol.format_version, schema_version=scan.SCHEMA_VERSION)


def _spectrum(block, pol, counts, valid) -> dict:
    """One det0 spectrum of RAW COUNTS; monitor==0 points masked + flagged (kept, never deleted)."""
    counts = np.asarray(counts, dtype="float64")
    point_flags = [scan.PointFlag(int(i), "monitor_dropout", f"{pol.normalize_by}==0")
                   for i in np.nonzero(~valid)[0]]
    det = pol.detectors[0]
    return {det.detector_id: scan.Spectrum(
        detector_id=det.detector_id, q=[0.0, 0.0, 0.0], q_source="reported",
        intensity=counts, variance=counts.copy(), mask=valid.astype(bool),
        point_flags=point_flags, spectrum_flags=[], resolution=None)}


# ---------------------------------------------------------------------------
# Per-scan parsing
# ---------------------------------------------------------------------------
def _parse_measurement(block, pol, fname) -> scan.ScanRecord:
    comnd = block.keywords.get("COMND", "")
    (h, k, l), deltas = _bs_q_and_deltas(comnd)
    energy = _col(block, pol.energy_column)
    if energy is None or float(np.ptp(energy)) <= 1e-9:
        raise ValueError(f"{fname}: no varying energy axis — constant-energy Q-scan not yet supported")
    if deltas is not None and (abs(deltas[3]) <= 1e-9 or max(abs(d) for d in deltas[:3]) > 1e-6):
        raise ValueError(f"{fname}: Q is scanned (deltas={deltas}) — only constant-Q energy scans (§5.6)")
    nP = len(energy)

    monitor = _col(block, pol.monitor_column)
    monitor = np.asarray(monitor if monitor is not None else np.zeros(nP), dtype="float64")
    time = _col(block, pol.time_column)
    time = np.asarray(time if time is not None else np.zeros(nP), dtype="float64")
    denom = monitor if pol.normalize_by == "monitor" else time
    valid = denom != 0

    counts = _col(block, pol.detectors[0].signal_column)
    spectra = _spectrum(block, pol, counts, valid)
    spectra[pol.detectors[0].detector_id].q = [h, k, l]

    counting_mode, preset = _counting(comnd)
    scan_axes = [axes.energy_axis(energy), *axes.constant_q_axes(h, k, l, nP)]
    record = scan.ScanRecord(
        experiment_id=block.keywords.get("EXPNO", ""),
        scan_number=int(block.keywords.get("FILE_", "0")),
        scan_type="measurement", scan_type_source="inferred",
        scan_axes=scan_axes, spectra=spectra,
        normalization=scan.Normalization(
            counting_mode=counting_mode, monitor=monitor, time=time,
            preset=preset, monitor_correction=False),
        temperature=_temperature(block, pol, nP),
        environment=scan.Environment(field=None, logs={}),
        geometry=_geometry(block),
        energy_calibration=scan.EnergyCalibration(
            raw_abscissa_kind=pol.raw_abscissa_kind, transform=None,
            elastic_zero_offset=None, status=pol.energy_status),
        scan_flags=[], raw_header=_raw_header(block), source=_source(block, fname, pol))
    axes.assert_constant_q_energy(record)
    return record


def _parse_alignment(block, pol, fname) -> scan.ScanRecord:
    comnd = block.keywords.get("COMND", "")
    toks = comnd.split()
    motor_col = toks[1].upper() if len(toks) > 1 else ""      # "a3" -> "A3"
    motor_vals = _col(block, motor_col)
    if motor_vals is None:
        motor_col = block.column_labels[1] if len(block.column_labels) > 1 else "axis"
        motor_vals = _col(block, motor_col)
    nP = len(motor_vals)

    monitor = _col(block, pol.monitor_column)
    monitor = np.asarray(monitor if monitor is not None else np.zeros(nP), dtype="float64")
    time = _col(block, pol.time_column)
    time = np.asarray(time if time is not None else np.zeros(nP), dtype="float64")
    denom = monitor if pol.normalize_by == "monitor" else time
    valid = denom != 0

    counts = _col(block, pol.detectors[0].signal_column)
    spectra = _spectrum(block, pol, counts, valid)
    q = [numor_base.param(block, c) or 0.0 for c in ("QH", "QK", "QL")]
    spectra[pol.detectors[0].detector_id].q = [float(v) for v in q]

    counting_mode, preset = _counting(comnd)
    scanned = scan.ScanAxis(name=motor_col, kind="angle", role="scanned", units="",
                            values=np.asarray(motor_vals, dtype="float64"))
    return scan.ScanRecord(
        experiment_id=block.keywords.get("EXPNO", ""),
        scan_number=int(block.keywords.get("FILE_", "0")),
        scan_type="alignment", scan_type_source="inferred",
        scan_axes=[scanned], spectra=spectra,
        normalization=scan.Normalization(
            counting_mode=counting_mode, monitor=monitor, time=time,
            preset=preset, monitor_correction=False),
        temperature=_temperature(block, pol, nP),
        environment=scan.Environment(field=None, logs={}),
        geometry=_geometry(block),
        energy_calibration=scan.EnergyCalibration(
            raw_abscissa_kind="angle", transform=None, elastic_zero_offset=None,
            status="needs_conversion_and_zero"),
        scan_flags=[], raw_header=_raw_header(block), source=_source(block, fname, pol))


# ---------------------------------------------------------------------------
# Emit (the §4 inverse) — synthetic numor(s) for the round-trip gate
# ---------------------------------------------------------------------------
_BANNER = {c: c * 80 for c in "RAV"}
_MEAS_COLS = ["PNT", "QH", "QK", "QL", "EN", "M1", "M2", "TIME", "CNTS", "TT", "TRT"]


def emit_file(run, card: Card) -> bytes:
    """Serialize a synthetic run to numor bytes (one numor block per scan, concatenated).

    Byte-plausible ILL numor: the `RRRR/AAAA/VVVV` banners, the keyword header (`INSTR`, `EXPNO`,
    `FILE_`, `DATE_`, `COMND`, `POSQE`, the geometry `PARAM`s, and `TT/RT` temperature), then the
    `DATA_` column table. The parser inverts this exactly (Gate A). Duck-typed on `run`.
    """
    out: list = []
    for sc in run.scans:
        d = sc.detectors[0]
        h, k, l = (float(d.q[0]), float(d.q[1]), float(d.q[2]))
        nP = len(sc.energy)
        out += [
            _BANNER["R"],
            f"  {sc.scan_number}       2       0",
            "ILL TAS data in the new ASCII format follow after the line VV..V",
            _BANNER["A"],
            "      80",
            "IN8   synthetic   09-Sep-25 00:00:00",
            _BANNER["V"],
            "INSTR: IN8",
            f"EXPNO: {run.experiment_id}",
            "USER_: synthetic",
            "LOCAL: ISAR",
            f"FILE_: {sc.scan_number}",
            "DATE_: 09-Sep-25 00:00:00",
            "TITLE: synthetic IN8 run for ISAR round-trip validation",
            "TYPE_: tas",
            f"COMND: {sc.command}",
            f"POSQE: QH= {h:.4f}, QK= {k:.4f}, QL= {l:.4f}, EN= {float(sc.energy[0]):.4f}, UN=meV",
            "CURVE: MONO= auto, ANA= manu",
            "PARAM: DM=   3.35500, DA=   3.35500, KFIX=   2.66202",
            "PARAM: SM=  1, SS= -1, SA=  1, FX=  2",
            f"PARAM: TT= {float(sc.temperature_sensor):.2f}, RT= {float(sc.temperature_setpoint):.2f}, "
            f"MAG=      0.00",
            "FORMT:",
            "DATA_:",
            "  " + "  ".join(_MEAS_COLS),
        ]
        for p in range(nP):
            out.append("  ".join([
                str(p + 1), f"{h:.4f}", f"{k:.4f}", f"{l:.4f}", f"{float(sc.energy[p]):.6f}",
                str(int(round(float(sc.monitor[p])))), "50", f"{float(sc.time[p]):.4f}",
                str(int(round(float(d.counts[p])))),
                f"{float(sc.temperature_sensor):.3f}", f"{float(sc.temperature_setpoint):.3f}"]))
    return ("\n".join(out) + "\n").encode("utf-8")


def parse_file(path: str | Path, card: Card) -> list[scan.ScanRecord]:
    """Parse a numor file into one ScanRecord per numor block (real files: one; emitted: several)."""
    pol = card.policy
    fname = Path(path).name
    records: list[scan.ScanRecord] = []
    for block in numor_base.read_numor(path):
        comnd = block.keywords.get("COMND", "").strip()
        if comnd.startswith(pol.measurement_pattern):
            records.append(_parse_measurement(block, pol, fname))
        else:
            records.append(_parse_alignment(block, pol, fname))
    return records

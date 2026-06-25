"""S30 HERIX RAW parser — ingests the raw `.spec` with individual miniscans (§6, §0.4).

The supplementary parser (`spec_s30`) reads the beamline-cleaned file, where each energy point's
analyzer counts have already been combined with an irreversible cosmic-ray rejection. This parser
reads the *raw* file instead: each fscan energy point is a group of `miniscans_per_point` (=15)
consecutive repeated-energy acquisition passes. We keep **all** passes as a `[nP, n_mini]` ensemble
on each `Spectrum` so the rejection ISAR does later (`reduce.combine`) is controlled, tracked, and
reversible — we mark passes, never delete them (§0.4).

Parsing here is **parse + group only**: the ensemble is filled but no rejection/combination runs, so
the record is rule-independent and the reduce step can be re-run (or re-tuned) without re-parsing.
Until reduced, each spectrum's `intensity/variance` are zero, its `mask` all-False, and it carries
an `unreduced` flag. The emitter and the reduce step are added in later build steps.

ascans (motor scans the cleaned file discards) are captured too — classified alignment/calibration
by their scanned motor, with that motor recorded as the single scanned axis (§5.2).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from isar.parsers import registry, spec_base
from isar.parsers.spec_s30 import _meta_value, _normalize, _raw_header
from isar.schema import axes, scan
from isar.schema.card import Card
from isar.schema.scan import RAW_ABSCISSA_KINDS

name = "spec_s30_raw"
version = "0.1.0"

_CARD_PATH = Path(__file__).parent / "cards" / "s30_raw.policy.toml"


def card_template() -> Card:
    """The bundled S30 raw card (policy from the shipped TOML; fresh, empty state)."""
    return Card.load(_CARD_PATH)


def matches(path: str | Path) -> bool:
    try:
        fac, inst, ver = registry.detect(path)
        return (fac, inst) == ("APS", "S30") and ver.endswith("-raw")
    except (ValueError, OSError):
        return False


def _classify(command: str, card: Card) -> tuple[str, str]:
    """Return (scan_type, scanned_motor). fscan→measurement; ascan→its motor's class (§5.2)."""
    pol = card.policy
    toks = command.strip().split()
    if command.strip().startswith(pol.measurement_pattern):
        return "measurement", ""
    if command.strip().startswith(pol.alignment_pattern):
        motor = toks[1] if len(toks) > 1 else ""
        return pol.ascan_classification.get(motor, "alignment"), motor
    return "measurement", ""


# ---------------------------------------------------------------------------
# Miniscan grouping
# ---------------------------------------------------------------------------
_ENERGY_TOL = 1e-6   # adjacent passes share an energy to within this; trajectory points differ by ≫


def _group_rows(energy: np.ndarray) -> list[list[int]]:
    """Group consecutive rows sharing an energy value into one energy point's miniscan passes.

    HERIX fscans acquire a *variable* number of repeated-energy passes per point (not a fixed 15):
    validated against the beamline, runs of equal energy reproduce the supplementary's point count
    exactly on all 104 fscans, and the summed monitor matches. So grouping is by runs of equal
    energy — robust to variable pass counts (2..45 observed) and a truncated final group alike.
    """
    nrows = len(energy)
    groups, start = [], 0
    for i in range(1, nrows + 1):
        if i == nrows or abs(float(energy[i]) - float(energy[start])) > _ENERGY_TOL:
            groups.append(list(range(start, i)))
            start = i
    return groups


def _stack_groups(column: np.ndarray, groups: list[list[int]], width: int) -> np.ndarray:
    """Rectangular `[n_groups, width]` array; short (truncated) groups are NaN-padded."""
    out = np.full((len(groups), width), np.nan)
    for gi, grp in enumerate(groups):
        out[gi, : len(grp)] = column[grp]
    return out


# ---------------------------------------------------------------------------
# Shared record assembly
# ---------------------------------------------------------------------------
def _temperature(header, block, pol, nP) -> scan.Temperature:
    setpoint_field = pol.temperature_rule.get("setpoint_field", "")
    sp_val = _meta_value(header, block, setpoint_field)
    setpoint = np.full(nP, float(sp_val) if sp_val is not None else float("nan"))
    sensors = {}
    for sname in pol.temperature_rule.get("sensor_fields", []):
        v = _meta_value(header, block, sname)
        if v is not None:
            sensors[sname] = scan.Sensor(sname, np.full(nP, float(v)),
                                         np.ones(nP, dtype=bool), "monitored")
    return scan.Temperature(setpoint, sensors, np.ones(nP, dtype=bool), False)


def _reported_q(block, det):
    if det.value_index in block.V_groups:
        return list(block.V_groups[det.value_index]), "reported"
    return (list(det.q_offset) if det.q_offset else [float("nan")] * 3), "card_offset"


def _record(header, block, card, fname, *, scan_type, scan_axes, spectra, normalization,
            raw_abscissa_kind) -> scan.ScanRecord:
    pol = card.policy
    return scan.ScanRecord(
        experiment_id=fname,
        scan_number=block.scan_number,
        scan_type=scan_type,
        scan_type_source="inferred",
        scan_axes=scan_axes,
        spectra=spectra,
        normalization=normalization,
        temperature=_temperature(header, block, pol, len(scan_axes[0].values)),
        environment=scan.Environment(field=None, logs={}),
        geometry=None,
        energy_calibration=scan.EnergyCalibration(
            raw_abscissa_kind=raw_abscissa_kind, transform=None,
            elastic_zero_offset=None, status=pol.energy_status),
        scan_flags=[],
        raw_header=_raw_header(header, block),
        source=scan.Source(
            file=fname, byte_range=None, line_range=list(block.line_range),
            plugin=name, plugin_version=version,
            format_version=pol.format_version, schema_version=scan.SCHEMA_VERSION),
    )


# ---------------------------------------------------------------------------
# fscan (measurement): the miniscan ensemble, unreduced
# ---------------------------------------------------------------------------
def _parse_fscan(header, block, card, fname) -> scan.ScanRecord:
    pol = card.policy
    data = block.data

    def col(nm):
        return data[:, spec_base.column_index(block, nm)]

    energy_rows = col(pol.energy_column)
    groups = _group_rows(energy_rows)
    nP = len(groups)
    width = max(len(g) for g in groups)
    energy = np.array([energy_rows[g[0]] for g in groups], dtype="float64")

    mon_rows = col(pol.monitor_column)
    time_rows = col(pol.time_column) if pol.time_column else np.zeros(len(energy_rows))
    mini_monitor = _stack_groups(mon_rows, groups, width)
    mini_time = _stack_groups(time_rows, groups, width)

    spectra: dict[str, scan.Spectrum] = {}
    for det in pol.detectors:
        if not det.enabled:
            continue
        q, q_source = _reported_q(block, det)
        counts = _stack_groups(col(det.signal_column).astype("float64"), groups, width)
        spectra[det.detector_id] = scan.Spectrum(
            detector_id=det.detector_id, q=q, q_source=q_source,
            intensity=np.zeros(nP), variance=np.zeros(nP), mask=np.zeros(nP, dtype=bool),
            point_flags=[],
            spectrum_flags=[scan.Flag("unreduced", "info",
                                      "raw miniscans not yet combined (reduce.combine)")],
            miniscan_counts=counts, miniscan_reject=None, miniscan_reject_source=None,
        )

    q0 = block.Q if len(block.Q) == 3 else [0.0, 0.0, 0.0]
    scan_axes = [axes.energy_axis(energy), *axes.constant_q_axes(q0[0], q0[1], q0[2], nP)]
    normalization = scan.Normalization(
        counting_mode=pol.counting_mode,
        monitor=np.nansum(mini_monitor, axis=1), time=np.nansum(mini_time, axis=1),
        preset=block.T[0] if block.T else 0.0, monitor_correction=False,
        miniscan_monitor=mini_monitor, miniscan_time=mini_time)

    record = _record(header, block, card, fname, scan_type="measurement",
                     scan_axes=scan_axes, spectra=spectra, normalization=normalization,
                     raw_abscissa_kind=pol.raw_abscissa_kind)
    axes.assert_constant_q_energy(record)
    return record


# ---------------------------------------------------------------------------
# ascan (alignment / calibration): a single scanned motor axis, 1 row per point
# ---------------------------------------------------------------------------
def _parse_ascan(header, block, card, fname, scan_type) -> scan.ScanRecord:
    pol = card.policy
    data = block.data
    nP = data.shape[0]

    def col(nm):
        return data[:, spec_base.column_index(block, nm)]

    motor_label = block.column_labels[0]
    kind = "energy" if motor_label.lower().startswith("herixe") else "angle"
    scanned = scan.ScanAxis(name=motor_label, kind=kind, role="scanned", units="",
                            values=col(motor_label).astype("float64"))

    monitor = col(pol.monitor_column)
    time = col(pol.time_column) if pol.time_column else np.zeros(nP)
    denom = monitor if pol.normalize_by == "monitor" else (
        time if pol.normalize_by == "time" else np.ones(nP))

    spectra: dict[str, scan.Spectrum] = {}
    for det in pol.detectors:
        if not det.enabled:
            continue
        q, q_source = _reported_q(block, det)
        N = col(det.signal_column).astype("float64")
        intensity, variance, valid = _normalize(N, denom)
        point_flags = [scan.PointFlag(int(i), "monitor_dropout", f"{pol.normalize_by}==0")
                       for i in np.nonzero(~valid)[0]]
        spectra[det.detector_id] = scan.Spectrum(
            detector_id=det.detector_id, q=q, q_source=q_source,
            intensity=intensity, variance=variance, mask=valid.astype(bool),
            point_flags=point_flags, spectrum_flags=[], resolution=None)

    normalization = scan.Normalization(
        counting_mode=pol.counting_mode, monitor=monitor, time=time,
        preset=block.T[0] if block.T else 0.0, monitor_correction=False)
    raw_abscissa = kind if kind in RAW_ABSCISSA_KINDS else "angle"
    return _record(header, block, card, fname, scan_type=scan_type,
                   scan_axes=[scanned], spectra=spectra, normalization=normalization,
                   raw_abscissa_kind=raw_abscissa)


def parse_file(path: str | Path, card: Card) -> list[scan.ScanRecord]:
    pol = card.policy
    header, blocks = spec_base.read_spec(path, multiword_columns=pol.multiword_columns)
    fname = Path(path).name
    records: list[scan.ScanRecord] = []
    for block in blocks:
        scan_type, _motor = _classify(block.command, card)
        if scan_type == "measurement":
            records.append(_parse_fscan(header, block, card, fname))
        else:
            records.append(_parse_ascan(header, block, card, fname, scan_type))
    return records


# ---------------------------------------------------------------------------
# Emitter (§4 inverse): serialize a synthetic raw run to a raw .spec file
# ---------------------------------------------------------------------------
# The real raw #L body (73 columns, with literal multi-word names and the duplicated trailing
# Seconds). Column 0 is the scanned motor (HerixE for fscans / herixE ascans, the motor otherwise).
_RAW_L_BODY = (
    "HerixE  H  K  L  fdbk_hrm  fdbk_hhl  T_sample  T_sampTC  T_sampPT100  T_control  NRS_keV  "
    "HRMth3  HRMth2  HRMth1  HRMpzt1  HRMTh3_enc  HRMTh2_enc  HRMTh1_enc  HRMTemp6  HRMTemp5  "
    "HRMTemp4  HRMTemp3  HRMTemp2  HRMTemp1  An9_Temp  An9-AirTemp  An8_Temp  An7_Temp  An6_Temp  "
    "An5_Temp  An5-AirTemp  An4_Temp  An3_Temp  An2_Temp  An1_Temp  Epoch  HM_IC1  HM_IC2  PIN-B  "
    "HM_IC3  HM_IC0  APD4  OldAna8  OldAna6  OldAna4  OldAna2  ICO-C  PIN-D  PIN-C  IN-P  IN-D  "
    "FWD-P  FWD-D  det-r0  APD_B  Heat Stage T1  Heat Stage T2  Heat Stage T3  ampv  amph  "
    "Ana1  Ana2  Ana3  Ana4  Ana5  Ana6  Ana7  Ana8  Ana9  APD_V  APD_H  Seconds  Seconds")
_RAW_MULTIWORD = ["Heat Stage T1", "Heat Stage T2", "Heat Stage T3"]
_RAW_LABELS = spec_base._join_multiword(_RAW_L_BODY, _RAW_MULTIWORD)
_IDX = {nm: _RAW_LABELS.index(nm) for nm in
        ["HerixE", "ICO-C", "Heat_Stage_T1", "Seconds"] + [f"Ana{i}" for i in range(1, 10)]}


def _raw_row(abscissa, monitor, time, counts9, temperature) -> list[float]:
    """One 73-column data row: zeros except the load-bearing + a couple of eyeball columns."""
    row = [0.0] * len(_RAW_LABELS)
    row[_IDX["HerixE"]] = float(abscissa)              # column 0 = the scanned value
    row[_IDX["ICO-C"]] = float(monitor)
    row[_IDX["Seconds"]] = float(time)                 # first Seconds (the duplicate stays 0)
    row[_IDX["Heat_Stage_T1"]] = float(temperature)
    for k in range(1, 10):
        row[_IDX[f"Ana{k}"]] = float(counts9[k - 1])
    return row


def emit_file(run, card: Card) -> bytes:
    """Serialize a raw run to a raw S30 `.spec` (the inverse of `parse_file`).

    fscans write `nP × n_mini` rows (every pass of every point at that point's energy, so the parser
    regroups them by runs of equal energy); ascans write one row per point with column 0 the scanned
    motor. The global header and per-scan #G/#P/#V templates are reused from the supplementary
    emitter so the file reads like genuine HERIX output for the eyeball pause.
    """
    from isar.parsers.spec_s30 import (
        _S30_GEOM_BLOCK, _S30_HEADER_BLOCK, _S30_PBLOCK, _S30_V_EXTRA, _fmt, _num)

    out: list[str] = [
        f"#F /synthetic/sector30/HERIX/{run.experiment_id}.spec",
        "#E 1741359480",
        "#D Tue Oct 21 13:48:36 2025",
        f"#C synthetic RAW S30 run '{run.experiment_id}' for ISAR raw-ingestion validation",
        _S30_HEADER_BLOCK,
    ]
    for sc in run.scans:
        out.append(f"#S {sc.scan_number}  {sc.command}")
        out.append("#D Tue Oct 21 13:48:36 2025")
        out.append("#T 2  (Seconds)")
        out.append(_S30_GEOM_BLOCK)
        qh, qk, ql = sc.central_q
        out.append(f"#Q {_num(qh)} {_num(qk)} {_num(ql)}")
        out.append(_S30_PBLOCK)
        out.append(f"#C synthetic scan {sc.scan_number}")
        out.append(_S30_V_EXTRA[0]); out.append(_S30_V_EXTRA[1])
        out.append(f"#V2  {_num(sc.temperature_sensor)}  {_num(sc.temperature_setpoint)}")
        out.append(_S30_V_EXTRA[3])
        for i, d in enumerate(sc.detectors):
            out.append(f"#V{4 + i}  {_num(d.q[0])}  {_num(d.q[1])}  {_num(d.q[2])}")
        out.append(_S30_V_EXTRA[13]); out.append(_S30_V_EXTRA[14]); out.append(_S30_V_EXTRA[15])
        out.append("#N 73")
        out.append("")
        col0 = sc.scanned_motor or "HerixE"
        out.append("#L " + (_RAW_L_BODY if col0 == "HerixE"
                            else col0 + _RAW_L_BODY[len("HerixE"):]))

        if sc.miniscan_monitor is not None:                # fscan: nP × n_mini pass rows
            n_mini = sc.miniscan_monitor.shape[1]
            for p in range(len(sc.energy)):
                for m in range(n_mini):
                    counts9 = [d.miniscan_counts[p, m] for d in sc.detectors]
                    row = _raw_row(sc.energy[p], sc.miniscan_monitor[p, m],
                                   sc.miniscan_time[p, m], counts9, sc.temperature_setpoint)
                    out.append(" ".join(_fmt(v) for v in row))
        else:                                              # ascan: one row per point
            for p in range(len(sc.energy)):
                counts9 = [d.counts[p] for d in sc.detectors]
                row = _raw_row(sc.energy[p], sc.monitor[p], sc.time[p], counts9,
                               sc.temperature_setpoint)
                out.append(" ".join(_fmt(v) for v in row))

    return ("\n".join(out) + "\n").encode("utf-8")

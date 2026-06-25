"""S30 HERIX parser — a thin card-driven layer over the shared SPEC reader (§6).

One physical fscan carries up to nine analyzer spectra (one per `#V4`–`#V12`); this captures *all*
nine into one multi-spectrum `ScanRecord` (§0.3, §3), each at its own reported Q (D2). The old
MnTe parser kept only Ana5 and applied a 1-count error floor; here every analyzer survives and
variance is stored honestly (0 for 0 counts, D3 — no floor). Nothing is ever dropped: a
zero-denominator point is masked and flagged, not removed (§0.4).

The emitter (`emit_file`, the §4 inverse) is added in the next build step; parse is validated
against the real file first (Gate B) so the emitter inverts a parser already grounded in reality.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from isar.parsers import registry, spec_base
from isar.schema import axes, scan
from isar.schema.card import Card

name = "spec_s30"
version = "0.1.0"

_CARD_PATH = Path(__file__).parent / "cards" / "s30.policy.toml"


def card_template() -> Card:
    """The bundled S30 card (policy from the shipped TOML; fresh, empty state)."""
    return Card.load(_CARD_PATH)


def matches(path: str | Path) -> bool:
    try:
        return registry.detect(path)[:2] == ("APS", "S30")
    except (ValueError, OSError):
        return False


def _classify(command: str, card: Card) -> str:
    cmd = command.strip()
    pol = card.policy
    if cmd.startswith(pol.measurement_pattern):
        return "measurement"
    if cmd.startswith(pol.alignment_pattern):
        return "alignment"
    return "measurement"


def _meta_value(header, block, field_name: str):
    """A scalar metadata value, found by name in the parallel #H/#V groups (e.g. 'T_Control')."""
    if not field_name:
        return None
    for idx, names in header.H_groups.items():
        if field_name in names and idx in block.V_groups:
            return block.V_groups[idx][names.index(field_name)]
    return None


def _normalize(N: np.ndarray, denom: np.ndarray):
    """intensity = N/denom, variance = N/denom² (Poisson, NO floor). denom==0 → masked + flagged."""
    valid = denom != 0
    intensity = np.zeros_like(N)
    variance = np.zeros_like(N)
    np.divide(N, denom, out=intensity, where=valid)
    np.divide(N, denom * denom, out=variance, where=valid)
    return intensity, variance, valid


def parse_file(path: str | Path, card: Card) -> list[scan.ScanRecord]:
    header, blocks = spec_base.read_spec(path)
    pol = card.policy
    fname = Path(path).name
    records: list[scan.ScanRecord] = []

    for block in blocks:
        data = block.data
        nP = data.shape[0]
        energy = data[:, spec_base.column_index(block, pol.energy_column)]
        monitor = data[:, spec_base.column_index(block, pol.monitor_column)]
        time = (data[:, spec_base.column_index(block, pol.time_column)]
                if pol.time_column else np.zeros(nP))

        denom = monitor if pol.normalize_by == "monitor" else (
            time if pol.normalize_by == "time" else np.ones(nP))

        spectra: dict[str, scan.Spectrum] = {}
        for det in pol.detectors:
            if not det.enabled:
                continue
            if det.value_index in block.V_groups:
                q = list(block.V_groups[det.value_index])
                q_source = "reported"
            else:
                q = list(det.q_offset) if det.q_offset else [float("nan")] * 3
                q_source = "card_offset"
            N = data[:, spec_base.column_index(block, det.signal_column)].astype("float64")
            intensity, variance, valid = _normalize(N, denom)
            point_flags = [scan.PointFlag(int(i), "monitor_dropout", f"{pol.normalize_by}==0")
                           for i in np.nonzero(~valid)[0]]
            spectra[det.detector_id] = scan.Spectrum(
                detector_id=det.detector_id, q=q, q_source=q_source,
                intensity=intensity, variance=variance, mask=valid.astype(bool),
                point_flags=point_flags, spectrum_flags=[], resolution=None,
            )

        q0 = block.Q if len(block.Q) == 3 else [0.0, 0.0, 0.0]
        scan_axes = [axes.energy_axis(energy), *axes.constant_q_axes(q0[0], q0[1], q0[2], nP)]

        setpoint_field = pol.temperature_rule.get("setpoint_field", "")
        sp_val = _meta_value(header, block, setpoint_field)
        setpoint = np.full(nP, float(sp_val) if sp_val is not None else float("nan"))
        sensors = {}
        for sname in pol.temperature_rule.get("sensor_fields", []):
            v = _meta_value(header, block, sname)
            if v is not None:
                sensors[sname] = scan.Sensor(sname, np.full(nP, float(v)),
                                             np.ones(nP, dtype=bool), "monitored")
        temperature = scan.Temperature(setpoint, sensors, np.ones(nP, dtype=bool), False)

        record = scan.ScanRecord(
            experiment_id=fname,
            scan_number=block.scan_number,
            scan_type=_classify(block.command, card),
            scan_type_source="inferred",
            scan_axes=scan_axes,
            spectra=spectra,
            normalization=scan.Normalization(
                counting_mode=pol.counting_mode, monitor=monitor, time=time,
                preset=block.T[0] if block.T else 0.0, monitor_correction=False),
            temperature=temperature,
            environment=scan.Environment(field=None, logs={}),
            geometry=None,
            energy_calibration=scan.EnergyCalibration(
                raw_abscissa_kind=pol.raw_abscissa_kind, transform=None,
                elastic_zero_offset=None, status=pol.energy_status),
            scan_flags=[],
            raw_header=_raw_header(header, block),
            source=scan.Source(
                file=fname, byte_range=None, line_range=list(block.line_range),
                plugin=name, plugin_version=version,
                format_version=pol.format_version, schema_version=scan.SCHEMA_VERSION),
        )
        axes.assert_constant_q_energy(record)
        records.append(record)

    return records


def _raw_header(header: spec_base.SpecHeader, block: spec_base.SpecScanBlock) -> dict:
    """The instrument header blob, verbatim-ish and JSON-able, for provenance."""
    return {
        "global": {"F": header.F, "E": header.E, "D": header.D, "C": header.C,
                   "H": {str(k): v for k, v in header.H_groups.items()},
                   "O": {str(k): v for k, v in header.O_groups.items()}},
        "S": block.command, "D": block.D, "T": list(block.T) if block.T else None,
        "Q": block.Q, "C": block.C,
        "G": {str(k): v for k, v in block.G.items()},
        "P": {str(k): v for k, v in block.P_groups.items()},
        "V": {str(k): v for k, v in block.V_groups.items()},
    }


# Real S30 global header blocks (#H names + #O motor names), verbatim from the 2025 file, so
# emitted files look like genuine HERIX output for the human-eyeball pause. #H2 (T_Sample/
# T_Control) and #H4–#H12 (the nine analyzers) are load-bearing for parsing; the rest is fidelity.
_S30_HEADER_BLOCK = """\
#H0  SR_current  SR_status  barometer_mbar
#H1  Q_Angstrom  Q_nm  Q_au
#H2  T_Sample  T_Control
#H3  RV_B1  RV_B2  RV_B3
#H4  Analyzer1_H  Analyzer1_K  Analyzer1_L
#H5  Analyzer2_H  Analyzer2_K  Analyzer2_L
#H6  Analyzer3_H  Analyzer3_K  Analyzer3_L
#H7  Analyzer4_H  Analyzer4_K  Analyzer4_L
#H8  Analyzer5_H  Analyzer5_K  Analyzer5_L
#H9  Analyzer6_H  Analyzer6_K  Analyzer6_L
#H10  Analyzer7_H  Analyzer7_K  Analyzer7_L
#H11  Analyzer8_H  Analyzer8_K  Analyzer8_L
#H12  Analyzer9_H  Analyzer9_K  Analyzer9_L
#H13  Main_tth_analyzer
#H14  OR0_H  OR0_K  OR0_L  OR1_H  OR1_K  OR1_L
#H15  Lattice_a  Lattice_b  Lattice_c  Lattice_alpha  Lattice_beta  Lattice_gamma
#O0 HerixTTH        th       chi       phi  WBSlitV_B  WBSlitH_R  WBSlitV_T  WBSlitH_L
#O1    CRL_Y     CRL_X    BIM1_X    CRL_AX  HHLMTheta   HHLM_Y1   HHLM_Z2  HHLMTheta2
#O2  HHLM_X2  HHLM_Chi2   HHLM_XT   HHLM_YT  BSlitV-DW  BSlitV-UP  BSlitH-Out  BSlitH-In
#O3    secth      secy      secx    CRL_AY     wbsvg     wbsvc     wbshg     wbshc
#O4    bslvg     bslvc     bslhg     bslhc    KohzuE    HRM_X1    HRM_X2    HRM_X3
#O5  HRM_th1   HRM_th2   HRM_th3    HRM_y1    HRM_y2    HRM_y3  HRM_tilt1  HRM_tilt3
#O6 HRM_Mini1  HRM_Mini2      s2th     cam-x     cam-y     focus     atten     stbly
#O7    stblx     stblz     slity     slitx    cam_AX        xu        zu        yu
#O8       xd        yd    HerixX    HerixY    HerixZ   HerixAX   HerixAY    HerixE
#O9     a1ax      a1ay      a2ax      a2ay      a3ax      a3ay      a4ax      a4ay
#O10     a5ax      a5ay      a6ax      a6ay      a7ax      a7ay      a8ax      a8ay
#O11     a9ax      a9ay  cslit_vd  cslit_vu  cslit_ho  cslit_hi   micro_y   micro_x
#O12   rbs_xy   micro_z     cslvg     cslvc     cslhg     cslhc   HM_PZT1   HM_PZT2
#O13  HM_PZT3       pzt  Motor 169     b2vup   b2vdown  Motor 172     b2hin  Motor 174
#O14   b2hout  Motor 176    b2slvg    b2slvc    b2slhg    b2slhc  Slit ana5  PIN ana5
#O15    FPD X  Zoom Cam    cam_AY    Ruby_X      NRSE  HRM IC2 Y  HRM IC3 Y  Anal5 Diam
#O16 sample_th_b  micro_x_b  micro_y_b  micro_z_b  clean_up_x_b  clean_up_y_b  sample_x_b  sample_y_b
#O17 sample_z_b  sample_Phi_b  Ana5_Focus  Anal9 Diam  Anal7_Diam  Anal3_Diam  Anal1 Diam   btablex
#O18  btabley   btablez  Beam_Stop_Herix     phply     phplx      anc1      anc2    phplth
#O19    Ge_th  Dummymotor  phplth_c  Analyzer_Aux  HRM IC1 Y  HRM IC1 X  HRM IC2 X  HRM IC3 X
#O20 sample_Chi_b"""

# Per-scan geometry (#G) and motor-position (#P) blocks — a constant template carried from real
# scan 53. The parser ignores these; they are pure eyeball fidelity (geometry/motors do not vary
# across the synthetic test scans).
_S30_GEOM_BLOCK = """\
#G0 0 0 0 0 0 1 0 0 0 0 0 0 50 0 0.1 0 68 68 50 -1 1 1 3.13542 3.13542 0 463.6 838.8
#G1 3.762866878 3.76287 5.47 90 90 90 1.669786764 1.669785378 1.148662762 90 90 90 2 0 0 1 1 0 15.96709999 7.983499825 0.4371291504 -4.974978027 0 0 11.27209999 5.635999825 45.39437915 -6.691478027 0 0 0.52262 0.52262 0 0
#G3 1.663447578 -0.01699456055 -0.09930991957 -0.1448022867 -0.04839551962 -1.143851154 0.01273925226 1.668997384 -0.0341792024
#G4 1.999997299 0.05000104898 -5.659687578e-06 0.52262 81.83480376 -81.83502191 0 178.2939732 0 0 0 0 0 0 0 0 -180 -180 -180 -180 -180 -180 -180 -180 -180 0 0"""

_S30_PBLOCK = """\
#P0 15.9721 7.986 1.868625 -5.0175 2.0309308 0.84206228 2.6309431 0.23956254
#P1 -0.33998305 6.790625 49.999997 0.5952 4.7824547 -12.543651 149.929 0.18214946
#P2 -1.556 15.225187 10.3 6.0000011 -3.83 -0.28999996 -1.064 -3.9959999
#P3 85.920482 85.639967 -26.14 0 0.6 2.331 0.6025 0.541
#P4 3.54 -2.06 2.932 -2.53 23.719942 25.15 42 23.03
#P5 4618.7707 -30717.332 13420.129 -15.96 -180.4 -36.331 -0.97002 1.62027
#P6 1741.9203 -1425.852 4.0685015 2.0600002 -1.0300008 -2.83 1.1444092e-07 11.1825
#P7 6.019 6.992 0.20499199 1.0110005 -1.595 638.993 78.2805 0.55751418
#P8 2134.45 1.4206494 9.41 0.194 2.543 0.162 4.99 0
#P9 1579.4552 -3053.6809 -337.36649 -304.74801 -13.726692 -1496.807 -2283.7791 111.43007
#P10 716.04313 -606.8104 568.77185 -271.39715 244.83363 246.39616 -515.79129 -591.08715
#P11 -461.49232 164.99659 -3.1625 -2.6625 4.161 3.261 2.9499999 2.5365
#P12 91.279501 0.2495 0.5 -2.9125 0.9 3.711 4 87.3131
#P13 38.3 -0.1773 0 5.1149999 -9.4850001 0 9.0000003 0
#P14 -6.9999992 0 14.6 -2.185 -16 1 14.735 75
#P15 5 10 24.304 -79.55 -89812.65 27.300002 19.6695 95.00384
#P16 13.0007 -0.99958 2.08004 90 -124.0917 -122.1994 3.2786 0.3141
#P17 0.576 0.48000019 0.5 95.000002 94.999997 95.004 95.004 13.001
#P18 -23 0 -16.26 3.1 29 0 0 4.3935
#P19 10.0827 3 17.049502 -7.4000002 0.50125004 119.2994 8 -26.5
#P20 0"""

# Ancillary #V lines (constant template): #V0 ring/baro, #V1 Q, #V3 RV, #V13 main_tth, #V14 OR, #V15 lattice.
_S30_V_EXTRA = {
    0: "#V0  200.863  1  980.182",
    1: "#V1  3.34061  33.4061  1.76778",
    3: "#V3  1  2  3",
    13: "#V13  5",
    14: "#V14  2  0  0  1  1  0",
    15: "#V15  3.76287  3.76287  5.47  90  90  90",
}


def _column_labels() -> list[str]:
    labels = ["HerixE"]
    for i in range(1, 10):
        labels += [f"Ana{i}_sum_o", f"Ana{i}_3std", f"Ana{i}_5std"]
    labels += ["ICO-C", "Seconds"]
    return labels


def _fmt(x: float) -> str:
    """High-precision scientific, for the data table (matches the real file's column format)."""
    return f"{float(x):.18e}"


def _num(x: float) -> str:
    """Short native number format for header/#V/#Q lines (matches real S30: '2', '0.050001').

    Uses `repr` for the shortest round-tripping float, and bare integers for integer values — so
    emitted header values both look like the real file AND re-parse to the exact truth float.
    """
    f = float(x)
    if f == int(f) and abs(f) < 1e15:
        return str(int(f))
    return repr(f)


def emit_file(run, card: Card) -> bytes:
    """The §4 inverse: serialize a synthetic run to a growing SPEC file (byte-faithful to real S30).

    Writes the global header once, then one `#S` block per scan in scan-number order (the
    growing-file model, D4), mirroring the real per-scan layout: #S/#D/#T/#G/#Q/#P/#C/#V0..#V15/
    #N/#L/data. Header/#V/#Q values use the short native format (`_num`); the data table uses
    `%.18e` (`_fmt`) like the real file. All three AnaN sub-columns carry the analyzer's counts;
    the parser reads `_3std`, normalizes by `ICO-C`, and must reproduce the run's truth (Gate A).
    Duck-typed on the run (`run_builder.Run`) so `parsers` need not import `synth_format`.
    """
    labels = _column_labels()
    out: list[str] = [
        f"#F /synthetic/sector30/HERIX/{run.experiment_id}.spec",
        "#E 1741359480",
        "#D Fri Mar 07 08:58:00 2025",
        f"#C synthetic S30 run '{run.experiment_id}' for ISAR round-trip validation",
        _S30_HEADER_BLOCK,
    ]
    for sc in run.scans:
        out.append(f"#S {sc.scan_number}  {sc.command}")
        out.append("#D Fri Mar 07 11:32:32 2025")
        out.append("#T 2  (Seconds)")
        out.append(_S30_GEOM_BLOCK)
        qh, qk, ql = sc.central_q
        out.append(f"#Q {_num(qh)} {_num(qk)} {_num(ql)}")
        out.append(_S30_PBLOCK)
        out.append(f"#C synthetic scan {sc.scan_number}")
        # #V0..#V15: ancillary lines are a constant template; #V2 (T_Sample/T_Control) and
        # #V4..#V12 (the nine analyzer Qs) carry the run's truth.
        out.append(_S30_V_EXTRA[0])
        out.append(_S30_V_EXTRA[1])
        out.append(f"#V2  {_num(sc.temperature_sensor)}  {_num(sc.temperature_setpoint)}")
        out.append(_S30_V_EXTRA[3])
        for i, d in enumerate(sc.detectors):
            out.append(f"#V{4 + i}  {_num(d.q[0])}  {_num(d.q[1])}  {_num(d.q[2])}")
        out.append(_S30_V_EXTRA[13])
        out.append(_S30_V_EXTRA[14])
        out.append(_S30_V_EXTRA[15])
        out.append("#N 30")
        out.append("")
        out.append("#L " + "  ".join(labels))
        for p in range(len(sc.energy)):
            row = [sc.energy[p]]
            for d in sc.detectors:
                c = d.counts[p]
                row += [c, c, c]
            row += [sc.monitor[p], sc.time[p]]
            out.append(" ".join(_fmt(v) for v in row))

    return ("\n".join(out) + "\n").encode("utf-8")

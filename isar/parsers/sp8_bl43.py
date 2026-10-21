"""BL43LXU (SPring-8) processed-IXS parser: one scan is a SET of per-analyzer files.

The beamline's processing writes one text file per analyzer (and, for the CsV3Sb5 layout, a `.var`
sidecar); the operator's group's Ba122 export writes one three-column file per analyzer with the
scan, temperature and HKL in its name. `parse_set` turns a set (grouped by ingest from the card's
`[files]` table) into ONE `ScanRecord` holding one `Spectrum` per analyzer:

  * counts are RAW: N = (intensity / error)^2 through `metadata.recover_counts`, variance = N, no
    floor (hard rule 3). Monitor and time travel beside the counts on each spectrum, never folded in;
    the record-level normalization is NaN so a consumer that forgets `scan.exposure_for` gets "no
    measure", not a wrong number. `_i` counts are integers; `_lr` counts are kept unrounded and flagged.
  * analyzers can have different bins, so the scan axis is the union of every analyzer's bins; a bin an
    analyzer lacks is intensity 0, variance 0, monitor 0, time 0, masked, with a `bin_absent` point flag.
    Nothing is resampled or dropped. The CsV3Sb5 layout's bins sit on one lattice (`bin_meV`), and a
    member off it is left out and named; the Ba122 layout has irregular, per-analyzer energy axes, so
    its bins are matched to 1 micro-eV instead.
  * identity is (scan, detector): detector_id = `a` + two-digit analyzer number. A scan's number is its
    scan number(s) when known, else a hash of the set key above 1e9 (the stem rides in `scan_label`).
  * every fact carries its source. Q is attached by rung: a header or filename HKL on an analyzer is rung 1;
    the CsV3Sb5 filename HKL is only the scan's nominal arm-centre `hkl_nominal`, never a spectrum's Q;
    otherwise `q = None` (rung 4).
  * the energy zero is not known here: `status = "needs_zero"` comes from the card, never a defaulted 0.0.

The trailing `#Q=` / `#T=` lines of the real analyzer files are header facts (`Q_nm` per analyzer,
`T_K` for the scan); a leading header value wins over a disagreeing trailer, which is kept as a conflict.
Other trailing comments are skipped.
"""
from __future__ import annotations

import logging
import re
import tempfile
import zlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from isar.parsers import metadata, registry
from isar.schema import axes, scan
from isar.schema.card import Card

log = logging.getLogger(__name__)

name = "sp8_bl43"
version = "0.1.0"

_CARD_PATH = Path(__file__).parent / "cards" / "bl43lxu.policy.toml"

# Column family -> the columns it names. `_i` is the beamline's integer-count family; `_lr` splits
# counts between bins, so it is not raw (the card selects one; `_i` is the template's default).
_FAMILIES = {
    "_i": dict(y="y_i", e="ye_i", monitor="norm_i", time="secs", time_scale=1.0, integer=True),
    "_lr": dict(y=re.compile(r"a\d\d_lr"), e="error_lr", monitor="norm_lr", time="lrn",
                time_scale=0.25, integer=False),
}
_QUANT = 1e-3                  # meV: bins closer than this are the same bin / a bin is on the lattice
_KEV_PER_ORDER = 1.977         # incident energy per silicon reflection order (keV)
_HASHED_BASE = 1_000_000_000   # hashed scan numbers sit above any real scan number

# .var names -> fact names
_VAR_SCAN = {"silicon_order": ("si_order", int), "meV_bin_size": ("bin_meV", float),
             "nscans": ("n_merged_scans", int)}
_VAR_ANALYZER = {"analeff": "efficiency", "anal_polfactor": "polarisation_factor",
                 "elpk_centroid_meV": "elastic_centroid_meV", "elpk_area_meV": "elastic_area"}
_VAR_LINE = re.compile(r"^([A-Za-z_]\w*)(?:\[(\d+)\])?\s*=\s*(.+?)\s*$")
_TRAILER = re.compile(r"#([QT])=\s*(\S+)")     # the analyzer files' own |Q| and the scan's temperature
_TRAILER_KEYS = {"Q": "Q_nm", "T": "T_K"}


def card_template() -> Card:
    """The bundled BL43LXU card (policy from the shipped TOML; fresh, empty state)."""
    return Card.load(_CARD_PATH)


def matches(path: str | Path) -> bool:
    try:
        return registry.detect(path)[:2] == ("SP8", "BL43LXU")
    except (ValueError, OSError):
        return False


def detector_id(analyzer) -> str:
    """`a` + two-digit analyzer number: `03` -> a03, and the Ba122 `A6` -> a06."""
    return f"a{int(analyzer):02d}"


# ---------------------------------------------------------------------------
# Reading one member file
# ---------------------------------------------------------------------------
class MemberError(Exception):
    """A member that cannot be used; the message is its cause, named in `missing_analyzer`."""


@dataclass
class Member:
    detector: str
    energy: np.ndarray
    y_text: list[str]
    e_text: list[str]
    monitor: np.ndarray | None     # None: the file carries no monitor (Ba122)
    time: np.ndarray | None
    npts: np.ndarray | None
    header: dict
    labelled: bool                 # named columns (CsV3Sb5 layout) vs bare energy/intensity/error
    clash: dict                    # trailer values that disagree with the leading header (kept as conflicts)
    bad_trailer: list[str]         # trailing `#Q=`/`#T=` lines whose number does not parse


def _column(labels: list[str], spec) -> int | None:
    for i, label in enumerate(labels):
        if (spec.fullmatch(label) if hasattr(spec, "fullmatch") else label == spec):
            return i
    return None


def _trailer(lines: list[str]) -> tuple[dict, list[str]]:
    """`#Q=` and `#T=` lines after a file's data (`#Q=   11.250 /nm`, `#T=  250.000 K`) as header facts,
    and the lines whose number does not parse. Other trailing comments are skipped."""
    out: dict = {}
    bad: list[str] = []
    for s in lines:
        m = _TRAILER.match(s)
        if m:
            try:
                out[_TRAILER_KEYS[m.group(1)]] = float(m.group(2))
            except ValueError:
                bad.append(s)
    return out, bad


def _floats(rows: list[list[str]], col: int, what: str) -> np.ndarray:
    try:
        return np.array([float(r[col]) for r in rows], dtype=float)
    except ValueError as exc:
        raise MemberError(f"{what} column is not numeric ({exc})") from exc


def read_member(path: str | Path, family: str = "_i") -> Member:
    """Read one analyzer file. Comment lines anywhere are skipped; the leading ones (bar the column
    label line) are the `# key value` header. Raises MemberError, naming the cause."""
    spec = _FAMILIES[family]
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise MemberError(f"cannot be read ({type(exc).__name__}: {exc})") from exc
    labels: list[str] | None = None
    lead: list[str] = []
    trail: list[str] = []
    rows: list[list[str]] = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if not s.startswith("#"):
            rows.append(s.split())
        elif not rows:
            words = s[1:].split()
            if labels is None and words and words[0] == "meV":
                labels = words
            else:
                lead.append(s)
        else:
            trail.append(s)
    if not rows:
        raise MemberError("holds no data rows")
    width = len(rows[0])
    for i, r in enumerate(rows):
        if len(r) != width:
            raise MemberError(f"data row {i + 1} has {len(r)} columns, expected {width}")
    header = metadata.read_header(lead)
    trailer, bad = _trailer(trail)
    clash = {}                                             # a trailer that disagrees with the header: kept aside
    for key, value in trailer.items():                     # ...and a trailer fills only a gap
        if key not in header:
            header[key] = value
        elif header[key] != value:
            clash[key] = value
    if labels is None:                                     # bare energy, intensity, error
        if width < 3:
            raise MemberError(f"has {width} columns, expected energy, intensity, error")
        cols = dict(energy=0, y=1, e=2)
        monitor = time = npts = None
    else:
        if len(labels) != width:
            raise MemberError(f"names {len(labels)} columns but its rows hold {width}")
        cols = dict(energy=_column(labels, "meV"), y=_column(labels, spec["y"]),
                    e=_column(labels, spec["e"]), monitor=_column(labels, spec["monitor"]),
                    time=_column(labels, spec["time"]))
        absent = [spec[k].pattern if hasattr(spec[k], "pattern") else spec[k]
                  for k in ("y", "e", "monitor", "time") if cols[k] is None]
        if cols["energy"] is None:
            absent.insert(0, "meV")
        if absent:
            raise MemberError(f"lacks the {family} column family (no {', '.join(absent)} column)")
        monitor = _floats(rows, cols["monitor"], "monitor")
        time = _floats(rows, cols["time"], "time") * spec["time_scale"]
        n_col = _column(labels, "npts")
        npts = _floats(rows, n_col, "npts") if n_col is not None else None
    energy = _floats(rows, cols["energy"], "energy")
    for key, what in (("y", "intensity"), ("e", "error")):
        _floats(rows, cols[key], what)                     # fail early, naming the column
    return Member("", energy, [r[cols["y"]] for r in rows], [r[cols["e"]] for r in rows],
                  monitor, time, npts, header, labels is not None, clash, bad)


def read_var(path: str | Path) -> dict:
    """The `.var` sidecar as {"scalars": {name: value}, "arrays": {name: {index: value}}}. Numbers
    are floats, quoted values text; `array` declarations and comments are skipped."""
    scalars: dict = {}
    arrays: dict = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("array "):
            continue
        m = _VAR_LINE.match(s)
        if m is None:
            continue
        key, idx, raw = m.groups()
        try:
            value = float(raw)
        except ValueError:
            value = raw.strip('"')
        if idx is None:
            scalars[key] = value
        else:
            arrays.setdefault(key, {})[int(idx)] = value
    return {"scalars": scalars, "arrays": arrays}


def _var_facts(var: dict) -> tuple[dict, dict, list[str]]:
    """(scan facts, {detector: analyzer facts}, expected detector ids) from a parsed `.var`."""
    scan_facts = {}
    for key, (fact, cast) in _VAR_SCAN.items():
        v = var["scalars"].get(key)
        if isinstance(v, float):
            scan_facts[fact] = cast(v)
    numbers = var["arrays"].get("ano", {})
    per = {}
    for idx, number in numbers.items():
        if not isinstance(number, float):
            continue
        facts = {fact: var["arrays"][key][idx] for key, fact in _VAR_ANALYZER.items()
                 if isinstance(var["arrays"].get(key, {}).get(idx), float)}
        per[detector_id(number)] = facts
    return scan_facts, per, sorted(per)


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------
def _as_floats(v):
    return [float(x) for x in v] if isinstance(v, list) else v


def _scan_header_facts(members: list[Member]) -> tuple[dict, list[scan.Flag], dict]:
    """Scan-level facts from the members' headers: first member wins, a later member that disagrees
    raises `fact_conflict`. A member whose trailing `#T=` disagrees with its own `T_K` header keeps the
    header, and the trailer value is returned as {fact: conflicts} for the caller to attach. The one place
    header slit keys are known (R8): they become `slit`."""
    keys = {"scan": "scan_numbers", "T_K": "T_K", "si_order": "si_order", "energy_keV": "energy_keV",
            "agaph_mm": "agaph_mm", "agapv_mm": "agapv_mm", "lattice": "lattice", "time": "time"}
    out: dict = {}
    flags: list[scan.Flag] = []
    clashes: dict = {}
    for m in members:
        for key, fact in keys.items():
            if key not in m.header:
                continue
            if fact not in out:
                out[fact] = m.header[key]
            elif out[fact] != m.header[key]:
                flags.append(scan.Flag("fact_conflict", "warning",
                                       f"{fact}: header of {m.detector} gives {m.header[key]!r}, "
                                       f"an earlier analyzer gives {out[fact]!r}"))
        if "T_K" in m.clash:
            clashes.setdefault("T_K", []).append({"source": "trailer", "value": m.clash["T_K"]})
            flags.append(scan.Flag("fact_conflict", "warning",
                                   f"{m.detector}: T_K header gives {m.header['T_K']!r}, "
                                   f"its trailing line gives {m.clash['T_K']!r}"))
    h, v = out.get("agaph_mm"), out.get("agapv_mm")
    if all(isinstance(x, float) for x in (h, v)):
        out["slit"] = f"{h:g}x{v:g}"
    return out, flags, clashes


def _number(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _scan_number(facts: dict, key: str) -> int:
    """First scan number when known, else a hash of the set key above any real scan number."""
    numbers = facts.get("scan_numbers", {}).get("value")
    if isinstance(numbers, list) and numbers and all(isinstance(n, int) for n in numbers):
        return int(numbers[0])
    return _HASHED_BASE + zlib.crc32(key.encode("utf-8")) % _HASHED_BASE


# ---------------------------------------------------------------------------
# parse_set
# ---------------------------------------------------------------------------
def parse_set(file_set, card: Card) -> list[scan.ScanRecord]:
    pol = card.policy
    family = (pol.reader or {}).get("intensity_family", "_i")
    if family not in _FAMILIES:
        raise ValueError(f"[reader] intensity_family {family!r} must be one of {sorted(_FAMILIES)}")
    patterns = (pol.files or {}).get("measurement", [])
    disabled = {d.detector_id for d in pol.detectors if not d.enabled}

    # --- sidecar -------------------------------------------------------------------------
    var_path = next((p for p in file_set.sidecars if Path(p).suffix.lower() == ".var"), None)
    var_scan: dict = {}
    var_per: dict = {}
    expected: list[str] = []
    sidecar_flag = None
    if var_path is not None:
        try:
            var_scan, var_per, expected = _var_facts(read_var(var_path))
            if not (var_scan and expected):    # empty or cut short (still being copied): same as unreadable
                raise ValueError("holds no analyzer list or no scan facts")
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            var_scan, var_per, expected = {}, {}, []
            log.warning("%s: cannot read sidecar %s (%s)", file_set.key, Path(var_path).name, exc)
            sidecar_flag = scan.Flag("missing_sidecar", "warning",
                                     f"{Path(var_path).name} cannot be read ({exc}); its facts are missing")

    # --- members -------------------------------------------------------------------------
    members: dict[str, Member] = {}
    filename_facts: dict[str, dict] = {}
    causes: dict[str, str] = {}
    for path, groups in sorted(file_set.measurements, key=lambda pg: Path(pg[0]).name):
        det = detector_id(groups["analyzer"])
        if det in disabled:
            continue
        if det in members or det in causes:
            causes[det] = f"{Path(path).name} duplicates another file for this analyzer"
            continue
        try:
            m = read_member(path, family)
        except MemberError as exc:
            causes[det] = str(exc)
            log.warning("%s: analyzer %s (%s) left out: %s", file_set.key, det, Path(path).name, exc)
            continue
        m.detector = det
        members[det] = m
        filename_facts[det] = metadata.match_filename(Path(path).name, patterns)

    # --- bins: lattice for the named-column layout, 1 micro-eV matching otherwise ----------
    step = origin = None
    if any(m.labelled for m in members.values()):
        bin_meV = var_scan.get("bin_meV")
        if bin_meV is not None and bin_meV > 0:
            step, origin = float(bin_meV), 0.0
        else:
            spacings = [float(np.median(np.diff(m.energy))) for m in members.values()
                        if len(m.energy) > 1]
            if spacings:
                first = members[min(members)]
                step, origin = float(np.median(spacings)), float(first.energy[0])
    keys: dict[str, np.ndarray] = {}
    for det in sorted(members):
        m = members[det]
        if step is not None and m.labelled:
            k = np.round((m.energy - origin) / step)
            off = int((np.abs(m.energy - (origin + k * step)) > _QUANT).sum())
            if off:
                causes[det] = f"off the bin lattice ({off} of {len(k)} bins, step {step:g} meV)"
        else:
            k = np.round(m.energy / _QUANT)
        if det not in causes and len(set(k.tolist())) != len(k):
            causes[det] = "holds duplicate bins"
        keys[det] = k.astype(np.int64)
    for det in causes:
        members.pop(det, None)
    if not members:
        raise ValueError(f"{file_set.key}: no analyzer could be read ("
                         + "; ".join(f"{d}: {c}" for d, c in sorted(causes.items())) + ")")

    all_keys = np.unique(np.concatenate([keys[d] for d in members]))
    n = len(all_keys)
    energy = np.empty(n)
    seen = np.zeros(n, dtype=bool)
    for det in sorted(members):                       # printed energies, first analyzer wins
        pos = np.searchsorted(all_keys, keys[det])
        fresh = ~seen[pos]
        energy[pos[fresh]] = members[det].energy[fresh]
        seen[pos] = True
    labelled = any(m.labelled for m in members.values())

    # --- scan-level facts ----------------------------------------------------------------
    first_name = Path(sorted(file_set.measurements, key=lambda pg: Path(pg[0]).name)[0][0]).name
    fn = metadata.match_filename(first_name, patterns)
    scan_flags: list[scan.Flag] = []
    header_facts, conflict_flags, header_clashes = _scan_header_facts([members[d] for d in sorted(members)])
    scan_flags += conflict_flags
    from_name: dict = {"scan_label": file_set.key}
    if "T_K" in fn and _number(fn["T_K"]) is not None:
        from_name["T_K"] = float(fn["T_K"])
    if "hkl_nominal" in fn and isinstance(fn["hkl_nominal"], list):
        from_name["hkl_nominal"] = _as_floats(fn["hkl_nominal"])
    if "scan_numbers" in fn:
        sn = fn["scan_numbers"]
        from_name["scan_numbers"] = [int(x) for x in (sn if isinstance(sn, list) else [sn])]
    expect = ["scan_numbers", "T_K", "slit", "energy_keV", "lattice", "time"]
    if labelled:
        expect += ["hkl_nominal", "si_order", "bin_meV", "n_merged_scans"]
    facts, flags = metadata.merge_facts(
        {"header": header_facts, "sidecar": var_scan, "filename": from_name, "card": dict(pol.facts or {})},
        expected=expect)
    scan_flags += flags
    for fact, entries in header_clashes.items():
        facts[fact].setdefault("conflicts", []).extend(entries)
    order = _number(facts.get("si_order", {}).get("value"))
    if facts["energy_keV"]["source"] == "missing" and order is not None:
        facts["energy_keV"] = {"value": order * _KEV_PER_ORDER, "source": "inferred"}
    if sidecar_flag is not None:
        scan_flags.append(sidecar_flag)
    elif labelled and var_path is None:
        scan_flags.append(scan.Flag("missing_sidecar", "warning",
                                    f"no {file_set.key}.var beside the analyzer files; "
                                    "bin size, silicon order and per-analyzer sidecar facts are missing"))
    gone = {d: causes.get(d, "no file") for d in expected if d not in members and d not in disabled}
    gone.update({d: c for d, c in causes.items() if d not in gone})
    if gone:
        scan_flags.append(scan.Flag(
            "missing_analyzer", "warning",
            "; ".join(f"{d}: {c}" for d, c in sorted(gone.items())) + " (left out of this scan)"))

    # --- spectra -------------------------------------------------------------------------
    spectra: dict[str, scan.Spectrum] = {}
    for det in sorted(members):
        spectra[det] = _spectrum(members[det], keys[det], all_keys, filename_facts[det],
                                 var_per.get(det, {}), family, labelled)

    # --- the record ----------------------------------------------------------------------
    t = _number(facts["T_K"]["value"])
    session_t = np.full(n, t if t is not None else np.nan)
    sample = pol.sample or {}
    nan = np.full(n, np.nan)
    return [scan.ScanRecord(
        experiment_id=str(sample.get("experiment_id") or sample.get("name") or "bl43lxu"),
        scan_number=_scan_number(facts, file_set.key),
        scan_type="measurement", scan_type_source="inferred",
        scan_axes=[axes.energy_axis(energy)],
        spectra=spectra,
        normalization=scan.Normalization(counting_mode=pol.counting_mode, monitor=nan.copy(),
                                         time=nan.copy(), preset=float("nan"), monitor_correction=False),
        temperature=scan.Temperature(session_t, {}, np.ones(n, dtype=bool), False),
        environment=scan.Environment(field=None, logs={}),
        geometry=None,
        energy_calibration=scan.EnergyCalibration(
            raw_abscissa_kind=pol.raw_abscissa_kind, transform=None, elastic_zero_offset=None,
            status=pol.energy_status),
        scan_flags=scan_flags,
        raw_header={"layout": "named_columns" if labelled else "bare_columns",
                    "intensity_family": family,
                    "files": {d: Path(p).name for p, g in file_set.measurements
                              for d in [detector_id(g["analyzer"])] if d in spectra},
                    "headers": {d: dict(members[d].header) for d in sorted(members)},
                    "sidecar": Path(var_path).name if var_path is not None else None},
        source=scan.Source(file=file_set.key, byte_range=None, line_range=None, plugin=name,
                           plugin_version=version, format_version=pol.format_version,
                           schema_version=scan.SCHEMA_VERSION),
        facts=facts)]


def _spectrum(m: Member, k: np.ndarray, all_keys: np.ndarray, fname_facts: dict, sidecar: dict,
              family: str, labelled: bool) -> scan.Spectrum:
    """One analyzer on the union axis: raw counts, own exposure, facts with sources, Q by rung."""
    det, n = m.detector, len(all_keys)
    pos = np.searchsorted(all_keys, k)
    rec = metadata.recover_counts(m.y_text, m.e_text, _FAMILIES[family]["integer"])
    present = np.zeros(n, dtype=bool)
    present[pos] = True
    counts = np.zeros(n)
    counts[pos] = np.where(rec.invalid, 0.0, rec.counts)
    keep = present.copy()
    keep[pos[rec.invalid]] = False
    point_flags = [scan.PointFlag(int(i), "bin_absent", f"{det} has no bin here")
                   for i in np.nonzero(~present)[0]]
    point_flags += [scan.PointFlag(int(pos[i]), "invalid_point",
                                   "intensity and error give no count (error 0 with intensity, or not finite)")
                    for i in np.nonzero(rec.invalid)[0]]
    if m.monitor is None:                               # exposure missing: unknown, not zero
        monitor, time = np.full(n, np.nan), np.full(n, np.nan)
    else:
        monitor, time = np.zeros(n), np.zeros(n)
        monitor[pos], time[pos] = m.monitor, m.time
        dropped = m.monitor == 0
        keep[pos[dropped]] = False
        point_flags += [scan.PointFlag(int(pos[i]), "monitor_dropout", "monitor==0")
                        for i in np.nonzero(dropped)[0]]
    spectrum_flags = []
    if family == "_lr" or rec.not_integer.any():       # `_lr` is never raw, whatever its bins recover to
        what = ("_lr counts are not raw (they split counts between bins)" if family == "_lr"
                else "bins beyond the printed digits")
        # Under half a count the nearest integer is still unambiguous, so only a notice; `_lr` is never raw.
        severity = "warning" if family == "_lr" or rec.max_deviation >= 0.5 else "info"
        spectrum_flags.append(scan.Flag(
            "counts_not_integer", severity,
            f"{det}: {int(rec.not_integer.sum())} of {len(k)} bins have non-integer counts, {what}; "
            f"largest deviation {rec.max_deviation:.3g}"))

    header = {fact: m.header[key] for key, fact in (("hkl", "hkl"), ("dq", "dq"), ("Q_nm", "Q_nm"))
              if key in m.header}
    from_name = {"hkl": _as_floats(fname_facts["hkl"])} if isinstance(fname_facts.get("hkl"), list) else {}
    expect = ["hkl", "dq", "Q_nm"] + (list(_VAR_ANALYZER.values()) if labelled else [])
    if m.monitor is None:
        expect += ["monitor", "time"]
    trailer = {key: v for key, v in m.clash.items() if key == "Q_nm"}
    facts, flags = metadata.merge_facts({"header": header, "trailer": trailer, "sidecar": sidecar,
                                         "filename": from_name}, expected=expect, scope=f"{det}: ")
    spectrum_flags += flags
    spectrum_flags += [scan.Flag("trailer_unparsed", "warning",
                                 f"{det}: trailing line {s!r} does not parse; its value is left out")
                       for s in m.bad_trailer]
    if m.npts is not None:
        ppb = np.zeros(n)
        ppb[pos] = m.npts
        facts["points_per_bin"] = {"value": ppb.astype(int).tolist(), "source": "header"}
    facts["count_recovery_max_deviation"] = {"value": rec.max_deviation, "source": "inferred"}

    hkl = facts["hkl"]["value"]
    if facts["hkl"]["source"] in ("header", "filename") and isinstance(hkl, list) and len(hkl) == 3 \
            and all(isinstance(x, float) for x in hkl):
        q, q_source = [float(x) for x in hkl], facts["hkl"]["source"]
    else:
        q, q_source = None, "none"
    return scan.Spectrum(
        detector_id=det, q=q, q_source=q_source, intensity=counts, variance=counts.copy(), mask=keep,
        point_flags=sorted(point_flags, key=lambda p: p.index), spectrum_flags=spectrum_flags,
        resolution=None, monitor=monitor, time=time, facts=facts)


# ---------------------------------------------------------------------------
# parse_file / emit_file
# ---------------------------------------------------------------------------
def parse_file(path: str | Path, card: Card) -> list[scan.ScanRecord]:
    """A BL43LXU scan is a set of files; ingest groups them from the card's `[files]` table and calls
    `parse_set`. A card without `[files]` has no way to name a set."""
    raise ValueError(f"{Path(path).name}: BL43LXU scans are sets of per-analyzer files; "
                     "the card needs a [files] table (see the shipped bl43lxu template)")


def emit_file(record: scan.ScanRecord, card: Card, out_dir: str | Path | None = None) -> Path:
    """Write a record in the CsV3Sb5 layout: `<stem>_aNN` per analyzer (the `_i` family: bin centre,
    seconds, points, monitor, normalised intensity and error, then the `#Q=`/`#T=` lines) plus a minimal
    `<stem>.var`. Returns the folder. Absent bins are not written; a zero-count bin is written as zero over zero.
    """
    out = Path(out_dir) if out_dir is not None else Path(tempfile.mkdtemp(prefix="bl43_emit_"))
    out.mkdir(parents=True, exist_ok=True)
    label = (record.facts.get("scan_label") or {}).get("value")
    stem = str(label) if label else f"scan{record.scan_number}"
    energy = axes.scanned_axes(record)[0].values
    dets = sorted(record.spectra)
    t_k = (record.facts.get("T_K") or {}).get("value")
    for det in dets:
        sp, ex = record.spectra[det], scan.exposure_for(record, det)
        absent = {p.index for p in sp.point_flags if p.type == "bin_absent"}
        rows = ["#      meV      secs   npts          norm_i             y_i            ye_i"]
        for i in range(len(energy)):
            if i in absent:
                continue
            counts, mon, secs = float(sp.intensity[i]), float(ex.monitor[i]), float(ex.time[i])
            y = counts / mon if mon else 0.0
            e = counts ** 0.5 / mon if mon else 0.0
            rows.append(f"{energy[i]:10.4f} {secs:9.1f} {int(round(secs / 10)):6d} "
                        f"{mon:15.7E} {y:15.7E} {e:15.7E}")
        q_nm = (sp.facts.get("Q_nm") or {}).get("value")
        if q_nm is not None:
            rows.append(f"#Q=   {q_nm:.3f} /nm")
        if t_k is not None:
            rows.append(f"#T=  {t_k:.3f} K")
        (out / f"{stem}_{det}").write_text("\n".join(rows) + "\n", encoding="utf-8", newline="\n")
    fact = lambda k, default: (record.facts.get(k) or {}).get("value") or default  # noqa: E731
    var = [f' file_name_prefix = "{stem}"', f" silicon_order = {int(fact('si_order', 12))}",
           f" meV_bin_size = {float(fact('bin_meV', 0.2)):.2f}",
           f" nscans = {int(fact('n_merged_scans', 1))}", "", f"array ano[{len(dets)}]", ""]
    for i, det in enumerate(dets, 1):
        sf = record.spectra[det].facts
        var.append(f"ano[{i}] = {int(det[1:])}")
        for key, fname in _VAR_ANALYZER.items():
            v = (sf.get(fname) or {}).get("value")
            var.append(f"{key}[{i}] = {float(v) if v is not None else 0.0:.4f}")
        var.append("")
    (out / f"{stem}.var").write_text("\n".join(var), encoding="utf-8", newline="\n")
    return out

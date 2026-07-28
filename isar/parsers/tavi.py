"""TAVI parser — the McStas triple-axis SIMULATOR plugin (§6, dedicated catalogue).

TAVI is not a real instrument: it is a McStas-backed virtual triple-axis spectrometer driven over an
HTTP API. The reduction input is one JSON file per completed scan — the saved body of TAVI's
`GET /scan/{id}/data` response. This plugin is deliberately a SEPARATE catalogue from the real-numor
parsers (ill_in8, spec_s30): TAVI emits API job JSON, never numors, so the two never share a reader.

A TAVI JSON carries a frozen `launch.parameters` set (the full instrument state) plus a `result`
block (the scanned variable, its values, and per-point RAW COUNTS). The stored signal is RAW COUNTS;
the "monitor" is the simulated source budget `number_neutrons` per point (constant) — it is stored,
not divided in, so the Poisson error stays plain (variance = counts). Fixed-ki/kf geometry makes the
meV energy axis absolute (no elastic-zero fit needed — this is a simulator, the axis is exact).

TAVI's payload now carries the per-scan instrument truth that older JSONs lacked, all in
`launch.parameters` (duplicated in `result.metadata`): sample `temperature`, monochromator/analyzer
mosaics `eta_m`/`eta_a`, sample mosaic `eta_s`, scattering senses `sense_m`/`sense_s`/`sense_a`, the
VERTICAL collimations `beta_1..beta_4`, and a `sample_key`. The plugin PREFERS these payload values
and falls back to the card [calibration]/[temperature_rule] constants only when a field is absent —
the same payload-wins-when-present rule already used for the horizontal collimations
`collimation.alpha_1..alpha_4` (card `collimations` is only their gap-filler). Legacy JSONs that carry
none of the truth fields parse identically to before (card constants throughout). Per-field
provenance (payload-truth vs card-fallback) is recorded in `raw_header["provenance"]` and summarized
as an info-level `card_fallback` scan flag so downstream QC can tell which CN inputs are constants.

TRUTH FIREWALL (background). TAVI can *generate* background, and stamps the planted numeric truth
(rates, widths, source parameters, event seed, and sparse cosmic-event realizations) into
`result.metadata.background`. That truth must never reach ISAR's analysis record: the fitter would
then be conditioning on the answer. So this
plugin splits the block in two. The SAFE half — `enabled`, the v1 `source` or v2
`delivery_source`, `background_schema`, v2 `catalog_version`, `profile_fingerprint`, and
`effective_fingerprint` — is copied to `raw_header["background_provenance"]`;
it carries identity only (the effective fingerprint is the pooling identity collation keys on) and no
magnitude, which is why the preset NAME is excluded too ("flat_high" states a magnitude as plainly as
a number does). The NUMERIC half is REDACTED from the stored payload: `raw_header["tavi"]` holds a
shallow copy whose `result.metadata.background` is `{"redacted": "sim_truth",
"effective_fingerprint": ...}`. The firewall is physical, not a naming convention — the numbers are
simply not on the record. The raw TAVI JSON on disk is untouched, and validation scoring reads the
truth there, AFTER fitting. The two leak paths are closed INDEPENDENTLY: a payload carrying only
`launch.background` (an interrupted job whose result block never landed) is still redacted, and gets a
provenance whose fingerprints are `None` — collation then classes it UNKNOWN and refuses to pool it.
A payload with NEITHER block (older TAVI, and every real instrument) gets no `background_provenance`
key and parses exactly as it did before.

The HORIZONTAL collimations ALF1..4 come from the payload directly (arcmin); d-spacings DM/DA come
from the crystal-id → d map below (card override wins). experiment_id comes from the card ([sample]
experiment_id/name) since TAVI JSONs carry no EXPNO; the payload `sample_key` is recorded in
`raw_header["sample_key"]` for collation but does not (yet) drive a per-sample card section.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

import numpy as np

from isar.schema import axes, scan
from isar.schema.card import Card

name = "tavi"
version = "0.1.0"

_CARD_PATH = Path(__file__).parent / "cards" / "tavi.policy.toml"

# meV = _EK * k^2  (k in Angstrom^-1); k = sqrt(E / _EK). Matches isar.calibrate.tas (_F = 1/_EK).
_EK = 2.072142

# Crystal id → d-spacing (Angstrom). Card [calibration] dm/da override these when present.
_DSPACING = {
    "pg002": 3.355, "pg002_test": 3.355,
    "si111": 3.135, "cu220": 1.278, "ge311": 1.714,
}

# Which result.variable_1 values are elastic-scan (calibration) candidates when deltaE ≈ 0.
_ELASTIC_VARS = {"h", "k", "l", "omega", "a3"}
_AXIS_KIND = {"deltae": "energy", "h": "q_h", "k": "q_k", "l": "q_l"}
_AXIS_UNITS = {"energy": "meV", "q_h": "rlu", "q_k": "rlu", "q_l": "rlu", "angle": "deg"}


# ---------------------------------------------------------------------------
# Card template + signature sniff
# ---------------------------------------------------------------------------
def card_template() -> Card:
    """The bundled TAVI card (policy from the shipped TOML; fresh, empty state)."""
    return Card.load(_CARD_PATH)


def matches(path: str | Path) -> bool:
    """A TAVI scan file is JSON with top-level `job_id` + `result`. Cheap sniff then full load."""
    p = Path(path)
    if p.suffix.lower() != ".json":
        return False
    try:
        with open(p, "rb") as f:
            head = f.read(512).lstrip()
        if not head.startswith(b"{"):
            return False
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and "job_id" in data and "result" in data


# ---------------------------------------------------------------------------
# Small coercions
# ---------------------------------------------------------------------------
def _num(v):
    """Best-effort float (accepts numeric strings like "1e8"); None if not parseable."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _scan_number(job_id: str) -> int:
    """"j-0001" → 1. The trailing digit run; 0 if none."""
    m = re.search(r"(\d+)\s*$", str(job_id or ""))
    return int(m.group(1)) if m else 0


# ---------------------------------------------------------------------------
# Cooper-Nathans header (raw_header["params"]) — the keys isar.calibrate.tas reads
# ---------------------------------------------------------------------------
def _dspacing(crystal_id, cal: dict, override_key: str) -> float:
    """d-spacing (Angstrom): card [calibration] override wins, else crystal-id map, else PG002."""
    if override_key in cal:
        return float(cal[override_key])
    cid = str(crystal_id or "").lower().replace(" ", "")
    return _DSPACING.get(cid, _DSPACING["pg002"])


def _kfix(params: dict):
    """(KFIX, FX): fixed wavevector + FX convention (1 = Ki fixed, 2 = Kf fixed).

    isar.calibrate.tas uses ki = sqrt(kfix^2 + (fx-1)*_F*w), kf = sqrt(kfix^2 - (2-fx)*_F*w), so
    FX=1 pins ki and FX=2 pins kf — matching TAVI's `K_fixed` mode string.
    """
    mode = str(params.get("K_fixed", "")).lower()
    if "ki" in mode:
        fx = 1
        k = _num(params.get("Ki"))
        e_fallback = _num(params.get("Ei"))
    else:  # default / "Kf Fixed"
        fx = 2
        k = _num(params.get("Kf"))
        e_fallback = _num(params.get("Ef"))
    if k is None:                                   # derive k from the fixed energy: E = _EK * k^2
        e = _num(params.get("fixed_E"))
        if e is None:
            e = e_fallback
        k = math.sqrt(e / _EK) if (e is not None and e > 0) else None
    return k, fx


def _collimation_alfs(coll, cal: dict) -> list[float]:
    """ALF1..4 (arcmin) from TAVI `collimation.alpha_1..alpha_4`. Multi-select slot → tightest
    (min) blade. Non-numeric or missing slot → card [calibration] `collimations` fallback.
    """
    fallback = [float(x) for x in (cal.get("collimations") or [30.0, 30.0, 30.0, 30.0])]
    if not isinstance(coll, dict):
        return list(fallback)
    out: list[float] = []
    for i, slot in enumerate(("alpha_1", "alpha_2", "alpha_3", "alpha_4")):
        v = coll.get(slot)
        if isinstance(v, (list, tuple)):
            nums = [x for x in (_num(z) for z in v) if x is not None]
            out.append(min(nums) if nums else fallback[i])
        else:
            fv = _num(v)
            out.append(fv if fv is not None else fallback[i])
    return out


def _pick_num(params: dict, payload_key: str, cal: dict, card_key: str, default: float):
    """Payload value wins when present+parseable; else card [calibration] constant; else `default`.

    Returns (value, source) where source is "payload" or "card" for the provenance record.
    """
    n = _num(params.get(payload_key))
    if n is not None:
        return float(n), "payload"
    if card_key in cal:
        return float(cal[card_key]), "card"
    return float(default), "card"


def _pick_senses(params: dict, cal: dict):
    """Scattering senses (SM, SS, SA): payload `sense_m/sense_s/sense_a` when ALL present, else the
    card [calibration] `senses` triple. Returns ((SM, SS, SA), source)."""
    vals = [_num(params.get(k)) for k in ("sense_m", "sense_s", "sense_a")]
    if all(v is not None for v in vals):
        return (int(vals[0]), int(vals[1]), int(vals[2])), "payload"
    card = cal.get("senses", [1, -1, 1])
    return (int(card[0]), int(card[1]), int(card[2])), "card"


def _pick_beta(params: dict, cal: dict):
    """Vertical collimations BET1..4: payload `beta_1..beta_4` when ALL present, else the card
    [calibration] `bet` list. Returns ([BET1..4], source)."""
    vals = [_num(params.get(k)) for k in ("beta_1", "beta_2", "beta_3", "beta_4")]
    if all(v is not None for v in vals):
        return [float(v) for v in vals], "payload"
    card = [float(x) for x in (cal.get("bet") or [30.0, 30.0, 30.0, 30.0])]
    return card, "card"


def _cn_params(params: dict, card: Card, prov: dict) -> dict:
    """Build raw_header["params"] with the Cooper-Nathans keys isar.calibrate.tas.cn_energy_fwhm reads.

    Records per-field provenance (payload-truth vs card-fallback) into `prov` as it goes.
    """
    cal = card.policy.calibration or {}
    p: dict = {}
    p["QH"] = _num(params.get("H")) or 0.0
    p["QK"] = _num(params.get("K")) or 0.0
    p["QL"] = _num(params.get("L")) or 0.0
    p["EN"] = _num(params.get("deltaE")) or 0.0
    p["AS"] = _num(params.get("lattice_a")) or 1.0
    p["BS"] = _num(params.get("lattice_b")) or 1.0
    p["CS"] = _num(params.get("lattice_c")) or 1.0
    p["DM"] = _dspacing(params.get("monocris"), cal, "dm")
    p["DA"] = _dspacing(params.get("anacris"), cal, "da")
    kfix, fx = _kfix(params)
    if kfix is not None:
        p["KFIX"] = float(kfix)
    p["FX"] = int(fx)
    # Mosaics: payload truth (eta_m/eta_a) preferred, card constants fill the gap.
    p["ETAM"], prov["eta_m"] = _pick_num(params, "eta_m", cal, "etam", 30.0)
    p["ETAA"], prov["eta_a"] = _pick_num(params, "eta_a", cal, "etaa", 30.0)
    # Sample mosaic: payload eta_s wins when present, card [calibration] etas fills the gap. A
    # NON-POSITIVE value (from either source) breaks the Cooper-Nathans matrix (1/(etas*q)^2 term),
    # so it is OMITTED and cn_resolution_matrix falls back to ETAM (regression guard, fixed
    # 2026-07-03). The provenance still records where the (omitted) value came from.
    etas, prov["eta_s"] = _pick_num(params, "eta_s", cal, "etas", 0.0)
    if etas > 0.0:
        p["ETAS"] = etas
    # Scattering senses: payload sense_m/sense_s/sense_a preferred, card `senses` fills the gap.
    (p["SM"], p["SS"], p["SA"]), prov["senses"] = _pick_senses(params, cal)
    # Horizontal collimations from the payload; vertical (BET) payload-truth then card.
    alf = _collimation_alfs(params.get("collimation"), cal)
    for i in range(4):
        p[f"ALF{i + 1}"] = float(alf[i])
    bet, prov["beta"] = _pick_beta(params, cal)
    for i in range(4):
        p[f"BET{i + 1}"] = float(bet[i])
    return p


# ---------------------------------------------------------------------------
# Background truth firewall (see the module docstring)
# ---------------------------------------------------------------------------
# The only fields of TAVI's background blocks allowed onto the record: identity and bookkeeping.
# ``source`` is retained for historical tavi.background/1 payloads; v2 calls it ``delivery_source``.
# Everything else (terms/sources, params, units, scales, preset name, seed, realized events) states
# planted truth.
BACKGROUND_SAFE_KEYS = ("enabled", "source", "delivery_source", "background_schema",
                        "catalog_version", "profile_fingerprint", "effective_fingerprint")


def _background_block(result: dict):
    """TAVI's `result.metadata.background` block, or None when the payload carries none."""
    md = result.get("metadata")
    block = md.get("background") if isinstance(md, dict) else None
    return block if isinstance(block, dict) else None


def _launch_background_present(data: dict) -> bool:
    """True when the payload's launch summary echoes a REQUESTED background spec."""
    launch = data.get("launch")
    return isinstance(launch, dict) and launch.get("background") is not None


def _background_provenance(block, *, launch_source=None) -> dict:
    """The SAFE half of the background block: identity only, never a rate/width/preset name.

    `block` is ``None`` for a payload that requested a background (`launch.background`) but carries no
    resolved `result.metadata.background` — a malformed or interrupted job. All safe keys are then
    populated from what exists (`launch.background_source` if present, the rest ``None``), which is a
    provenance WITHOUT an effective fingerprint: collation classes it UNKNOWN and refuses to pool it,
    which is the fail-closed answer for a scan whose background truth was never resolved.
    """
    if isinstance(block, dict):
        return {key: block.get(key) for key in BACKGROUND_SAFE_KEYS}
    provenance = {key: None for key in BACKGROUND_SAFE_KEYS}
    provenance["source"] = launch_source
    return provenance


def _redact_background(data: dict, block) -> dict:
    """The stored payload with every numeric background trace replaced by a fingerprint pointer.

    Two leak paths, both closed here and INDEPENDENTLY: `result.metadata.background` (the resolved
    truth block, including sparse cosmic-event realizations) and `launch.background` (TAVI's launch
    summary echoes the requested spec — historical v1 preset/frozen terms or the current v2 keyed
    sources). Either can appear without the other: an interrupted job can carry the request with no
    result, and redacting the launch spec only when the result block existed left that request's
    numerics on the record.
    `launch.background_source` is safe and kept for historical payloads.

    Returns a SHALLOW COPY of `data` (and of the nested dicts it edits) so nothing the rest of
    `parse_file` reads — or any caller sharing the parsed object — is mutated. Every other key still
    aliases the original payload.
    """
    pointer = {"redacted": "sim_truth",
               "effective_fingerprint": (block.get("effective_fingerprint")
                                         if isinstance(block, dict) else None)}
    stored = dict(data)
    if isinstance(block, dict):
        result = dict(data.get("result") or {})
        metadata = dict(result.get("metadata") or {})
        metadata["background"] = dict(pointer)
        result["metadata"] = metadata
        stored["result"] = result
    if _launch_background_present(data):
        launch = dict(data["launch"])
        launch["background"] = dict(pointer)
        stored["launch"] = launch
    return stored


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
def _classify(variable_1, params: dict) -> str:
    var = str(variable_1 or "").lower()
    if var == "deltae":
        return "measurement"
    deltaE = _num(params.get("deltaE")) or 0.0
    if abs(deltaE) <= 1e-6 and var in _ELASTIC_VARS:
        return "calibration"
    return "alignment"


# ---------------------------------------------------------------------------
# parse_file
# ---------------------------------------------------------------------------
def parse_file(path: str | Path, card: Card) -> list[scan.ScanRecord]:
    """Parse one TAVI scan JSON into a single ScanRecord (all points; masked, never dropped)."""
    pol = card.policy
    p = Path(path)
    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)

    result = data.get("result") or {}
    launch = data.get("launch") or {}
    params = launch.get("parameters") or {}

    mode = str(result.get("mode", "1D"))
    if mode == "2D" or result.get("counts_grid") is not None or result.get("variable_2"):
        # 2D scans are out of scope for v1. Raise a clear ValueError — the ISAR convention callers
        # catch to skip gracefully with a reason (mirrors ill_in8's unsupported-scan handling).
        raise ValueError(f"{p.name}: 2D TAVI scan (mode={mode!r}) not supported by the v1 parser")

    variable_1 = result.get("variable_1")
    values = np.asarray(result.get("scan_values_1") or [], dtype="float64")
    nP = len(values)

    # Signal: RAW COUNTS; None entries (skipped/infeasible points) → NaN + masked, never deleted.
    counts_raw = result.get("counts") or []
    intensity = np.array([np.nan if c is None else float(c) for c in counts_raw], dtype="float64")
    if len(intensity) != nP:                       # defensive: pad/trim to the axis length
        intensity = np.resize(intensity, nP) if nP else intensity

    vm = result.get("valid_mask_1")
    if vm is None:
        mask = ~np.isnan(intensity)
    else:
        mask = np.asarray(vm, dtype=bool)
        if len(mask) != nP:
            mask = np.resize(mask, nP)
    mask = mask & ~np.isnan(intensity)
    for idx in _skipped_indices(result.get("skipped_points")):
        if 0 <= idx < nP:
            mask[idx] = False

    point_flags = [scan.PointFlag(int(i), "invalid_point", "skipped or infeasible (valid_mask_1)")
                   for i in np.nonzero(~mask)[0]]

    h = _num(params.get("H")) or 0.0
    k = _num(params.get("K")) or 0.0
    l = _num(params.get("L")) or 0.0
    det = pol.detectors[0]
    spectra = {det.detector_id: scan.Spectrum(
        detector_id=det.detector_id, q=[h, k, l], q_source="reported",
        intensity=intensity, variance=intensity.copy(), mask=mask.astype(bool),
        point_flags=point_flags, spectrum_flags=[], resolution=None)}

    scan_type = _classify(variable_1, params)
    var = str(variable_1 or "").lower()
    if var == "deltae":
        scan_axes = [axes.energy_axis(values), *axes.constant_q_axes(h, k, l, nP)]
        raw_abscissa_kind = "energy"
    else:
        kind = _AXIS_KIND.get(var, "angle")
        scan_axes = [scan.ScanAxis(name=str(variable_1 or "axis"), kind=kind, role="scanned",
                                   units=_AXIS_UNITS.get(kind, ""), values=values)]
        raw_abscissa_kind = "angle"                # RAW_ABSCISSA_KINDS has no q_* member

    # Monitor == the simulated source budget per point (number_neutrons), constant. Stored, not
    # divided in — this is the count TAVI shot at each point, so it is the honest normalizer.
    nn = _num(params.get("number_neutrons")) or _num(launch.get("number_neutrons")) or 0.0
    monitor = np.full(nP, float(nn), dtype="float64")
    time = np.zeros(nP, dtype="float64")

    prov: dict = {}                                # per-field provenance: "payload" | "card"
    # Temperature: payload `temperature` is the true setpoint; card [temperature_rule] setpoint fills
    # the gap for legacy JSONs (TAVI historically had no sample sensor).
    rule = pol.temperature_rule or {}
    t_payload = _num(params.get("temperature"))
    if t_payload is not None:
        t_set, prov["temperature"] = float(t_payload), "payload"
    else:
        t_set, prov["temperature"] = float(rule.get("setpoint", 300.0)), "card"
    temperature = scan.Temperature(setpoint=np.full(nP, t_set, dtype="float64"),
                                   sensors={}, valid_mask=np.ones(nP, dtype=bool), snap_applied=False)

    kfix, fx = _kfix(params)
    geometry = scan.Geometry(
        fixed_mode={1: "ki", 2: "kf"}.get(fx),
        fixed_value=(float(kfix) if kfix is not None else None),
        mono={"d_spacing": _dspacing(params.get("monocris"), pol.calibration or {}, "dm"),
              "crystal": params.get("monocris")},
        ana={"d_spacing": _dspacing(params.get("anacris"), pol.calibration or {}, "da"),
             "crystal": params.get("anacris")},
        collimation=_collimation_alfs(params.get("collimation"), pol.calibration or {}), filter=None)

    cn_params = _cn_params(params, card, prov)

    # sample_key: the payload's sample identity. Recorded for collation (which keys on
    # experiment_id today); a per-sample card section is a future TODO, not wired here.
    sample_key = params.get("sample_key")
    prov["sample_key"] = "payload" if sample_key else "absent"

    # Provenance → QC-visible signal: one info flag naming the CN inputs that fell back to card
    # constants (empty flag list ⇒ every instrument-truth field came from the payload).
    fallback_fields = sorted(k for k, v in prov.items() if v == "card")
    scan_flags = []
    if fallback_fields:
        scan_flags.append(scan.Flag(
            "card_fallback", "info",
            "instrument truth from card constants (payload lacked): " + ", ".join(fallback_fields)))

    # Background truth firewall: safe provenance onto the record, planted numerics redacted out of the
    # stored payload (module docstring). NEITHER a resolved `result.metadata.background` nor a
    # requested `launch.background` (older TAVI / real data) → neither key changes. A launch spec
    # alone (interrupted job) is still redacted, and gets a fingerprint-less provenance so collation
    # fails closed on it rather than pooling an unresolved background as background-free.
    bg_block = _background_block(result)
    bg_launch = _launch_background_present(data)
    bg_present = bg_block is not None or bg_launch
    stored_payload = _redact_background(data, bg_block) if bg_present else data

    raw_header = {"tavi": stored_payload, "params": cn_params, "provenance": prov,
                  "sample_key": sample_key}
    if bg_present:
        launch_source = (data.get("launch") or {}).get("background_source") if bg_launch else None
        raw_header["background_provenance"] = _background_provenance(
            bg_block, launch_source=launch_source)
    source = scan.Source(
        file=p.name, byte_range=None, line_range=None, plugin=name, plugin_version=version,
        format_version=pol.format_version, schema_version=scan.SCHEMA_VERSION)

    experiment_id = (pol.sample.get("experiment_id") or pol.sample.get("name") or "tavi")

    record = scan.ScanRecord(
        experiment_id=str(experiment_id),
        scan_number=_scan_number(data.get("job_id")),
        scan_type=scan_type, scan_type_source="inferred",
        scan_axes=scan_axes, spectra=spectra,
        normalization=scan.Normalization(
            counting_mode=pol.counting_mode, monitor=monitor, time=time,
            preset=float(nn), monitor_correction=False),
        temperature=temperature,
        environment=scan.Environment(field=None, logs={}),
        geometry=geometry,
        energy_calibration=scan.EnergyCalibration(
            raw_abscissa_kind=raw_abscissa_kind, transform=None, elastic_zero_offset=None,
            status="absolute"),                    # fixed-ki/kf simulator: exact absolute meV axis
        scan_flags=scan_flags, raw_header=raw_header, source=source)
    return [record]


def _skipped_indices(skipped) -> list[int]:
    """Accept either a list of point indices or a list of {"index": i, ...} descriptors."""
    out: list[int] = []
    for s in skipped or []:
        if isinstance(s, dict):
            i = s.get("index", s.get("point"))
            if i is not None:
                out.append(int(i))
        else:
            try:
                out.append(int(s))
            except (TypeError, ValueError):
                continue
    return out


# ---------------------------------------------------------------------------
# emit_file — the inverse (TAVI JSON) for the round-trip gate
# ---------------------------------------------------------------------------
def emit_file(run, card: Card) -> bytes:
    """Serialize one synthetic scan back to TAVI `GET /scan/{id}/data` JSON bytes.

    TAVI stores one scan per file, so this emits `run.scans[0]` (a deltaE measurement). Duck-typed on
    the run_builder `Run`/`RunScan` shape: `scan_number`, `energy`, single `det0` detector with `q` +
    `counts`, `monitor`. `parse_file` inverts it exactly (Gate A).
    """
    sc = run.scans[0]
    d = sc.detectors[0]
    h, k, l = float(d.q[0]), float(d.q[1]), float(d.q[2])
    energy = np.asarray(sc.energy, dtype="float64")
    counts = np.asarray(d.counts, dtype="float64")
    nP = len(energy)
    nn = int(round(float(np.asarray(sc.monitor)[0]))) if nP else 0

    parameters = {
        "H": h, "K": k, "L": l, "deltaE": 0.0,
        "Ki": 2.662, "Ei": 14.7, "Kf": 2.662, "Ef": 14.7,
        "K_fixed": "Kf Fixed", "fixed_E": 14.7,
        "monocris": "pg002", "anacris": "pg002",
        "lattice_a": 3.94, "lattice_b": 3.94, "lattice_c": 3.94,
        "lattice_alpha": 90.0, "lattice_beta": 90.0, "lattice_gamma": 90.0,
        "collimation": {"alpha_1": "40", "alpha_2": ["40"], "alpha_3": "30", "alpha_4": "30"},
        "number_neutrons": nn,
    }
    result = {
        "mode": "1D", "variable_1": "deltaE", "variable_2": None,
        "total_counts": float(counts.sum()), "max_counts": float(counts.max()) if nP else 0.0,
        "output_folder": "", "skipped_points": [],
        "scan_values_1": energy.tolist(), "scan_values_2": None,
        "valid_mask_1": [True] * nP,
        "counts": counts.tolist(), "counts_grid": None, "metadata": {},
    }
    launch = {
        "scan_command1": getattr(sc, "command", f"deltaE {energy[0] if nP else 0} "
                                 f"{energy[-1] if nP else 0} 0.5"),
        "scan_command2": "", "number_neutrons": nn, "isolated": True, "parameters": parameters,
    }
    obj = {
        "job_id": f"j-{int(sc.scan_number):04d}", "source": "api", "state": "done",
        "submitted_at": 0.0, "started_at": 0.0, "finished_at": 0.0,
        "progress": {"done": nP, "total": nP}, "error": None,
        "launch": launch, "result": result,
    }
    return (json.dumps(obj) + "\n").encode("utf-8")

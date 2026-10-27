"""Calibration wiring (the retired unification plan §3, git history; current contract: docs/WORKSPACE_AND_CLI.md) — the session layer that finally calls the orphaned
`isar/calibrate/`.

Card-driven by the `[calibration]` policy section. Three methods cover the known cases:

- `bragg_companion` (S30 supplementary): the K≈0 elastics live in a companion raw `.spec`. Fit them
  into a scan-keyed energy-zero series + per-slit resolution, and build a scan→slit map from the
  same companion (via the validated `spec_base` reader, NOT `spec_s30_raw.parse_file` — R2). The
  measurement's own slit is recovered from that map (closing the gap where the gold check took the
  slit from its CSV).
- `elastic_records` (S30 raw): the elastics arrive in-stream as parsed calibration records.
- `cooper_nathans` (IN8/TAS): stateless per-scan analytical resolution; center = 0 (absolute axis).
- `tabulated_elastic` (shared; BL43LXU switches it on): a measured resolution curve per detector and
  setting from the experiment's saved resolution folder, else the card's `resolution_folder`; per scan the
  setting ladder and per detector the elastic-line zero (`refresh_tabulated`, keyed `exp:scan` in
  `card.state.tabulated_calibration`, reused only while its `inputs_key` is unchanged).

`refresh` updates `card.state` (energy_zero_series + resolution_versions). `calibration_view`
rebuilds a run-level calibration object from state — state, not memory, is authoritative, so a
restart is a non-event. `resolve` produces the per-scan (center, widths) a fit needs, or flags the
scan held when no zero is available (never E0 = 0).
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np

from isar.analyze.resolution import TabulatedResolution, file_stamp
from isar.calibrate import bragg
from isar.calibrate import tabulated
from isar.calibrate import tas
from isar.calibrate.energy_zero import (
    _DEFAULT_RESOLUTION,
    EnergyCalibration as RunEnergyCalibration,   # alias: schema.scan also has EnergyCalibration
    ZeroSeries,
    build_energy_calibration,
)
from isar.intake.curves import LoadedCurves, load_curves, resolution_location
from isar.intake.inputs import calibration_inputs_key
from isar.parsers import metadata, sarf, spec_base
from isar.schema import axes
from isar.schema.card import Card
from isar.schema.scan import Flag
from isar.session.state import SessionState, canonical_hash, content_hash

log = logging.getLogger(__name__)

# Default P-group / index for the S30 per-scan slit in the raw .spec (matches bragg._SLIT_PINDEX).
_DEFAULT_SLIT_PGROUP = 17
_DEFAULT_SLIT_PINDEX = 4
_S30_MULTIWORD = ("Heat Stage T1", "Heat Stage T2", "Heat Stage T3")


# ===========================================================================
# refresh — consume calibration events + companion files into card.state
# ===========================================================================
def refresh(card: Card, session: SessionState, *, calibration_records=None,
            companion_files=None) -> list[str]:
    method = (card.policy.calibration or {}).get("method")
    if not method:
        return []
    if method == "bragg_companion":
        return _refresh_bragg_companion(card, companion_files or [])
    if method == "elastic_records":
        return _refresh_elastic_records(card, calibration_records or [])
    if method == "cooper_nathans":
        return _refresh_cooper_nathans(card)
    if method == "tabulated_elastic":      # built from the measurement records: refresh_tabulated
        return []
    return [f"calibration: unknown method {method!r} — no calibration state built"]


def _slit_map_from_spec(spec_path, pgroup: int, pindex: int) -> dict[int, int]:
    """scan_number -> slit for EVERY block in a raw .spec (phonon + elastic), from #P<pgroup>[pindex].

    Uses the validated shared reader; the measurement scan's slit is read from here, not the gold CSV.
    """
    _h, blocks = spec_base.read_spec(spec_path, multiword_columns=list(_S30_MULTIWORD))
    out: dict[int, int] = {}
    for b in blocks:
        row = b.P_groups.get(pgroup)
        if row and len(row) > pindex:
            try:
                out[int(b.scan_number)] = int(round(float(row[pindex])))
            except (TypeError, ValueError):
                continue
    return out


def _refresh_bragg_companion(card: Card, companion_files) -> list[str]:
    cfg = card.policy.calibration or {}
    analyzer = cfg.get("analyzer_column", "Ana7")
    k_max = float(cfg.get("k_max", 0.05))
    pgroup = int(cfg.get("slit_p_group", _DEFAULT_SLIT_PGROUP))
    pindex = int(cfg.get("slit_p_index", _DEFAULT_SLIT_PINDEX))

    elastic_scans: list = []
    slit_by_scan: dict[int, int] = {}
    used: list[str] = []
    for f in companion_files:
        try:
            elastic_scans.extend(bragg.read_elastic_scans(f, analyzer=analyzer, k_max=k_max))
            slit_by_scan.update(_slit_map_from_spec(f, pgroup, pindex))
            used.append(Path(f).name)
        except (ValueError, OSError) as exc:      # a companion we can't read is noted, not fatal
            used.append(f"{Path(f).name} (skipped: {exc})")

    if not elastic_scans:
        return [f"calibration(bragg_companion): no elastic scans found in {used or 'companions'}"]

    calib = build_energy_calibration(elastic_scans, key="scan_number")
    zero_points = [{"key": float(k), "e0": float(v)}
                   for k, v in zip(calib.zeros.keys.tolist(), calib.zeros.vals.tolist())]
    res_by_slit = {str(s): [float(g), float(l)] for s, (g, l) in calib.resolution_by_slit.items()}
    version_body = {
        "method": "bragg_companion",
        "analyzer": analyzer,
        "resolution_by_slit": res_by_slit,
        "slit_by_scan": {str(k): int(v) for k, v in slit_by_scan.items()},
        "default_resolution": list(_DEFAULT_RESOLUTION),
        "source_files": used,
        "n_elastic": len(elastic_scans),
    }
    card.state.energy_zero_series = zero_points
    changed = _append_version_if_changed(card, version_body)
    span = (f"[{calib.zeros.keys.min():.0f},{calib.zeros.keys.max():.0f}]"
            if len(calib.zeros) else "(empty)")
    tag = "new version" if changed else "unchanged"
    return [f"calibration(bragg_companion): {len(calib.zeros)} zeros {span}, "
            f"slits {sorted(calib.resolution_by_slit)}, {len(slit_by_scan)} scan slits ({tag})"]


def _refresh_elastic_records(card: Card, calibration_records) -> list[str]:
    """S30-raw path: build ElasticScan objects from in-stream calibration records, same downstream."""
    cfg = card.policy.calibration or {}
    analyzer = cfg.get("analyzer_column", "Ana7")
    scans: list = []
    for rec in calibration_records:
        sp = rec.spectra.get(analyzer.lower()) or next(iter(rec.spectra.values()), None)
        if sp is None:
            continue
        from isar.schema import axes
        x = np.asarray(axes.scanned_axes(rec)[0].values, dtype=float)
        scans.append(bragg.ElasticScan(
            scan_number=int(rec.scan_number), time=float(rec.scan_number), K=0.0,
            slit=0, energy=x, counts=np.asarray(sp.intensity, dtype=float)))
    if not scans:
        return ["calibration(elastic_records): no elastic records this batch"]
    calib = build_energy_calibration(scans, key="scan_number")
    card.state.energy_zero_series = [{"key": float(k), "e0": float(v)}
                                     for k, v in zip(calib.zeros.keys.tolist(), calib.zeros.vals.tolist())]
    body = {"method": "elastic_records", "analyzer": analyzer,
            "resolution_by_slit": {str(s): [float(g), float(l)]
                                   for s, (g, l) in calib.resolution_by_slit.items()},
            "slit_by_scan": {}, "default_resolution": list(_DEFAULT_RESOLUTION)}
    _append_version_if_changed(card, body)
    return [f"calibration(elastic_records): {len(calib.zeros)} zeros from {len(scans)} elastic records"]


def _refresh_cooper_nathans(card: Card) -> list[str]:
    """Stateless per-scan analytical resolution (IN8). Record one provenance version only."""
    body = {"method": "cooper_nathans", "per_scan": True}
    changed = _append_version_if_changed(card, body)
    return ["calibration(cooper_nathans): per-scan analytical resolution "
            + ("(version recorded)" if changed else "(unchanged)")]


# ---------------------------------------------------------------------------
# tabulated_elastic — the setting ladder and the per-detector elastic zero, per measurement scan
# ---------------------------------------------------------------------------
NON_RAW_REASON = tabulated.NON_RAW_REASON
ZERO_CROSSCHECK_MEV = 0.3       # above the 0.1 meV agreement seen; the beamline centroid has background and phonon tails, so never the zero


def _flag_row(flag: Flag) -> dict:
    return {"type": flag.type, "severity": flag.severity, "reason": flag.reason}


def _num(v) -> float | None:
    """A finite float for the JSON state, else None (NaN would not survive a strict reader)."""
    v = float(v)
    return v if math.isfinite(v) else None


def _calibrate_detector(x, det, spectrum, library, setting, problem, identified, accepted=False) -> dict:
    """One detector's entry of the state map: its zero and elastic fit, or why it is held."""
    why = tabulated.count_hold(spectrum, accepted=accepted)   # the info notice is print rounding: raw, fitted
    if why == NON_RAW_REASON:
        return {"held_reason": NON_RAW_REASON, "flags": []}      # parsed and shown, never fitted
    if why is not None:                                   # _i counts off an integer: held until accepted
        return {"held_reason": f"{det}: {why}", "flags": []}
    why = tabulated.curve_need(library, det, setting, problem)
    if why is None and not identified:                   # its file was unreadable between the load and its identity
        why = f"the curve at {setting} is unreadable for its identity (its file changed or was locked while read)"
    if why is not None:
        return {"held_reason": f"{det}: {why}",
                "flags": [_flag_row(Flag("missing_resolution", "warning",
                                         f"{det}: {why}; parsed and shown, not fitted"))]}
    curve = library.get(det, setting)
    result = tabulated.zero_for(x, spectrum, curve)
    f = result.fit
    zero = _num(result.centre)
    flags = [_flag_row(fl) for fl in result.flags]
    centroid = (spectrum.facts.get("elastic_centroid_meV") or {}).get("value")
    if zero is not None and isinstance(centroid, float) and abs(zero - centroid) > ZERO_CROSSCHECK_MEV:
        reason = (f"{det}: ISAR zero {zero:.3f} meV ({result.source}) differs from the beamline elastic "
                  f"centroid {centroid:.3f} meV by more than {ZERO_CROSSCHECK_MEV:g} meV")
        flags.append(_flag_row(Flag("zero_disagrees_with_beamline", "warning", reason)))
    return {"zero": zero, "zero_source": result.source,
            "fit": {"centre": _num(f.centre), "amplitude": _num(f.amplitude),
                    "background": _num(f.background), "deviance_per_point": _num(f.deviance_per_point),
                    "n_points": f.n_points, "significant": bool(f.significant)},
            "flags": flags}


def _declared_slit(rec) -> str | None:
    slit = (rec.facts.get("slit") or {}).get("value")
    return None if slit is None else (metadata.normalise_slit(slit) or str(slit))


def _accepted(rec) -> list:
    """The analyzers whose counts the user accepted (the effective record's ``counts_accepted`` fact)."""
    return list(((rec.facts or {}).get("counts_accepted") or {}).get("value") or [])


def _calibrate_scan(rec, library, fp, problem, folder, pattern, chash, identity, inputs_key=None) -> dict:
    x = axes.scanned_axes(rec)[0].values
    declared = _declared_slit(rec)
    choice = tabulated.choose_setting(x, rec.spectra, library, declared)
    accepted = set(_accepted(rec))
    detectors = {}
    for det, sp in rec.spectra.items():
        d = _calibrate_detector(x, det, sp, library, choice.setting, problem, (det, choice.setting) in identity,
                                det in accepted)
        if "held_reason" not in d:           # the curve its zero was fitted with, by content
            d["curve"] = identity[(det, choice.setting)]
        detectors[det] = d
    return {"content_hash": chash, "inputs_key": inputs_key, "library": fp,
            "folder": None if folder is None else str(folder),
            "pattern": pattern, "declared": declared,
            "setting": choice.setting, "setting_source": choice.source,
            "setting_basis": tabulated.setting_basis(choice),
            "deviances": {k: _num(v) for k, v in choice.deviances.items()},
            "comparison_set": list(choice.comparison_set),
            "flags": [_flag_row(f) for f in choice.flags],
            "detectors": detectors}


def _curves(card: Card, base_dir, loaded: LoadedCurves | None) -> LoadedCurves:
    """The given curve snapshot, else the folder in force loaded now."""
    if loaded is not None:
        return loaded
    folder, pattern, _source = resolution_location(card, base_dir)
    return load_curves(folder, pattern)


def _inputs_key(record, data_hash: str, loaded: LoadedCurves) -> str:
    return calibration_inputs_key(
        data_revision=data_hash, folder=None if loaded.folder is None else str(loaded.folder),
        pattern=loaded.pattern, library=loaded.fingerprint, declared_slit=_declared_slit(record),
        analyzers=sorted(record.spectra), counts_accepted=_accepted(record))


def tabulated_key(card: Card, record, *, data_hash: str, base_dir=None, loaded: LoadedCurves | None = None) -> str:
    """What this scan's calibration is computed from: data revision, folder, pattern, library, declared
    slit, analyzers and accepted counts (C-effective ``calibration_inputs_key``)."""
    return _inputs_key(record, data_hash, _curves(card, base_dir, loaded))


def calibrate_scan_state(card: Card, record, *, data_hash: str, base_dir=None,
                         loaded: LoadedCurves | None = None) -> dict:
    """One scan's `tabulated_elastic` state, computed now against ``loaded`` (else the folder in force);
    writes nothing. An odd scan gets an ``error`` state with the reason, never an exception."""
    loaded = _curves(card, base_dir, loaded)
    key = _inputs_key(record, data_hash, loaded)
    try:
        return _calibrate_scan(record, loaded.library, loaded.fingerprint, loaded.problem, loaded.folder,
                               loaded.pattern, data_hash, loaded.identity, key)
    except Exception as exc:  # noqa: BLE001 — one odd scan is held with the reason, never fatal
        log.exception("calibration(tabulated_elastic): scan %s:%s failed", record.experiment_id, record.scan_number)
        return {"content_hash": data_hash, "inputs_key": key, "library": loaded.fingerprint,
                "error": f"{type(exc).__name__}: {exc}", "flags": [], "detectors": {}}


def refresh_tabulated(card: Card, records, *, base_dir=None, data_hashes=None,
                      loaded: LoadedCurves | None = None) -> list[str]:
    """Build the `tabulated_elastic` state for these measurement records; no-op for any other method.

    Per scan the slit setting (declared, else inferred, with its candidate deviances and flags) and
    per detector the elastic zero (or the reason it is held) go into
    ``card.state.tabulated_calibration["exp:scan"]``. A scan is recomputed only when its calibration
    inputs key (data revision, folder, pattern, library, declared slit, analyzers, accepted counts)
    changed; the resolution version records only the method, folder, pattern and library fingerprint,
    so the state grows linearly with the scans. ``data_hashes`` (scan key -> the parsed record's
    ``content_hash``) is the data revision when the records are effective records; ``loaded`` is the
    run's curve snapshot. Never writes the policy."""
    cfg = card.policy.calibration or {}
    if cfg.get("method") != "tabulated_elastic":
        return []
    lines: list[str] = []
    if cfg.get("zero_fallback", "file_axis") != "file_axis":
        lines.append(f"calibration(tabulated_elastic): zero_fallback {cfg['zero_fallback']!r} is not "
                     "supported; an analyzer without a significant elastic line uses the file's own axis")
    loaded = _curves(card, base_dir, loaded)
    folder, fp, problem = loaded.folder, loaded.fingerprint, loaded.problem
    _append_version_if_changed(card, {"method": "tabulated_elastic", "folder": None if folder is None else str(folder),
                                      "pattern": loaded.pattern, "library": fp})
    if problem:
        lines.append(f"calibration(tabulated_elastic): {problem}; every analyzer is held for want of a curve")
    state = card.state.tabulated_calibration
    built = 0
    for rec in records:
        if rec.scan_type != "measurement":
            continue
        key = f"{rec.experiment_id}:{rec.scan_number}"
        chash = data_hashes[key] if data_hashes and key in data_hashes else content_hash(rec)
        if tabulated.calibration_reusable(state.get(key), content_hash=chash, library=fp,
                                          inputs_key=_inputs_key(rec, chash, loaded)):
            continue
        state[key] = calibrate_scan_state(card, rec, data_hash=chash, loaded=loaded)
        built += 1
    if built:
        lines.append(f"calibration(tabulated_elastic): {built} scan(s) calibrated against "
                     f"{len(loaded.library)} resolution curve(s)")
    return lines


def scan_view(runcal: "RunCalibration", scan_key: str, state: dict | None,
              loaded: LoadedCurves | None = None) -> "RunCalibration":
    """One scan's frozen calibration: the run's view holding only this scan's state and, from the run's
    curve snapshot, the curves at its setting, so a pooled worker fits with them and never re-reads the folder."""
    curves, stamps = {}, {}
    if loaded is not None and state and "error" not in state and state.get("library") == loaded.fingerprint:
        dets = state.get("detectors") or {}
        for c in loaded.library:
            key = (c.detector_id, c.setting)
            if c.setting == state.get("setting") and c.detector_id in dets and key in loaded.stamps:
                curves[key], stamps[key] = c, loaded.stamps[key]
    return replace(runcal, tabulated={} if state is None else {scan_key: state}, curves=curves,
                   curve_stamps=stamps)


def _append_version_if_changed(card: Card, body: dict) -> bool:
    """Append a resolution version only when its content differs from the latest (recalibration is
    versioned and first-class, but a no-op re-run must not grow the list)."""
    versions = card.state.resolution_versions
    sig = canonical_hash(body)
    if versions and versions[-1].get("_sig") == sig:
        return False
    entry = dict(body)
    entry["version"] = f"v{len(versions) + 1}"
    entry["_sig"] = sig
    versions.append(entry)
    return True


# ===========================================================================
# calibration_view — rebuild the run-level calibration from card.state
# ===========================================================================
@dataclass
class RunCalibration:
    method: str
    zeros: "ZeroSeries | None"
    resolution_by_slit: dict          # int slit -> (gfw, lfw)
    slit_by_scan: dict                # int scan -> int slit
    default_resolution: tuple
    tabulated: dict = field(default_factory=dict)   # "exp:scan" -> tabulated_elastic state (a copy)
    # (detector, setting) -> TabulatedResolution from the run's curve snapshot (a scan view, `scan_view`)
    curves: dict = field(default_factory=dict)
    curve_stamps: dict = field(default_factory=dict)   # the same keys: each file's stamp as the snapshot read it

    def zero(self, scan_number) -> float | None:
        return self.zeros.interpolate(scan_number) if self.zeros is not None else None

    def slit_for(self, scan_number):
        return self.slit_by_scan.get(int(scan_number))

    def resolution_for_slit(self, slit) -> tuple:
        try:
            return self.resolution_by_slit.get(int(slit), self.default_resolution)
        except (TypeError, ValueError):
            return self.default_resolution


def calibration_view(card: Card) -> RunCalibration:
    method = (card.policy.calibration or {}).get("method", "")
    versions = card.state.resolution_versions
    latest = versions[-1] if versions else {}
    res_by_slit = {int(k): tuple(v) for k, v in (latest.get("resolution_by_slit") or {}).items()}
    slit_by_scan = {int(k): int(v) for k, v in (latest.get("slit_by_scan") or {}).items()}
    default_res = tuple(latest.get("default_resolution") or _DEFAULT_RESOLUTION)
    zeros = None
    if card.state.energy_zero_series:
        zeros = ZeroSeries.build([(p["key"], p["e0"]) for p in card.state.energy_zero_series])
    return RunCalibration(method=method, zeros=zeros, resolution_by_slit=res_by_slit,
                          slit_by_scan=slit_by_scan, default_resolution=default_res,
                          tabulated=dict(card.state.tabulated_calibration or {}))


# ===========================================================================
# resolve — the per-scan (center, resolution) a fit needs
# ===========================================================================
@dataclass
class CalibResolution:
    center: float | None
    gauss_fwhm: float
    lorentz_fwhm: float
    zero_source: str | None
    resolution_source: str
    resolution_version: str
    held_reason: str | None = None
    # tabulated_elastic only: the curve the fit convolves with, where it came from (folder, pattern,
    # library fingerprint, detector, setting: enough to rebuild it), and what the inspector shows.
    curve: TabulatedResolution | None = None
    reference: dict | None = None
    calibration: dict | None = None
    fingerprint: str | None = None

    @property
    def held(self) -> bool:
        return self.held_reason is not None

    def add_flag(self, flag: Flag) -> None:
        """A flag raised at fit time (the inspector lists it with the calibration's own)."""
        if self.calibration is not None:
            self.calibration["flags"].append(_flag_row(flag))

    def summary(self) -> dict:
        out = {"center": self.center, "gauss_fwhm": self.gauss_fwhm,
               "lorentz_fwhm": self.lorentz_fwhm, "zero_source": self.zero_source,
               "resolution_source": self.resolution_source,
               "resolution_version": self.resolution_version, "held_reason": self.held_reason}
        if self.curve is not None:          # persisted with the fit so a reopened workspace rebuilds it
            out["tabulated"] = self.reference
            out["calibration"] = self.calibration
        return out

    def provenance(self) -> dict:
        """What a ledger entry records about this calibration (every terminal outcome stamps it)."""
        out = {"resolution_version": self.resolution_version}
        if self.calibration is not None:
            out["calibration"] = self.calibration
        if self.fingerprint is not None:
            out["calibration_fingerprint"] = self.fingerprint
        return out


def _scan_state(runcal: RunCalibration, record) -> dict | None:
    if runcal.method != "tabulated_elastic":
        return None
    return runcal.tabulated.get(f"{record.experiment_id}:{record.scan_number}")


def calibration_block(runcal: RunCalibration, record, detector_id) -> dict | None:
    """The tabulated calibration of one unit as the inspector shows it (setting and how it was
    chosen, candidate deviances, zero and its source, elastic-fit numbers, flags), or None."""
    scan_state = _scan_state(runcal, record)
    if scan_state is None or "error" in scan_state:
        return None
    d = scan_state["detectors"].get(detector_id) or {}
    return {"setting": scan_state["setting"], "setting_source": scan_state["setting_source"],
            "deviances": dict(scan_state["deviances"]), "comparison_set": list(scan_state["comparison_set"]),
            "zero": d.get("zero"), "zero_source": d.get("zero_source"), "elastic_fit": d.get("fit"),
            "held_reason": d.get("held_reason"),
            "flags": [*scan_state["flags"], *d.get("flags", [])]}


def calibration_fingerprint(runcal: RunCalibration, record, detector_id) -> str | None:
    """What a fit of this unit depends on in the tabulated calibration (library, setting, zero and its
    source, or the hold); None for every other method. The driver refits when it changes. A declared slit
    depends on the unit's own curve, not the whole library: another analyzer's curve changes nothing."""
    scan_state = _scan_state(runcal, record)
    if scan_state is None:
        return None
    d = (scan_state.get("detectors") or {}).get(detector_id) or {}
    curve = d.get("curve") if scan_state.get("setting_basis") == "declared" and not d.get("held_reason") else None
    library = {"curve": curve.get("sha256")} if curve else {"library": scan_state.get("library")}
    return canonical_hash({**library, "setting": scan_state.get("setting"),
                           "zero": d.get("zero"), "zero_source": d.get("zero_source"),
                           "held": d.get("held_reason"), "error": scan_state.get("error")})


def _resolve_tabulated(record, detector_id, runcal: RunCalibration, version: str) -> CalibResolution:
    """A measured curve and an elastic-fit zero per detector; anything missing holds the unit (never E0 = 0)."""
    scan_state = _scan_state(runcal, record)
    block = calibration_block(runcal, record, detector_id)

    def held(why: str) -> CalibResolution:
        return CalibResolution(center=None, gauss_fwhm=runcal.default_resolution[0],
                               lorentz_fwhm=runcal.default_resolution[1], zero_source=None,
                               resolution_source="none", resolution_version=version, held_reason=why,
                               calibration=block, fingerprint=calibration_fingerprint(runcal, record, detector_id))

    if scan_state is None:
        return held("no tabulated calibration for this scan (run `isar run` to build it)")
    if "error" in scan_state:
        return held(f"tabulated calibration failed for this scan ({scan_state['error']})")
    d = scan_state["detectors"].get(detector_id)
    if d is None:
        return held(f"{detector_id}: no tabulated calibration for this analyzer")
    if d.get("held_reason"):
        return held(d["held_reason"])
    ref = {"folder": scan_state["folder"], "pattern": scan_state["pattern"],
           "fingerprint": scan_state["library"], "detector": detector_id, "setting": scan_state["setting"]}
    try:
        # The run's snapshot when the scan view carries it (a pooled worker never re-reads the folder), with
        # the stamp the snapshot took of its file. shortcut: a curve outside the snapshot is stamped from
        # disk here; no BL43 fit reaches that except when its file vanished before the snapshot stamped it.
        key = (detector_id, scan_state["setting"])
        curve = runcal.curves.get(key)
        if curve is not None:
            stamp = runcal.curve_stamps[key]
        else:
            curve = sarf.curve_from_reference(ref)
            stamp = file_stamp(Path(ref["folder"]) / curve.source)
        # the curve's own file and stamp: what the read-only facade rebuilds it from (G16, no parser)
        ref = {**ref, "file": curve.source, "stamp": stamp}
    except (OSError, ValueError, KeyError, re.error) as exc:
        log.warning("calibration(tabulated_elastic): %s: %s", detector_id, exc)
        return held(f"{detector_id}: {exc}")
    return CalibResolution(
        center=float(d["zero"]), gauss_fwhm=float(curve.fwhm), lorentz_fwhm=0.0,
        zero_source=d["zero_source"],
        resolution_source=f"tabulated slit {scan_state['setting']} ({scan_state['setting_source']})",
        resolution_version=version, curve=curve, reference=ref, calibration=block,
        fingerprint=calibration_fingerprint(runcal, record, detector_id))


def resolve(record, detector_id, card: Card, runcal: RunCalibration) -> CalibResolution:
    method = (card.policy.calibration or {}).get("method", "")
    version = runcal_version(card)

    if method == "tabulated_elastic":       # before the absolute branch: BL43LXU is energy_status needs_zero
        return _resolve_tabulated(record, detector_id, runcal, version)

    if method == "cooper_nathans" or card.policy.energy_status == "absolute":
        gfw = float(tas.cn_energy_fwhm(record, w=0.0))
        return CalibResolution(center=0.0, gauss_fwhm=gfw, lorentz_fwhm=0.0,
                               zero_source="absolute", resolution_source="cooper_nathans",
                               resolution_version=version)

    # Measured (S30): the zero comes from the series; a missing zero -> held (never E0 = 0).
    center = runcal.zero(record.scan_number)
    if center is None:
        return CalibResolution(center=None, gauss_fwhm=runcal.default_resolution[0],
                               lorentz_fwhm=runcal.default_resolution[1], zero_source=None,
                               resolution_source="none", resolution_version=version,
                               held_reason="no energy zero in calibration coverage")
    slit = runcal.slit_for(record.scan_number)
    gfw, lfw = runcal.resolution_for_slit(slit)
    res_source = f"measured_slit_{slit}" if slit in runcal.resolution_by_slit else "default"
    return CalibResolution(center=float(center), gauss_fwhm=float(gfw), lorentz_fwhm=float(lfw),
                           zero_source="interpolated", resolution_source=res_source,
                           resolution_version=version)


def runcal_version(card: Card) -> str:
    versions = card.state.resolution_versions
    return versions[-1].get("version", "v0") if versions else "v0"

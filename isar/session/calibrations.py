"""Calibration wiring (UNIFICATION_DESIGN §3) — the session layer that finally calls the orphaned
`isar/calibrate/`.

Card-driven by the `[calibration]` policy section. Three methods cover the known cases:

- `bragg_companion` (S30 supplementary): the K≈0 elastics live in a companion raw `.spec`. Fit them
  into a scan-keyed energy-zero series + per-slit resolution, and build a scan→slit map from the
  same companion (via the validated `spec_base` reader, NOT `spec_s30_raw.parse_file` — R2). The
  measurement's own slit is recovered from that map (closing the gap where the gold check took the
  slit from its CSV).
- `elastic_records` (S30 raw): the elastics arrive in-stream as parsed calibration records.
- `cooper_nathans` (IN8/TAS): stateless per-scan analytical resolution; center = 0 (absolute axis).

`refresh` updates `card.state` (energy_zero_series + resolution_versions). `calibration_view`
rebuilds a run-level calibration object from state — state, not memory, is authoritative, so a
restart is a non-event. `resolve` produces the per-scan (center, widths) a fit needs, or flags the
scan held when no zero is available (never E0 = 0).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from isar.calibrate import bragg
from isar.calibrate import tas
from isar.calibrate.energy_zero import (
    _DEFAULT_RESOLUTION,
    EnergyCalibration as RunEnergyCalibration,   # alias: schema.scan also has EnergyCalibration
    ZeroSeries,
    build_energy_calibration,
)
from isar.parsers import spec_base
from isar.schema.card import Card
from isar.session.state import SessionState, canonical_hash

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
                          slit_by_scan=slit_by_scan, default_resolution=default_res)


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

    @property
    def held(self) -> bool:
        return self.held_reason is not None

    def summary(self) -> dict:
        return {"center": self.center, "gauss_fwhm": self.gauss_fwhm,
                "lorentz_fwhm": self.lorentz_fwhm, "zero_source": self.zero_source,
                "resolution_source": self.resolution_source,
                "resolution_version": self.resolution_version, "held_reason": self.held_reason}


def resolve(record, detector_id, card: Card, runcal: RunCalibration) -> CalibResolution:
    method = (card.policy.calibration or {}).get("method", "")
    version = runcal_version(card)

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

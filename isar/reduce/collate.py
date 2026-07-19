"""Deterministic collation (§7.5): organize reduced `ScanRecord`s into a human-facing catalogue.

This is step 6's deterministic half — no LLM. It groups the per-analyzer spectra by the card's
collation key (S30: sample, rounded-Q, T), maps which Qs were measured (with gap detection, §7.1),
and rolls up what the pipeline flagged and removed. For S30 `merge = "separate"` the duplicates stay
separate (collation is a catalogue, not a physical combine); `merge_before_divide` is the primitive
the IN8/`merge` path will use — sum kept counts AND kept monitors, THEN divide (§7.5, never average
already-normalized rates), with no zero-count floor (D3).

The output feeds the report (`isar/report`): the catalogue, the coverage map, and the flag rollup.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

_COMPONENTS = ("h", "k", "l")


# ---------------------------------------------------------------------------
# Collation data model (lightweight; the heavy arrays stay referenced, not copied)
# ---------------------------------------------------------------------------
@dataclass
class SpectrumRef:
    """One analyzer spectrum located in (sample, Q, T) space, with refs back to its data."""
    experiment_id: str
    scan_number: int
    detector_id: str
    scan_type: str
    q: list                      # [h, k, l]
    q_key: tuple                 # integer grid key (stable for grouping)
    temperature: float
    t_key: float
    spectrum: object = None      # the schema.scan.Spectrum (arrays; not serialized in the catalogue)
    energy: object = None        # the scanned-axis values (ndarray)
    normalization: object = None # the record's Normalization (for monitor / miniscan_monitor)


@dataclass
class MergedSpectrum:
    """The combine-before-divide result for a `merge="merge"` group (raw counts kept; §7.5)."""
    energy: object               # the common energy grid (sorted)
    intensity: object            # ΣN — summed raw counts (NOT divided by the monitor)
    variance: object             # = ΣN (honest Poisson, no floor)
    monitor: object              # ΣM — summed monitor (divide downstream if you wish)
    mask: object                 # ΣM != 0
    members: list                # [(experiment_id, scan_number, detector_id), …] that contributed
    n_members: int = 0


def persist_merged_spectrum(data_dir, merged: MergedSpectrum) -> str:
    """Atomically persist a pooled spectrum and return its workspace-relative content address."""
    def _values(value):
        return value.tolist() if hasattr(value, "tolist") else list(value)

    payload = {
        "schema": "isar.pooled_spectrum/1",
        "energy": _values(merged.energy),
        "counts": _values(merged.intensity),
        "variance": _values(merged.variance),
        "monitor": _values(merged.monitor),
        "mask": [bool(v) for v in _values(merged.mask)],
        "members": [list(member) for member in merged.members],
        "n_members": int(merged.n_members),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    data_path = Path(data_dir)
    pooled_dir = data_path / "pooled"
    pooled_dir.mkdir(parents=True, exist_ok=True)
    target = pooled_dir / f"{digest}.json"
    if not target.exists():
        fd, temporary = tempfile.mkstemp(prefix=f".{digest}.", suffix=".tmp", dir=pooled_dir)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError as cleanup_error:
                log.warning("could not remove incomplete pooled spectrum %s: %s",
                            temporary, cleanup_error)
            raise
    return target.relative_to(data_path.parent).as_posix()


@dataclass
class CollationGroup:
    key: tuple
    key_fields: dict
    members: list
    merge: str = "separate"
    merged: MergedSpectrum | None = None   # the combined spectrum when merge="merge" (else None)


@dataclass
class Coverage:
    component: str               # "h" | "k" | "l": the dominant-varying Q component
    values: list                 # sorted unique measured values of that component
    gaps: list                   # (lo, hi) holes in the coverage


@dataclass
class Rollup:
    point_flags: dict = field(default_factory=dict)
    spectrum_flags: dict = field(default_factory=dict)
    scan_flags: dict = field(default_factory=dict)
    points_with_rejection: int = 0
    passes_rejected: int = 0
    counts_removed: float = 0.0


@dataclass
class Collation:
    groups: list
    coverage: Coverage
    rollup: Rollup
    merge: str = "separate"


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------
def _q_key(q, step: float) -> tuple:
    """Integer grid key on the rounding step — stable for dict grouping (avoids float-key drift)."""
    return tuple(int(round(float(qi) / step)) for qi in q)


def _t_key(t: float, t_round: float = 0.1) -> float:
    """Group temperatures into bins of width `t_round` (default 0.1 K preserves the S30 behavior).

    IN8 sets a coarser bin (regulation T drifts within/between scans, so same-setpoint repeats must
    still group together)."""
    tr = float(t_round) if t_round and t_round > 0 else 0.1
    return round(round(float(t) / tr) * tr, 6)


def _scanned_energy(record):
    scanned = [a for a in record.scan_axes if a.role == "scanned"]
    return scanned[0].values if scanned else None


def _spectrum_refs(record, step: float, t_round: float) -> list:
    t = float(record.temperature.setpoint[0]) if len(record.temperature.setpoint) else float("nan")
    energy = _scanned_energy(record)
    refs = []
    for det_id, sp in record.spectra.items():
        refs.append(SpectrumRef(
            experiment_id=record.experiment_id, scan_number=record.scan_number,
            detector_id=det_id, scan_type=record.scan_type, q=list(sp.q),
            q_key=_q_key(sp.q, step), temperature=t, t_key=_t_key(t, t_round),
            spectrum=sp, energy=energy, normalization=record.normalization))
    return refs


def _auto_energy_tol(energy) -> float:
    """A binning tolerance ~¼ of the median grid step (so distinct points stay apart, repeats merge)."""
    uniq = np.unique(np.round(np.asarray(energy, dtype="float64"), 9))
    if len(uniq) < 2:
        return 1e-6
    return max(0.25 * float(np.median(np.diff(uniq))), 1e-9)


def _merge_group(members, *, energy_tol: float = 0.0) -> MergedSpectrum:
    """Combine-before-divide: align members on a common energy grid, then SUM counts and monitors.

    The merged signal stays RAW COUNTS (intensity=ΣN, variance=ΣN) with the summed monitor preserved —
    never average already-normalized rates (§7.5). Points whose summed monitor is zero are masked.
    """
    Es, Ns, Ms = [], [], []
    for ref in members:
        e = np.asarray(ref.energy, dtype="float64")
        n = np.asarray(ref.spectrum.intensity, dtype="float64")
        mon = np.asarray(ref.normalization.monitor, dtype="float64")
        w = min(len(e), len(n), len(mon))
        Es.append(e[:w]); Ns.append(n[:w]); Ms.append(mon[:w])
    E = np.concatenate(Es) if Es else np.empty(0)
    N = np.concatenate(Ns) if Ns else np.empty(0)
    M = np.concatenate(Ms) if Ms else np.empty(0)
    tol = energy_tol if energy_tol and energy_tol > 0 else _auto_energy_tol(E)

    keys = np.round(E / tol).astype("int64") if len(E) else np.empty(0, dtype="int64")
    out_e, out_n, out_m = [], [], []
    for kk in sorted(set(keys.tolist())):
        sel = keys == kk
        out_e.append(float(E[sel].mean()))
        out_n.append(float(N[sel].sum()))
        out_m.append(float(M[sel].sum()))
    energy = np.array(out_e, dtype="float64")
    Nsum = np.array(out_n, dtype="float64")
    Msum = np.array(out_m, dtype="float64")
    return MergedSpectrum(
        energy=energy, intensity=Nsum, variance=Nsum.copy(), monitor=Msum, mask=(Msum != 0),
        members=[(m.experiment_id, m.scan_number, m.detector_id) for m in members],
        n_members=len(members))


def _group_component(name: str, ref: SpectrumRef):
    """Resolve one collation-key component to a groupable value (and a display value)."""
    if name in ("sample",):
        return ref.experiment_id, ref.experiment_id
    if name in ("q", "Q"):
        return ref.q_key, [round(v, 4) for v in ref.q]
    if name in ("T", "temperature"):
        return ref.t_key, ref.t_key
    return None, None


def collate(records, card) -> Collation:
    """Group reduced measurement spectra into a catalogue + coverage map + flag rollup."""
    pol = card.policy
    step = pol.hkl_round_step or 0.05
    collation = pol.collation or {}
    key_names = collation.get("key", ["sample", "q", "T"])
    merge = collation.get("merge", "separate")
    t_round = collation.get("t_round", 0.1)
    energy_tol = collation.get("energy_round", 0.0)

    measurements = [r for r in records if r.scan_type == "measurement"]
    refs = [ref for r in measurements for ref in _spectrum_refs(r, step, t_round)]

    groups: dict = {}
    for ref in refs:
        resolved = [_group_component(n, ref) for n in key_names]
        gkey = tuple(k for k, _disp in resolved)
        if gkey not in groups:
            fields = {n: disp for n, (_k, disp) in zip(key_names, resolved)}
            groups[gkey] = CollationGroup(key=gkey, key_fields=fields, members=[], merge=merge)
        groups[gkey].members.append(ref)

    if merge == "merge":
        for g in groups.values():
            g.merged = _merge_group(g.members, energy_tol=energy_tol)

    return Collation(
        groups=list(groups.values()),
        coverage=_coverage(refs),
        rollup=_rollup(measurements),
        merge=merge)


# ---------------------------------------------------------------------------
# Coverage + gap detection (§7.1)
# ---------------------------------------------------------------------------
def detect_gaps(values, *, factor: float = 1.8) -> list:
    """Holes in a 1-D coverage: spacings larger than `factor`x the median spacing. (lo, hi) pairs."""
    uniq = sorted(set(round(float(v), 6) for v in values))
    if len(uniq) < 3:
        return []
    diffs = np.diff(uniq)
    med = float(np.median(diffs))
    if med <= 0:
        return []
    return [(uniq[i], uniq[i + 1]) for i in range(len(diffs)) if diffs[i] > factor * med]


def _coverage(refs) -> Coverage:
    if not refs:
        return Coverage(component="h", values=[], gaps=[])
    arr = np.array([r.q for r in refs], dtype="float64")       # [n, 3]
    spreads = arr.max(axis=0) - arr.min(axis=0)
    ci = int(np.argmax(spreads))
    vals = sorted(set(round(float(v), 6) for v in arr[:, ci]))
    return Coverage(component=_COMPONENTS[ci], values=vals, gaps=detect_gaps(vals))


# ---------------------------------------------------------------------------
# Flag / removal rollup
# ---------------------------------------------------------------------------
def _bump(d: dict, key: str, n: int = 1) -> None:
    d[key] = d.get(key, 0) + n


def _rollup(records) -> Rollup:
    r = Rollup()
    for rec in records:
        for f in rec.scan_flags:
            _bump(r.scan_flags, f.type)
        for sp in rec.spectra.values():
            for pf in sp.point_flags:
                _bump(r.point_flags, pf.type)
            for f in sp.spectrum_flags:
                _bump(r.spectrum_flags, f.type)
            if sp.miniscan_reject is not None and sp.miniscan_counts is not None:
                rejected = sp.miniscan_reject & ~np.isnan(sp.miniscan_counts)
                r.passes_rejected += int(rejected.sum())
                r.points_with_rejection += int(rejected.any(axis=1).sum())
                r.counts_removed += float(np.nansum(np.where(rejected, sp.miniscan_counts, 0.0)))
    return r


# ---------------------------------------------------------------------------
# Combine-before-divide merge primitive (IN8 / merge="merge"; off the S30 path)
# ---------------------------------------------------------------------------
def merge_before_divide(n_list, m_list):
    """Combine repeated measurements: sum kept counts AND kept monitors, THEN divide (§7.5, no floor).

    `n_list`/`m_list` are per-member kept-count and kept-monitor arrays on a shared grid. Returns
    `(intensity, variance, mask)`; a point whose summed monitor is zero is masked (kept, not deleted).
    """
    N = np.sum(np.asarray(n_list, dtype="float64"), axis=0)
    M = np.sum(np.asarray(m_list, dtype="float64"), axis=0)
    valid = M != 0
    intensity = np.zeros_like(N)
    variance = np.zeros_like(N)
    np.divide(N, M, out=intensity, where=valid)
    np.divide(N, M * M, out=variance, where=valid)
    return intensity, variance, valid
log = logging.getLogger(__name__)

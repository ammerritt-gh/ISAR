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

from dataclasses import dataclass, field

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
class CollationGroup:
    key: tuple
    key_fields: dict
    members: list
    merge: str = "separate"


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


def _t_key(t: float) -> float:
    return round(float(t), 1)


def _scanned_energy(record):
    scanned = [a for a in record.scan_axes if a.role == "scanned"]
    return scanned[0].values if scanned else None


def _spectrum_refs(record, step: float) -> list:
    t = float(record.temperature.setpoint[0]) if len(record.temperature.setpoint) else float("nan")
    energy = _scanned_energy(record)
    refs = []
    for det_id, sp in record.spectra.items():
        refs.append(SpectrumRef(
            experiment_id=record.experiment_id, scan_number=record.scan_number,
            detector_id=det_id, scan_type=record.scan_type, q=list(sp.q),
            q_key=_q_key(sp.q, step), temperature=t, t_key=_t_key(t),
            spectrum=sp, energy=energy, normalization=record.normalization))
    return refs


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
    key_names = (pol.collation or {}).get("key", ["sample", "q", "T"])
    merge = (pol.collation or {}).get("merge", "separate")

    measurements = [r for r in records if r.scan_type == "measurement"]
    refs = [ref for r in measurements for ref in _spectrum_refs(r, step)]

    groups: dict = {}
    for ref in refs:
        resolved = [_group_component(n, ref) for n in key_names]
        gkey = tuple(k for k, _disp in resolved)
        if gkey not in groups:
            fields = {n: disp for n, (_k, disp) in zip(key_names, resolved)}
            groups[gkey] = CollationGroup(key=gkey, key_fields=fields, members=[], merge=merge)
        groups[gkey].members.append(ref)

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

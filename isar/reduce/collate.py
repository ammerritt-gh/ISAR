"""Deterministic collation (§7.5): organize reduced `ScanRecord`s into a human-facing catalogue.

This is step 6's deterministic half — no LLM. It groups the per-analyzer spectra by the card's
collation key (S30: sample, rounded-Q, T), maps which Qs were measured (with gap detection, §7.1),
and rolls up what the pipeline flagged and removed. For S30 `merge = "separate"` the duplicates stay
separate (collation is a catalogue, not a physical combine); `merge_before_divide` is the primitive
the IN8/`merge` path will use — sum kept counts AND kept monitors, THEN divide (§7.5, never average
already-normalized rates), with no zero-count floor (D3).

Pooling has compatibility limits, and they refuse rather than average: a `merge` group whose members
were measured under different BACKGROUND TRUTH (the simulator's planted background, identified by the
effective fingerprint the TAVI parser keeps — `background_pool_key`) gets no master at all. Its
`pool_refusal` says why, the rollup counts it, and every member is kept, fit and reported on its own
(flag, never delete). The same refusal covers a multi-member group holding an UNIDENTIFIED background
(provenance with no fingerprint): sharing the "unknown" label is not evidence of shared truth.

The output feeds the report (`isar/report`): the catalogue, the coverage map, and the flag rollup.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from isar.schema.scan import exposure_for
from isar.schema.steering import CANONICAL_MASTER, evidence_sink_for

_COMPONENTS = ("h", "k", "l")

# Background-truth pooling classes (see `background_pool_key`) and the refusal they can produce.
NO_BACKGROUND = "absent"
UNKNOWN_BACKGROUND = "unknown"
BACKGROUND_MISMATCH = "background_fingerprint_mismatch"
# A spectrum with no Q (rung 4) has no state to pool on, and the master-fit seam derives its Q from
# the group key, so such a group gets no master (its spectrum is still fit on its own).
NO_Q = "no_q"


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
    q: list | None               # [h, k, l]; None = no Q (rung 4)
    q_key: tuple                 # integer grid key (stable for grouping); unique per spectrum if no Q
    temperature: float
    t_key: float | tuple         # a tuple unique to the scan when the temperature is NaN
    spectrum: object = None      # the schema.scan.Spectrum (arrays; not serialized in the catalogue)
    energy: object = None        # the scanned-axis values (ndarray)
    normalization: object = None # this spectrum's Normalization, via exposure_for (monitor / miniscan_monitor)
    background_key: str = NO_BACKGROUND   # background-truth pooling class (`background_pool_key`)


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
    consistency_statistic: float | None = None
    consistency_pvalue: float | None = None
    consistency_alpha: float = 0.01
    pool_consistent: bool | None = None
    consistency_seed: int | None = None


def merged_spectrum_payload(merged: MergedSpectrum) -> dict:
    """Canonical, JSON-compatible scientific payload for one pooled spectrum."""
    def _values(value):
        return value.tolist() if hasattr(value, "tolist") else list(value)

    return {
        "schema": "isar.pooled_spectrum/1",
        "energy": _values(merged.energy),
        "counts": _values(merged.intensity),
        "variance": _values(merged.variance),
        "monitor": _values(merged.monitor),
        "mask": [bool(v) for v in _values(merged.mask)],
        "members": [list(member) for member in merged.members],
        "n_members": int(merged.n_members),
        "consistency_statistic": merged.consistency_statistic,
        "consistency_pvalue": merged.consistency_pvalue,
        "consistency_alpha": merged.consistency_alpha,
        "pool_consistent": merged.pool_consistent,
        "consistency_seed": merged.consistency_seed,
    }


def merged_spectrum_digest(merged: MergedSpectrum) -> str:
    """Content digest shared by persistence, manifests, and fit fingerprints."""
    encoded = json.dumps(merged_spectrum_payload(merged), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def persist_merged_spectrum(data_dir, merged: MergedSpectrum) -> str:
    """Atomically persist a pooled spectrum and return its workspace-relative content address."""
    payload = merged_spectrum_payload(merged)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    digest = merged_spectrum_digest(merged)
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
    pool_refusal: str | None = None        # why this group was NOT pooled (members all kept)


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


def background_pool_key(raw_header) -> str:
    """The background-truth class a spectrum may be pooled within.

    A simulator can PLANT a background; TAVI stamps the effective fingerprint (the profile plus the
    sample scaling actually applied) into ``raw_header["background_provenance"]`` and the parser keeps
    that fingerprint while redacting the numbers. Records with no provenance key — real data, and every
    scan taken before TAVI generated background — share the single class :data:`NO_BACKGROUND` and pool
    exactly as they always did.

    Absent and present are DIFFERENT classes, not a compatible pair: combine-before-divide would fold
    counts from a planted background into the ΣN that certifies the point, so the master would carry an
    intensity no member's exposure accounts for. A provenance block whose fingerprint is missing fails
    CLOSED to :data:`UNKNOWN_BACKGROUND` rather than falling open to "absent" — the one case where
    guessing hands untraceable truth the pooling privilege. Because that class is a "we do not know"
    and not an identity, TWO unknown members do not pool with each other either; that refusal lives in
    :func:`collate`'s merge loop, where the group is visible.

    DISABLED NORMALIZES TO ABSENT. A provenance block whose ``enabled`` is exactly ``False`` maps to
    :data:`NO_BACKGROUND`, so an explicitly-clean simulator scan pools with legacy background-free data.
    The justification is TAVI's bit-identity guarantee: a disabled profile adds no counts and produces
    output bit-identical to pre-background TAVI, so the two carry numerically equivalent evidence and
    refusing to pool them would only lose exposure. Only the literal boolean ``False`` normalizes —
    a missing/``None``/true ``enabled`` keeps the fingerprint rules above (and a disabled block with no
    fingerprint is still clean, so it normalizes too). The full provenance stays on the record
    untouched; this is a pooling-key normalization, not a rewrite of the audit trail.
    """
    raw = raw_header if isinstance(raw_header, dict) else {}
    provenance = raw.get("background_provenance")
    if not isinstance(provenance, dict):
        return NO_BACKGROUND
    if provenance.get("enabled") is False:
        return NO_BACKGROUND
    fingerprint = provenance.get("effective_fingerprint")
    if fingerprint is None:
        log.warning("background provenance without an effective fingerprint: %r — the spectrum is "
                    "still catalogued, fit and reported, but it pools only with its own class.",
                    provenance)
        return UNKNOWN_BACKGROUND
    return f"fp:{fingerprint}"


def _spectrum_refs(record, step: float, t_round: float) -> list:
    t = float(record.temperature.setpoint[0]) if len(record.temperature.setpoint) else float("nan")
    energy = _scanned_energy(record)
    background_key = background_pool_key(getattr(record, "raw_header", None))
    refs = []
    for det_id, sp in record.spectra.items():
        # No Q (rung 4) or no temperature is no evidence of sharing a state with any other scan, so
        # that key component is unique to this scan (and analyzer, for Q): never pooled across scans.
        if sp.q is None:
            q, q_key = None, ("noq", record.experiment_id, record.scan_number, det_id)
        else:
            q, q_key = list(sp.q), _q_key(sp.q, step)
        t_key = (("not", record.experiment_id, record.scan_number) if math.isnan(t)
                 else _t_key(t, t_round))
        refs.append(SpectrumRef(
            experiment_id=record.experiment_id, scan_number=record.scan_number,
            detector_id=det_id, scan_type=record.scan_type, q=q,
            q_key=q_key, temperature=t, t_key=t_key,
            spectrum=sp, energy=energy, normalization=exposure_for(record, det_id),
            background_key=background_key))
    return refs


def _auto_energy_tol(energy) -> float:
    """A binning tolerance ~¼ of the median grid step (so distinct points stay apart, repeats merge)."""
    uniq = np.unique(np.round(np.asarray(energy, dtype="float64"), 9))
    if len(uniq) < 2:
        return 1e-6
    return max(0.25 * float(np.median(np.diff(uniq))), 1e-9)


def _poisson_deviance(observed, expected) -> float:
    observed = np.asarray(observed, dtype="float64")
    expected = np.asarray(expected, dtype="float64")
    valid = expected > 0.0
    if np.any((observed > 0.0) & ~valid):
        return float("inf")
    term = np.zeros_like(observed)
    positive = valid & (observed > 0.0)
    term[positive] = observed[positive] * np.log(observed[positive] / expected[positive])
    term[valid] -= observed[valid] - expected[valid]
    return float(2.0 * np.sum(term[valid]))


def _member_consistency(counts, monitors, *, seed: int, alpha: float,
                        bootstrap_samples: int = 512) -> tuple[float, float, bool]:
    """Exposure-scaled Poisson-deviance homogeneity test for one pooled group.

    Conditioning each energy bin on its observed total removes the unknown common spectrum.  The
    seeded multinomial bootstrap is the corresponding parametric Poisson bootstrap with that
    nuisance profile fitted, and remains well-defined for zero-count bins.
    """
    counts = np.asarray(counts, dtype="float64")
    monitors = np.asarray(monitors, dtype="float64")
    if counts.shape[0] < 2:
        return 0.0, 1.0, True
    total_counts = counts.sum(axis=0)
    total_monitor = monitors.sum(axis=0)
    usable = total_monitor > 0.0
    probabilities = np.zeros_like(monitors)
    np.divide(monitors, total_monitor, out=probabilities, where=usable)
    expected = probabilities * total_counts
    observed_stat = _poisson_deviance(counts[:, usable], expected[:, usable])
    rng = np.random.default_rng(int(seed))
    exceed = 0
    for _ in range(max(int(bootstrap_samples), 1)):
        simulated = np.zeros_like(counts)
        for column in np.flatnonzero(usable):
            n = int(round(max(total_counts[column], 0.0)))
            if n:
                simulated[:, column] = rng.multinomial(n, probabilities[:, column])
        stat = _poisson_deviance(simulated[:, usable], expected[:, usable])
        exceed += int(stat >= observed_stat)
    pvalue = float((exceed + 1) / (max(int(bootstrap_samples), 1) + 1))
    return observed_stat, pvalue, bool(pvalue >= float(alpha))


def _merge_group(members, *, energy_tol: float = 0.0, consistency_alpha: float = 0.01,
                 consistency_bootstrap_samples: int = 512) -> MergedSpectrum:
    """Combine-before-divide: align members on a common energy grid, then SUM counts and monitors.

    The merged signal stays RAW COUNTS (intensity=ΣN, variance=ΣN) with the summed monitor preserved —
    never average already-normalized rates (§7.5). Points whose summed monitor is zero are masked.
    """
    Es, Ns, Ms = [], [], []
    for ref in members:
        e = np.asarray(ref.energy, dtype="float64")
        n = np.asarray(ref.spectrum.intensity, dtype="float64")
        mon = np.asarray(ref.normalization.monitor, dtype="float64")
        keep = np.asarray(ref.spectrum.mask, dtype=bool)
        w = min(len(e), len(n), len(mon), len(keep))
        valid = keep[:w] & np.isfinite(e[:w]) & np.isfinite(n[:w]) & np.isfinite(mon[:w])
        Es.append(e[:w])
        Ns.append(np.where(valid, n[:w], 0.0))
        Ms.append(np.where(valid, mon[:w], 0.0))
    E = np.concatenate(Es) if Es else np.empty(0)
    N = np.concatenate(Ns) if Ns else np.empty(0)
    M = np.concatenate(Ms) if Ms else np.empty(0)
    tol = energy_tol if energy_tol and energy_tol > 0 else _auto_energy_tol(E)

    keys = np.round(E / tol).astype("int64") if len(E) else np.empty(0, dtype="int64")
    out_e, out_n, out_m = [], [], []
    member_n = np.zeros((len(members), len(set(keys.tolist()))), dtype="float64")
    member_m = np.zeros_like(member_n)
    for column, kk in enumerate(sorted(set(keys.tolist()))):
        sel = keys == kk
        out_e.append(float(E[sel].mean()))
        out_n.append(float(N[sel].sum()))
        out_m.append(float(M[sel].sum()))
        offset = 0
        for row, (e_values, n_values, m_values) in enumerate(zip(Es, Ns, Ms)):
            width = len(e_values)
            local = sel[offset:offset + width]
            member_n[row, column] = float(n_values[local].sum())
            member_m[row, column] = float(m_values[local].sum())
            offset += width
    energy = np.array(out_e, dtype="float64")
    Nsum = np.array(out_n, dtype="float64")
    Msum = np.array(out_m, dtype="float64")
    seed_payload = json.dumps([list((m.experiment_id, m.scan_number, m.detector_id))
                               for m in members], sort_keys=True).encode("utf-8")
    consistency_seed = int.from_bytes(hashlib.sha256(seed_payload).digest()[:8], "big") & 0x7fffffff
    statistic, pvalue, consistent = _member_consistency(
        member_n, member_m, seed=consistency_seed, alpha=consistency_alpha,
        bootstrap_samples=consistency_bootstrap_samples)
    return MergedSpectrum(
        energy=energy, intensity=Nsum, variance=Nsum.copy(), monitor=Msum, mask=(Msum != 0),
        members=[(m.experiment_id, m.scan_number, m.detector_id) for m in members],
        n_members=len(members), consistency_statistic=statistic,
        consistency_pvalue=pvalue, consistency_alpha=float(consistency_alpha),
        pool_consistent=consistent, consistency_seed=consistency_seed)


def _group_component(name: str, ref: SpectrumRef):
    """Resolve one collation-key component to a groupable value (and a display value)."""
    if name in ("sample",):
        return ref.experiment_id, ref.experiment_id
    if name in ("q", "Q"):
        return ref.q_key, (None if ref.q is None else [round(v, 4) for v in ref.q])
    if name in ("T", "temperature"):
        return ref.t_key, (None if isinstance(ref.t_key, tuple) else ref.t_key)
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
    consistency_alpha = float(collation.get("master_consistency_alpha", 0.01))
    consistency_bootstrap_samples = int(collation.get("master_consistency_bootstrap_samples", 512))

    measurements = [r for r in records if r.scan_type == "measurement"]
    refs = [ref for r in measurements for ref in _spectrum_refs(r, step, t_round)]

    # Non-canonical evidence is excluded from *grouping only* — never from the rollup or the coverage
    # map, which must keep reporting every scan that was taken. An auxiliary probe is a deliberately
    # off-target window fired to settle an identity question; combine-before-divide would fold its
    # counts into the ΣN pool that certifies the target at this (Q,T), so the master would claim
    # exposure it never spent on the target and a certification could come to rest on it. The probe is
    # still fit and still reported — on its own, through its declared sink.
    provenance = getattr(getattr(card, "state", None), "acquisition_provenance", None) or []
    poolable = [r for r in measurements
                if evidence_sink_for(getattr(r, "raw_header", None), provenance) == CANONICAL_MASTER]
    group_refs = (refs if len(poolable) == len(measurements)
                  else [ref for r in poolable for ref in _spectrum_refs(r, step, t_round)])

    groups: dict = {}
    for ref in group_refs:
        resolved = [_group_component(n, ref) for n in key_names]
        gkey = tuple(k for k, _disp in resolved)
        if gkey not in groups:
            fields = {n: disp for n, (_k, disp) in zip(key_names, resolved)}
            groups[gkey] = CollationGroup(key=gkey, key_fields=fields, members=[], merge=merge)
        groups[gkey].members.append(ref)

    rollup = _rollup(measurements)
    if merge == "merge":
        for g in groups.values():
            if any(ref.q is None for ref in g.members):
                g.pool_refusal = NO_Q
                continue
            # A group whose members were measured under different background truth is NOT pooled:
            # summing them would put counts nobody's exposure accounts for into ΣN. Nothing is
            # deleted — the members stay catalogued, fit and reported individually, and the group
            # records why it has no master (mirrors the non-canonical-evidence rule above).
            classes = sorted({ref.background_key for ref in g.members})
            # UNKNOWN_BACKGROUND is a "we cannot identify this", not an identity: two records that
            # both lost their fingerprint land in the same *label* without any evidence they were
            # measured under the same truth. Pooling them would make the fail-closed class fail
            # open the moment a second unknown arrived, so a multi-member group containing one
            # refuses as well.
            n_unknown = sum(ref.background_key == UNKNOWN_BACKGROUND for ref in g.members)
            unknown_multi = n_unknown > 0 and len(g.members) > 1
            if len(classes) > 1 or unknown_multi:
                g.pool_refusal = BACKGROUND_MISMATCH
                _bump(rollup.scan_flags, BACKGROUND_MISMATCH)
                log.warning(
                    "refusing to pool %s: %d members carry %d background-truth classes (%s)%s. The "
                    "spectra are kept and still fit individually, but they have no pooled master.",
                    g.key_fields, len(g.members), len(classes), ", ".join(classes),
                    (f", of which {n_unknown} is/are unidentified and cannot be shown to share a "
                     "truth with anything") if unknown_multi else "")
                continue
            g.merged = _merge_group(
                g.members, energy_tol=energy_tol, consistency_alpha=consistency_alpha,
                consistency_bootstrap_samples=consistency_bootstrap_samples)

    return Collation(
        groups=list(groups.values()),
        coverage=_coverage(refs),
        rollup=rollup,
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
    refs = [r for r in refs if r.q is not None]     # a Q-less spectrum maps no Q
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

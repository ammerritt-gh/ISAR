"""Ingestion (UNIFICATION_DESIGN §2.3, DESIGN.md §7.1) — discover, classify, parse, dedup, order.

Files are the source of truth (U4/§0.5). `discover` walks the data paths, classifies each file
(measurement / calibration-companion / foreign / unrecognized), and `parse_all` parses the
measurement files with the card-pinned plugin, dedups by `(experiment_id, scan_number)` keeping the
last occurrence (growing-file resend), and orders by scan number. `file_gaps` reports holes in the
scan-number sequence — the file-level gap detection `collate.detect_gaps` (value space) does not do.
"""
from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from isar.parsers import registry
from isar.schema.card import Card

log = logging.getLogger(__name__)

# Suffixes that are never instrument data — skip without opening (binaries, logs, our own output,
# and the `.UNUSED` relabel marker for retired datasets). `.json` is NOT skipped: TAVI simulator
# scans are JSON and `registry.detect` recognizes them structurally; workspace state JSONs are
# excluded by name below, and unrelated JSON fails detection and lands in `unrecognized`.
SKIP_SUFFIXES = {".nxs", ".log", ".md", ".png", ".csv", ".tmp", ".unused"}
# Our own workspace files — never ingest them even if a data path points at a workspace.
# `gui.state.json` is a GUI-owned per-workspace file (Phase E) — it must never be classified as data.
WORKSPACE_NAMES = {"card.policy.toml", "card.state.json", "session.json", "report.json",
                   "gui.state.json"}


@dataclass
class IngestPlan:
    measurement_files: list[Path] = field(default_factory=list)
    companion_files: list[Path] = field(default_factory=list)   # calibration sources (§3)
    foreign: list[Path] = field(default_factory=list)           # recognized, different instrument
    unrecognized: list[Path] = field(default_factory=list)      # detect() could not classify


def _iter_files(data_paths) -> list[Path]:
    out: list[Path] = []
    for raw in data_paths:
        p = Path(raw)
        if p.is_dir():
            out.extend(sorted(f for f in p.rglob("*") if f.is_file()))
        elif p.is_file():
            out.append(p)
    # de-dup while preserving order
    seen, uniq = set(), []
    for f in out:
        r = f.resolve()
        if r not in seen:
            seen.add(r)
            uniq.append(f)
    return uniq


def _skip(path: Path) -> bool:
    return path.suffix.lower() in SKIP_SUFFIXES or path.name in WORKSPACE_NAMES


def discover(data_paths, card: Card) -> IngestPlan:
    pol = card.policy
    companion_glob = (pol.calibration or {}).get("companion_glob")
    plan = IngestPlan()
    for f in _iter_files(data_paths):
        if _skip(f):
            continue
        # Companion calibration files are classified by NAME before detection — the S30 companion
        # raw .spec detects as ("APS","S30",...) too, but must never be parsed as measurements (R2).
        if companion_glob and fnmatch.fnmatch(f.name, companion_glob):
            plan.companion_files.append(f)
            continue
        try:
            facility, instrument, _ver = registry.detect(f)
        except (ValueError, OSError):
            plan.unrecognized.append(f)
            continue
        if (facility, instrument) == (pol.facility, pol.instrument):
            plan.measurement_files.append(f)
        else:
            plan.foreign.append(f)
    return plan


def parse_all(plan: IngestPlan, card: Card) -> list:
    """Parse the measurement files with the card-pinned plugin; dedup + order.

    Dedup key is `(experiment_id, scan_number)`; the LAST occurrence wins (a resent growing file
    supersedes an earlier copy). Records are returned sorted by `(experiment_id, scan_number)`.
    """
    plugin = registry.plugin_by_name(card.policy.plugin)
    by_key: dict[tuple, object] = {}
    for f in plan.measurement_files:
        for rec in plugin.parse_file(f, card):
            by_key[(rec.experiment_id, rec.scan_number)] = rec   # last wins
    return [by_key[k] for k in sorted(by_key, key=lambda k: (str(k[0]), k[1]))]


@dataclass
class IngestReport:
    """The result of :func:`ingest_batch` — what was seen and how the ledger changed.

    Scan-level ids in ``added``/``updated``/``unchanged`` are the ``"exp:scan"`` form (each scan
    appears in exactly one list, precedence added > updated > unchanged); the ledger itself is keyed
    per-detector (``exp:scan:det``). This is the GUI's parse-preview feed (Phase E consumes it via
    ``--json``). ``dirty`` is False iff nothing changed on the session — the caller SKIPs ``save()``
    then, so a re-ingest no-op does not tick the workspace watch.
    """
    files: list = field(default_factory=list)       # {"path","class","n_scans","scan_ids","n_context"}
    added: list = field(default_factory=list)        # scan-level ids "exp:scan" newly entered
    updated: list = field(default_factory=list)      # existing entries whose content_hash drifted
    unchanged: list = field(default_factory=list)    # re-ingest no-ops
    deduped: list = field(default_factory=list)      # {"scan","kept","superseded":[...]}
    gaps: list = field(default_factory=list)         # file_gaps output
    dirty: bool = False

    def to_jsonable(self) -> dict:
        return {"files": [dict(f) for f in self.files],
                "added": list(self.added), "updated": list(self.updated),
                "unchanged": list(self.unchanged),
                "deduped": [dict(d) for d in self.deduped],
                "gaps": [list(g) for g in self.gaps]}


def ingest_batch(card: Card, session, data_paths) -> IngestReport:
    """Parse ``data_paths`` into the session ledger WITHOUT fitting (UNIFICATION_DESIGN §2.3).

    Discovers/classifies files (reusing :func:`discover`), parses the measurement files with the
    card-pinned plugin, dedups by ``(experiment_id, scan_number)`` last-wins, and upserts one ledger
    entry per ``(experiment_id, scan_number, detector)`` at status ``"ingested"``. Never fits, never
    deletes, never writes ``card.policy.toml``. Precedence contract (the load-bearing part): an
    unchanged re-ingest leaves a fitted/held/excluded entry COMPLETELY untouched — status is only
    ever driven up to ``"ingested"`` when the parsed content actually drifted.

    Mutates ``session`` in place; the caller persists it (skip ``save()`` when ``report.dirty`` is
    False). Returns an :class:`IngestReport`.
    """
    from isar.reduce import combine, qc
    from isar.session import router
    from isar.session.state import ScanEntry, content_hash

    report = IngestReport()
    plan = discover(data_paths, card)

    # --- parse measurement files, tracking record -> source file(s) for dedup reporting ---
    plugin = registry.plugin_by_name(card.policy.plugin)
    by_key: dict[tuple, object] = {}                 # (exp, scan) -> record (last wins)
    sources: dict[tuple, list[str]] = {}             # (exp, scan) -> ordered distinct source paths
    file_scan_keys: dict[str, list[tuple]] = {}      # resolved measurement path -> [(exp, scan), ...]
    for f in plan.measurement_files:
        rp = str(Path(f).resolve())
        seen_here: set[tuple] = set()
        for rec in plugin.parse_file(f, card):
            key = (rec.experiment_id, rec.scan_number)
            by_key[key] = rec                        # last occurrence wins (growing-file resend)
            paths = sources.setdefault(key, [])
            if rp not in paths:
                paths.append(rp)
            if key not in seen_here:
                seen_here.add(key)
                file_scan_keys.setdefault(rp, []).append(key)

    records = [by_key[k] for k in sorted(by_key, key=lambda k: (str(k[0]), k[1]))]

    # Partition exactly like the driver (driver.py: `router.route` then ledger only
    # `routed.measurements`): calibration/alignment/environment/unknown scans are CONTEXT, not
    # results — ledgering them would strand phantom `ingested` entries that never fit and re-queue
    # every run. We do NOT apply context updates here (that mutates card.state — a run-time job).
    routed = router.route(records)
    measurement_keys = {(r.experiment_id, r.scan_number) for r in routed.measurements}

    # Dedup alerts: any scan whose surviving record was sourced from more than one file.
    for key in sorted(sources, key=lambda k: (str(k[0]), k[1])):
        paths = sources[key]
        if len(paths) > 1:
            report.deduped.append({"scan": f"{key[0]}:{key[1]}",
                                   "kept": paths[-1], "superseded": paths[:-1]})

    # --- upsert one ledger entry per (exp, scan, det), MEASUREMENTS ONLY ---
    import numpy as np
    now_iso = datetime.now(timezone.utc).isoformat()
    _RANK = {"unchanged": 0, "updated": 1, "added": 2}
    ledgered_keys: set[tuple] = set()                # measurement scans that produced ≥1 entry
    for rec in routed.measurements:
        scan_short = f"{rec.experiment_id}:{rec.scan_number}"
        # Hash the SAME canonical form the driver hashes (reduced + QC'd, both in-place no-ops where
        # not applicable and neither raises) so a re-ingest after `isar run` reads as "unchanged" and
        # never downgrades a fitted/held/excluded entry. content_hash on the raw record would drift
        # from the fit's stored hash and silently re-queue the whole workspace on every parse-preview.
        combine.reduce_record(rec, card)
        qc.qc_record(rec, card)
        chash = content_hash(rec)
        T = float(np.median(np.asarray(rec.temperature.setpoint, dtype=float)))
        src = sources[(rec.experiment_id, rec.scan_number)][-1]   # the kept (last-wins) source file
        scan_rank = 0                                             # collapse per-detector states
        touched = False                                          # any detector entry produced?
        for det, sp in rec.spectra.items():
            touched = True
            key = f"{rec.experiment_id}:{rec.scan_number}:{det}"
            q = [float(v) for v in sp.q] if sp.q is not None else None
            prov_add = {"source_file": src, "T": T, "q": q, "ingested_at": now_iso}
            prev = session.get(key)
            if prev is None:
                session.put(ScanEntry(
                    scan_id=key, scan_number=rec.scan_number, scan_type=rec.scan_type,
                    status="ingested", content_hash=chash, guidelines_hash="",
                    detectors={}, provenance=dict(prov_add)))
                det_state = "added"
                report.dirty = True
            elif prev.content_hash == chash:
                # Same content: leave the entry COMPLETELY untouched — never downgrade a
                # fitted/held/excluded status, never re-stamp provenance.
                det_state = "unchanged"
            else:
                # Content drifted: re-queue for fitting. Keep the (now stale) detectors payload so
                # the old fit stays visible until `isar run` supersedes it; merge provenance.
                prev.content_hash = chash
                prev.status = "ingested"
                prev.provenance = {**(prev.provenance or {}), **prov_add}
                det_state = "updated"
                report.dirty = True
            scan_rank = max(scan_rank, _RANK[det_state])
        if touched:
            # A no-spectra measurement produces no ledger entry — keep it out of all three lists.
            ledgered_keys.add((rec.experiment_id, rec.scan_number))
            {2: report.added, 1: report.updated, 0: report.unchanged}[scan_rank].append(scan_short)

    # --- file manifest for ALL classified files (merge, never replace) ---
    # For a measurement-class FILE, its scans still split into measurement scans (ledgered, listed
    # in scan_ids) and CONTEXT scans routed out (counted as n_context, never ledgered). scan_ids
    # therefore maps 1:1 to ledger entries; n_scans == len(scan_ids); n_context is the honest home
    # for the in-file calibration/alignment/environment scans a run would consume as context.
    _classes = [(plan.measurement_files, "measurement"), (plan.companion_files, "companion"),
                (plan.foreign, "foreign"), (plan.unrecognized, "unrecognized")]
    for files, klass in _classes:
        for f in files:
            rp = str(Path(f).resolve())
            try:
                stat = Path(f).stat()
            except OSError as exc:
                log.warning("ingest: could not stat %s (%s); manifest row skipped", f, exc)
                continue
            keys = file_scan_keys.get(rp, []) if klass == "measurement" else []
            scan_ids = [f"{e}:{s}" for (e, s) in keys if (e, s) in ledgered_keys]
            n_context = sum(1 for k in keys if k not in measurement_keys)
            row = {"size": stat.st_size, "mtime": stat.st_mtime, "format": klass}
            if klass == "measurement":
                row["n_scans"] = len(scan_ids)
                row["scan_ids"] = list(scan_ids)
                row["n_context"] = n_context
            target = session.files.setdefault(rp, {})
            before = dict(target)
            target.update(row)
            if target != before:
                report.dirty = True
            report.files.append({"path": rp, "class": klass, "n_scans": len(scan_ids),
                                 "scan_ids": list(scan_ids), "n_context": n_context})

    # --- remember new data locations (mirror run_batch) ---
    for p in map(str, data_paths):
        if p not in session.data_paths:
            session.data_paths.append(p)
            report.dirty = True

    report.gaps = file_gaps(records)
    return report


def file_gaps(records) -> list[tuple[str, int, int]]:
    """Holes in the scan-number sequence per experiment: `(experiment_id, lo, hi)` for each gap
    where scan numbers jump from `lo` to `hi` (hi > lo + 1). A silent hole in the dispersion is the
    worst failure for a speed tool, so this is surfaced in status and the report header.
    """
    by_exp: dict[str, list[int]] = {}
    for rec in records:
        by_exp.setdefault(str(rec.experiment_id), []).append(rec.scan_number)
    gaps: list[tuple[str, int, int]] = []
    for exp, nums in by_exp.items():
        nums = sorted(set(nums))
        for a, b in zip(nums, nums[1:]):
            if b > a + 1:
                gaps.append((exp, a, b))
    return gaps

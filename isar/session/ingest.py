"""Ingestion (UNIFICATION_DESIGN §2.3, DESIGN.md §7.1) — discover, classify, parse, dedup, order.

Files are the source of truth (U4/§0.5). `discover` walks the data paths, classifies each file
(measurement / calibration-companion / foreign / unrecognized), and `parse_all` parses the
measurement files with the card-pinned plugin, dedups by `(experiment_id, scan_number)` keeping the
last occurrence (growing-file resend), and orders by scan number. `file_gaps` reports holes in the
scan-number sequence — the file-level gap detection `collate.detect_gaps` (value space) does not do.
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path

from isar.parsers import registry
from isar.schema.card import Card

# Suffixes that are never instrument data — skip without opening (binaries, logs, our own output,
# and the `.UNUSED` relabel marker for retired datasets). `.json` is NOT skipped: TAVI simulator
# scans are JSON and `registry.detect` recognizes them structurally; workspace state JSONs are
# excluded by name below, and unrelated JSON fails detection and lands in `unrecognized`.
SKIP_SUFFIXES = {".nxs", ".log", ".md", ".png", ".csv", ".tmp", ".unused"}
# Our own workspace files — never ingest them even if a data path points at a workspace.
WORKSPACE_NAMES = {"card.policy.toml", "card.state.json", "session.json", "report.json"}


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

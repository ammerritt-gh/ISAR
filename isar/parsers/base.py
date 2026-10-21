"""The parser-plugin contract (§6). Every plugin parses a facility file into `ScanRecord`s and
emits the inverse (the §4 synthetic format), reading all policy from the card.

A plugin is anything satisfying this Protocol:
- `parse_file(path, card) -> list[ScanRecord]` — one record per physical scan, all detectors
  captured, scan_type classified, `source` stamped with plugin/format/schema versions.
- `emit_file(run, card) -> bytes | Path` — the §4 inverse, schema-faithful (D5), for round-trip.
- `card_template() -> Card` — a fully-populated card for a fresh beamtime to copy and edit.
- `matches(path) -> bool` — a cheap signature sniff for the §6 pre-run format check.

Optional hook (not part of the Protocol, so existing plugins stay conformant):
- `parse_set(file_set, card) -> list[ScanRecord]` — for a processed format that publishes one
  scan as a set of files (one per detector/analyzer). When the card declares `[files]`, ingest
  groups the matching files into `FileSet`s itself (the card regex's `set` group is the key) and
  calls this once per set instead of `parse_file` per file; a plugin without it keeps the
  one-file-at-a-time behaviour. A member that cannot be read must not abort the set: the plugin
  parses the rest and flags the gap. Grouping lives once, in `isar.session.ingest`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass
class FileSet:
    """The files of one scan, as grouped by ingest from the card's `[files]` table.

    `key` is the value of the `set` regex group shared by every member. `measurements` pairs each
    member path with its regex groups (`{"set": ..., "analyzer": ..., ...}`), sorted by path.
    `sidecars` are the files beside the measurement (the card's `sidecar` templates filled with
    `key`) that exist; they are never parsed as measurements.
    """
    key: str
    measurements: list[tuple[Path, dict]] = field(default_factory=list)
    sidecars: list[Path] = field(default_factory=list)


@runtime_checkable
class ParserPlugin(Protocol):
    name: str
    version: str

    def parse_file(self, path: str | Path, card) -> list: ...

    def emit_file(self, run, card) -> bytes | Path: ...

    def card_template(self): ...

    def matches(self, path: str | Path) -> bool: ...

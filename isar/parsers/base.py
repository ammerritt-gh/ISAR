"""The parser-plugin contract (§6). Every plugin parses a facility file into `ScanRecord`s and
emits the inverse (the §4 synthetic format), reading all policy from the card.

A plugin is anything satisfying this Protocol:
- `parse_file(path, card) -> list[ScanRecord]` — one record per physical scan, all detectors
  captured, scan_type classified, `source` stamped with plugin/format/schema versions.
- `emit_file(run, card) -> bytes | Path` — the §4 inverse, schema-faithful (D5), for round-trip.
- `card_template() -> Card` — a fully-populated card for a fresh beamtime to copy and edit.
- `matches(path) -> bool` — a cheap signature sniff for the §6 pre-run format check.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class ParserPlugin(Protocol):
    name: str
    version: str

    def parse_file(self, path: str | Path, card) -> list: ...

    def emit_file(self, run, card) -> bytes | Path: ...

    def card_template(self): ...

    def matches(self, path: str | Path) -> bool: ...

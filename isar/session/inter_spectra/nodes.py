"""Spectrum keys, series keys and temperatures for the inter-spectra graph (C-keys). Stdlib only; no Qt.

Read-only: ``read_nodes`` reads the import record, the intake entries and the ledger, and never writes.
S1U4 fills the bodies.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SpectrumNode:
    key: str            # "<experiment>:<scan_number>:<detector>"  (FileItem.unit_key, ScanEntry.scan_id)
    scan: str           # "<experiment>:<scan_number>"             (row["scan"], scan_prefix(key))
    detector: str       # "a03"
    label: str          # the set stem for messages, e.g. "Smp_100K_0p5_0p5_3" (row["set"])
    series: str | None  # series key; None: no q setting recorded, so no T-links
    T: float | None     # K in force (user value over file value); None: none
    eligible: bool      # has a usable fast solo fit
    reason: str | None  # why not eligible / why no series / why no T; None when all fine


@dataclass(frozen=True)
class NodeSet:
    nodes: dict[str, SpectrumNode]          # key -> node, insertion order = natural scan then detector order
    series: dict[str, tuple[str, ...]]      # series key -> scan keys ordered by T slot (ties by scan number)
    slots: dict[str, float]                 # scan key -> T slot value (t_key binned), only scans with T
    repeats: tuple[str, ...]                # one line per series slot holding >1 scan (assumption U-b)


def series_key(row: dict, *, sample: str | None, hkl_step: float) -> str | None:
    raise NotImplementedError("stub: filled by S1U4")


def read_nodes(root) -> NodeSet:
    raise NotImplementedError("stub: filled by S1U4")

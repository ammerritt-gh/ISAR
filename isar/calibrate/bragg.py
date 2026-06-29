"""Ingest dedicated Bragg/elastic (`herixE`) scans from the raw S30 SPEC file (§7, D8).

The energy zero is fixed from elastic-only scans, *not* from a phonon scan's own elastic line (which
`fit_elastic` mis-locates when the phonon is taller than the elastic). The S30 phonon scans live in the
9-analyzer supplementary `.dat`; the elastic scans are `[ad]scan herixE` blocks in the **raw `.spec`**
(standard SPEC, columns `HerixE` + `Ana1..Ana9`). This reader reuses the shared `spec_base.read_spec`
and returns one :class:`ElasticScan` per K≈0 herixE block, which `energy_zero.build_energy_calibration`
fits into a scan-keyed zero series + per-slit resolution.

The raw `.spec` `#L` carries multi-word column names (``Heat Stage T1`` …); they are passed to
`read_spec` so columns resolve by name. K comes from `#Q`, the slit from `#P17`, the time from `#D`.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from isar.parsers import spec_base
from isar.calibrate.energy_zero import _parse_dt

# Multi-word #L column names in the real S30 raw .spec (collapsed so columns resolve by name).
_S30_MULTIWORD = ("Heat Stage T1", "Heat Stage T2", "Heat Stage T3")
# #P17 motor row, 0-based index of the Ana7 slit (Anal7_Diam) among the values after the tag.
_SLIT_PINDEX = 4


@dataclass(frozen=True)
class ElasticScan:
    """One dedicated elastic (Bragg) scan: the energy axis + analyzer counts plus the keys needed to
    place its fitted zero (scan number / acquisition time) and pick its resolution (slit)."""

    scan_number: int
    time: float
    K: float
    slit: int
    energy: np.ndarray
    counts: np.ndarray


def _is_herixE(command: str) -> bool:
    toks = (command or "").split()
    return len(toks) >= 2 and toks[0] in ("ascan", "dscan") and toks[1] == "herixE"


def _slit(block) -> int:
    row = block.P_groups.get(17)
    if row and len(row) > _SLIT_PINDEX:
        try:
            return int(round(float(row[_SLIT_PINDEX])))
        except (TypeError, ValueError):
            return 0
    return 0


def read_elastic_scans(spec_path: str | Path, *, analyzer: str = "Ana7",
                       k_max: float = 0.05) -> list[ElasticScan]:
    """Read the K≈0 `herixE` elastic scans from a raw S30 `.spec` file.

    `analyzer` is the `#L` column to read counts from (``"Ana7"`` for FeSe_1, matching the phonon
    analysis). Blocks whose `#Q` |K| exceeds `k_max` (not zone-centre elastic) or that lack the
    `HerixE`/`analyzer` columns are skipped.
    """
    _header, blocks = spec_base.read_spec(spec_path, multiword_columns=list(_S30_MULTIWORD))
    out: list[ElasticScan] = []
    for b in blocks:
        if not _is_herixE(b.command):
            continue
        K = float(b.Q[1]) if len(b.Q) >= 2 else 0.0
        if abs(K) > k_max:
            continue
        try:
            ie = b.column_labels.index("HerixE")
            ia = b.column_labels.index(analyzer)
        except ValueError:
            continue
        if b.data is None or b.data.size == 0 or b.data.shape[1] <= max(ie, ia):
            continue
        dt = _parse_dt(b.D)
        time = dt.timestamp() if dt is not None else float(b.scan_number)
        out.append(ElasticScan(
            scan_number=int(b.scan_number), time=time, K=K, slit=_slit(b),
            energy=np.asarray(b.data[:, ie], dtype=float),
            counts=np.asarray(b.data[:, ia], dtype=float)))
    return out

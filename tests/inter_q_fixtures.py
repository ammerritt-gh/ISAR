"""BL43 workspaces that carry accepted per-analyzer hkl through the landed gpi path, and lattice epochs, generated
at test time (hard rule 10): every number is made up.

A top-level helper beside ``tests/intake_fixtures.py``, imported the same way (``tests/`` is on ``sys.path``).
"""
from __future__ import annotations

from pathlib import Path

import intake_fixtures as fx
from isar.intake.gpi import check_gpi
from isar.session import ingest
from isar.session.workspace import Workspace

Q_NM = 20.0                                              # the |Q| every test analyzer file and gpi macro states


def epoch(version: int = 1, *, a: float = 4.0, b: float | None = None, c: float | None = None,
          alpha: float = 90.0, beta: float = 90.0, gamma: float = 90.0, scan_ref: str | None = None) -> dict:
    """A lattice epoch record (``isar.geometry.epochs``) for the cell (a, b, c, alpha, beta, gamma)."""
    lattice = {"a": a, "b": a if b is None else b, "c": a if c is None else c,
               "alpha": alpha, "beta": beta, "gamma": gamma}
    return {"version": version, "created": "2026-10-29T00:00:00+00:00", "valid_from": {"scan_ref": scan_ref, "ts": None},
            "supersedes": None, "lattice": lattice, "orient": None, "method": "test", "source": "test"}


def set_epochs(root: Path, *epochs: dict) -> None:
    """Write ``card.state.lattice_epochs`` of the workspace at ``root``."""
    card, session = Workspace.open(root).load()
    card.state.lattice_epochs = list(epochs)
    Workspace.open(root).save(card, session)


def workspace_with_gpi(tmp_path: Path, scans: dict[str, dict[str, tuple | None]], *,
                       dets: tuple[str, ...] = fx.ANALYZERS) -> Path:
    """An imported BL43 workspace, one scan per entry of ``scans`` (scan file stem -> {analyzer: gpi hkl or None}),
    each with its ``gpi.<stem>`` macro beside the data, and the gpi check run. Returns the workspace root."""
    ws = fx.workspace(tmp_path, sample="Smp")
    data = tmp_path / "data"
    for stem, hkl in scans.items():
        fx.write_scan(data, stem=stem, analyzers=dets, seed=1, energy=fx.ENERGY_120, q_nm={d: Q_NM for d in dets})
        fx.write_gpi(data, stem, q_nm={d: Q_NM for d in dets}, hkl=hkl)
    card, session = ws.load()
    ingest.ingest_batch(card, session, [data], workspace_root=ws.root)
    ws.save(card, session)
    card, _session = ws.load()
    check_gpi(ws.root, card, [data], by="import")
    return ws.root


def spectrum_keys(root: Path) -> list[str]:
    """Every spectrum key of the workspace at ``root``, in ingest order."""
    _card, session = Workspace.open(root).load()
    return list(session.scans)


def scan_number(root: Path, scan: str) -> str:
    """The scan's ``scan_number`` as the epoch's ``scan_ref`` is compared (``str``), read from the ledger."""
    _card, session = Workspace.open(root).load()
    return str(next(e.scan_number for k, e in session.scans.items() if k.rsplit(":", 1)[0] == scan))

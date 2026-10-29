"""The synthetic BL43-like workspace for the inter-spectra tests, generated at test time (hard rule 10).

Two q settings x several temperatures x the analyzers. Each set is written as the BL43 analyzer and var files
(``bl43_fixture``'s writers, with a temperature), ingested as ``isar ingest`` does (``intake=True``), and each
analyzer is given a stored fast solo fit (``bl43_fixture.outcome``, written in one load and save). Counts come
from ``bl43_fixture``'s made-up profile; no measured value is written.

A top-level helper beside ``tests/bl43_fixture.py``, imported the same way.
"""
from __future__ import annotations

import shutil
from collections.abc import Sequence
from pathlib import Path

from bl43_fixture import ANALYZERS, _analyzer_file, _var_file, outcome


def spectrum_keys(ws_root: Path) -> list[str]:
    """Every spectrum key (``<scan key>:aNN``) of the workspace, in ingest order: scan by scan, analyzer by analyzer."""
    from isar.session.workspace import Workspace

    _, session = Workspace.open(ws_root).load()
    return list(session.scans)


def scan_keys(ws_root: Path) -> list[str]:
    """The scan keys of ``spectrum_keys``, each once, in the same order."""
    return list(dict.fromkeys(key.rsplit(":", 1)[0] for key in spectrum_keys(ws_root)))


def build_inter_workspace(root: Path, temperatures: Sequence[float] = (100, 150, 200),
                          setups: Sequence[str] = ("0p5_0p5_3", "0p3_0p3_2"), n_analyzers: int = 28,
                          fits: bool = True) -> Path:
    """Write the data under ``root/incoming``, ingest it into ``root/ws``, stamp the fast solo fits; return ``ws``."""
    from isar.parsers import sp8_bl43
    from isar.session import ingest
    from isar.session.outcome import _write_results_csv
    from isar.session.state import SessionState
    from isar.session.workspace import Workspace

    root = Path(root)
    incoming = root / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    numbers = ANALYZERS[:n_analyzers]
    for setup in setups:
        for temperature in temperatures:
            stem = f"CsV3Sb5_{temperature:g}K_{setup}_sum"
            for number in numbers:
                _analyzer_file(incoming / f"{stem}_a{number:02d}", number, temperature=float(temperature))
            _var_file(incoming / f"{stem}.var", numbers)
    ws_root = root / "ws"
    ws_root.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(sp8_bl43._CARD_PATH, ws_root / "card.policy.toml")   # the shipped card, verbatim
    (ws_root / "card.state.json").write_text("{}", encoding="utf-8", newline="\n")
    SessionState().save(ws_root / "session.json")
    ws = Workspace.open(ws_root)
    card, session = ws.load()
    ingest.ingest_batch(card, session, [incoming], data_dir=ws_root / "data", workspace_root=ws_root)
    if fits:       # stamp_fit's effect on every spectrum, in one load and save (stamp_fit per spectrum: about 10 s)
        for key in session.scans:
            det = key.rsplit(":", 1)[1]
            session.scans[key].detectors = {det: outcome(det, "resolved")}
            session.scans[key].status, session.scans[key].reasons = "fitted", []
        _write_results_csv(ws, session)
    ws.save(card, session)
    return ws_root

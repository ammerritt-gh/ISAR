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
        from isar.intake import inputs, scans as intake_scans, store
        from isar.session import file_fit
        ws.save(card, session)                                 # the ingest, for file_fit's freeze to read
        frozen = file_fit.freeze(ws_root)
        card, session = frozen.card, frozen.session
        rows = intake_scans.read_scans(ws_root)
        read = store.read_all(ws_root, store.ENTRIES)
        index = inputs.index_entries(read.entries, lines=read.lines)
        scan_preps = {}
        for key in session.scans:
            scan, det = key.rsplit(":", 1)
            exp, scan_no = scan.rsplit(":", 1)
            entry = session.scans[key]
            if scan not in scan_preps:                         # one parse per scan, as the batch runner does
                scan_preps[scan] = file_fit.prepare_scan(frozen, exp, int(scan_no), entry)
            # file_fit's own preparation: the hashes it stamps, and the calibration state a save stores on the card
            prep = file_fit.prepare_unit(frozen, scan_preps[scan], key, ("fast",))
            card.state.tabulated_calibration.update(prep.sp.recalibrated)
            si = inputs.scan_inputs(rows[scan], index, data_dir=ws_root / "data")
            unit = inputs.unit_inputs(rows[scan], si, det, card.state.tabulated_calibration.get(scan))
            entry.detectors = {det: {**outcome(det, "resolved"), "T": entry.provenance["T"]}}     # the spectrum's own T
            entry.status, entry.reasons = "fitted", []
            entry.guidelines_hash = prep.now.guidelines_hash
            entry.provenance = {**(entry.provenance or {}),
                                "calibration_fingerprint": prep.now.calibration_fingerprint,
                                "fit_inputs": {"run": "", "hash": prep.now.inputs_hash,
                                               "fallbacks": list(inputs.fallbacks(unit))}}
        _write_results_csv(ws, session)
    ws.save(card, session)
    return ws_root

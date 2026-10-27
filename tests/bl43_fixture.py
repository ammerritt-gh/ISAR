"""The shared BL43LXU-shaped test workspace, generated at test time (hard rule 10: no ``real_data/``).

A top-level helper beside ``tests/synthetic_ws.py``: ``tests/`` is on ``sys.path``, so ``tests/gui``,
``tests/report`` and ``tests/cli`` import it alike. ``build_bl43_workspace`` writes BL43-shaped analyzer
files, ingests them with today's ingest, and (``fits=True``) stamps a handful of stored fits so the scan
list has fast-pass, deep-pass, held and excluded spectra to show.

Layout it builds under ``root``:

    incoming/   the data files (sets A and B, one Ba122 single scan, one set that fails, a note)
    ws/         the workspace (card, ledger, ``report/results.csv``) -- the path it returns

The workspace's manifest holds absolute paths into ``incoming/``. A ``shutil.copytree`` of ``root``
(the ``bl43_ws`` fixture) therefore still points at the original ``incoming/``; tests that write to
the copy change the ledger and ``results.csv``, never the data files.

``sample_snapshot`` is the scan-list read's answer for the 4-analyzer workspace, written out by hand:
consumer tests monkeypatch ``scan_list`` to return it, and the read's own test asserts the real read
equals it, which ties the consumers' data to the provider.
"""
from __future__ import annotations

import shutil
import types
from pathlib import Path

import numpy as np

from isar.workspace_api.scan_list import (Fact, ScanListSnapshot, ScanSetRow,
                                          SpectrumRow, Status, UnreadFile)

STEM_A = "CsV3Sb5_97K_0p5_0p5_3_sum"
STEM_B = "CsV3Sb5_97K_0p5_0p5_3_rep_sum"
KEY_A = "CsV3Sb5_97K_0p5_0p5_3_sum:1842762476"         # set keys as ingest makes them
KEY_B = "CsV3Sb5_97K_0p5_0p5_3_rep_sum:1126149381"
KEY_SINGLE = "spec1:58"
ANALYZERS = (3, 4, 5, 6, 7, 8, 9, 14, 15, 16, 17, 18, 19, 20, 25, 26, 27, 28, 29, 30, 31,
             35, 36, 37, 38, 39, 40, 41)                # BL43's 28, in the card's order

BA122_FILE = "spec1_T26K__scan_58_A6_Q_4_0p075_0_inel.dat"
BAD_FILE = "CsV3Sb5_97K_0p5_0p5_3_bad_a03"
_N_BINS = 60
_ENERGY = np.round(np.arange(_N_BINS) * 0.2 - 4.0, 4)   # -4.0 .. 7.8 meV

_FAST_DEFAULTS = {"schema": "isar.fast-pass/1", "preferred_count": 1,
                  "count_probabilities": {"0": 0.05, "1": 0.95},
                  "peaks": [{"index": 0, "confident": True}], "flags": [], "models": []}
_DEEP_RESOLVED = {"schema": "isar.deep-pass/1", "state": "resolved",
                  "reason": "resolved: count 1 (P 0.91)", "best_count": 1, "p_best": 0.91,
                  "close_call": {"numerical": False}}


# ---------------------------------------------------------------------------
# data files
# ---------------------------------------------------------------------------
def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


def _analyzer_file(path: Path, number: int, *, with_q: bool = True) -> None:
    """One analyzer file in ``sp8_bl43.emit_file``'s six-column ``_i`` layout."""
    monitor, secs = 1e5, 10.0
    rows = ["#      meV      secs   npts          norm_i             y_i            ye_i"]
    for i, energy in enumerate(_ENERGY):
        counts = 25 + i % 7
        rows.append(f"{energy:10.4f} {secs:9.1f} {int(round(secs / 10)):6d} {monitor:15.7E} "
                    f"{counts / monitor:15.7E} {counts ** 0.5 / monitor:15.7E}")
    if with_q:
        rows.append(f"#Q=   {20.0 + number / 10:.3f} /nm")
    rows.append("#T=  97.000 K")
    _write(path, "\n".join(rows) + "\n")


def _var_file(path: Path, numbers) -> None:
    lines = [f' file_name_prefix = "{path.stem}"', " silicon_order =    12",
             " meV_bin_size =    0.20", " nscans =     2", "", f"array ano[{len(numbers)}]", ""]
    for i, number in enumerate(numbers, 1):
        lines += [f"ano[{i}] = {number}", f"analeff[{i}] = 0.5{i}", f"anal_polfactor[{i}] = 0.9{i}",
                  f"elpk_centroid_meV[{i}] = 0.0{i}", f"elpk_area_meV[{i}] = 0.2E-0{i}", ""]
    _write(path, "\n".join(lines))


def write_bl43_files(folder: Path, n_analyzers: int = 4) -> Path:
    """Write the incoming data files into ``folder`` (created); return it."""
    folder.mkdir(parents=True, exist_ok=True)
    numbers = ANALYZERS[:n_analyzers]
    for stem in (STEM_A, STEM_B):
        for number in numbers:
            # set A's a05 has no #Q= line: a missing Q the scan list must show as a dash
            _analyzer_file(folder / f"{stem}_a{number:02d}", number,
                           with_q=not (stem == STEM_A and number == 5))
        _var_file(folder / f"{stem}.var", numbers)
    counts = (np.arange(8) + 1.0) ** 2                 # N = (y/e)^2 recovers exactly
    energies = np.round(np.arange(8) * 0.2, 4)
    _write(folder / BA122_FILE, "\n".join(
        f"{e:.4f} {n:.6E} {n ** 0.5:.6E}" for e, n in zip(energies, counts)) + "\n")
    _write(folder / f"{STEM_B}_a07", "garbage\n")        # a member left out of a read set
    _write(folder / BAD_FILE, "not a data file\n")       # a set that fails as a whole
    _write(folder / "notes.txt", "beamtime notes\n")     # unrecognised
    return folder


# ---------------------------------------------------------------------------
# stored fits
# ---------------------------------------------------------------------------
def outcome(det: str, fast_state: str, *, deep: dict | None = None) -> dict:
    """A minimal stored ``FitOutcome`` dict (the ledger form) with a fast-pass record."""
    oc = {"detector_id": det, "verdict": "resolved", "disposition": "resolved", "T": 97.0,
          "q": None, "omega0": 5.0, "omega0_err": 0.1, "gamma": 0.5, "n_modes": 1,
          "best_label": "1 DHO",
          "modes": [{"omega0": 5.0, "omega0_err": 0.1, "gamma": 0.5, "amplitude": 100.0}],
          "fast_pass": {**_FAST_DEFAULTS, "state": fast_state,
                        "deep_pass_candidate": fast_state != "resolved"}}
    if deep is not None:
        oc["deep_pass"] = dict(deep)
    return oc


def _load(ws_root: Path):
    from isar.session.workspace import Workspace
    ws = Workspace.open(ws_root)
    card, session = ws.load()
    return ws, card, session


def _save(ws, card, session) -> None:
    from isar.session.outcome import _write_results_csv
    _write_results_csv(ws, session)
    ws.save(card, session)


def stamp_fit(ws_root: Path, unit_key: str, fast_state: str, *, deep: dict | None = None) -> None:
    """Make ``unit_key`` a fitted spectrum with the given fast-pass state (and deep-pass record)."""
    ws, card, session = _load(ws_root)
    entry = session.scans[unit_key]
    det = unit_key.rsplit(":", 1)[1]
    entry.detectors = {det: outcome(det, fast_state, deep=deep)}
    entry.status, entry.reasons = "fitted", []
    _save(ws, card, session)


def stamp_status(ws_root: Path, unit_key: str, status: str, reasons: list[str]) -> None:
    """Give ``unit_key`` a non-fitted ledger status (``held_for_calibration``, ``excluded``) and reasons."""
    ws, card, session = _load(ws_root)
    entry = session.scans[unit_key]
    entry.detectors = {}
    entry.status, entry.reasons = status, list(reasons)
    _save(ws, card, session)


def build_bl43_workspace(root: Path, n_analyzers: int = 4, *, fits: bool = True) -> Path:
    """Write the data under ``root/incoming``, ingest it into ``root/ws``, stamp the fits; return ``ws``."""
    from isar.parsers import sp8_bl43
    from isar.session import ingest
    from isar.session.state import SessionState
    from isar.session.workspace import Workspace

    incoming = write_bl43_files(Path(root) / "incoming", n_analyzers)
    ws_root = Path(root) / "ws"
    ws_root.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(sp8_bl43._CARD_PATH, ws_root / "card.policy.toml")   # the shipped card, verbatim
    (ws_root / "card.state.json").write_text("{}", encoding="utf-8", newline="\n")
    SessionState().save(ws_root / "session.json")
    ws = Workspace.open(ws_root)
    card, session = ws.load()
    ingest.ingest_batch(card, session, [incoming], data_dir=ws_root / "data")
    ws.save(card, session)
    if fits:
        stamp_fit(ws_root, f"{KEY_A}:a03", "resolved")
        stamp_fit(ws_root, f"{KEY_A}:a04", "close_call", deep=_DEEP_RESOLVED)
        stamp_fit(ws_root, f"{KEY_A}:a06", "search_limited")
        stamp_status(ws_root, f"{KEY_B}:a03", "held_for_calibration",
                     ["a03: no resolution curve for a03 at slit 60x60"])
        stamp_status(ws_root, f"{KEY_B}:a04", "excluded", ["undersampled"])
    else:
        from isar.session.outcome import _write_results_csv
        _write_results_csv(ws, session)
    return ws_root


def pooled_workspace(root: Path) -> Path:
    """A synthetic TAVI workspace with one pooled master ``tavi:900:det`` of ``tavi:11:det`` and
    ``tavi:12:det``."""
    from synthetic_ws import detector_outcome, scan_entry, stamp_ws
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    members = ["tavi:11:det", "tavi:12:det"]
    scans = {
        "tavi:11:det": scan_entry(11, detectors={"det": detector_outcome(omega0=5.0, gamma=0.5)}),
        "tavi:12:det": scan_entry(12, detectors={"det": detector_outcome(omega0=5.1, gamma=0.5)}),
        "tavi:900:det": scan_entry(900, detectors={"det": detector_outcome(
            omega0=5.05, gamma=0.5, pooled=True, n_members=2, members=members,
            q=[2.0, 0.0, 0.0], T=200.0)}),
    }
    return stamp_ws(root, scans=scans)


# ---------------------------------------------------------------------------
# the scan list, written out by hand
# ---------------------------------------------------------------------------
_NOT_IN_FILE = "not in the file"
_MISSING_SLIT = Fact(None, None, "missing", "missing", _NOT_IN_FILE)
_NOT_RUN = Status("not run", False)


def _incoming_path(ws_root: Path, name: str) -> str:
    return str((Path(ws_root).parent / "incoming" / name).resolve())


def _fit(key: str):
    return types.SimpleNamespace(unit_id=key, pooled=False)   # `fit` is excluded from equality


def _bl43_spectrum(ws_root: Path, stem: str, set_key: str, number: int, *, fast: Status,
                   deep: Status = _NOT_RUN, fitted: bool = False, with_q: bool = True) -> SpectrumRow:
    det = f"a{number:02d}"
    key = f"{set_key}:{det}"
    q = (Fact(round(20.0 + number / 10, 3), "nm⁻¹", "header", "file") if with_q
         else Fact(None, None, "missing", "missing", _NOT_IN_FILE))
    parsed = (Status("ok", True) if with_q
              else Status("no Q", True, "read; Q is not in the file"))
    name = f"{stem}_{det}"
    return SpectrumRow(key=key, set_key=set_key, analyzer=det, q=q, parsed=parsed, fast=fast,
                       deep=deep, file_name=name, file_path=_incoming_path(ws_root, name),
                       n_points=_N_BINS, fit=_fit(key) if fitted else None)


def sample_snapshot(ws_root: Path) -> ScanListSnapshot:
    """What ``scan_list()`` must return for ``build_bl43_workspace(root)`` with ``n_analyzers=4``
    (worked examples 1 to 5 and 7 of the scan-list read)."""
    ws_root = Path(ws_root)
    temp = Fact(97.0, "K", "header", "file")
    a = [_bl43_spectrum(ws_root, STEM_A, KEY_A, 3, fast=Status("resolved", True), fitted=True),
         _bl43_spectrum(ws_root, STEM_A, KEY_A, 4, fast=Status("close_call", True),
                        deep=Status("resolved", True, "resolved: count 1 (P 0.91)"), fitted=True),
         _bl43_spectrum(ws_root, STEM_A, KEY_A, 5, fast=_NOT_RUN, with_q=False),
         _bl43_spectrum(ws_root, STEM_A, KEY_A, 6, fast=Status("search_limited", True), fitted=True)]
    b = [_bl43_spectrum(ws_root, STEM_B, KEY_B, 3, fast=Status(
            "held", False, "a03: no resolution curve for a03 at slit 60x60")),
         _bl43_spectrum(ws_root, STEM_B, KEY_B, 4, fast=Status("not fitted", False, "undersampled")),
         _bl43_spectrum(ws_root, STEM_B, KEY_B, 5, fast=_NOT_RUN),
         _bl43_spectrum(ws_root, STEM_B, KEY_B, 6, fast=_NOT_RUN)]
    unread_member = UnreadFile(
        path=_incoming_path(ws_root, f"{STEM_B}_a07"), name=f"{STEM_B}_a07",
        reason="a07: has 1 columns, expected energy, intensity, error (left out of this scan)",
        set_key=KEY_B)
    key58 = f"{KEY_SINGLE}:a06"
    single = SpectrumRow(
        key=key58, set_key=KEY_SINGLE, analyzer="a06",
        q=Fact((4.0, 0.075, 0.0), "rlu", "filename", "file"), parsed=Status("ok", True),
        fast=_NOT_RUN, deep=_NOT_RUN, file_name=BA122_FILE,
        file_path=_incoming_path(ws_root, BA122_FILE), n_points=8)
    sets = (
        ScanSetRow(key=KEY_SINGLE, label="58", experiment_id="spec1", scan_numbers=(58,),
                   summed=False, n_merged=None, temperature=Fact(26.0, "K", "filename", "file"),
                   slit=_MISSING_SLIT, spectra=(single,), files_received=1),
        ScanSetRow(key=KEY_B, label=STEM_B, experiment_id=STEM_B, scan_numbers=(), summed=True,
                   n_merged=2, temperature=temp, slit=_MISSING_SLIT, spectra=tuple(b),
                   unread=(unread_member,), files_received=5),
        ScanSetRow(key=KEY_A, label=STEM_A, experiment_id=STEM_A, scan_numbers=(), summed=True,
                   n_merged=2, temperature=temp, slit=_MISSING_SLIT, spectra=tuple(a),
                   files_received=4),
    )
    not_read = (
        UnreadFile(path=_incoming_path(ws_root, BAD_FILE), name=BAD_FILE,
                   reason=("ValueError: CsV3Sb5_97K_0p5_0p5_3_bad: no analyzer could be read (a03: "
                           "energy column is not numeric (could not convert string to float: 'not'))")),
        unread_member,
        UnreadFile(path=_incoming_path(ws_root, "notes.txt"), name="notes.txt",
                   reason="not recognised as a data file"),
    )
    return ScanListSnapshot(sets=sets, files_not_read=not_read, pooled=())

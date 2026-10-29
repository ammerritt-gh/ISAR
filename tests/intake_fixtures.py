"""Generated BL43LXU workspaces for the intake tests (importable by bare name, like ``synthetic_ws``).

Everything is generated at test time in the CsV3Sb5 layout with the shipped card, plus a SARF curve
library with three settings; nothing reads ``real_data/``. ``tests/session/test_bl43_resolution.py``
imports its generators from here.
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np

from isar.analyze.lineshapes import convolute_voigt_dho
from isar.intake.settings import save_resolution_folder
from isar.parsers import sp8_bl43
from isar.session.workspace import Workspace

SETTINGS = {"60x60": 0.9, "40x40": 1.3, "20x20": 2.0}      # slit -> curve FWHM (meV) in the generated library
TRUE_SLIT = "40x40"
ZERO = 0.15                                                 # where the generated elastic line really sits
STEM = "Smp_100K_0p5_0p5_3"
ENERGY = np.round(np.arange(-8.0, 8.0001, 0.2), 4)
ENERGY_120 = np.round(np.arange(-12.0, 11.9999, 0.2), 4)    # 120 bins: an off-integer analyzer is off in 40
ANALYZERS = ("a03", "a04", "a05")


def curve_values(e, fwhm):
    """An asymmetric curve with a long tail (not a Voigt, so a Voigt cannot stand in for it)."""
    s = fwhm / 2.355
    return np.exp(-0.5 * (e / s) ** 2) + 0.03 * (1 + 0.5 * np.tanh(e)) / (1 + (e / (2.5 * fwhm)) ** 2)


def write_library(folder: Path, settings=SETTINGS, analyzers=ANALYZERS, *, skip=(), prefix="sarf_t"):
    """`<prefix>_s<n>_<h>_<v>_aNN` files for every analyzer and setting, minus the `skip` pairs."""
    folder.mkdir(parents=True, exist_ok=True)
    grid = np.round(np.arange(-15.0, 15.0001, 0.1), 4)
    for n, (setting, fwhm) in enumerate(settings.items(), start=1):
        h, v = setting.split("x")
        for det in analyzers:
            if (det, setting) in skip:
                continue
            rows = ["# SARF generated for the test", "# center of FWHM at zero", "#E_meV SARF fit"]
            rows += [f" {e:8.3f} {y:12.5E} {y:12.5E}" for e, y in zip(grid, curve_values(grid, fwhm))]
            (folder / f"{prefix}_s{n}_{h}_{v}_{det}").write_text("\n".join(rows) + "\n", encoding="utf-8",
                                                               newline="\n")


def write_scan(folder: Path, stem=STEM, analyzers=ANALYZERS, *, slit=None, seed=1, monitors=None, weak=(),
               hkl=None, zero=ZERO, true_slit=TRUE_SLIT, centroids=None, energy=ENERGY, off_integer=(),
               off_step=3, q_nm=None):
    """One scan in the CsV3Sb5 layout: an elastic line at `zero` through the `true_slit` curve, one DHO
    mode at 4 meV, a flat background; Poisson counts. `slit` = (h, v) written as header facts.
    `q_nm` (analyzer -> |Q| in 1/nm) writes the trailing `#Q=` line of each analyzer it names.
    `centroids` (analyzer -> beamline elastic centroid, meV) writes the `.var` sidecar: every analyzer is
    listed, and only the analyzers in the mapping carry `elpk_centroid_meV`. An analyzer in `off_integer`
    has 5 counts added to every bin and its printed error set so that (y/e)^2 is about half a count off an
    integer in every `off_step`-th bin: every third gives 40 of 120 on `ENERGY_120`, a warning-level
    `counts_not_integer`; a sparse step (60: 2 of 120) gives the notice level."""
    from isar.analyze.resolution import TabulatedResolution
    folder.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    grid = np.round(np.arange(-15.0, 15.0001, 0.1), 4)
    if centroids is not None:
        var = ["silicon_order = 4", "meV_bin_size = 0.2", "nscans = 1", f"array ano[{len(analyzers)}]"]
        var += [f"ano[{i}] = {int(det[1:])}" for i, det in enumerate(analyzers)]
        var += [f"elpk_centroid_meV[{i}] = {centroids[det]}" for i, det in enumerate(analyzers) if det in centroids]
        (folder / f"{stem}.var").write_text("\n".join(var) + "\n", encoding="utf-8", newline="\n")
    for det in analyzers:
        curve = TabulatedResolution(grid, curve_values(grid, SETTINGS[true_slit]), det, true_slit)
        a_ph, a_el = (0.0, 0.0) if det in weak else (40.0, 900.0)       # weak: background only
        mean = convolute_voigt_dho(energy, 4.0, zero, 0.0, 0.0, 0.6, a_ph, a_el, 1.0, 100.0,
                                   resolution_curve=curve)
        counts = rng.poisson(np.clip(mean, 0, None)).astype(float)
        err_counts = counts.copy()                                      # what the printed error squares to
        if det in off_integer:
            counts += 5.0
            err_counts = counts.copy()
            err_counts[::off_step] += 0.5
        mon = (monitors or {}).get(det, 1.0e5)
        rows = []
        if slit:
            rows += [f"# agaph_mm {slit[0]}", f"# agapv_mm {slit[1]}"]
        if hkl:
            rows.append("# hkl " + " ".join(str(h) for h in hkl))
        rows.append("#      meV      secs   npts          norm_i             y_i            ye_i")
        rows += [f"{e:10.4f} {10.0:9.1f} {1:6d} {mon:15.7E} {n / mon:15.7E} {np.sqrt(ne) / mon:15.7E}"
                 for e, n, ne in zip(energy, counts, err_counts)]
        if q_nm and det in q_nm:
            rows.append(f"#Q=   {q_nm[det]:.3f} /nm")
        (folder / f"{stem}_{det}").write_text("\n".join(rows) + "\n", encoding="utf-8", newline="\n")


def write_gpi(data_folder: Path, set_name: str, *, q_nm, hkl, slit=(40, 40), sarf_name="sarf_z_s1_40_40",
              problem_name=None) -> Path:
    """`data_folder/gpi_files/gpi.<set_name>` in the beamline macro's layout (banner, `set` lines, names,
    `sarf=1; if (...)`, the slit with `#mm`, and per analyzer its HKL line, `Q_aNN` and a dQ line). A None in
    `q_nm` or `hkl` (analyzer -> value) leaves that line out; `slit=None` leaves agaph/agapv out. Every
    number is the caller's, none measured."""
    folder = data_folder / "gpi_files"
    folder.mkdir(parents=True, exist_ok=True)
    rows = ["#################################################", f"# gnuplot macro for {set_name} (generated)",
            "#################################################", "set terminal postscript enhanced color",
            'set xlabel "Energy (meV)"', 'set ylabel "Counts"', "",
            f"problem_name1='{problem_name if problem_name is not None else set_name}'",
            "resolution_name = 'rf_t_s1_40_40'"]
    if sarf_name is not None:
        rows.append(f"sarf_name = '{sarf_name}'")
    rows.append('sarf=1; if (resolution_name eq "") sarf=0')
    if slit is not None:
        rows += [f"agaph = {slit[0]:g} #mm", f"agapv = {slit[1]:g} #mm"]
    for det in sorted(set(q_nm) | set(hkl)):
        h = hkl.get(det)
        if h is not None:
            rows.append(f'hkl_{det} = "({h[0]:g}, {h[1]:g}, {h[2]:g})"; hkl_{det}_H = {h[0]:g}; '
                        f"hkl_{det}_K = {h[1]:g}; hkl_{det}_L = {h[2]:g}")
        if q_nm.get(det) is not None:
            rows.append(f'Q_{det} = " {q_nm[det]:.2f}/nm"')
        rows.append(f'dQ_{det} = "(0.01, 0, 0)"; dQ_{det}_H = 0.01; dQ_{det}_K = 0; dQ_{det}_L = 0')
    rows += ["", f"plot '{set_name}_a03' u 1:2 w lp title problem_name1"]
    path = folder / f"gpi.{set_name}"
    path.write_text("\n".join(rows) + "\n", encoding="utf-8", newline="\n")
    return path


def workspace(tmp_path, *, merge="separate", sample=None):
    """An empty workspace holding the shipped BL43 card (with `merge` and an optional `[sample] name`)."""
    ws = Workspace.open(tmp_path / "ws")
    ws.root.mkdir(parents=True, exist_ok=True)
    text = (Path(sp8_bl43.__file__).parent / "cards" / "bl43lxu.policy.toml").read_text(encoding="utf-8")
    text = text.replace('merge = "separate"', f'merge = "{merge}"')
    if sample:
        text += f'\n[sample]\nname = "{sample}"\n'
    ws.policy_path.write_text(text, encoding="utf-8", newline="\n")
    return ws


def bl43_workspace(tmp_path, *, stems=("A", "B"), analyzers=ANALYZERS, slit=None, weak=(), library_skip=(),
                   off_integer=()):
    """``(ws, data_folder)``: a BL43 workspace (experiment ``Smp``) with the curve library in
    ``ws.root / "rf"``, saved as the experiment's resolution folder (it is not beside the data, so
    discovery would not find it), and one scan per stem (``Smp_100K_0p5_0p5_3_<stem>``) on the 120-bin
    axis. Nothing is imported yet."""
    ws = workspace(tmp_path, sample="Smp")
    write_library(ws.root / "rf", analyzers=analyzers, skip=library_skip)
    assert save_resolution_folder(ws.root, ws.root / "rf").ok
    data = tmp_path / "data"
    for i, s in enumerate(stems):
        write_scan(data, stem=f"{STEM}_{s}", analyzers=analyzers, slit=slit, seed=1 + i, weak=weak,
                   energy=ENERGY_120, off_integer=off_integer)
    return ws, data


def records_for(ws, data_folder) -> list:
    """The parsed, reduced and QC'd records of the folder, as ingest and the run hash them."""
    from isar.reduce import combine, qc
    from isar.session import ingest
    card, _session = ws.load()
    recs = ingest.parse_all(ingest.discover([data_folder], card), card)
    for r in recs:
        combine.reduce_record(r, card)
        qc.qc_record(r, card)
    return recs


def rows_for(ws, data_folder) -> list[dict]:
    """The import-record rows `ingest_batch` would write for the folder (C-scan-record)."""
    from isar.intake import scans
    from isar.session.state import content_hash
    return [scans.scan_row(r, content_hash(r)) for r in records_for(ws, data_folder)]


def log_lines(*events) -> list[dict]:
    """C-fit-inputs lines from compact events, in order:
    ``("start", run, passes)``, ``("started", run, unit, pass[, inputs, hash])``,
    ``("finished", run, unit, pass[, status])``, ``("end", run, outcome)``."""
    out = []
    for i, ev in enumerate(events):
        at = f"2026-10-28T10:00:{i:02d}+00:00"
        kind, run = ev[0], ev[1]
        if kind == "start":
            out.append({"kind": "run", "event": "start", "run": run, "at": at, "passes": list(ev[2]), "pid": os.getpid()})
        elif kind == "end":
            out.append({"kind": "run", "event": "end", "run": run, "at": at, "outcome": ev[2]})
        elif kind == "started":
            unit, pass_ = ev[2], ev[3]
            out.append({"kind": "started", "run": run, "at": at, "unit": unit, "scan": unit.rsplit(":", 1)[0],
                        "pass": pass_, "inputs": ev[4] if len(ev) > 4 else {}, "hash": ev[5] if len(ev) > 5 else "h",
                        "fallbacks": []})
        elif kind == "finished":
            unit, pass_ = ev[2], ev[3]
            out.append({"kind": "finished", "run": run, "at": at, "unit": unit, "scan": unit.rsplit(":", 1)[0],
                        "pass": pass_, "status": ev[4] if len(ev) > 4 else "fitted", "fallbacks": [], "result": None})
        else:
            raise ValueError(f"unknown event {kind!r}")
    return out

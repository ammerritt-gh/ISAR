"""PROOF (real data, run on purpose): the fast pass on the le Tacon BL43LXU CsV3Sb5 spectra.

Gated like the golden proofs: the module is skipped unless ``ISAR_LETACON=1`` (it takes tens of
minutes), so run it with ``run-isar-proofs.bat letacon`` or

    ISAR_LETACON=1 python -m pytest proofs/test_letacon_fast_pass.py -q -s

What it does, in the order the tests report it:

1. Each temperature folder (302 K, 97 K; 28 analyzers each) goes through ``isar init`` and
   ``isar run --workers 4`` with render and scene on, the GUI's command, as a child process whose
   environment has none of the pytest-only caps (``ISAR_WORKERS``, ``ISAR_BOOTSTRAP_WORKERS``,
   ``ISAR_ESCALATION_N_LIVE``, ``ISAR_MODE_CEILING``). It prints the batch wall time, the stage split and
   the per-spectrum fast-pass median, p90 and max.
2. Analyzer a06 (the one at the M point) was calibrated with the resolution file recorded by hand in
   the expectation file. This can fail: it checks the analyzer-to-curve mapping.
3. It prints a06's fitted peaks beside the paper's phonons A-D (undamped energy E0 against E0). The paper
   is a reference, not the truth: it fixed amplitudes across temperatures, a single spectrum cannot.
   ``must_match`` and ``match_tolerance_meV`` in the expectation file start empty; the operator sets them.
4. The prior sensitivity check: all 56 spectra refitted under each of the other
   ``prior.SENSITIVITY_ARMS``, then which count decisions and confident-peak lists hold across the arms.

No measured or paper value is written here: the expectation file
``real_data/leTacon_SP8_CsV3Sb5/test_letacon_fast_pass.expect.json`` carries
``{analyzer, q_hkl, q_source, temperatures, resolution_file{T: name},
paper{source, T: {A..D: {omega0, Gamma|null, note}}}, match_tolerance_meV|null, must_match[]}``.
A ``must_match`` entry is ``{"T": 302, "peak": "B"}``. Results land in
``out/letacon_fast_pass/<timestamp>/`` (gitignored): workspaces, logs and ``summary.json``.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import fields
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

from isar.analyze.prior import SENSITIVITY_ARMS, PriorSettings
from isar.parsers import sarf
from isar.session.outcome import fast_pass_of
from isar.session.workspace import Workspace
from proofs.conftest import REAL_DATA

if os.environ.get("ISAR_LETACON") != "1":
    pytest.skip("the le Tacon fast-pass proof runs only when named: run-isar-proofs.bat letacon "
                "(sets ISAR_LETACON=1)", allow_module_level=True)

# pyproject's 900 s per-test timeout is the routine suite's guard; this proof runs for an hour and every
# subprocess carries its own timeout (TIMEOUT_S), so the global one is switched off for it.
pytestmark = pytest.mark.timeout(0)

ROOT = Path(__file__).resolve().parents[1]
FOLDER = "leTacon_SP8_CsV3Sb5"
TEMPERATURES = (302, 97)
ANALYZER = "a06"
N_ANALYZERS = 28
BATCH_WORKERS = 4                     # the laptop's core count: what the batch budget is stated for
TIMEOUT_S = 3600
ONE_FWHM = 1.2                        # meV: the beamline's nominal 1.1 meV resolution, 1.1-1.2 on most analyzers
# Set by pytest in the repo-root conftest; the child must run with the production values.
DROPPED_ENV = ("ISAR_WORKERS", "ISAR_BOOTSTRAP_WORKERS", "ISAR_ESCALATION_N_LIVE", "ISAR_MODE_CEILING")
ARMS = [name for name in SENSITIVITY_ARMS if name != "recipe"]


# ---------------------------------------------------------------- running isar as the GUI does
def _child_env():
    env = {k: v for k, v in os.environ.items() if k not in DROPPED_ENV}
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONUTF8"] = "1"
    return env


def _isar(args, log, what):
    """Run ``python -m isar <args>`` from the repo root; stdout+stderr go to ``log``. A run past its
    timeout is killed with its pool children and fails naming the timeout. Returns (exit code, wall s)."""
    t0 = time.perf_counter()
    with open(log, "w", encoding="utf-8", newline="\n") as fh:
        proc = subprocess.Popen([sys.executable, "-m", "isar", *map(str, args)], cwd=ROOT, env=_child_env(),
                                stdout=fh, stderr=subprocess.STDOUT)
        try:
            code = proc.wait(timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
            pytest.fail(f"{what} timed out after {TIMEOUT_S} s and was killed (log: {log})", pytrace=False)
    return code, time.perf_counter() - t0


def _prior_table(arm):
    """The ``[fitting.prior]`` text for one sensitivity arm: only the fields that differ from the recipe."""
    default, arm_settings = PriorSettings(), SENSITIVITY_ARMS[arm]
    lines = [f"{f.name} = {json.dumps(getattr(arm_settings, f.name))}" for f in fields(PriorSettings)
             if getattr(arm_settings, f.name) != getattr(default, f.name)]
    return "\n[fitting.prior]\n" + "\n".join(lines) + "\n"


def _init_workspace(data, ws, rf, log, prior_arm=None):
    """``isar init`` on ``data``, then the card's resolution folder made absolute (and, for a sensitivity
    arm, its prior table appended) the way ``proofs/test_bl43_resolution.py`` does."""
    code, _ = _isar(["init", data, "--out", ws], log, "isar init")
    assert code == 0, f"isar init failed (exit {code}); log: {log}"
    policy = ws / "card.policy.toml"
    text = policy.read_text(encoding="utf-8")
    assert 'resolution_folder = "rf"' in text
    text = text.replace('resolution_folder = "rf"', f'resolution_folder = "{rf.as_posix()}"')
    if prior_arm:
        text += _prior_table(prior_arm)
    policy.write_text(text, encoding="utf-8", newline="\n")


def _fits(ws):
    """{(T, analyzer): (entry, fast-pass record)} of every fitted ledger entry of a workspace."""
    _card, session = Workspace.open(ws).load()
    out = {}
    for entry in session.scans.values():
        if entry.status != "fitted":
            continue
        t = round(float(entry.provenance["T"]))
        for det, outcome in entry.detectors.items():
            out[(t, det)] = (entry, fast_pass_of(outcome))
    return out


def _timings(ws):
    return json.loads((ws / "report" / "run_timings.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- fixtures
@pytest.fixture(scope="module")
def real():
    folder, rf = REAL_DATA / FOLDER, REAL_DATA / FOLDER / "rf"
    for path in (folder, rf):
        if not path.is_dir():
            pytest.fail(f"proof needs real data that is missing: {path}", pytrace=False)
    return folder, rf


@pytest.fixture(scope="module")
def out_dir():
    path = ROOT / "out" / "letacon_fast_pass" / datetime.now().strftime("%Y%m%d-%H%M%S")
    path.mkdir(parents=True)
    print(f"\nle Tacon proof output: {path}")
    return path


@pytest.fixture(scope="module")
def data_dirs(real, out_dir):
    """{T: folder holding that temperature's files}: the files named ``CsV3Sb5_<T>K_*`` copied beside each other."""
    folder, _rf = real
    dirs = {}
    for t in TEMPERATURES:
        dirs[t] = out_dir / f"in_{t}"
        dirs[t].mkdir()
        files = sorted(folder.glob(f"CsV3Sb5_{t}K_*"))
        assert files, f"no CsV3Sb5_{t}K_* files in {folder}"
        for f in files:
            shutil.copy2(f, dirs[t] / f.name)
    return dirs


@pytest.fixture(scope="module")
def batches(real, out_dir, data_dirs):
    """{T: {ws, exit, wall, log}}: each temperature's 28 analyzers through ``isar run``, one after the other."""
    _folder, rf = real
    done = {}
    for t in TEMPERATURES:
        ws = out_dir / f"ws_{t}"
        _init_workspace(data_dirs[t], ws, rf, out_dir / f"init_{t}.log")
        log = out_dir / f"run_{t}.log"
        code, wall = _isar(["run", data_dirs[t], "--workspace", ws, "--workers", BATCH_WORKERS], log,
                           f"{t} K batch")
        done[t] = {"ws": ws, "exit": code, "wall": wall, "log": log}
    return done


def _percentiles(values):
    return float(np.median(values)), float(np.percentile(values, 90)), float(np.max(values))


# ---------------------------------------------------------------- tests
def test_each_batch_fits_all_28_analyzers_with_timing(batches, capsys):
    problems = []
    with capsys.disabled():
        for t, b in batches.items():
            if b["exit"] != 0:
                problems.append(f"{t} K: isar run exited {b['exit']} (log {b['log']})")
                continue
            fits = _fits(b["ws"])
            fitted = [k for k, (_e, fp) in fits.items() if fp.get("schema")]
            if len(fits) != N_ANALYZERS or len(fitted) != N_ANALYZERS:
                problems.append(f"{t} K: {len(fits)} fitted ledger entries, {len(fitted)} with a fast_pass "
                                f"record (expected {N_ANALYZERS} each)")
            timings = _timings(b["ws"])
            stages = {s["stage"]: s for s in timings["stages"]}
            for needed in ("fit_scans", "scene_observe"):
                if needed not in stages:
                    problems.append(f"{t} K: run_timings.json has no {needed} stage")
            per_task = (stages.get("fit_scans") or {}).get("per_task") or []
            secs = [x["fast_pass"]["total_seconds"] for x in per_task if (x.get("fast_pass") or {}).get("total_seconds")]
            print(f"\n{t} K batch ({BATCH_WORKERS} workers, render and scene on): outer wall {b['wall']:.1f} s, "
                  f"isar total {timings.get('total_seconds')} s")
            print("  stages: " + ", ".join(f"{n} {s['seconds']:.1f}" for n, s in stages.items()))
            if secs:
                med, p90, mx = _percentiles(secs)
                print(f"  fast pass per spectrum over {len(secs)}: median {med:.1f} s, p90 {p90:.1f} s, max {mx:.1f} s")
                stops = {}
                for x in per_task:
                    r = (x.get("fast_pass") or {}).get("stop_reason")
                    stops[r] = stops.get(r, 0) + 1
                print(f"  stop reasons: {stops}")
            else:
                problems.append(f"{t} K: no per-spectrum fast-pass times in run_timings.json")
    assert not problems, "\n".join(problems)


def test_a06_was_calibrated_with_the_recorded_resolution_file(batches, expect):
    exp = expect(FOLDER)
    assert exp["analyzer"] == ANALYZER, "the expectation file names a different analyzer"
    problems = []
    for t, b in batches.items():
        card, _session = Workspace.open(b["ws"]).load()
        scans = card.state.tabulated_calibration
        assert len(scans) == 1, f"{t} K: expected one calibrated scan, got {list(scans)}"
        scan = next(iter(scans.values()))
        det = scan["detectors"].get(ANALYZER)
        if det is None or det.get("held_reason"):
            problems.append(f"{t} K: {ANALYZER} has no calibration ({(det or {}).get('held_reason')})")
            continue
        ref = {"folder": scan["folder"], "pattern": scan["pattern"], "fingerprint": scan["library"],
               "detector": ANALYZER, "setting": scan["setting"]}
        used = Path(sarf.curve_from_reference(ref).source).name
        want = exp["resolution_file"][str(t)]
        print(f"\n{t} K: {ANALYZER} used resolution file {used} (slit {scan['setting']}, "
              f"{scan['setting_source']}); recorded {want}")
        if used != want:
            problems.append(f"{t} K: {ANALYZER} was calibrated with {used}, the record says {want}")
    assert not problems, "\n".join(problems)


def test_a06_peaks_beside_the_papers_A_to_D(batches, expect):
    exp = expect(FOLDER)
    tol, must = exp.get("match_tolerance_meV"), exp.get("must_match") or []
    print(f"\n{ANALYZER} at Q = {exp['q_hkl']} ({exp['q_source']}); paper: {exp['paper']['source']}")
    print("The paper fixed some amplitudes across temperatures; a single-spectrum fit cannot, so this is a "
          "reference, not the truth. Energies are E0 (undamped) on both sides.")
    nearest = {}
    for t, b in batches.items():
        entry, fp = _fits(b["ws"])[(t, ANALYZER)]
        peaks = fp.get("peaks") or []
        probs = fp.get("count_probabilities") or {}
        p_pref = probs.get(str(fp.get("preferred_count")))
        print(f"\n{t} K  {ANALYZER}: state {fp.get('state')}, preferred count {fp.get('preferred_count')} "
              f"(P {p_pref if p_pref is None else round(p_pref, 3)}), q {entry.provenance.get('q')}")
        print("  omega0   Gamma    kind       delta regime       status(omega0/Gamma/area)       area_stokes  area status")
        for p in peaks:
            s = p["status"]
            print(f"  {p['omega0']:6.2f}  {p['Gamma']:6.2f}   {'confident' if p['confident'] else 'tentative':9s}  "
                  f"{str(p['delta']):5s} {p['regime']:12s} {s['omega0']}/{s['Gamma']}/{s['area']}  "
                  f"{p['area_stokes']:11.1f}  {p['area_status']}")
        print("  paper phonon  E0     nearest fitted (any kind)   |d omega0|")
        for name, ref in sorted(exp["paper"][str(t)].items()):
            if not peaks:
                print(f"  {name:12s}  {ref['omega0']:5.2f}   (no peaks fitted)")
                continue
            near = min(peaks, key=lambda p: abs(p["omega0"] - ref["omega0"]))
            nearest[(t, name)] = abs(near["omega0"] - ref["omega0"])
            print(f"  {name:12s}  {ref['omega0']:5.2f}   {near['omega0']:6.2f} "
                  f"({'confident' if near['confident'] else 'tentative'})            {nearest[(t, name)]:.2f}   {ref.get('note') or ''}")
    if must:
        assert tol is not None, "must_match is set but match_tolerance_meV is null"
        misses = [f"{m['peak']} at {m['T']} K: nearest fitted peak is {nearest.get((m['T'], m['peak']), float('inf')):.2f} meV away"
                  for m in must if nearest.get((m["T"], m["peak"]), float("inf")) > tol]
        assert not misses, "; ".join(misses)


def _stable_list(a, b):
    """The same length, matched one-to-one (sorted order is the optimal 1-D matching) within one FWHM."""
    return len(a) == len(b) and all(abs(x - y) <= ONE_FWHM for x, y in zip(sorted(a), sorted(b)))


def _summary_of(fits):
    return {key: {"count": fp.get("preferred_count"), "state": fp.get("state"),
                  "confident": [p["omega0"] for p in fp.get("peaks") or [] if p["confident"]]}
            for key, (_e, fp) in fits.items()}


def test_prior_sensitivity_of_counts_and_confident_peaks(batches, real, out_dir, data_dirs, capsys):
    _folder, rf = real
    workers = os.environ.get("ISAR_LETACON_WORKERS", "4")
    arm_results = {"recipe": {}}
    for t in TEMPERATURES:
        arm_results["recipe"].update(_summary_of(_fits(batches[t]["ws"])))
    arm_walls = {}
    for arm in ARMS:
        ws = out_dir / f"ws_{arm}"
        _init_workspace(data_dirs[TEMPERATURES[0]], ws, rf, out_dir / f"init_{arm}.log", prior_arm=arm)
        code, wall = _isar(["run", *[data_dirs[t] for t in TEMPERATURES], "--workspace", ws, "--workers", workers,
                            "--no-render", "--no-scene"], out_dir / f"run_{arm}.log", f"sensitivity arm {arm}")
        assert code == 0, f"sensitivity arm {arm}: isar run exited {code}; log {out_dir / f'run_{arm}.log'}"
        fits = _fits(ws)
        assert len(fits) == len(TEMPERATURES) * N_ANALYZERS, f"arm {arm}: {len(fits)} fitted spectra"
        arm_results[arm], arm_walls[arm] = _summary_of(fits), wall
    assert len(arm_results["recipe"]) == len(TEMPERATURES) * N_ANALYZERS

    rows, totals = [], {"count_stable": 0, "count_dependent": 0, "list_stable": 0, "list_dependent": 0}
    moved = {arm: {"count": 0, "list": 0} for arm in ARMS}
    for key in sorted(arm_results["recipe"]):
        base = arm_results["recipe"][key]
        count_moved = [arm for arm in ARMS if (arm_results[arm][key]["count"], arm_results[arm][key]["state"])
                       != (base["count"], base["state"])]
        list_moved = [arm for arm in ARMS if not _stable_list(arm_results[arm][key]["confident"], base["confident"])]
        for arm in count_moved:
            moved[arm]["count"] += 1
        for arm in list_moved:
            moved[arm]["list"] += 1
        totals["count_dependent" if count_moved else "count_stable"] += 1
        totals["list_dependent" if list_moved else "list_stable"] += 1
        rows.append({"T": key[0], "analyzer": key[1], "arms": {a: arm_results[a][key] for a in arm_results},
                     "count_moved_by": count_moved, "list_moved_by": list_moved})
    (out_dir / "summary.json").write_text(json.dumps(
        {"arms": ["recipe", *ARMS], "arm_walls_s": arm_walls, "totals": totals, "moved_by_arm": moved,
         "spectra": rows}, indent=1), encoding="utf-8", newline="\n")
    with capsys.disabled():
        print("\nPrior sensitivity (arms: recipe, " + ", ".join(ARMS) + f"; {len(rows)} spectra)")
        for row in rows:
            if not (row["count_moved_by"] or row["list_moved_by"]):
                continue
            print(f"  {row['T']} K {row['analyzer']}: " + "; ".join(
                f"{a} {row['arms'][a]['count']}/{row['arms'][a]['state']} "
                f"{[round(x, 2) for x in sorted(row['arms'][a]['confident'])]}" for a in row["arms"])
                + f"   [count moved by {row['count_moved_by'] or '-'}; list moved by {row['list_moved_by'] or '-'}]")
        print(f"  count decision (preferred count and state): stable {totals['count_stable']}, "
              f"prior-dependent {totals['count_dependent']}")
        print(f"  confident-peak list: stable {totals['list_stable']}, prior-dependent {totals['list_dependent']}")
        print("  spectra moved per arm (count / list): " + ", ".join(f"{a} {m['count']}/{m['list']}" for a, m in moved.items()))
        print(f"  arm walls (s): {({a: round(w) for a, w in arm_walls.items()})}; summary: {out_dir / 'summary.json'}")

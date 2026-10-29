"""Helpers shared by the le Tacon real-data proofs (``proofs/test_letacon_*.py``): running ``isar`` as the GUI
does, the workspace set-up from the real folder, and reading a workspace's fits and timings.

Nothing here skips or collects: each proof module keeps its own gate (``ISAR_LETACON``, ``ISAR_LETACON_INTER``).
"""
import json
import os
import subprocess
import sys
import time
from dataclasses import fields
from pathlib import Path

import numpy as np
import pytest

from isar.analyze.prior import SENSITIVITY_ARMS, PriorSettings
from isar.intake import scans, values
from isar.intake.settings import save_resolution_folder
from isar.session.outcome import fast_pass_of
from isar.session.workspace import Workspace

ROOT = Path(__file__).resolve().parents[1]
FOLDER = "leTacon_SP8_CsV3Sb5"
TEMPERATURES = (302, 97)
N_ANALYZERS = 28
BATCH_WORKERS = 4                     # the laptop's core count: what the batch budget is stated for
TIMEOUT_S = 3600
# Set by pytest in the repo-root conftest; the child must run with the production values.
DROPPED_ENV = ("ISAR_WORKERS", "ISAR_BOOTSTRAP_WORKERS", "ISAR_ESCALATION_N_LIVE", "ISAR_MODE_CEILING")
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


def _init_workspace(datas, ws, rf, log, prior_arm=None):
    """``isar init`` on the first of ``datas``, then (for a sensitivity arm) its prior table appended, then
    ``isar ingest`` of EVERY folder the workspace will run, then the resolution folder ``rf`` and the 40x40
    slit saved in ISAR on every imported scan (a save refuses a scan the import record lacks)."""
    code, _ = _isar(["init", datas[0], "--out", ws], log, "isar init")
    assert code == 0, f"isar init failed (exit {code}); log: {log}"
    if prior_arm:
        policy = ws / "card.policy.toml"
        policy.write_text(policy.read_text(encoding="utf-8") + _prior_table(prior_arm), encoding="utf-8",
                          newline="\n")
    code, _ = _isar(["ingest", *datas, "--workspace", ws], log.with_name(log.stem + "_ingest.log"), "isar ingest")
    assert code == 0, f"isar ingest failed (exit {code}); log: {log.with_name(log.stem + '_ingest.log')}"
    assert save_resolution_folder(ws, rf).ok
    # The paper records the 40x40 slit; a saved slit is always kept (inferred, a06 gets 20x20).
    keys = list(scans.read_scans(ws))
    assert keys, "the import record holds no scan"
    saved = values.save_value(ws, "slit", "40x40", keys)
    assert saved.ok, saved


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

def _percentiles(values):
    return float(np.median(values)), float(np.percentile(values, 90)), float(np.max(values))

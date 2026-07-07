"""Run ISAR as a subprocess (WORKING — LLM_HARNESS_DESIGN §2.3, §4).

``isar run`` is idempotent — it re-parses everything and re-fits only new / content-drifted /
previously-held scans — so the driver just re-invokes it after saving each new scan. We shell out via
``python -m isar.cli.main`` (robust: does not depend on the ``isar`` console script being on PATH),
capture stdout/stderr, and raise on a nonzero exit. Running ISAR in a child process IS the analysis
boundary: the driver never imports a fit internal.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Sequence


class IsarRunError(RuntimeError):
    """``isar run`` exited nonzero. Carries the return code and captured stdout/stderr."""

    def __init__(self, returncode: int, stdout: str, stderr: str, cmd: Sequence[str]) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.cmd = list(cmd)
        super().__init__(
            f"`isar run` failed (exit {returncode}): {' '.join(map(str, cmd))}\n"
            f"--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}")


def init_isar(workspace: str | Path, data_path: str | Path, *, timeout: float | None = None):
    """Run ``isar init <data> --out <workspace>`` in a subprocess (idempotent guard is the caller's).

    The driver calls this lazily after the FIRST scan lands in the data dir — ``isar init`` needs a
    recognizable data file to detect the instrument, which does not exist when the campaign starts.
    Raises :class:`IsarRunError` on a nonzero exit.
    """
    cmd = [sys.executable, "-m", "isar.cli.main", "init", str(data_path), "--out", str(workspace)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise IsarRunError(proc.returncode, proc.stdout, proc.stderr, cmd)
    return proc


def run_isar(workspace: str | Path, data_paths: Sequence[str | Path], *, force: bool = False,
             extra_args: Sequence[str] | None = None, timeout: float | None = None):
    """Run ``isar run <data...> --workspace <workspace>`` in a subprocess.

    Args:
        workspace: the ISAR workspace directory (must already be `isar init`-ialized — it needs a
            ``card.policy.toml``).
        data_paths: the scan files (or directories) to feed ISAR. Typically the workspace data dir, or
            the individual ``{job_id}.json`` files written by :func:`save_scan_json`.
        force: pass ``--force`` to refit every scan, ignoring the ledger.
        extra_args: additional ``isar run`` flags (e.g. ``["--formats", "json"]``).
        timeout: optional subprocess timeout (seconds).

    Returns the completed :class:`subprocess.CompletedProcess`. Raises :class:`IsarRunError` on a
    nonzero exit.
    """
    paths = [str(p) for p in data_paths]
    if not paths:
        raise ValueError("run_isar needs at least one data path")
    cmd = [sys.executable, "-m", "isar.cli.main", "run", *paths, "--workspace", str(workspace)]
    if force:
        cmd.append("--force")
    if extra_args:
        cmd.extend(str(a) for a in extra_args)

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise IsarRunError(proc.returncode, proc.stdout, proc.stderr, cmd)
    return proc

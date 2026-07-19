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
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


@dataclass
class RunResult:
    """Structured result of one ``isar run`` invocation (the runner-seam return type).

    ``timings`` carries the subprocess stage-timing dict when a runner has it inline (the persistent
    worker will); the SubprocessRunner leaves it ``None`` — the loop reads ``report/run_timings.json``
    off disk instead (see :func:`isar.drive.loop._read_run_timings`)."""

    returncode: int
    stdout: str
    stderr: str
    timings: dict | None = None
    completed: bool = True


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


def _exec_run(cmd: Sequence[str], timeout: float | None) -> RunResult:
    """Run ``cmd`` capturing stdout/stderr; raise :class:`IsarRunError` on a nonzero exit.

    The single subprocess-execution point shared by :class:`SubprocessRunner` and the legacy
    :func:`run_isar` wrapper, so both preserve identical error semantics."""
    proc = subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)
    if proc.returncode == 75:
        return RunResult(proc.returncode, proc.stdout, proc.stderr, completed=False)
    if proc.returncode != 0:
        raise IsarRunError(proc.returncode, proc.stdout, proc.stderr, cmd)
    return RunResult(proc.returncode, proc.stdout, proc.stderr)


class SubprocessRunner:
    """Run ``isar run`` as a fresh subprocess per job — the cold-start invoker (also the worker's
    fallback). One instance is reused across a campaign; it holds no state, so :meth:`close` is a no-op.

    Boundary: the child process IS the analysis boundary; nothing here imports a fit internal.
    """

    supports_master_certification = True

    def run(self, workspace: str | Path, data_paths: Sequence[str | Path], *, force: bool = False,
            errors_tier: str = "full", render: bool = True, timeout: float | None = None,
            certification_manifest: str | Path | None = None,
            certification_request: str | Path | None = None,
            analysis_allowance_seconds: float | None = None,
            finish_initial_wave: bool = False) -> RunResult:
        """Run ``isar run <data...> --workspace <workspace> [--force] --errors <tier>``.

        Args:
            workspace: the ISAR workspace directory (must already be ``isar init``-ialized).
            data_paths: the scan files (or directories) to feed ISAR (typically the workspace data dir).
            force: pass ``--force`` to refit every scan, ignoring the ledger.
            errors_tier: the ``--errors`` tier (``cheap`` | ``fast`` | ``full``).
            render: whether the run should render human reports. ``False`` appends ``--no-render`` so
                the child skips report.md/.html/.json + figures (results.csv / card.state.json still
                written); campaign call sites pass ``render=False`` and re-render once at end.
            timeout: optional subprocess timeout (seconds).

        Returns a :class:`RunResult`. Raises :class:`IsarRunError` on a nonzero exit.
        """
        paths = [str(p) for p in data_paths]
        if not paths:
            raise ValueError("SubprocessRunner.run needs at least one data path")
        cmd = [sys.executable, "-m", "isar.cli.main", "run", *paths, "--workspace", str(workspace)]
        if force:
            cmd.append("--force")
        cmd.extend(["--errors", str(errors_tier)])
        if not render:
            cmd.append("--no-render")
        if certification_manifest is not None:
            cmd.extend(["--certification-manifest", str(certification_manifest)])
        if certification_request is not None:
            cmd.extend(["--certification-request", str(certification_request)])
        if analysis_allowance_seconds is not None:
            cmd.extend(["--analysis-allowance-seconds", str(float(analysis_allowance_seconds))])
        if finish_initial_wave:
            cmd.append("--finish-initial-wave")
        return _exec_run(cmd, timeout)

    def close(self) -> None:
        """No-op — the subprocess runner spawns per job and holds nothing to release."""


class _CallableRunner:
    """Adapt a bare ``(workspace, data_paths, extra_args=...) -> Any`` callable to the runner protocol.

    The wrapped callable is invoked EXACTLY as the loop invoked ``run_isar`` before the seam existed —
    ``obj(workspace, data_paths, extra_args=[...])`` with ``extra_args`` reconstructed from the
    structured ``(force, errors_tier)`` kwargs — so every existing config-supplied runner and test fake
    keeps working unchanged. Whatever the callable returns (``None``, an ``int``, a
    :class:`subprocess.CompletedProcess`, a :class:`RunResult`, ...) is coerced to a :class:`RunResult`.
    """

    # Bare historical hooks only receive ``extra_args`` and cannot implement the frozen manifest
    # protocol. They remain a test/integration seam; production runners advertise v2 support.
    supports_master_certification = False

    def __init__(self, fn):
        self._fn = fn

    def run(self, workspace, data_paths, *, force: bool = False, errors_tier: str = "full",
            render: bool = True, timeout: float | None = None,
            certification_manifest: str | Path | None = None,
            certification_request: str | Path | None = None,
            analysis_allowance_seconds: float | None = None,
            finish_initial_wave: bool = False) -> RunResult:
        # ``render`` is accepted for protocol parity but deliberately NOT reflected into ``extra_args``:
        # bare callables (config["run_isar"] hooks / test fakes) don't parse argv, and the extra_args
        # contract was frozen at the Stage-1 shape (--errors <tier> [--force]). Adding --no-render here
        # would break every fake that asserts on extra_args. A real fake never renders anyway.
        extra_args = ["--errors", str(errors_tier)]
        if force:
            extra_args.append("--force")
        result = self._fn(workspace, data_paths, extra_args=extra_args)
        return _coerce_run_result(result)

    def close(self) -> None:
        """No-op — a bare callable owns no resources to release."""


def _coerce_run_result(result) -> RunResult:
    """Coerce a legacy runner's return value into a :class:`RunResult`."""
    if isinstance(result, RunResult):
        return result
    if result is None:
        return RunResult(0, "", "")
    if isinstance(result, int):
        return RunResult(result, "", "")
    # subprocess.CompletedProcess or any object exposing returncode/stdout/stderr.
    rc = getattr(result, "returncode", 0)
    return RunResult(rc if rc is not None else 0, getattr(result, "stdout", "") or "",
                     getattr(result, "stderr", "") or "")


def as_runner(obj):
    """Return a runner-protocol object (``.run`` + ``.close``) for ``obj``.

    - Already a runner (has ``.run`` and ``.close``) → returned as-is.
    - A bare callable (the ``config["run_isar"]`` hook / test fakes) → wrapped in
      :class:`_CallableRunner`, which invokes it exactly as the loop did before the seam existed.
    """
    if hasattr(obj, "run") and hasattr(obj, "close"):
        return obj
    if callable(obj):
        return _CallableRunner(obj)
    raise TypeError(f"as_runner: expected a runner or a callable, got {type(obj).__name__}")


def run_isar(workspace: str | Path, data_paths: Sequence[str | Path], *, force: bool = False,
             extra_args: Sequence[str] | None = None, timeout: float | None = None) -> RunResult:
    """Legacy ``isar run`` invoker — thin wrapper preserving the pre-seam signature for back-compat.

    New code should use :class:`SubprocessRunner`. This form (with free-form ``extra_args``) is kept for
    existing importers. Returns a :class:`RunResult`; raises :class:`IsarRunError` on a nonzero exit.
    """
    paths = [str(p) for p in data_paths]
    if not paths:
        raise ValueError("run_isar needs at least one data path")
    cmd = [sys.executable, "-m", "isar.cli.main", "run", *paths, "--workspace", str(workspace)]
    if force:
        cmd.append("--force")
    if extra_args:
        cmd.extend(str(a) for a in extra_args)
    return _exec_run(cmd, timeout)

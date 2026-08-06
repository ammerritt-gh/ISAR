"""Run ISAR as a subprocess (WORKING — LLM_HARNESS_DESIGN §2.3, §4).

``isar run`` is idempotent — it re-parses everything and re-fits only new / content-drifted /
previously-held scans — so the driver just re-invokes it after saving each new scan. We shell out via
``python -m isar.cli.main`` (robust: does not depend on the ``isar`` console script being on PATH),
capture stdout/stderr, and raise on a nonzero exit. Running ISAR in a child process IS the analysis
boundary: the driver never imports a fit internal.
"""
from __future__ import annotations

import inspect
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from isar.drive.session_log import AnalysisLimitExceeded


@dataclass
class RunResult:
    """Structured result of one ``isar run`` invocation (the runner-seam return type).

    ``timings`` carries the subprocess stage-timing dict when a runner has it inline (the persistent
    worker will); the SubprocessRunner leaves it ``None`` — the loop reads ``report/run_timings.json``
    off disk instead (see :func:`isar.drive.isar_io.readers.read_run_timings`)."""

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
            scan_errors_tier: str | None = None, master_errors_tier: str | None = None,
            force_masters: Sequence[str] | None = None,
            certification_manifest: str | Path | None = None,
            certification_request: str | Path | None = None,
            analysis_allowance_seconds: float | None = None,
            finish_initial_wave: bool = False) -> RunResult:
        """Run ``isar run <data...> --workspace <workspace> [--force] --errors <tier>``.

        Args:
            workspace: the ISAR workspace directory (must already be ``isar init``-ialized).
            data_paths: the scan files (or directories) to feed ISAR (typically the workspace data dir).
            force: pass ``--force`` to refit every scan, ignoring the ledger.
            force_masters: master ledger ids passed as repeated ``--force-master`` — refit exactly
                these masters even when their member set is unchanged (the predictive-escalation
                path), without the whole-batch cost of ``--force``.
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
        if scan_errors_tier is not None:
            cmd.extend(["--scan-errors", str(scan_errors_tier)])
        if master_errors_tier is not None:
            cmd.extend(["--master-errors", str(master_errors_tier)])
        for master_id in (force_masters or ()):
            cmd.extend(["--force-master", str(master_id)])
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
            scan_errors_tier: str | None = None, master_errors_tier: str | None = None,
            force_masters: Sequence[str] | None = None,
            certification_manifest: str | Path | None = None,
            certification_request: str | Path | None = None,
            analysis_allowance_seconds: float | None = None,
            finish_initial_wave: bool = False) -> RunResult:
        # ``render`` is accepted for protocol parity but deliberately NOT reflected into ``extra_args``:
        # bare callables (config["run_isar"] hooks / test fakes) don't parse argv, and the extra_args
        # contract was frozen at the Stage-1 shape (--errors <tier> [--force]). Adding --no-render here
        # would break every fake that asserts on extra_args. A real fake never renders anyway.
        extra_args = ["--errors", str(errors_tier)]
        if scan_errors_tier is not None:
            extra_args.extend(["--scan-errors", str(scan_errors_tier)])
        if master_errors_tier is not None:
            extra_args.extend(["--master-errors", str(master_errors_tier)])
        for master_id in (force_masters or ()):
            extra_args.extend(["--force-master", str(master_id)])
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


def runner_accepts(runner, name: str) -> bool:
    """Whether ``runner.run`` takes ``name`` (or absorbs it via ``**kwargs``).

    Runners are a configurable seam — a campaign may supply its own — so a keyword added to the
    protocol cannot be assumed present. An unreadable signature is treated as accepting, which
    keeps callable/proxy doubles working exactly as they did before the keyword existed.
    """
    try:
        parameters = inspect.signature(runner.run).parameters
    except (TypeError, ValueError):
        return True
    return name in parameters or any(item.kind == inspect.Parameter.VAR_KEYWORD
                                     for item in parameters.values())


def refresh_analysis(runner, workspace, data_dirs, *,
                     errors_tier, scan_errors_tier=None, master_errors_tier=None,
                     force: bool = False, force_masters=None,
                     certification_request=None,
                     analysis_allowance_seconds: float | None = None,
                     finish_initial_wave: bool | None = None,
                     enforce_completion: bool = False,
                     analysis_limit_seconds: float | None = None,
                     render: bool = False) -> RunResult:
    """Re-run ISAR over ``data_dirs`` through the runner seam and return its :class:`RunResult`.

    This owns every mechanical difference between runner shapes so the campaign loop does not:
    optional keywords the configured runner cannot take (``force_masters``,
    ``certification_request``, ``analysis_allowance_seconds``, ``finish_initial_wave``) are omitted
    rather than passed, since a config-supplied runner predating a keyword would raise ``TypeError``.

    ``analysis_allowance_seconds`` is the ALREADY-COMPUTED remaining allowance (the loop owns the
    analysis clock); ``None`` means the campaign declares no analysis limit, in which case
    ``finish_initial_wave`` is passed as ``None`` too and both keywords stay off the call — the
    pre-seam behaviour, where an unlimited campaign sent neither.

    With ``enforce_completion``, a soft runner checkpoint stop (``completed=False``) is turned into
    the campaign's explicit incomplete outcome, :class:`AnalysisLimitExceeded`.
    """
    kwargs = {"errors_tier": errors_tier, "force": force, "render": render}
    if scan_errors_tier is not None:
        kwargs["scan_errors_tier"] = scan_errors_tier
    if master_errors_tier is not None:
        kwargs["master_errors_tier"] = master_errors_tier
    if force_masters and runner_accepts(runner, "force_masters"):
        kwargs["force_masters"] = list(force_masters)
    if certification_request is not None and runner_accepts(runner, "certification_request"):
        kwargs["certification_request"] = certification_request
    if (analysis_allowance_seconds is not None
            and runner_accepts(runner, "analysis_allowance_seconds")):
        kwargs["analysis_allowance_seconds"] = analysis_allowance_seconds
    if finish_initial_wave is not None and runner_accepts(runner, "finish_initial_wave"):
        kwargs["finish_initial_wave"] = bool(finish_initial_wave)
    result = runner.run(workspace, list(data_dirs), **kwargs)
    if enforce_completion and not getattr(result, "completed", True):
        raise AnalysisLimitExceeded(
            "analysis limit exhausted; active fit wave checkpointed "
            f"(limit {float(analysis_limit_seconds or 0.0):.1f}s)")
    return result


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

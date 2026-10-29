"""The self-test of an installed ISAR copy: ``python -I -m isar.install_check``.

The installer runs it from the private environment; the routine tests run it from a source tree with
``--dev-root``. It prints one ``CHECK <name> PASS|FAIL <detail>`` line per check, then ``SELFTEST PASS``
or ``SELFTEST FAIL``. Exit 0 when every check passed, 1 otherwise, 2 for a refused command line. The GUI
runs as a child process, so this module imports no Qt and opens no window or dialog itself.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import subprocess
import sys
import traceback
import uuid
from pathlib import Path

RESULT_NAME = "gui-selftest-result.json"  # inside the work folder; a stale copy is removed before the start
PID_NAME = "gui-child.pid"  # the GUI child's process id, so a test can confirm a killed start left nothing behind
MODULES = ("isar", "isar_gui", "isar.synth.bl43_curves")


def _under(path, root) -> bool:
    """True when ``path`` resolves inside ``root``; case-insensitive, as Windows paths are."""
    if not path:  # realpath("") is the working folder, which must never count as the copy
        return False
    p = os.path.normcase(os.path.realpath(path)).lower()
    r = os.path.normcase(os.path.realpath(root)).lower().rstrip(os.sep)
    return p == r or p.startswith(r + os.sep)


def _tail(text: str, lines: int = 3) -> str:
    return " | ".join(text.strip().splitlines()[-lines:])


def _with_tail(detail: str, stderr: str) -> str:
    return f"{detail}; stderr: {_tail(stderr)}" if stderr.strip() else detail


def _imports(expect):
    bad = []
    for name in MODULES:
        path = getattr(importlib.import_module(name), "__file__", None) or ""
        if not _under(path, expect):
            bad.append(f"{name} loaded from {path or 'nowhere'}")
    return (not bad), ("; ".join(bad) if bad else f"{len(MODULES)} modules from {expect}")


def _curve(expect):
    import numpy as np

    from isar.analyze.components import CompositeModel, ConstantBackground, PhononDHO
    from isar.analyze.context import ScanContext
    from isar.synth import bl43_curves

    if not _under(bl43_curves.CURVES_DIR, expect):
        return False, f"resolution curves at {bl43_curves.CURVES_DIR}, outside {expect}"
    curve = bl43_curves.curve()
    x = np.linspace(-10.0, 20.0, 241)
    ctx = ScanContext(x=x, counts=np.zeros_like(x), mask=np.ones_like(x, dtype=bool), T=300.0,
                      center=0.0, gauss_fwhm=curve.fwhm, lorentz_fwhm=0.0, instrument="BL43LXU",
                      energy_status="needs_zero", resolution_curve=curve)
    # theta = (omega0, Gamma, h, bg.BG): one phonon mode convolved with the measured curve, plus background
    y = CompositeModel([PhononDHO(), ConstantBackground()]).evaluate(x, [6.0, 0.8, 1.0, 0.1], ctx)
    if not (np.all(np.isfinite(y)) and y.max() > 0.0):
        return False, "model values are not all finite and positive"
    return True, f"{curve.detector_id} curve, {y.size} points, peak {y.max():.3g}"


def _child_env(dev_root, work) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PYTHON")}
    env["PYTHONNOUSERSITE"] = "1"
    env["MPLCONFIGDIR"] = str(work / "mplconfig")  # else matplotlib creates .matplotlib under USERPROFILE
    if dev_root is not None:
        env["PYTHONPATH"] = str(dev_root)
    return env


def _cli(expect, dev_root, work):
    env = _child_env(dev_root, work)
    if dev_root is None:
        help_cmd = [str(Path(sys.prefix) / "Scripts" / "isar.exe"), "--help"]
    else:
        help_cmd = [sys.executable, "-m", "isar.cli.main", "--help"]
    run = subprocess.run(help_cmd, cwd=work, env=env, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=60)
    if run.returncode != 0 or "usage" not in run.stdout.lower():
        return False, _with_tail(f"isar --help exit code {run.returncode}", run.stderr)
    # The launcher's interpreter imports the CLI from the copy under test (provenance).
    prov = subprocess.run([sys.executable, "-c", "import isar.cli.main as m; print(m.__file__)"],
                          cwd=work, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=60)
    where = prov.stdout.strip()
    if prov.returncode != 0 or not _under(where, expect):
        return False, _with_tail(f"CLI imported from {where or 'nowhere'}, not {expect}", prov.stderr)
    return True, "isar --help exit 0; CLI from the copy under test"


def _kill_tree(proc) -> None:
    """End the GUI child and whatever it started, by process id, so a hung start leaves nothing behind."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=60)
    if proc.poll() is None:
        proc.kill()
    proc.wait()


def _gui(work, timeout, qt_platform, dev_root):
    profile = work / "profile"
    dirs = {"APPDATA": profile / "appdata", "LOCALAPPDATA": profile / "localappdata",
            "USERPROFILE": profile / "userprofile"}
    for folder in [*dirs.values(), work / "workspaces"]:
        folder.mkdir(parents=True, exist_ok=True)
    result = work / RESULT_NAME
    result.unlink(missing_ok=True)  # a stale result is removed, never read as success
    nonce = uuid.uuid4().hex
    env = {k: v for k, v in os.environ.items() if not k.startswith("QT_")}
    env.update({k: str(v) for k, v in dirs.items()})
    env.update(ISAR_EXPERIMENTS_ROOT=str(work / "workspaces"), QT_QPA_PLATFORM=qt_platform,
               MPLCONFIGDIR=str(work / "mplconfig"),
               ISAR_GUI_SELFTEST_RESULT=str(result), ISAR_GUI_SELFTEST_NONCE=nonce)
    if dev_root is None:
        cmd = [sys.executable, "-I", "-m", "isar_gui"]
    else:
        cmd = [sys.executable, "-m", "isar_gui"]
        env["PYTHONPATH"] = str(dev_root)
    with open(work / "gui.out.txt", "wb") as out, open(work / "gui.err.txt", "wb") as err:
        proc = subprocess.Popen(cmd, cwd=work, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err)
        (work / PID_NAME).write_text(str(proc.pid), encoding="ascii")
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            code = None
    stderr = (work / "gui.err.txt").read_text(encoding="utf-8", errors="replace")
    if code is None:
        return False, _with_tail(f"timed out after {timeout} s", stderr)
    if code != 0:
        return False, _with_tail(f"exit code {code}", stderr)
    if not result.exists():
        return False, _with_tail("no result written", stderr)
    try:
        data = json.loads(result.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return False, f"result unreadable: {exc}"
    if not isinstance(data, dict) or data.get("nonce") != nonce:
        return False, "nonce mismatch"
    if data.get("platform") != qt_platform:
        return False, f"platform {data.get('platform')}, expected {qt_platform}"
    return True, f"platform {qt_platform}, pid {data.get('pid')}"


def _run_check(name, fn) -> bool:
    try:
        ok, detail = fn()
    except Exception as exc:  # noqa: BLE001 — reported as this check's FAIL line; the traceback goes to stderr
        traceback.print_exc()
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    detail = " ".join(str(detail).split())[:400]
    print(f"CHECK {name} {'PASS' if ok else 'FAIL'} {detail}", flush=True)
    return ok


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -I -m isar.install_check",
                                     description="Self-test of an installed ISAR copy.")
    parser.add_argument("--expect-root", type=Path, required=True,
                        help="the folder ISAR must load from (the installed copy)")
    parser.add_argument("--work", type=Path, required=True, help="private working folder")
    parser.add_argument("--timeout", type=int, default=120, help="seconds allowed for the GUI start")
    parser.add_argument("--qt-platform", default="windows", help="Qt platform the GUI must start on")
    parser.add_argument("--dev-root", type=Path,
                        help="test only: run the children from this source tree, without -I")
    args = parser.parse_args(argv)
    if not args.work.is_dir():
        parser.error(f"--work {args.work} is not a folder")
    expect = args.expect_root.resolve()
    work = args.work.resolve()
    dev = args.dev_root.resolve() if args.dev_root else None
    checks = [
        ("imports", lambda: _imports(expect)),
        ("curve", lambda: _curve(expect)),
        ("cli", lambda: _cli(expect, dev, work)),
        ("gui", lambda: _gui(work, args.timeout, args.qt_platform, dev)),
    ]
    ok = all([_run_check(name, fn) for name, fn in checks])  # every check runs and names its own failure
    print("SELFTEST PASS" if ok else "SELFTEST FAIL", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

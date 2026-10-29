"""The installed-copy self-test (``isar/install_check.py``), run against this source tree.

Each case starts real GUI processes offscreen, so each takes a few seconds. The subprocess timeouts
are the wall-clock limit: a hung child fails the case instead of hanging the suite.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import isar

ROOT = Path(__file__).resolve().parents[2]
RESULT = "gui-selftest-result.json"


def _run(tmp_path, *extra, env=None, timeout=90):
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    base = {k: v for k, v in os.environ.items() if not k.startswith("ISAR_GUI_SELFTEST_")}
    cmd = [sys.executable, "-m", "isar.install_check", "--expect-root", str(ROOT),
           "--dev-root", str(ROOT), "--work", str(work), "--qt-platform", "offscreen", *extra]
    proc = subprocess.run(cmd, cwd=ROOT, env=dict(base, **(env or {})), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout)
    return proc, work


def _check_lines(stdout):
    return [line.split()[1:3] for line in stdout.splitlines() if line.startswith("CHECK ")]


def _alive(pid):
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True).stdout
        return f'"{pid}"' in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_passing_run_reports_four_passes_and_exit_zero(tmp_path):
    # The import-root rule: this run must test this worktree's ISAR, not another checkout's.
    assert Path(isar.__file__).resolve().parents[1] == ROOT
    proc, _ = _run(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _check_lines(proc.stdout) == [["imports", "PASS"], ["curve", "PASS"],
                                         ["cli", "PASS"], ["gui", "PASS"]]
    assert proc.stdout.splitlines()[-1] == "SELFTEST PASS"


def test_forced_gui_failure_fails_the_gui_check(tmp_path):
    proc, _ = _run(tmp_path, env={"ISAR_GUI_SELFTEST_FORCE": "fail"})
    assert proc.returncode == 1
    assert "CHECK gui FAIL" in proc.stdout
    assert proc.stdout.splitlines()[-1] == "SELFTEST FAIL"


def test_forced_hang_is_killed_at_the_timeout(tmp_path):
    started = time.monotonic()
    proc, work = _run(tmp_path, "--timeout", "5", env={"ISAR_GUI_SELFTEST_FORCE": "hang"})
    assert time.monotonic() - started < 20
    assert proc.returncode == 1
    assert "CHECK gui FAIL timed out after 5 s" in proc.stdout
    assert not _alive(int((work / "gui-child.pid").read_text(encoding="ascii")))


def test_stale_result_file_is_removed_not_read(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / RESULT).write_text(json.dumps({"nonce": "stale", "platform": "offscreen", "pid": 1}), encoding="utf-8")
    proc, _ = _run(tmp_path, env={"ISAR_GUI_SELFTEST_FORCE": "fail"})
    assert ["gui", "PASS"] not in _check_lines(proc.stdout)
    assert not (work / RESULT).exists()


def test_cli_check_leaves_the_user_profile_alone(tmp_path, monkeypatch):
    """isar --help imports matplotlib, which creates a .matplotlib folder under USERPROFILE."""
    from isar.install_check import _cli
    profile, work = tmp_path / "profile", tmp_path / "work"
    profile.mkdir()
    work.mkdir()
    monkeypatch.setenv("USERPROFILE", str(profile))
    ok, detail = _cli(ROOT, ROOT, work)
    assert ok, detail
    assert not any(profile.iterdir()), [p.name for p in profile.iterdir()]


def test_empty_path_is_never_under_the_copy():
    # realpath("") is the working folder; a missing __file__ must not pass as the copy under test.
    from isar.install_check import _under
    assert not _under("", ROOT)


def test_expect_root_elsewhere_fails_imports(tmp_path):
    proc, _ = _run(tmp_path, "--expect-root", str(tmp_path))
    assert proc.returncode == 1
    assert "CHECK imports FAIL" in proc.stdout

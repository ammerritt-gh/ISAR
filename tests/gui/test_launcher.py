"""The no-console launch: ``isar_gui.__main__.run`` (failures logged and shown) and ``run-isar-gui.bat``.

No test opens a visible window: the GUI runs offscreen and the batch tests use no-console starts.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from isar_gui import __main__ as launcher
from isar_gui import logging_setup

REPO = Path(__file__).resolve().parents[2]
BAT = REPO / "run-isar-gui.bat"
WINDOWS_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="the launcher is a Windows batch file")


def _env_pythonw(name: str) -> Path:
    root = os.environ.get("MAMBA_ROOT_PREFIX") or Path(os.environ["USERPROFILE"]) / "AppData" / "Roaming" / "mamba"
    return Path(root) / "envs" / name / "pythonw.exe"


@pytest.fixture
def fresh_log():
    """Detach a GUI log handler an earlier test attached, so this test's log path is its own."""
    root = logging.getLogger()

    def _marked():
        return [h for h in root.handlers if getattr(h, logging_setup._HANDLER_MARKER, False)]

    for handler in _marked():
        root.removeHandler(handler)
        handler.close()
    yield
    for handler in _marked():
        root.removeHandler(handler)
        handler.close()


def test_windowless_bad_workspace_shows_message(tmp_path, fresh_log):
    shown: list[str] = []
    missing = tmp_path / "no-such"
    assert launcher.run([str(missing)], windowless=True, show_error=shown.append) == 2
    assert len(shown) == 1
    assert str(missing) in shown[0]
    assert str(tmp_path / "appdata" / "isar_gui" / "isar-gui.log") in shown[0]


def test_windowless_import_failure_is_logged_and_shown(tmp_path, monkeypatch, fresh_log):
    monkeypatch.setitem(sys.modules, "isar_gui.app", None)  # the app import now raises ImportError
    shown: list[str] = []
    assert launcher.run([], windowless=True, show_error=shown.append) == 1
    assert len(shown) == 1 and "isar_gui.app" in shown[0]
    log_text = (tmp_path / "appdata" / "isar_gui" / "isar-gui.log").read_text(encoding="utf-8")
    assert "Traceback" in log_text and "isar_gui.app" in log_text


@WINDOWS_ONLY
def test_bat_reports_missing_environment():
    env = dict(os.environ, ISAR_ENV="no-such-env")
    result = subprocess.run(["cmd", "/c", str(BAT)], cwd=REPO, env=env,
                            stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
                            encoding="utf-8", errors="replace")
    assert result.returncode == 1
    assert "[ERROR]" in result.stdout and "no-such-env" in result.stdout


@WINDOWS_ONLY
@pytest.mark.slow
def test_bat_starts_the_gui_windowless(tmp_path):
    if not any(_env_pythonw(name).exists() for name in ("isar", "tavi-dev")):
        pytest.skip("no ISAR or tavi-dev environment with pythonw.exe on this machine")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", ISAR_GUI_SMOKE_MS="500", APPDATA=str(tmp_path))
    result = subprocess.run(["cmd", "/c", str(BAT)], cwd=REPO, env=env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=5)
    assert result.returncode == 0
    log = tmp_path / "isar_gui" / "isar-gui.log"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not (log.exists() and "main window shown" in log.read_text(encoding="utf-8")):
        time.sleep(0.2)
    assert "main window shown" in log.read_text(encoding="utf-8")


@WINDOWS_ONLY
def test_pythonw_child_output_reaches_pipes(qtbot):
    pytest.importorskip("PySide6")
    from isar_gui.process import ProcessRunner

    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.exists():
        pytest.skip("no pythonw.exe beside the test interpreter")
    code = "import sys; print(42); sys.exit(3)"
    direct = subprocess.run([str(pythonw), "-c", code], capture_output=True, text=True, timeout=30)
    assert direct.stdout.strip() == "42" and direct.returncode == 3

    runner = ProcessRunner()
    lines: list[str] = []
    runner.line.connect(lines.append)
    with qtbot.waitSignal(runner.finished, timeout=15000) as blocker:
        runner.start([str(pythonw), "-c", code])
    assert blocker.args == [3]
    assert "42" in lines

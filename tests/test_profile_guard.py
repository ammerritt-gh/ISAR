"""The whole test session is walled off from the operator's real profile (root ``conftest.py``).

A per-test ``monkeypatch`` of ``APPDATA`` is undone before pytest-qt closes a left-over window, and
that close once rewrote the operator's real ``session.json``. This test runs outside ``tests/gui``,
so only the session-wide redirect applies to it.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


def _real_dirs():
    """The operator's real per-user GUI folders, from the environment the root conftest captured."""
    roaming = os.environ.get("ISAR_TEST_REAL_APPDATA", os.environ.get("APPDATA", ""))
    local = os.environ.get("ISAR_TEST_REAL_LOCALAPPDATA", os.environ.get("LOCALAPPDATA", ""))
    home = Path.home()
    return [Path(roaming) / "isar_gui" if roaming else home / ".isar_gui",
            Path(local) / "isar_gui" if local else home / ".isar_gui",
            home / "isar_gui"]


def _profile() -> Path:
    assert "ISAR_TEST_PROFILE_ROOT" in os.environ, "the root conftest did not redirect the profile"
    return Path(os.environ["ISAR_TEST_PROFILE_ROOT"]).resolve()


def test_gui_paths_resolve_inside_the_session_profile():
    pytest.importorskip("PySide6")
    from isar_gui.logging_setup import gui_log_dir
    from isar_gui.session import session_path
    from isar_gui.workspace_manager import experiments_root, temp_root

    profile = _profile()
    paths = {"session": session_path(), "log": gui_log_dir(), "temp workspaces": temp_root(),
             "experiments": experiments_root()}
    for what, path in paths.items():
        assert path.resolve().is_relative_to(profile), f"{what} path {path} is outside {profile}"
        for real in _real_dirs():
            assert not path.resolve().is_relative_to(real.resolve()), \
                f"{what} path {path} is inside the operator's real profile {real}"


def test_session_profile_is_not_the_real_one():
    profile = _profile()
    for real in _real_dirs():
        assert not profile.is_relative_to(real.resolve())
        assert not real.resolve().is_relative_to(profile)

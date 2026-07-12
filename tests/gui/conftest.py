"""Shared setup for the isar_gui tests.

Forces the Qt offscreen platform BEFORE any Qt import so the suite runs headless and xdist-safe, and
exposes the shared synthetic-workspace factory (from tests/synthetic_ws) as a ``make_ws`` fixture so
the GUI tests build workspaces the same way the facade tests do — no copy.
"""
from __future__ import annotations

import os

# Must precede any PySide6 import (test modules importorskip PySide6 at module scope).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from synthetic_ws import stamp_ws


@pytest.fixture
def make_ws(tmp_path):
    """Return a factory that stamps a synthetic workspace under a fresh subdir and returns its path."""
    counter = {"n": 0}

    def _make(**parts):
        counter["n"] += 1
        root = tmp_path / f"ws{counter['n']}"
        root.mkdir()
        return stamp_ws(root, **parts)

    return _make

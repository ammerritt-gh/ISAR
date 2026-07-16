"""Structural tests for the single inspected-unit identity.

The controller is deliberately tiny: these tests protect the important semantic boundary that a
multi-selection is not an inspection request, and a refresh only preserves an identity that still
exists in the current ledger.
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from isar_gui.active_unit import ActiveUnitController


def test_active_unit_emits_only_real_identity_changes(qtbot):
    controller = ActiveUnitController()
    seen = []
    controller.active_changed.connect(seen.append)
    controller.set_active("exp:7:det-a")
    controller.set_active("exp:7:det-a")
    controller.set_active(None)
    assert seen == ["exp:7:det-a", None]


def test_active_unit_survives_refresh_only_when_still_present(qtbot):
    controller = ActiveUnitController()
    controller.set_active("exp:7:det-a")
    controller.retain({"exp:7:det-a", "exp:8:det-a"})
    assert controller.key == "exp:7:det-a"
    controller.retain({"exp:8:det-a"})
    assert controller.key is None

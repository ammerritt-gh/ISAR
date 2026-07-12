"""Synthetic-workspace fixture for the workspace_api facade tests.

The builder functions now live in the shared ``tests/synthetic_ws`` module (imported by the GUI
tests too); this conftest re-exports them for the ``from .conftest import ...`` call sites and wires
the ``make_ws`` pytest fixture on top.
"""
from __future__ import annotations

import pytest

from synthetic_ws import (  # noqa: F401 — re-exported for `from .conftest import ...` call sites
    FULL_COLUMNS,
    OLD_COLUMNS,
    _POLICY_TOML,
    detector_outcome,
    scan_entry,
    stamp_ws,
    write_campaign,
    write_data_file,
    write_policy,
    write_results,
    write_session,
    write_session_log,
    write_state,
)


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

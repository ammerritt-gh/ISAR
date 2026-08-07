"""Compatibility shim: the grid+NNLS initial-condition machinery moved to
:mod:`isar.analyze.phonon` in slice P2.7 (it is DHO-specific). Kept so existing callers
keep importing ``isar.analyze.ic``; removed in P2.8.
"""
from __future__ import annotations

from isar.analyze.phonon import (  # noqa: F401
    EPS_OMEGA_D,
    GAMMA0,
    PRIOR_HALF_WIDTH,
    STEP,
    greedy_multimode_ic,
    omega_d_grid,
    phonon_structure_ic as structure_ic,
)

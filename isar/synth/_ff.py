"""Path bootstrap + re-export of the production lineshape library.

`Fitting_Functions.py` lives at the repository root and is imported **unchanged**
(the code under test). This shim is the single place that owns the path logic so
every other module can simply do ``from ._ff import convolute_voigt_dho`` and have
it resolve whether the package is run via ``python -m isar.synth.score`` from the
repo root, under pytest, or from an editable install.
"""
from __future__ import annotations

import sys
from pathlib import Path

# repo root = .../ISAR  (this file is .../ISAR/isar/synth/_ff.py)
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from Fitting_Functions import (  # noqa: E402  (import after sys.path tweak)
    KB,
    Voigt,
    Stokes_DHO,
    antiStokes_DHO,
    convolute_voigt_dho,
    voigt_area_for_height,
)

__all__ = [
    "KB",
    "Voigt",
    "Stokes_DHO",
    "antiStokes_DHO",
    "convolute_voigt_dho",
    "voigt_area_for_height",
]

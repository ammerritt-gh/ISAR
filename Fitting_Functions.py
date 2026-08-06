"""Temporary compatibility shim — removed at Pass-2 closure.

The lineshape library now lives at :mod:`isar.analyze.lineshapes`. Import from there;
this root-level module exists only so external notebooks and scripts that still do
``from Fitting_Functions import ...`` keep working during the transition.
"""
from isar.analyze.lineshapes import *  # noqa: F401,F403
from isar.analyze.lineshapes import KB, kB  # noqa: F401  (not caught by `*` conventions)

__all__ = [
    "KB",
    "kB",
    "Gaussian",
    "Lorentzian",
    "Voigt",
    "Voigt_wBG",
    "pseudo_voigt",
    "Stokes_DHO",
    "antiStokes_DHO",
    "convolute_voigt_dho",
    "voigt_area_for_height",
]

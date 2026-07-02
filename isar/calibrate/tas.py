"""TAS analytical resolution — the public `isar.calibrate` home for the Cooper-Nathans functions.

The validated implementation lives in `isar.synth.resolution` (built and tested in Phase 1/3).
This is a thin re-export so the session/calibration layer can import it from `isar.calibrate`
without moving validated code (UNIFICATION_DESIGN §3).
"""
from isar.synth.resolution import (  # noqa: F401
    FALLBACK_FWHM,
    cn_energy_fwhm,
    cn_resolution_matrix,
)

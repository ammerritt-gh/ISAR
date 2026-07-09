"""Phase 3 analysis layer — the structure-hypothesis loop over the 1-D Voigt fitter.

Models are built out of composable :mod:`~isar.analyze.components` (PUFFIN-style): a
``CompositeModel`` sums ``Component`` parts, so a structure hypothesis that omits a
component simply drops its parameters. See ``docs/archive/PHASE_3_DESIGN.md`` §14 step 1.
"""

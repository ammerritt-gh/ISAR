"""The deterministic acquisition engine (LLM_HARNESS_DESIGN §2.2) — the statistical heart, no LLM.

Exactly one thing chooses a scan: :mod:`eig_planner`, whose ``plan_next`` ranks every (Q, energy
window, exposure) candidate by expected information per neutron over the hypothesis map
(:mod:`classify_eig`) and the intensity map.  :mod:`acquire` holds the request value type the
instrument client submits and the resolution a window is sized against; :mod:`scan_grid` (with
:mod:`grid_adapters`) owns the one uniform-grid arithmetic a design and its submitted command share;
:mod:`inventory` holds the action-identity and evaluation records the discovery lane enumerates
into; :mod:`calibration` applies the σ-calibration so a convergence test consumes a calibrated
interval half-width, never ISAR's floored prior sigma.
"""

"""The deterministic acquisition engine (LLM_HARNESS_DESIGN §2.2) — the statistical heart, no LLM.

It holds a model of ω(q) over ISAR's accumulated per-scan results (:mod:`model`) and an acquisition
function that chooses the next SCAN (:mod:`acquire`) — a Q on the objective path, an energy window
(center = predicted ω, half-width = predicted uncertainty + margin), and a neutron count. The
acquisition unit is a SCAN, not a point.

:mod:`calibration` is WORKING this round: it applies the σ-calibration (milestone 0, §5) so the
convergence test consumes a calibrated interval half-width, never ISAR's floored prior sigma. The
model and the acquisition function are stubs for the next round.
"""

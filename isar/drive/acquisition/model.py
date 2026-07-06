"""The ω(q) model over ISAR's accumulated results (STUB — LLM_HARNESS_DESIGN §2.2, §9.2).

Contract: ``fit(points) -> model`` then ``model.predict(q) -> (mean, sigma)`` — a predictive model of
ω₀ along the objective path with an uncertainty the acquisition function can act on. Candidate forms
(open question §9.2): a local linear fit (cheap, mirrors ISAR's own prior builder) or a Gaussian
process (smooth, principled uncertainty, a kernel to choose). Lean: start local, move to GP if path
curvature demands it.

The input ``points`` are ISAR's delivered results — read from ``card.state.json``'s
``dispersion_prior`` and ``results.csv`` via :mod:`isar.drive.isar_io.readers`. The model never
produces a number for the record; it only guides WHERE to measure next. Not implemented this round.
"""
from __future__ import annotations


def fit(points):
    """Fit a predictive ω(q) model to ISAR's delivered results (NOT YET IMPLEMENTED).

    Returns an object with ``predict(q) -> (mean, sigma)``. See LLM_HARNESS_DESIGN §2.2, §9.2.
    """
    raise NotImplementedError(
        "acquisition ω(q) model not yet implemented (next round); see LLM_HARNESS_DESIGN §2.2."
    )

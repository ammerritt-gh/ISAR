"""Computed confidence (§9) — the number that replaces the old high/medium/low self-rating.

Built from quantities steps 2–3 already produce, each mapped to a [0,1] sub-score and combined
by a geometric mean so any single weak factor pulls the trust down (and the sub-scores are
returned for audit, never a black box):

- ``s_structure``  — ω agreement across surviving structures (fold → high, fork → low).
- ``s_interval``   — bootstrap ω₀ interval tightness vs a resolution-scaled reference.
- ``s_paradigm``   — the MINOS↔bootstrap regime (asymptotic → 1, sparse → low).
- ``s_prior``      — distance from the running dispersion prior; **neutral when no prior** (per
                     the §0.4 guard, absence of a prior is not evidence the fit is wrong).
- ``s_provenance`` — P6: the energy-calibration status (provisional inputs → lower confidence).

The spec names the inputs but not the combination rule; the geometric mean + documented
sub-score maps are this layer's choice. Sparse-prior / early-run / resolution-version P6 factors
are future additions to ``s_provenance``.

**Phonon-less winners (SCENE-1 PR-a).** When selection prefers ``elastic+bg`` the scan's claim is
``elastic_only`` — scored by :func:`compute_null_confidence`, never by :func:`compute_confidence`
(there is no ω₀ for the phonon sub-scores to read). The score is capped at
:data:`S_SCOPE_ELASTIC` by construction: elastic intensity is ubiquitous (incoherent +
can/sample background, strongest near Bragg) and can mask arbitrary signals, so "nothing beyond
the elastic model was resolvable *in this scan*" is a categorically humble, scan-local claim —
never region exclusion (owner ruling 2026-08-08; mapping/excluding elastic is future scene-level
work).
"""
from __future__ import annotations

import numpy as np

SIGMA_REF = 0.1  # meV — interval-tightness reference (a typical target ω₀ σ)
# The categorical-humility cap on any elastic_only claim: applied as a product factor (hard
# ceiling) and exposed as its own named component in the audit dict.
S_SCOPE_ELASTIC = 0.5
CONFIDENCE_SCORER_REVISION = "isar.confidence/1"
_REGIME_S = {"asymptotic": 1.0, "unknown": 0.6, "sparse": 0.3}
_PROVENANCE_S = {"absolute": 1.0, "needs_zero": 0.85, "needs_conversion_and_zero": 0.5}


def _s_structure(selection) -> float:
    modality = getattr(selection, "modality", None)
    if modality == "fold":
        return 1.0
    if modality == "fork":
        return 0.2
    return 0.6


def _s_interval(result, *, sigma_ref: float = SIGMA_REF) -> float:
    wm = result.within_model or {}
    fi = wm.get("freq_interval")
    hw = None
    if fi is not None and np.isfinite(fi[0]) and np.isfinite(fi[2]):
        hw = (float(fi[2]) - float(fi[0])) / 2.0
    elif np.isfinite(result.omega0_err_hesse):
        hw = float(result.omega0_err_hesse)
    if hw is None or hw < 0.0:
        return 0.5
    return float(sigma_ref / (sigma_ref + hw))


def _s_paradigm(result) -> float:
    ag = (result.within_model or {}).get("agreement")
    if ag is None:
        return 0.6
    return _REGIME_S.get(getattr(ag, "regime", None), 0.6)


def _s_prior(result, prior) -> float:
    if prior is None or not np.isfinite(result.omega0):
        return 1.0                                       # neutral: absence is not 'wrong'
    sigma = prior.omega0_sigma if (prior.omega0_sigma and prior.omega0_sigma > 0) else 1.0
    z = (float(result.omega0) - float(prior.omega0)) / sigma
    return float(np.exp(-0.5 * z * z))


def _s_provenance(ctx) -> float:
    return _PROVENANCE_S.get(ctx.energy_status, 0.7)


def _s_detectability(detection) -> float:
    """Could this scan have seen the prior-expected mode above the elastic shoulder?

    ``detection`` is the :func:`isar.analyze.verdict.detection_power` payload, or ``None`` when
    no prior places a mode in the window (nothing to have missed → neutral 1). An abstaining
    power test (no expected strength supplied) scores a mildly humble 0.7 — never confident.
    Otherwise the expected significance maps monotonically to [0,1) with 0.5 at the detection
    floor: ``sig / (sig + floor)``.
    """
    if detection is None:
        return 1.0
    sig = detection.get("expected_significance")
    floor = float(detection.get("floor", 3.0))
    if detection.get("detectable") is None or sig is None or not np.isfinite(sig):
        return 0.7
    sig = max(float(sig), 0.0)
    return float(sig / (sig + floor)) if (sig + floor) > 0 else 0.0


def compute_confidence(result, ctx, selection, *, prior=None) -> tuple[float, dict]:
    """Return ``(confidence in [0,1], components)`` — the geometric mean of the exposed
    sub-scores. See the module docstring for each factor."""
    comp = {
        "s_structure": _s_structure(selection),
        "s_interval": _s_interval(result),
        "s_paradigm": _s_paradigm(result),
        "s_prior": _s_prior(result, prior),
        "s_provenance": _s_provenance(ctx),
    }
    vals = np.clip(np.array(list(comp.values()), dtype=float), 1e-6, 1.0)
    score = float(np.exp(np.mean(np.log(vals))))         # geometric mean
    return score, comp


def compute_null_confidence(ctx, selection, *, detection=None) -> tuple[float, dict]:
    """Confidence for the ``elastic_only`` claim (a phonon-less ``elastic+bg`` winner).

    ``S_SCOPE_ELASTIC × geomean(s_structure, s_provenance, s_detectability)`` — the cap is a
    product factor (hard ceiling ≤ 0.5) and an explicit named component in the audit dict.
    None of the phonon sub-scores (interval, paradigm, prior distance) apply: there is no ω₀.
    ``detection`` is the verdict layer's :func:`~isar.analyze.verdict.detection_power` payload
    (or ``None`` when no prior expects a mode in the window).
    """
    comp = {
        "s_scope_elastic": S_SCOPE_ELASTIC,
        "s_structure": _s_structure(selection),
        "s_provenance": _s_provenance(ctx),
        "s_detectability": _s_detectability(detection),
    }
    inner = np.clip(np.array([comp["s_structure"], comp["s_provenance"],
                              comp["s_detectability"]], dtype=float), 1e-6, 1.0)
    score = float(S_SCOPE_ELASTIC * np.exp(np.mean(np.log(inner))))
    return score, comp


def probability_semantics() -> dict:
    """Producer-owned declaration for the persisted ``confidence`` score."""
    from isar.schema.statistics import authority_scopes

    return {
        "quantity_kind": "heuristic_score",
        "source_measure_semantics": "heuristic_triage_score",
        "scorer_revision": CONFIDENCE_SCORER_REVISION,
        "calibration_reference": None,
        "conditioning": "fitted_candidate_and_available_error_diagnostics",
        "inference_paradigm": "normalized_heuristic",
        "authority_scopes": authority_scopes(user_diagnostic="active"),
    }

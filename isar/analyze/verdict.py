"""Verdict buckets + detection power (§6, §0.4) — classify every scan into a named outcome,
never delete (D7).

The buckets: **resolved** (success), **held-for-calibration** (energy axis uncalibrated),
**unconstrained** (nothing converges; fork with no prior to break the tie; or — frequentist
proxy for the §6 "both paradigms wide+disagreeing" — a sparse regime with a pathologically
wide interval), **underpowered** (the prior expects a phonon the scan lacked the counts to
detect — *provisional*, the §0.4 circularity guard), **no-signal** (no excursion above
background and the prior expects none), **junk** (catastrophic GoF with a structureless
residual). The load-bearing **model-incomplete** route keeps a *structured* residual (the broad
quasi-elastic tail, cost/dof >> 1 yet the phonon is real) out of the junk bucket.

The detection-power test is the single-scan analog of ``synth.score.data_threshold``'s
reliability-floor idea (it does not run the MC harness): predict the prior phonon's expected
peak counts, ask whether the achieved statistics give it enough significance to be seen.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# Tunable thresholds (documented defaults; not from the spec, which leaves them to impl).
GOF_BAD = 5.0          # cost/dof above this -> junk (structureless) or model-incomplete (structured)
EXCURSION_K = 5.0      # signal if max-median > k*sqrt(median) (matches reduce.qc_scans._is_flat)
RUNS_Z = 2.0           # runs-test z <= -RUNS_Z -> structured residual
DETECT_FLOOR = 3.0     # expected significance (sigma) for the prior phonon to be 'detectable'
WIDE_REL = 0.5         # freq half-width / omega0 above this, with sparse regime -> unconstrained


def _excursion_above_bg(ctx, *, k: float = EXCURSION_K) -> bool:
    """Is there a real excursion above background? Poisson floor from the QC layer:
    ``max - median > k*sqrt(max(median, 1))``."""
    y = ctx.counts_used
    if y.size == 0:
        return False
    med = float(np.median(y))
    return bool(float(np.max(y) - med) > k * np.sqrt(max(med, 1.0)))


def _residual_structured(pull, *, z: float = RUNS_Z) -> bool:
    """Wald-Wolfowitz runs test on the pull signs: significantly *fewer* runs than expected
    under randomness ⇒ a coherent (structured) residual, not noise. This is the clause that
    routes a high-GoF quasi-elastic tail to model-incomplete rather than junk."""
    s = np.sign(np.asarray(pull, dtype=float))
    s = s[s != 0]
    n = s.size
    if n < 8:
        return False
    n_pos = int(np.sum(s > 0))
    n_neg = n - n_pos
    if n_pos == 0 or n_neg == 0:
        return True                                    # all one sign = maximally structured
    runs = 1 + int(np.sum(s[1:] != s[:-1]))
    mu = 2.0 * n_pos * n_neg / n + 1.0
    var = 2.0 * n_pos * n_neg * (2.0 * n_pos * n_neg - n) / (n * n * (n - 1.0))
    if var <= 0.0:
        return False
    return bool((runs - mu) / np.sqrt(var) <= -z)


def _pull(result, ctx):
    """Pull residual (counts - model)/sqrt(counts) for the winning structure, rebuilt from its
    label + fitted params. Used only on the GoF-bad branch (junk vs model-incomplete)."""
    from isar.analyze.structures import enumerate_structures

    hyp = {h.label: h for h in enumerate_structures(ctx)}.get(result.label)
    if hyp is None:
        return None
    comp = hyp.build()
    theta = np.array([result.params[n] for n in comp.param_names()], dtype=float)
    y = ctx.counts_used
    model = comp.evaluate(ctx.x_used, theta, ctx)
    return (y - model) / np.sqrt(np.maximum(y, 1.0))


def _freq_proxy_unconstrained(result) -> bool:
    """Frequentist proxy for §6's 'both paradigms wide and disagreeing' (the Bayesian half is
    step 7): a sparse regime AND a pathologically wide ω₀ interval."""
    wm = result.within_model or {}
    ag, fi = wm.get("agreement"), wm.get("freq_interval")
    if ag is None or fi is None or getattr(ag, "regime", None) != "sparse":
        return False
    hw = (float(fi[2]) - float(fi[0])) / 2.0
    return bool(result.omega0 > 0 and hw / result.omega0 > WIDE_REL)


def _prior_expects_phonon(prior, ctx) -> bool:
    """The prior places a phonon inside this scan's window (so an absence is meaningful)."""
    if prior is None or not np.isfinite(prior.omega0):
        return False
    return bool(0.0 < prior.omega0 <= ctx.window[1] + ctx.gauss_fwhm)


def detection_power(ctx, prior, result, *, floor: float = DETECT_FLOOR) -> dict:
    """Single-scan analog of ``synth.score.data_threshold``'s reliability floor: would the prior's
    expected phonon (peak strength ``prior.amplitude`` counts on the fitted background) clear the
    detection floor at this scan's statistics? Expected significance ``A/sqrt(A+B)`` vs ``floor``.

    ``detectable`` is ``None`` (abstain) when no expected strength is supplied — the underpowered
    verdict then defaults on (conservative: never confirm empty without power evidence)."""
    bg = (result.params or {}).get("bg.BG") if result is not None else None
    if bg is None:
        y = ctx.counts_used
        bg = float(np.median(y)) if y.size else 0.0
    bg = max(float(bg), 0.0)
    amp = None if prior is None else prior.amplitude
    if amp is None:
        return {"detectable": None, "expected_significance": float("nan"),
                "expected_amplitude": None, "background": bg, "floor": float(floor)}
    amp = float(amp)
    sig = amp / np.sqrt(amp + bg) if (amp + bg) > 0 else 0.0
    return {"detectable": bool(sig >= floor), "expected_significance": float(sig),
            "expected_amplitude": amp, "background": bg, "floor": float(floor)}


def _prior_breaks_fork(pooled, prior):
    """If exactly one fork mode is consistent with the prior (within 2·omega0_sigma), return its
    location — the prior breaks the tie. Otherwise ``None`` (no prior ⇒ stays a fork)."""
    if prior is None or pooled is None or not pooled.modes:
        return None
    tol = 2.0 * (prior.omega0_sigma if prior.omega0_sigma and prior.omega0_sigma > 0 else 1.0)
    near = [m for m in pooled.modes if abs(m.location - prior.omega0) <= tol]
    return near[0].location if len(near) == 1 else None


@dataclass
class ScanVerdict:
    """The per-scan triage outcome Phase 4 acts on: the bucket, a [0,1] confidence (0 when
    there is no usable ω), the exposed confidence sub-scores, human-readable reasons, the
    detection-power payload (when computed), and ``provisional`` (the underpowered guard)."""

    verdict: str
    confidence: float
    components: dict = field(default_factory=dict)
    reasons: list = field(default_factory=list)
    detection: dict | None = None
    provisional: bool = False


def classify(results, ctx, selection, *, prior=None) -> ScanVerdict:
    """Classify one scan into a named bucket (§6) with a computed confidence (§9). Deterministic
    precedence (held → unconstrained → junk/model-incomplete → no-signal/underpowered → resolved);
    never deletes (D7). When the data is flat, the prior + detection-power test split no-signal from
    (provisional) underpowered."""
    from isar.analyze import confidence as _confidence

    def conf(win):
        return _confidence.compute_confidence(win, ctx, selection, prior=prior)

    # 1. held-for-calibration — energy axis not usable (no fitted zero / needs conversion).
    if ctx.energy_status == "needs_conversion_and_zero":
        return ScanVerdict("held_for_calibration", 0.0,
                           reasons=["energy axis uncalibrated (needs_conversion_and_zero)"])

    # 2. nothing converged.
    if selection.best is None:
        return ScanVerdict("unconstrained", 0.0, reasons=["no admissible structure converged"])
    win = {r.label: r for r in results}[selection.best]
    reasons = []

    # 3. across-model fork — unconstrained unless the prior breaks the tie.
    if selection.modality == "fork":
        mode = _prior_breaks_fork(selection.pooled, prior)
        if mode is None:
            return ScanVerdict("unconstrained", 0.0,
                               reasons=["across-model fork with no prior to break the tie"])
        reasons.append(f"fork broken by prior -> mode at {mode:.2f} meV")

    # 4. frequentist proxy for 'both paradigms wide and disagreeing'.
    if _freq_proxy_unconstrained(win):
        return ScanVerdict("unconstrained", 0.0,
                           reasons=["sparse regime with a pathologically wide interval"])

    # 5. catastrophic GoF -> junk (structureless residual) or model-incomplete (structured).
    if np.isfinite(win.cost_per_dof) and win.cost_per_dof >= GOF_BAD:
        pull = _pull(win, ctx)
        structured = pull is not None and _residual_structured(pull)
        if structured:
            score, comp = conf(win)
            return ScanVerdict("model_incomplete", score, components=comp,
                               reasons=reasons + [f"cost/dof={win.cost_per_dof:.1f}, structured "
                                                  "residual (missing component, not junk)"])
        return ScanVerdict("junk", 0.0,
                           reasons=reasons + [f"cost/dof={win.cost_per_dof:.1f}, structureless residual"])

    # 6. no excursion above background -> no-signal / underpowered (the detection-power split).
    if not _excursion_above_bg(ctx):
        if _prior_expects_phonon(prior, ctx):
            dp = detection_power(ctx, prior, win)
            if dp["detectable"] is True:                 # had the power, saw nothing: genuine absence
                return ScanVerdict("no_signal", 0.0, detection=dp,
                                   reasons=reasons + ["no excursion; prior expected a phonon the "
                                                      "scan had power to see (prior may be wrong)"])
            # couldn't (or can't confirm) the power -> underpowered, provisional (the §0.4 guard:
            # prior-conditional, never marks the region confirmed-empty).
            return ScanVerdict("underpowered", 0.0, detection=dp, provisional=True,
                               reasons=reasons + ["prior expects a phonon below the detection "
                                                  "threshold at the achieved statistics"])
        return ScanVerdict("no_signal", 0.0, reasons=reasons + ["no excursion above background"])

    # 7. resolved.
    score, comp = conf(win)
    return ScanVerdict("resolved", score, components=comp, reasons=reasons or ["resolved"])

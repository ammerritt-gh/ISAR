"""Verdict buckets + detection power (§6, §0.4) — classify every scan into a named outcome,
never delete (D7).

The buckets: **resolved** (success), **held-for-calibration** (energy axis uncalibrated),
**unconstrained** (nothing converges; fork with no prior to break the tie; or — frequentist
proxy for the §6 "both paradigms wide+disagreeing" — a sparse regime with a pathologically
wide interval), **underpowered** (the prior expects a phonon the scan lacked the counts to
detect — *provisional*, the §0.4 circularity guard), **no-signal** (no excursion above
background and the prior expects none, or the bare-background structure won selection),
**elastic-only** (the phonon-less ``elastic+bg`` structure won selection with a real fit: a
positive but categorically humble scan-local claim, confidence capped — SCENE-1 PR-a), **junk**
(catastrophic GoF with a structureless residual). The load-bearing **model-incomplete** route
keeps a *structured* residual (the broad quasi-elastic tail, cost/dof >> 1 yet the phonon is
real) out of the junk bucket.

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

# Phase 4a — under-model falsification (the L0 descriptor as a model-free check on the fit).
FLAG_ALPHA = 0.01      # family-wise false-flag prob (p_value*n_res) a descriptor peak must beat to
#                        count for/against the model. Stricter than the descriptor's ~0.05 detection
#                        alpha so a *resolved* fit is not spuriously flagged by a bare detection.
RESID_Z = 4.0          # a descriptor peak is UNMATCHED when the fitted model leaves a positive
#                        residual this significant (in σ) in its resolution window — i.e. the model
#                        under-predicts the observed excess there (real signal the model can't fit).
#                        Comparing to the model curve (not a fixed Ω_d position) is damping- and
#                        side-aware by construction (a stokes-only model predicts no anti-Stokes lobe,
#                        so a real anti-Stokes peak leaves a residual and is unmatched).
ELASTIC_LRT_ALPHA = 0.05  # a phonon-less `elastic+bg` winner claims elastic_only only when the
#                           elastic line is significant over bare background (the selection layer's
#                           nested LRT vs `bg`). A marginal AIC preference on Poisson noise is not
#                           an elastic line — without this gate a blank scan can read elastic_only.

MODEL_FRAC = 0.05      # …AND the model must treat that location as ~background (its peak-normalised
#                        signal there below this). Together these separate a DISTINCT unmodeled peak
#                        (real excess where the model predicts background) from a shoulder/broad tail
#                        of the model's own peak (which stays a model_incomplete, ω₀ still usable).


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


def _fitted_model(label, params, ctx):
    """The winning structure evaluated on ``ctx.x_used`` (counts space), rebuilt from its ``label`` +
    fitted ``params``. Spans BOTH the 1-mode and 2-mode enumerations: a winning 2-mode label
    (`stokes2+bg`) must reconstruct here, else a GoF-bad two-branch fit is silently mis-routed to
    `junk` (and the descriptor falsification is disabled for it). ``None`` if the label is not
    enumerable here or its params do not pack."""
    from isar.analyze.phonon import enumerate_phonon_multimode, enumerate_phonon_structures

    hyps = {h.label: h for h in (*enumerate_phonon_structures(ctx), *enumerate_phonon_multimode(ctx))}
    hyp = hyps.get(label)
    if hyp is None and "bg.BG" not in (params or {}):
        # A background-free degenerate twin (loop.without_background): the enumerated structure
        # that, less its background, carries exactly these parameters.
        from isar.analyze.loop import without_background

        names = set(params or ())
        hyp = next((twin for base in hyps.values() for twin in [without_background(base)]
                    if twin is not None and set(twin.build().param_names()) == names), None)
    if hyp is None:
        return None
    comp = hyp.build()
    try:
        theta = np.array([params[n] for n in comp.param_names()], dtype=float)
    except (KeyError, TypeError):
        return None
    return comp.evaluate(ctx.x_used, theta, ctx)


def _pull(result, ctx):
    """Pull residual (counts - model)/sqrt(counts) for the winning structure, rebuilt from its
    label + fitted params. Used only on the GoF-bad branch (junk vs model-incomplete)."""
    model = _fitted_model(result.label, result.params, ctx)
    if model is None:
        return None
    y = ctx.counts_used
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


def _elastic_significant(selection, *, alpha: float = ELASTIC_LRT_ALPHA) -> bool:
    """Whether the winning ``elastic+bg`` structure's elastic line is significant over bare
    background (its nested LRT vs ``bg``). **Fails closed**: the positive ``elastic_only``
    claim requires an established LRT, so a missing ``bg`` comparison (bg not converged, no
    nested diagnostic, invalid p) means NOT significant and the scan falls to the
    ``no_signal``/``underpowered`` null convention — never a positive claim without its
    evidence. Tests that expect ``elastic_only`` must therefore supply a real nested score."""
    scores = getattr(selection, "scores", None) or []
    sc = next((s for s in scores if s.label == "elastic+bg"), None)
    nested = getattr(sc, "nested", None)
    if not nested or nested.get("reduced") != "bg":
        return False
    p = nested.get("p_value")
    return bool(p is not None and np.isfinite(p) and p <= alpha)


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


# --- Phase 4a: under-model falsification (descriptor vs winning model) -----------------------
def _safe_descriptor(ctx):
    """Build the L0 descriptor from the context (self-contained default when the caller does not
    supply the already-computed one). ``None`` on any failure — the gate then no-ops."""
    from isar.analyze.descriptor import describe_spectrum
    try:
        return describe_spectrum(ctx.x, ctx.counts, center=ctx.center, gauss_fwhm=ctx.gauss_fwhm,
                                 lorentz_fwhm=ctx.lorentz_fwhm, mask=ctx.mask)
    except Exception:
        return None


def _significant_peaks(descriptor, *, alpha: float = FLAG_ALPHA):
    """Descriptor peaks that clear the *family-wise* flag strength ``p_value·n_res < alpha`` — the
    Bonferroni language of the descriptor's own detection contract, tightened for flagging."""
    if descriptor is None or not getattr(descriptor, "peaks", None):
        return []
    n_res = max(float(getattr(descriptor, "n_res", 1.0)), 1.0)
    return [p for p in descriptor.peaks if float(p.p_value) * n_res < alpha]


def _model_curve(win, ctx):
    """The winning model evaluated on ``ctx.x_used`` (counts space), rebuilt from its label + fitted
    params — like :func:`_pull`. ``None`` if the label is not enumerable here."""
    return _fitted_model(win.label, win.params, ctx)


def _residual_z_at(energy, model, ctx) -> float:
    """Significance (σ) of the model's *under-prediction* in the resolution window around ``energy``:
    ``Σ(counts − model) / √Σ max(model,1)`` over ±½·gauss_fwhm. Positive ⇒ the model leaves excess
    counts there (signal it can't fit); ≈0 for a good fit; negative when the model over-predicts."""
    x = ctx.x_used
    y = ctx.counts_used
    half = 0.5 * float(ctx.gauss_fwhm)
    win = np.abs(x - float(energy)) <= max(half, _grid_dx(x))
    if not np.any(win):
        return 0.0
    resid = float(np.sum(y[win] - model[win]))
    noise = float(np.sqrt(max(np.sum(np.maximum(model[win], 1.0)), 1.0)))
    return resid / noise


def _grid_dx(x) -> float:
    x = np.asarray(x, dtype=float)
    return float(np.median(np.abs(np.diff(x)))) if x.size > 1 else 1.0


def _model_bg(win, ctx, model) -> float:
    """The model's flat background (``bg.BG`` if fit, else the model's own minimum) — the level a
    'no peak here' region sits at."""
    bg = (getattr(win, "params", None) or {}).get("bg.BG")
    if bg is not None:
        return float(bg)
    return float(np.min(model)) if np.size(model) else 0.0


def _unmatched_descriptor_peaks(descriptor, win, ctx, model, *, z: float = RESID_Z,
                                frac: float = MODEL_FRAC):
    """Family-wise-significant descriptor peaks that are DISTINCT unmodeled features: the model both
    (a) under-predicts them (positive residual ≥ ``z`` σ in the window) and (b) treats their location
    as ~background (peak-normalised model signal < ``frac``). (b) keeps a shoulder / broad tail of the
    model's own peak — where the model already has intensity — out (that stays model_incomplete, ω₀
    usable); only a real peak where the model predicts background is unmatched. Curve-based ⇒ damping-
    and side-aware without a positional tolerance. ``model`` is the precomputed :func:`_model_curve`
    (``None`` if not enumerable)."""
    peaks = _significant_peaks(descriptor)
    if not peaks:
        return []
    if model is None:
        return []
    x = ctx.x_used
    signal = model - _model_bg(win, ctx, model)
    peak_h = float(np.max(signal)) if np.size(signal) else 0.0
    out = []
    for p in peaks:
        if _residual_z_at(p.energy, model, ctx) < z:
            continue
        i = int(np.argmin(np.abs(x - float(p.energy))))
        mf = (float(signal[i]) / peak_h) if peak_h > 1e-9 else 0.0
        if mf < frac:                                    # model treats this location as background
            out.append(p)
    return out


def _has_matched_descriptor_peak(descriptor, win, ctx, model, *, z: float = RESID_Z) -> bool:
    """Whether a family-wise-significant descriptor peak is *explained* by the model (residual below
    ``z`` — the model already predicts the excess there). The exact-Poisson confirmation the crude
    ``_excursion_above_bg`` can miss on a weak high-q Stokes line. ``model`` is the precomputed
    :func:`_model_curve` (``None`` if not enumerable)."""
    peaks = _significant_peaks(descriptor)
    if not peaks:
        return False
    if model is None:
        return False
    return any(_residual_z_at(p.energy, model, ctx) < z for p in peaks)


def _signal_unmodeled(unmatched, reasons, descriptor) -> "ScanVerdict":
    """The ``signal_unmodeled`` verdict: the descriptor localises real signal the phonon model does
    not explain. Confidence 0 (the phonon number is not trusted); the descriptor peaks are the
    deliverable. The driver nulls ω₀ and tags ``fit_kind="descriptor_only"`` (machine contract)."""
    n_res = max(float(getattr(descriptor, "n_res", 1.0)), 1.0)
    desc = ", ".join(f"E={float(p.energy):.2f} meV (p_fw={min(float(p.p_value) * n_res, 1.0):.1e})"
                     for p in unmatched)
    return ScanVerdict(
        "signal_unmodeled", 0.0,
        components={"unmatched_peaks": [{"energy": float(p.energy), "width": float(p.width),
                                         "significance": float(p.significance)} for p in unmatched]},
        reasons=reasons + [f"descriptor localises {len(unmatched)} significant peak(s) the model "
                           f"does not account for: {desc}"])


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


def held_for_calibration(reason: str) -> ScanVerdict:
    """Construct the held-for-calibration verdict. The pre-fit calibration gate (a missing energy
    zero, §7) calls this before any fit — a missing zero is a flagged non-result, never E0=0."""
    return ScanVerdict("held_for_calibration", 0.0, reasons=[reason])


def _count_probabilities(results, selection) -> dict:
    """Probability of each mode count, summed over the selection's scored structures."""
    n_modes = {r.label: int(r.n_modes) for r in results if getattr(r, "n_modes", None) is not None}
    out = {}
    for s in getattr(selection, "scores", None) or []:
        if s.label in n_modes and getattr(s, "weight", None) is not None and np.isfinite(s.weight):
            out[n_modes[s.label]] = out.get(n_modes[s.label], 0.0) + float(s.weight)
    return out


def _count_verdict(win, results, selection, count_probabilities, count_state, reasons, *,
                   default_rule):
    """The ``unconstrained`` verdict for an ambiguous peak count, else None: the fast pass's
    ``close_call``/``unresolved``, or ``search_limited`` with the winner's count below
    ``close_call_p`` (the search limit is named beside the alternatives). ``default_rule`` applies
    the same 0.80 rule to ``count_probabilities`` when the fast pass gave no state."""
    from isar.analyze.fast_pass import FastPassSettings

    close_call_p = FastPassSettings().close_call_p
    probs = {int(k): float(v) for k, v in
             (_count_probabilities(results, selection) if count_probabilities is None
              else count_probabilities).items()}
    n = None if getattr(win, "n_modes", None) is None else int(win.n_modes)
    if count_state is None and default_rule and n is not None and n in probs:
        count_state = "close_call" if probs[n] < close_call_p else None
    limited = count_state == "search_limited" and n in probs and probs[n] < close_call_p
    if count_state not in ("close_call", "unresolved") and not limited:
        return None
    others = ", ".join(f"{k} (P={p:.2f})" for k, p in sorted(probs.items()) if k != n)
    own = f" (P={probs[n]:.2f})" if n in probs else ""
    what = ("close call, search limited: the best count is at the search's limit" if limited
            else count_state.replace("_", " "))
    return ScanVerdict("unconstrained", 0.0, reasons=reasons + [
        f"peak count {what}: {n} modes{own}" + (f"; alternatives {others}" if others else "")])


def classify(results, ctx, selection, *, prior=None, descriptor=None,
             count_probabilities=None, count_state=None) -> ScanVerdict:
    """Classify one scan into a named bucket (§6) with a computed confidence (§9). Deterministic
    precedence (held → unconstrained → junk/model-incomplete/**signal_unmodeled** →
    elastic-only/no-signal (phonon-less winner) → no-signal/underpowered → resolved); never
    deletes (D7). When the data is flat, the prior + detection-power test split no-signal from
    (provisional) underpowered.

    ``descriptor`` (a :class:`~isar.schema.steering.ScanDescriptor`, Phase 4a) is the model-free
    peak list; when the winning model leaves a family-wise-significant peak unmatched the verdict is
    ``signal_unmodeled`` (under-model falsification). Built from ``ctx`` when None (self-contained).

    ``count_state`` is the fast pass's (:mod:`isar.analyze.fast_pass`): ``close_call`` and
    ``unresolved`` make the scan ``unconstrained``, naming the alternatives, even when the winner
    has no phonon; ``search_limited`` does the same when the winner's count is below the 0.80 rule
    and otherwise only adds its reason. Without it, the scan is a close call when the winner's count carries less than
    ``FastPassSettings.close_call_p`` of ``count_probabilities`` (count -> probability; defaults to
    the selection's). A close count is never ``resolved``: since only the winner is bootstrapped it
    can no longer show as a fork, and hard rule 5 lets only a clean single answer into the
    dispersion prior."""
    from isar.analyze import confidence as _confidence

    def conf(win):
        return _confidence.compute_confidence(win, ctx, selection, prior=prior)

    # 1. held-for-calibration — energy axis not usable (no fitted zero / needs conversion).
    if ctx.energy_status == "needs_conversion_and_zero":
        return held_for_calibration("energy axis uncalibrated (needs_conversion_and_zero)")

    # 2. nothing converged.
    if selection.best is None:
        return ScanVerdict("unconstrained", 0.0, reasons=["no admissible structure converged"])
    win = {r.label: r for r in results}[selection.best]
    reasons = []

    # Phase 4a — model-free descriptor as an under-model falsification check on the winning fit.
    if descriptor is None:
        descriptor = _safe_descriptor(ctx)
    # Rebuild the winning model once (both descriptor helpers reuse it); skip the rebuild entirely
    # when there is no significant descriptor peak, matching the helpers' own early-out.
    model_curve = _model_curve(win, ctx) if _significant_peaks(descriptor) else None
    unmatched = _unmatched_descriptor_peaks(descriptor, win, ctx, model_curve)
    matched = _has_matched_descriptor_peak(descriptor, win, ctx, model_curve)

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

    # 5. catastrophic GoF -> signal_unmodeled (descriptor localises the miss) / model-incomplete
    #    (structured residual) / junk (structureless).
    if np.isfinite(win.cost_per_dof) and win.cost_per_dof >= GOF_BAD:
        if unmatched:
            return _signal_unmodeled(unmatched, reasons + [f"cost/dof={win.cost_per_dof:.1f}"],
                                     descriptor)
        pull = _pull(win, ctx)
        structured = pull is not None and _residual_structured(pull)
        if structured:
            score, comp = conf(win)
            return ScanVerdict("model_incomplete", score, components=comp,
                               reasons=reasons + [f"cost/dof={win.cost_per_dof:.1f}, structured "
                                                  "residual (missing component, not junk)"])
        return ScanVerdict("junk", 0.0,
                           reasons=reasons + [f"cost/dof={win.cost_per_dof:.1f}, structureless residual"])

    # 5b. phonon-less winner (SCENE-1 PR-a): selection preferred a structure with no inelastic
    #     component. `bg` keeps the existing null convention (the no-signal / underpowered
    #     detection-power split, constant 0.0); `elastic+bg` is the positive scan-local
    #     `elastic_only` claim — categorically humble (capped computed confidence), never region
    #     exclusion. A significant descriptor peak the null leaves unexplained outranks both.
    if win.label in ("bg", "elastic+bg", "elastic"):   # "elastic": elastic+bg's background-free twin
        if unmatched:
            return _signal_unmodeled(unmatched, reasons + ["phonon-less winner leaves descriptor "
                                                           "peak(s) unexplained"], descriptor)
        # a close call between none and some peaks is no claim of absence (fast pass state only)
        if count_state is not None:
            v = _count_verdict(win, results, selection, count_probabilities, count_state,
                               reasons, default_rule=False)
            if v is not None:
                return v
        dp = detection_power(ctx, prior, win) if _prior_expects_phonon(prior, ctx) else None
        claim_elastic = win.label != "bg" and _elastic_significant(selection)
        if not claim_elastic:
            base = ("background alone won selection" if win.label == "bg" else
                    "elastic+bg won selection without established significance over "
                    "background (insignificant or missing LRT vs bg)")
            if dp is None:
                return ScanVerdict("no_signal", 0.0, reasons=reasons + [base])
            if dp["detectable"] is True:
                return ScanVerdict("no_signal", 0.0, detection=dp,
                                   reasons=reasons + [base + "; prior expected a phonon the scan "
                                                      "had power to see (prior may be wrong)"])
            return ScanVerdict("underpowered", 0.0, detection=dp, provisional=True,
                               reasons=reasons + [base + "; prior expects a phonon below the "
                                                  "detection threshold"])
        score, comp = _confidence.compute_null_confidence(ctx, selection, detection=dp)
        provisional = dp is not None and dp["detectable"] is not True
        return ScanVerdict("elastic_only", score, components=comp, detection=dp,
                           provisional=provisional,
                           reasons=reasons + ["elastic line + background explain this scan; "
                                              "scan-local claim only (elastic intensity can mask "
                                              "signals), never region exclusion"])

    # 6. no excursion above background -> the exact-Poisson descriptor overrides the crude excursion
    #    test: a matched significant peak rescues toward resolved (weak high-q Stokes), an unmatched
    #    one is signal_unmodeled; otherwise the no-signal / underpowered (detection-power) split.
    if not _excursion_above_bg(ctx):
        if unmatched:
            return _signal_unmodeled(unmatched, reasons + ["no crude excursion"], descriptor)
        if not matched:                                  # descriptor confirms nothing -> flat
            if _prior_expects_phonon(prior, ctx):
                dp = detection_power(ctx, prior, win)
                if dp["detectable"] is True:             # had the power, saw nothing: genuine absence
                    return ScanVerdict("no_signal", 0.0, detection=dp,
                                       reasons=reasons + ["no excursion; prior expected a phonon the "
                                                          "scan had power to see (prior may be wrong)"])
                # couldn't (or can't confirm) the power -> underpowered, provisional (the §0.4 guard:
                # prior-conditional, never marks the region confirmed-empty).
                return ScanVerdict("underpowered", 0.0, detection=dp, provisional=True,
                                   reasons=reasons + ["prior expects a phonon below the detection "
                                                      "threshold at the achieved statistics"])
            return ScanVerdict("no_signal", 0.0, reasons=reasons + ["no excursion above background"])
        # matched significant descriptor peak with no unmatched -> fall through to resolved.

    # 7. resolved — unless the descriptor confirms a peak the model missed (under-model falsification).
    if unmatched:
        return _signal_unmodeled(unmatched, reasons + ["model resolved fewer peaks than the "
                                                       "descriptor confirms"], descriptor)
    # ...or the peak count is a close call or unresolved: no single answer, like a fork with no
    # prior to break the tie. The fast pass's state decides; without one (hand-built selections),
    # the same 0.80 rule on the count probabilities.
    v = _count_verdict(win, results, selection, count_probabilities, count_state, reasons,
                       default_rule=True)
    if v is not None:
        return v
    if count_state == "search_limited":
        reasons.append("peak count search limited: the best count is at the search's limit")
    score, comp = conf(win)
    return ScanVerdict("resolved", score, components=comp, reasons=reasons or ["resolved"])

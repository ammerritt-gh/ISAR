# One scan, start to finish

To make the pipeline concrete, here is a single S30 (IXS) control scan followed all the way through —
the same path every scan takes. Figures referenced live in `out/phase3_step1/figures/` (regenerate with
`python docs/phase3_step1_check.py`); each carries its own caption.

We use **scan 168**, analyzer **ana5**, at Q = (3.77, K, −0.09), T ≈ 100 K — one of the six FeSeS_1
control scans.

## 1. The raw scan

We start with counts vs energy transfer for one analyzer: a peak near zero (the elastic line) and a
phonon peak a few meV out, on a flat background. Errors are √N (Poisson). Nothing is normalized away
or deleted; masked points (if any) stay visible.

## 2. Resolution — measured, not assumed

ISAR fits the elastic line to get the instrument resolution as a **Voigt** (a Gaussian width and a
Lorentzian width). On IXS the elastic line is in-window, so the resolution is *measured* per scan rather
than taken from a table. (This is exactly the contrast with triple-axis, where without a vanadium scan
the resolution can't be separated from the intrinsic width — see the IN8 report.)

## 3. Energy zero

The elastic line also fixes the **energy zero** (where 0 meV sits). This scan has its own usable
elastic, so it keeps its own zero. A scan *without* one would borrow an interpolated zero from
neighbouring calibration scans, or — if none is available — be `held_for_calibration` rather than fit
with the zero forced to 0. See `figures/energy_zero_series.png`.

## 4. Structure loop — which model?

ISAR enumerates candidate structures — here `{stokes+bg, elastic+stokes+bg}` — and fits each. The
initial conditions come from a grid scan over the phonon's nonlinear parameter plus non-negative
least-squares for the amplitudes (no hand-tuning). On this scan `elastic+stokes+bg` fits far better
(cost/dof a few, vs ~16–33 for `stokes+bg`): the elastic line is real and must be in the model.
See `figures/s30_scan168_ana5.png` — the data with the winning fit drawn as separate phonon / elastic /
background pieces and a pull residual underneath.

## 5. The fit

The winning model gives the phonon energy ω₀ ≈ 6.6 meV (with Γ and the component amplitudes). ω₀ is
derived as √(Ω_d² + Γ²), never fit directly, which keeps it physical.

## 6. Honest error bars

ISAR runs a **parametric bootstrap** (resample Poisson data from the best fit, refit many times) and a
profile-likelihood interval, then checks them against the quick curvature (HESSE) error. On this
well-counted scan the bootstrap 68% interval and HESSE ±σ overlap — the *asymptotic* regime — so the
quick number is trustworthy here. See `figures/bootstrap_omega0_histograms.png` (the panel for this
scan shows the two bands sitting on top of each other). Remember these are **statistical-only** error
bars (counting noise), per the [stats primer](STATS_PRIMER.md).

## 7. Model selection + modality

Across the candidate models, AIC gives `elastic+stokes+bg` essentially all the Akaike weight, and the
pooled energy distribution is unimodal → **fold** (a single agreed answer). See
`figures/selection_akaike_weights.png`.

## 8. Verdict

Putting it together — clean fit, bands agree, one clear model — the scan is **resolved** with high
confidence. See `figures/verdict_confidence.png`. In a real run this is one of the scans you'd find
already fit when you walk in; you could move straight to the ones ISAR flagged or declined.

---

The same eight steps run on every scan. What changes from scan to scan is *where it stops being clean* —
a missing elastic (step 3 → held), an ambiguous structure (step 7 → fork), too few counts (→
underpowered), a structureless misfit (→ junk). Those stops are the triage signal; see
[INTERPRETING_A_VERDICT.md](INTERPRETING_A_VERDICT.md).

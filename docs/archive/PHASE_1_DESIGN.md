# ISAR Phase 1 — Synthetic Core Validation

**Status:** Design, ready to build.
**Relationship to master design:** This is the concrete realization of `DESIGN.md` §11
(validation harness) and step 1 of §14, done with **synthetic ground truth** instead of the
human-curated FeSeS_1 CSV. It also pre-tests two §7.5 core items (AIC/BIC peak-count selection,
the overdamped-penalty behavior) before they touch real data. It deliberately ignores
everything in §§4–7 about ingestion, parsing, calibration, sessions, and LLMs.

---

## 1. Purpose — two deliverables from one machine

The synthetic harness serves two distinct goals, and both are explicit from the start because they
shape the build differently:

**1a — Validation (internal gate).** Given data generated from a known model with known noise, does
the analysis core recover the known parameters, and are its reported uncertainties honest? If the
core cannot do that on data we made ourselves, no amount of parsing, orchestration, or LLM assistance
matters. This is the gate everything else stands on. The output is a **quantified** statement about
bias, uncertainty calibration, and failure rate — not "a fit that looks right."

**1b — Demonstration (external credibility).** Show, by example on data where the answer is known,
what the common naive assumptions do — √N error bars, Neyman weighting, the Gaussian-channel
assumption at low counts — versus the principled choice. A bias *argued for* is dismissed as a
difference of opinion; a bias *shown* on known-truth data is a fact that has to be accepted or
refuted. This is the persuasive core: it makes the methodology believable to collaborators and
reviewers rather than something to wave off. It is also the principled successor to the
Errors-in-Fitting program (§8).

The two share one engine — generate from truth, fit, score against truth — so building 1a correctly
gives 1b almost for free, *provided the fit's cost function is a pluggable choice* (§6, §8).

---

## 2. Scope

**In:**
- A synthetic-scan generator built on the **production** `Fitting_Functions.py`
  (`convolute_voigt_dho` and friends), so we test the real lineshape code, not a re-implementation.
- Poisson noise with a physically interpretable counting-statistics knob.
- A **cost-function-agnostic** fit layer so the same data can be fit under Neyman χ², Pearson χ², and
  the Poisson likelihood (Baker–Cousins), side by side.
- A Monte-Carlo scorer measuring recovery (bias), uncertainty calibration (coverage / pull), and
  fit success rate.
- A ground-truth manifest per scan, RNG-seeded for exact reproducibility.

**Out (deliberately):**
- No SPEC/IN8 parsing — synthetic data starts as arrays, bypassing the format problem entirely.
- No energy-zero calibration — `center` is fixed at 0 (see §4).
- No resolution *measurement* — resolution is a known input, fixed during the fit.
- No LangGraph, no LLMM, no session store, no reports. Phase 1 is a standalone module.

---

## 3. Core method, and the inverse crime

Phase 1 generates data from the same model it fits with. In inverse-problems terms this is the
**inverse crime**, and here it is committed *on purpose*: sharing the model isolates the optimizer
and the error machinery from any model-mismatch, so a failure can only be the fitter's fault.

This is necessary but **not sufficient**. Passing means the code is self-consistent; it does not
mean it survives real data, where the lineshape is not exactly DHO⊗Voigt, the assumed resolution
is not exactly the true one, and counting is not perfectly Poisson. Phase 1 therefore proves the
*machinery*; **Phase 1b (§10.1) breaks the inverse crime deliberately** to probe robustness. Keeping
these two stages distinct is the point — don't let a clean inverse-crime pass be mistaken for
"the fitter works on real data."

---

## 4. The happy path, defined

The happy path is the comfortable regime where everything should work. Concretely:

- **One Stokes phonon + one elastic line + flat background**, generated with the full model
  (`peak='all'`) so anti-Stokes is present but small at the chosen temperature — physically honest,
  no model mismatch.
- **`center = 0`, fixed.** This is the correct INS case: on a fixed-kf triple-axis the energy axis
  is absolute (`energy.absolute = true` in the card), so the elastic line sits at zero by
  construction and there is no zero-offset to fit. Fixing it removes the peak-assignment ↔
  energy-zero coupling (a HERIX problem, `DESIGN.md` §8) so Phase 1 tests *only* ω/Γ recovery.
- **ω₀ comfortably above Γ** (well underdamped, far from the `Stokes_DHO` overdamped boundary at
  Γ = ω₀).
- **Phonon well separated** from the elastic line (peak position √(ω₀²−Γ²) several resolution
  widths off zero).
- **Healthy counting statistics** (high peak counts; see §5.3).
- **Temperature fixed** at the true value — on a real experiment T is the regulated setpoint and is
  known, so it is an input, not a fit parameter.

**Fixed vs free during the fit:**

| Parameter | Phase 1 happy path | Why |
|---|---|---|
| `center` (energy zero) | **fixed = 0** | INS absolute axis; isolates ω/Γ recovery |
| `gauss_fwhm`, `lorentz_fwhm` (resolution) | **fixed = true** | resolution is measured separately and fixed in the real pipeline; degenerate with Γ if freed |
| `T` (temperature) | **fixed = true** | known setpoint; only enters via the Bose factor |
| `phonon_energy` (ω₀) | **free** | primary recovery target |
| `damping` (Γ) | **free** | primary recovery target |
| `phonon_amplitude` | **free** | |
| `elastic_amplitude` | **free** | |
| `BG` | **free** | |

Five free parameters. This matches how the production pipeline fits (resolution fixed from Bragg,
zero fixed or pre-calibrated, T known).

---

## 5. Synthetic data generation

### 5.1 Model
Use `convolute_voigt_dho(...)` from the production `Fitting_Functions.py` unchanged. The DHO peak
sits at √(ω₀²−Γ²); the anti-Stokes amplitude is tied to the Stokes amplitude by the Bose factor
`exp(−ω₀/(k_B T))` already implemented there. Reusing this exact function is what makes Phase 1 a
test of the real code.

### 5.2 Ground-truth manifest
Every generated scan records the full parameter set and the RNG seed:

```
{ omega0, gamma, phonon_amplitude, elastic_amplitude, BG,
  gauss_fwhm, lorentz_fwhm, T, center=0,
  x_grid (min, max, step), target_peak_counts, seed }
```

This is the synthetic analog of the gold-standard CSV — but **exact**, not human-judged. The scorer
reads it to compute recovery. Seeds make any failing realization reproducible bit-for-bit.

### 5.3 Noise model, and why the weighting question is deeper than it looks
Scale the model so the phonon maximum reaches a chosen count level above background (**target peak
counts** — the physically interpretable knob, mapping onto "how long we counted," which is what the
triage tool reasons about), then draw **one** `Poisson(model)` sample per channel.

The subtlety: a χ² fit treats each channel as value ± σ drawn from a **symmetric Gaussian**. The √N
error bar, Neyman weighting, and Pearson weighting are all **Gaussian approximations to a Poisson
channel**, and they only hold when the per-channel mean is large (λ ≳ ~20). At λ = 0, 1, 2 the true
Poisson distribution is discrete and strongly asymmetric — nothing like a bell curve — so the failure
there is in the **channel model itself**, not in the choice of denominator. This is the point worth
being precise about from the beginning, because the wings of every scan are low-count, and the wings
are where Γ and the weak low-T anti-Stokes live — the two quantities the whole design exists to
extract.

Three estimators are therefore on the table, and the harness treats the choice as pluggable:

| Estimator | Per-channel weight | Behavior at low counts | 0-count channel |
|---|---|---|---|
| **Neyman χ²** | σ² = data (N) | biases **low** (chases down-fluctuations) | σ = 0 → ∞ weight; must floor/drop (arbitrary) |
| **Pearson χ²** | σ² = model (f) | smaller **upward** bias | finite (model > 0); no floor needed |
| **Poisson likelihood (Baker–Cousins)** | 2Σ[fᵢ − yᵢ + yᵢ ln(yᵢ/fᵢ)] | **unbiased** (it is the likelihood) | term → 2fᵢ as y → 0; exact, no floor |

The constant-rate identity makes the straddle concrete: fitting one flat rate μ to N Poisson channels,
Neyman → **harmonic mean**, Pearson → **RMS**, likelihood → **arithmetic mean**. Since harmonic ≤
arithmetic ≤ RMS always, the two χ²'s sit on either side of the unbiased likelihood, and all three
biases shrink like 1/counts. That identity is also the cleanest demonstration (§8).

### 5.4 Grid
A representative IN8/IXS energy grid (e.g. −N…+N meV in ~0.25–0.5 meV steps, matching real scan
point counts). Keep it in the manifest so the scorer and any later parser-fed data use the same axis.

---

## 6. Fitting setup — cost function is a first-class, pluggable choice

The fit layer must run any of the three estimators on the same data, because both the validation
(1a) and the demonstration (1b) depend on swapping them cleanly.

- **`curve_fit` cannot carry all three.** It is hardwired to least-squares: `sigma` is a fixed array
  scaling squared residuals, so it natively does **Neyman** (σ = √data). **Pearson** needs iteration
  (fit, recompute σ² = f at the best fit, refit until stable). **Baker–Cousins** needs a
  likelihood-capable minimizer — `scipy.optimize.minimize` on −2 ln L, `lmfit` with a custom
  objective, or `iminuit` (built for binned-likelihood cost functions, with MINOS for correct
  asymmetric parameter errors). Choosing the backend is a real decision (§13), not a one-line swap.
- **Parameter errors split from plotted error bars under likelihood.** χ² hands you a symmetric ±σ
  per point that doubles as the error bar you draw. The likelihood does not — the true low-count
  uncertainty is asymmetric. Fit-parameter errors come from the likelihood curvature or, better, the
  profile (Δ(−2 ln L) = 1) and come out correctly asymmetric; the error bar *drawn* on a 1-count
  point becomes a separate, approximate object (Garwood / Feldman–Cousins, or the √(N+1) hack). Keep
  these two as distinct quantities in code and in figures.
- **`sigma`-convention check (carry over from Errors-in-Fitting).** The demo passes `sigma = 1/error`
  to `curve_fit`, which inverts the weighting (up-weights the high-count peak channels instead of
  down-weighting them) and, with `absolute_sigma=True`, distorts the reported covariance. The Neyman
  path here passes `sigma =` the per-point standard deviation with `absolute_sigma=True`. **Action:
  confirm the production `FeSeS_1_Analysis.py` curve_fit call does not inherit the same flip** — if it
  does, that is a real finding affecting every reported error in the existing results.
- Initial guesses come from the true values perturbed, or simple data heuristics (argmax for the
  peak). The happy path should be insensitive to reasonable IC choices; IC *sensitivity* is a boundary
  test (§10), not a happy-path concern.

---

## 7. Scoring & acceptance

For a fixed ground-truth parameter set, run **M noise realizations** (distinct seeds), fit each, and
accumulate distributions of:

- **Bias:** ⟨p_fit⟩ − p_true for each parameter p (focus on ω₀ and Γ).
- **Coverage / pull:** the pull (p_fit − p_true)/σ_p, where σ_p is the fitter's reported error.
- **Fit success rate:** fraction that converge without the overdamped penalty or `RuntimeError`.

The **pull distribution is the single cleanest diagnostic** and subsumes the other two: its mean
tests for bias, its width tests whether the reported errors are honest. This is the rigorous version
of the cov-vs-empirical-error comparison already built in
`Demonstrate_Cov_vs._Fit_of_Single_Gaussian.py`.

**Happy-path pass criteria** (run under the chosen production estimator):
1. Fit success rate ≈ 100 % in the comfortable regime.
2. |bias| ≪ σ for ω₀ and Γ (statistically negligible offset).
3. Pull for ω₀ and Γ consistent with unit-normal: mean ≈ 0, std ≈ 1 (within a tolerance band over M).
4. Recovered ω₀, Γ within a stated absolute tolerance of truth.

If pull-width > 1, the fitter is **overconfident** (reported errors too small) — a result we need to
know before any confidence score (`DESIGN.md` §12) is built on those errors.

Then **sweep the ground-truth set** across a grid (ω₀, Γ, target counts) and report the
pull/bias/success-rate surface.

---

## 8. Demonstration framework — the three-estimator comparison

This is deliverable 1b made concrete: the same generate→fit→score engine, run under all three
estimators, producing artifacts that *show* what naive assumptions cost. The design choices here are
about persuasion, so they matter.

**Layered, so it builds from the undeniable to the consequential:**

- **Layer 1 — the constant-rate identity (analytic, indisputable).** Fit a single flat rate μ to N
  Poisson channels under all three estimators; recover harmonic mean (Neyman), RMS (Pearson),
  arithmetic mean (likelihood), landing exactly where the algebra in §5.3 says. Minimal, no model
  complexity, and the result is a theorem — the opener a skeptic cannot argue with. It establishes
  "these methods provably disagree, and here is which way each leans" before any DHO is involved.
- **Layer 2 — the DHO case in physics terms.** Carry the same comparison into the real model and show
  the bias propagates into **Γ** (linewidth) and the **Stokes/anti-Stokes amplitude ratio** at
  realistic low counts. Framing the impact as *the physics quantities we publish* — not abstract
  parameter bias — is what makes it land with physicists rather than statisticians. These are the
  numbers that go in papers, so a demonstrable bias in them is the whole argument.

**Honesty is the persuasion strategy.** Report the **full** bias/pull surface versus counts, including
the high-count regime where all three estimators agree. Showing that the naive method is *not always*
wrong — that it fails specifically and predictably at low counts — makes the claim credible rather
than polemical. A reviewer who sees the methods converge where they should is far more likely to
believe the divergence where they don't.

**Successor to Errors-in-Fitting.** This generalizes that program from a single Gaussian to the real
DHO⊗Voigt model, and from "covariance error vs empirical fit-error spread" to "three estimators vs
known truth." The figures it produces — the constant-rate straddle, the Γ-bias-vs-counts curves, the
pull histograms — are reusable directly in methods sections, talks, and onboarding. Treat them as
durable outputs, not throwaway diagnostics.

*(This is a methods-credibility goal, somewhat separate from ISAR's live-triage purpose, but cheap to
carry because it rides on the validation engine. Keeping it explicit from the start prevents the
estimator choice from being quietly hardcoded, which would forfeit the demonstration.)*

---

## 9. Module layout & outputs

Standalone, no orchestration:

```
isar/
  synth/
    generate.py     # build noisy scans from convolute_voigt_dho + manifest; seeded
    noise.py        # counts scaling + Poisson draw
    estimators.py   # Neyman / Pearson / Baker-Cousins cost fns + param-error extraction
    fit.py          # run a chosen estimator against a scan; backend (curve_fit / minimize / iminuit)
    score.py        # Monte-Carlo recovery: bias, pull, coverage, success rate
    demo.py         # the §8 three-estimator comparison (constant-rate + DHO layers)
    config.py       # ground-truth ranges, grid, M, sweep axes, estimator set
  Fitting_Functions.py   # the production library, imported unchanged (do not fork)
  out/
    manifests/      # per-scan ground truth (JSON)
    figures/        # pull histograms, recovery-vs-counts surfaces, estimator-comparison plots
    summary.csv     # per-cell bias/pull-width/success-rate, per estimator
```

Run (cmd):

```
python -m isar.synth.score      :: validation gate
python -m isar.synth.demo       :: three-estimator demonstration figures
```

A green happy path is flat ~zero bias, ~unit pull width, ~100 % success across the comfortable cells.
The demonstration output is the estimator-comparison surface showing where the naive choices diverge
from truth.

---

## 10. Boundary sweeps (after the happy path passes)

The generator should be built to reach the regimes where the design's hard decisions live, even though
the happy path stays comfortable. These are the cells where the §7 surface is expected to degrade, and
characterizing that degradation is the real payoff:

- **Overdamped boundary:** ω₀ → Γ. `Stokes_DHO` returns a `1e6` penalty when Γ ≥ ω₀, which makes the
  objective non-smooth right at the boundary and is a plausible source of the optimizer-init failures
  seen on real data. Phase 1 is the right place to characterize this and decide whether to replace the
  penalty with a **bounded/transformed parameterization** (fit the damped frequency directly, or bound
  via `least_squares`/`minimize` with a transform). **Flag, don't fix yet** — the sweep tells us if it
  is needed.
- **Stokes–elastic merge:** ω₀ shrinking toward the resolution width, so the phonon slides under the
  elastic line — the low-K problem.
- **Anti-Stokes on/off:** raise T so k_B T approaches ℏω₀ and the anti-Stokes grows from negligible to
  comparable — tests the Bose-factor handling across temperature. (Also a Layer-2 demonstration cell,
  since the anti-Stokes is low-count.)
- **Low counts:** drop target peak counts until fits start failing — where the estimator choice (§5.3)
  bites hardest and where IC sensitivity appears.

### 10.1 Phase 1b — break the inverse crime
Generate with a resolution slightly different from the one the fitter assumes (the real case:
resolution is measured with error), and/or inject a faint second mode the fitter does not model. Watch
the **Γ bias** — resolution error feeds directly into the reported linewidth (`DESIGN.md` §8). This is
the test that estimates what the tool does on real data, as opposed to on its own model.

---

## 11. Phase 1c — peak-count model selection

Once single-phonon recovery passes, the high-value synthetic test targets the design's hardest
deterministic decision (`DESIGN.md` §7.5): **how many peaks.** Generate two-peak data — *elastic +
Stokes* versus *Stokes + anti-Stokes* at a T where both are visible — and check whether **AIC/BIC (or a
nested F-test)** selects the true peak count, **never raw χ²** (which always favors more peaks). Because
the true count is known by construction, this is the only clean way to validate that criterion before
it runs on real spectra. (Note: AIC/BIC are likelihood-based, which lines up with adopting the Poisson
likelihood in §6 — the model-selection number and the fit cost then come from the same statistic.)

---

## 12. Explicit non-goals (to prevent scope creep)

- Not wiring any orchestration, LLM, or parser — the middle of the pipeline only.
- Not measuring resolution — it is a known input here.
- Not fitting the energy zero — `center = 0`, fixed.
- Not chasing real-data robustness in Phase 1 proper — that is Phase 1b's job, kept separate on purpose.

---

## 13. Open questions / decisions

1. **Cost function and minimizer backend.** The demonstration (§8) runs all three estimators
   regardless; the open decision is which becomes ISAR's **production default** and what minimizer
   carries it. Lean: **Baker–Cousins (Poisson likelihood)** for honest low-count behavior on Γ and the
   anti-Stokes, which means leaving `curve_fit` for `minimize`/`lmfit`/`iminuit`. This choice also sets
   whether parameter errors come from covariance or the likelihood profile, and lines up with the
   likelihood-based AIC/BIC in §11. The cost is a heavier fit backend than the existing code uses.
2. **M (realizations per cell)** and sweep-grid density — trade fidelity against runtime. The prior
   demo ran 100k single Gaussian fits via multiprocessing; the convolution plus a likelihood minimizer
   is much heavier per fit, so size M accordingly.
3. **Tolerance bands** for the pull mean/width and the ω₀/Γ recovery — what counts as "pass."
4. **Overdamped penalty (§10):** characterize now or defer the fix. Lean: characterize in the sweep,
   fix only if it causes failures inside the regime we care about.
5. **Grid/axis (§5.4):** pick the representative energy range and step to match real IN8/IXS scans so
   the synthetic axis is the one a parser will later feed.
6. **Drawn error bars for low-count channels:** which interval convention (Garwood, Feldman–Cousins,
   √(N+1)) for *display*, given it is now decoupled from the fit uncertainty (§6).

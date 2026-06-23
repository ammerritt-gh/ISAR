# ISAR Phase 1 — Synthetic Core Validation

**Status:** Design, settled — ready to build.
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
reviewers. It is also the principled successor to the Errors-in-Fitting program (§8).

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
- A **reparameterized fit** (damped peak position Ω_d, Γ) that makes the overdamped boundary
  unreachable (§4, §6).
- A Monte-Carlo scorer measuring recovery (bias), uncertainty calibration (coverage / pull), and
  fit success rate.
- A concrete test dispersion (acoustic phonon, §5.4) so the harness exercises a realistic Q-series,
  not isolated scans.
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

## 4. The happy path, and the fit parameterization

The happy path is the comfortable regime where everything should work — concretely, the **mid-zone**
points of the test dispersion (§5.4), where the phonon is well above Γ and well clear of the elastic
line. The near-zone-center points of that same dispersion are the boundary tests (§10), reached by
walking the same curve outward.

- **One Stokes phonon + elastic line + flat background**, generated with the full model
  (`peak='all'`) so anti-Stokes is present at the level the Bose factor dictates for the chosen T —
  physically honest, no model mismatch, and it exercises the Bose-factor handling.
- **`center = 0`, fixed.** The correct INS case: on a fixed-kf triple-axis the energy axis is
  absolute (`energy.absolute = true`), the elastic line sits at zero by construction, and there is no
  zero-offset to fit. Fixing it removes the peak-assignment ↔ energy-zero coupling (a HERIX problem,
  `DESIGN.md` §8) so Phase 1 tests *only* ω/Γ recovery.
- **Resolution and T fixed at the true values** — resolution is measured separately and fixed in the
  real pipeline (and is degenerate with Γ if freed); T is the known regulated setpoint, entering only
  via the Bose factor.

### 4.1 Reparameterize to the damped peak position (overdamping fix)
The DHO Stokes term `amplitude / ((x − √(ω₀²−Γ²))² + Γ²)` is only valid underdamped (ω₀ > Γ); the
production `Stokes_DHO` guards the boundary with a flat `1e6` penalty, which is **discontinuous** and
hands the optimizer a useless gradient right where the test dispersion drives it (ω → 0 near the zone
centers). The fix is not a softer penalty but a **reparameterization**: fit the **damped peak
position** Ω_d = √(ω₀²−Γ²) and Γ, instead of (ω₀, Γ).

- ω₀ = √(Ω_d² + Γ²) ≥ Γ holds **by construction** → the overdamped region is unreachable, no guard
  needed.
- The Stokes term collapses to a plain Lorentzian centered at Ω_d — no √-branch, no cliff.
- The old boundary ω₀ = Γ maps to **Ω_d = 0**, which is the smooth, correct physical limit (phonon on
  the elastic line). The regime that used to be a wall now produces the right merge behavior for free.
- **No hyperparameter** (unlike a barrier strength), which is why this beats a soft penalty.

Implementation: confine it to the **fit layer** — vary (Ω_d, Γ) with Ω_d ≥ small ε, convert to
ω₀ = √(Ω_d²+Γ²), call the production model unchanged (the guard then never fires, since
ω₀²−Γ² = Ω_d² ≥ ε²). Hold a **log-barrier** on (ω₀²−Γ²) in reserve only if genuinely overdamped modes
are ever needed — this acoustic case does not. If Phase 1 confirms the behavior, the durable fix is to
push the peak-position parameterization into the shared `Fitting_Functions.py` and retire the guard.

**Fixed vs free during the fit:**

| Parameter | Phase 1 happy path | Why |
|---|---|---|
| `center` (energy zero) | **fixed = 0** | INS absolute axis; isolates recovery |
| resolution (`gauss_fwhm`, `lorentz_fwhm`) | **fixed = true** | measured separately; degenerate with Γ if freed |
| `T` (temperature) | **fixed = true** | known setpoint; enters via Bose factor |
| `Ω_d` (damped peak position) | **free** | primary target; reported as ω₀ via transform |
| `Γ` (damping) | **free** | primary target |
| `phonon_amplitude` | **free** | |
| `elastic_amplitude` | **free** | |
| `BG` | **free** | |

Five free parameters. Recovered (Ω_d, Γ) are reported as (ω₀, Γ) with errors propagated through the
transform; ω₀ is the conventional dispersion quantity.

---

## 5. Synthetic data generation

### 5.1 Model
Use `convolute_voigt_dho(...)` from the production `Fitting_Functions.py` unchanged. The DHO peak sits
at Ω_d = √(ω₀²−Γ²); the anti-Stokes amplitude is tied to the Stokes amplitude by the Bose factor
`exp(−ω₀/(k_B T))` already implemented there. Reusing this exact function is what makes Phase 1 a test
of the real code.

### 5.2 Ground-truth manifest
Every generated scan records the full parameter set and the RNG seed:

```
{ H, omega0, gamma, phonon_amplitude, elastic_amplitude, BG,
  gauss_fwhm, lorentz_fwhm, T, center=0,
  x_grid (min, max, step), target_peak_counts, seed }
```

The synthetic analog of the gold-standard CSV — but **exact**, not human-judged. Seeds make any
failing realization reproducible bit-for-bit.

### 5.3 Noise model, and why the weighting question is deeper than it looks
Scale the model so the phonon maximum reaches a chosen count level above background (**target peak
counts** — the physically interpretable knob, mapping onto "how long we counted," which is what the
triage tool reasons about), then draw **one** `Poisson(model)` sample per channel.

A χ² fit treats each channel as value ± σ from a **symmetric Gaussian**. The √N error bar, Neyman
weighting, and Pearson weighting are all **Gaussian approximations to a Poisson channel**, valid only
when the per-channel mean is large (λ ≳ ~20). At λ = 0, 1, 2 the true Poisson distribution is discrete
and strongly asymmetric, so the failure there is in the **channel model itself**, not the choice of
denominator. This matters because the wings of every scan are low-count, and the wings carry Γ and the
weak low-T anti-Stokes — the two quantities the whole design exists to extract.

Three estimators, treated as a pluggable choice:

| Estimator | Per-channel weight | Low counts | 0-count channel |
|---|---|---|---|
| **Neyman χ²** | σ² = data (N) | biases **low** (chases down-fluctuations) | σ = 0 → ∞ weight; must floor/drop (arbitrary) |
| **Pearson χ²** | σ² = model (f) | smaller **upward** bias | finite (model > 0); no floor |
| **Poisson likelihood (Baker–Cousins)** | 2Σ[fᵢ − yᵢ + yᵢ ln(yᵢ/fᵢ)] | **unbiased** (it is the likelihood) | term → 2fᵢ as y → 0; exact, no floor |

Constant-rate identity (fit one flat rate μ to N Poisson channels): Neyman → **harmonic mean**,
Pearson → **RMS**, likelihood → **arithmetic mean**. Since harmonic ≤ arithmetic ≤ RMS, the two χ²'s
straddle the unbiased likelihood; all three biases shrink like 1/counts. That identity is also the
cleanest demonstration (§8).

### 5.4 Test dispersion and grid (settled)
A typical acoustic phonon, **(2,0,0) → (3,0,0)**:

- **H from 2.00 to 3.00, step 0.05** → 21 Q points, each its own scan.
- **Energy axis: step 0.25 meV**, window ≈ **−10 … +10 meV** (~81 channels) — covers elastic, Stokes,
  and anti-Stokes with resolution-tail margin. Window is tunable.
- **Dispersion:** ω₀(H) = **6·|sin(π·(H − 2))|** meV — zero at the zone centers (H = 2, 3), maximum
  **6 meV at the zone boundary** (H = 2.5).
- **Γ** constant and modest (underdamped at mid-zone, e.g. ~0.5 meV); amplitudes and resolution
  constant; **T** a representative fixed value.

Property worth noting: walking this dispersion **sweeps easy → hard automatically**. Mid-zone
(H ≈ 2.5) is the happy path — ω ≫ Γ, well clear of elastic. Approaching either endpoint, ω → 0 drives
into both the **Stokes–elastic merge** and the **overdamped boundary** — exactly what §4.1 handles. The
**exact endpoints (H = 2.00, 3.00) are zone-center/Bragg positions** where ω₀ = 0 (phonon buried in the
elastic line, unmeasurable, and the generator's guard region), so the measurable dispersion is the
interior; the near-endpoint points are the boundary cells.

---

## 6. Fitting setup — cost function is a first-class, pluggable choice

The fit layer must run any of the three estimators on the same data, because both validation (1a) and
demonstration (1b) depend on swapping them cleanly. It also varies (Ω_d, Γ) per §4.1.

- **`curve_fit` cannot carry all three.** Hardwired least-squares: `sigma` is a fixed array scaling
  squared residuals, so it natively does **Neyman** (σ = √data). **Pearson** needs iteration (fit,
  recompute σ² = f at the best fit, refit until stable). **Baker–Cousins** needs a likelihood-capable
  minimizer — `scipy.optimize.minimize` on −2 ln L, `lmfit` with a custom objective, or `iminuit`
  (built for binned-likelihood cost functions, with MINOS for correct asymmetric parameter errors).
  **Decided default: Baker–Cousins** (§13.1), so the backend leaves `curve_fit`.
- **Parameter errors split from plotted error bars under likelihood.** χ² hands you a symmetric ±σ per
  point that doubles as the error bar you draw. The likelihood does not — the true low-count
  uncertainty is asymmetric. Fit-parameter errors come from the likelihood profile (Δ(−2 ln L) = 1) and
  come out correctly asymmetric; the error bar *drawn* on a 1-count point becomes a separate,
  approximate object (Garwood / Feldman–Cousins, or the √(N+1) hack). Keep these distinct in code and
  figures.
- **`sigma`-convention check (carry over from Errors-in-Fitting).** The demo passes `sigma = 1/error`
  to `curve_fit`, inverting the weighting (up-weights the high-count peak channels) and, with
  `absolute_sigma=True`, distorting the covariance. The Neyman path here passes `sigma =` the per-point
  standard deviation with `absolute_sigma=True`. **Action: confirm the production
  `FeSeS_1_Analysis.py` call does not inherit the same flip** — if it does, that affects every reported
  error in the existing results.
- Initial guesses from perturbed truth or simple data heuristics (argmax for the peak). The happy path
  should be insensitive to reasonable IC choices; IC *sensitivity* is a boundary test (§10).

---

## 7. Scoring & acceptance (settled tolerances)

For a fixed ground-truth set, run **M = 1000 noise realizations** (distinct seeds) per cell, fit each,
and accumulate:

- **Bias:** ⟨p_fit⟩ − p_true for each p (focus ω₀, Γ).
- **Coverage / pull:** (p_fit − p_true)/σ_p, σ_p the fitter's reported error.
- **Fit success rate:** fraction converging cleanly.

The **pull distribution is the single cleanest diagnostic**: its mean tests bias, its width tests
whether the reported errors are honest. 1k realizations resolve bias to ~0.03σ and pull-width to ~2%
— ample for the gate; the demonstration figures (§8) may use more for smoothness.

**Happy-path pass criteria** (under the production estimator, mid-zone cells):
1. Fit success rate ≈ 100 %.
2. **ω₀ and Γ recovered within 0.1 meV** of truth — the *accuracy* target in the well-counted regime
   (where σ < 0.1 meV). At low counts σ can exceed 0.1 meV, and there the governing criterion is the
   **pull (within ~1σ)**, not the absolute number — otherwise we would penalize a fit that is doing
   everything right against Poisson noise. Γ is the harder of the two (wings, correlated with
   resolution and amplitude).
3. Pull for ω₀ and Γ consistent with unit-normal (mean ≈ 0, std ≈ 1).

Pull-width > 1 means the fitter is **overconfident** — a result we must know before any confidence
score (`DESIGN.md` §12) rides on those errors.

**The sweep is the dispersion itself** (§5.4): the ω axis is physical (H along the curve), crossed
with target counts and, optionally, Γ. Report the pull/bias/success-rate surface over (H, counts). The
near-zone-center cells are where the surface is expected to degrade (§10).

### 7.1 Capstone — recovered dispersion vs truth
Overlay the **recovered ω(H) ± σ on the true 6·|sin(π(H−2))| curve.** One figure that validates the
whole core end-to-end (fit → extract → assemble-by-Q) against a known dispersion, and doubles as a
demonstration artifact (§8). The mid-zone points should land on the curve within error; the
near-endpoint points show where/how recovery degrades.

---

## 8. Demonstration framework — the three-estimator comparison

Deliverable 1b made concrete: the same generate→fit→score engine, run under all three estimators,
producing artifacts that *show* what naive assumptions cost. The choices here are about persuasion.

**Layered, undeniable → consequential:**

- **Layer 1 — the constant-rate identity (analytic).** Fit a flat rate μ to N Poisson channels under
  all three estimators; recover harmonic mean (Neyman), RMS (Pearson), arithmetic mean (likelihood),
  landing exactly where the algebra in §5.3 says. No model complexity — the result is a theorem, the
  opener a skeptic cannot argue with. It establishes which way each method leans before any DHO.
- **Layer 2 — the DHO case in physics terms.** Carry the comparison into the real model and show the
  bias propagate into **Γ** (linewidth) and the **Stokes/anti-Stokes amplitude ratio** at realistic
  low counts. Framing the impact as *the physics quantities we publish* is what makes it land with
  physicists rather than statisticians.

**Honesty is the persuasion strategy.** Report the **full** bias/pull surface vs counts, *including*
the high-count regime where all three agree. Showing the naive method is not *always* wrong — that it
fails specifically and predictably at low counts — makes the claim credible rather than polemical.

**Successor to Errors-in-Fitting.** This generalizes that program from a single Gaussian to the real
DHO⊗Voigt model, and from "covariance vs empirical fit-error spread" to "three estimators vs known
truth." Its figures — the constant-rate straddle, the Γ-bias-vs-counts curves, the pull histograms, the
recovered-dispersion overlay (§7.1) — are reusable directly in methods sections and talks. Durable
outputs, not throwaway diagnostics.

*(A methods-credibility goal, somewhat separate from ISAR's live-triage purpose, but cheap because it
rides on the validation engine. Explicit from the start so the estimator choice is never quietly
hardcoded — which would forfeit the demonstration.)*

---

## 9. Module layout & outputs

```
isar/
  synth/
    generate.py     # build noisy scans from convolute_voigt_dho + manifest; seeded
    dispersion.py   # acoustic ω(H)=6|sin(π(H-2))|; emit the (2,0,0)->(3,0,0) Q-series
    noise.py        # counts scaling + Poisson draw
    estimators.py   # Neyman / Pearson / Baker-Cousins cost fns + param-error extraction
    fit.py          # (Ω_d, Γ) reparameterized fit; chosen estimator; backend (minimize/iminuit)
    score.py        # Monte-Carlo recovery: bias, pull, coverage, success rate; (H,counts) surface
    demo.py         # the §8 three-estimator comparison (constant-rate + DHO layers)
    config.py       # dispersion, grid, Γ, T, counts, M, sweep axes, estimator set
  Fitting_Functions.py   # the production library, imported unchanged (do not fork)
  out/
    manifests/      # per-scan ground truth (JSON)
    figures/        # pull histograms, (H,counts) surfaces, estimator comparison, dispersion overlay
    summary.csv     # per-cell bias/pull-width/success-rate, per estimator
```

Run (cmd):

```
python -m isar.synth.score      :: validation gate (+ §7.1 dispersion overlay)
python -m isar.synth.demo       :: three-estimator demonstration figures
```

Green happy path: flat ~zero bias, ~unit pull width, ~100 % success across the mid-zone cells, and the
recovered dispersion sitting on the true sine within error.

---

## 10. Boundary sweeps (after the happy path passes)

Reached by walking the same dispersion outward and dialing counts down — no separate machinery:

- **Overdamped / merge boundary:** the near-zone-center points, where ω → 0. §4.1's reparameterization
  is what keeps the fit well-defined here; this sweep is the proof it works (and the test of whether the
  shared-library fix is warranted). **Characterized, not deferred** — the test dispersion drives into
  it by design.
- **Stokes–elastic merge:** same region, viewed as the phonon sliding under the elastic line.
- **Anti-Stokes on/off:** sweep T from low (anti-Stokes negligible) to high (k_B T ≳ ℏω₀, comparable) —
  tests the Bose-factor handling and feeds a Layer-2 demonstration cell (the anti-Stokes is low-count).
- **Low counts:** drop target peak counts until fits fail — where the estimator choice (§5.3) bites
  hardest and IC sensitivity appears.

### 10.1 Phase 1b — break the inverse crime
Generate with a resolution slightly off from what the fitter assumes (the real case: resolution measured
with error), and/or inject a faint unmodeled second mode. Watch the **Γ bias** — resolution error feeds
directly into the reported linewidth (`DESIGN.md` §8). This is the test that estimates real-data
behavior, as opposed to behavior on the fitter's own model.

---

## 11. Phase 1c — peak-count model selection

Once single-phonon recovery passes, the high-value test targets the hardest deterministic decision
(`DESIGN.md` §7.5): **how many peaks.** Generate two-peak data — *elastic + Stokes* versus *Stokes +
anti-Stokes* at a T where both are visible — and check whether **AIC/BIC (or a nested F-test)** selects
the true count, **never raw χ²** (which always favors more peaks). The true count is known by
construction. AIC/BIC are likelihood-based, lining up with the Poisson likelihood in §6 — model
selection and fit cost then come from the same statistic.

---

## 12. Explicit non-goals

- No orchestration, LLM, or parser — the middle of the pipeline only.
- No resolution measurement — known input here.
- No energy-zero fit — `center = 0`, fixed.
- No real-data robustness chase in Phase 1 proper — that is Phase 1b, kept separate on purpose.

---

## 13. Decisions (settled) and remaining

**Settled:**
1. **Cost function / backend:** **Baker–Cousins (Poisson likelihood)** as production default, for
   honest low-count behavior on Γ and the anti-Stokes; backend leaves `curve_fit` for
   `minimize`/`lmfit`/`iminuit`; parameter errors from the likelihood profile. Demonstration (§8) still
   runs all three.
2. **M:** 1000 per cell to start; tune later (more for the §8 figures than the gate).
3. **Tolerance:** 0.1 meV on ω₀ and Γ in the well-counted regime; pull (~1σ) governs at low counts (§7).
4. **Overdamping:** reparameterize to (Ω_d, Γ) — boundary unreachable, no penalty, no hyperparameter
   (§4.1); log-barrier held in reserve; shared-library fix deferred until Phase 1 confirms.
5. **Grid/dispersion:** (2,0,0)→(3,0,0), ΔH = 0.05, ΔE = 0.25 meV, ω₀(H) = 6|sin(π(H−2))| meV (§5.4).

**Remaining:**
6. **Drawn error-bar convention** for low-count channels (Garwood / Feldman–Cousins / √(N+1)) for
   *display*, now decoupled from the fit uncertainty (§6). Cosmetic; pick before the §8 figures.
7. **Representative T and constant Γ value** for the happy path (sets anti-Stokes strength and the
   underdamped margin) — minor, choose at build.
8. **ε floor on Ω_d** and the energy-window width — small numerical choices, fix at build.

# ISAR Phase 1 — Handoff for the build session

This briefs a fresh Claude Code session (full repo access, can run Python) to build Phase 1.
Keep it short; the spec lives in the design docs, which are authoritative.

## Read first (authoritative)
- `docs/archive/PHASE_1_DESIGN.md` — the spec for this build. Follow it. §13 lists the settled decisions.
- `DESIGN.md` — master design, for context on where Phase 1 sits (this realizes §11 / §14 step 1).

## Reference code (read; do not modify in Phase 1)
- `Fitting_Functions.py` (this repo, copied from production) — **import unchanged.** The lineshapes,
  the DHO, the Voigt⊗DHO convolution. This is the code under test.
- Production reference for how the real fit is wired (read for structure, do not copy wholesale):
  `C:\Users\AMM\Documents\Github\2025 APS S30 FeSeS FMO Data Processing\FeSeS_1\Analysis\FeSeS_1_Analysis.py`
  — see `fit_scan()` for the IC structure, the `has_elastic` / `has_antistokes` gating, the bounds, and
  the (Neyman) sigma convention.

## Task
Build the standalone `synth/` module that generates synthetic IXS scans from the production model,
fits them back, and scores recovery against known truth. No orchestration, no parsing, no LLM.

## Build order (pause after step 1 for a human sanity-check of the synthetic scans)
1. `dispersion.py` + `generate.py` + `noise.py` — emit the 21-scan (2,0,0)→(3,0,0) Q-series with
   per-scan ground-truth manifests; plot a few spectra and eyeball them before fitting.
2. `estimators.py` + `fit.py` — the (Ω_d, Γ) reparameterized fit under Baker–Cousins first; then add
   Neyman and Pearson for the comparison.
3. `score.py` — M=1k Monte Carlo, the (H, counts) pull/bias/success surface, and the §7.1 recovered-
   dispersion-vs-sine overlay. This is the validation gate.
4. `demo.py` — the §8 three-estimator demonstration (constant-rate layer, then DHO layer).

## Load-bearing constraints (don't re-litigate these — they're settled in §13)
- **Import `Fitting_Functions.py` unchanged.** Do the (Ω_d, Γ) reparameterization in the **fit layer**:
  vary (Ω_d ≥ ε, Γ), convert ω₀ = √(Ω_d²+Γ²), call the production model. The guard then never fires.
  Do **not** edit the shared library in Phase 1.
- **`center = 0`, fixed** (INS absolute-axis case).
- **Resolution and T fixed at the true values** during the fit (resolution degenerate with Γ if freed).
- **Baker–Cousins is the production default.** The minimizer backend (`scipy.optimize.minimize` /
  `lmfit` / `iminuit`) is an **empirical** choice — pick by running, and report which converges
  reliably on the reparameterized DHO and gives usable profile errors (Δ(−2lnL)=1, asymmetric).
- **Estimators are pluggable** (Neyman / Pearson / Baker–Cousins) — the demonstration needs all three
  on the same data.
- **Test dispersion:** ω₀(H) = 6·|sin(π(H−2))| meV, H = 2.00–3.00 step 0.05, energy step 0.25 meV,
  window ≈ ±10 meV. Endpoints (H=2.00, 3.00) are zone-center/Bragg positions (ω₀=0, unmeasurable) —
  sample the interior; near-endpoint points are the boundary cells.

## Pass criteria (the deliverable is a number, hence run-and-check)
- Mid-zone cells: fit success ≈ 100 %; ω₀ and Γ within 0.1 meV in the well-counted regime; pull for ω₀
  and Γ ~ unit-normal (mean ≈ 0, std ≈ 1). Pull-width > 1 = overconfident, must be surfaced.
- At low counts the governing criterion is the pull (~1σ), not the 0.1 meV absolute number.
- Capstone: recovered ω(H) ± σ sits on the true sine within error across the mid-zone.

## Settled findings carried in from the design session (no action needed, context only)
- **Production `sigma` convention is correct** (σ = √counts, `absolute_sigma=True`); the inverted-weight
  flip was demo-only. Resolved.
- **Production uses Neyman χ²** (σ = √data + count-floor). This is exactly what the §8 demonstration
  targets. Once the harness works, a high-value follow-up is to re-fit representative *real* FeSeS scans
  under Baker–Cousins and quantify whether the existing Γ values carry low-count bias. (Follow-up, not
  Phase 1 proper.)
- **Overdamped penalty is reachable in production** (bounds don't enforce ω > Γ). The §4.1
  reparameterization fixes it; pushing it into the shared library later benefits production too.

## The one open reasoning thread (do NOT barrel through)
Whether/when to push the peak-position (Ω_d) parameterization into the shared `Fitting_Functions.py`
and retire the `1e6` guard. **Deferred until Phase 1 confirms the behavior.** Do not fork or rewrite the
shared library preemptively — keep the reparameterization in the Phase 1 fit layer, and raise the
library change as a separate proposal once the synthetic results justify it.

## Minor build-time choices (pick sensible defaults, note them)
- Display error-bar convention for low-count channels (Garwood / Feldman–Cousins / √(N+1)) — decoupled
  from the fit uncertainty.
- Representative T and constant Γ for the happy path (sets anti-Stokes strength and underdamped margin).
- ε floor on Ω_d and the exact energy-window width.

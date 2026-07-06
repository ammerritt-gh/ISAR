# σ-calibration (phonon_dft, TAVI deterministic engine)

First-pass closed-loop Phase-0 calibration. Reproduce with `python docs/sigma_calibration_campaign.py`.
Consumed by `isar.drive.acquisition.calibration.load_calibration("calibration/phonon_dft_deterministic.json")`.

- date: 2026-07-06   ISAR f1228d8   TAVI 1937fe6a
- sample: Al_phonon_DFT   truth: `isar.analyze.truth.phonon_dft` (E = 6·sin(π·q/2) meV, q = H−2), T = 200 K
- TAVI scans submitted/delivered: 99/99
  (TAVI-failed: 0)
- ISAR fits delivered/abstained: 46/53
  (of 99 scans; only the 46 delivered rows with a truth match are scored)
- H grid: [2.03, 2.06, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9]   ncounts: ['1e+07', '3e+07', '1e+08']   seeds: [101, 202, 303]
- structure pinned to `stokes+bg` (the sample has no elastic line: `PhononSQW.elastic()` returns []),
  ω₀ prior = expected dispersion, σ = 0.5 meV — see `docs/sigma_calibration_campaign.py` for why.

## Headline

- **68% interval coverage: 0.022** (target 0.68). The bars are drastically
  overtight — nearly every truth lands OUTSIDE ISAR's reported 68% interval. Coverage measured
  honestly is a RESULT, not a failure.
- **Global inflation factor k = 14.265** (68th pct of |Δ|/halfwidth). This is a
  CRUDE summary — see the tier table: k is unstable across count rate because the reported interval is
  statistical-only and the dominant error here is *systematic model mismatch*, which does not shrink
  with counts. At 1e8 n/pt the statistical half-width is ~1e-3 meV while |Δ| is 0.01–0.5 meV, so no
  single finite k re-calibrates all tiers. Treat k as an order-of-magnitude floor (~10×), not a
  precise multiplier.
- **Center pull (mean signed Δ = ω₀ − truth): +0.117 meV.** A SEPARATE
  defect from bar optimism: a systematic shift of the fitted center that inflation CANNOT fix
  (widening a bar does not re-center it). Reported separately by design.

## What ISAR did with the 99 scans (delivery is honest triage, not 100% coverage)

TAVI delivered all 99 scans; ISAR delivered a fit on 46 and
abstained on 53. Abstention is ISAR being conservative, not broken:

dispositions: {'resolved': 40, 'reject': 34, 'flagged': 6, 'abstain': 19}

| n | first abstention reason |
|---|---|
| 31 | no admissible structure converged |
| 19 | prior expects a phonon below the detection threshold at the achieved statistics |
| 1 | sparse regime with a pathologically wide interval |
| 1 | cost/dof=11.3, structureless residual |
| 1 | cost/dof=8.7, structureless residual |

- **"no admissible structure converged"** dominates: at high SNR the DHO×resolution model does not
  perfectly match TAVI's analytic S(Q,ω)×resolution, so cost/dof ≈ 2–3 and the optimizer/MINOS flags
  non-convergence — ISAR declines to report an (overtight, slightly wrong) number rather than deliver
  one. This is model-mismatch honesty, and it is independent of the structure pin (both `stokes+bg`
  and `elastic+stokes+bg` fail to converge on the same scans).
- **"prior expects a phonon below the detection threshold"** is the low-count (1e7) detection-power
  abstention — the scan genuinely lacked the statistics to see the phonon. Honest and expected.

## Coverage by ncount tier

| ncount/pt | n | coverage68 | inflation k | center pull |
|---|---|---|---|---|
| 1e+07 | 8 | 0.000 | 26.823 | -0.068 |
| 3e+07 | 17 | 0.059 | 9.764 | +0.176 |
| 1e+08 | 21 | 0.000 | 13.313 | +0.140 |

Coverage is ~0 in every tier; k is noisy (small delivered-n per tier, dominated by the high-H
upward-pull scans). The tier factors are surfaced as `buckets` in the artifact so the driver *can*
pick a per-tier k, but the honest reading is "the statistical bar omits systematic mismatch".

## Center pull vs H — the center IS pulled systematically UP at high E

At the known anchor Q=(2.15,0,0): ISAR ω₀ = 1.494 vs truth 1.400 — pulled UP, outside the interval.
This campaign reproduces that direction: Δ is ~0 across the mid-zone (H = 2.1–2.6, |Δ| ≲ 0.03 meV) but
climbs steeply toward the zone boundary (**+0.18 / +0.44 / +0.50 meV at H = 2.7 / 2.8 / 2.9**), where
the acoustic branch (5.3–5.9 meV) nears the optic branch (≈8 meV) and the DHO lineshape asymmetry
under the Bose factor pulls the center up. The two deepest near-Γ probes pull the other way (Δ ≈ −0.23
/ −0.14 at H = 2.03 / 2.06): with E below the 1.37 meV resolution the phonon merges with the elastic
line and the center is pulled toward zero.

| H | E_true (meV) | n | mean Δ (ω₀−truth) |
|---|---|---|---|
| 2.03 | 0.28 | 3 | -0.230 |
| 2.06 | 0.56 | 2 | -0.140 |
| 2.1 | 0.94 | 8 | +0.002 |
| 2.2 | 1.85 | 4 | +0.007 |
| 2.3 | 2.72 | 2 | -0.005 |
| 2.4 | 3.53 | 2 | +0.003 |
| 2.5 | 4.24 | 5 | +0.013 |
| 2.6 | 4.85 | 4 | +0.030 |
| 2.7 | 5.35 | 5 | +0.183 |
| 2.8 | 5.71 | 5 | +0.441 |
| 2.9 | 5.93 | 6 | +0.503 |

## Near-Γ behavior

18 near-Γ probe scans (H ∈ [2.03, 2.06], E ≲ resolution 1.37 meV). Most abstain honestly
(unconstrained / underpowered); the few that deliver show the downward center pull noted above. This
is informative, not a bug.

## TAVI-failed scans

0 of 99 scans did not reach TAVI state `done` — none.

## Interpretation (triage framing)

Two independent defects, measured separately:
1. **Bar optimism** (coverage ≪ 0.68): the reported 68% interval is statistical-only and ignores the
   dominant systematic (model mismatch). A global inflation k ≈ 10× is a crude floor; because the
   systematic does not scale with counts, no single k is stable across count rate — the honest driver
   should carry the *larger* of (k·statistical-halfwidth) and a systematic floor (~the median |Δ|).
2. **Center pull** (mean signed Δ > 0, rising to +0.5 meV near the zone boundary): a systematic shift
   inflation cannot fix. The driver should treat |Δ|(H) as a floor on achievable accuracy.

This is a Phase-0 SEED measured on the deterministic engine at one sample; it is not a claim about
real-instrument coverage. The load-bearing artifact for the driver is the inflation/coverage/center-
pull numbers + per-tier buckets in `phonon_dft_deterministic.json`.

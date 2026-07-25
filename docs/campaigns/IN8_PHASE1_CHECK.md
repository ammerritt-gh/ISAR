# Phase 1 core check against real IN8 (TAS) data — handoff to a Claude Code session

> **Lifecycle:** Historical evidence
> **Provenance:** hand-written
> **Verified against code revision:** `d0ea1df` (2026-07-25)
> **Superseded by:** the current contract named in [`campaigns/README.md`](README.md). A dated record of what happened, never a statement of current behaviour (DECISIONS D3).

**Purpose.** Empirically check whether the Phase 1 fitter (`isar.synth.fit.fit`, DHO ⊗ 1-D Voigt,
Baker–Cousins) survives contact with **real, non-IXS** data — the IN8 numors in
`test_data/ILL IN8/`. This is the first stress-test of the synthetic core on a different instrument
class, and it doubles as a concrete messy-data example for `archive/PHASE_3_DESIGN.md`. Run this in Claude Code
(code + data + deps are all in-repo); this note only captures what was already read so it isn't re-derived.

---

## Outcome (executed 2026-06-29 — `docs/in8_phase1_check.py`, outputs in `out/in8_phase1/`)

**The core recovers ω₀ on real TAS data — the pass condition is met.** The check was run as a
standalone script that reuses the *production* parsers (`isar.parsers.ill_in8`, `isar.parsers.spec_s30`),
the fitter (`isar.synth.fit`), the collation merge (`isar.reduce.collate`), and the lineshape library
(`isar.synth._ff`). No throwaway reader was needed (the Phase-2 parser superseded that plan). Outputs:
`results_in8.csv`, `results_in8_merged.csv`, `results_s30.csv`, `figures/`, `FINDINGS.md`. Suite stays
green (268 tests).

Headline numbers:
- **055182 (clean anchor): ω₀ = 4.47 ± 0.03 meV, converged** — matches the hand-vetted ~4.2 meV phonon.
- **6 of 17 `bs qh` scans pass `assess_success`** (with the corrected resolution, below). On TAS the
  bottleneck is **convergence itself**, not error quality: MINOS is produced only for the converged scans.
- **S30 IXS positive control: 6/6 converge**, cost/dof ~1–4, *meaningful* Γ, and a clean ω₀(K) dispersion
  (~5→7 meV). Same core, validated instrument — the contrast is the product.

Three things we learned that change Phase 3 (all folded into `archive/PHASE_3_DESIGN.md` §3/§5/§6/§8):

1. **Resolution is now Cooper–Nathans from the header, not a guess.** The initial 0.25 meV guess was too
   narrow; CN (ported from ResCal `rc_cnmat` into the check; `cn_resolution_matrix`/`cn_energy_fwhm`)
   gives the elastic energy FWHM **≈0.58 meV** for this config (≈Q-independent at the elastic line, ~0.70
   at the phonon), corroborated by the cot(θ_M)·collimation estimate (~0.55). This scalar CN energy
   resolution is the precursor to the §8 4-D engine and a candidate for `isar/synth/resolution`.
2. **The broad low-E tail is NOT resolution — it is real quasi-elastic / diffuse scattering.** Sweeping
   the assumed width shows cost/dof only bottoms out (~7 on 055182) near ~1.5 meV, far broader than any
   plausible instrument resolution; a CN-width (or narrower) elastic line can at most spike the single
   nearest point (see the 055182 overlay) and leaves the tail in the residuals (cost/dof 121→21 once the
   resolution is corrected). Fe₂Mo₃O₈ is magnetic, so magnetic quasi-elastic scattering is the likely
   source. **Phase 3's TAS structure basis needs a quasi-elastic/background component**, distinct from the
   sharp elastic Voigt and the flat BG. ω₀ is robust to all of this.
3. **The "large elastic peak" the fit reports is a degenerate dummy.** With the elastic line off-window
   (FX=2, EN≥1, elastic at 0), the elastic NNLS column has no in-window leverage, so the fit hands it an
   arbitrary huge amplitude that contributes ~0 — it is *not* modeling the tail. (§3 IC note.)

Secondary findings: per-scan repeats reproduce ω₀ where the feature is strong (np=25 Q=(2,0,4):
055289/055294 agree to ~0.08 meV) and scatter 1–2 meV where sparse/weak (np=12 pairs) — the
detection-power signal for §6. Collation merges same-(sample,Q,T) spectra but regulation-T drift splits
nominal repeats across T-bins and different E-start windows combine into wider spectra (see
`results_in8_merged.csv`). `055189` (`bs a3p`) crashes the Q-token parse and is skipped; `055183` is a
3-point aborted scan. No vanadium/incoherent-elastic scan exists in the set, so Γ stays throwaway on TAS
regardless of resolution.

> The sections below are the original handoff/plan, kept for context. Where they propose a throwaway
> reader or a nominal resolution, see the Outcome above for what was actually done.

## What the fitter needs vs. what a numor gives
`fit(x, counts, *, gauss_fwhm, lorentz_fwhm, T, center=0.0, estimator="baker_cousins",
backend="iminuit", minos=True, ic_mode="grid")` wants an energy array (meV), integer counts, a Voigt
resolution (Gaussian + Lorentzian FWHM), T, and the energy-zero center.

**IN8 new-ASCII numor facts (read from 055181/055182/055289/055179):**
- Header lines `INSTR/EXPNO/FILE_/DATE_/TITLE/TYPE_`, then `COMND:` (the scan command — *is* the
  classification), `POSQE:` (Q + EN start), `STEPS:`, several `PARAM:` lines, `VARIA:`/`ZEROS:` motor
  blocks, a second `PARAM: TT=.. RT=.. MAG=..` (environment), then `FORMT:`, `DATA_:`, an explicit
  **column-header line**, and the data table.
- **`FX=2` → fixed-kf → the `EN` column is an absolute energy-transfer axis; `center=0.0` is fixed,
  no zero to fit.** (`KFIX=2.66202` in these files.) This is the IN8 vs HERIX difference in practice.
- Data columns vary per scan — **parse the header line, don't hardcode**. For these energy scans:
  `PNT QH QK QL EN M1 M2 TIME CNTS A2 A3 ... TT TRT [MAG]`.
- **`M1` = controlling monitor** (the `mn NNNN` preset, ≈constant); **`CNTS` = detector counts (the
  signal)**; `TIME` = seconds; `M2` = secondary monitor. Intensity = `CNTS`, σ = √`CNTS`; normalize by
  `M1` (constant within a scan, so it only matters for cross-scan comparison).
- Classification: `COMND: bs qh …` = measurement (constant-Q energy scan); `COMND: sc a3 …` /
  `sc a3p …` = alignment (skip for fitting).
- **Temperature is ambiguous on purpose:** header `PARAM: TT` (sample sensor) and the per-point `TT`
  column can disagree with `RT`/`TRT` (regulation). In 055181 header `TT=281.74` but the data-column
  `TT≈298.77`, `RT=300`. For the fit, T≈290–300 K is fine; just don't trust a single field blindly
  (this is the Phase 2 §7.2 `sensor_divergence` case, live).

## The three scans to use (already inspected)
| Numor | COMND | Q, T | Window (meV) | What it is | Role |
|---|---|---|---|---|---|
| **055182** | `bs qh 4 -0.4 0 1 … np 51 mn 50000` | (4,−0.4,0), ~290 K | EN 1.0→13 (0.5 step) | **Clean test.** Clear phonon peak at **EN≈4.2 meV** (CNTS 1286/1314 at pts 7/8) on a falling elastic/quasielastic tail from EN=1. **Truncated: 25 of 51 pts; pt 25 has `M1=31745`≠50000 → last point partial.** | primary: does the core run + recover ω₀≈4.2? |
| **055181** | `bs qh 4 -0.2 0 1 … np 41 mn 100000` | (4,−0.2,0), ~282 K | EN 1.0→21 (0.5 step) | **Messy.** Strong falling feature 1–3 meV, minimum ~9 meV, broad **rising optical feature 15–20 meV** (likely clipped at the high edge). | single-DHO won't fit both → Phase 3 structure-enumeration motivation |
| **055289** | `bs qh 2 0 4 3 … np 25 mn 750000` | (2,0,4), **~55 K** | EN 3→9 (0.25 step) | **Low-T, weak.** Mostly a falling tail with a weak bump ~5.5–6 meV. | tests detailed-balance gate (low T strongly suppresses anti-Stokes) |

## Model-mapping notes (why 055182 is the clean case)
- Phase 1 model = elastic Voigt at `center` + DHO Stokes(+anti-Stokes) + flat BG. For 055182 the
  **elastic-at-0 tail** explains the low-E falling part and a **DHO at ω₀≈4.2** explains the peak — a near
  one-to-one fit to the model, except the elastic peak itself is **off-window** (EN starts at 1.0), so
  `elastic_amplitude` is only constrained by its tail. Watch the grid IC / NNLS: with center=0 outside
  the window and a strong low-edge rise, it may load the elastic component heavily — informative either way.
- **Anti-Stokes (−ω) is entirely off-window** (EN ≥ 1, one-sided energy-loss scan), so it doesn't
  constrain the fit even though at 290 K / 4.2 meV the Bose factor is large (~0.85). Fine — just note it.

## Resolution — the load-bearing caveat
There is **no in-window elastic line in 055182** to measure the resolution from. For a first "does it run
and recover ω" pass, use a **nominal IN8 thermal energy resolution** (≈1 meV FWHM at this kf is a
reasonable placeholder; small/zero Lorentzian). Better, if time: pin it from a measured vanadium /
incoherent elastic scan if one exists among the numors, or compute Cooper–Nathans from the header
(`DM, DA, KFIX, ALF1-4, BET1-4, ETAM, ETAA`).

**State this loudly in any result:** ω₀ (peak position) is robust to the resolution width and to the
1-D-vs-4-D issue, so a recovered ω₀ is meaningful. **Γ is not** — on a TAS the 1-D Voigt convolution
omits the 4-D resolution-ellipsoid focusing effect (`DESIGN.md` §8, `archive/PHASE_3_DESIGN.md` §8), so any Γ
from this fit is **resolution-contaminated, not the intrinsic linewidth**. This check validates "the core
recovers ω on real TAS data"; it does **not** validate Γ on TAS. That is exactly the "ω trustworthy
before Γ on TAS" point — this spike is its first concrete evidence.

## The experiment to run
1. Write a small energy-scan numor reader (parse `COMND`/`FX`/`KFIX`/`TT`, then the `DATA_` table by its
   column header) → returns `EN`, `CNTS`, `M1`, T. Keep it throwaway; the real parser is Phase 2.
2. Honor the truncation in 055182 (use the 25 real points; optionally drop or down-weight the partial
   pt 25 with `M1=31745`).
3. Call `fit(EN, CNTS, gauss_fwhm=~1.0, lorentz_fwhm=~0.0, T=290, center=0.0, minos=True)` on 055182.
   Report ω₀ ± (MINOS) and overlay model+residual on the data.
4. Repeat on 055181 (expect the single DHO to fail to capture both features — note where it lands) and
   055289 (low-T weak; expect a marginal/underpowered result).
5. Findings to capture for Phase 3: did the grid IC seed sensibly without a dispersion prior? did
   center=0-off-window break the elastic handling? did MINOS vs HESSE diverge (the sparse-data signal)?
   These feed `archive/PHASE_3_DESIGN.md` §3 (IC), §5 (errors), §6 (verdict buckets).

## Plumbing reminders
- `isar/synth/_ff.py` adds the repo root to `sys.path` and imports `Fitting_Functions.py` unchanged —
  so run from the repo root (or editable install) and the path resolves.
- Deps already in `requirements.txt`: numpy, scipy, iminuit, lmfit, matplotlib.
- Default `fit()` backend is iminuit with MINOS; `ic_mode="grid"` (grid + NNLS) is the robust IC.

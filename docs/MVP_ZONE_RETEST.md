# MVP zone re-test — Phase 2–5 on the two trouble zones (2026-07-08)

Re-run of the Stage-5 campaign (`docs/MVP_FIRST_RUN.md`) on branch `model-free-foundation`
(Phases 2–5: low-q soft-prior branch + hard-fix, high-q Stokes-only + descriptor, EIG/Fisher
acquisition + VOI stop, durable branch threading). Same grid, same seed, LLM-free.

```
isar drive --spec objective.json --tier deterministic --workspace out/mvp_zone_test --seed 42
# objective: (2,0,0)->(3,0,0), step 0.05 (21 pts), tol 0.1 meV, 8h budget
isar score --workspace out/mvp_zone_test --truth-fn phonon_dft --bucket-by h --json score.json
```

Terminal `unmeetable`, 8/21 converged (baseline 11), sim-budget 20.6 s of 28800, no crash.

## Verdict: FAIL (regression vs the documented baseline)

| Criterion | Target | Result | |
|---|---|---|---|
| **P-Z1** low-q confident-wrong eliminated | none in H∈[2.0,2.15] | H=2.05 delivered ω₀ **6.578**, truth 0.471, **Δ+6.1**, hw 0.007 | ✗ |
| **P-Z2** high-q recovery {2.6,2.75,2.8} | ≥2 delivered, \|Δ\|<0.1 | only **2.6** (4.80 vs 4.85 ✓); 2.75→7.79, 2.8→7.15 stuck-wrong | ✗ (1/3) |
| **S1** interior preserved | median\|Δ\|≤0.05, ≥11 delivered | median **0.093** (baseline 0.020), **8** converged | ✗ |
| **S2** no new confident-wrong | ≤1 (baseline) | **4** genuine (H=2.05 Δ6.1, 2.55 Δ2.9, 2.85 Δ1.6, 2.5 Δ0.3) | ✗ |
| **S3** intended mechanisms fire | soft_prior/stokes_only/descriptor in the zones | **none fired**; only `branch_conditional` (on the wrong branch) | ✗ |
| **S4** clean terminate | terminal, no crash | `unmeetable`, fast, clean | ✓ |

Accurate deliveries (\|Δ\|<0.1): H=2.40, 2.60, 2.65, 2.70 — the mid-branch band where the acoustic
peak clears the elastic line and the second branch (below) has not yet captured certification.

## Root cause — a real second phonon branch, latched and self-reinforced

The `Al_phonon_DFT` deterministic sample (T=200 K) carries **≥2 branches**: the acoustic branch the
goal asks for (dominant; tracks truth `6·|sin(π(H−2)/2)|` — H=2.05 peak at E≈0 ~260 counts, H=2.10
Stokes/anti-Stokes at ±0.94), **and a weak second branch near ~6 meV** present across the zone
(H=2.10 shows a symmetric ±6.2 meV feature, ~15–22 counts — genuine, Bose-symmetric, not an
artifact). The truth function scores only the acoustic branch (the QoI).

At low/mid q the acoustic peak is buried under the elastic line, so the fitter escapes it and locks
the clean weak ~6 meV second branch. The **new acquisition widened low-q windows into that branch**
(baseline's narrower windows never reached it), and those confident wrong-branch fits — plain
`unconstrained resolved`, so they pass the clean-anchor gate — **seeded the branch model**:

```
branch "acoustic": w_min 4.80  w_max 7.79  n_anchors 13     ← the SECOND branch, mislabelled
dispersion_prior ω₀: 7.15 6.58 6.42 5.93 6.80 6.68 7.04 6.87 7.10 7.30 7.46 4.80 7.40 7.40 5.44 7.79
```

`branch_conditional` then hard-fixed Γ toward those anchors, reinforcing the wrong branch across the
whole low-mid-q range → the q-independent ~6.5→7.4 meV latch. This is
**misspecification-amplified-by-adaptivity** (Sloman) — the exact danger the plan's guardrails named.
Contract-4's self-reinforcement guard held (branch_conditional/descriptor never feed the branch) but
did not help: the *seeds* were confidently-wrong `unconstrained` fits, which are allowed to seed.

Why the Z1/Z2 rescues didn't engage: `soft_prior` and `stokes_only` never fired; the descriptor
(Phase 4a) catches *extra/unmodelled* signal, but here the model fits a **real** peak — the wrong
branch — so nothing reads as unmodelled. The gap is branch **selection**, which none of the built
mechanisms address. The correct acoustic fit existed as a `resolved` row at nearly every q
(H2.05→0.18, H2.1→**1.00**, H2.3→**2.75**, H2.35→3.08) but lost certification to the second branch.

## Takeaways for the next round

1. **Branch identity is the missing gate.** A confident `unconstrained` fit on the wrong branch can
   seed the "acoustic" branch model. Need a branch-assignment guard: reject/flag anchors that jump
   off the established dispersion (continuity vs neighbours), or require the seed to be the *dominant*
   peak, not a weak high-E shoulder.
2. **Wider low-q windows are double-edged.** The EIG/recenter widening exposed the second branch.
   The low-q window should stay anchored to the dominant (near-elastic) feature, or the fitter must
   prefer the dominant peak over a weak high shoulder.
3. **One genuine Z2 win** (H=2.6 recovered, baseline discarded as no_signal) shows the high-q
   detection path can work when the branch isn't hijacked.
4. Single seed 42, single run — the mechanism is structural (branch model + windows), so re-seeding
   won't change the picture, but a multi-seed sweep would quantify how often 2.6-style wins survive.

---

# Phase A — Bayesian branch assignment (2026-07-08, session 2)

Implemented the approved plan's Phase A (branch-assignment posterior + honest triage) and re-tested.
The single-branch assumption is replaced by a per-feature posterior `P(feature → {acoustic, optic,
new})` in which physics enters as *priors*, not switches (a decaying zone anchor pins acoustic→0 at Γ;
branch continuity in q; a new/spurious escape). New module `isar/session/branch_assignment.py`;
`session_prior`/`branch_model`/`driver`/`analyze`/`drive`/`truth` threaded for `branch_id`,
`assignment_entropy`, `resolvable`, and a `branch_continuation` abstain disposition. 782 not-slow +
14 new unit tests green.

## What Phase A fixed (from the unit + scored runs)

- **P-Z1 eliminated.** The low-q confident-wrong optic latch (H=2.05 → ω₀ 6.578) is gone across every
  run: the zone anchor rejects a 6 meV feature near Γ, so the optic can no longer seed "acoustic". Low
  q either abstains (`branch_continuation`) or the posterior tags the 6 meV feature `new`.
- Verdict 2-mode reconstruction bug fixed; master (pooled) fits now also go through triage (they
  previously bypassed it and could deliver the optic at the zone boundary).

## The deadlock this session chased down (acquisition, not identity)

After Phase A the re-test regressed to **0/21 delivered — "no signal" at every q**. Three distinct,
stacked causes, each fixed in `isar/drive/acquisition/acquire.py`:

1. **Cold-start window deadlock.** The scan window is centred on `e_pred = ω(q)` model prediction.
   Cold, `e_pred≈0` → window `[-2, +1.1]` meV. The mid-branch acoustic sits at 3.5–4.8 meV, *outside*
   the window → 0 in-window counts → abstain → never seeds → `e_pred` stays 0 forever. Earlier
   (pre-Phase-A) runs escaped only because the *latched* near-Γ delivery seeded a rising `ω(q)`; Phase
   A's honest near-Γ abstention removed that accidental bootstrap. **Fix:** a survey window — when the
   `ω(q)` prediction is uncertain (`σ_pred > FWHM`), extend the high side to scan the phonon band
   (`SURVEY_E_HI_MEV = 7`, which spans the acoustic band and clips the optic) so a dispersing feature
   lands in-window and seeds the model; the window tightens as the model localizes.

2. **Concave-dispersion undershoot.** Even once the model warms, a *linear* local fit through low-q
   anchors under-predicts the concave `6·sin(πu/2)` dispersion, so a tight window on the biased-low
   `e_pred` misses the real peak by a hair (H=2.5: window to +3.3, peak at +4.24). **Fix:** survey the
   band *until a q has a confirmed resolved fit* (`survey = not GridQ.best_resolved`), not merely until
   the model looks confident. Once resolved, the tight window returns.

3. **TAVI idempotency-cache poisoning (external, but exposed a real ISAR bug).** A prior over-large
   scan (594 pts > TAVI's 200 limit) poisoned TAVI's per-key result cache. ISAR's `Idempotency-Key`
   is `f"{objective.name}-{scan_counter}"`, and `objective.name` derives from the spec `notes` — both
   deterministic, so **every re-run replays the same keys** and TAVI returned the cached *truncated*
   (18-pt) results instead of running the wide survey window. Confirmed by a keyless curl running the
   full 50-pt window while ISAR got 18. **Workaround used:** a unique `notes` per run mints fresh keys.
   **Real fix (TODO):** the idempotency key must carry a per-run nonce (e.g. the workspace id) so a new
   campaign never collides with a prior run's cache — see [[tavi-scan-poisoning-api-block]].

## Status

Survey-window + survey-until-resolved fixes are in and unit-tested; with fresh idempotency keys the
wide windows submit and execute (verified: v10/v11 windows reach the survey band). **The full
end-to-end scored re-run is not yet confirmed** — the last campaign (v11, survey-until-resolved) was
interrupted before completion. Next session: run v11 to termination with a fresh `notes`, then
`score --truth-fn phonon_dft --bucket-by h` and check S1 (interior median |Δ|≤0.05), S2 (≤1
confident-wrong), and whether mid-branch now delivers now that the window reaches the acoustic.

# HANDOFF — Statistics-contract checkup (2026-07-19)

**What this is.** A post-MVP audit of how ISAR decides *how long to count, what a scan tells
us, and what to do next* — requested by Adrian before the next build phase. It is a findings
report: no code was changed. Each section states what exists (with file:line anchors verified
2026-07-19), what is missing, and concrete directions for fixing it. Status words follow
DESIGN §15 discipline: **Implemented / Partial / Planned** mean what they mean there.

**How to read it.** Five concerns, roughly in pipeline order: (1) counting statistics,
(2) cross-scan / cross-zone information sharing, (3) backgrounds, (4) scan placement,
(5) error-bar cost. A cross-cutting section at the end collects the themes that span all five.

**Overall verdict up front.** The statistical *core* is in better shape than the concern list
implied — Baker–Cousins everywhere, measured interval coverage, a fold-only prior gate, and an
EIG acquisition that already explains one of the observed symptoms (the "too wide" scans are
cold-start survey mode doing its designed job, not a bug). The genuine weak points are:
**backgrounds are a single flat constant** (the largest real-data risk), **counting targets are
absolute-meV only** (no signal-to-background awareness, so the 5-count-peak worry is real once
noise arrives), and **the error-tier choice is static** (the coverage evidence already justifies
a cheap-first/escalate-on-distrust policy, but nothing implements it).

---

## 1. Counting statistics — how long ISAR counts, and against what target

### What exists

- **Unit is monitor counts, not seconds.** All dwell decisions are in monitor counts; a fixed
  two-rung ladder `QUICKLOOK_NCOUNT = 1e7` → `PRODUCTION_NCOUNT = 1e8`
  (`isar/drive/supervisor/stopping.py:33-34`).
- **Dynamic ncount (Partial, DECISIONS O13).** When `SteeringPolicy.dynamic_ncount=True`
  (`isar/schema/hypotheses.py:98`), the requested exposure scales from the authoritative pooled
  interval toward the tolerance: `required = current × (halfwidth / (margin × tolerance))²`
  (`isar/drive/acquisition/acquire.py:192-194`), braked by a growth cap (default 4×/scan),
  ladder floors, instrument ceilings (§7.3 of MEASUREMENT_STEERING), and the budget.
- **Stopping is tolerance-driven.** A q-point converges when the certified pooled interval
  half-width ≤ tolerance (`stopping.py:228-241`); value-of-information saturation stops
  spending when the next scan would shrink σ(ω₀) by <1% (`isar/drive/acquisition/eig.py:29-34`),
  and an empirical interval-floor brake stops after 3 pooled updates with <5% improvement over
  ≥2× exposure growth (`stopping.py:262-295`).
- **Low-count fitting is honest.** Baker–Cousins on raw counts everywhere
  (`isar/synth/estimators.py:47-54`), σ=0 for zero counts (D3, `isar/schema/scan.py:22`),
  and verdict routing that names the sparse regimes: `underpowered` (3σ detection-power test,
  `isar/analyze/verdict.py:124-156`), `unconstrained`, `untrustworthy`.
- **Synthetic calibration already spans the regime.** The synth ensembles test peak amplitudes
  of 15/20/30/50/100/300 counts with the recorded judgment "~100 is good; 20–30 is the routine
  floor; below ~20 fits get very hard" (`isar/synth/config.py:101-116`).

### Gaps

1. **The counting target is absolute meV, blind to signal-to-background.** There is no way to
   ask for "peak ≥ N counts above background," "relative error < x%," or "detection power ≥ p."
   In analytic/no-background mode a 5-count peak can still localize ω₀ within tolerance, so the
   loop happily stops there — exactly the failure Adrian predicts once real background exists.
   The 3σ detection-power machinery (`verdict.py:124-156`) exists but is used only as a verdict
   gate, never as a *dwell requirement*.
2. **No observed-count-rate feedback.** `_count_request()` (`acquire.py:158-226`) never compares
   predicted to observed rates; if the sample scatters weaker than expected, the loop learns it
   only through interval non-shrinkage, several scans late.
3. **No minimum-count gate anywhere in acquisition** — ISAR will submit and fit scans whose
   expected peak counts are single-digit without flagging that the information simply is not
   purchasable at that dwell.
4. **Fisher-based dwell (per-second information `(∂λ/∂θ)²/λ`) is Planned**, not wired
   (MEASUREMENT_STEERING §4.1 item 4; `eig.py:16-19` has the 5b hook). The current 5a model is
   pure √N scaling, which assumes the parameter is identifiable — wrong exactly at low q where
   Γ/A merge.

### Directions

- **Make expected peak counts a first-class dwell constraint.** The forward model already
  predicts λ(E) per candidate scan; requiring `expected peak counts above predicted background
  ≥ N_min` (card-policy knob, default ~20–30 per the synth calibration) as a *floor* under the
  tolerance-driven formula is a small change to `_count_request()` and directly encodes the
  "5 counts won't cut it" instinct. Note this is a *floor*, not a target — the tolerance logic
  still owns the ceiling.
  **Owner clarification (2026-07-19):** this must stay a *per-request floor*, never a
  "count-until-peak-reaches-X" loop. ISAR runs scan-by-scan and re-decides after every scan;
  statistics accumulate through the pooled masters, so the right mechanism is: floor each
  submitted request, then let acquisition decide stay-vs-move on the pooled evidence. Grinding
  one q in a closed loop against a count target invites timing pathologies and forfeits
  ISAR's real advantage — e.g. when two phonons blend at one q, scanning *neighboring* q where
  they separate, then returning with tight neighbor priors, resolves the blend better than any
  amount of additional dwell at the original point. Breadth-with-priors is a first-class
  alternative to depth, and the EIG move-vs-stay logic is where that trade belongs.
- **Close the rate loop.** After each scan, compare observed integrated counts to predicted;
  carry a per-q (or global) rate-scale factor into the next `_count_request()`. Cheap, and it
  converts the ladder from open-loop to closed-loop.
- **Finish O13's planned remainder** (per-second Fisher information + OCBA/VOI
  keep-counting-vs-move) rather than inventing a parallel mechanism — the design is already
  written and cited in MEASUREMENT_STEERING §4.1 item 4 and §6 (Poisson OED: information linear
  in time).

---

## 2. Cross-scan / cross-zone tracking — what physics the prior actually carries

### What exists

- **The dispersion prior** lives in machine-owned `card.state.json`
  (`CardState.dispersion_prior`), updated through `isar/session/session_prior.py:add_point()`
  with the hard gate at lines 48-50: only `verdict=="resolved"`, non-fork, and (by default)
  fold-modality results enter. Consumption (`prior_for()`, lines 84-128) pools same-sample,
  same-T points (0.5 K tolerance) and fits a **1D linear polyfit on the dominant-varying Q
  component**, σ = residual std floored at **0.3 meV**.
- **Branch-aware layer (Implemented 2026-07-17/18).** `BranchBelief` regressions carry declared
  hypotheses as **priors on regression parameters** (gap, slope) — never pseudo-data
  (HYPOTHESES.md; the §4.5 anti-circularity rule). Branch assignment is a genuine posterior
  with reported entropy (`isar/session/branch_assignment.py`), and the O15 joint tracker walks
  a connected frontier.
- **Cross-q channels beyond ω₀:** Γ via the 1/ω branch-model law and A via provenance-tagged
  normalized intensity, both soft constraints (`isar/session/branch_model.py:52-56`); ω₀ enters
  the Bayesian sampler as a Normal prior (`isar/analyze/errors/bayes.py:151-159`).
- **The fit-vs-decide contract holds:** data cost and posterior cost are kept separate, so the
  prior never contaminates AIC selection (STATS_PRIMER §5; `fit_kind` provenance tags).

### Gaps

The Bayesian *skeleton* is right (soft priors, entropy-honest assignment, no pseudo-data). What
is missing is almost all of the *physics content* Adrian listed:

1. **No structure factors.** No S(Q), no form factor, no intensity-vs-zone expectation anywhere
   (scout sweep found zero hits). The acoustic 1/ω intensity law is the only intensity physics,
   and MEASUREMENT_STEERING §8 already flags it as structure-factor-blind.
2. **No polarization.** Longitudinal vs transverse does not exist as a concept in the code;
   HYPOTHESES' Tier-2 registry explicitly names "polarization" as a future classification
   feature, so the seam exists but nothing fills it.
3. **Brillouin-zone awareness is acoustic-only.** `reduced_q()`
   (`branch_assignment.py:66-70`) folds by the acoustic period; there is no general
   zone-folding model, no cross-zone identification ("this is the same phonon measured in zone
   (2,0,0) vs (3,0,0)"), and the prior pool never spans zones deliberately.
4. **The dispersion form is a straight line.** A linear polyfit with a 0.3 meV σ floor cannot
   represent curvature, zone-boundary flattening, or anticrossing — the very features a
   dispersion-resolving campaign cares about. The floor also means the prior never gets tighter
   than 0.3 meV no matter how much data exists.
5. **No temperature physics beyond pooling** (0.5 K same-T tolerance): no T-dependent softening
   prior, and T enters fits only through the Bose factor.

### Directions

- **The architecture already prescribes the right insertion points — use them.** HYPOTHESES'
  three-tier design says new physics enters as (Tier 1) preset parameters, (Tier 2) registry
  plugins supplying "prior contribution, likelihood features, steering defaults." Structure
  factor and polarization are textbook Tier-2 features: an expected-intensity-vs-Q function per
  hypothesis that *weights* the branch-assignment likelihood and the acquisition's predicted
  counts, with a confidence scale so it nudges rather than forces. This keeps the
  "no hardcoded rules, only prior nudges" requirement structurally.
- **Cross-zone tracking is mostly a keying problem.** The prior is keyed on the objective
  path's arclength; giving prior points a reduced-q key (general fold, not acoustic-only) plus
  a zone label would let clean folds from one zone inform another, with the structure-factor
  feature (above) explaining why intensities differ. Do this *after* the intensity provenance
  work, or cross-zone amplitudes will mislead.
- **Replace the linear polyfit with something curvature-capable** — a low-order constrained
  polynomial in reduced q (sine-like basis respects zone-boundary symmetry: slope zero at zone
  boundary, ω=0 at Γ for acoustic) or a GP with a periodic kernel. Keep the residual-based σ
  but let the floor decay with fold count instead of being constant 0.3 meV. This is the single
  highest-leverage change for "resolve the dispersion better between scans."
- **Do not** add these as if/else steering rules; every one of them should land as a prior
  precision or a likelihood feature per the §4.5 parameter-prior form, with the existing
  regression tests for prior-forcing circularity extended to cover each new feature.

---

## 3. Backgrounds — the largest real-data risk

### What exists

- **One background model: a flat constant.** `ConstantBackground`
  (`isar/analyze/components.py:152-162`), attached to every enumerated structure. There is no
  sloped, power-law, or structured background anywhere in the enumeration
  (`isar/analyze/structures.py:134` names "second-phonon / sloped-BG" as a later step).
- **The elastic line is a resolution-width Voigt** (`components.py:110-126`) — deliberately
  sharp; its tails are *not* a background mechanism. Broad diffuse scattering is handled by a
  separate `QuasiElastic` component with card-declared fixed width (`components.py:128-149`),
  gated per instrument (e.g. `isar/parsers/cards/in8.policy.toml:28-32`).
- **The documented rationale is conservatism:** "over-flexible backgrounds eat real signal"
  (archived Phase-3 design, §on backgrounds) — flat-by-default, escalate only on residual
  evidence. That escalation logic is **Planned**, not built. No DECISIONS ID governs
  background; it has never been treated as an open design question.
- Background amplitude for detectability checks is a median-of-counts heuristic
  (`structures.py:78-88`), not a fitted or cross-scan quantity.

### Gaps

1. **Long elastic tails have no representation.** On real IXS/TAS data the elastic line
   frequently has non-Voigt wings (sample mosaic, air scatter, analyzer tails). Today those
   counts must be absorbed by the flat constant or by biasing the phonon amplitude — the
   silent-bias failure mode. The resolution model gives the elastic component no tail freedom.
2. **No sloped/nonlinear background hypothesis exists**, so AIC never even gets the chance to
   choose one; the "conservative by default" stance currently means "impossible," not
   "penalized."
3. **Multiphonon and paramagnetic scattering** are acknowledged in archived design notes but
   have no component, no card declaration, and no residual-driven escalation path.
4. **No background information crosses scans.** Every fit re-estimates its constant from
   scratch; there is no per-instrument or per-region background prior, and no concept of
   *buying* background knowledge cheaply (a few off-peak points at a new q) even though the
   driver controls exactly where counts land.
5. **EIG does not know about background nuisances** in the intended goal-oriented way for
   backgrounds richer than a constant — the marginal-EIG design (MEASUREMENT_STEERING §4.1
   item 1) integrates out "background" but the model space only contains the constant.

### Directions

- **Widen the enumerated background family modestly and let AIC pay for it.** A linear-slope
  term and a card-declarable elastic-tail component (e.g. a broad Lorentzian pinned to the
  elastic center, width free-but-prior-bounded or card-fixed like `quasielastic_fwhm`) cover
  the two most common real gnarls. The existing model-selection machinery already handles the
  overfitting risk — that is exactly what Akaike weights are for. This is the natural first
  slice and needs a DECISIONS entry to make background an owned design area.
- **Make backgrounds card-declarable per instrument/sample,** the same pattern as
  `quasielastic_fwhm`: the scientist knows this beamline has a sloped air-scatter background;
  `card.policy.toml` is precisely the file for that intent.
- **Learn backgrounds across scans.** Background level (per unit exposure) at nearby q on the
  same instrument/configuration is strongly correlated; a provenance-tagged background prior
  (same normalization discipline §4.4 imposes on intensities) would stabilize low-count fits
  substantially — at 5-counts-at-peak, the background level is *the* limiting nuisance.
- **Exploit steering: spend a few cheap points on background.** Because the driver chooses the
  energy window, it can deliberately include (or occasionally extend to) off-peak bins whose
  only job is to pin the background — a Stage-C-style disambiguation purchase. This composes
  with the marginal-EIG design: once background has a prior, EIG can literally tell you whether
  a background point or a peak point buys more ω₀ information.
- **Multiphonon/paramagnetic:** don't model them deeply; give each a coarse hypothesis shape
  (smooth rising continuum; Q/T-lawed quasielastic) behind the existing Tier-2 registry seam so
  the falsification layer can *name* them instead of fitting-a-phonon-anyway. The Stage-C
  T/Q-lever design (phonon grows with Bose factor, magnetic falls with form factor) is already
  written down in MEASUREMENT_STEERING §2 — backgrounds should join that discrimination logic,
  not precede it.

---

## 4. Scan placement — where the beam time goes

### What exists

- **The acquisition unit is a whole constant-Q scan** (stated at
  `isar/drive/acquisition/acquire.py:3`), ranked by cost-normalized information:
  master-predictive mutual information about the joint ω/model/branch state per measurement
  cost (or per analysis-second in deterministic mode) (`acquire.py:484-681, 624-625`), with
  Fisher (5b) and counts (5a) tiers as fallbacks (`eig.py:58-180`).
- **Energy windows are already adaptive** (`_energy_window()`, `acquire.py:274-327`): tight
  mode is `e_pred + 1.5·FWHM` on the high side; **survey mode** extends to `e_pred + 3σ_pred`
  (capped +7 meV) whenever the prior is loose (`σ_pred > FWHM`) and collapses back as the prior
  tightens. Step = 0.25·FWHM; hard 100-point cap with step coarsening.
  **Finding: the "very large scan areas" in test campaigns are this mechanism operating as
  designed at cold start** — wide windows while σ_pred is large, narrowing as beliefs firm up.
  If they stayed wide late in a campaign, *that* would be a bug worth a targeted look at why
  σ_pred failed to shrink; the mechanism itself is sound and prevents a documented deadlock
  (narrow window → no signal → no anchor → stays narrow).
- **Stopping/budget:** grid-convergence `met`, budget-exhausted `incomplete`, systematic-floor
  `infeasible` (`isar/drive/supervisor/stopping.py:525-599`); per-scan neutron budgeting
  clamps against TAVI's advertised instrument limits (§7.3; `acquire.py:217-225`).

### Gaps

1. **No within-scan adaptivity.** Once submitted, a scan runs to completion; per-point
   decisions (stop early, densify near the peak) are Planned (MEASUREMENT_STEERING §4.1
   item 3-4). Every scan therefore spends identical dwell on its least informative bins.
2. **No motor/travel cost.** Cost is `n_points × ncount` only; `acquire.py:22` says
   movement-awareness waits on TAVI's virtual instrument clock. Until then EIG-per-cost ranks a
   far q and a near q identically, and there is no path-ordering of the worklist.
3. **Per-second information is Planned** (the O13 remainder) — the ranking currently treats a
   scan's cost as its neutron count, which is only proportional to time at fixed flux and
   ignores per-point overhead (the constant that makes many-short-points more expensive than
   few-long-points).
4. **Cold-start width is paid in full at every campaign start.** A declared hypothesis with a
   confident zone-centre prior already narrows σ_pred (the §4.5 machinery), but nothing else
   (e.g. a cheap coarse quicklook ladder specifically for localization before the first
   production window) shortens the survey phase.

### Directions

- **The prioritized path is already designed — execute it in this order:** (1) per-second
  Fisher/EIG cost normalization with a per-point overhead constant (small, immediate
  efficiency win; O13 remainder), (2) OCBA/VOI keep-counting-vs-move at scan granularity,
  (3) within-scan adaptive dwell, (4) movement-aware cost + travel-ordering once TAVI exposes
  a clock. Point-by-point steering with motor path planning (Adrian's ideal) is genuinely a
  later phase, and the doc already contains the right note for fast scanning: choose the
  *second-best* point so analysis of the last point overlaps with motor movement
  (MEASUREMENT_STEERING §3, Tier-3 note).
- **Cheap immediate win:** log and plot, per campaign, window width vs iteration (the data is
  in the session log). This turns "the scans look too wide" into a measurable
  survey-mode-decay curve and would have answered this checkup's question in one glance.
- Make sure hypothesis confidence actually reaches σ_pred at cold start (Bayesian optic
  cold-start is Partial per O13) — a confident declared gap should mean the very first
  production scan is already near-tight.

---

## 5. Error-bar cost — where fitting time goes and what a cheaper path forfeits

### What exists

- **The methods:** HESSE (iminuit), MINOS profile on fit params
  (`isar/analyze/errors/profile.py:38-59`), exact 1-D ω₀ profile (`profile.py:62-134`),
  parametric bootstrap (authoritative; `isar/analyze/errors/bootstrap.py:159-290`, default
  M=200), dynesty nested sampling (diagnostics only; `isar/analyze/errors/bayes.py:109-190`),
  plus per-scan agreement checks (`errors/agree.py:40-98`) feeding `hesse_trusted` /
  `interval_reliable`.
- **Three static tiers, chosen up front per run** (`isar/session/driver.py:53, 564-566`;
  CLI `--errors-tier`): `fast` = HESSE + trust flag (what the drive loop uses for provisional
  steering), `full` = MINOS + profile + bootstrap (authoritative), `bayes` = full + nested
  sampling on the preliminary-best structure only. **There is no dynamic escalation** — every
  fit in a run commits to its tier before seeing the data.
- **The coverage evidence (STATS_PRIMER §3) is the key asset here:** on ω₀, HESSE ≈ profile ≈
  bootstrap at *every* tested count level when the fit is healthy (0.68/0.68/0.64 at 15
  counts); the ladder only separates when Γ approaches the resolution floor (51% of scans
  outside the HESSE↔bootstrap agreement band in the skewed-Γ cell) — and the per-scan
  agreement flags detect exactly that case.
- **Instrumentation exists but is unexploited:** per-method wall time is recorded
  (`errors/__init__.py:45-74`, `method_seconds`) and `isar profile` aggregates it
  (`profile_cmd.py:91-113`), yet no benchmark numbers appear anywhere in docs.
- The certification architecture already separates provisional from final: pooled-master
  certification re-fits frozen pools at full honesty regardless of what the live loop used
  (STATS_PRIMER §4.1, MEASUREMENT_STEERING §7.4).

### Gaps

1. **The measured coverage result licenses a cheap-first policy that nothing implements.**
   Since HESSE matches bootstrap on healthy ω₀ fits and `hesse_trusted` identifies the healthy
   ones per-scan, running the bootstrap on every fit in a live loop is (by ISAR's own
   experiment) mostly wasted compute. The escalation rule "bootstrap only when `hesse_trusted`
   is false, Γ is near a bound, or the scan is entering certification/prior-update" follows
   directly from Figs. 4-6 — it is evidence-backed, not a shortcut.
2. **No benchmarks.** `method_seconds` is collected but never summarized; nobody can currently
   say "bootstrap is X% of campaign analysis time." The `analysis_limit_seconds` clock (§7.5)
   makes this an operational question, not a curiosity.
3. **Γ-at-bound fallback is stored but not promoted:** the ω₀ profile interval is computed and
   kept (`driver.py:92-93`), but the reported interval slot falls back to HESSE width per
   STATS_PRIMER §4 — the profile interval is arguably the better honest band there and is
   already paid for.
4. **`boot_adaptive` (early-stopping bootstrap) exists as a parameter** but is not the default
   and its behavior is undocumented.
5. **Tier is CLI-only** — no card-policy or per-campaign-objective control, so an operator
   cannot express "fast during drive, full at certification" declaratively (it happens today
   only because the drive loop and certification paths choose different tiers in code).

### Directions

- **Adopt escalate-on-distrust as the drive-loop default.** Fast tier everywhere; escalate a
  given fit to `full` when any of: `hesse_trusted=False`, Γ within its agreement-breaking zone
  of a bound, fork/ambiguous modality, or the result is about to seed the prior or a
  certification pool. The prior-seeding trigger matters most: nothing that enters
  `dispersion_prior` or a pooled master should ever carry only a HESSE bar. Certification
  already re-fits at full honesty, so final numbers are unaffected by making the live loop
  cheaper.
- **Publish the numbers first.** One session with `isar profile` on an archived campaign
  (e.g. `mvp_zone_test16`) turns gap #2 into a table: per-method seconds, share of
  analysis-limit budget, projected saving under escalate-on-distrust. Do this before tuning
  anything — it sizes the prize and it may show profile (not bootstrap) dominates in some
  regimes.
- Wire `boot_adaptive` on by default once its stopping rule is documented, and consider
  M as a function of role (M=50 for steering-provisional, M=200+ for certification).
- Promote the ω₀-profile interval to the delivered band in the Γ-at-bound case (it is
  boundary-aware by construction); keep the flag.

---

## Cross-cutting observations

1. **The systematic-uncertainty caveat is the elephant behind concerns 1, 3, and 5.**
   STATS_PRIMER §6 is explicit: all intervals are statistical-only, and the ~20% gold-set
   coverage against a human is the measured consequence. Background mis-modeling (concern 3)
   is the largest *named* systematic. Every efficiency gain in counting or error tiers makes
   intervals *tighter*, which makes the missing-systematics problem relatively *worse* — the
   background work should therefore be sequenced ahead of, or with, the counting-efficiency
   work, not after it.
2. **Most of what Adrian wants is already designed, not yet built.** The recurring pattern
   across all five concerns: MEASUREMENT_STEERING contains the correct design (per-second
   Fisher dwell, OCBA move-vs-stay, marginal EIG over nuisances, Tier-2 physics plugins,
   model-averaged acquisition), marked Partial or Planned. The checkup's main output is a
   *sequencing* recommendation, not new architecture:
   1. Background hypothesis family + card declaration + DECISIONS entry (biggest real-data risk,
      unlocks honest low-count work);
   2. Detection-power dwell floor + observed-rate feedback (concern 1, small diffs);
   3. Escalate-on-distrust error tiers + the profiling table (concern 5, pure win, evidence
      already in hand);
   4. Curvature-capable dispersion prior with decaying floor (concern 2's highest-leverage item);
   5. O13 remainder: per-second EIG + OCBA (concern 4);
   6. Structure-factor / polarization / cross-zone as Tier-2 registry features (concern 2's
      long arc — after intensity provenance is trustworthy).
3. **Guardrails to preserve while doing any of this** (they are load-bearing and easy to
   erode): the fit-vs-decide split (priors never touch AIC/goodness-of-fit), priors as
   parameter-precision never pseudo-data, fold-only prior admission, provenance-tagged
   intensity portability, and the frozen Stage II↔III file contract (additive-only from v2).
   Every proposed nudge above fits inside those rules; anything that doesn't should go through
   DECISIONS first.
4. **One measurement gap made this checkup harder than it should be:** window width, per-method
   fit seconds, and count-rate prediction error are all *recorded* but never *reported*. A
   small campaign-diagnostic addition (three plots) would let the next checkup read the answers
   off a report instead of re-auditing the code.

---

## Source anchors (for the person picking this up)

- Counting/dwell: `isar/drive/acquisition/acquire.py:158-226`, `isar/drive/supervisor/stopping.py:33-34, 228-295`, `isar/schema/hypotheses.py:98`
- EIG/placement: `isar/drive/acquisition/acquire.py:274-327, 484-681`, `isar/drive/acquisition/eig.py:16-180`, `isar/drive/supervisor/stopping.py:525-599`
- Prior/tracking: `isar/session/session_prior.py:48-50, 84-128`, `isar/session/branch_model.py:52-56`, `isar/session/branch_assignment.py:66-70, 156-296`, `isar/analyze/errors/bayes.py:151-159`
- Backgrounds: `isar/analyze/components.py:110-162`, `isar/analyze/structures.py:78-88, 134`, `isar/parsers/cards/in8.policy.toml:28-32`
- Errors/tiers: `isar/session/driver.py:53, 92-93, 556-566`, `isar/analyze/errors/` (`profile.py`, `bootstrap.py`, `bayes.py`, `agree.py`, `__init__.py:45-186`), `isar/cli/profile_cmd.py:91-113`
- Docs: `docs/STATS_PRIMER.md` (coverage evidence, statistical-only caveat), `docs/MEASUREMENT_STEERING.md` (§2, §4.1, §7.3-7.5, §8), `docs/HYPOTHESES.md` (three-tier extensibility), `docs/DECISIONS.md` (O6-O15, D3, D10)

*Audit performed 2026-07-19 by parallel read-only code sweeps plus doc review; no code changed.*

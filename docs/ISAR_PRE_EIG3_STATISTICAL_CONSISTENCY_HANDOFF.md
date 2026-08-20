# ISAR Pre-EIG-3 Statistical Consistency Pass

> **Status:** terminal
> **Maintenance note:** Retained temporarily for the original through-STAT-5 rationale. Its stage
> names and anchor revision are historical; current execution is governed by
> `docs/plans/stats-audit.md` and the applicable code-grounded plan under
> `docs/plans/`.

**Working name:** STAT-CONSISTENCY  
**Repository:** `ammerritt-gh/ISAR`  
**Anchor revision:** `e37b7b4fe0debbd0aa32402165b16c4e2aee3a93`  
**Position in roadmap:** after EIG-2.5, before any EIG-3 scorer implementation  
**Primary goal:** make ISAR's statistical quantities coherent, explicitly typed by paradigm, and safe to consume as a design measure.  
**Non-goal:** implement scan-window EIG, candidate scoring, selection authority, discovery steering, or TAVI changes.

---

## 1. Why this pass exists

EIG-3 will repeatedly update a probability measure over physical models and parameters using hypothetical future Poisson data. The mathematics of that update is straightforward only after ISAR has answered a more basic question:

> What probability or uncertainty measure do the retained model weights and parameter draws actually represent?

At the anchor revision, ISAR deliberately contains several statistical paradigms:

- data-only likelihood fits and information-criterion model comparison;
- soft-constrained and hard-conditioned fits;
- HESSE and MINOS/profile approximations;
- parametric-bootstrap confidence distributions;
- nested-sampling posterior draws and marginal-likelihood estimates;
- AIC-weighted across-model mixtures;
- branch-assignment weights from a versioned graphical scoring model;
- heuristic confidence scores used for diagnostics.

Using several paradigms is not itself inconsistent. The inconsistency is that some current artifacts combine or name them as though they were one probability model. The current EIG plan already identifies the retained design measure as `akaike_weighted_predictive_ensemble`, with likelihood reweighting and `formal_bayesian_posterior = false`. This pass should make that statement true throughout the code rather than merely adding an honest label at the EIG boundary.

The pass should establish a **declared multi-paradigm system**, not force all of ISAR to become Bayesian:

1. **Frequentist inference and certification lane** — likelihood fits, profile/bootstrap/HESSE intervals, with coverage as the authority criterion.
2. **Bayesian posterior/evidence lane** — nested sampling with explicit proper priors, posterior draws, evidence, and Bayesian diagnostics.
3. **Information-theoretic model-comparison lane** — AIC/AICc/BIC and their weights, never mislabeled as posterior model probabilities.
4. **Assignment/scoring lane** — branch-assignment and confidence scores, explicitly labeled according to whether they are calibrated probabilities, normalized score weights, or heuristic confidence measures.

No consumer should silently cross those lanes. A bridge is permitted only when its semantics are named and its calibration evidence is recorded.

---

## 2. Current statistical surfaces to verify

The following are code-factual starting points, not assumptions to preserve blindly.

### 2.1 Structure selection

`isar/analyze/select.py` selects converged structures by data-only Baker–Cousins AIC and forms Akaike weights. AICc and BIC are computed but do not control selection. The code explicitly calls the weights Akaike weights.

Questions for the audit:

- Is AIC rather than AICc still justified at the observed `n/k` ratios?
- Do all fits entering one Akaike normalization represent data-only maximum-likelihood estimates over comparable parameter spaces?
- Are soft-prior, hard-fixed, branch-conditional, and Stokes-only fits ever mixed into the same ordinary model-weight set?
- Are any downstream consumers treating Akaike weights as posterior model probabilities?

### 2.2 Constrained point estimates

`isar/analyze/loop.py` separates the data cost from the optimization objective. Soft constraints add a Gaussian penalty to the objective, while `StructureFitResult.cost` remains the data-only deviance evaluated at the penalized optimum. This is useful provenance, but ordinary AIC evaluated at a MAP-like penalized optimum is not standard maximum-likelihood AIC.

A clean model-selection contract should therefore do one of the following:

- compare data-only unconstrained MLEs, then apply constraints only after structure selection; or
- use a separately named and justified criterion for penalized/conditional fits; or
- mark constrained fits ineligible for ordinary Akaike model weights.

Do not leave the present hybrid implicit.

### 2.3 Within-model uncertainty

The main uncertainty methods currently answer different questions:

- HESSE: local asymptotic approximation;
- MINOS/profile: likelihood-based confidence construction;
- parametric bootstrap: repeated-sampling distribution of a refitted estimator;
- nested sampling: posterior under the declared prior and likelihood.

`isar/analyze/errors/__init__.py` treats the bootstrap interval as authoritative and the Bayesian result as a diagnostic. That is a coherent policy. The problem is that later predictive code may select the “strongest available” draw set model by model, producing one ensemble that mixes posterior draws, bootstrap refits, and Laplace draws.

### 2.4 Bayesian implementation

`isar/analyze/errors/bayes.py` already computes:

- equal-weight posterior resamples;
- marginal-likelihood `log_evidence` and uncertainty;
- posterior intervals and ESS diagnostics.

However, at the anchor revision:

- production passes no dispersion prior through `_bayes_prior_for`; the Bayesian run is bounds-only;
- only the preliminary best model receives nested sampling;
- the prior transform is the optimizer-bound box, and several bounds depend on the scan window or observed count scale;
- evidence is therefore not yet a ready-made posterior model-weight system;
- the current evidence precision gate was designed mainly as a sanity check, not as a model-weight calibration threshold.

The presence of `log_evidence` does not make current Akaike weights Bayesian, and it does not justify using the one sampled model’s evidence against unsampled alternatives.

### 2.5 Model-averaged master interval

The published master predictive artifact forms an across-model omega distribution using model weights and per-model draws. The master-reading path can use that `model_interval` as the certifying interval.

The audit must verify and correct these semantics:

- whether the draw source can differ by model;
- whether the interval is conditional on target-present models;
- whether null/no-target model mass is omitted from the omega interval;
- whether a narrow conditional omega interval can certify while target-presence support remains weak;
- whether `model_entropy` is actually Akaike-weight entropy rather than Bayesian posterior entropy.

A conditional target-energy interval and uncertainty about whether the target exists are separate objects and must never be collapsed silently.

### 2.6 Cross-paradigm agreement

The agreement layer compares bootstrap, profile/HESSE, and Bayesian intervals. This is meaningful only when they condition on the same model, parameterization, constraints, and data.

At minimum, the pass should verify that comparisons abstain or carry a mismatch reason when:

- the frequentist fit used a soft prior but the Bayesian run did not;
- a parameter was hard-fixed in one path but not the other;
- the selected model changed after the Bayesian run;
- a one-sided/Stokes-only structure differs from the compared structure.

### 2.7 Branch and confidence quantities

The branch tracker produces normalized assignment weights under a versioned factor/scoring model, including explicit heuristic penalties and confidence-scaled evidence. The general confidence layer produces a geometric-mean score. Neither should be silently described as a calibrated posterior probability unless calibration has demonstrated that interpretation.

This pass should inventory names and consumers, but it should not rewrite branch physics or branch-tracking behavior.

---

## 3. Governing rulings for this pass

### Ruling S-1 — Multiple paradigms are allowed; unlabeled mixtures are not

Every statistical artifact or field that can reach steering, certification, or user output must declare at least:

```text
estimator_kind
model_weight_kind
draw_measure_kind
interval_kind
probability_semantics
formal_bayesian
conditioning / fit_kind
```

Suggested controlled vocabulary:

```text
estimator_kind:
  maximum_likelihood
  penalized_likelihood
  conditional_fixed

model_weight_kind:
  akaike
  aicc
  bic
  bayesian_model_posterior
  none

draw_measure_kind:
  parametric_bootstrap_sampling_distribution
  posterior_resample
  laplace_pseudo_posterior
  none

interval_kind:
  bootstrap_confidence
  profile_likelihood_confidence
  hesse_asymptotic
  bayesian_credible
  akaike_weighted_bootstrap_mixture

probability_semantics:
  posterior_probability
  information_criterion_weight
  normalized_assignment_weight
  heuristic_confidence_score
```

The exact implementation may use enums, string constants, or versioned schema fields. It must use one shared vocabulary rather than scattered free-form labels.

### Ruling S-2 — EIG-3 must consume one homogeneous design measure

The EIG-facing design ensemble must not pick posterior draws for one model, bootstrap draws for another, and Laplace draws for a third.

**Recommended v1 design measure:**

```text
design_measure = akaike_weighted_parametric_bootstrap_predictive
model_weight_kind = akaike
draw_measure_kind = parametric_bootstrap_sampling_distribution
formal_bayesian = false
```

Requirements:

- every retained scored model supplies a valid bootstrap parameter ensemble;
- models lacking that ensemble make the EIG design basis unavailable rather than triggering a per-model fallback;
- nested posterior and Laplace draws remain available as separately named diagnostics, not substitutes inside this measure;
- a full-tier escalation may be used later to obtain the required bootstrap basis;
- likelihood reweighting in EIG remains explicitly an empirical design update, pending full-pipeline calibration.

This is not the eventual ideal Bayesian answer. It is a coherent baseline that is substantially safer than a mixed ensemble and can be calibrated against the actual refit pipeline.

### Ruling S-3 — Bayesian promotion requires a complete Bayesian model mixture

Do not use Bayesian evidence for model weights until all of the following exist:

- a versioned, normalized prior specification for every model parameter;
- priors that do not depend silently on observed `cmax` or other data-derived bounds;
- a declared prior over model structures;
- nested sampling for every retained model in the comparison set;
- evidence-convergence requirements appropriate to model-weight use;
- propagation or sensitivity analysis for `logz` uncertainty;
- posterior-draw and simulation-based-calibration evidence;
- a complete null/no-target model.

When those conditions hold, ISAR may publish a separate:

```text
design_measure = bayesian_model_averaged_posterior
model_weight_kind = bayesian_model_posterior
draw_measure_kind = posterior_resample
formal_bayesian = true
```

Do not overwrite the frequentist design measure in place. Compare them first.

### Ruling S-4 — Certification remains coverage-based during this pass

This pass must not make Bayesian credible intervals authoritative merely to make the system “more Bayesian.” The current certification lane remains frequentist unless a dedicated validation campaign supports a change.

The pass may repair mislabeled or statistically unsafe model averaging. Any behavior-changing certification repair must have synthetic coverage and false-certification evidence in the same slice.

### Ruling S-5 — Target presence and conditional target energy are separate

Persist separately:

```text
target_presence_support
conditional_target_qoi_interval
conditional_target_qoi_measure_kind
```

A conditional omega interval must not absorb or hide the mass assigned to no-target/null models. A target cannot certify solely because its conditional interval is narrow.

The threshold or rule for sufficient target-presence support must be calibrated; do not invent an untested 0.95 rule in production.

### Ruling S-6 — Standard AIC requires a standard comparison fit

Ordinary AIC/AICc weights may only be interpreted as such when the compared candidates are evaluated at data-only maximum-likelihood estimates over declared comparable model families.

Soft-prior and hard-conditioned results may still be delivered and reported, but they must not silently enter the ordinary Akaike weight normalization unless a separate data-only selection fit exists.

### Ruling S-7 — Boundary LRT values are diagnostic until calibrated

Nested component tests involving amplitudes on a zero boundary, or parameters undefined under the null, do not automatically satisfy the ordinary chi-square Wilks approximation. Current nested-test p-values may remain visible diagnostics, but no scientific authority should depend on them until a bootstrap or otherwise justified calibration is supplied.

---

## 4. Incremental execution plan

## STAT-0 — Statistical producer/consumer audit

**Behavior change:** none.

Create `docs/plans/stats-audit.md` with one row per load-bearing quantity:

```text
field / artifact
producer
mathematical meaning
conditioning
consumer(s)
current authority
current name accurate? yes/no
calibration evidence
proposed action
```

Mandatory inventory targets:

- `StructureFitResult.cost`, `posterior_cost`, `fit_kind`;
- AIC, AICc, BIC, Akaike weights, nested LRT p-values;
- HESSE, MINOS, profile, bootstrap, Bayesian intervals;
- bootstrap, posterior, and Laplace parameter draws;
- `log_evidence`, `log_evidence_err`, ESS;
- `model_interval`, `model_entropy`, `model_robust`;
- `confidence`, `branch_posterior`, `target_probability`, `identity_entropy`;
- every field used by stopping/certification;
- every field published in `master_predictive/2` and `target_projection/2`.

The audit must trace actual consumers, not rely on comments.

**Required findings report:**

1. Exactly which fit kinds enter ordinary structure selection.
2. Exactly which interval seats convergence at each tier.
3. Exactly how null/no-target weight affects certification today.
4. Exactly when one predictive artifact mixes draw paradigms.
5. Exactly where `log_evidence` is produced, persisted, and ignored.
6. Whether branch “posterior” quantities are calibrated probabilities or normalized assignment scores.

**Exit:** reviewed audit, no code behavior change, explicit list of behavior-changing repairs for the next slices.

---

## STAT-1 — Statistical semantics contract

**Behavior change:** metadata only.

Add one shared statistical-semantics vocabulary and propagate it through internal artifacts and result provenance. Prefer additive fields first; this is a pre-release repository, so a clean schema bump is acceptable if it removes ambiguity.

Minimum additions:

- point-estimator kind;
- model-weight kind;
- draw-measure kind per draw set;
- interval kind and nominal level;
- formal-Bayesian boolean;
- probability semantics for branch/confidence outputs;
- prior specification fingerprint when Bayesian;
- fit/constraint conditioning fingerprint.

Correct misleading user-facing and doc language:

- Akaike weight is not posterior model probability;
- `confidence` is a confidence score, not confidence level or posterior probability;
- branch assignment weights are named according to their actual scoring semantics;
- model-weight entropy names its weight system;
- Bayesian credible and frequentist confidence intervals remain distinct.

Add machine-checkable contract tests preventing a field from reverting to an ambiguous label.

**Exit:** every decision-bearing statistical quantity can be interpreted from its record alone.

---

## STAT-2 — Homogeneous EIG design basis

**Behavior change:** predictive side artifact and EIG eligibility only; no acquisition selection.

Refactor the predictive producer so it publishes separately named draw sets rather than one “strongest available” mixture.

Required v1 design set:

```text
schema / version
design_measure = akaike_weighted_parametric_bootstrap_predictive
model_weight_kind = akaike
formal_bayesian = false
per model:
  source model weight
  bootstrap parameter names/draws
  draw measure kind
  draw IDs and completeness
```

Rules:

- all retained models use bootstrap draws;
- no posterior/Laplace substitution in this set;
- if any material retained model lacks valid bootstrap draws, the design basis is unavailable with a stable reason;
- posterior and Laplace sets may be published separately for diagnostics;
- predictive basis fingerprint includes all semantic declarations;
- target projection joins by model label and draw ID, never by array position alone.

Update the EIG-3 handoff so its scorer consumes this named design set rather than generic per-model “best draws.”

**Tests:**

- Bayes-tier runs do not silently replace one model’s bootstrap draws with posterior draws in the v1 design set.
- Removing one model’s bootstrap draws refuses the complete design set.
- Reordering models/draws leaves joins and predictions unchanged.
- Every retained particle has one measure kind.
- Truth firewall remains clean.

**Exit:** EIG-3 has one coherent empirical design distribution.

---

## STAT-3 — Model-selection and constraint consistency

**Behavior change:** potentially scientific; land separately and validate carefully.

First characterize, then repair, how constrained fits interact with model comparison.

Preferred implementation direction:

1. Perform structure comparison on data-only unconstrained MLE fits.
2. Select/retain structures using the declared information criterion.
3. Apply soft priors or branch-conditional hard constraints only in a second, explicitly conditioned delivery fit.
4. Keep selection-fit and delivery-fit provenance linked but distinct.

A smaller acceptable repair is to mark constrained fits ineligible for ordinary Akaike weighting and refuse model-averaged design/certification outputs when only such weights are available.

Run an AIC/AICc/BIC study before changing the primary criterion. The study must include small-window and high-parameter-count cases, because the code already computes AICc and documents small `n/k` ratios.

Record:

- true-model recovery;
- false extra-mode rate;
- null versus phonon discrimination;
- stability under weak signal/background;
- effect on target interval coverage;
- effect on existing campaign outcomes.

Do not use the current nested LRT p-values as a deciding reference in boundary/non-identified cases.

**Exit:** every ordinary model weight is computed from a statistically comparable fit family.

---

## STAT-4 — Target presence and model-averaged certification interval

**Behavior change:** potentially scientific; requires calibration evidence.

Repair the current model-averaged master semantics:

- publish total support assigned to target-present structures separately from target-absent/null structures;
- label the omega interval as conditional on target presence;
- construct the frequentist model-averaged interval from one homogeneous within-model measure, preferably bootstrap draws;
- label its model weighting as Akaike/AICc according to the selected criterion;
- do not call it a Bayesian posterior or credible interval;
- prevent a narrow conditional omega interval from certifying when target presence is unresolved;
- preserve fork/multimodality behavior and explicit abstention.

Thresholds for target-presence support and model ambiguity must come from calibration campaigns. Before calibration, publish diagnostics without changing certification authority.

Required scenarios:

- true null/background;
- weak near-threshold phonon;
- strong isolated phonon;
- one-mode versus two-mode ambiguity;
- target absent but rival present;
- soft-prior rescue;
- gamma at boundary;
- background misspecification.

Required metrics:

- false certification;
- target-detection sensitivity/specificity;
- interval coverage conditional on target present;
- unconditional decision coverage including target absence;
- model-selection frequencies;
- current-versus-repaired campaign outcomes.

**Exit:** certification cannot hide target-existence uncertainty inside a conditional energy interval.

---

## STAT-5 — Bayesian model-mixture feasibility spike

**Behavior change:** none initially; evidence study and optional diagnostic artifact.

Build a small, isolated all-model Bayesian experiment on representative retained model sets.

### Prior contract

Define a versioned prior registry separate from optimizer bounds. Priors must be:

- proper and normalized;
- physically defensible;
- stable across equivalent scan windows where appropriate;
- explicit about any design-domain truncation;
- independent of observed `cmax` and other data-derived scaling unless formally labeled empirical Bayes;
- fingerprinted in every posterior/evidence artifact.

Define model priors explicitly.

### Sampling contract

For every retained model:

- run nested sampling under the same prior policy;
- require posterior and evidence diagnostics;
- retain posterior draws and log evidence;
- propagate or sample evidence uncertainty into model posterior weights;
- include the null model;
- refuse a Bayesian model mixture if any material model is missing or untrustworthy.

### Validation

Run:

- simulation-based calibration for parameter posteriors;
- posterior predictive checks;
- evidence stability across `nlive`, seed, and prior width;
- true-model/model-family recovery;
- comparison with AIC/AICc weights;
- runtime profiling per master and campaign.

### Decision gate

Record one of:

```text
GO:
  publish bayesian_model_averaged_posterior as a second design measure;
  compare it with the frequentist design measure in EIG-3 shadow.

CONDITIONAL GO:
  use only for selected simple model families / diagnostic campaigns;
  authority remains barred elsewhere.

NO GO FOR NOW:
  retain the coherent non-Bayesian design measure;
  keep log evidence diagnostic and do not imply Bayesian model averaging.
```

**Exit:** the project has an evidence-based decision, not a vague intention to “use logz later.”

---

## STAT-6 — Cross-paradigm agreement and documentation closeout

Make cross-method comparisons conditional on statistical comparability.

Agreement should state:

```text
same model? 
same constraints/prior? 
same target projection? 
same data and fit window? 
comparison valid? 
reason if invalid
```

Do not compute a reassuring width ratio between methods answering different questions.

Update:

- `docs/design/steering.md`;
- `docs/design/eig.md`;
- `docs/DECISIONS.md` and its index;
- documentation inventory;
- user-facing terminology.

Add a short `docs/stats/STATISTICAL_STANCE.md` that answers:

1. What is frequentist and authoritative?
2. What is Bayesian and diagnostic/authoritative?
3. What are Akaike weights used for?
4. What measure does EIG-3 consume?
5. What quantities called “probability” are calibrated?

---

## 5. Minimal exit bar before EIG-3 begins

EIG-3 may begin only when all of the following hold:

1. The statistical producer/consumer audit is complete.
2. Every EIG-relevant model weight and draw set declares its semantics.
3. The EIG design basis is homogeneous across retained models.
4. The design basis is explicitly non-Bayesian unless a complete Bayesian mixture has passed STAT-5.
5. AIC/AICc weights are not produced from penalized/conditional estimates without an explicit comparison contract.
6. Target-presence uncertainty is not silently discarded from target-energy reporting.
7. Bayesian evidence is not used for model weights unless all retained models share a proper, versioned prior and valid evidence.
8. Bayesian and frequentist intervals are named distinctly.
9. Branch/confidence quantities declare whether they are calibrated probabilities, normalized assignment weights, or heuristic scores.
10. The EIG-3 plan is updated to name the exact design measure it consumes.
11. Off-mode and shadow acquisition behavior remains unchanged throughout the pass unless a separately approved, calibrated scientific repair intentionally changes it.

The recommended EIG-3 input after the minimal pass is:

```text
akaike_weighted_parametric_bootstrap_predictive
formal_bayesian = false
```

The recommended long-term comparator is:

```text
bayesian_model_averaged_posterior
formal_bayesian = true
```

Both should coexist during shadow validation rather than one silently replacing the other.

---

## 6. Verification strategy

### Focused tests

At minimum:

```bash
python -m pytest -q \
  tests/analyze/test_select.py \
  tests/analyze/test_errors.py \
  tests/analyze/test_bayes.py \
  tests/session/test_master_predictive.py \
  tests/session/test_master_fit.py \
  tests/drive/test_stopping.py \
  tests/drive/test_loop.py \
  tests/docs/test_eig_steering_contract.py
```

Adjust paths to the actual repository test layout after discovery; do not invent missing test modules merely to satisfy this list.

Then:

```bash
python -m pytest -m "not slow" -n 16 --dist worksteal
python -m pytest -n 16 --dist worksteal
```

### Statistical validation scripts

Retain scripts/results for:

- AIC versus AICc/BIC selection;
- model-averaged interval coverage;
- target presence calibration;
- posterior SBC;
- evidence stability and prior sensitivity;
- design-measure sensitivity across bootstrap and Bayesian variants.

Evidence notes belong under `docs/stats/` or `docs/campaigns/` with commit, seeds, scenarios, and exact conclusions.

### Regression requirements

- Off-mode proposal and request sequences remain identical unless an explicitly approved scientific correction says otherwise.
- No TAVI changes are expected.
- Existing truth-firewall tests remain green.
- No Bayesian diagnostic failure may turn a valid frequentist fit into `non_result`.
- No absent Bayesian artifact may block legacy campaign operation.

---

## 7. Explicit non-goals

This pass does **not** implement:

- scan-window EIG mathematics;
- CRN simulation;
- target-QoI binning;
- candidate scoring or counterfactual winner selection;
- EIG authority;
- pointwise steering;
- model-agnostic discovery;
- ARIANE integration or benchmarking;
- branch-physics expansions;
- TAVI scan-system changes;
- time-aware scan cost.

It prepares the probability and uncertainty measures those later systems will consume.

---

## 8. Recommended first Codex task

Do **STAT-0 only** first.

The initial Codex session should:

1. verify the repository is exactly at or descended from `e37b7b4fe0debbd0aa32402165b16c4e2aee3a93` and stop immediately if the expected code is not present;
2. read this handoff plus the active EIG/statistics docs;
3. trace producers and consumers from code;
4. write `docs/plans/stats-audit.md`;
5. propose the exact PR breakdown for STAT-1 onward;
6. make no behavior-changing code edits until the audit is reviewed.

The audit should be specific enough that another agent can implement later slices without rediscovering what each field means.

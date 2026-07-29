# Statistical semantics audit — STAT-0

> **Lifecycle:** Active plan
> **Provenance:** hand-written
> **Verified against code revision:** `e37b7b4fe0debbd0aa32402165b16c4e2aee3a93` (2026-07-29)
> **Owner:** @ammerritt-gh
> **Exit criteria:** STAT-6 has transferred every accepted semantic contract into the canonical
> statistical, steering, schema, and decision documents; every rejected hypothesis is recorded; the
> audit is then deleted and entered in the `DOC_INVENTORY.md` tombstone table.

## Purpose, boundary, and result

This is the code-grounded STAT-0 producer/consumer audit required before any scan-window EIG
scorer. It describes the tree at the verified revision above. Source code wins over comments and
over earlier design prose.

STAT-0 changes no fit, interval, selection, calibration, steering, acquisition, or instrument
behavior. In particular, it does not implement scan-window EIG scoring, CRN simulation, QoI bins,
counterfactual selection, acquisition authority, discovery steering, pointwise scans, or TAVI
changes.

The immediate result is a **hold on statistical authority**, not a hold on all development:

- the current mixed `model_interval` must not be treated as a statistically homogeneous design
  measure;
- the first design measure worth developing is the explicitly non-Bayesian
  `akaike_weighted_parametric_bootstrap_predictive`, but only as a shadow empirical baseline;
- STAT-2 must first make model-selection weights interpretable, and STAT-4a must state target
  presence separately from the conditional omega distribution;
- full-pipeline realized-update calibration is required before the shadow bootstrap ensemble can
  acquire authority;
- an all-retained-model Bayesian posterior mixture remains the coherent candidate for formal
  Bayesian EIG, subject to STAT-5's prior and evidence gates.

### Evidence vocabulary

Every audit record uses one of four statuses:

| `evidence_status` | Meaning |
|---|---|
| `verified_code_fact` | Directly established by the cited producer and consumer code. |
| `derived_statistical_consequence` | Follows mathematically from verified code facts; no product preference is assumed. |
| `suspected_inconsistency` | Code establishes the ingredients, but calibration or an owner decision is needed to judge acceptability. |
| `open_design_question` | The code cannot answer the scientific or product choice. |

“Confidence” in the findings tables is confidence in this audit conclusion, not ISAR's
`confidence` field.

### Read set

The trace covered the live fit, selection, error, predictive, branch, certification, calibration,
and stopping paths, principally:

- `isar/analyze/loop.py`, `select.py`, `structures.py`, `confidence.py`, and
  `isar/analyze/errors/{__init__,bootstrap,profile,bayes,agree}.py`;
- `isar/session/{driver,predictive_artifact,branch_assignment,branch_tracking,certification}.py`;
- `isar/predictive.py`, `isar/drive/loop.py`,
  `isar/drive/supervisor/stopping.py`, and
  `isar/drive/acquisition/calibration.py`;
- `docs/STATS_PRIMER.md`, `docs/GLOSSARY.md`, `docs/INTERPRETING_A_VERDICT.md`,
  `docs/MEASUREMENT_STEERING.md`, and `docs/EIG_STEERING_DESIGN.md`.

The focused EIG-2.5 baseline at this revision is 137 passing tests:
`tests/session/test_master_predictive.py`,
`tests/session/test_target_projection.py`, and
`tests/docs/test_eig_steering_contract.py`.

## Executive findings

| ID | Priority | Conclusion | Evidence status | Confidence |
|---|---|---|---|---|
| STAT0-P0-1 | P0 | Ordinary AIC-family quantities are data-only deviance values evaluated at whatever optimizer solution the fit path produced. A soft-constrained solution is generally not the data-only MLE required by ordinary AIC. Hard conditioning can define a valid lower-dimensional likelihood model, but ISAR does not record whether the constraint was independently specified or introduced by a data-dependent rescue. | `verified_code_fact` + `derived_statistical_consequence` | High |
| STAT0-P0-2 | P0 | One `master_predictive/2` ensemble can mix nested-posterior, constrained or unconstrained parametric-bootstrap, and Laplace/HESSE particles model by model. The model weights are fixed observed-data ordinary-AIC weights; bootstrap draws do not rerun model selection or propagate weight uncertainty. | `verified_code_fact` | High |
| STAT0-P0-3 | P0 | That mixed ensemble's omega quantiles become `model_interval`; an external frequentist inflation is then applied and `model_robust` can seat certification. This is neither a Bayesian model-averaged credible interval nor a bootstrap distribution of the complete select/refit/reselect procedure. | `verified_code_fact` + `derived_statistical_consequence` | High |
| STAT0-P0-4 | P0 | Target-presence uncertainty is not carried into the certifying scalar interval. A retained model with no omega draws is skipped when the omega mixture is built, and the remaining mass is renormalized. The interval is therefore conditional on target-present retained mass, while its field name and certification consumer do not declare that conditioning. | `verified_code_fact` + `derived_statistical_consequence` | High |
| STAT0-P1-1 | P1 | `log_evidence` is produced for at most the preliminary-best model, using optimizer-bound uniform boxes whose support can depend on the observed window and maximum count. It is persisted and displayed but ignored by model selection and model weights. It is not a complete Bayesian model-weight system. | `verified_code_fact` | High |
| STAT0-P1-2 | P1 | The dormant Gaussian omega-prior seam adds a normalized Normal density inside `loglike` while the prior transform stays uniform over optimizer bounds. It gives the intended posterior kernel, but the returned evidence is not the evidence under a properly normalized truncated omega prior unless its support normalization is included. Production avoids this sub-defect because `_bayes_prior_for()` currently returns `None`. | `derived_statistical_consequence` | High |
| STAT0-P1-3 | P1 | MINOS/profile/bootstrap honor soft and hard constraints; the Bayesian path honors neither `ctx.constraints` soft penalties nor hard-fixed coordinates. `agreement()` tests interval availability and width only, so cross-paradigm flags can compare different statistical questions. | `verified_code_fact` | High |
| STAT0-P1-4 | P1 | `branch_posterior` and `target_probability` are normalized distributions under versioned scoring models containing occupancy, missing/unassigned, reliability, continuity, ordering, and character-handoff terms. They are useful probability-like assignment weights, but no reliability campaign establishes them as calibrated probabilities. General `confidence` is explicitly a heuristic score. | `verified_code_fact` + `derived_statistical_consequence` | High |
| STAT0-P1-5 | P1 | The nested LRT uses the ordinary chi-square reference. This is a formula-correct diagnostic for regular nested cases, but its p-value is not licensed as calibrated when added amplitudes are on a boundary or component location/width is unidentified under the null. | `verified_code_fact` + `derived_statistical_consequence` | High |
| STAT0-P1-6 | P1 | Trusted HESSE intervals are decision-bearing: covariance validity, gamma off-bound, and at least 200 kept counts can let the fast tier converge before the terminal full-tier check. Existing synthetic evidence supports selected regimes, not every exposure, constraint, and model-selection regime in which the predicate can be true. | `verified_code_fact` + `suspected_inconsistency` | High |
| STAT0-P2-1 | P2 | AICc and BIC are computed, but ordinary AIC alone selects, normalizes weights, triggers survivors, and feeds prediction. There is no committed AIC/AICc/BIC recovery campaign across ISAR's `n/k` regimes. | `verified_code_fact` | High |
| STAT0-P2-2 | P2 | “The model mixture” is not one set: the error-attachment and legacy pooling floor is 0.01, while the predictive completeness floor is 0.005 and the predictive producer starts from all scored models. Full-tier sub-survivors can therefore enter through Laplace draws. | `verified_code_fact` | High |
| STAT0-P2-3 | P2 | `model_entropy` is Shannon entropy of retained normalized Akaike weights; `model_robust` means only “an omega interval exists and the fit is not a fork” at production; `posterior_predictive_mc` may operate on non-posterior particles. These names are too broad without additive semantics metadata. | `verified_code_fact` | High |
| STAT0-P2-4 | P2 | The active statistical prose correctly calls nested intervals diagnostics-only in the single-fit lane, but also describes the mixed predictive interval as certifying. It does not expose that successful nested samples can re-enter certification through `master_predictive/2`, and it overstates the separation between penalized fits and AIC evidence. | `verified_code_fact` | High |

## 1. Estimators, conditioning, and model selection

### 1.1 Fit-objective producer/consumer audit

| Quantity | Producer and mathematical meaning | Conditioning | Consumers and authority | Name accurate? | Calibration/tests | Evidence status | Proposed repair |
|---|---|---|---|---|---|---|---|
| `Problem.costv` / `StructureFitResult.cost` | `loop.py:78-117,169-197,281-304`: Baker–Cousins data deviance \(C(y;\theta)\). `cost` is `costv(theta_hat)` after optimization. | Same `ctx.x_used` and `ctx.counts_used`; contains no soft penalty. The evaluated `theta_hat` can nevertheless come from a penalized or hard-conditioned optimization. | `select.py:75-93` uses it for AIC/AICc/BIC/LRT; verdict and `cost_per_dof` consume it; candidate rows persist it (`driver.py:518-545`). Decision-bearing through model selection. | “data-only cost” is accurate; “MLE cost” would not be. | Formula tests: `tests/analyze/test_select.py:35-53`. No test asserts that AIC inputs are data-only MLEs. | `verified_code_fact` | STAT-1 add `estimator_kind` and `selection_eligibility`; STAT-2 produce a separate selection fit or refuse ordinary IC weighting. |
| `Problem.penalty` / `prior_penalty` | `loop.py:202-222,281-304`: \(\sum_j((\theta_j-\mu_j)/\sigma_j)^2\) over soft-constrained parameters, excluding hard-fixed coordinates. | Prior/branch-regularized estimator; targets come from `ctx.constraints`. | Optimizer objective, HESSE covariance, MINOS/profile, and every constrained bootstrap refit. It is persisted as fit provenance but not added to AIC. | “penalty” and `prior_penalty` are accurate. | Soft-constraint and hard-fix unit tests cover mechanics; no IC-validity calibration. | `verified_code_fact` | Record `constraint_origin`, `conditioning_fingerprint`, and `bootstrap_measure_kind`. |
| `Problem.objective` / `posterior_cost` | `loop.py:112-117,281-304`: objective \(C+\mathrm{penalty}\); `posterior_cost` records its value at `theta_hat`. | Soft-penalized when a soft constraint applies; otherwise equals data cost conceptually, though the field is `None` on unconstrained fits. | Optimizer; profile baseline uses it on soft fits (`errors/profile.py:21-33,98-103`). It does not drive AIC. | `posterior_cost` is defensible as an unnormalized negative-log-posterior kernel, not a Bayesian model evidence or normalized posterior quantity. | Objective mechanics are tested. No posterior calibration. | `verified_code_fact` | STAT-1 declare `estimator_kind=penalized_likelihood` and `formal_bayesian=false` for this path. |
| `fixed_pos`, `fixed_vals`, reduced `k` | `loop.py:194-199,225-277`: hard-fixed coordinates are removed from the optimized subspace; covariance is expanded with zero rows/columns; \(k\) is reduced. | Conditional on fixed values. Valid lower-dimensional likelihood-model semantics require values specified independently of the data and a common conditioning question. | Optimizer, HESSE, MINOS/profile, bootstrap, AIC parameter penalty. | Mechanical names are accurate; `fit_kind=branch_conditional` does not state constraint origin. | `tests/analyze/test_hard_fix.py:35-82` verifies freezing, covariance, and reduced `k`. No selection-validity campaign. | `verified_code_fact` + `open_design_question` | STAT-1 add `constraint_origin` and `selection_eligibility`; STAT-2 distinguish prespecified conditional models from data-dependent delivery rescues. |
| `fit_kind` | `loop.py:305-316`: precedence is `branch_conditional` for any hard fix, then `stokes_only`, then `soft_prior`, else `unconstrained`; `descriptor_only` is imposed after verdict (`driver.py:820-829`). | Provenance is coarse: it records the winning structural/constraint class but not origin, data dependence, or the constraints' values. | Persisted to ledger/CSV; read by triage and branch delivery; `score_models()` does not inspect it. | Incomplete rather than false. `branch_conditional` conflates prespecified and rescue conditioning. | Enum behavior is covered by hard-fix/stokes tests. | `verified_code_fact` | Keep the additive v2 field; add `constraint_origin`, `conditioning_fingerprint`, and `selection_eligibility`. |
| Near-gamma hard constraints | `driver.py:3375-3403`: if a prior/branch predicts an in-anchor unresolved target, gamma and amplitude can be hard-fixed before fitting. | Depends on previous campaign belief, q, resolution, branch model, and exposure; it is not selected from this scan's residual, but is not a universal fixed model either. | The complete `fit_one()` model set is fit under the resulting context. | `branch_conditional` is directionally accurate but insufficient. | Behavioral tests exist; no IC comparison calibration. | `suspected_inconsistency` | Owner must decide whether this is a prespecified conditional likelihood question eligible for IC weights. Record the origin either way. |
| Post-rail hard-fix escalation | `driver.py:3465-3507`: after a soft/Stokes fit rails gamma, it reruns `fit_one()` with gamma fixed to a branch value and adopts the rerun if it delivers a non-railed omega. | Explicitly data-dependent rescue/post-selection operation. The earlier fit is replaced, not mixed into the same returned selection table. | The adopted rerun supplies delivery, weights, intervals, and predictive artifact. | `branch_conditional` omits the crucial data-dependent rescue origin. | Rescue tests cover adoption/skip behavior, not post-selection IC validity. | `verified_code_fact` + `derived_statistical_consequence` | Ordinary AIC weights from this rerun must be marked ineligible unless STAT-2 defines and validates a conditional selection contract. |

### 1.2 Comparability of candidates entering one selection call

The broad hypothesis that production already combines arbitrary unconstrained and conditional fits
in every AIC table is **narrowed** by the code:

- `driver.fit_one()` calls `fit_structures_two_stage_hinted()` once with one `ctx`, then passes the
  returned results to `select_models()` (`driver.py:590-670`). Within that call, candidates share
  `ctx.mask`, `x_used`, `counts_used`, normalization, Baker–Cousins likelihood, and the constraint
  object.
- Hard-fix escalation reruns the entire `fit_one()` call under a replacement context and, when
  accepted, replaces the earlier fit dictionary (`driver.py:3465-3507`).
- The public `score_models(results)` API itself accepts any converged result and has no compatibility
  or `fit_kind` guard (`select.py:75-93`). Mixed inputs remain possible for direct callers or future
  assembly.
- Even under one shared context, constraints apply only to parameter names a structure contains,
  and `stokes_only` takes provenance precedence. Comparable data alone therefore do not establish
  a comparable estimator.

| Comparability axis | Current production fact | Consequence | Evidence status | Repair |
|---|---|---|---|---|
| Data mask | Candidate fits inside one `fit_one()` share `ctx.mask`; guideline region masking happens before fitting (`driver.py:3375-3461`). | Within-call AIC comparisons use the same kept data. Separate delivery reruns need a fingerprint to prove this. | `verified_code_fact` | Hash mask/grid into `conditioning_fingerprint`. |
| Energy grid | Candidates share `ctx.x_used`. | Within-call likelihood sample size `n` is common. | `verified_code_fact` | Persist the grid/mask fingerprint, not a claim. |
| Likelihood | `build_problem()` always requests `baker_cousins` (`loop.py:169-197`). | Within-call likelihood family is common. | `verified_code_fact` | Add `estimator_kind`; retain a strict comparison guard. |
| Normalization/exposure | Candidates evaluate the same context and exposure scale. | Within-call normalization is common. | `verified_code_fact` | Include normalization/exposure basis in the conditioning fingerprint. |
| Constraints | One `ctx.constraints` is shared, but structures contain different parameter sets; stokes-only and hard-fixed applicability can differ. | Same constraint object does not imply the same estimator or conditioning dimension. | `derived_statistical_consequence` | Record effective constraints per candidate. |
| Candidate generation | One-mode structures are always offered; elastic/QE and anti-Stokes forms are gated; a residual trigger can add two-mode models; guideline labels can filter; a previous-structure hint can reduce the run to one hinted phonon model after a no-phonon check (`structures.py:127-188`; `loop.py:494-562`; `driver.py:590-652`). | The compared candidate set is data-, card-, and history-dependent. AIC weights are conditional on the offered set and are not absolute model probabilities. | `verified_code_fact` + `derived_statistical_consequence` | Add `candidate_generation_route` and `model_set_fingerprint`; calibrate selection as the complete procedure. |
| Hard-conditioning origin | Pre-fit near-gamma conditioning and post-rail rescue both map to `branch_conditional`. | Weight eligibility cannot be inferred from stored `fit_kind`. | `verified_code_fact` | Add `constraint_origin` with at least prespecified/prior-predictive/data-dependent-rescue/post-selection vocabulary. |

### 1.3 Information criteria, weights, and LRT

| Quantity | Producer and mathematical meaning | Conditioning | Consumers and authority | Name accurate? | Calibration/tests | Evidence status | Proposed repair |
|---|---|---|---|---|---|---|---|
| `aic` | `select.py:75-93`: \(C(\hat\theta)+2k\). | At the supplied estimate. Ordinary Akaike interpretation requires a comparable data-likelihood estimator; soft-penalized estimates fail that condition in general. Hard-fixed estimates need the qualification above. | Selects `best`; source for `delta_aic`, weights, candidate table, predictive source weights. Primary authority. | Formula name is accurate only when eligibility conditions hold. | Formula tests only (`test_select.py:35-53`). | `verified_code_fact` + `derived_statistical_consequence` | STAT-2 compute selection IC from eligible selection fits; persist ineligibility rather than silently normalizing. |
| `aicc` | `select.py:54-56,87-93`: AIC plus \(2k(k+1)/(n-k-1)\), infinity if denominator is non-positive. | Same estimator caveat as AIC. | Stamped into selection diagnostics; not used to choose, weight, survive, predict, or certify. | Accurate formula. | Unit formula test. No recovery campaign. | `verified_code_fact` | Keep diagnostic until an `n/k` campaign supports an owner choice. |
| `bic` | `select.py:87-93`: \(C(\hat\theta)+k\log n\). | Same estimator caveat; no model-prior or Laplace-evidence contract is declared. | Stamped diagnostic only. | Accurate formula, not “Bayesian evidence.” | Unit formula test. | `verified_code_fact` | Label diagnostic; do not use as an evidence substitute. |
| `delta_aic`, `weight` | `select.py:82-93`: \(\Delta_i=AIC_i-\min AIC\); \(w_i=\exp(-\Delta_i/2)/\sum_j\exp(-\Delta_j/2)\). | Conditional on the offered, converged model set and its estimator semantics. Fixed from the observed dataset. | Survivor gate, auto-tier runner-up trigger, across-model pooling, predictive artifact, acquisition diagnostics, GUI/CSV. Decision-bearing. | “Akaike weight” is accurate; “posterior model probability” or unqualified “evidence” is not. | Normalization/ranking tests; no calibration as probability or design weight. | `verified_code_fact` | Add `model_weight_kind=akaike`, model-set fingerprint, and `formal_bayesian=false`. |
| `survives` | `select.py:32,75-93`: `weight >= 0.01`. | Offered/converged set and ordinary-AIC weights. | Only survivors receive full error attachment (`driver.py:659-669`) and enter legacy `pool_across_model()`. | Accurate local name; not the predictive-retained set. | Survivor-floor unit test. | `verified_code_fact` | Name the set `error_attachment_survivors`; fingerprint it. |
| Nested `stat`, `dof`, `p` | `select.py:59-71`: statistic is reduced cost minus full cost; reference is `chi2.sf(max(stat,0), dof)`. Pairing requires parameter-name subset. | Regular chi-square asymptotics assumed; code does not test boundary or null-identification conditions. | Persisted in selection diagnostics; no selection branch consumes the p-value. Diagnostic only. | “nested LRT” is accurate; an unqualified p-value overstates calibration for boundary components. | Pairing/formula tests (`test_select.py:64-106`); no null simulation. | `verified_code_fact` + `derived_statistical_consequence` | Add `probability_semantics=asymptotic_diagnostic`; calibrate boundary cases by parametric bootstrap before authority. |
| Legacy `Pooled.interval` / `modality` | `select.py:154-241`: weighted quantiles/KDE over survivor omega bootstrap samples, per-draw weight `Akaike weight / sample count`; models with no samples are absent. | Conditional on error-attachment survivors and on models carrying omega samples. | Verdict/modality; physical-row fallback if best lacks a bootstrap interval (`driver.py:674-685`). This is distinct from master `model_interval`. | “across-model pooled” is accurate only with the set and conditioning stated. | Modality/weight mechanics are tested; no model-averaged coverage. | `verified_code_fact` | Attach model-set, weight-kind, particle-kind, target conditioning, and calibration metadata. |

## 2. Interval and particle semantics

### 2.1 Within-model intervals and draws

| Quantity | Producer and mathematical meaning | Conditioning | Consumers and authority | Name accurate? | Calibration/tests | Evidence status | Proposed repair |
|---|---|---|---|---|---|---|---|
| Raw HESSE covariance / `omega0_err_hesse` | Backend covariance propagated by the delta method in `loop.py:281-362`. | Curvature of the actual optimizer objective: data-only when unconstrained, data plus penalty when soft-constrained, reduced subspace when hard-fixed. | Point diagnostics; cheap-tier steering seed; fast-tier interval producer; Laplace draw fallback. | “HESSE” accurate; it is not uniformly a data-only MLE sampling covariance. | Unit behavior plus synthetic width/coverage campaign. | `verified_code_fact` | Add estimator/constraint semantics to every HESSE-derived interval and particle set. |
| `hesse_interval` | `driver.py:342-361,697-708`: \(\hat\omega_0\pm\sigma_{\mathrm{HESSE}}\), nominal 0.68. | Same objective conditioning as HESSE. | Fast-tier `_pick_interval()` fallback and potentially convergence/certification. | Accurate with conditioning metadata. | Committed synthetic campaign; see §7. | `verified_code_fact` | Record `interval_kind=hesse_delta`, estimator kind, nominal level, and calibration target. |
| `hesse_trusted` | `driver.py:321-361`: finite positive sigma, valid covariance, gamma off-bound, and at least 200 kept counts. | A deterministic regime predicate, not an empirical posterior probability. | `_pick_interval()` and `_is_certifiable()` (`drive/loop.py:742-786`); auto escalation; terminal full-tier recheck. Decision-bearing. | “trusted” is stronger than its evidence supports across all contexts. | Synthetic evidence covers selected unconstrained single-model regimes; the 200-count threshold is documented in code, not a complete stratified campaign. | `suspected_inconsistency` | Rename semantically via metadata (`authority_kind=provisional_fast_certification`) and calibrate by exposure, constraint, model selection, and wall regime. |
| MINOS parameter intervals | `errors/profile.py:38-59`: asymmetric Ωd/Gamma intervals from MINOS on `_cost_closure`. | Data objective, soft penalty when present, hard-fixed coordinates held. | Agreement diagnostics and `profile_ok`; does not directly provide scalar omega certification. | Accurate, but “confidence interval” needs estimator conditioning. | Method/attach tests; ensemble evidence is mostly for scalar profile/HESSE/bootstrap. | `verified_code_fact` | Add `interval_kind=minos_objective_profile` and conditioning metadata. |
| `profile_interval` / `omega0_profile` | `errors/profile.py:21-33,62-155`: fix omega, re-minimize nuisance parameters, find \(\Delta\) objective = 1 crossings. Baseline is `posterior_cost` on soft fits, otherwise data cost. | Soft-penalized posterior-kernel profile or hard-conditioned profile, not always an ordinary likelihood profile. | Physical-row fallback and diagnostics. `_pick_interval()` accepts a profile when bootstrap is absent/unreliable, but `_is_certifiable()` rejects it if `interval_reliable is False` unless trusted HESSE is also present (`drive/loop.py:742-786`). | “profile interval” is accurate; “profile-likelihood” is inaccurate on soft-penalized fits. | Synthetic scalar-profile coverage in one model; no constraint-stratified campaign. | `verified_code_fact` | Add `interval_kind=likelihood_profile`, `penalized_profile`, or `conditional_profile`. |
| Profile-only certification | Code permits profile-only certification when profile bounds exist and `interval_reliable` is not false. The usual gamma-wall fallback has `interval_reliable=False` and therefore cannot certify. | Depends on the row's reliability flag, not merely interval source. | `_row_certifiable()` and stopping. | Current prose that profile is categorically certifying or categorically non-certifying is too broad. | Stop-path tests cover reliability gates; no profile-only coverage campaign. | `verified_code_fact` | Test and document the exact allowed cases; do not promote the gamma-wall fallback. |
| `freq_interval`, `omega0_samples`, `parameter_samples` | `errors/bootstrap.py:159-289`; attached in `errors/__init__.py:24-96,116-218`. Poisson parametric bootstrap from the fitted mean; percentile interval over successful refits. | Refits repeat the same objective and fixed values (`bootstrap.py:108-148`), so three procedures exist: unconstrained, soft-penalized, and hard-conditioned bootstrap. Model selection is not rerun inside a replicate. | Best physical interval; legacy pooled interval; predictive particles; agreement; confidence subscore. Decision-bearing. | “parametric bootstrap” is accurate but underspecified. `freq_interval` overstates ordinary frequentist-MLE semantics for penalized/conditional variants. | Synthetic campaign is primarily an unconstrained, fixed-model, correctly specified generator. | `verified_code_fact` + `derived_statistical_consequence` | Add `bootstrap_measure_kind` = `unconstrained_parametric_bootstrap`, `penalized_parametric_bootstrap`, or `conditional_parametric_bootstrap`. |
| `bayes_interval`, `bayes_parameter_samples` | `errors/bayes.py:109-190`; equal-weight resampling of one dynesty posterior. | Uniform optimizer-bound parameter support; no `ctx.constraints`; optional omega Gaussian factor in `loglike`. | Single-fit diagnostics and, unexpectedly, `bounded_parameter_draws()` gives successful Bayes samples first priority in the master artifact. | “Bayesian credible interval” is accurate for the implemented prior kernel; its prior provenance is incomplete. | Slow broad coverage test and sampler unit tests; no promotion-quality coverage/prior-sensitivity campaign. | `verified_code_fact` | Add prior fingerprint, `formal_bayesian`, particle-measure kind, and prohibit certification/design use until gates pass. |
| `bayes_freq_ratio`, `hesse_freq_ratio`, agreement flags | `errors/agree.py:40-97`: ratios of half-widths plus heuristic flags. | Checks numerical availability, not equality of model, constraints, priors, projection, data, or window. | Persisted diagnostic/reasons; not a direct certification gate. | “agreement” is incomplete; it is width agreement, not commensurability. | Ratio/flag tests, no cross-paradigm calibration. | `verified_code_fact` | STAT-6 require matching semantic fingerprints or abstain with a reason. |

### 2.2 Exact interval authority by tier

| Tier/path | Produced interval | What can seat convergence | Evidence status |
|---|---|---|---|
| `cheap` | Rough HESSE point error only; `interval`, profile, and `hesse_interval` are absent and `interval_reliable=False` (`driver.py:710-716`). | Nothing. Quicklook error can seed acquisition only. | `verified_code_fact` |
| `fast` / unpromoted `auto` | `hesse_interval`; `interval_reliable=False`; `hesse_trusted` separately recorded (`driver.py:697-708`). | Trusted HESSE can seat provisional convergence; terminal certification forces a full pass before a final met result (`drive/loop.py:4410-4480`). | `verified_code_fact` |
| `full` | Bootstrap `interval`, MINOS, and scalar profile for survivors. | Reliable bootstrap first. A profile can be selected, but a gamma-wall row with `interval_reliable=False` is blocked by `_is_certifiable()`. | `verified_code_fact` |
| `bayes` physical row | Full frequentist layer plus Bayes on preliminary best. | The physical-row ladder remains bootstrap/profile/trusted HESSE; `_diagnostic_interval()` can display Bayes but `_pick_interval()` never selects it (`drive/loop.py:742-851`). | `verified_code_fact` |
| Pooled master | Predictive artifact `omega_interval` copied to `model_interval`; `model_robust` initially means interval present and non-fork (`driver.py:2993-3012`). | `_master_obs_from_row()` applies the external calibration and sets both `reliable` and `certifiable` to the resulting `model_robust` (`drive/loop.py:965-1020`). | `verified_code_fact` |

The single-fit statement “Bayesian intervals never certify” is therefore locally true but globally
incomplete: successful Bayesian parameter samples can enter `master_predictive/2`, its quantiles can
become `model_interval`, and that interval can certify without the consumer inspecting its particle
basis.

## 3. Bayesian evidence

### 3.1 Production and prior semantics

| Quantity | Producer and mathematical meaning | Conditioning | Consumers and authority | Name accurate? | Calibration/tests | Evidence status | Proposed repair |
|---|---|---|---|---|---|---|---|
| Uniform box parameter prior | `errors/bayes.py:133-149` maps the unit cube linearly onto `Problem.bounds`. `loop.py:64-74,178-188` sets Omega-d upper support from `max(x)` and amplitude/background support from observed `max(counts)`. | Support is scan-window/design dependent and partly observed-data dependent. | Defines posterior samples and `log_evidence`. | Not explicitly published as a prior. Calling it merely “bounds-only/uninformative” is inaccurate for evidence. | Sampler tests verify finite output, not cross-model/window comparability. | `verified_code_fact` + `derived_statistical_consequence` | STAT-1 publish prior kind/support/fingerprint. STAT-5 replace with a scientific prior registry before evidence comparison. |
| Optional omega Gaussian | `errors/bayes.py:151-160` adds a normalized untruncated Normal log-density to `loglike`; prior transform remains the optimizer box. | Posterior kernel is uniform-box × likelihood × Normal(omega). If interpreted as a truncated omega prior on that box, its missing truncation normalizer changes evidence while leaving posterior shape unchanged. | Dormant in production because `driver._bayes_prior_for()` returns `None` (`driver.py:470-474`). | “literal Normal prior” is incomplete with bounded support. | No active production calibration. | `derived_statistical_consequence` | Implement the prior in a normalized prior transform or include the exact support normalizer; test evidence invariance/sensitivity. |
| `log_evidence` | `errors/bayes.py:164-190`: final dynesty `res.logz[-1]`. | Evidence under the box-support kernel above, for one sampled hypothesis. | Attached to `within_model` (`errors/__init__.py:77-88,190-201`), copied into `FitOutcome`, ledger/results CSV (`driver.py:737-769,3026-3030,3686-3692,3836-3848`), and exposed by workspace views (`workspace_api/views.py:265-270`). No active selection or weighting consumer exists. | Mathematically a log integral for the implemented kernel; the unqualified field omits prior and model-set provenance. | Finite-output tests only. | `verified_code_fact` | Add evidence/prior/model fingerprints; prohibit model-weight interpretation until all retained models are sampled under comparable normalized priors. |
| `log_evidence_err` | `errors/bayes.py:90-99,164-190`: dynesty `logzerr`; runs fail `bayes_ok` when above the coarse threshold. | Same prior/model conditioning as `log_evidence`; uncertainty for a single run, not model-weight uncertainty. | Persisted/displayed beside evidence; no model-selection consumer. | Accurate locally. | Threshold behavior tests; no repeated-seed stability campaign. | `verified_code_fact` | STAT-5 propagate into model-weight uncertainty and require repeated-run stability. |
| `bayes_ess`, `bayes_ok`, `bayes_fail_reason` | `errors/bayes.py:84-106,168-190`: Kish ESS from nested weights plus gates for finite evidence, max iterations, ESS, evidence error, Omega-d rail, and nondegenerate interval. | One model/run. | Determines whether Bayes samples outrank bootstrap in `bounded_parameter_draws()`; diagnostic fields are persisted. | `bayes_ok` sounds broader than “this run passed these numerical gates.” | Unit tests. | `verified_code_fact` | Rename semantically through `authority_kind=numerically_accepted_diagnostic_posterior`; keep scientific eligibility separate. |
| Sampled model set | `driver.py:659-669,719-769`: Bayes is requested only for `prelim.best`; if final best changes it is reported unsampled. | Preliminary observed-data selection. | Only the successful sampled model can supply nested particles; every other model falls back. | No field states “one-model-only Bayesian run.” | Bayes-tier tests cover best-change fallback. | `verified_code_fact` | STAT-5 require all retained models, explicit model priors, null inclusion, and evidence uncertainty before Bayesian model averaging. |

`isar/session/branch_tracking.py:691-700` also has a local variable named `log_evidence`, but it is
the forward-algorithm normalizer for the branch state sequence. It is not the persisted dynesty
field and is not a downstream use of Bayesian model evidence.

### 3.2 Bayesian conclusion

Current `log_evidence` production is useful as a sampler diagnostic and future seam. It cannot
currently provide Bayesian model weights because:

1. only the preliminary-best hypothesis is sampled;
2. model priors are absent;
3. parameter support is not a fixed scientific prior registry;
4. some bounds depend on the observed scan;
5. the optional omega-prior seam needs support normalization;
6. evidence uncertainty is not propagated into weights; and
7. the null/target-absent model is not guaranteed to be in the sampled set.

This verifies suspected inconsistency 4 without implying that dynesty itself is wrong.

## 4. Predictive mixture, target conditioning, and certification

### 4.1 Particle choice and model-set consistency

`bounded_parameter_draws()` selects the strongest available source **separately for each model**
(`session/predictive_artifact.py:147-200`):

1. successful `bayes_parameter_samples` → `nested_sampling`;
2. otherwise bootstrap `parameter_samples` → `parametric_bootstrap`;
3. otherwise a multivariate Normal around the fit/HESSE covariance → `laplace_hesse`;
4. otherwise the model is dropped.

This priority interacts with two different weight floors:

| Stage/set | Rule | Consequence | Evidence status |
|---|---|---|---|
| Scored set | Every converged result; ordinary AIC weights normalized over that set (`select.py:75-93`). | Source weights are conditional on all converged/offered candidates. | `verified_code_fact` |
| Error-attachment survivors | `weight >= 0.01` (`select.py:32,75-93`; `driver.py:659-669`). | Only these normally receive bootstrap/profile; preliminary best alone receives Bayes. | `verified_code_fact` |
| Legacy across-model pool | Survivors that actually carry bootstrap omega samples (`select.py:154-241`). | Missing-sample survivor mass is omitted from the pooled interval. | `verified_code_fact` |
| Predictive producer | Starts from **all scores**, not `prelim.survivors` (`driver.py:806-818`; `predictive_artifact.py:310-380`). | A below-0.01 model with covariance can enter through Laplace while survivors enter through bootstrap/Bayes. | `verified_code_fact` |
| Predictive completeness | A dropped model is individually negligible below 0.005, total pruned mass must be ≤0.01, and dropped draw mass ≤0.01 (`predictive.py:140-142`; `predictive_artifact.py:381-400`). | “Complete for EIG” is a pruning-completeness check, not a homogeneous-measure or target-completeness check. | `verified_code_fact` |
| Target projection | Mirrors every retained predictive model and weight; no new weight threshold (`predictive_artifact.py:552-630`). | Projection set equals predictive retained set when complete, but target presence remains per draw/model. | `verified_code_fact` |
| Certification omega set | Only retained models with non-empty `omega0_draws` contribute to `omega_interval`; the cumulative weights are renormalized (`predictive_artifact.py:403-415`). | Target-absent mass is conditioned away. | `verified_code_fact` |

### 4.2 Model-averaged interval audit

| Quantity | Producer and mathematical meaning | Conditioning | Consumers and authority | Name accurate? | Calibration/tests | Evidence status | Proposed repair |
|---|---|---|---|---|---|---|---|
| Per-model `source_weight` / `weight` | `predictive_artifact.py:310-383`: observed-data ordinary Akaike weight, then renormalized over models with usable draws/manifests. | Conditional on offered/converged models and successful artifact construction. Weight uncertainty is absent. | Predictive evaluator, outer scorer, omega mixture, entropy. | `weight` is ambiguous; it is not a posterior model probability. | Contract/normalization tests only. | `verified_code_fact` | Add `model_weight_kind=akaike`, source selection fingerprint, retained-set fingerprint, and `formal_bayesian=false`. |
| Per-model particles | `predictive_artifact.py:147-200,327-379`. | Model-specific nested posterior, bootstrap sampling distribution, or Laplace approximation; constraints differ as described in §2–3. | Expected-count predictions, omega draws, target projection, current action/Q scorer, future EIG evaluator. | `draw_basis` states algorithm, but not measure/conditioning. | Shape, determinism, and evaluator parity tests; no mixed-measure calibration. | `verified_code_fact` | Add `particle_measure_kind`, `conditioning_fingerprint`, and homogeneous-measure eligibility. |
| Model-selection uncertainty | No producer. Bootstrap simulates counts and refits the same structure only (`bootstrap.py:159-289`). Model weights are fixed once from observed data. | Selection and weights are conditioned on the observed dataset. | Implicitly omitted from `omega_interval` and predictive scoring. | No field declares the omission. | No full-pipeline refit/reselect campaign. | `verified_code_fact` | Decide whether design/certification requires re-selection per replicate; calibrate the complete update. |
| `omega_interval` | `predictive_artifact.py:403-415`: 0.16/0.50/0.84 weighted quantiles of all finite `omega0_draws`; within-model particles get equal share of model weight. Models with no omega draws are skipped, then cumulative mass is divided by its final sum. | Conditional on retained draw-cap output and on the target/local omega existing. Null/no-target mass is omitted. Particle paradigms can be mixed. | Copied to `FitOutcome.model_interval` (`driver.py:2993-3012`) and results CSV. | Unqualified name is inaccurate. At minimum it is `conditional_target_present_mixed_particle_omega_interval`. | No coverage campaign for this producer. | `verified_code_fact` + `derived_statistical_consequence` | Split target-presence support from conditional omega distribution; create homogeneous shadow producer; prohibit certification until calibrated. |
| `model_entropy_nats` / `model_entropy` | `predictive_artifact.py:415-437`: \(-\sum_m w_m\log w_m\) over all retained model weights. | Entropy of renormalized retained Akaike weights, including a retained null model. | Persisted/displayed and stopping diagnostics; no direct threshold in convergence. | “model entropy” is underspecified; not Bayesian posterior entropy and not identity entropy. | Formula behavior tests only. | `verified_code_fact` | Add `model_weight_kind` and `probability_semantics=akaike_relative_weight`. |
| `model_robust` | Producer `driver.py:2993-3012`: true iff `model_interval` exists and fit modality is not `fork`. Consumer may clear it when calibration fails (`drive/loop.py:978-999`). | Does not test homogeneous particles, target-presence support, model-set consistency, or interval coverage. | Sets `reliable` and `certifiable` in master observation; stopping reads it. | Too strong. It is an eligibility bit under current mechanics, not demonstrated robustness. | Control-flow tests only. | `verified_code_fact` | Replace authority via additive `authority_kind`; require explicit gates rather than renaming the legacy field. |
| External calibrated half-width | `drive/loop.py:978-999` calls `Calibration.calibrated_halfwidth`; calibration is multiplicative inflation \(k(hi-lo)/2\) (`drive/acquisition/calibration.py:29-104`). | Uses exposure bucket if present, otherwise global. It does not inspect interval paradigm or draw basis. | Master `halfwidth`, convergence, stop decision. | “calibrated” is accurate only for the interval population represented by the calibration artifact. | Current artifact is based on DFT/human comparison of legacy reported intervals, not mixed-predictive intervals (`calibration/README.md`; `phonon_dft_deterministic.json`). | `verified_code_fact` + `derived_statistical_consequence` | Add `calibration_target` and interval/measure fingerprint; refuse mismatched calibration. |

The result verifies suspected inconsistencies 2 and 3. Homogenizing particles fixes only the first
layer. It does not by itself propagate model-selection uncertainty or establish a sequential
conditioning interpretation.

### 4.3 Target-present versus target-absent semantics

The code already supports a no-phonon predictive model as a valid physical state:
`tests/session/test_master_predictive.py:349-364` constructs one, and
`build_target_projection()` treats a model with no modes as target absent rather than invalid
(`predictive_artifact.py:513-528`). The normal structure enumerator offers phonon-containing
families (`structures.py:127-188`); the hint shortcut fits a no-phonon baseline only as a gate and
does not return it into ordinary selection (`loop.py:494-537`). The audit therefore does **not**
prescribe a new physical null structure. It records the narrower contract failure:

| Required quantity | Current representation | Current authority | Conclusion | Evidence status | Repair |
|---|---|---|---|---|---|
| \(P(\text{target present}\mid data)\) or non-Bayesian support analogue | `target_projection/2` carries `target_present_by_draw` and model weights, but no consumer reduces them to a declared target-presence support. | None. Projection is persisted/read only. | The ingredients can represent target absence, but no authoritative scalar semantics exist. | `verified_code_fact` | STAT-4a define target-presence mass/support and its probability semantics. |
| \(p(\omega\mid\text{target present},data)\) or empirical analogue | Current `omega_interval` silently conditions on models with omega draws and renormalizes. | Master certification. | Conditional quantity exists but is not named as conditional. | `verified_code_fact` + `derived_statistical_consequence` | Publish an explicitly conditional distribution/interval, measure kind, and denominator mass. |
| Unconditional decision uncertainty | No unified producer combines target absence, conditional omega, model uncertainty, and decision loss. | Stopping approximates identity through track state, but not from the predictive mixture. | Missing. A scalar omega interval cannot encode target absence. | `verified_code_fact` | Keep presence/support and conditional omega separate; define the decision rule only after calibration. |

### 4.4 Certification and statistical stopping

`_master_obs_from_row()` turns the calibrated mixed interval into the sole decision-bearing master
assessment. `GridQ.converged` then requires a confirmed track where configured, a non-null certifying
half-width no larger than tolerance, and an optional independent crossing fingerprint
(`stopping.py:304-356`). Static/posterior-off campaigns allow `track_state is None`, so branch
probability fields are not a universal target-presence gate.

The following stops do not claim to be posterior or frequentist tests, but they are
decision-bearing and therefore part of the statistical semantics:

| Stop/redirect | Producer and rule | Consumer/effect | Name accurate? | Calibration evidence | Evidence status | Proposed repair |
|---|---|---|---|---|---|---|
| `info_saturated` | `stopping.py:361-377`: if not converged, call `voi_saturated()` using current certifying or diagnostic sigma, counts spent, and last production cost. | Diagnostic and component of `exposure_saturated`; can redirect/terminate local acquisition routes. | Heuristic name is acceptable if kept non-probabilistic. | No complete realized-utility calibration. | `verified_code_fact` | Record `authority_kind=heuristic_stop`; include in full-pipeline campaign. |
| `interval_floored` | `stopping.py:379-421`: comparable update window, exposure-ratio floor, and insufficient fractional half-width improvement. | `exposure_saturated`, diagnostics, ambiguity exhaustion. | Accurate empirical brake. | Threshold tests, no scientific operating-characteristic campaign. | `verified_code_fact` | Calibrate false saturation under mixed/changed interval sources. |
| `identity_saturated` | `stopping.py:441-503`: comparable identity updates, exposure growth, and stalled improvements in both identity entropy and target probability. | Can exhaust branch ambiguity and block further same-q identity work. | Accurate as heuristic, not a posterior test. | Behavioral tests only; inputs themselves are uncalibrated. | `verified_code_fact` + `suspected_inconsistency` | Reliability-calibrate inputs and the saturation rule. |
| `exposure_saturated` | `stopping.py:424-429`: repeated instrument clamp or interval/info saturation. | Redirects/limits further same-q dwell. | Accurate controller state. | Behavioral tests. | `verified_code_fact` | Keep separate from statistical confidence; record reason component. |
| Same-q strategy ceiling | `stopping.py:431-438`: production count reaches policy maximum. | Bounds dwell; can contribute to ambiguity exhaustion only with saturation. | Accurate policy ceiling. | No calibration required, but owner policy is required. | `verified_code_fact` | Keep explicitly policy-based. |

## 5. Confidence, branch assignment, and tracking

| Quantity | Producer and mathematical meaning | Conditioning | Consumers and authority | Name accurate? | Calibration/tests | Evidence status | Proposed repair |
|---|---|---|---|---|---|---|---|
| `confidence` | `confidence.py:27-80`: geometric mean of structure, interval tightness, paradigm-regime, prior-distance, and energy-provenance sub-scores. | Heuristic maps; prior absence is neutral. | Verdict/report/GUI/gold-set triage. It does not seat certification. | Acceptable as “confidence score”; inaccurate if read as probability of correctness. | Gold evidence shows mean 0.760 for resolved-close vs 0.264 for resolved-far (`docs/assets/phase3_gold/FINDINGS.md:103,153`); no reliability curve, Brier score, or probability calibration. Unit tests check range/monotonicity. | `verified_code_fact` | Add `probability_semantics=heuristic_triage_score`; retain user-facing warning. |
| Phase-A `branch_posterior` | `branch_assignment.py:305-338`: per-feature Gaussian branch density × `(anchor count + 1)`, plus fixed `NEW_LEVEL=0.01`, then normalization; optional target-energy likelihood ratio. | Conditional on versioned branch beliefs, occupancy pseudo-count, feature error/slack, and fixed new/spurious score. | `acoustic_qoi`/branch QoI, delivery thresholds, branch banking, persisted outcome, candidate rejection logic. | It is a normalized assignment distribution under the declared scoring construction; “posterior” overclaims formal/calibrated Bayesian status because the new-state scale and occupancy terms are heuristic and no reliability validation exists. | Extensive behavior/threshold tests and campaign examples; no calibration. | `verified_code_fact` + `derived_statistical_consequence` | Add `probability_semantics=normalized_assignment_weight`, scorer version, and calibration version. |
| `branch_weight`, `p_acoustic`, `p_branch` | Selected feature's normalized branch weight (`branch_assignment.py:347-447`). Delivery also requires entropy ≤0.7, dominance ≥0.7, probability ≥0.6, and resolvability. | Same Phase-A scoring model; selection of a feature is a separate operation. | Delivery/withholding, persisted outcome, dispersion-prior admission. | “weight” is accurate; `p_*` notation suggests probability calibration that is absent. | Threshold behavior tests only. | `verified_code_fact` | Preserve legacy fields; add semantic metadata and calibrate thresholds. |
| `assignment_entropy` | Shannon entropy over the full normalized assignment map, including new/spurious (`branch_assignment.py:343-369`; tracker version at `branch_tracking.py:461-476,740-744`). | Depends on which assignment model produced the probabilities. | Delivery gate, reports, track/frontier diagnostics. | Accurate if paired with its probability semantics and state set. | Behavioral tests. | `verified_code_fact` | Publish state vocabulary, scorer revision, and semantics. |
| Tracker state weights | `branch_tracking.py:159-220,319-426,603-744`: forward/backward normalization over state scores combining truncated-density compatibility, missing/unassigned penalties, clean-null relaxation, reliability factor, ordering, continuity, and character-handoff penalties. | Versioned factor/scoring model over the whole objective path; some factors are likelihood-like, some deliberate penalties. | Candidate probabilities, MAP path, track/frontier states. | Useful probability-like marginal weights; not a calibrated physical posterior. | Extensive regression/behavior tests; no reliability campaign against labeled trajectories. | `verified_code_fact` + `derived_statistical_consequence` | Add `probability_semantics=normalized_factor_graph_score` unless/until a fully normalized generative model and reliability campaign are established. |
| `target_probability` | `branch_tracking.py:700-744`: marginal weight assigned to the selected candidate as the declared target branch. | Conditional on candidate set, hypotheses, energy domain, scoring revision, path, and penalties. | `local_identity_clear` threshold 0.85, track states, identity saturation, stopping diagnostics. Decision-bearing indirectly. | “probability” lacks the necessary qualifier. | Threshold/regression tests; no empirical calibration. | `verified_code_fact` | Add semantic and calibration metadata; do not interpret 0.85 as 85% frequentist reliability without evidence. |
| `identity_entropy` | `branch_tracking.py:465-476`: entropy of acoustic/optic weights conditional on known-branch mass; excludes `new`. | Conditional on the normalized tracker score and on known identity. | `local_identity_clear`, track state, identity saturation, reports. | Accurate when its conditional state space is declared. | Behavioral tests. | `verified_code_fact` | Publish `conditioning=known_branch` and state vocabulary. |
| `local_identity_clear` / `track_state` | `branch_tracking.py:479-515`: selected candidate, `target_probability >= 0.85`, and identity entropy ≤ configured threshold; then frontier connectivity assigns confirmed/ambiguous/missing/provisional/blocked. | Thresholded scorer output plus path connectivity. | Target projection completeness, master matching, convergence eligibility, acquisition routing. | Operational names are accurate; not statistical calibration claims. | Strong control-flow tests, no operating-characteristic campaign. | `verified_code_fact` | Keep operational vocabulary; calibration must precede any probabilistic interpretation. |

This verifies suspected inconsistency 6 in the limited sense that the fields are not calibrated
probabilities. It does not say the score distributions are useless; they are central and
well-tested controller inputs.

## 6. Predictive contract inventory

### 6.1 `isar.master_predictive/2`

Producer: `session/predictive_artifact.py:272-439`. Strict reader/evaluator:
`predictive.py:403-618`. Current readers are the workspace facade, pure evaluator, and drive loop;
the current outer action/Q scorer consumes the stored model matrices. Alternative-window EIG does
not yet consume the artifact (`docs/EIG_STEERING_DESIGN.md:13-15`).

| Field(s) | Mathematical/contract meaning and conditioning | Current consumers/authority | Name accuracy and calibration | Evidence status | Proposed repair |
|---|---|---|---|---|---|
| `schema`, `contract` | Artifact schema and payload revision (`isar.master_predictive/2+physical_only.1`). | Strict validation, cache fingerprint, workspace/evaluator/loop compatibility. Contract authority, not statistical authority. | Accurate; schema tests are strong. | `verified_code_fact` | Any semantics addition is additive and must bump the contract/fingerprint as required. |
| `models` | Retained model mixture after draw/manifest availability filtering and weight renormalization. | All predictive consumers. | “models” accurate; the retained-set semantics are incomplete. | `verified_code_fact` | Add model-set and source-selection fingerprints. |
| `energy_meV` | Ordered kept fitted grid for stored `expected_counts`. | Validator, fitted-grid diagnostics, evaluator echo. | Accurate. | `verified_code_fact` | Include in conditioning fingerprint; no statistical repair. |
| `evaluation_context` | Center, Gaussian/Lorentzian resolution widths, temperature, optional QE width. | Rebuilds forward model on arbitrary grids. | Accurate physical conditioning. | `verified_code_fact` | No change beyond fingerprint linkage. |
| `exposure_basis` | Request/exposure units, conversion, fit-reference exposure, per-bin monitor and stored ratio, support/homogeneity declarations, member/point counts. | Strict validation and arbitrary-grid scaling. | Accurate after EIG-2.5; parity tests are strong. | `verified_code_fact` | No STAT behavior change. Reference it from future design artifacts instead of copying. |
| `evaluator_version`, negative-prediction fields | Exact forward evaluator and refusal tolerance/rule. | Validator, fingerprint, evaluator. | Accurate. | `verified_code_fact` | Source-link future design artifacts. |
| `completeness.source_model_weight_total` | Sum of positive input Akaike weights. Normally 1 over scored converged models. | Validation/diagnostic. | Accurate but weight kind unstated. | `verified_code_fact` | Add `model_weight_kind`. |
| `completeness.retained_model_weight_total`, `pruned_model_weight`, `dropped_models` | Mass surviving/dropped because a fit, draw basis, or manifest was unavailable. | EIG completeness gate and diagnostics. | Accurate construction completeness; not target or measure completeness. | `verified_code_fact` | Split construction, measure-homogeneity, and target completeness. |
| `completeness.model_weight_floor`, `max_pruned_mass`, `max_dropped_draw_mass`, `predictive_complete_for_eig` | ADR-014 thresholds and Boolean construction gate. | Strict validator refuses false. | `predictive_complete_for_eig` overclaims: it does not check homogeneous particles, comparable weights, target projection, or calibration. | `verified_code_fact` | Keep legacy field; add explicit homogeneous-design eligibility and reasons. |
| `predictive_basis_fingerprint` | Hash of physical models/draws/grid/context/exposure/pruning/evaluator basis; excludes target projection and seed. | Strict validation, joins, future EIG cache/refusal. | Accurate after EIG-2.5. | `verified_code_fact` | Future shadow artifact must source-link this fingerprint. |
| `uncertainty_provenance.draw_basis` | Sorted set of per-model algorithm labels; multiple labels explicitly reveal mixing. `deterministic` and `seed` record generation reproducibility. | Diagnostics only; certification ignores it. | `draw_basis` is accurate algorithm provenance, not measure semantics. | `verified_code_fact` | Add `particle_measure_kind`, constraint and formal-Bayesian fields; make authority consumers inspect them. |
| `seed` | Master-seeded draw generation/thinning seed. | Reproducibility. | Accurate. | `verified_code_fact` | Separate producer seed from future CRN seed; no CRN work in STAT. |
| `omega_interval` | Mixed weighted 16/50/84% quantiles conditional on models with omega draws. | Becomes certifying `model_interval`. | Inaccurately unconditioned; uncalibrated. | `verified_code_fact` + `derived_statistical_consequence` | Deprecate as authority additively; publish explicit conditional homogeneous interval later. |
| `model_entropy_nats` | Shannon entropy of retained normalized Akaike weights. | Persisted diagnostic/stopping output. | Needs weight-kind qualifier. | `verified_code_fact` | Add probability semantics and model-set fingerprint. |
| `diagnostics` | `predictive_score()` result using a fixed seed and 128 simulations at production. | Current action/Q ranking diagnostics. | Method field `posterior_predictive_mc` can be inaccurate for bootstrap/Laplace particles. | `verified_code_fact` | Add design-measure metadata; do not reuse this as EIG-3 authority. |

Per-model fields:

| Field(s) | Mathematical/contract meaning and conditioning | Current consumers/authority | Name accuracy and calibration | Evidence status | Proposed repair |
|---|---|---|---|---|---|
| `label`, `identity_label` | Fit-model identity; `identity_label` currently defaults to label and is not the authoritative target projection. | Fingerprint, scorer/evaluator labels. | Accurate with documented limitation. | `verified_code_fact` | Keep separate from target identity. |
| `source_weight` | Input ordinary Akaike weight before artifact pruning. | Fingerprint and diagnostics. | “source weight” accurate; kind missing. | `verified_code_fact` | Add `model_weight_kind=akaike`. |
| `weight` | `source_weight` renormalized over retained artifact models. | All mixture/scoring operations. | Ambiguous; not posterior probability. | `verified_code_fact` | Add probability semantics and denominator/model-set fingerprint. |
| `draw_basis` | `nested_sampling`, `parametric_bootstrap`, or `laplace_hesse`. | Diagnostics/evaluator echo; ignored by certification. | Accurate algorithm name, incomplete statistical meaning. | `verified_code_fact` | Add `particle_measure_kind`, estimator/constraint metadata, `formal_bayesian`. |
| `draw_weighting`, `draw_weights` | Equal-weight resampled today; explicit weights are allowed by the contract. | Validator and evaluator/scorer weighting seam. | Accurate. | `verified_code_fact` | Future scorer must honor rather than infer it. |
| `draw_ids` | Stable IDs after finite filtering/thinning. | Validation, future CRN/join seam. | Accurate. | `verified_code_fact` | No CRN implementation in STAT; retain source linkage. |
| `parameter_names`, `parameter_draws` | Named full-parameter particles from the selected per-model measure. Nonnegative physical parameters are clipped after sampling. | Forward evaluator and stored predictions. | Accurate shape; clipping changes a raw Laplace/bootstrap particle measure and is not separately named. | `verified_code_fact` | Record clipping/transform rule in particle semantics. |
| `model_manifest` | Component structure needed for pure re-evaluation. | Strict validator/evaluator. | Accurate. | `verified_code_fact` | No statistical change. |
| `omega0_draws` | Mode nearest the fit's local delivered omega for each draw; empty for no-mode models. It is not campaign-target projection. | Current outer scorer and certifying omega mixture. | Name is physically local but easy to misread as target omega. | `verified_code_fact` | Stop using it for target certification/design; join `target_projection/2`. |
| `mode_energies` | Every fitted mode energy per draw in composite order. | Target projection and evaluator echo. | Accurate. | `verified_code_fact` | This is the correct physical source for future projections. |
| `expected_counts` | Forward mean counts per draw on stored grid at stored incremental exposure. | Current predictive scorer and parity tests. | Accurate. | `verified_code_fact` | Future design scoring should use pure reevaluation on candidate grid, source-linked to this model/draw. |
| `source_draw_count`, `retained_draw_count`, `dropped_draws`, `draw_mass_retained`, `draw_cap`, `thinned_from_draw_count` | Construction diagnostics for nonfinite filtering and deterministic cap thinning. | Completeness and audit diagnostics. | Accurate for equal-weight draws; “mass retained” for deterministic thinning is a construction proxy, not lost probability mass. | `verified_code_fact` | Add measure-aware completeness semantics if explicit weights are used. |

`predictive_score()` (`predictive.py:38-107`) retains no-phonon models in joint/model
information through a missing-omega state, but computes omega-only information only on finite omega
draws. Its `method="posterior_predictive_mc"` is therefore a naming issue, not proof that the
particles are posterior draws.

### 6.2 `isar.target_projection/2`

Producer: `session/predictive_artifact.py:444-630`, after triage and connected tracking. Publisher:
`session/driver.py:3258-3303`. Current semantic consumer: workspace facade read only
(`workspace_api/workspace.py:459-472`). There is no current steering/scoring consumer.

| Field(s) | Mathematical/contract meaning and conditioning | Current consumers/authority | Name accuracy and calibration | Evidence status | Proposed repair |
|---|---|---|---|---|---|
| `schema` | `isar.target_projection/2`. | Workspace/read compatibility; future EIG join. | Accurate. | `verified_code_fact` | Keep additive contract. |
| `qoi`, `projection_version` | QoI `omega0` under resolution-bounded nearest-authoritative-mode projection. | Future semantic routing. | Accurate. | `verified_code_fact` | Future scorer must require the registered version. |
| `predictive_basis_fingerprint` | Joins projection to the exact physical predictive basis. | Staleness/future scoring. | Accurate. | `verified_code_fact` | Required source link for STAT-3. |
| `reference_energy_meV` | Post-triage/post-tracker authoritative delivered target energy used to choose a mode. | Projection construction/audit only. | Accurate; it is itself a selected estimate, not truth. | `verified_code_fact` | Record the selection/track semantics alongside it, as authority block already does. |
| `mode_selection` | Resolution FWHM, maximum support distance, ambiguity tolerance, and versioned rule constants. | Re-derivation and completeness. | Accurate. | `verified_code_fact` | No STAT change. |
| `authority.action_role`, `master_revision`, `track_revision`, `track_state`, `candidate_id`, `candidate_state`, `selected_candidate_index`, `calibration_fingerprint`, `policy_fingerprint` | Exact operational state against which the projection is valid. | Future staleness guard. | Accurate and strong after EIG-2.5. | `verified_code_fact` | Future design artifact must copy the fingerprint/reference, not duplicate model data. |
| `complete`, `reason` | Fail-closed declaration; incomplete projection clears all target QoI draws. | Workspace display; future eligibility. | Accurate construction/identity completeness, not statistical calibration. | `verified_code_fact` | Add target-presence measure semantics separately; do not reinterpret completeness as probability. |
| Per-model `label`, `identity_label`, `weight` | Mirrors retained physical model identity and renormalized artifact weight. | Future design mixture. | Weight kind remains unstated. | `verified_code_fact` | Inherit/source-link model weight metadata. |
| `target_present_by_draw` | Boolean: a uniquely supported target mode was found for this draw; no-mode model yields false. | No current consumer. Future target-presence support. | Accurate Boolean projection result. | `verified_code_fact` | STAT-4a define aggregation and probability semantics. |
| `target_qoi_draws` | Selected target-mode omega per draw or `None`; entirely cleared on incomplete projection. | No current consumer. Future conditional QoI design measure. | Accurate if read jointly with `target_present_by_draw` and `complete`. | `verified_code_fact` | Build explicitly conditional distributions without discarding denominator mass. |

## 7. Calibration evidence and gaps

### 7.1 Evidence that exists

- `docs/stats/figures/coverage_summary.json` is deterministic
  (`seed=20260710`, 200 scans per count cell, 100 bootstrap draws, nominal 0.68). For omega at
  count levels 15/30/100, observed coverage is:

  | Counts | HESSE | Profile | Bootstrap |
  |---:|---:|---:|---:|
  | 15 | 0.675 | 0.680 | 0.635 |
  | 30 | 0.655 | 0.660 | 0.630 |
  | 100 | 0.605 | 0.605 | 0.610 |

  Standard errors are about 0.033–0.035. The high-count shortfall is documented as an unmodeled
  shared shift, not fixed by any same-fit interval.
- In the skewed-gamma cell (true gamma 0.1, 15 counts), including at-bound fits gives HESSE gamma
  coverage 0.64 and bootstrap coverage 0.81; 36/200 fits are at the bound. This supports the
  gamma-wall distrust rule, not the mixed model-interval authority.
- `tests/analyze/test_bootstrap_coverage.py` provides a smaller executable fixed-model synthetic
  gate. `tests/analyze/test_bayes_coverage.py` is explicitly slow/broad and is not a promotion gate.
- Gold-set evidence is useful for ranking and systematics: confidence is higher for resolved-close
  than resolved-far rows (0.760 vs 0.264), median energy disagreement is about 0.16 meV, and legacy
  statistical intervals cover the human reference only about 18%. It does not calibrate
  `confidence` as a probability or validate model-averaged intervals.
- The drive calibration artifact records global/bucket inflation and a systematic center-pull
  floor. Its current deterministic file reports global coverage 0.4 and inflation about 1.451
  (`calibration/phonon_dft_deterministic.json`). Its training target is not the mixed predictive
  interval.

### 7.2 Evidence that does not yet exist

Before a scientific behavior change, run and freeze these campaigns:

1. **Selection recovery:** AIC/AICc/BIC across relevant `n/k`, null/one-/two-mode truth, signal and
   background strength, misspecification, guideline/hint routes, and every constraint origin.
   Report selected-model frequency, weight calibration only if claimed, target bias, interval
   coverage, and failure/refusal rate.
2. **Boundary LRT:** parametric-bootstrap null distributions for amplitude-on-zero and
   location/width-unidentified cases. Compare empirical tail probability with chi-square and label
   the regions where asymptotic p-values remain diagnostic only.
3. **Interval ladder by estimator:** HESSE/profile/bootstrap coverage stratified by unconstrained,
   soft-penalized, prespecified conditional, and data-dependent rescue fits; exposure bucket;
   gamma-wall state; model-selection correctness; and target presence.
4. **Full-pipeline bootstrap design update:** generate a current experiment, draw a future
   observation, rerun the actual mask/fit/candidate-generation/selection/constraint/delivery
   pipeline, and compare predicted likelihood-reweighted utility with the realized update. Include
   a variant that reselects models inside every replicate.
5. **Target presence and certification:** target-present and target-absent truth with retained null
   mass; measure conditional omega coverage, unconditional false-certification, abstention,
   target-support operating characteristics, and rollback thresholds.
6. **Branch/confidence reliability:** frozen scorer versions, labeled branch trajectories and
   delivery correctness; reliability diagrams, Brier/log scores where probability semantics are
   claimed, ranking metrics for heuristic scores, and threshold sensitivity.
7. **Bayesian feasibility:** fixed scientific priors and model priors, all retained models including
   null, prior normalization tests, repeated-seed/nlive evidence stability, prior sensitivity,
   simulation-based calibration, posterior predictive checks, and runtime.
8. **Authority firewall:** off/shadow/authoritative regression proving design artifacts and failures
   cannot alter acquisition until the separately approved authority gate.

## 8. Documentation inconsistencies discovered

These were STAT-0 audit findings. STAT-1 made the minimum canonical wording repairs needed to keep
the active contracts honest; STAT-6 still owns final consolidation and audit retirement:

- `STATS_PRIMER.md:47-53,326-334` and `GLOSSARY.md:55-58` say Bayesian credible intervals are
  diagnostics-only and every certifying interval is frequentist. The physical-row lane satisfies
  that statement, but the master lane can prefer nested parameter samples and certify their mixed
  omega quantiles.
- `STATS_PRIMER.md:284-289` and `MEASUREMENT_STEERING.md:747-755` accurately describe the retained
  model-weighted predictive interval as certifying, including the nested→bootstrap→Laplace particle
  fallback. They do not state that this is a heterogeneous measure or that null mass is conditioned
  away from omega quantiles.
- `STATS_PRIMER.md:314-323` says separating `cost` from `posterior_cost` prevents a constrained fit
  from being mistaken for independent evidence. Persistence is separated, but `select.py` still
  feeds `cost` at the constrained optimum into ordinary AIC.
- `GLOSSARY.md` calls branch assignment a Bayesian posterior. The implementation is a useful
  normalized versioned scoring model, but calibrated-posterior language is not earned.
- `EIG_STEERING_DESIGN.md:13-15` correctly says alternative-window EIG scoring/selection is not
  implemented. No status correction is needed in STAT-0.

The repairs now distinguish single-fit Bayesian intervals from the mixed master interval, ordinary
Akaike support from Bayesian evidence, normalized branch-assignment scores from calibrated
posteriors, and the intended fit-vs-decide contract from current behavior. Broader explanatory and
figure-language consolidation remains deferred to STAT-6.

## 9. Exact PR sequence after STAT-0

### STAT-1 — Additive semantics metadata — Implemented 2026-07-29

STAT-1 landed as four metadata-only slices from clean base `da760f2`:

1. `isar/schema/statistics.py` defines the pure vocabularies, frozen effective-constraint record,
   strict writer/tolerant reader validation, finite canonical JSON SHA-256 fingerprints, scoped
   authority, and compact CSV projection.
2. Effective per-parameter constraint origin, candidate-generation route, estimator/fit
   classification, selection eligibility, and separate offered/scored/weight-denominator/error/
   legacy-pool fingerprints are now producer-owned and propagated without changing AIC or ranking.
3. Producer-owned HESSE, profile, bootstrap, pooled, and optimizer-bound nested interval semantics
   persist fully in `session.json` and compactly at the end of `results.csv`. Legacy rows return
   `None`; metadata is excluded from scientific, tracking, action, and cache identities.
4. `isar.master_predictive/2` and `isar.target_projection/2` now carry full predictive semantics,
   exact retained/projection set fingerprints, actual per-model particle provenance, Bayesian
   dimensions, construction-versus-design completeness, and scoped authority. Confidence,
   branch/tracker weights, and entropy fields carry producer-owned probability-like semantics.

The four schemas are `isar.fit_selection_semantics/1`, `isar.interval_semantics/1`,
`isar.probability_semantics/1`, and `isar.predictive_semantics/1`. Writers never emit
`unknown_legacy`. A construction failure emits `semantics_construction_failed` and publishes the
scientific result with semantics `None`; it cannot create a `non_result`, alter a verdict, or
suppress a predictive/projection artifact.

Current nested output is explicitly a Bayesian posterior under an
`optimizer_bound_data_dependent` prior kernel; `evidence_comparable=false` and
`bayesian_model_average=false`. The current mixed predictive ensemble has
`formal_bayesian_model=false`. Its model interval remains active for
`legacy_master_certification`, `stopping`, and diagnostic display, while all EIG and acquisition
scopes are ineligible. This is descriptive metadata, not a scientific promotion.

STAT-1 intentionally did not change `PREDICTIVE_CONTRACT`, the numerical
`predictive_basis_fingerprint`, master cache identity, or the unchanged-master carry gate, and it
did not backfill legacy cached masters. STAT-2 must introduce or bump the selection contract used
by both cache gates. STAT-3 must refuse pre-STAT-2 or semantics-incomplete inputs. A workspace
cannot enter EIG-3 merely by carrying a pre-STAT-1 cached artifact.

### STAT-2 — Selection and constraint consistency

Separate the unconstrained data-only selection fit from penalized/conditional delivery fits, or make
the latter explicitly ineligible for ordinary AIC-family weights.

Hard-conditioned fits may be weight-eligible only when the constraint is independently specified,
common to the comparison question, and reflected in `k`. Data-dependent rescue/post-selection
fits are ineligible by default. Resolve AIC versus AICc only after the §7 selection campaign.

Acceptance:

- no ordinary Akaike weight can be produced from an ineligible estimator;
- selection and delivery fits are linked by provenance;
- masks, grids, likelihood, normalization, constraints, candidate route, and model set are
  fingerprinted and tested.

### STAT-3 — Homogeneous shadow design measure

Publish one source-linked
`akaike_weighted_parametric_bootstrap_predictive` shadow artifact:

- source it from the existing master artifact/selection record rather than copying independent
  model truth;
- require parametric-bootstrap particles for every material retained model;
- label each model's bootstrap as unconstrained, penalized, or conditional;
- prohibit nested/Laplace fallback and emit a stable unavailable reason;
- carry source-master, predictive-basis, selection, conditioning, model-set, and calibration
  fingerprints;
- keep authority prohibited.

Acceptance is construction homogeneity and replay, not scientific validity.

### STAT-4a — Target-presence semantics

Represent separately:

- target-present mass/support;
- the omega distribution conditional on target presence;
- explicit target-absent state;
- projection completeness and its denominator/model set.

Reuse an adequate existing null family if it answers the scientific question. Do not invent a new
physical null merely to satisfy a schema. This slice is required before physical target-EIG.

### STAT-4b — Certification and coverage

Calibrate target-support thresholds, unconditional false-certification, and coverage of the actual
model-averaged delivery. Define rollback criteria and bind calibration artifacts to their interval
measure.

This blocks EIG-4 acquisition authority. It need not block a separately owner-approved pure-shadow
EIG-3 scorer.

### STAT-5 — Bayesian comparator

Define fixed scientific parameter priors or an explicitly non-evidence empirical-prior
interpretation; define model priors; include every retained model and null; normalize priors;
propagate evidence uncertainty; and run the §7 Bayesian campaign.

The PR must end in one explicit state:

- `not_viable`;
- `diagnostic_comparator_only`;
- `shadow_design_eligible`; or
- `authority_eligible`.

### STAT-6 — Comparability and closeout

Make cross-paradigm agreement abstain unless model, estimator/constraints, priors, projection,
dataset/mask, grid/window, and calibration target match. Update `STATS_PRIMER.md`, `GLOSSARY.md`,
`INTERPRETING_A_VERDICT.md`, `MEASUREMENT_STEERING.md`, `EIG_STEERING_DESIGN.md`, schemas, decision
ledger/index, and drift tests as appropriate. Then delete and tombstone this audit.

## 10. Owner decisions

| Decision | Why code cannot decide it | Recommended default pending evidence |
|---|---|---|
| May EIG-3 run in shadow before full-pipeline calibration? | This is an authority/product boundary. | Yes, only if output cannot affect acquisition and is labeled empirical/unvalidated. |
| What is the primary model-averaged objective: predictive accuracy, parameter inference, certification coverage, or design utility? | One weighting rule need not optimize all four. | Treat them as separate products; do not force one mixture to serve all authorities. |
| Are near-gamma pre-fit hard constraints a legitimate prespecified conditional model for IC comparison? | Depends on the scientific conditioning question and ownership of previous-branch belief. | Mark ineligible until the owner approves a precise conditional contract and campaign. |
| Must model-selection uncertainty rerun selection inside each bootstrap replicate? | Computational/statistical tradeoff and target estimand choice. | Yes for calibration of the complete frequentist procedure; compare a fixed-weight approximation only as a cheaper shadow estimator. |
| AIC or AICc? | Requires empirical `n/k` and recovery evidence. | Keep ordinary AIC behavior unchanged until STAT-2 evidence; do not silently switch. |
| What is the target-absent state, and is an existing no-phonon family adequate? | Scientific model-family choice. | Reuse existing null mechanics if they answer the target question; add no model without evidence. |
| What target-support threshold can certify? | Requires operating-characteristic and loss choice. | No default 0.95 or other threshold before calibration. |
| May data-dependent/empirical priors be used for Bayesian evidence? | This defines what “evidence” means across designs/campaigns. | Fixed scientific priors for comparable evidence; otherwise label the result non-evidence/empirical. |
| Must Bayesian model averaging be complete before EIG-4? | Depends on comparative shadow performance and desired formal semantics. | Complete it as a comparator before authority review, even if bootstrap shadow performance is good. |
| Rename legacy probability-like fields or add aliases/metadata? | v2 contracts are additive. | Keep legacy fields and add explicit semantic aliases/metadata; do not break frozen consumers. |

## 11. Initial EIG design recommendation

The recommended first EIG design measure is an explicitly non-Bayesian, homogeneous
`akaike_weighted_parametric_bootstrap_predictive` **shadow baseline**.

Its formal semantics should be:

```text
design_measure =
    akaike_weighted_parametric_bootstrap_predictive

particle semantics =
    empirical frequentist sampling distribution of the declared estimator

future-update approximation =
    likelihood-reweighted future outcomes

formal_bayesian_model =
    false

authority =
    prohibited until full-pipeline realized-update calibration
```

This recommendation is deliberately limited:

- Homogeneous bootstrap particles remove the verified per-model paradigm mixing.
- Likelihood reweighting bootstrap sampling distributions does not have the direct sequential
  conditioning interpretation that posterior reweighting has.
- Fixed observed-data Akaike weights do not reproduce selection/weight uncertainty.
- Penalized and conditional bootstraps remain different measures and must be labeled.
- The baseline is valid only to the extent that it predicts the realized complete
  refit-and-reselect update measured in §7.

A properly specified `bayesian_model_averaged_posterior`, with normalized fixed priors, model
priors, all retained models including null, and propagated evidence uncertainty, remains the
mathematically coherent candidate for formal Bayesian EIG. STAT-5 develops it as the later
comparator; STAT-0 does not prejudge whether it will be operationally viable.

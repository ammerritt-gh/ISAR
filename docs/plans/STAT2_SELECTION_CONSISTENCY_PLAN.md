# STAT-2 — Selection and constraint consistency

> **Lifecycle:** Active plan
> **Provenance:** hand-written
> **Verified against code revision:** `b543f6f` (2026-08-02)
> **Owner:** @ammerritt-gh
> **Status:** STAT-2a, the STAT-2a.1 evidence-contract repair, and the corrected STAT-2b evidence
> harness are implemented (repair commit `5767dea`); corrected STAT-2b.1 evidence is complete and
> S8 is recorded. Production remains AIC; no AICc switch is authorized. Difficult-regime findings
> remain open, STAT-2c and STAT-2d remain pending, and restricted EIG-4 may proceed independently
> under O36.
> **Exit criteria:** STAT-2a through STAT-2d are implemented and verified; accepted behavior is
> folded into `docs/STATS_PRIMER.md`, `docs/GLOSSARY.md`, and an S-series ruling; this execution
> plan is then deleted and tombstoned in `docs/DOC_INVENTORY.md`.

## Objective

STAT-2 should make one statistical statement true throughout ISAR:

> **Model structure is selected using comparable data-likelihood fits. Scientific priors, branch constraints, and data-dependent rescues may influence parameter delivery, but they may not silently influence ordinary information-criterion weights.**

At present, `score_models()` computes ordinary AIC weights from every converged result it receives, irrespective of the estimator eligibility recorded by STAT-1.  STAT-2 should replace that implicit coupling with two explicit lanes:

```text
selection lane
    unconstrained data-likelihood fits
    → information criteria
    → model weights and selected structure

delivery lane
    optional soft/hard constraints
    → delivered parameter estimate
    → intervals, rescue, triage and reporting
```

The selected structure and model weights come only from the first lane. The second lane cannot rewrite them.

## Code-grounded STAT-2a execution plan

This section is binding for STAT-2a on the checkout above. The later sections retain the whole
STAT-2 design, but where an end-state description conflicts with the no-authority-change shadow
phase, this section wins.

The STAT-1/1.5 prerequisite spot-check passes: `isar/schema/statistics.py`,
`isar/analyze/loop.py`, and `isar/session/driver.py` contain per-constraint origins, complete
comparison-context identities, canonical model-set identities, aggregate eligibility and reasons,
realized interval authority, authenticated predictive/projection semantics, and the identity
firewall. The focused schema/statistical-semantics tests pass at this revision. The governing
producer/consumer findings remain in
[`../stats/STATISTICAL_SEMANTICS_AUDIT.md`](../stats/STATISTICAL_SEMANTICS_AUDIT.md).

### Grounding corrections

There are three constraints in the current code that the abstract two-bundle design must make
explicit:

1. Candidate generation is not yet a context-free operation. `loop._enumerate_and_fit()` fits the
   one-mode family and then lets `_offer_multimode()` inspect those fitted residuals before it
   offers the two-mode family. A constrained delivery context and its unconstrained selection
   context can therefore offer different two-mode sets.
2. `loop.fit_structures_two_stage_hinted()` may deliberately return one hinted result for an
   unconstrained master. That legacy route cannot simultaneously be the complete shadow
   comparison. The shadow lane must reuse that result object and fit the missing alternatives; it
   must not accept the shortcut as complete.
3. S6's cache/carry contract bump applies when selection authority moves in STAT-2c. STAT-2a is
   diagnostics-only, so `isar.model_selection/2-shadow` must not enter any scientific, cache,
   master, tracking, stopping, request, or acquisition identity.

Accordingly, “generate the candidate family once” means once for the complete **standard shadow
route**. The existing legacy delivery route remains byte-for-byte in authority. A shared
authoritative family is a STAT-2c change.

### 1. Current authoritative selection and constrained-delivery flow

The current authority chain is:

```text
driver.fit_one()
    → loop.fit_structures_two_stage_hinted(ctx, ...)
        → _try_structure_hint(), or
        → _enumerate_and_fit()
            → enumerate_structures(ctx)
            → fit one-mode candidates under ctx.constraints
            → _offer_multimode() residual/cost gate
            → enumerate_multimode(ctx) and fit, when offered
    → guideline label filter
    → select.select_models(..., stamp=False)       # preliminary legacy AIC
    → attach errors to legacy survivors only
    → select.select_models(...)                    # authoritative legacy AIC + pool
    → _build_selection_semantics()
    → verdict.classify()
    → delivered scalar/interval/candidate rows
```

`select.score_models()` currently accepts every converged result, computes
`AIC = cost + 2k`, AICc, BIC, and ordinary Akaike weights, and does not inspect
`fit_semantics.selection_eligibility`. The second `select_models()` result controls
`best_label`, survivors, the across-model pool, modality, verdict, candidate rows, error
economy, predictive inputs, and all downstream delivery.

Constraints enter before that chain:

- `_resolve_prior_and_context()` calls `_apply_low_q_rescue()`, which may replace the context with
  soft or prior-predictive hard `ParamConstraints`.
- `loop.build_problem()` applies `_soft_penalty()` to the optimizer objective and `_hard_fixed()`
  to the optimized parameter subspace.
- `_hard_fix_escalation()` may call `fit_one()` again with a data-dependent hard gamma constraint
  and replace the first delivered fit dict.

STAT-2a leaves every one of those authority edges on the legacy result.

### 2. Where candidate-set changes occur

The complete shadow route must record, rather than erase, all four current set transitions:

- Physical admissibility: `enumerate_structures()` and `enumerate_multimode()` in
  `isar/analyze/structures.py` gate elastic, quasielastic, anti-Stokes, Stokes-only, and
  multi-mode hypotheses from the scientific context.
- Residual/expected-mode escalation: `loop._offer_multimode()` and the `peaks` guideline decide
  whether two-mode hypotheses are offered. The shadow trigger is evaluated from the complete
  unconstrained one-mode result set.
- Scientist-authored structure filtering: `fit_one()` applies `structure_labels` after point
  fitting. The same declared filter applies to the shadow set and remains in the canonical route.
- Previous-structure shortcut: `_try_structure_hint()` may reduce legacy delivery to one hinted
  result. For shadow selection the hint may provide a reusable result/initialization, but never
  suppresses another admissible candidate.

`fit_one()` must retain the pre-guideline result objects as well as the current post-filter
delivery list. The former prove/reuse the complete generation route; the latter remain the exact
legacy selection input. This is provenance-only retention and cannot reorder or reintroduce a
guideline-excluded model into legacy selection.

The standard route records offered, attempted, converged, eligible-denominator, and failed labels
separately. An optimizer failure is not a missing attempt; silently failing to attempt an offered
candidate is an incomplete comparison.

### 3. Shadow-lane seam and unconstrained context

Add a point-fit-only helper beside the existing loop, not inside `score_models()` and not after
error attachment. It returns the complete shadow candidates plus the canonical generation route.
It must not attach MINOS, bootstrap, profile, Bayes, pooled intervals, verdicts, or stamps.

In `fit_one()`, keep the existing delivery enumeration and guideline filtering unchanged. After
the legacy point-result set and route are known, but before the preliminary legacy
`select_models()` call:

```python
delivery_results_before_guideline = all_results
delivery_results = results                    # exact post-filter legacy input
selection_ctx = replace(ctx, constraints=ParamConstraints())
shadow = build_shadow_selection(
    selection_ctx,
    reusable_pre_guideline_results=delivery_results_before_guideline,
    legacy_selection_results=delivery_results,
    delivery_route=candidate_generation_route,
    structure_labels=structure_labels,
    peaks=peaks,
    supplied_hint=structure_hint,
)
```

This call returns a point-fit bundle, not the final persisted comparison. The latter is finalized
only after the unchanged final legacy `select_models()` and `_build_selection_semantics()` calls
have produced the authoritative legacy labels, weights, and fingerprint. This keeps the shadow
optimizer work before error attachment while keeping every legacy comparison field sourced from
the actual final legacy record.

`replace()` changes only `constraints`. Arrays, mask, energy calibration, exposure scale/reference,
resolution, temperature, admissibility fields, `prior_omega`, `acoustic_omega`, guideline inputs,
and initialization information remain the same objects/values. The fit semantics'
`data_likelihood_fingerprint` is the common-context proof; model parameterization fingerprints
prove each label's exact components, bounds, and free space.

The shadow helper proceeds in two stages, enumerating each stage's hypotheses once. It reuses an
existing delivery result only when the context is effectively unconstrained, the result has valid
STAT-1.5 semantics, is selection-eligible, has the same data-likelihood identity, and joins the
generated hypothesis by label and model-parameterization fingerprint. It fits only missing
candidates. It then evaluates the standard residual trigger over the complete unconstrained
one-mode set and repeats the same reuse/join for any offered multi-mode set. A hinted two-mode
result is reusable only if that residual/`peaks` route independently offers its hypothesis; the
hint itself is not a standard candidate-offer route in STAT-2a.

This preserves the common path without weakening the hint rule:

- Unconstrained + full legacy route: the shadow result list aliases the delivery result list and
  every `StructureFitResult` is the same object; no optimizer call is added.
- Unconstrained + accepted structure hint: the hinted result object is reused, while missing
  alternatives are fitted so the shadow comparison is complete.
- Constrained delivery: constrained objects are never reused as standard evidence; all standard
  candidates are fitted under `selection_ctx`.

### 3.1 Deadline and failure isolation

`run_batch()` gates scan and master waves with `_AnalysisDeadline`. Extra point fitting inside a
wave could otherwise consume the remaining allowance, reduce the number of authoritative waves
completed, suppress publication, or delay an action. Therefore:

- when `analysis_allowance_seconds` is set, thread a diagnostics-only execution policy into scan
  and master fit tasks and do not launch any additional shadow optimizer work;
- persist `selection_status = "unavailable"` with
  `unavailable_reason_code = "shadow_skipped_soft_deadline"` and zero incremental fit runtime;
- do not make the current deadline, worker count, or completion order part of any scientific or
  shadow selection fingerprint.

Unbounded runs execute the shadow lane and record its overhead. This keeps deadline-bounded
campaign publication/action behavior unchanged while still producing representative shadow
evidence in ordinary and explicitly forced validation runs.

Every shadow boundary—context construction, candidate fitting/reuse, semantics validation,
comparison, fingerprinting, and persistence projection—is catch-and-log isolated from legacy
delivery. A failure produces a JSON-clean structured unavailable record with a stable stage/reason
code. If even the schema validator is the injected failure, the producer writes the minimal
unavailable record directly; it never raises through `_process_one()` or `_fit_master_group()` and
never turns a valid legacy fit into `non_result`.

### 4. Shadow comparison and rescue isolation

Add a pure shadow comparator that validates each candidate's recognized STAT semantics before
calling the unchanged legacy scoring formulas. It sorts the standard candidate records by stable
model identity, uses only converged `selection_eligibility == "eligible"` results, and resolves an
exact criterion tie by stable model identity rather than input order.

If any required attempted candidate has missing/malformed semantics, an incomparable
data-likelihood identity, an unapproved estimator, or an identity join failure, the whole standard
comparison is `unavailable`; it does not normalize the remaining subset and does not borrow legacy
weights. A complete attempted family with one eligible converged denominator member is
`degenerate_single_candidate`. A complete attempted family with zero eligible converged members is
`unavailable`. Non-converged attempts remain visible and outside the denominator.

`_hard_fix_escalation()` must preserve the original shadow record across its delivery-only retry.
The retry neither invokes the shadow fitter again nor replaces the original
`standard_selection_fingerprint`. Adoption of the rescued delivery dict copies the original shadow
record unchanged.

### 5. Versioned record and persistence

Define a separately validated shadow schema in `isar/schema/statistics.py` and keep
`MODEL_SELECTION_CONTRACT = "isar.model_selection/2-shadow"` out of production identities. The
record contains:

```text
schema and selection_contract_revision
legacy_model_selection_fingerprint
standard_selection_fingerprint
selection_context_fingerprint
canonical candidate_generation_route
legacy_selected_label / standard_selected_label / selection_changed
legacy and standard AIC weights
per-standard-model cost, k, n, AIC, AICc, BIC, delta-AIC, weight, and fit semantics
delivery effective constraint origins
offered / attempted / converged / denominator / failed model identities
selection_complete and selection_status
runtime_increment_seconds
stable unavailable_reason_code and affected candidate identities
```

`standard_selection_fingerprint` covers the contract revision, selection-context identity,
canonical route, offered/attempted/converged/denominator model-set identities, criterion inputs,
and statuses. It excludes candidate order, wall time, legacy results, and delivery constraints.
The schema reader recomputes that fingerprint. `selection_changed` is `null`, not `false`, when
the standard comparison is unavailable.

Persist the full record as an additive `FitOutcome.shadow_model_selection` field in `session.json`.
Append a compact JSON projection as the final `results.csv` column, following the existing
full-session/compact-CSV STAT-semantics pattern, and register that column with
`isar/drive/isar_io/readers.py`'s JSON-column decoder. Do not add a second sidecar or a
GUI/scientific reader in STAT-2a. Legacy cached/carried outcomes may lack the field; the optional
reader reports `unavailable` with `legacy_artifact_not_evaluated` and never synthesizes weights.

### 6. Identities that remain unchanged in STAT-2a

The shadow field and contract are metadata-only for this phase. Do not change:

- `select.MODEL_SELECTION_REVISION`, legacy `score_models()`, survivor floor, or stamps;
- scan checkpoint keys in `run_batch()` / `CheckpointStore`;
- `certification.MASTER_FIT_CONTRACT`, `driver._MASTER_FIT_CONTRACT`, or
  `master_fit_fingerprint()` inputs;
- the unchanged-master carry gate (`content_hash` + `PREDICTIVE_CONTRACT`);
- member-set `canonical_hash` / `master_revision`;
- `PREDICTIVE_CONTRACT`, numerical `predictive_basis_fingerprint`, retained-model weights, or
  predictive artifacts;
- promotion candidate identity, branch-track revision/assignments, calibration bucket lookup,
  result `content_hash`, stopping interval choice, drive request fingerprints, proposals, or EIG.

The production selection/cache/carry revision bump described later in this document is therefore a
STAT-2c gate, not a STAT-2a task.

### 7. Exact STAT-2a proof set

Focused tests are added beside the seams they prove:

1. `tests/analyze/test_shadow_selection.py`
   - formulas use standard-fit `cost`, `k`, and `n`;
   - order-independent weights, winner, route, set fingerprints, and full record fingerprint;
   - worse-model addition changes only normalization, not existing criterion values;
   - missing/malformed/ineligible/incomparable input refuses with stable reasons;
   - one denominator member is `degenerate_single_candidate`.
2. `tests/analyze/test_select.py`
   - legacy AIC/AICc/BIC, weights, survivor floor, stamps, and tie behavior remain unchanged.
3. `tests/session/test_stat2_shadow_lane.py`
   - complete unconstrained legacy route makes zero extra optimizer calls and reuses the exact list
     and result objects;
   - accepted structure hint reuses the hinted object but cannot suppress alternatives;
   - changing only soft/hard delivery constraints leaves standard weights and fingerprints fixed
     when data and offered models are fixed;
   - data, grid, mask, likelihood revision, normalization, parameterization, or candidate family
     changes the appropriate standard identity;
   - residual-trigger and guideline routes are visible;
   - rescue preserves the original shadow fingerprint and does not run shadow twice;
   - injected failures at context, fit, validation, comparison, fingerprint, and projection stages
     log and persist structured unavailable without changing delivery or producing `non_result`;
   - legacy best label, candidates, intervals, verdict, confidence, and error-attachment labels are
     identical with and without shadow production.
4. `tests/session/test_fit_persistence.py` and `tests/schema/test_statistics.py`
   - full session and compact CSV round-trip the record;
   - recognized records authenticate on read; missing legacy records become structured
     unavailable only in the optional diagnostic view.
5. `tests/session/test_statistical_semantics.py`
   - injecting/changing the shadow field cannot alter scientific result hashing, master revision or
     fit fingerprint, promotion ordering, predictive basis, tracked authority/revision,
     calibration lookup, stopping choice, or drive request fingerprint.
6. `tests/session/test_certification_checkpoint.py`
   - a forced initial wave followed by an expired soft deadline completes exactly the same
     authoritative units with STAT-2a enabled;
   - deadline-bounded scan and master tasks make no extra optimizer calls and persist
     `shadow_skipped_soft_deadline` rather than changing publication/completion.
7. Deterministic campaign harness
   - compare all pre-existing scientific fields and actions while excluding only the new shadow
     record and already-existing timing fields; unconstrained and constrained fixtures retain
     their current delivery results.

Implementation verification, after approval, is the focused tests above, parse/import checks,
`python -m pytest -m "not slow" -n 16 --dist worksteal`, the full parallel suite, documentation
drift checks, and `git diff --check`.

### Bounded implementation commits

1. **STAT-2a schema and pure comparator** — versioned record, strict validation/authentication,
   statuses/reasons, criterion and fingerprint invariants. Implemented in `5295475`.
2. **STAT-2a shadow producer** — complete unconstrained candidate helper, exact reuse, hint/residual
   route handling, rescue isolation, and focused zero-authority-change tests. Implemented in
   `1310969`.
3. **STAT-2a persistence and evidence** — session/compact CSV plumbing, identity-firewall and
   deterministic campaign proofs, documentation updates, representative shadow observations,
   runtime overhead, unavailable cases, and a frozen proposed STAT-2b campaign specification.

STAT-2a evidence records complete, degenerate-single-candidate, and structured-unavailable outcomes.
Focused proofs cover exact common-path fit-object reuse, constraint-invariant standard identities
and weights, finite-allowance skip, injected comparator/schema failure isolation, rescue
preservation, persistence compatibility, and exclusion from cache and downstream authority
fingerprints. `runtime_increment_seconds` is persisted for later campaign aggregation; STAT-2a
makes no benchmark or overhead acceptance claim from the focused tests.

# Binding statistical decisions

## 1. What counts as an eligible selection fit

Eligible:

```text
data-only Baker–Cousins likelihood
same observed spectrum
same fitted energy grid and mask
same exposure/normalization basis
same likelihood revision
full free parameterization of the declared model
physical parameter bounds that define the model
```

Ineligible for ordinary AIC weights:

```text
soft-penalized estimates
prior-predictive hard fixing
data-dependent rescue fixing
post-selection fixing
delivery estimates conditioned on branch beliefs
fits with missing or invalid STAT-1.5 comparison metadata
```

A Stokes-only model remains eligible because it is a separately declared physical model, not a fit-time parameter constraint. A nonnegative amplitude bound likewise belongs to the model parameter space; it is not a branch-derived delivery constraint. Boundary non-regularity remains a reason to validate the criterion empirically, not to call the fit penalized.

## 2. Structure hints may initialize selection, but not suppress its alternatives

An accepted previous-structure hint may:

* determine candidate order;
* provide initial parameter values;
* reduce optimization work after an exact cache hit.

It may not turn the authoritative selection comparison into a single-model fit.

Any result produced from the current structure-hint shortcut should be classified as:

```text
delivery eligible
model comparison incomplete
model weights unavailable
design ineligible
```

For pooled masters and any fit expected to publish model weights, run the full declared candidate-generation route.

Scientist-authored guideline filters may restrict the model family, but the resulting weights are explicitly conditional on that declared set.

## 3. Data-dependent multimode generation remains conditional

The residual trigger that decides whether to offer two-mode models may remain. It must be:

* included in the model-selection fingerprint;
* treated as part of the complete selection procedure in calibration;
* described as a data-dependent candidate-generation route.

Its Akaike weights are relative support within the offered set, not absolute probabilities that excluded structures are false.

## 4. AIC remains the provisional production criterion

Do not switch to AICc or BIC in the implementation PR merely because they are available.

STAT-2 includes a predeclared comparison campaign. That campaign decides whether production remains:

```text
criterion = AIC
```

or changes to:

```text
criterion = AICc
```

BIC remains a model-identification comparator. The production criterion should primarily be judged by predictive performance and target inference, not simply by how often it recovers the exact generating model.

## 5. No silent model substitution after delivery failure

Once the selection lane chooses model (m^*):

* the delivery lane may fit (m^*) with the approved constraints;
* a data-dependent rescue may refit (m^*);
* failure may fall back to the unconstrained selection fit of (m^*), if that fit passes the ordinary delivery gates;
* otherwise ISAR abstains.

The delivery lane must not silently choose the constrained fit with the lowest cost among another collection of models. That would recreate post-selection model weighting under another name.

# Runtime design

## One candidate family, two fit bundles

Generate the candidate hypotheses once. Then construct:

```text
SelectionFitBundle
    results_by_label
    selection_context_fingerprint
    candidate_generation_route
    model_selection
    criterion
    weights
    selected_label

DeliveryFitBundle
    results_by_label
    delivery_context_fingerprint
    source_selection_fingerprint
    selected_label
    rescue provenance
```

### Selection context

Clone the scientific context but remove `ParamConstraints`:

```python
selection_ctx = replace(ctx, constraints=ParamConstraints())
```

Retain:

* data and mask;
* energy calibration;
* resolution model;
* temperature;
* physical structure;
* Stokes/anti-Stokes admissibility;
* initialization hints;
* model-family guidelines.

### Delivery context

Use the original context, including its per-parameter constraint origins.

### Reuse

When the delivery context has no effective constraints:

```text
selection result = delivery result
```

Do not refit. This keeps the common unconstrained path computationally and numerically unchanged.

When constraints are effective:

1. fit all offered models in the unconstrained selection lane;
2. derive the authoritative selected label and survivor labels;
3. fit the same candidate labels under the delivery context, initialized from their selection MLEs;
4. attach expensive errors only to delivery fits whose labels survive the selection lane.

This roughly doubles point fitting only on constrained paths, while preserving the current full-error economy.

# Model-weight and interval flow

After STAT-2:

```text
model weights
    come from SelectionFitBundle only

delivered best estimate
    comes from DeliveryFitBundle[selected_label]

legacy across-model interval
    selection weights
    × delivery-fit uncertainty particles

current master_predictive/2
    selection weights
    × delivery-fit nested/bootstrap/Laplace particles
```

The latter two objects remain statistically mixed and authority-limited. Their semantics must say:

```text
model_weight_source = comparable_selection_fit
particle_source = constrained_or_unconstrained_delivery_fit
formal_bayesian_model = false
homogeneous_design_eligible = false
```

STAT-3 will later construct the homogeneous bootstrap design artifact from the selection-fit lane. STAT-2 should not attempt that work.

# PR sequence

## STAT-2a — Dual-fit shadow lane

**Behavior change:** none.

Run and persist the new selection lane beside current behavior, but keep the existing delivery selection authoritative.

Deliver:

* `MODEL_SELECTION_CONTRACT = "isar.model_selection/2-shadow"`
* unconstrained selection-fit bundle;
* delivery-fit bundle;
* side-by-side legacy and standard selection tables;
* selected-label and weight differences;
* runtime overhead;
* complete provenance and fingerprints.

For an unconstrained context, assert object/numerical reuse rather than two fits.

Persist comparisons such as:

```text
legacy_selected_label
standard_selected_label
selection_changed
legacy_weights
standard_weights
criterion_rank_changes
delivery_constraint_origin
runtime_increment
```

Do not expose these as new scientific verdicts.

### Key acceptance tests

* Changing a delivery constraint cannot change the shadow selection weights.
* Changing the data or candidate family does change them.
* Structure hints cannot suppress selection candidates.
* Selection and delivery labels join by model identity and parameterization fingerprint.
* Rescue operations cannot change the selection fingerprint.
* Missing selection metadata produces no shadow weights rather than guessed weights.
* No cache, verdict, interval, certification, stopping, or acquisition behavior changes.

## STAT-2b — Selection-criterion campaign

This is evidence work, not a production switch. The scenario matrix, metrics, and decision gate
below were frozen as the proposed campaign specification on 2026-07-30. The evidence-only
implementation lives under `isar.synth.stat2b`; its public surface is `load_campaign_spec()`,
`run_campaign()`, and `summarize_campaign()`. Development and sealed held-out specifications live
under `tools/stats/`; large local campaign outputs remain in the gitignored output tree, while
reviewed aggregate evidence is promoted here. The original development campaign completed on
2026-07-30 with 21 full-path smoke
realizations and 420 development realizations. Its campaign-spec fingerprint was
`47c2a32b1a7d8a193b3f2c3caefb87b4beac60e4248fe0d538f887792ea136b1`, and its
development-summary fingerprint is
`dcd2fdddb37313a066402f1169616dec783545a62d889e174cc90331af55bd47`. The owner
approved its proposed gate in commit `58e9648`, after which 1,254 of 2,163 held-out checkpoints
were opened before the run was stopped. No held-out scientific records were inspected. The run is
aborted and scientifically ineligible because full-delivery reuse was keyed too narrowly to
represent every input consumed by delivery. The original development output and held-out output
are preserved separately as pre-STAT-2b.1 and aborted evidence, and the approved gate remains
byte-identical with a superseded-status sidecar.

STAT-2b.1 then repaired and authenticated delivery inputs in commit `5767dea`. Its open scaling
preflight completed 42 datasets at each of 4, 8, 16, 24, and 28 workers with zero failures and
identical scientific record fingerprints. The frozen rule selected 24 workers; scaling-summary
fingerprint
`0c803fd7e2516b60dc7812819515067679f24ac9d1e0ffd24fa7b7c37d74342d`.
Fresh corrected development completed 21 full-path smoke realizations plus 420 development
realizations. Its campaign-spec fingerprint is
`29f6b0623c4bf6c17666116741383d615f354eef939b12e72c901f3600b08c96`, and its
development-summary fingerprint is
`cae8a48fd687a2cb725b2a36bc9f82ae2a4cb4874ef490b8b7bb61b8dfcb0da7`.
The corrected proposal retains 103 held-out realizations per cell, uses 24 workers, projects
3,374.71 seconds, and proposes a 5,062.07-second ceiling. The promoted proposal remains the
unaltered pre-approval historical artifact.
Reviewed aggregate evidence is committed under
[`../assets/stat2b/2026-07-30-stat2b1/`](../assets/stat2b/2026-07-30-stat2b1/).
The owner approved those unchanged margins, replication count, worker count, and runtime ceiling
on 2026-07-30. The active gate is `tools/stats/stat2b1-heldout-gate.json`; its held-out
specification fingerprint is
`43b9fcbc7f6c2cbb57510c4801ac8084e96caecc7b511e6707240e257e974a54`.
That activation authorizes only the sealed STAT-2b held-out run. No criterion switch is
authorized.

The first corrected held-out attempt exited operationally on 2026-07-30 after writing 564 of
2,163 atomic checkpoints: five scenario cells were complete and `continuum-absent` had 49
checkpoints. No held-out scientific record was inspected, no complete summary was generated, and
the attempt is scientifically ineligible. Its stale `running` ledger and checkpoints remain in
the local failed-attempt quarantine.
The owner authorized a clean operational restart with the unchanged 103 realizations per cell,
24-worker count, sealed namespace, numerical margins, and runtime ceiling. Before that restart,
the evidence-only runner was hardened to cancel queued futures after the first task exception,
wait only for already-running work, close the runtime ledger with a safe failed-task identity, and
retain a durable console traceback. The active gate's source hashes and recovery authorization
were refreshed for that orchestration-only repair. The clean restart has not yet opened any seed.

That clean restart also stopped fail-closed after 564 checkpoints on 2026-07-31. Its durable
traceback identified a finite analytic Poisson-KL contribution that had become nonfinite solely
because the direct `lambda / mu` ratio overflowed before the record fingerprint was constructed.
No held-out scientific record was inspected. The incomplete output remains in the local
failed-attempt quarantine.
The owner authorized a numerically stable, log-space evaluation of the same tiny-clamped
mean-per-kept-bin KL definition. The source-bound active gate was refreshed for that repair; no
scenario, seed namespace, replication count, worker count, margin, or runtime ceiling changed.

The third clean attempt completed all 2,163 held-out datasets across 21 cells at 24 workers in
3,146.31 seconds, below the frozen 5,062.07-second ceiling. Its evidence-completeness check
passed, but the frozen gate returned `authority_withheld`: comparative false certification,
target-absent comparison completeness, partially resolved two-mode predictive/missed-mode
performance, and near-elastic/partially-resolved/weak-signal RMSE violated independent vetoes.
The complete held-out aggregate is preserved with the dated historical record at
[`../campaigns/2026-07-31_STAT2B1_HELDOUT.md`](../campaigns/2026-07-31_STAT2B1_HELDOUT.md).
S8 accepts the completed evidence, retains production AIC, and authorizes no AICc switch. S8 does
not authorize STAT-2c or another production-selection authority change; STAT-2c requires a separate
owner decision.

Compare:

```text
legacy constrained AIC
unconstrained AIC
unconstrained AICc
unconstrained BIC
```

Optionally include an oracle full-candidate route to measure the cost of residual-triggered or hint-driven candidate omission.

### Scenario matrix

Truth/model regimes:

* no phonon or background-only;
* one clean phonon;
* weak one-mode signal;
* near-elastic acoustic signal;
* one phonon plus elastic;
* one phonon plus quasielastic;
* two separated modes;
* two partially resolved modes;
* Stokes-only windows;
* anti-Stokes-supported windows;
* incorrect background family;
* broad/continuum-like signal outside the model family;
* resolution/calibration perturbation.

Data regimes:

```text
n/k from sparse to comfortable
weak to strong exposure
gamma near and away from its boundary
narrow and broad windows
homogeneous and heterogeneous pooled exposure
```

Constraint regimes:

```text
unconstrained
soft prior-predictive
near-gamma hard prior-predictive
post-rail data-dependent rescue
```

### Primary metrics

The criterion decision should prioritize:

1. held-out Poisson predictive deviance or log score;
2. target-(\omega_0) bias and RMSE;
3. target interval coverage after the delivery lane;
4. false extra-mode rate;
5. missed true two-mode rate;
6. null/phonon confusion where a null comparator exists;
7. delivery and rescue success;
8. selection stability;
9. runtime.

Exact generating-model recovery is diagnostic, not the sole optimization target. AIC and AICc are predictive criteria; BIC’s preference for exact model identification may not align with efficient target inference.

Use separate development and held-out seeds. Freeze the campaign specification and decision rule before examining the final results.

### Decision gate

Choose AICc over AIC only when it provides a reproducible practical improvement in held-out predictive and target metrics without a material increase in missed real modes or false certification.

When practically tied, retain AIC to minimize behavioral change.

Record the decision as a new S-series ruling.

The completed held-out run did not clear this frozen gate. S8 therefore retained production AIC.

## STAT-2c — Authoritative selection switch

After reviewing STAT-2b:

* bump to `isar.model_selection/2`;
* set the chosen criterion;
* make `score_models()` authority require eligible selection results;
* move current constrained fits entirely into the delivery lane;
* prohibit ordinary model weights from delivery results;
* rewire every downstream model-weight consumer to the new selection record.

### Required downstream consumers

Audit and update:

```text
selected best label
survivor/error-attachment set
legacy across-model pool
master_predictive/2 source weights
model entropy
candidate rows and GUI AIC table
cache and unchanged-master carry gates
predictive and target-projection provenance
```

The current `score_models()` accepts all converged results without reading estimator eligibility; this is the precise authority boundary STAT-2c must close.

### No eligible comparison

If no complete eligible selection exists:

```text
selection_status = unavailable
model_weight_kind = none
model_selection_reason = <stable code>
```

A constrained delivery may still be reported, but:

* it carries no ordinary Akaike weights;
* no model-averaged predictive artifact is authoritative;
* STAT-3 design construction refuses it;
* acquisition falls back to the existing non-EIG behavior.

### One-model comparison

A complete comparison with one offered eligible model may deliver that model, but record:

```text
selection_status = degenerate_single_candidate
weight = 1.0
model_uncertainty_not_evaluated = true
design_eligible = false
```

Do not present weight 1 as overwhelming data support.

## STAT-2d — Cleanup and closeout

Remove or quarantine the legacy constrained-AIC authority path.

Retain legacy diagnostics only when they are useful for historical comparison. Update:

* `STATS_PRIMER.md`;
* `MEASUREMENT_STEERING.md`;
* `GLOSSARY.md`;
* `DECISIONS.md` and index;
* STAT audit status;
* schema and drift tests.

Do not retire the full audit until STAT-6.

# Selection contract

A production selection record should minimally contain:

```text
schema = isar.model_selection/2
criterion_kind
criterion_revision
selection_fit_context_fingerprint
candidate_generation_route
offered_model_set_fingerprint
converged_model_set_fingerprint
weight_denominator_fingerprint
selection_complete
selection_status
selected_label
survivor_labels
model_weight_kind
per-model:
    label
    model_parameterization_fingerprint
    comparison_context_fingerprint
    cost
    k
    n
    AIC
    AICc
    BIC
    selected_criterion_value
    delta_criterion
    weight
    convergence status
```

The corresponding delivery record should contain:

```text
source_model_selection_fingerprint
selected_label
delivery_estimator_kind
delivery_conditioning_fingerprint
effective constraints and origins
delivery fit status
rescue route
fallback route
```

# Cache and replay contract

STAT-2 changes the source of selected labels and model weights. Therefore:

* the model-selection contract revision must enter the fit/master cache key;
* the unchanged-master carry gate must require the new revision;
* pre-STAT-2 cached masters must regenerate before publishing selection weights;
* predictive artifacts must regenerate from the new source weights;
* old workspaces remain readable but are selection-semantics-ineligible until refitted.

STAT-1 already anticipated that STAT-2 must update the selection contract used by cache and unchanged-master carry identity.

# Required mathematical and behavioral tests

## Statistical invariants

* Delivery constraints do not affect selection weights when selection data and models are fixed.
* Reordering candidates does not affect scores, weights, or fingerprints.
* Adding a worse eligible model changes the normalization but not existing criterion values.
* Ineligible fits can never enter an authoritative denominator.
* All weights are finite, positive, and normalized over the exact declared denominator.
* AIC/AICc/BIC formulas use selection-fit cost and full selection-fit (k), never delivery-fit (k).
* Changing a rescue-fixed parameter cannot change the original model-selection fingerprint.
* A single-candidate comparison is marked degenerate.
* Candidate-generation omission is visible, never silently interpreted as zero probability.

## Pipeline tests

* Unconstrained campaigns remain bit-identical through selection, delivery, intervals, and proposals.
* Soft-prior and hard-conditioned campaigns may change model weights only at the intentional STAT-2c switch.
* Data-dependent rescue can change delivery parameters but not selected structure or weights.
* A failed constrained delivery does not select another model behind the selection lane.
* Current mixed predictive artifacts identify separate weight and particle sources.
* No EIG scorer or acquisition authority is introduced.
* Full deterministic campaigns remain replayable after cache regeneration.

# Explicitly deferred

STAT-2 does not implement:

* the homogeneous bootstrap design artifact—that is STAT-3;
* target-presence aggregation or a no-target certification rule—that is STAT-4a/4b;
* full-pipeline bootstrap model-weight uncertainty;
* Bayesian model weights or scientific priors—that is STAT-5;
* scan-window EIG implementation; a separate restricted workstream is authorized by O36;
* TAVI changes;
* EIG acquisition authority.

# Next state

STAT-2c remains pending a separate owner decision, and STAT-2d remains pending. Difficult-regime
findings remain open; no AICc switch is scheduled. The restricted EIG-4 workstream may proceed
independently under O36, but O36 does not relax STAT-2 requirements for later broader statistical
authority.

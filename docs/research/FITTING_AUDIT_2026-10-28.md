# Fitting audit: faster deep passes and more reliable comparisons

> **Status:** draft
> **Scope:** audit and recommendations, 2026-10-28; no production fitting changes
> **Source revision:** `c8a7e8e` (clean tracked working tree when the audit began)

**Start with continuous model scheduling, remove unused area calculations, and reuse the
overlapping narrow-peak logarithm stencil.** These address avoidable elapsed time without
changing the model family or sampler settings. A short synthetic prototype measured about
40–45% less evaluation time for six narrow peaks when the two evaluator changes were combined.
That is a promising kernel result, not a demonstrated reduction of a four-hour deep pass.

The other immediate work is numerical correctness: two small Laplace boundary defects,
failed-model recording, and qualification when the deep winner reaches the count ceiling.
Several findings in the [external fitting review](../ChatGPT_Handoff_Fitting.md) remain present.
The largest eventual savings may require fewer separate integrations and better exploration,
but those are experiments with statistical consequences, not the first easy wins.

The operator's subsequent 2026-10-28 brief supersedes this initial ordering: retain stopped-run
timings first, repair numerical defects, combine independent stages with continuous refill,
then measure the sampler before choosing evaluator changes. A bounded rslice comparison runs
alongside the reliability work. This audit will be absorbed into the owning documents when
the work closes.

## 1. What this audit establishes

The audit followed the current fast pass, deep sampler, chosen-model error calculation and
GUI worker allocation. It checked the external review against source, read the
[statistics contract](../STATS_PRIMER.md), [performance plan](../plans/speedup.md), and
[owner's objectives](../DESIGN_GOALS.md), and ran bounded synthetic probes.
The existing speedup plan concerns campaign iteration and older error-tier work; its timing
breakdown must not be reused as the breakdown of today's deep pass.

Evidence is separated throughout:

- **Source-confirmed:** behavior directly visible at the cited revision.
- **Fresh measurement:** short generated-data probes on this Windows workstation.
- **Recorded evidence:** the earlier calibration summarized in the statistics primer.
- **Proposal:** benefit or reliability still to be measured before implementation is accepted.

The operator reports more than four hours per deep pass. This audit did not identify and
profile that particular completed run, so it cannot assign percentages of those four hours
to sampling, idle workers, retries or final errors. No long fit, real-data proof, sealed golden
gate, GUI session or statistical campaign was run. Production code and settings are unchanged.

## 2. Ranked opportunities

Effort below is relative engineering scope, not a promised delivery time. Benefits must not be
added together: several remove work from the same evaluations.

| Order | Change | Applies to | Evidence and likely benefit | Effort / qualification |
|---|---|---|---|---|
| 1 | Refill a worker whenever a model finishes, within each existing stage | Deep | Removes source-confirmed idle gaps behind slow models; saving depends on run-duration imbalance | Small–medium; preserve model seeds, stage barriers and failure records; document budget admission change |
| 2 | Calculate height during fitting, areas only when needed | Both | Fresh probe: identical means; roughly 6–20% less evaluator time across the tested cases | Small; preserve height interpolation and delivered areas |
| 3 | Share the overlapping complex-log stencil | Both, especially narrow/delta modes | Fresh probe: roughly 34–39% less narrow evaluator time alone; 40–45% combined with order 2 | Small–medium; same mathematical correction, but numerical cancellation needs boundary/curvature checks |
| 4 | Prepare grid/Bose factors, interpolation, static columns and prior groups once | Mostly deep; some shared work | Source-confirmed repeated work at every likelihood/transform call | Small–medium; speed unmeasured; cache identity must include the full frozen context |
| 5 | Use the existing parallel profile-error path for the selected model | Deep completion | MINOS and two profile sides currently serialize; an existing parallel helper can run the three independently | Small–medium; only helps the errors portion; retain seeds and interval semantics |
| 6 | Preserve per-method timings and expose promotion/restart costs | Deep diagnosis | Much timing already exists; delivery discards part of the errors breakdown | Small; enables attribution before more tuning |
| 7 | Compare analytic amplitude derivatives and amplitude profiling | Fast, deterministic refits | Existing convex solver and generic profiling machinery; robustness and conditioning may improve | Medium experiment; fast pass already caches unchanged mode responses |
| 8 | Reassess exploratory work that will almost certainly be promoted | Deep | Current uncertainty allowance is broad; promoted models pay for two independent integrations | Medium, policy-sensitive; measure promotion rate before changing allocation |
| 9 | Compare width-count regrouping and alternative exploration mechanisms | Deep | Potentially much larger savings and complete family coverage | Larger method experiment; no demonstrated ISAR speedup or accuracy gain yet |

## 3. CPU use: improve scheduling before increasing worker counts

### Refill within a stage

[`deep_pass.run_stage`](../../isar/analyze/deep_pass.py) (342–367) submits a chunk of
`workers` models to `executor.map`, consumes the entire chunk, then submits the next.
When one model takes much longer than its companions, the finished workers have no new work.
Ordered result delivery also delays visible progress behind an early slow model.

Use a bounded set of futures and submit the next eligible model on completion. Keep the
contender, exploratory and promotion stage boundaries initially; this avoids changing how the
promotion leader is calculated. Keep the existing mapping from model identity to seed and
emit results in deterministic record order. Unlimited runs should integrate the same models
with the same settings. With an explicit time budget, checking before each admission changes
which models can start compared with checking before each chunk; that needs a documented test.
A merely unordered iterator over the same fixed chunks would not remove the idle gaps.

For a completed stage, let each model duration include all of its retries. The current idealized
schedule takes the sum of chunk maxima. Any schedule with P workers takes at least
`max(sum(model durations)/P, longest model duration)`, before overhead. This gives a useful
ceiling on the opportunity without replaying the fit. It is a scheduling estimate: recorded
wall seconds per worker are not measured CPU seconds, and other workstation load changes them.

### Resource ownership is broader than the sampler pool

The GUI queue gives fast items one core and a deep item the idle allowance
([`analysis_queue.py`](../../isar/session/analysis_queue.py), 208–237).
[`file_fit.py`](../../isar/session/file_fit.py), 177–181, reduces that allowance once using
the maximum of the contender and exploratory stage widths. The service accepts only one
such notice ([`analysis_service.py`](../../isar/session/analysis_service.py), 557–565).
Consequently a small contender stage or serial profile calculation can reserve more cores
than it uses, delaying other files.

Dynamic lending between stages could improve throughput, but requires changes to resource
ownership and the queue protocol. The existing priority/start rules are operator decisions
recorded in [the GUI design](../design/gui.md); this is a separate design proposal.
The first scheduler repair can retain those rules and the fixed per-item allowance.

Keep model-level process parallelism as the first choice while many models are ready.
Putting a second process pool inside every sampler risks oversubscription and adds IPC to
sub-millisecond or millisecond likelihood calls. Dynesty supports internal pools, but its
documentation explicitly discusses avoiding repeated serialization; benchmark an inner pool
only for a later situation with too few independent models to occupy the allowance.
([Dynesty 3.0 parallelization](https://dynesty.readthedocs.io/en/v3.0.0/quickstart.html#parallelization)).

One BLAS thread per worker is already the intended setup in `file_fit.py:263–266` and
`driver.py:188–191`, using environment `setdefault`. That does not override existing thread
settings or resize a library already loaded. Record actual `threadpool_info()` inside workers
before calling oversubscription the cause. The audit probe verified one BLAS thread in its own
process only. Compare a few sensible worker allowances, such as 4/8/16, during a deliberately
scheduled benchmark; 32 logical processors do not imply that 32 workers is optimal or available.

### Reuse the error-layer parallelism that already exists

[`deep_delivery._chosen`](../../isar/session/deep_delivery.py), 121–135, calls
`errors.attach(..., with_profile=True)`. Bootstrap uses the supplied pool, then MINOS and the
profile calculation run serially ([`errors/__init__.py`](../../isar/analyze/errors/__init__.py),
212–238). `attach_batch`, 290–348, already runs MINOS, profile-low and profile-high as separate
tasks before giving bootstrap the pool. Adapting the one selected-model call is a concrete reuse
opportunity; it does not promise to shorten the sampling stage.

## 4. Fresh evaluator measurements

These are generated spectra/resolution inputs, not measured sample data. The probe used six
peaks at energies `[2, 4.5, 7, 10, 14, 19]` meV, a generated asymmetric resolution, elastic plus
constant background, 181 points from −12 to 24 meV, and 250 K. Energies varied over a 0.16 meV
range so an exact parameter cache could not manufacture the saving. Each median is four rounds
of 120 evaluations, with candidate order reversed on alternate rounds. Profiling was separate.

Environment: Python 3.12.10, NumPy 2.4.4, SciPy 1.17.1, dynesty 3.0.0; 32 logical processors;
both loaded OpenBLAS libraries reported one thread. This was the available Python interpreter,
not verification of the GUI's micromamba interpreter. The machine was shared and not isolated.

| Width of each peak (meV) | Current (ms/evaluation) | Height only | Shared log stencil only | Both | Time reduction, both |
|---|---:|---:|---:|---:|---:|
| 0.001 | 1.078 | 1.015 | 0.713 | 0.645 | 40% |
| 0.03 | 1.248 | 1.092 | 0.759 | 0.689 | 45% |
| 0.2 | 0.423 | 0.350 | 0.437 | 0.351 | 17% |
| 1.5 | 0.440 | 0.351 | 0.422 | 0.345 | 22% |

The shared stencil does no additional useful work on these broad cases; small differences there
are timing noise and prototype overhead, not evidence of a broad-peak stencil speedup.
The first, simpler height-only probe independently measured about 6% narrow and 19% broad savings.

**Why this helps.** [`unit_peak_of_nodes`](../../isar/analyze/lineshapes.py), 332–366,
calculates two trapezoid areas even when only height is consumed by the model mean.
The prototype retained the height/vertex calculation and omitted those reductions.

For narrow modes, `_unit_dho`, 291–294, evaluates the same antiderivative at four grid shifts
for each of two poles. Each antiderivative includes a complex logarithm. On the uniform grid,
these are overlapping slices of one extended array. The prototype reused that array while
retaining the current pole window and B-spline formula. It introduced no lookup table or new
compiled dependency. In the baseline narrow profile, DHO signal construction took about 73%
of cumulative evaluator time, complex-log antiderivatives about 38%, and convolution about 6%.
Nested cumulative percentages overlap and are not additive.

**Numerical checks and limits.** At 12 parameter vectors per width, height-only predicted means
were bit-identical. The shared stencil changed means by at most `5.5e-8` counts and the Poisson
log likelihood by at most `8.1e-8` nat for the generated observations checked. These are empirical
checks, not uniform error bounds. Tiny widths, critical damping, grid crossings, high-count
spectra, unknown temperature, Stokes-only responses and fitted curvature still need targeted
validation. The prototype was applied in memory, not to production source.

Local reproduction artifacts are retained under the gitignored `out/fitting_audit/`:
`benchmark.py`, `benchmark.json`, and `run.py`. From the repo root in cmd:

```bat
python out\fitting_audit\run.py out\fitting_audit\benchmark.py
```

The final probe exited 0 in 6.037 seconds under a 60-second child-process timeout that kills the
process tree on expiry. This is an evaluator benchmark; it includes neither nested-sampler
overhead nor end-to-end fitted-result acceptance.

### Other cheap evaluator preparation

- `_unit_dho:269–271` rebuilds grid powers and the fixed-temperature Bose factor. Prepare them
  once for a fixed context; preserve the existing unknown-temperature limit.
- [`components.py`](../../isar/analyze/components.py), 45–63 and 265–289, repeatedly resolves
  interpolation/static-component work. Deep uses the general evaluator through
  [`EvidenceProblem`](../../isar/analyze/errors/bayes.py), 178–193. A prepared callable can
  retain the same likelihood while removing namespace, exposure and elastic-column work.
- [`catalogue.py`](../../isar/analyze/catalogue.py), 498–550, reconstructs exchangeable-energy
  groups inside each ordered prior transform. Precompute the groups and fixed transform
  constants once, without changing their ordering or normalization.

The FFT kernel is **already cached**, multiple phonons **already share a batched convolution**,
and fast fitting **already caches each unchanged mode response**
([`fast_pass.py`](../../isar/analyze/fast_pass.py), 655–673; `lineshapes.py:129–198`).
An amplitude finite difference in the fast pass therefore does not recompute every convolution.
Porting an exact-value cache into nested sampling is less attractive because proposals commonly
change all continuous coordinates. Cache deterministic setup first.

## 5. Quality repairs that should accompany the speed work

| Finding | Current source and evidence | Recommended action |
|---|---|---|
| A failed structural variant can disappear from the deep input | `fast_pass.py:831–833` returns without a basin; `1530–1538` creates placeholders only for wholly unfitted counts; `deep_pass.py:493` reads exported labels | Preserve attempted/declared identities independently of successful fits; retain explicit failure/omission status |
| Finished work can be called a full comparison despite missing masks | `fast_pass.py:1254–1259` generates near-zero masks heuristically; `deep_pass.py:389–402` checks the scheduled set | Separate scheduled completion, declared family coverage, and numerical reliability; report represented prior mass and omissions |
| Deep's own winner at the ceiling is not requalified | `deep_delivery.py:179` inherits the fast limit, but `195–196` never compares the new winner to `mode_ceiling` | Apply the search-ceiling condition to the deep winner |
| Laplace stencil can leave prior support just inside a bound | `prior.py:449–457`; fresh bounded Gaussian probe fails at 0.99999 within [0,1], but succeeds at 0.999 and 1.0 | Use support-aware steps or one-sided stencils; validate with known truncated integrals |
| Gaussian tail-box clipping can greatly inflate evidence | `prior.py:413–414`; exact quadratic probe below | Preserve both endpoints in same-tail intervals and use stable tail/log-difference arithmetic |
| Missing evidence can be ignored for an invalid reason | `_tally`, `fast_pass.py:365–375`; fresh toy probe below | Revisit the completeness policy; comparing best local likelihoods between counts cannot establish that missing evidence is negligible |

The last rule is explicitly encoded by existing tests and an earlier amendment. Changing it is a
policy revision, with an expected effect on abstention, rather than a typographical repair.
Likewise, honest coverage accounting must not silently launch hundreds of extra integrations.
Record and qualify missing coverage first; decide how to integrate it in the model-family work.

**Fresh numerical probes:** the delegated probe completed with exit 0 in 0.739 seconds under a
30-second timeout; its command, full output and named source were inspected for this report.

- For `f(t)=9t+t²/2` on `[0,0.001]`, direct quadrature gives log integral `−6.91225207`;
  production Laplace gives `−2.20921058`, an overestimate of **4.70304149 nats**. This is an
  exactly quadratic objective, so the discrepancy is not a generic failure of Laplace's quadratic
  approximation. `_ln_interval_mass(9,9.001)` also raises `ValueError`; preserving the endpoints
  alone is insufficient without stable positive-tail arithmetic.
- A priced three-peak entry with `lnLmax=−10, lnZ=−50` and an unpriced two-peak entry with
  `lnLmax=−11` produces `P(3)=1` and no unpriced contender. A two-peak `lnZ=−20` is compatible
  with those numbers and would win. Evidence averages likelihood over the prior; a slightly
  worse best fit can have much more supporting parameter volume.

The local 4.5-standard-error width shortcut is also not a Bayes-factor bound: prior density,
posterior volume and other basins matter. The deep pass cannot check a mask it never receives.
Another limitation to track is `prior.py:427–431`: a tiny correlated Gaussian box probability
falls back to a product of marginal probabilities, which is exact only without correlations.
Its frequency and impact were not measured here; it should not be advertised as a proven cause
of current real-spectrum failures.

## 6. Bigger savings: pursue after the cheap work

### Avoid paying for exploration followed by a fresh accepted run unnecessarily

The current accepted settings are 400 live points, random walk factor 4, `dlogz=0.05`;
exploratory settings are 128 live points, the same factor, `dlogz=0.5`.
Promotion uses `3 × sigma + 10` nats with exploratory sigma at least 12.38:
an allowance of about **47.1 nats** (`deep_pass.py:377–381`).
If most exploratory models promote, the cheaper first pass saves little.

The [recorded calibration](../STATS_PRIMER.md#24-the-deep-pass-nested-sampling-evidence-on-the-spectra-you-choose-partial-one-pass-calibration-done-and-its-accuracy-aims-missed-settings-provisional)
shows that a promoted model paid about 31% extra relative to accepted-only, or 24% of
the combined cost, in its five-peak comparison.
Those are earlier measurements on one model, not the observed promotion fraction of today's run.
First extract the current promotion/restart fraction; then compare direct accepted sampling for
predictably promoted work. Do not obtain speed by reducing uncertainty allowances or accepting
suspect runs. Continuing an exploratory sampler also needs its own statistical and persistence
design; it is not equivalent to the current independent rerun.

### Reduce the number of separate width integrations

The external review's regrouping is worth a small two-peak experiment. For exchangeable priors,
group k peaks by the number m of delta peaks, order each group internally, and assign weight
`binomial(k,m)/2^k`. This preserves the joint prior when other priors and constraints permit
exchangeability. At k=6 it replaces 64 rank masks with seven width-count groups per structural
variant. The complete illustrative family through k=6 drops from 507 separate integrations to
111; summing all discrete variants inside each count would use seven integrations.

These are integration counts, **not speedup factors**. The grouped posterior must explore all
interleavings, and the discrete-sum likelihood does more work per call. Current sampling already
has mode-discovery trouble. Check equality against complete explicit sums at k=2, including
prior normalization and hard-fixed widths, before increasing k. Keep physically meaningful
50/50 delta/finite prior weights separate from an implementation convenience.

### Improve exploration before another broad calibration campaign

The primer records missing reference results for deciding counts and a three-seed range
far larger than the sampler's nominal uncertainty on a deciding model. Source still marks every deep decision untrusted while settings
are provisional. Small reported sampler error or successful termination does not establish that
important modes were found. Faster likelihoods do not remove this limitation.

The existing `SamplerSettings` already supports `rslice`; compare it on a few difficult deciding
models first. A configured UltraNest slice-step sampler is another independent trial: the current
calibration tool's UltraNest construction did not configure one. UltraNest's own difficult-fit
example uses slice steps and checks longer step counts for consistency
([official example](https://johannesbuchner.github.io/UltraNest/example-sine-highd.html#step-samplers-in-ultranest)).
Keep repeated-seed evidence, best likelihood, exploration diagnostics, calls and wall time.
Do not initialize live points only around the fast optimum under the unchanged prior transform.

### Fast fitting and approximation work

At fixed shapes, bounded Poisson amplitude fitting is convex. Compare the present joint TRF,
analytic amplitude Jacobians, and an outer shape fit using the existing Poisson NNLS solver.
With six finite peaks plus elastic/background, profiling reduces the outer dimension from 20 to
12, but introduces inner solves. Preserve full joint curvature, including amplitude/shape cross
terms, in the Laplace evidence. Profiled optimization is not Bayesian marginalization.
The existing fast column cache makes a measured comparison essential.
Also check the inner solve's bound-aware optimality conditions (KKT: no feasible amplitude
change lowers the objective) and provide a visible failure/fallback. The current
[`poisson_nnls`](../../isar/analyze/backends.py), 302–332, can return amplitudes after a failed
linear solve, unsuccessful line search or iteration cap without a convergence result.
Convexity establishes the shape of the problem; it does not certify the returned solution.

A fixed-width delta-response table may later help; it must reproduce the pinned finite width
`GAMMA_FLOOR=0.001`, not silently substitute a true zero-width model. Two-dimensional response
tables, smaller pole windows, compiler dependencies, GPU batching and another sampler package
belong after the measured cheap changes. Their accuracy/build costs are larger, and no benefit
was demonstrated for them in this audit.

## 7. Measurement and acceptance for the next implementation

Use the next completed ordinary deep run's existing `run_timings.json` before commissioning
another four-hour run. [`deep_delivery.py`](../../isar/session/deep_delivery.py), 215–219,
already records total/sampling/errors wall time, stage time and per-model run times/calls.
Add only the missing useful pieces: actual worker/thread counts, model start/end timestamps,
promotion/restart totals, and selected-model `method_seconds` (currently dropped at 134–135).
Report worker occupancy separately from sampled CPU utilization, and wall time separately
from the sum of worker durations. A user-entered sampling budget is not currently a hard total
deadline: admitted runs/retries finish, then errors run outside that budget.

Recommended first implementation sequence:

1. **Small correctness repairs and baseline visibility:** boundary numerics, failed identities,
   ceiling qualification, explicit coverage, retained errors timing. Avoid changing count priors.
2. **Scheduling and exact evaluator work:** continuous refill, height-only evaluation, shared
   stencil, immutable preparation, and reuse of parallel profile methods. Each change gets its
   own timing/likelihood comparison so its benefit is attributable.
3. **Only then choose the next method experiment:** use measured promotion cost and remaining
   bottlenecks to choose allocation changes, amplitude profiling, sampler exploration or width
   regrouping. Larger model-family and resource-lending choices remain proposals.

Focused validation should cover scheduler completion/budget/failure/seed behavior, known
bounded integrals, deliberately missing variants and a deep winner at the ceiling. Existing
`tests/analyze/test_fit_core_speed.py` and `test_dho_full.py` cover useful convolution,
height, detailed-balance, narrow-width and curvature behavior. Extend the specific checks
affected rather than rerunning a calibration campaign after each edit. Production slices still
follow the repository's slice-boundary checks; this report-only audit did not run those suites.

For quality assessment, separate confident recall, tentative/abstained cases, wrong-but-resolved
counts and probability calibration. The earlier benchmark's zero-miss number is not confident
recall. Split prior-sensitivity results into changed count, changed confidence state and numerical
failure before interpreting them. Future bounded tests should emphasize weak shoulders, close
doublets, elastic overlap and modest resolution/zero/background mismatch. Intervals for a chosen
model remain conditional on that model when several decompositions are plausible.

**Expected outcome of the first work:** a demonstrably cheaper evaluator and better use of the
existing CPU allowance, with clearer failure and coverage labels. There is not yet enough
end-to-end evidence to promise that four hours becomes a particular number of minutes.

Report validation: the repository's five document checks for corrected phrases, relative links,
status banners, plan metadata and reading-guide reachability passed with the new, untracked
report explicitly included in their inventory (93 Markdown files; exit 0, 0.864 seconds under
the same 60-second wrapper). A separate technical read checked the recommendations and numerical
qualifications. `git diff --check` passed. No fitting implementation or acceptance claim is made.

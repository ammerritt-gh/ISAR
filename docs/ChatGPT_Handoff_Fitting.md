# ISAR fitting: external design review

> **Status:** live
> **Kind:** external design review the operator obtained (2026-10-27) of the fitting design (fast and deep pass), kept as received; not maintained with the code

**I would keep the fast/slow split, but change the deep pass’s model accounting and the way it groups hypotheses before spending much more time tuning sampler settings.** Your four follow-ups are well chosen. There are several additional opportunities, including two that could improve accuracy and performance together:

1. **Make the deep pass independent of which models the fast optimiser successfully records.**
2. **Combine the zero-width/finite-width hypotheses more efficiently, while preserving their prior weights.**
3. **Exploit the convex amplitude-fitting problem already supported elsewhere in ISAR.**
4. **Target the narrow-peak calculation specifically; the FFT is not its main expense.**
5. **Obtain a trustworthy reference for the deciding models before running another broad calibration grid.**

I reviewed the merged heads of [PR #48, `0b59dca39747`](https://github.com/ammerritt-gh/ISAR/pull/48) and [PR #49, `048ddc57e903`](https://github.com/ammerritt-gh/ISAR/pull/49), including the implementation, calibration report in the statistics primer, tests, and accepted-issue ledgers. I also ran focused numerical checks and synthetic probes. The measured sample spectra and full calibration outputs are not committed, so the discussion of a05 uses the repository’s recorded results; the new timings below are local microbenchmarks.

There is a substantial improvement path here, Adrian. The most useful gains involve doing less redundant inference and making the meaning of a completed comparison more precise.

## 1. The most important correctness issue: the deep pass inherits gaps in the fast pass’s model coverage

There are two related problems.

### A failed fast-pass model can disappear before the deep pass sees it

The fast pass creates model identities as it tries variants. However, a failed start adds no basin. At the end, it inserts a `not_fitted` placeholder only when **an entire peak count has no fitted result**.

Consequently, if the one-peak model with elastic line and background fails, but another one-peak variant succeeds, the failed variant can vanish from the exported record. The deep pass builds its sampling list from that record, so it cannot independently rescue the missing model. See the [failed-start handling and model export](https://github.com/ammerritt-gh/ISAR/blob/048ddc57e903be6b1025ed948b729f9f4e78e0e2/isar/analyze/fast_pass.py#L828-L851), [placeholder construction](https://github.com/ammerritt-gh/ISAR/blob/048ddc57e903be6b1025ed948b729f9f4e78e0e2/isar/analyze/fast_pass.py#L1531-L1551), and [deep-pass reconstruction](https://github.com/ammerritt-gh/ISAR/blob/048ddc57e903be6b1025ed948b729f9f4e78e0e2/isar/analyze/deep_pass.py#L430-L475).

**This was reproduced:** on a generated one-phonon spectrum with elastic line and background, forcing the four starts of `elastic+stokes+bg` to fail left that identity inside the engine with zero basins, but absent from the final record. Other one-peak variants remained.

**Recommended fix:** maintain the model manifest independently of successful fits. Record each attempted model with its status, including models with no successful basin. Ideally, construct the deep pass’s eligible family from the model specification and use fast-pass results to prioritize it.

This is a small architectural correction with a direct scientific benefit: optimiser failure stops determining which hypotheses the supposedly independent calculation can examine.

### “Full” currently means full over the recorded subset

The intended model prior gives each structural variant and width mask weight

\\[ p(v,d\\mid k)=\\frac{1}{V(k)\\,2^k}. \\]

But the fast pass generates width masks heuristically. It uses a local covariance test, limits each near-zero expansion to three modes, and skips sufficiently “resolved” widths. The deep pass subsequently samples the labels it receives; it does not systematically inspect the excluded masks. Nevertheless, it can report `comparison_scope="full"` after completing that list. These choices are explicit in the [near-zero mask expansion](https://github.com/ammerritt-gh/ISAR/blob/048ddc57e903be6b1025ed948b729f9f4e78e0e2/isar/analyze/fast_pass.py#L1251-L1277) and [deep-pass completeness calculation](https://github.com/ammerritt-gh/ISAR/blob/048ddc57e903be6b1025ed948b729f9f4e78e0e2/isar/analyze/deep_pass.py#L341-L365).

That matters because missing masks can change the relative evidence of different counts.

For a simple illustration, suppose every model has equal evidence. Count zero has its entire prior weight represented, while count one has only one of its two equally weighted masks represented. Normalizing the available contributions gives

\\[ P(k=0)=\\frac23,\\qquad P(k=1)=\\frac13, \\]

instead of the correct equal probabilities. This is an illustration of the accounting effect, not a claim that the omitted real-data masks have equal evidence.

There is also a weakness in the stated justification for one shortcut. The code comments say that a width at least \\(4.5\\sigma\\) from zero makes its delta model’s evidence smaller by approximately \\(e^{-10}\\). That is a likelihood-tail argument, not generally a Bayes-factor bound. Under matching nuisance priors, the relevant identity is

\\[ \\frac{Z\_{\\delta}}{Z\_{\\mathrm{finite}}} = \\frac{p(\\Gamma\_\\delta\\mid y,\\mathrm{finite})} {\\pi(\\Gamma\_\\delta\\mid\\mathrm{finite})}. \\]

A Gaussian approximation therefore contains the additional factor

\\[ \\frac{1}{\\sqrt{2\\pi}\\,\\sigma\_\\Gamma\\,\\pi(\\Gamma\_\\delta)} \\]

alongside the familiar \\(e^{-z^2/2}\\). Local covariance also does not establish the marginal tail density when other basins or curved degeneracies exist.

**The deep pass can check this fast-pass shortcut only if it actually includes the skipped hypotheses.**

I would distinguish three quantities in its output:

- Completion of the scheduled calculations.
- Coverage of the declared model family.
- Numerical reliability of the evidence estimates.

The existing untrusted label is useful protection, but these distinctions will still matter after the sampler is calibrated.

## 2. The largest structural performance opportunity: combine the width hypotheses

The 143 entries are **not repeated sampling of different optimiser starts**. PR #49 already combines the fast-pass basins for each model label before sampling. The expensive multiplicity comes from genuinely distinct structural variants and width masks. The [record-to-model conversion](https://github.com/ammerritt-gh/ISAR/blob/048ddc57e903be6b1025ed948b729f9f4e78e0e2/isar/analyze/deep_pass.py#L450-L474) handles the former correctly.

There are two promising ways to represent the latter more efficiently.

### Option A: group masks by the number of delta peaks

Instead of a separate model for every rank mask, define a model by:

- Total number of peaks, \\(k\\).
- Number of delta peaks, \\(m\\).
- Elastic/background variant.

Order the \\(m\\) delta energies among themselves and the \\(k-m\\) finite-width energies among themselves. Allow the two groups to interleave.

Give that model weight

\\[ p(m\\mid k)=\\frac{\\binom{k}{m}}{2^k}. \\]

Its energy-prior density is

\\[ \\pi(\\mathbf E\_\\delta,\\mathbf E_f\\mid k,m) =\\frac{m!(k-m)!}{W^k}. \\]

Their product is \\(k!/(2^kW^k)\\), exactly the original joint density for a globally ordered energy vector and equally weighted rank mask. This is an **exact regrouping**, provided the other priors are exchangeable and there are no rank-specific constraints.

At six peaks, this replaces 64 width-mask models with seven models. Different interleavings can still produce multiple posterior modes, so the runtime gain must be measured. However, it removes many separate integrations without introducing unused width coordinates.

### Option B: sum all discrete variants inside a fixed-count likelihood

For fixed \\(k\\), let \\(s\\) identify a width mask and elastic/background choice. Use a shared continuous parameter vector and define

\\[ \\overline L_k(\\theta) = \\sum_s p(s\\mid k)L(y\\mid\\theta,s). \\]

With correctly normalized priors for coordinates unused by a particular state,

\\[ Z_k = \\int \\pi_k(\\theta)\\overline L_k(\\theta)\\,d\\theta = \\sum_s p(s\\mid k)Z\_{k,s}. \\]

Thus, one integration can produce the entire count evidence. Marginalizing bounded discrete choices is an established technique; this is my proposed application of it to ISAR’s particular family. [mc-stan.org](http://mc-stan.org)

The calculation need not perform a convolution for each variant. At a parameter vector:

1. Compute the finite-width and delta response of each peak.
2. Assemble the alternative means from those columns.
3. Evaluate their Poisson likelihoods in a batch.
4. Combine them with `logsumexp`, retaining every model-weight normalization.

For context, an ordinary elastic-admissible family with all four elastic/background variants at \\(k\\ge1\\), three nonempty variants at \\(k=0\\), and a ceiling of six gives:


| Representation                             | Independent integrations for the complete family |
| ------------------------------------------ | ------------------------------------------------ |
| Separate rank masks                        | 507                                              |
| Grouped by number of delta peaks           | 111                                              |
| Discrete variants summed inside each count | 7                                                |


**These are illustrative family sizes, not a claim that a05 admits exactly 507 models.** Its admissibility gates must be checked.

I tested the arithmetic part of Option B locally. For six peaks and 181 energy points, assembling and scoring all 256 mask/elastic/background combinations took approximately **0.30 ms after the response columns were available**. Including the twelve finite/delta columns took approximately **3.18 ms** in that particular benchmark.

That does not establish an end-to-end speedup: the combined integral has different geometry and potentially more active dimensions. It does show that evaluating the discrete combinations need not be prohibitively expensive.

**My recommendation:** prototype both representations at \\(k=2\\), where their answers can be compared against explicit separate-model sums. Option A is the more conservative change; Option B has the greater potential saving and can cover the complete declared family.

## 3. Other correctness repairs worth doing before the next calibration

### The fast pass can dismiss an unpriced count for the wrong mathematical reason

In `_tally`, an unpriced count is considered relevant only if its best likelihood reaches the selected model’s best likelihood. But a smaller model can fit slightly worse and still have substantially higher evidence because its Occam penalty is smaller. The rule is in [the count tally](https://github.com/ammerritt-gh/ISAR/blob/048ddc57e903be6b1025ed948b729f9f4e78e0e2/isar/analyze/fast_pass.py#L340-L380).

A direct probe of the production function used:


| Model       | Maximum log likelihood | Log evidence       |
| ----------- | ---------------------- | ------------------ |
| Three peaks | −10                    | −50                |
| Two peaks   | −11                    | Calculation failed |


The result assigned probability one to three peaks and reported no unpriced contender. Yet a two-peak evidence of −20 would be entirely consistent with its likelihood maximum and would win decisively.

The related rule that *any priced basin supplies the count’s evidence* can also conceal an unpriced, potentially important basin. I recognize that this was deliberately introduced to reduce excessive unresolved outcomes; it is a tradeoff to revisit, rather than an accidental typo.

**Recommended change:** separate “we have an evidence estimate” from “important missing contributions have been excluded.” A local likelihood comparison cannot certify the latter. Even using \\(Z\\le L\_{\\max}\\) requires an actual upper bound on the maximum, not merely the best local fit found.

### A deep-pass winner at the ceiling can be labelled resolved

The deep pass carries forward the fast pass’s search-limit reason, but does not recheck its **own** winning count against the ceiling.

A controlled orchestration test produced:

```
Fast preferred count: 5
Ceiling:              6
Deep preferred count: 6
Deep state:           resolved
Comparison scope:     full
```

Pass the ceiling into the final qualification and apply the same rule used by the fast pass. The relevant code is in [deep delivery](https://github.com/ammerritt-gh/ISAR/blob/048ddc57e903be6b1025ed948b729f9f4e78e0e2/isar/session/deep_delivery.py#L159-L184). The present untrusted flag mitigates the immediate consequence, but this bug would survive a successful sampler calibration.

### Two avoidable numerical problems affect the Laplace calculation

**First, the finite-difference Hessian can step outside the prior support.** The stencil size and the “at boundary” tolerance leave a gap: a valid mode just inside an upper bound can produce infinite stencil values. On a Gaussian with \\(\\sigma=0.03\\), truncated to \\([0,1]\\), the production function succeeded at modes 0.999 and 1.0, but returned `no_evidence` at 0.99999. Use distance-aware or one-sided stencils. [Source](https://github.com/ammerritt-gh/ISAR/blob/048ddc57e903be6b1025ed948b729f9f4e78e0e2/isar/analyze/prior.py#L434-L459).

**Second, the six-sigma box cutoff can materially overestimate a boundary integral.** For the exactly quadratic negative log integrand

\\[ f(t)=9t+\\frac12t^2,\\qquad 0\\le t\\le0.001, \\]

I obtained:


| Calculation                 | Log integral       |
| --------------------------- | ------------------ |
| Direct quadrature           | −6.912252          |
| Production Laplace function | −2.209211          |
| Error                       | **+4.703041 nats** |


This is not failure of a Gaussian approximation: the objective is exactly quadratic. The correction’s Gaussian has mean −9 and standard deviation one, so the retained interval is \\([9,9.001]\\) in standardized coordinates. The code replaces the upper endpoint with infinity because it exceeds six sigma. An absolute omitted-tail criterion is insufficient when the entire retained interval is already in that tail. [Source](https://github.com/ammerritt-gh/ISAR/blob/048ddc57e903be6b1025ed948b729f9f4e78e0e2/isar/analyze/prior.py#L406-L431).

Preserve both endpoints in this situation and use stable tail-probability arithmetic. **I have not measured either defect’s frequency in the real spectra**, and neither explains the independent deep-sampler failures. They are nevertheless precise, inexpensive numerical repairs.

## 4. The fast pass has an unused structural advantage: amplitudes form a convex subproblem

At fixed energies and widths, the expected counts are

\\[ \\boldsymbol\\mu=B(\\phi)\\mathbf a, \\]

where \\(\\mathbf a\\) contains peak heights, elastic height, and background.

For the Poisson negative log likelihood,

\\[ F(\\mathbf a)=\\sum_i\\left[\\mu_i-n_i\\log\\mu_i\\right], \\]

the amplitude Hessian is

\\[ \\nabla\_{\\mathbf a}^2F = B^\\mathsf T \\operatorname{diag}\\!\\left(\\frac{n_i}{\\mu_i^2}\\right)B \\succeq0. \\]

Thus, under the current flat bounded amplitude priors, this is a small convex optimization problem. Generalized variable elimination for bound-constrained Poisson problems has a direct methodological basis. [arxiv.org](http://arxiv.org)

ISAR already has `backends.poisson_nnls` and existing profiling machinery. The new fast pass uses Poisson NNLS to initialize missing heights, then optimizes all coordinates jointly with TRF. See [initialization and joint fitting](https://github.com/ammerritt-gh/ISAR/blob/048ddc57e903be6b1025ed948b729f9f4e78e0e2/isar/analyze/fast_pass.py#L763-L826).

For six finite-width peaks plus elastic/background, profiling the amplitudes turns the difficult outer optimization from 20 coordinates into 12 shape coordinates, with a small inner solve.

I would compare:

- Current joint TRF.
- Joint TRF with analytic amplitude derivatives.
- An outer shape optimizer with Poisson amplitude profiling.

The second is a smaller first experiment; the third may also improve robustness. Neither has a guaranteed speedup because inner solves and changing active bounds have costs.

**Preserve the amplitude volume in the evidence.** Profiling is useful for locating an optimum. It does not replace Bayesian integration. The Laplace calculation must retain the full curvature determinant, including cross terms, or its equivalent block-determinant calculation.

A later, more specialized possibility is to integrate the **total Poisson intensity scale** analytically. Writing \\(\\mu_i=\\Lambda p_i\\), with \\(\\sum_i p_i=1\\), isolates an \\(e^{-\\Lambda}\\Lambda^N\\) factor. The current bounded height priors produce a truncated-gamma integral with a known Jacobian. This removes one stiff direction without changing the prior, but requires more care than the profiling experiment.

## 5. The curve-speed job should target narrow peaks and unnecessary per-call work

Some of your proposed speed work has already landed:

- The resolution kernel’s FFT is cached.
- Multiple phonon signals are convolved in a batch.

Those are present in [the convolver](https://github.com/ammerritt-gh/ISAR/blob/048ddc57e903be6b1025ed948b729f9f4e78e0e2/isar/analyze/lineshapes.py#L129-L198) and [phonon-column construction](https://github.com/ammerritt-gh/ISAR/blob/048ddc57e903be6b1025ed948b729f9f4e78e0e2/isar/analyze/components.py#L45-L63). Further precomputation remains valuable, but “cache the FFT” will not be the main new gain.

Using the committed a05 resolution, six synthetic peaks, 181 points and 302 K, I measured:


| Width of each peak      | Model evaluation time |
| ----------------------- | --------------------- |
| \\(\\Gamma=0.001\\) meV | About 2.52 ms         |
| \\(\\Gamma=0.03\\) meV  | About 2.29 ms         |
| \\(\\Gamma=0.2\\) meV   | About 0.64 ms         |
| \\(\\Gamma=1.5\\) meV   | About 0.64 ms         |


These are local repeated-evaluation timings, not laptop or full-fit benchmarks.

For the narrow case, approximately three quarters of profiled evaluator time was in constructing the DHO signals, chiefly the area-preserving pole correction and its complex logarithms. The convolution itself was about 7%. For broad peaks, computing the normalization and areas became a substantial fraction.

That suggests a specific order:

1. **Precompute the fixed-width delta response as a function of energy.** Its width is pinned, making this a relatively small one-dimensional table.
2. **Separate height normalization from area reporting.** `unit_peak_of_nodes` computes areas during likelihood evaluations even though the likelihood only uses the normalized column. Compute areas when delivering results. [Source](https://github.com/ammerritt-gh/ISAR/blob/048ddc57e903be6b1025ed948b729f9f4e78e0e2/isar/analyze/lineshapes.py#L332-L370).
3. **Hoist fixed quantities:** Bose factors on the internal grid, grid powers, interpolation indices/weights, static component columns, and prior-transform grouping.
4. **Apply compilation to the measured expensive routines.** The narrow-pole correction deserves particular attention.
5. **Then test a two-dimensional convolved-response table over energy and width**, with the temperature and resolution included in its identity.

A delta lookup should preserve the implementation’s pinned finite width, or explicitly validate a change to the true zero-width limit.

For any interpolated approximation, judge error in **log likelihood**, not only maximum percentage error in the plotted curve. If a surrogate satisfies

\\[ |\\widetilde{\\log L}(\\theta)-\\log L(\\theta)|\\le\\epsilon \\]

throughout the integration support, then its log-evidence error is bounded by \\(\\epsilon\\). Empirical checks over sampled points do not establish that global bound, but it is the right numerical quantity to monitor.

My limited grid-refinement probes did not reveal anything resembling the 10–18-nat seed instability. They support treating that instability as an exploration problem, while still checking numerical accuracy before introducing a surrogate.

## 6. There are runtime savings outside the likelihood

### Keep workers occupied continuously

The deep engine submits a chunk of models, waits for the entire chunk, and then submits the next one. A short model can finish while its worker sits idle behind an expensive model. This is visible in [the stage runner](https://github.com/ammerritt-gh/ISAR/blob/048ddc57e903be6b1025ed948b729f9f4e78e0e2/isar/analyze/deep_pass.py#L305-L325).

Use a bounded queue that submits another eligible model whenever a worker finishes, checking the budget before each submission. Keep random seeds attached to model identities.

### Revisit the exploratory stage once the method changes

The near-universal promotion problem is already acknowledged in PR #49. With exploratory \\(\\sigma=12.38\\), the \\(3\\sigma\\) allowance plus the 10-nat relevance window spans roughly 47 nats. Many models consequently pay for both exploratory and accepted runs.

The recorded five-peak times are 796 seconds exploratory and 2584 seconds accepted, so this overhead is material. [Calibration timings](https://github.com/ammerritt-gh/ISAR/blob/048ddc57e903be6b1025ed948b729f9f4e78e0e2/docs/STATS_PRIMER.md#L864-L878).

A future scheduler should allocate work according to uncertainty in the **count decision**, while keeping unbounded missing contributions explicit. A staged scheme earns its complexity only if it actually avoids expensive work.

### Retain expensive samples for reuse

The current persisted run information is mainly scalar diagnostics and a best point. Keeping weighted samples for calibration and selected difficult cases would enable:

- Inspection of missed basins.
- Posterior comparisons between methods.
- Some prior-sensitivity calculations without resampling.

For compatible supports,

\\[ \\frac{Z\_{\\mathrm{new}}}{Z\_{\\mathrm{old}}} = \\mathbb E\_{\\theta\\sim p\_{\\mathrm{old}}(\\theta\\mid y)} \\left[\\frac{\\pi\_{\\mathrm{new}}(\\theta)}{\\pi\_{\\mathrm{old}}(\\theta)}\\right]. \\]

Dynesty provides reweighting support; importance-weight diagnostics are needed to detect poor overlap. This cannot recover support outside the original prior, such as widths from 20 to 40 meV, or repair previously missed modes. [dynesty 3.0.0 documentation](https://dynesty.readthedocs.io/en/v3.0.0/api.html?utm_source=chatgpt.com)

## 7. The next sampler experiment should test exploration mechanisms

The calibration record is more informative than a single “17.6-nat spread” headline. It says the spread is largely associated with runs settling in modes 12–20 nats lower in maximum likelihood. All 84 screening runs finished, but no reference for counts 3–6 was recorded, and all six UltraNest attempts timed out. [Recorded outcome](https://github.com/ammerritt-gh/ISAR/blob/048ddc57e903be6b1025ed948b729f9f4e78e0e2/docs/STATS_PRIMER.md#L805-L862).

That points toward failed exploration. A small `dlogz` or internal `logzerr` does not establish that all relevant constrained-prior regions were sampled. This distinction is central to the nested-sampling diagnostics literature. [arxiv.org](http://arxiv.org)

I would run this narrow comparison:


| Candidate                               | Why it deserves a trial                                                          | Required qualification                                                |
| --------------------------------------- | -------------------------------------------------------------------------------- | --------------------------------------------------------------------- |
| **Dynesty** `rslice`                    | Already supported by ISAR’s settings; changes the constrained sampling mechanism | Calibrate slice length and live population independently              |
| **UltraNest with a slice step sampler** | Independent implementation with useful mixing diagnostics                        | The existing timeout result does not evaluate this configuration      |
| **Nautilus**                            | Importance nested sampling can use evaluations efficiently                       | Check repeatability, exploration diagnostics, and reference agreement |


ISAR’s UltraNest calibration currently instantiates `ReactiveNestedSampler` without configuring a step sampler. UltraNest’s own difficult-problem example specifically demonstrates switching to slice steps when region sampling becomes inefficient. That makes a configured second attempt worthwhile; the timeout is not evidence that this alternative has been exhausted. [ISAR configuration](https://github.com/ammerritt-gh/ISAR/blob/048ddc57e903be6b1025ed948b729f9f4e78e0e2/tools/calibrate_deep_pass.py#L215-L244). [UltraNest 4.6.0 documentation](https://johannesbuchner.github.io/UltraNest/example-sine-highd.html?utm_source=chatgpt.com)

Nautilus is a credible performance candidate, but its published speedups should not be transferred to ISAR. Its documentation explicitly distinguishes stopping from convergence and recommends varying settings; it also offers `discard_exploration=True` to remove its exploration-phase pseudo-importance bias. [arxiv.org](http://arxiv.org)

For each candidate, retain maximum likelihood, mode occupancy, evidence trajectories, repeated-seed variation, likelihood calls, wall time, and termination diagnostics. Insertion-order and relative-jump-distance tests can expose poor constrained sampling, although neither proves discovery of a mode absent from the live population. [arxiv.org](http://arxiv.org)

**Dynamic nested sampling is a later efficiency experiment.** It does not repair missed modes. If used, configure it for evidence: dynesty’s default allocation is 80% posterior-oriented, and its default stopping rule is entirely posterior-oriented. The documented evidence-oriented configuration sets `pfrac=0` in both weighting and stopping. [dynesty 3.0.0 documentation](https://dynesty.readthedocs.io/en/v3.0.0/dynamic.html?utm_source=chatgpt.com)

### Give the warm-start job a precise statistical contract

Simply placing initial live points around the fast optimum would invalidate standard nested-sampling evidence. The initial population must follow the sampling prior. [dynesty 3.0.0 documentation](https://dynesty.readthedocs.io/en/v3.0.0/api.html?utm_source=chatgpt.com)

A principled alternative is posterior repartitioning:

\\[ \\widetilde\\pi(\\theta)=q(\\theta),\\qquad \\widetilde L(\\theta)=L(\\theta)\\frac{\\pi(\\theta)}{q(\\theta)}. \\]

Then \\(\\widetilde L\\widetilde\\pi=L\\pi\\), preserving the target evidence. A properly normalized proposal informed by fast-pass basins, with full support, can accelerate exploration. The literature establishes this construction; its practical benefit here still needs measurement. [Springer Nature Link](https://link.springer.com/article/10.1007/s11222-018-9841-3?utm_source=chatgpt.com)

## 8. The accuracy assessment needs clearer separation of numerical and scientific questions

### “6 of 56 stable” is not necessarily 50 changed peak counts

The sensitivity proof compares

```
(preferred_count, state)
```

so a transition from resolved to close call counts as instability even when the preferred count is unchanged. Its arms also include the radically different legacy amplitude prior alongside modest changes. [Sensitivity comparison](https://github.com/ammerritt-gh/ISAR/blob/048ddc57e903be6b1025ed948b729f9f4e78e0e2/proofs/test_letacon_fast_pass.py#L288-L318), [prior arms](https://github.com/ammerritt-gh/ISAR/blob/048ddc57e903be6b1025ed948b729f9f4e78e0e2/isar/analyze/prior.py#L116-L150).

Before changing priors, extract from the existing results:

- Preferred-count changes, per arm.
- State-only changes.
- Changes to the confident peak list.
- Numerical failures versus successful evidence calculations.

That will tell you how much is prior sensitivity, how much is threshold sensitivity, and how much is numerical instability.

Also, some sensitivity is mathematically expected. If an amplitude ceiling expands by a factor \\(r\\), with posterior support comfortably inside both bounds, each affected amplitude contributes a \\(-\\log r\\) evidence shift. Changing the margin from 1.2 to 3 gives \\(r=2.5\\), or approximately **0.92 nat per additional amplitude** in an adjacent-count comparison.

For comparison, a best-count probability of 0.8 corresponds to log odds of only

\\[ \\log\\frac{0.8}{0.2}=\\log4\\approx1.39 \\]

against all alternatives combined. Even a one-nat numerical error can materially move a borderline decision.

The prior job should also explicitly consider the current **50/50 delta-versus-finite-width weight**. That is a physical modelling choice, not just a normalization convention.

### The fast benchmark is encouraging, but narrower than “accurate peak recovery”

The recorded evaluation reports:

- Zero misses among 551 should-find peaks.
- One false confident peak among 712 confident fitted peaks.
- Hidden-component flags on 13/71 planted shoulder/broad/soft cases.
- No such flags in the 38 low-count planted cases.
- A 79-second median per spectrum against the 60-second aim.

However, “tentative or abstained” is a separate outcome from “missed,” and the calibration examples lie well above the chosen visibility boundaries. Those qualifications are documented in the [benchmark results](https://github.com/ammerritt-gh/ISAR/blob/048ddc57e903be6b1025ed948b729f9f4e78e0e2/docs/STATS_PRIMER.md#L650-L694).

I would add confident recall, abstention rate, wrong-but-resolved count rate, and calibration of reported count probabilities. Concentrate new cases near detection boundaries: close doublets, weak shoulders, broad low-energy contributions, and peaks near the elastic line.

Then add a controlled model-mismatch layer: modest resolution-width or tail errors, energy-zero errors, and background departures. These are tests I recommend, not defects I have established in the current real-data treatment. Perfect integration of an imperfect response model can still produce a convincing extra peak.

Finally, keep uncertainty over count and decomposition visible in reported parameters. A chosen-model profile/bootstrap interval is conditional on that model. Where several models remain plausible, compare or average matched peak quantities across them, while leaving ambiguous assignments explicit.

## 9. How I would organize the next work


| Order | Work                                                                                       | Concrete success condition                                                                                          |
| ----- | ------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------- |
| **1** | Fix model recording, deep ceiling qualification, and the two Laplace boundary calculations | The focused failure probes behave correctly; omitted model coverage is explicit                                     |
| **2** | Optimize the measured hot path and compare Poisson amplitude profiling                     | Lower runtime on frozen inputs, with unchanged likelihood values and checked fit/evidence behavior                  |
| **3** | Establish references for the hard deciding models using alternative sampling mechanisms    | Agreement across independent runs and methods at the stated accuracy, with retained diagnostics                     |
| **4** | Compare grouped-width models and discrete marginalization                                  | Agreement with explicit separate-model sums at small \\(k\\), then improved complete-count coverage per unit time   |
| **5** | Reassess priors and decision calibration                                                   | Per-arm sensitivity is interpretable; confident claims remain reliable near detection and model-mismatch boundaries |


For the bounded calibration run, secure the difficult reference first, then expand coverage. Calibrating one top variant per count is a useful sampler test, but the eventual scientific target is the **sum over all material variants of that count**.

My strongest recommendation is to add **independent model coverage and exact width-hypothesis regrouping** to your follow-up list. Pair those with the existing Poisson amplitude solver and the measured narrow-peak hot path. Those changes address why the calculation is expensive, what it can miss, and whether a completed answer deserves its confidence label—all before asking a better sampler to do the same 143 separate jobs.
**I would keep the hybrid workflow and revise several parts of the statistical design before implementing it.** The central idea is useful: let clear spectra help resolve ambiguous ones, preserve what each spectrum said independently, and bring difficult cases to the operator. The main risk is that the algorithm could produce a stable, smooth result while overstating how much the data actually determined.

I read the [design](https://chatgpt.com/library?file_id=libfile_d1652b3146948191abc7da80b4cdc11a&account_id=personal) against the [full transcript](https://chatgpt.com/library?file_id=libfile_55ff3d49c1b4819193a4a307b1ff959b&account_id=personal), checked the relevant current ISAR fitting code, and looked at the paper being used for calibration. The final design captures your decisions well. My strongest concerns are about the interaction between **penalties, neighbour-informed evidence, branch identity, and flags**.

There is also a straightforward answer to the design’s final question: **reusing the covariance calculation method is reasonable; copying the fast solo fit’s numerical error bars is not.** A deep pass can remain outside this job.

## 1. The overall workflow is worth keeping

The transcript arrives at a sensible division of responsibilities:

- **The user chooses anchors.** This gives ISAR a starting interpretation the scientist actually trusts.
- **The anchored pass establishes a useful initial solution.** Restricting it to already processed neighbours makes its history understandable.
- **Later simultaneous batches allow corrections to propagate in both directions.** An initial mistake can be revisited.
- **History, authority, and locks have distinct meanings.** An unlocked choice can be improved by a later requested batch; a lock preserves the chosen fit.
- **The existing Processing dock and Scan(s) view are sufficient.** The proposed additions fit the way you want to operate.
- **The 30-minute target remains a performance target.** Measuring an overrun should lead to optimization.

I would also preserve the distinction between *fitting a difficult mode successfully* and *recognizing that it needs attention*. That is particularly valuable during beamtime.

The document’s remaining open questions are uneven in importance, however. Choosing a starting tolerance is calibration work. Deciding what evidence means after neighbours change the priors is part of the algorithm’s definition. **That second category needs to be resolved before the implementation plan can treat the fitter as specified.**

## 2. The biggest issue: continuous refinement and count selection need separate contracts

The design currently describes two operations:

1. Minimize one shared objective consisting of each spectrum’s Poisson fit cost plus coupling penalties.
2. Change a spectrum’s mode count by comparing evidences computed with neighbour-informed priors.

Both are reasonable ingredients. They do not automatically optimize the same quantity.

For fixed counts and branch assignments, a shared objective can be written schematically as

\\[ F(\\theta)= \\sum_s D_s(\\theta_s) + \\sum_f P_f(\\theta), \\]

with the existing local prior terms included consistently. Here \\(D_s\\) is the Baker–Cousins data cost, and \\(f\\) indexes coupling terms.

An evidence comparison additionally integrates over parameter volume. Consequently, a count change preferred by evidence need not decrease this MAP-style objective. Moreover, changing the neighbouring fits changes the conditional prior used in the next comparison.

That does **not** invalidate your decision to let neighbours tip an ambiguous spectrum toward two modes. It means the design needs to describe what each stage promises.

### A practical implementation contract

I would use the following structure:

1. **Propose count and assignment alternatives using a frozen neighbour snapshot.** All alternatives in a particular comparison see the same neighbour information.
2. **Compare them using properly normalized neighbour-informed priors.** Record the result as conditional evidence or neighbour-conditioned model support.
3. **Hold counts and assignments fixed during continuous refinement.** This stage minimizes one explicit shared objective.
4. **Reopen count and assignment alternatives in subsequent outer rounds when appropriate.**
5. **Declare convergence only when counts, assignments, and continuous parameters have stabilized.**

This retains your option (ii) and the existing per-spectrum fitting machinery. It also gives an honest meaning to “settled.” The implementation should not claim monotonic improvement of one score across every count-changing operation unless it actually establishes that property.

Likewise, the transcript’s assurance that iterative refits reach the same answer as a single joint optimization is too strong. With overlapping DHOs, changing assignments, and bounded penalties, different initializations can reach different local solutions. Your history and re-anchoring workflow already provide a useful way to deal with that.

### The penalty and the informed prior must describe the same coupling

This is a concrete integration point in ISAR.

The current [evidence implementation](https://github.com/ammerritt-gh/ISAR/blob/main/isar/analyze/evidence.py) explicitly uses the data-only cost and includes transported priors once. That is a good foundation.

If \\(P\\) is expressed in deviance units, its corresponding prior factor is proportional to

\\[ \\exp(-P/2). \\]

Adding \\(P\\) to the objective and then adding a separate narrow energy prior for the same neighbour information would count that information twice.

There is a second trap: the existing transported soft constraint in [the prior-resolution code](https://github.com/ammerritt-gh/ISAR/blob/main/isar/analyze/catalogue.py) becomes a truncated Gaussian. A bounded coupling penalty cannot simply be routed through that interface unchanged. A Gaussian contributes a quadratic log penalty, which continues growing across the allowed domain. That could undo the very behaviour the bounded penalty was chosen to provide.

**The experts should demonstrate that optimization and evidence use the same normalized coupling density, with the same tails, support, and strength.** Prior normalization matters to model selection even when it does not change the location of a fixed-model optimum. [arxiv.org](http://arxiv.org)

### Mutual support is allowed; repeated fitting must not manufacture independent evidence

You explicitly accepted two weak neighbours supporting a second mode. I would keep that.

The distinction to preserve is between those two spectra jointly suggesting a mode and repeated sweeps making the same suggestion look increasingly independent. In particular:

- Do not multiply previous fit evidences back in as new observations.
- Do not progressively narrow predictions merely because the same fits have circulated through more sweeps.
- Record which neighbour snapshot produced a conditional score.
- Avoid comparing scores from different snapshots as though only the fitted parameters changed.

A fit can legitimately depend strongly on its neighbours. Its provenance should make that dependence visible.

## 3. “A newly fitted peak” needs to be distinguished from “a new branch”

This is the most important correction to the mid-window entry rule.

Consider the exact success case motivating the feature:

- Two modes are clearly separated in spectrum A.
- Spectrum B has them overlapping, so its fast solo fit selected one broad component.
- The inter-spectra pass recovers two components in B.

One component has been **added to B’s fitted model**, but no new branch has appeared along the dispersion. Charging that recovery the full mid-window birth penalty would penalize the result you wanted.

The same distinction matters when a branch becomes weak and later reappears. IXS intensity depends on polarization projections and interference within the unit cell, so a branch can become difficult to observe while continuing through the measured energy range. The one-phonon structure factor makes that dependence explicit. [arxiv.org](http://arxiv.org)

I would preserve your requested larger cost for a mid-window appearance, while defining the event more carefully:


| Event                                                                     | Suggested interpretation                        |
| ------------------------------------------------------------------------- | ----------------------------------------------- |
| A local split recovers two branches already supported nearby              | Newly resolved components                       |
| A known branch has too little local intensity to determine its parameters | Not resolved or not observed here               |
| A component has no plausible association with nearby branches             | Unmatched branch appearance                     |
| A supported branch ceases to be identifiable locally                      | Loss of observation, with the reason unresolved |


The stronger birth cost belongs primarily to the **unmatched branch appearance**. A recovery or temporary non-detection may still deserve a recorded flag, but should not automatically be treated as creation or destruction of a phonon.

This does not require forcing every spectrum to contain every branch. A branch association can exist without claiming that its local energy or amplitude was measured.

### Assignment affects evidence, not just labels

Suppose neighbours show branches A and B, but a local candidate contains one component. “It is A” and “it is B” are two possible assignments.

If both are retained, their probabilities need normalized weights. Schematically,

\\[ \\pi(E)=w_A\\pi_A(E)+w_B\\pi_B(E), \\qquad w_A+w_B=1. \\]

The corresponding evidence is the weighted combination of the assignment evidences. Taking an unweighted sum, or simply taking whichever assignment gives the best score, introduces a preference related to how many matchings were tried.

The design need not retain every conceivable assignment. Keeping a few plausible alternatives and marking an unresolved crossing is consistent with your triage goal.

There is a specific code integration issue here too: ISAR currently canonicalizes mode blocks by energy, while its ordered prior machinery treats appropriate groups as exchangeable. **Persistent branch identity must remain separate from energy ordering once branch-specific priors are introduced.** An energy sort must carry the correct parameter block, covariance, assignment, and prior association with it. The relevant existing machinery is in [components.py](http://components.py) and [catalogue.py](http://catalogue.py).

Finally, define the birth cost in the model/assignment odds or an explicitly normalized joint local prior. A constant penalty attached to a particular count can disappear if it is placed inside that count’s parameter density and then normalized away.

## 4. Curvature changes the optimization graph

The choice of curvature across q is sensible, but the design repeatedly calls it a “link.” **A curvature term involves three spectra.** This affects both the local fit objective and parallel scheduling.

For equally spaced points,

\\[ r_i=E_i-\\frac{E\_{i-1}+E\_{i+1}}{2} \\]

is a local deviation from a straight line.

When spectrum \\(i\\) changes, it changes the curvature terms centered at \\(i-1\\), \\(i\\), and \\(i+1\\), wherever those terms exist. Therefore:

> Each per-spectrum refit must include every objective term containing that spectrum, including terms centered on neighbouring spectra.

This is particularly important at endpoints. The design says an endpoint participates through its neighbour’s curvature. That only happens if the endpoint update includes that neighbouring term.

### Ordinary nearest-neighbour checkerboarding is insufficient

Spectra \\(i-1\\) and \\(i+1\\) are not nearest neighbours, but both occur in the curvature term centered at \\(i\\). Updating them independently at the same time does not give the independent coordinate updates described in the design.

The scheduling rule should therefore be:

> Spectra in the same parallel update group share no coupling term.

Three groups can work for the regular row/column triples and T pairs envisaged here. The implementation should verify the rule from the actual terms, rather than assuming that any grouping of non-nearest-neighbours is valid.

### Use the actual spacing

With unequal spacings \\(h\_-\\) and \\(h\_+\\), the straight-line prediction is

\\[ \\widehat E_i= \\frac{h\_+E\_{i-1}+h\_-E\_{i+1}}{h\_-+h\_+}, \\qquad r_i=E_i-\\widehat E_i. \\]

The spacings should come from a defined physical reciprocal-space coordinate. Euclidean distance between raw hkl triples is generally not that distance; the reciprocal metric matters.

The design also needs a modest rule for triplets that are only approximately collinear. Grid adjacency supplies the neighbourhood, but the accepted q values must supply a meaningful path coordinate or geometric tolerance.

Two consequences should be explicit:

- The residual above has units of energy. It measures departure from a local line; it is not itself a second derivative.
- Under the stated no-fallback rule, a q-only component containing two spectra has no curvature term. The batch summary should make that visible.

None of this requires a fitted \\(\\omega(q,T)\\) surface. It requires a precise definition of the local terms already proposed.

## 5. The flags need to detect uncertainty and damping, as well as disagreement

The existing flag proposal is a good start, but it has several blind spots.

### Compare coupling strain at the same count

The design compares the coupled fit’s data cost with the fast solo fit’s data cost. Those fits may have different numbers of modes.

If the coupled fit adds a component, it can improve the raw-count likelihood. The resulting “regret” can become negative and cancel a large mismatch term, even though that is not what the diagnostic is intended to mean.

For the cost of coupling, use a release fit:

\\[ R_s= D_s(\\widehat\\theta\_{s,\\mathrm{coupled}}) - \\min\_{\\theta_s\\text{ at the final count}} D_s(\\theta_s). \\]

Practically: start from the final coupled result, retain its count and forward model, remove neighbour coupling, and refit against its own counts. This measures how much the neighbour constraint cost that spectrum.

The change relative to fast solo remains useful, but it should be shown separately. It answers “what changed from the initial analysis?” rather than “how much tension did coupling introduce?”

The mismatch term also needs explicit dimensionless normalization before being added to a deviance. A threshold such as nine should not acquire an ordinary “three sigma” interpretation without validation.

### A well-behaved energy can coexist with an interesting linewidth

A mode can broaden strongly while retaining essentially the same \\(E_0\\). Its own-data fit can remain good, its count can remain unchanged, and its energy mismatch can be small. The proposed flag score could then remain quiet.

That misses part of your motivating physics.

I would keep linewidths free, while adding diagnostics for substantial changes in damping, entry into a poorly resolved regime, and ambiguity with the central response. The motivating paper itself describes broadening and cases near L where reliable separation became impossible. [publikationen.bibliothek.kit.edu](http://publikationen.bibliothek.kit.edu)

These diagnostics do not require a linewidth smoothing prior.

### A neighbour can select a point on a broad ridge without resolving it

This is an even subtler case.

Suppose the local DHO fit has a long energy–width ridge. Coupling moves it onto the neighbours’ preferred energy with almost no deterioration in likelihood. The final mismatch is also small.

The score now says everything is comfortable, but the local spectrum still did not determine the energy.

The result should retain a distinction between:

- a mode well determined by its own spectrum;
- a mode stabilized mainly by neighbouring spectra;
- a mode still poorly identified.

Existing diagnostics such as bound hits, ill-conditioned covariance, failed uncertainty estimation, and strong parameter correlations can contribute. Selected difficult cases may justify a short profile calculation. A deep pass on every spectrum is unnecessary.

### Smooth cumulative temperature changes can evade local thresholds

If every adjacent temperature step is small, a branch can move substantially over the full temperature range without any one link crossing the threshold.

Your goal includes noticing that a branch varies while others stay steady. I would therefore add a cheap branch-level diagnostic for total temperature dependence or a fitted trend, alongside local mismatch flags.

With only two temperatures, report a substantial difference over that interval. The data cannot establish whether the intervening evolution was smooth or abrupt.

### Successful repairs should not create an unmanageable review list

The design flags every count changed through neighbour priors. That is transparent, but it also flags a central success mode.

I would retain the reason and make its severity distinguishable in the existing interface: a neighbour-supported split deserves a different presentation from an unresolved identity, serious data tension, or nonconvergence. Otherwise the system risks handing every repaired ambiguity back to the operator.

## 6. Penalty calibration needs to separate three jobs

The discussion sometimes asks the same penalty to:

1. stabilize an ambiguous fit;
2. prevent implausible branch matches;
3. decide what deserves attention.

These jobs can use related quantities, but they need not have identical behaviour.

### A bounded penalty can protect a real anomaly while weakening identity discrimination

Once a penalty saturates, two very different mismatches can have nearly the same cost. That is useful for limiting how strongly a neighbour pulls a genuinely different mode. It also means the penalty alone becomes less effective at deciding whether two distant peaks are the same branch.

The assignment layer therefore needs an explicit unmatched alternative and a sensible treatment of assignment uncertainty. Otherwise a wildly displaced peak may retain an old branch identity simply because the penalty has already reached its ceiling.

Similarly, uncertainty in the neighbour’s energy should influence how informative its prediction is. A broad, unresolved neighbour should not provide the same narrow prediction as a sharply determined one merely because both use the same configured physical tolerance.

### The fourth-root rule is a reasonable experiment, not a transferable law

For T links, an explicit form might be

\\[ s_T(\\Delta T) = s\_{\\mathrm{ref}} \\left(\\frac{|\\Delta T|}{T\_{\\mathrm{ref}}}\\right)^\\alpha, \\]

with a sensible small-step floor and a defined treatment of repeated measurements at the same temperature.

The reference scale makes the units clear. The exponent expresses a modelling choice.

There is a useful consequence to test. For a branch with a steady temperature drift,

\\[ \\frac{|\\Delta E|}{s_T(\\Delta T)} \\propto |\\Delta T|^{1-\\alpha}. \\]

With \\(\\alpha=1/4\\), adding intermediate temperatures makes every local normalized mismatch substantially smaller. In a simple synthetic calculation, a 4 meV drift over 200 K gives a normalized mismatch of about 3.36 as one step, but about 0.60 per step when sampled in ten equal intervals, using a 1 meV tolerance at 100 K.

That calculation does not reject the rule. It shows why a local threshold alone cannot reliably represent “this branch changes appreciably with temperature.”

For q curvature, the scaling is different: the local-line residual of a smooth quadratic branch scales with \\(h\_-h\_+\\). **The T first-difference exponent should not be copied onto q curvature by default.**

### Pin the scientific parameter explicitly

The code inspection gives a reassuring result: ISAR already fits `omega0` as the undamped \\(E_0\\), including overdamped configurations. Figure 4 of the paper also reports the undamped energy, distinguished in its Methods from the damped energy. Keep that exact convention in coupling, matching, and calibration. [publikationen.bibliothek.kit.edu](http://publikationen.bibliothek.kit.edu)

For amplitudes, use the intrinsic quantity already exposed by ISAR through \\(A=h/\\text{UnitPeak.height}\\). Sharing an analyzer does not make convolved heights comparable when energy and linewidth change. The paper’s fixed quantity was its intrinsic structure-factor parameter, with additional qualifications around the avoided crossing. [publikationen.bibliothek.kit.edu](http://publikationen.bibliothek.kit.edu)

Energy-only coupling remains a reasonable default while that amplitude experiment is evaluated.

## 7. Error bars must belong to the new fit

The wording “refits carry the fast pass’s covariance error bars” should be replaced.

After refitting:

- the optimum may have moved;
- the curvature may have changed;
- the mode count may differ;
- parameter associations may differ;
- the result depends on fitted neighbours.

Copying old errors cannot represent those changes.

Recomputing covariance at the final solution is a useful minimum, but holding neighbours fixed gives **conditional** uncertainties. These can be much smaller than uncertainties that include the neighbours’ uncertainty.

A simple Gaussian example illustrates the difference. Suppose two independent spectra each measure an energy with standard error 1 meV. Under a very strong equality tie, their shared energy should approach an uncertainty of \\(1/\\sqrt{2}\\) meV. Holding the other fitted energy fixed instead can make the local conditional error approach zero as the tie becomes stronger.

The extra precision in the latter calculation comes from treating an estimated neighbour as known.

My recommendation is:

- Recompute uncertainty for the final fit.
- Label local covariance errors as conditional on the neighbouring fits and selected model.
- Report unavailable or poorly constrained uncertainty when that calculation fails.
- Do not recycle conditional errors through successive sweeps as independent measurements of precision.

A joint Hessian calculation after counts and assignments settle is a possible improvement for marginal covariance estimates. It does not inherently require nested sampling, although it still cannot resolve multimodal assignment uncertainty.

The user-facing answer to the pending design question can therefore be: **use the existing covariance approach on the new solution, with an honest conditional label; leave coupled deep sampling outside this job.**

## 8. A few batch and history semantics need explicit answers

These are smaller than the statistical issues, but they are normal-use cases.

### Multiple anchors can meet between spectra

The design describes growth fronts meeting at a spectrum that sees both sides. They can also meet across an edge.

Consider four spectra in a row, with the two endpoints anchored. Both interior spectra are ring 1. If fitted in parallel, each sees its own anchor and neither sees the other. Their mutual discrepancy was never charged during that anchored pass.

A clean rule is:

- freeze eligible conditioning fits at the start of each ring;
- fit against earlier rings, anchors, and valid locked boundaries;
- evaluate diagnostics on all active relationships at completion, including same-ring seams;
- let a later simultaneous batch reconcile those seams.

That preserves predictable parallel rings. The document should avoid promising that every meeting seam has already influenced its fits.

### Branch identities must agree around loops

The q rows, q columns, and T links create loops. Individually plausible pairwise matches can return to the starting spectrum with branch labels permuted.

Reusing `track_path` may help, but the result needs a consistency check when paths meet. Conflicting assignments can remain ambiguous and flagged; a large global tracking solver is unnecessary.

### Scientific staleness includes conditioning inputs

If B was fitted using A, then changing A’s calibration can undermine B’s coupled result even when B’s own inputs did not change.

Record immutable conditioning-fit references and scientific-input fingerprints. A batch or connected-component fingerprint may be sufficient initially.

Distinguish this from ordinary later authority changes: selecting another historical fit need not recursively invalidate the old history. Changing the scientific inputs on which that history depended is a different event.

A stale locked fit also needs an explicit rule. It can remain visible and locked, but it must not silently condition new fits as though it were current.

### Completion and acceptance must remain distinct

A numerical optimizer returning is not enough to make a result authoritative. The design already records failed and rejected attempts; specify the acceptance conditions and check current locks and input versions before publishing a late worker result.

Also state what happens when missing hkl or failed fits leave part of an anchored scope unreachable. The progress display should distinguish completed, failed, and not reached spectra.

## 9. Performance should be measured around a warm refit, including count changes

The planned early timing on a real row is exactly the right step. It should include the overlapping, ambiguous cases that motivate the feature.

A useful arithmetic check is

\\[ t\_{\\mathrm{wall}} \\approx \\frac{N\_{\\mathrm{spectra}}\\, N\_{\\mathrm{sweeps}}\\, t\_{\\mathrm{refit}}} {N\_{\\mathrm{workers}}}, \\]

before barriers and other overhead.

Using 14 sweeps as an illustrative case:


| Batch      | Workers | Mean refit time compatible with 30 minutes, before overhead |
| ---------- | ------- | ----------------------------------------------------------- |
| 28 spectra | 4       | About 18.4 seconds                                          |
| 56 spectra | 4       | About 9.2 seconds                                           |


These are requirements implied by the arithmetic, not measured ISAR performance. At 60 seconds per refit, the corresponding idealized totals become 98 and 196 minutes.

The benchmark should separate continuous refinement, count/assignment search, likelihood evaluation, and final uncertainty calculation. A fast fixed-count experiment does not establish the runtime of the requested one-versus-two recovery.

The most useful implementation choices are likely to be:

- warm continuous refinement without repeating the full initial candidate search;
- explicit rounds for structural proposals;
- reuse of existing convolution and basis calculations;
- one controlled parallelism budget;
- final uncertainty work after stabilization, where intermediate calculations are unnecessary.

If many sweeps are required, benchmark a sparse joint continuous update for fixed counts. The transcript’s assumption that one larger optimizer must be slower is not established.

The target should retain exactly the meaning you gave it: an incentive to improve computation.

## What I would ask the expert pair to return before implementation

I would ask for a compact algorithm specification and a prototype demonstrating the specific promises:


| Demonstration                                           | What it must establish                                                           |
| ------------------------------------------------------- | -------------------------------------------------------------------------------- |
| Two resolved neighbours surrounding an overlapping pair | The middle spectrum can recover two modes without a spurious branch-birth charge |
| A branch fading and reappearing mid-window              | Non-detection does not force fabricated finite intensity                         |
| A broad energy–width ridge                              | Neighbour stabilization remains distinguishable from local identification        |
| Strong broadening at nearly fixed \\(E\_0\\)            | The operator receives a useful diagnostic                                        |
| A three-spectrum curvature problem                      | Endpoint updates include the correct terms and parallel updates are valid        |
| Two opposing anchors with a same-ring seam              | The final discrepancy is detected                                                |
| A crossing and a closed loop of matches                 | Identities remain consistent or explicitly ambiguous                             |
| Different q spacings and T sampling densities           | Tolerance and alert behaviour follow the intended interpretation                 |
| Changed inputs after coupled fitting                    | Dependent results and stale locks are handled coherently                         |
| An actual DHO row with count changes                    | Runtime measures the requested feature                                           |


The written specification should define the coupling density, its normalization, the count/assignment comparison, the continuous objective, the acceptance rule, and the meaning of the reported errors.

**My recommendation is to proceed with the workflow as designed and resolve those algorithmic definitions first.** The GUI outline is already coherent. The substantive work is ensuring that a smooth fitted branch, a neighbour-supported estimate, and a well-measured phonon remain distinguishable. That distinction is what will let this feature save the operator’s attention while still bringing phonon B to the front of the queue.
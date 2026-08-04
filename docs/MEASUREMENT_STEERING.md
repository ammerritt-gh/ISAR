# Autonomous measurement steering — the research foundation

> **Lifecycle:** Current
> **Authority:** canonical for the steering research foundation and the current controller contract
> **Provenance:** hand-written
> **Verified against code revision:** `b6db5bc` (2026-08-03)

> **Status: design locked; the core is built.** The model-free descriptor (L0), branch model +
> normalized branch-assignment scoring, soft/hard Γ constraints, Stokes-only rescue, under-model
> falsification, cost-normalised EIG acquisition, and the anti-Stokes fold + rival-branch banking
> are implemented (`isar/analyze/descriptor.py`, `isar/session/branch_model.py` /
> `branch_assignment.py`, `isar/drive/acquisition/eig.py`); what ran and what failed is recorded in
> `campaigns/2026-06_MVP_FIRST_RUN.md` and `campaigns/2026-07_MVP_ZONE_RETEST.md`.
> **Branch-identity-aware acquisition (§2 fourth consequence, §4.1 item 7, §4.5): first
> implementation landed 2026-07-17** (DECISIONS O6–O12; user-facing reference
> `docs/HYPOTHESES.md`); joint gap-aware tracking and the connected frontier landed 2026-07-18
> (O15). Dynamic tolerance-driven exposure and Bayesian optic cold-start are Partial
> as of 2026-07-18 (O13); sampled Stage-C information and Fisher dwell optimisation remain planned.
> This document remains the durable record of *why*
> the loop steers the way it does — what the literature actually says and the design options we
> considered — behind the "R" in ISAR that acts to *resolve a measurement problem* (`DESIGN.md`). Where a claim rests on a paper we
> give the citation; citation-confidence flags are collected at the end (the reference list is meant
> to seed a reading library).

### Tracked-branch frontier and evidence semantics (updated 2026-07-21)

`target_selector="tracked_branch"` makes identity coverage precede final precision. The driver first
confirms an optic target at the point nearest reduced q=0. A gapless acoustic target instead starts at
the nearest point whose predicted energy is separated from the elastic line by the local resolution;
the zone-centre point is classified later from connected neighbors or remains explicitly ambiguous.
The walk then advances only to adjacent q connected to the confirmed component. One unresolved point
permits at most `maximum_bridge_steps=1` provisional quicklook beyond it; the bridge supplies evidence
for a joint forward/backward reassignment but cannot seed the prior, certify the objective, or extend a
plotted line.

Tracked campaigns use this connected walk from their resolvable anchor; they do not begin with a full
path survey. At the anchor and every later frontier, acquisition distinguishes statistical evidence
from identity evidence, and these are **separate adequacy questions**. Candidate precision above the
objective tolerance permits another same-q production. Precision adequacy does *not* settle identity:
whether the branch label is decided is the tracker's own confirmation predicate
(`local_identity_clear`, from `target_probability` and `identity_entropy`), never the ω₀ interval
width. Reading the interval as an identity verdict is what stopped OP9/OP10 (O27) — a 0.129 meV
interval against a 0.5 meV tolerance made an undecided assignment look settled.

Once candidates are precise but their identity is undecided, cheap discrimination still comes first:
the driver prefers an unvisited adjacent classification point, chosen by expected classification
information with path order as the deterministic tie-breaker, and the whole path is retracked after
that result before another proposal is made. A neighbor-informed same-q revisit still requires that
neighbor to have produced fitted candidate/track evidence — a submitted or visited-only quicklook is
insufficient.

When those routes are spent the driver does **not** stop while authorised work remains. Because a
tracked target is confirmed by continuity, an undecided identity can be blocked by a hole in the
*rival* trajectory rather than by anything about the frontier. Acquisition therefore walks back along
the confirmed run and, where a q resolved fewer modes than the frontier does, spends a production
there to restore the rival evidence, bounded per gap q so a reconnect that never resolves yields the
beam. Only below that does the frontier receive more of its own exposure. A proposal-less frontier
that still holds authorised same-q work is a controller fault and raises `ControllerInvariantError`
rather than being reported as a scientific `branch_ambiguity` result.

The state contract deliberately separates three facts. A current pooled fit is **evidence** that q was
measured and preserves every fitted mode candidate. A branch assignment is **identity** and may remain
ambiguous. Only a confirmed target master is **delivery**: it supplies authoritative `omega0`, seats
convergence, or refreshes the target prior. Candidate modes remain visible as open, unwhiskered markers
while delivery is null.

`maximum_production_batches_per_q` is a same-q strategy ceiling, not a quota and not an absence verdict.
It is also the **single** same-q budget: every production at a q charges to it, whether the driver
called that scan statistics, an identity revisit, or a rival reconnection. `maximum_bridge_steps`
means path geometry only — how far acquisition may cross an unresolved gap — and never caps same-q
counting. Charging identity revisits to the geometry knob (a default of one) is the second half of
the OP9/OP10 stop (O27); it made the production ladder, the ncount escalation and `exposure_saturated`
unreachable at an ambiguous frontier. `identity_revisit_sequences` survives as provenance, not as a
spending allowance.

Adequate ambiguous evidence switches to an adjacent differentiation probe immediately; it does not wait
for the ceiling. Statistics-limited evidence may continue at the same q until that ceiling, at which
point the driver must try an unmeasured adjacent probe before revisiting the frontier. The durable
outcome is `incomplete` with `branch_ambiguity` plus `frontier_blocked` only once the **complete
registered action inventory** is exhausted *and* the q is genuinely done: its same-q ceiling reached,
its repeated requests clamped to the same instrument dwell, or a saturation diagnostic independently
proved it. Exhaustion is not "no connected classification route remains" — that local approximation,
computed before acquisition ever enumerated the action space, is the early-terminal defect O28 repaired.
It is `ActionInventory.terminal_cause` proving the space empty *relative to* `GENERATOR_REGISTRY`, so
every declaratively registered generator — the narrow rival probe included — is asked before the
campaign may call itself blocked, and growing the registry cannot leave an old proof looking current.
Once an action is selected, `ScanDesignInventory` resolves it into window/count candidates. The
`legacy_uniform/1` candidate — the exact historical window arithmetic — is always generated and is
always the selected, executed one. The `scan_window_eig_mode` steering knob decides what else is
recorded beside it: `off` designs that candidate alone (`legacy_control_off`), while `shadow`
additionally generates the alternative uniform-window family and, when one exact completed source
master passes the frozen eligibility and policy joins, scores only `legacy_uniform/1` against
`target_core/1`. A valid score records `shadow_legacy_control_scored`; every scoreless or unavailable
case records `shadow_legacy_control_unscored`. Both retain the unique legacy selection and leave the
emitted request unchanged by a single neutron.
Declaring `authoritative` refuses at campaign setup with `scan_window_eig_authoritative_unavailable`,
because no authoritative selector exists. The derived `/3` inventory is content-addressed under
`report/scan-design/<sha256>.json` and referenced from proposal provenance. Its candidate id names the
pre-finalization design; the existing proposal fingerprint separately names the exact request after
live point/count clamps. A changed realization appends an `isar.scan_design_realization/1` record
linking those identities through initial finalization, target-support step reduction, or missed-window
recovery. Every physical submission and acquisition-provenance entry repeats the design reference,
candidate/context fingerprints, and exact realized window/count, so crash recovery does not sever the
join; the inventory itself remains derived and recomputed rather than loaded as controller state.
Shadow scoring runs at the loop's pre-finalization seam under its own five-second soft allowance and
is charged to the cumulative analysis clock. It resolves the source through its exact session key,
detector, revision, and side-artifact references—never by Q or recency. Deadline, missing evidence,
unsupported policy, and unexpected scorer failure all persist diagnostic unavailable evidence and
continue with the same legacy request. The score envelope and variant fingerprints travel only as
compact provenance; they do not enter the finalized request fingerprint. Historical inventory `/2`
remains a strict read contract and is never reminted as `/3`.

After finalization, shadow mode additionally persists
`isar.scan_design_realized_comparison/o36.7` under
`report/scan-design-realizations/<sha256>.json`. It records the exact rendered legacy request and an
isolated target-core counterfactual at the same finalized per-point `ncount`. If identities, ordered
adapter grids, and exposure still equal the Slice 6 pair, the sidecar references the existing score;
otherwise both realized candidates are evaluated and scored together under a separate cumulative
five-second per-proposal allowance. Failure publishes numerical unavailability but still carries the
exact selected legacy request and grid fingerprints. Initial and execution-time finalization refresh
stale evidence; minimum-target-support step reduction forces a refresh before revalidation.

Submission and card provenance carry the comparison reference/fingerprint, variant fingerprints, and
selected request/grid fingerprints. Completed TAVI data compare `result.scan_values_1` with that
ordered adapter-canonical grid: representation noise at the declared precision is normalized first,
then equality is exact. A changed value/order/length logs `executed_grid_mismatch` and cannot support
later authority, while ordinary shadow analysis continues. Missed-window recovery clears the realized
variant/request/grid claims, marks `eig_realization_invalidated_by_recovery`, and proceeds outside EIG.
If recovery cannot authenticate the retained sidecar, it logs `realized_comparison_unavailable` and
continues without recomputation or replacement evidence.

The allowance is cooperative, not preemptive: the adapter checks before each synchronous variant
score and again after it returns. If the second check crosses the deadline, that raw score set is
discarded before validation, the current and remaining variants record `shadow_scoring_deadline`,
and the in-flight legacy proposal continues normally.

The proposal-wide `authoritative_evidence_revision` remains transition-first and continues to name
the evidence transition used by proposal/request identity. Shadow scoring has a narrower exact-source
join: its in-memory action provenance names the selected completed master's `master_revision` (or
null), independently of the proposal-wide revision. This preserves off/shadow request parity while
refusing same-Q or later-master substitution. Historical `isar.objective/2` records that omit the
additive `scan_window_eig` block load as no-policy/default-scoring objectives; readers never consult
the current card to supplement such a full spec, envelope, or resume record.
Alongside the interval/no-progress brakes there is an identity-space one, `identity_saturated`, for
the case those cannot speak to — an identity-ambiguous q never matches the target branch, so it never
accumulates the pooled-master updates `interval_floored` reads. It requires both the target
probability and the identity entropy to stall across a real exposure increase; a partial identity
signal is unknown, not saturation, and unknown means keep measuring. The driver never pauses for an
operator, never blindly repeats a saturated q, and never reports ambiguity while it still holds
authorised scans.
Once the acoustic rival's identity entropy is below the stopping threshold, it remains labeled context
and receives no target-precision acquisition value.

This generalizes into a standing terminal-taxonomy invariant (DECISIONS O29, amended by O31): **the
campaign must not enter a terminal stop while measurement budget remains** unless one of six legitimate
terminals holds — `met`, `infeasible` (tolerance below the systematic floor), `incomplete` on genuine
budget/analysis-clock exhaustion, `operator_stopped`, a `controller_fault` surfaced for
repair-and-resume, or an exhaustion whose one remaining action the operator's **own declared policy
forbids**. Any *other* stop with beam time left — an `action_inventory_exhausted` or a
`branch_ambiguity`/`frontier_blocked` while budget remains — is a defect to hunt, not a result: either
the generator registry lacks an action for a situation that is in fact resolvable, or a resolvable state
was misclassified as terminal. A software fault must be classified `controller_fault` rather than
reported as a data-limited `incomplete`, and a point that only needs more or better data (an
inconsistent pool, an unconfirmed identity) must be re-acquired rather than have its confirmed
neighbours discarded.

The sixth terminal is the O28/O29 seam and is narrow on purpose. A search band the operator declared —
or one carrying no provenance at all — is a hard constraint, so an identity action that would have to
reach outside it is reported `policy_forbidden` rather than silently scanned past, and a durable
`OPERATOR` rejection lets the inventory prove exhaustion honestly. It is not a `controller_fault`: the
inventory is *complete* and the proof stands; the one action left is withheld by policy, not unknown.
Because the escape is neither more beam nor a repair, the stop reason itself names the forbidden action
and how to authorise it (declare `identity_search_meV`, or re-compile so the band carries compiler
provenance) — burying that in the persisted inventory would report a resolvable campaign as a
scientific dead end.

---

## 1. The problem

ISAR is a **triage tool** for inelastic neutron / X-ray spectroscopy: fit every scan, report a phonon
energy with an honest verdict, and flag what needs a human (`docs/READING_GUIDE.md`). The autonomous
driver (`isar drive`, `docs/campaigns/2026-06_MVP_FIRST_RUN.md`) closes the loop — from a goal it proposes scans, drives
the instrument, fits, and decides where to remeasure and when to stop.

The first autonomous campaigns exposed three failure zones, and thinking about them exposed a deeper risk.

**The concrete failures (MVP + TA slice):**
- **Low q** (H≈2.05, near the zone centre): the acoustic peak merges with the elastic line. Γ (damping)
  and A (intensity) become unidentifiable and the fit is *confidently wrong*.
- **High q** (H>2.4, low temperature): the signal is weak and the anti-Stokes side is Bose-suppressed.
  In the old scan-authoritative loop, the prior-agnostic excursion gate discarded recoverable 4–6 meV
  signal as `no_signal`, and after a few declines abandoned q. O18 removes that stopping behavior.
- **Branch confusion (TA campaign, 2026-07-17): branch identity is only a triage filter, never a
  steering signal.** The Bayesian assignment posterior exists (`branch_assignment.py`) but the
  acquisition knows nothing of it: energy windows are deliberately target-tight (`e_pred +
  1.5·FWHM`), treating the rival branch as contamination to *exclude* rather than information to
  *buy cheaply*; near Γ, where the acoustic is unresolvable, the strong ~6.5 meV optic features
  were absorbed as *acoustic* anchors and the branch model sawtoothed between the two branches;
  and a clearly visible optic mode at the zone centre carried **zero** acquisition value because
  it was not the target's ω₀ — the loop never spent a scan establishing what it was. The fix is
  the branch-identity-aware acquisition (§2 fourth consequence, §4.1 item 7, §4.5; implemented
  through the O15 joint tracker and connected frontier).

**The deeper risk (what must not happen):** the natural fix — build a phonon dispersion model and lean
on it — would lock ISAR into being **phonon-only and model-required**. A triage tool cannot assume it
knows what it is looking at. Three situations break a model-required tool, and ISAR has to handle all
three:

1. **No good model, told to measure a region.** Must survey and report signal *model-free* — "there is
   something at E = X, width W, intensity I" — without asserting what it is.
2. **Signal that does not match the model.** Must *demote* the model and flag the anomaly, never
   fit-a-phonon-anyway (`archive/PHASE_3_DESIGN.md` already warns: a sharp non-phonon feature is *flagged, not
   fitted as a phonon*).
3. **Features stuck together.** Must report the blend honestly and know how to *separate* them (change
   T, change Q, higher resolution) rather than force a fit.

A generality audit of the current code confirms the risk is real: every enumerated hypothesis contains a
`PhononDHO`; the initial-condition grid scans `Ω_d` only; all ω₀ reporting is gated on
`isinstance(PhononDHO)`; a structured residual escalates only to "another phonon"; and there is **no
model-free spectral description anywhere** — every "is there signal" test returns a bare boolean. The
*fitter* abstraction (`Component`/`CompositeModel` + NNLS, with `QuasiElastic` as proof a non-phonon
component slots in) and the honest-fork / runs-test / calibration layers are already general; the
phonon-lock is in the enumeration, the reporting, and the whole drive/objective/stopping stack, which
targets a single scalar ω₀(q).

---

## 2. The organizing principle

One principle subsumes the two trouble zones *and* the three challenges:

> **Choose the next measurement to maximise cost-normalised expected information gain (EIG) about the
> current quantity-of-interest — where the quantity-of-interest and the model set both evolve as
> evidence accumulates.**

This is Bayesian optimal experimental design (BOED). Its value here is that a *single* loop spans the
whole range from "I have no idea what's here" to "I need ω₀ to ±0.1 meV," because the quantity-of-interest
changes as belief sharpens. That gives the **confidence ladder**:

| Stage | Quantity of interest | Acquisition reduces uncertainty about… | Field exemplar |
|---|---|---|---|
| **A. Discovery** (vague prior) | *where / what is signal* — the intensity field + a model-free peak descriptor | field uncertainty (self-reduces to plain exploration) | ARIANE, gpCAM |
| **B. Inference** (rough → result) | the physics parameters ω₀, Γ | ω₀ (marginal EIG / Fisher-information design) | Durant, Nabika |
| **C. Discrimination** (rival models) | the *model index* (1 vs 2 peaks; phonon vs magnetic vs unknown) | where the models' predictions most disagree | Box–Hill, McCluskey |

Four consequences worth stating plainly:

- **The original phonon rescue is Stage B, made principled.** A cross-q "branch" prior over (ω₀, Γ, A) is
  the evolving prior; constraining Γ and A at low q is that prior's Bayesian update; a Stokes-only fit at
  high q is "the posterior is informative enough to fit under constraint." The acquisition then learns to
  *spend counts where ω₀ is least pinned down* — the principled form of "focus scans where they do the
  most good."
- **Exploration is not a separate mode.** From a broad prior the EIG landscape is flat (everything is
  uncertain) so the loop explores; as the posterior concentrates the landscape sharpens onto whatever is
  still in doubt. Uncertainty sampling (the ARIANE/gpCAM rule) is the Gaussian-limit special case of EIG
  (MacKay 1992). We get explore→exploit for free.
- **Disentanglement is an acquisition goal.** "These two features are stuck together" becomes "measure the
  T or Q where the competing hypotheses most disagree" — Stage C. A phonon grows with temperature (Bose
  n+1), magnetic scattering falls with Q (form factor) and has a different T-law, an elastic line is
  ~T-independent, a quasi-elastic line broadens with T; those are the levers the loop should *choose* to
  pull.
- **Branch identity is part of the quantity of interest** *(first implementation 2026-07-17; O6/O8/O11)*.
  Stage-C discrimination applies *within* a campaign, not only between fit models: the
  branch-assignment entropy is a QoI the acquisition must be allowed to buy down. Knowing that a
  rival feature *is* the acoustic branch is precisely what certifies the target as the optic — so
  a cheap scan that labels a rival can legitimately outrank another polish scan on an
  already-labeled branch, and once a rival is confidently labeled not-the-target the loop stops
  spending on it. This is your "we scan other branches only to establish that they are not the
  target," made mechanical (§4.1 item 7, §4.5).

---

## 3. Cheap (analytic) vs expensive (real), with simulation in the middle

A steering algorithm is only trustworthy if it has been exercised where the truth is known and the cost
is negligible, *before* it spends beam time. ISAR should run the **same acquisition objective** against
three tiers of "measurement" that differ only in cost and realism — this is both a development/validation
ladder and, later, an in-loop multi-fidelity strategy.

**Tier 1 — Analytic (cheap, effectively free).** The forward model itself: a DHO lineshape convolved with
an analytic resolution, evaluated in closed form (`Fitting_Functions.convolute_voigt_dho`), optionally
with analytic Poisson noise. A "measurement" is a draw from the model; evaluations are unlimited and
instant.
- *Role:* algorithm development, unit tests, and — crucially — **EIG-estimator ground truth**. Because
  the model is exact and differentiable, closed-form linear-Gaussian EIG and the Poisson Fisher
  information are available in microseconds, and a full (expensive, unbiased) nested-Monte-Carlo EIG can
  be run here as the reference the cheap estimators must match. This tier is also the *screen* tier of the
  live loop: the Poisson-Fisher score `(∂λ/∂θ)²/λ` is analytic in the model.

**Tier 2 — Simulation (the middle).** A realistic virtual instrument: the TAVI simulator, DFT/SpinW
dispersions (`isar/synth/generate.py`, `isar/synth/dispersion.py`), full resolution convolution, realistic
Poisson counts with a systematic floor, background, and planted spurions / model-mismatched features.
Moderate cost per scan, still far cheaper than beam.
- *Role:* end-to-end validation **against known ground truth** (the only tier where truth is known and no
  human is present to over-fit to), σ-calibration, the benchmarking cost-benefit metric (Teixeira Parente
  2022), and calibration of the misspecification guards (plant a non-phonon feature; confirm the loop
  demotes and flags rather than steering counts into it). The MVP campaign already lives here.

**Tier 3 — Real (expensive).** Actual neutron counts: ~1 minute per point, peaks that top out at 30–50
counts or fewer. Here the **count is the bottleneck and compute is nearly free per point**.
- *Role:* deployment. Strategy: spend compute *freely* on the honest, Poisson-exact, posterior-based EIG
  (a 10–30 s decision is <5% overhead on a 1-min count); use the analytic screen only to shortlist
  candidates; escalate estimator honesty (HESSE → bootstrap → nested sampling) exactly where the posterior
  is non-Gaussian; and lean hard on the misspecification guards, because at low counts the data cannot
  strongly contradict a wrong model.

  User i input: tier 3 we may want to choose the second-best point: scan immediately, recompute based on previous scan, wait for next point to arrive; this is better for very fast scanning.

**Why the middle matters.** Tier 1 is where the acquisition *math* is developed and given ground truth;
Tier 2 is where the *decisions* are validated against known truth and confidence is calibrated; Tier 3 is
where we deploy, having earned trust on the two cheaper tiers. This extends ISAR's existing two-tier
validation ethos (synthetic ground truth + real-data reasonableness, `READING_GUIDE.md`) to the steering
loop. The same EIG objective runs at every tier; only the estimator and the measurement cost change. The
formal multi-fidelity version — a short low-count survey as a cheap information source feeding an
expensive long count (Kandasamy 2017) — is a natural later extension, but the primary use of the tiers is
as a development-and-validation ladder.

---

## 4. Design options and the recommendation

### 4.1 Acquisition engine — how to compute "where does data most reduce uncertainty"

The field spans a spectrum by *how much physics lives in the acquisition loop*:

| Engine | What it optimises | Model? | Cost | Notes |
|---|---|---|---|---|
| GP posterior variance (uncertainty sampling) | field variance | no | cheap | ARIANE, gpCAM default; explores, never pins ω₀ |
| Info-theoretic GP acquisition | mutual info about the *field* | no | moderate | gpCAM `shannon_ig`; still field-level |
| Hybrid: physics-mean GP + residual | field, physics-guided | partial | moderate | Kennedy–O'Hagan discrepancy; mismatch-robust |
| **Fisher-information / D-optimal on physics params** | parameter covariance (ω₀, Γ) | yes | cheap–moderate | Durant (reflectometry); local, sequential |
| Full EIG (variational / amortised) | posterior over θ | yes | heavy | Foster/DAD; most principled; usually needs amortisation |

**Recommendation (driven by the real low-count regime):** peaks top out at 30–50 counts and a point
costs ~1 minute, so **per-decision compute is nearly free** — which *inverts* the usual tractability
logic. Generic BOED is dominated by decision latency (must choose in milliseconds → forces cheap
surrogates or amortised policies); ISAR is not, so it should not settle for the cheapest local
approximation. And it must not, because at 30–50 counts the Gaussian shortcuts break exactly where we
need them:

- the **log-GP** (ARIANE) assumes Poisson → Gaussian (large λ); its own paper proves that limit converges
  only as counts → ∞, so it is weakest at low counts — do not lean on it for *inference*;
- the **Laplace / Fisher-information** EIG assumes a near-Gaussian posterior at the MAP, but a weak peak's
  ω₀ posterior is skewed and sometimes bimodal ("is that bump real?") — the local log-det EIG is least
  trustworthy in exactly the trouble zones;
- **χ² / Neyman** likelihoods bias low at low counts; ISAR's **Baker–Cousins Poisson −2 ln L is correct**
  and should be extended *into* the acquisition.

So the chosen engine is:
1. **Objective = marginal (goal-oriented) EIG on ω₀** via the outcome-entropy form
   `EIG(d) = H(y|d) − E_θ[H(y|θ,d)]`, nuisances (background, Γ, A) integrated out so beam is not spent
   pinning nuisances (Foster's posterior bound; Attia's GOODE; Elfving c-optimality). The **Poisson
   likelihood is exact** and its per-outcome entropies are closed-form — but the *scan-level* EIG is
   **not** closed-form: for a multi-point scan the posterior-predictive `p(y|d)` is a mixture over the
   posterior samples, so EIG is a **sample-based estimate**, not an analytic quantity. (Only the
   linear-Gaussian special case, §6, is closed-form.)
2. **Estimate it from one explicitly declared parameter measure.** ISAR currently stores
   parametric-bootstrap, nested-posterior, and HESSE/Laplace particles. They are not interchangeable
   posterior samples. The first scan-window design contract will therefore use a homogeneous
   `akaike_weighted_parametric_bootstrap_predictive` shadow measure and validate it against realized
   complete refit-and-reselect updates. A later all-model Bayesian posterior mixture is the formal
   comparator. Hold the estimator fixed when ranking designs so its bias is common-mode.
3. **Cheap Poisson-Fisher score `(∂λ/∂θ_ω₀)²/λ` as a fast screen** to shortlist candidates before the
   heavy rank (light for simulation/proof-of-concept, heavy where it matters). The **acquisition unit is a
   whole constant-Q scan**: rank scan proposals by EIG-per-cost first; per-energy-bin dwell optimisation
   is a later refinement.
4. **Counting time is a design variable** *(Partial 2026-07-18 — DECISIONS O13)*. **Implemented
   subset:** newly compiled objectives treat quicklook/production `ncount` as floors. Pooled fits use
   summed per-point monitor exposure in the raw-count Poisson mean, and refinement requests scale from
   the authoritative pooled interval toward the requested tolerance, subject to deterministic
   ladder/growth/cap/budget brakes and an empirical interval-floor stop. The ledger preserves every
   physical submission and pooled update for exact replay. **Planned remainder:** rank
   **EIG-per-second** (Snoek EI/cost; Poisson information is linear in time, so per-second information
   at a point is `(∂λ/∂θ)²/λ`) and decide keep-counting-here vs move with a Fisher/sample-based OCBA or
   value-of-information rule (Chen 2000; Wald 1945).
5. **Escalate estimator honesty where the posterior is non-Gaussian** (weak signal): HESSE/Laplace when
   clean → bootstrap / nested sampling when not. ISAR already has all three; wire the acquisition to the
   same ladder.
6. **Average the design score over competing hypotheses using an explicitly named weight measure**
   so the loop does not optimise for one possibly-wrong model (see §4.2 and §6).
7. **Two-term scan value** *(Implemented 2026-07-17 — DECISIONS O6)*:
   The shipped master-authoritative path draws incremental Poisson counts from every retained
   plausible model in `isar.master_predictive/2` and computes a legacy sample-based information
   score about the joint target-ω/model/branch state. Its model-dependent mix of nested,
   bootstrap, and HESSE/Laplace particles is not a formal Bayesian posterior predictive and is
   ineligible for scan-window EIG or acquisition under STAT-1 semantics. It reports expected
   ω-half-width reduction and model/branch-identity entropy reduction separately; neither is
   converted into the other's units by a λ weight. Common random numbers make action/Q comparisons
   deterministic, with progressively larger simulation batches when the leading candidates are not
   separated. The older λ-weighted deterministic
   branch-confusion proxy remains only in the explicit classification/opening-move fallback where a
   master predictive artifact does not yet exist; it is not the acquisition score for a usable master.
   Here a “candidate” is a scientific action/Q alternative whose counts are already evaluated on its
   master's fitted grid. In `shadow` mode the controller may diagnostically score the exact
   `legacy_uniform/1` and `target_core/1` pair from one exact eligible source master, but still never
   selects among them: it executes the legacy control in every outcome. The restricted contract is
   specified in `EIG_STEERING_DESIGN.md`.

**Honest limit:** at single-digit counts the information is simply not there; no method conjures it.
Correct behaviour is to report low confidence honestly and let EIG decide *count more here / move*;
low utility remains diagnostic and acquisition continues until a global limit. That is the deliverable,
not a failure.

### 4.2 Falsification — deciding a model is wrong / the signal doesn't match

**Chosen: cheap heuristic gate first, Bayesian evidence when ambiguous.** The heuristic gate reuses what
exists — goodness-of-fit, the Wald–Wolfowitz runs test on residual structure, and a new check that the
*model-free descriptor's peak count* matches the *fitted model's mode count* (well-defined only once the
descriptor has a characterised null false-positive rate, §4.3). Only when that gate is
ambiguous do we compute **Bayesian evidence** (ISAR already has `dynesty` nested sampling) comparing the
physics model against a generic "just-a-peak" / nonparametric alternative. On failure the model is
**demoted to the model-free descriptor and the scan is flagged as unexpected signal**, never fit as a
phonon anyway. The mismatch-honest refinements from the literature — an explicit model/theory-error term
in the information budget (Melendez 2021) and a discrepancy GP (Kennedy–O'Hagan 2001) — are the principled
upgrades if the heuristic proves too blunt.

### 4.3 Model-free foundation (L0)

**Chosen: a per-scan descriptor now, a GP intensity-field survey map later.** The descriptor is a
Poisson-aware model-free peak description per scan — position(s), width, integrated intensity, significance
— built by upgrading the existing excursion / detection-power machinery from a boolean to a structured
output. It is the foundation for triage (Stage A), for falsifying models against ground truth (Stage C),
for seeding fits, and for counting features in a blend. Because downstream falsification counts its peaks,
the descriptor must be a **formal statistical contract, not a boolean in a lab coat**: a controlled
false-positive rate under Poisson background, with explicit handling of elastic-line exclusion,
masked/QC-failed points, and multiple testing across energy bins (FDR or family-wise per scan). The
ARIANE/gpCAM-style GP reconstruction of the whole S(Q,E) field is a bigger subsystem and is deferred to a
later "survey mode."

### 4.4 The fit-vs-decide contract (engineering invariants)

Constraining a fit with a prior and *deciding what the data actually says* are different jobs. This
is the target contract; STAT-1 exposes where the current implementation does not yet satisfy it, and
STAT-2 owns enforcement:

- **Separate the data cost from the posterior cost.** A soft prior penalty enters the *optimiser's*
  objective, while goodness-of-fit and AIC/BIC read the pure Poisson `data_cost`. Today that data cost
  is nevertheless evaluated at the constrained optimum, so it is not generally the comparable
  data-only MLE required by ordinary model comparison. STAT-1 labels such candidates
  selection-ineligible but leaves their scores and weights unchanged; STAT-2 must separate
  comparable selection fits from constrained delivery fits.
- **Intensity is provenance-bearing, not a bare number.** A fitted amplitude is not portable across dwell
  time, monitor/time normalisation, resolution, detector, instrument, or merge state. What crosses between
  scans (to seed detection power or a branch prior) is a **normalised integrated intensity with its
  normalisation provenance**, converted into the target scan's expected counts before use — otherwise the
  weak-signal rescue becomes confidently wrong in a new way.
- **Tag every fit with its provenance.** `unconstrained`, `soft_prior`, `branch_conditional` (a parameter
  hard-fixed from the branch model), `stokes_only`, and `descriptor_only` (model demoted to the L0
  descriptor) are not interchangeable with an ordinary `resolved` phonon fit in reports, priors, or
  acquisition. A constrained or demoted fit **never feeds the prior it was constrained by** — that
  self-reinforcement is exactly the confident-wrong feedback loop (§6).

### 4.5 Excitation hypotheses — physics vs tactics *(branch slice Implemented 2026-07-17;
`level`/`continuum` Partial; DECISIONS O7–O11)*

The user's physics expectations ("we think the target is an optic phonon with a ~10 ± 2 meV
zone-centre gap, roughly flat — but we may be wrong") must **weight** the Bayesian machinery —
seeding, classification, scan selection — and never **force** it. Two interfaces, deliberately
separate:

- **`ExcitationHypothesis` — what the excitation looks like.** Declared in human-owned
  `card.policy.toml` (`[hypotheses.<name>]` tables — a hypothesis *is* hand-edited human intent;
  code never writes it). Parameters: `dispersion_type` (`branch` / `level` / `continuum`), a
  **non-negative** zone-centre energy prior (a lower-truncated normal; the zero-mean acoustic
  case is half-normal), shape priors (slope-at-ZC, flatness, fold periodicity), and a `confidence` that
  scales prior precision. `acoustic`, `optic`, and `null` ship as **named presets-as-data**; the
  full TOML reference lives in `docs/HYPOTHESES.md`.
- **`SteeringPolicy` — what the planner does about it.** Opening moves, energy-search bands, λ.
  The hypothesis *suggests* defaults (optic → scan the zone centre first at positive E — scanning
  the Bragg peak tells you nothing; acoustic → start at the smallest q whose predicted ω is
  resolvable, trace toward Γ later); the planner owns the decisions and the objective's
  `steering` block can override them. Physics and tactics never live in one object.

**How the prior enters — priors on parameters, never pseudo-data.** A hypothesis becomes a prior
mean + precision on the branch-belief regression's *parameters* (intercept = gap, slope at ZC),
added directly into the weighted-least-squares normal equations. It is never injected as fake
anchor points: pseudo-data double-counts evidence, and worse, feeds the classifier features the
prior itself painted in — a circular loop in which the hypothesis starts forcing instead of
weighting (the §6 misspecification-under-adaptivity trap, self-inflicted). The existing acoustic
zone anchor migrates into this form. Corollaries: prior influence decays automatically as real
data precision accumulates; classification likelihoods and branch-occupancy counts are computed
from **real anchors only**; a prior-only belief (a declared hypothesis with no data yet) predicts
from its parameters with honest width, which is what lets a declared optic hypothesis claim a
6.5 meV zone-centre feature cold instead of the acoustic belief absorbing it.

**Three-tier extensibility — "character" is a point in parameter space, not a code branch.**
1. **Tier 1 — new character, no code.** Anything branch-shaped is a new preset of the shared
   parameters, declarable inline in the policy TOML (a gapped magnon is optic-shaped in
   dispersion terms with different intensity expectations).
2. **Tier 2 — new mechanics, registry plugin.** Code only when the *machinery* changes: a
   `continuum` is not a curve (density estimation, not regression); a crystal-field `level`
   disables the slope prior; a new classification feature (intensity-vs-Q, polarization) must be
   supplied by the fitter. Each `dispersion_type` is a registry entry with a small interface —
   prior contribution, likelihood features, steering defaults. The first slice implements
   `branch` only and declares the interface, so `level`/`continuum` are additions, never rebuilds.
3. **Tier 3 — the truly unknown, at runtime, no declaration.** The assignment posterior's `new`
   class plus the `null` hypothesis: a feature matching no declared hypothesis is banked as an
   agnostic branch with wide priors and *reported plainly* ("a third feature near 8 meV matches
   none of your hypotheses"). The human closes the loop after the beamtime by naming it and
   writing a Tier-1 preset. The system never needs to understand new physics mid-flight — it
   needs to notice it, not force it, and hand it to the human.

**The hypothesis may be wrong — mechanically, not rhetorically.** Three guards, none optional:
a mandatory **sanity-check opening move** that probes the declared hypothesis's most
discriminating feature first (for "optic, gap ≈ 10 meV": the zone centre at the claimed gap
± 2σ); a **deterministic search-band widening schedule** when the pooled evidence misses the expected
signal; and a **posterior-predictive
disagreement check** (prior-prediction vs data-only prediction) whose verdict is flagged in
machine-owned `card.state.json` and stated in the report — loudly, because silently fitting a
wrong model to the end of a beamtime is the §6 failure this section exists to prevent.

**Path is first-guess** *(DECISIONS O9)*: the compiled Q path remains the certification set —
"done" still means *the user's* q points converged on the target branch — but the driver may
insert or extend q without restriction when classification value warrants (a zone-centre
classification scan, a disambiguation point where two branches approach), each logged as
driver-inserted with a machine-readable purpose. Driver-inserted q never seat convergence.

**Reproducibility**: the resolved hypothesis (parameters + preset version) is content-hashed
into `card.state.json` at campaign start — templates are part of the determinism chain; "same
TOML, same steering" must survive preset evolution.

---

## 5. Prior art in scattering (and where the gap is)

The published work sorts by how much physics is in the loop. The headline: **almost all deployed
scattering autonomy is model-free** (map the intensity field); the model-based machinery that targets a
*physical parameter* is proven in reflectometry but **has never been transplanted to inelastic TAS/IXS
dispersion + lineshape estimation**. That gap is ISAR's niche.

- **ARIANE** — Teixeira Parente et al. 2023 (*Nat. Commun.* 14:2246). A **log-Gaussian process** on
  intensity over a (Q,E) slice; acquisition = **maximum posterior variance**. The log link enforces
  positivity, compresses the signal/background dynamic range, and stabilises Poisson noise — and, because
  posterior variance in log-space is largest where intensity is high, biases sampling toward signal. It is
  explicitly **model-free**: it finds *where* signal is, not ω₀/Γ, and states that a physics-in-the-loop
  fit is future work. This is the "ARIANE" in `examples/` (code) with the benchmarking companion paper.
- **Benchmarking companion** — Teixeira Parente et al. 2022 (*Front. Mater.* 8:772014, in `examples/`).
  A cost-benefit framework: **cost = counting time + axes-travel time**; **benefit = signal-weighted L²
  error** between true and reconstructed intensity (threshold-capped so bright Bragg peaks don't dominate).
  Its phenomenology taxonomy — *non-dispersive* (crystal field), *dispersive* (spin waves, acoustic
  phonons), *(pseudo-)continua* (spinons) — is exactly the anti-phonon-lock map. Its stated "neglected
  aspects" are our openings: background/noise, **variable counting time**, environment (T, field) as extra
  axes, and an **autonomous stopping criterion** (which ISAR already has).
- **gpCAM / Noack et al.** — 2019 (*Sci. Rep.* 9:11809), 2020 (*Sci. Rep.* 10:17663 & 10:1325), 2021
  review (*Nat. Rev. Phys.* 3:685). GP surrogate with a **pluggable acquisition function** including a
  genuinely information-theoretic (`shannon_ig`) option and an adaptive cost function; supports custom
  kernels / physics mean functions. Deployed for X-ray (SAXS/GISAXS) and SANS mapping; O(N³) GP cost is
  the core constraint.
- **Durant et al.** — 2021 (*J. Appl. Cryst.* 54:1100) / 2022 (*J. Appl. Cryst.* 55). The closest
  model-based template: a physical reflectometry model, **Fisher-information** design (D-/A-optimality) to
  choose angle, counting time, and contrast, sub-second and real-time, with an "enough data collected"
  signal. Swap the layer model for dispersion + DHO + resolution and the machinery carries over.
- **McCluskey / Nelson** — refnx 2019 (*J. Appl. Cryst.* 52:193); McCluskey et al. 2020
  (*Mach. Learn.: Sci. Technol.* 1:035002). **Bayesian model selection via nested-sampling evidence** for
  reflectometry — the principled "which model?" tool, directly relevant to the falsification layer.
- **Ratcliff (NIST) 2026** — arXiv:2604.23821. **The closest prior art**: a hybrid, staged autonomous loop
  on TAS **spin waves** (magnetic, not phonon) — (1) agnostic log-GP discovery, (2) physics-informed
  inference with model discrimination, (3) strategic falsification audit; "signal structure triggers the
  transition from broad exploration to model-based planning." Acquisition = `InfoGain^γ / (CountTime +
  MoveTime)`. This validates the confidence-ladder architecture and de-risks it — but means it is not
  unoccupied. ISAR's differentiators: honest-triage / legible falsification, a **true Poisson** fitting
  core (ahead of the log-GP approximation), and multi-instrument (TAS + IXS).
- **Physics-model spectroscopy (adjacent):** Nabika et al. 2023 (arXiv:2305.07040 — parametric model +
  Bayesian model selection in the loop, **beats GP** active learning); Chen et al. 2023 (XPFS BOED,
  *MLST* 4:045056); Zhang et al. 2023 (XAS Hamiltonian by adversarial BO, *npj Comput. Mater.* 9:46);
  Ueno et al. 2018 (XMCD adaptive design, *npj Comput. Mater.* 4:4); Ito et al. 2025 (XAS Bayesian design,
  *MLST*); Melendez et al. 2021 (proton Compton BOED with a **model-error term**, *EPJ A* 57:81); Loredo
  2004 (Bayesian adaptive exploration, astro).

**The gaps = ISAR's opportunities:** (1) no model-based EIG for inelastic TAS/IXS dispersion; (2) no true
Poisson (not log-GP) likelihood in a scattering steering loop; (3) no principled dwell/stopping rule wired
into a where-to-measure loop; (4) no D-optimal/EIG design that explicitly targets *peak position /
dispersion* as the named quantity of interest.

---

## 6. Bayesian experimental design — the research, by thread

Grounding for the acquisition math. (Citation-confidence flags in §9.)

**Foundations.** EIG is the expected reduction in Shannon entropy of the parameters, identical to the
prior→posterior KL and to the mutual information I(θ;y|d) (Lindley 1956; Chaloner & Verdinelli 1995;
Ryan et al. 2016 review; Rainforth et al. 2023 modern review). Four algebraically identical forms exist;
for Poisson counts the **outcome-entropy form** `H(y|d) − E_θ[H(y|θ,d)]` is the one to use because it needs
only the closed-form likelihood, not the intractable posterior. Chaloner–Verdinelli's utility framework
recovers the classical "alphabet": Shannon/KL → **Bayesian D-optimality** (max E[log det Fisher]);
quadratic loss → **A-optimality**; a linear functional cᵀθ → **c-optimality**.

**Sequential / greedy.** The adaptive loop: last step's posterior is this step's prior; choose each design
to maximise immediate EIG (myopic) — near-optimal when each measurement is individually informative and
there is no hard budget/path coupling. Non-myopic (lookahead / RL / POMDP) matters under tight budgets or
strong explore-then-exploit structure (Ryan 2016; Foster 2021 DAD; Blau 2022 RL; Shen & Huan 2021).
**Build myopic first.**

**Estimation tractability (the crux).** EIG is doubly intractable. Nested Monte Carlo is the biased
baseline (downward bias O(1/M); MSE floor worse than plain MC; optimal M ∝ √N — Ryan 2003; Rainforth
2018) — reference/offline only. Fast options: **variational bounds** (Barber–Agakov 2003; Foster 2019
posterior/marginal/VNMC family; Foster 2020 ACE/PCE contrastive bounds; Foster 2021 sPCE), **amortised
policies** (DAD 2021, iDAD 2021 — millisecond decisions, but heavy offline training + a differentiable
simulator), **Laplace** (Long 2013 — Gaussian posterior collapses the double loop; not a bound; degrades
on multimodal/low-SNR), and **closed-form linear-Gaussian** (EIG = ½ log det(I + Σ₀ GᵀΣε⁻¹G) ≡ D-optimal,
microseconds). *For ISAR at a minute/point the binding constraint is honesty, not latency, so the viable
tiers are a heavy posterior-sample EIG per candidate plus a cheap Fisher/Laplace screen — with offline
nested-MC as ground truth. Track bound directions; never compare designs across different estimators.*

**Goal-oriented / marginal EIG.** Target ω₀, marginalise nuisances: EIG_ψ = I(ψ;y|d) with η integrated
out (Foster 2019 posterior bound; Attia 2018 GOODE; Elfving 1952 c-optimality). Score by the expected
sharpening of the **1-D marginal posterior of ω₀**, not full-parameter D-optimality — else beam is wasted
refining nuisances.

**Model discrimination.** Choose designs that best distinguish rival models: maximise I(m;y|d), the mutual
information about the model index (Box–Hill 1967 Gaussian criterion; Cavagnaro 2010; Drovandi 2014 SMC
over models). **Nested-model subtlety** (critical for 1-vs-2-peak: 1 peak = 2 peaks with A₂ = 0): pure
discrimination degenerates on the boundary, so use the **total-entropy** criterion (model-EIG + parameter-
EIG on A₂; Borth 1975) and the Savage–Dickey ratio (Dickey 1971) for the nested Bayes factor.

**Poisson & cost.** For y ~ Poisson(μ = λt), Fisher info `I = t·(∂λ/∂θ)²/λ` — **time enters linearly**, so
per-second information at a point is `(∂λ/∂θ)²/λ` (Wang 2006 Poisson OED). Cost-normalise: acquisition
÷ cost (Snoek 2012 EI/cost; Lee 2020 CArBO cost-cooling; Lee 2021 nonmyopic). Stop by value-of-information
/ best-arm allocation (Wald 1945 SPRT; Chen 2000 OCBA; Audibert 2010, Gabillon 2012 fixed-budget best-arm).

**Exploration is free.** Under a Gaussian/linear model, EIG = ½ log(1 + β·gᵀA⁻¹g) where gᵀA⁻¹g *is* the
predictive variance — so **max-EIG reduces to uncertainty sampling** from a broad prior and self-sharpens
as the posterior concentrates (MacKay 1992; Seo 2000). For *batch* selection, use mutual information (not
raw variance) to avoid boundary-piling, inheriting the submodular (1−1/e) greedy guarantee (Krause &
Guestrin 2008; Nemhauser 1978).

**The pitfall that will actually bite: model misspecification amplified by adaptivity.** A wrong model
steers counts to where the *wrong* model says signal is; adaptivity turns this into a positive-feedback
bias (Sloman 2022) — precisely the MVP's "confident-wrong" low-q zone. Defences that earn their keep:
(1) carry competing models and **model-average / robustify** the design objective (Box–Hill 1967;
DuMouchel & Jones 1994; Go & Isaac 2022; Overstall & McGree 2022); (2) **keep assumed noise generous** so
the loop stays exploratory and representative (Sloman 2022); (3) **hold the EIG estimator fixed and
bias-characterised** when ranking designs. This is the acquisition-side twin of the §4.2 falsification
layer.

---

## 7. How this maps onto ISAR

The confidence ladder maps directly onto ISAR's layers, mostly by *extending* what exists:

- **L0 — model-free descriptor** (new): per-scan Poisson-aware peak description; upgrades the excursion /
  `detection_power` bool. Foundation for Stages A/C.
- **Physics-model layer**: the phonon `PhononDHO` becomes *one hypothesis among several*
  {generic-peak, elastic+QE, 1-mode, 2-mode}, not the mandatory core; the cross-q **branch prior**
  (ω₀,Γ,A, with the acoustic 1/ω intensity) is the evolving Stage-B prior; soft-then-hard constraints are
  its Bayesian update. Requires generalising the `Ω_d`-only IC grid, the phonon-suffix bounds, and the
  `isinstance(PhononDHO)` reporting. The branch prior is keyed on objective-path **arclength + an explicit
  branch id** (reusing `path_scalars`), not the current dominant-varying-Q heuristic, and never spans a
  branch crossing. *(Implemented branch slice, §4.5: per-branch beliefs carry the declared
  hypothesis's parameter prior — priors on the regression's intercept/slope, never pseudo-anchors;
  `level`/`continuum` belief plugins remain O10-Partial.)*
- **L3 — falsification**: heuristic gate → evidence when ambiguous → demote + flag; model-averaged.
- **Acquisition**: sampled posterior-predictive information about joint target-ω/model/branch state,
  ranked per predicted analysis second or measurement cost at the **scan-proposal** level. Expected
  ω-half-width and identity-entropy reductions are reported separately. Character-aware opening moves,
  rival-covering windows, and purpose-tagged driver-inserted classification q remain deterministic
  fallbacks and never seat convergence.
- **Engineering invariants (§4.4):** the data-cost / posterior-cost split, provenance-bearing normalised
  intensity, and the `fit_kind` tag are load-bearing — and their serializable schemas (descriptor,
  branch-prior point, `ScanContext` constraints, acquisition observation, report fields) are defined
  **up front**, before the rescue phases accrete ad-hoc fields.
- **Honesty (unchanged ethos):** keep the Baker–Cousins Poisson core; estimator ladder
  HESSE → bootstrap → nested by non-Gaussianity; legible reports that state confidence and next action.

**Near-term work.** The immediate, shippable fix for the MVP's low-q/high-q zones is a phased arc that
lands the interfaces + provenance plumbing (store Γ and a normalised, provenance-tagged intensity on prior
points), the L0 descriptor, the branch prior + soft constraint
(low-q rescue), the hard-fix + Stokes-only path (high-q rescue), the falsification layer, and finally the
BOED acquisition — phonon-verified before any non-phonon plugin. That tactical plan is tracked separately;
this document is the core it is a first slice of.

**Deliberately deferred:** the GP intensity-field survey map (full L0); non-phonon physics plugins (magnon
dispersion, crystal-field flat-in-Q, quasi-elastic-as-signal) behind the same seam; amortised / non-myopic
policies (unnecessary while decisions are cheap relative to a 1-min count); full nested-MC EIG (kept
offline as ground truth).

### 7.1 Operator control of a running campaign (implemented, GUI Pass 3 / 2026-07-16)

The loop gained a small, additive operator-control contract (GUI_DESIGN.md G19 has the ruling):

- **Cooperative stop.** `isar drive --control-token <t>` (token alphabet `[A-Za-z0-9_-]`, max
  64 — it names the control file) makes the loop check `report/control/stop-<t>.json` at every
  iteration boundary: the top of each iteration **and once more after the final allowed
  iteration** (so a stop written during the last `--max-iters` scan is still honored — with
  `--max-iters 1` that post-exhaustion boundary is the only one that can see it). Present → one
  acknowledgement `decide` event (`action="operator_stop"`), the in-flight iteration having
  already completed (wait → persist → analyze), then a clean exit with the first-class terminal
  state **`operator_stopped`** (CLI exit **4**; met 0 / incomplete 3 / infeasible 6 / error 2):
  certification is skipped, the final grid is still evaluated honestly and recorded as
  `stop["grid_state"]`, exactly one terminal `stop` event stays last in the log, and the control
  file is retained (nothing is ever deleted; other tokens' files are inert). No token → no
  checks, byte-identical behavior. A campaign that ends on its own (met/incomplete/infeasible) at a
  boundary where no stop was yet visible reports its true state — the stop check always runs
  before, never after, that decision.
- **`--max-iters` is a per-invocation cap.** `run_campaign` restarts iteration numbering at zero
  each invocation; a resume gets a fresh cap. Spend does not reset — the ledger is whole-log.
- **LLM-free resume.** `--resume-latest` (mutually exclusive with `--goal`/`--spec`) rebuilds
  the objective from the last `campaign_start` event's frozen `objective` dict — a goal is never
  re-compiled through the LLM on resume. `--budget` may still override explicitly; **`--tier` is
  frozen with the objective** (an explicit different `--tier` gets a stderr note and is ignored).
- **Start fingerprints.** `campaign_start` payloads carry `source: {kind: goal|spec|resume,
  sha256}` (goal text / spec file bytes / inherited), so a consumer can tell "same objective
  resumed" from "new objective over the same workspace spend ledger".

A hard kill remains possible (the GUI escalation path tree-kills the process group) and safe by
the crash-resume contract, at the documented cost: a scan whose `run` event was logged before
the kill is spend-counted but unfetched, and the next invocation re-proposes that q.

### 7.2 Frozen objective preview and first optic-target slice (implemented, 2026-07-17)

Natural-language campaigns now compile generic reciprocal-space line segments (`q_start`, `q_end`,
`step_rlu`) into an explicit path before measurement. `step_rlu` is a maximum interval: segment
expansion uses `ceil(distance / step_rlu)` and includes both endpoints. The legacy `along_h` wrapper
retains its round-based grid exactly, including its non-commensurate-span behavior. Natural-language
degenerate segments are rejected; explicit and resumed one-point objectives remain valid.

`isar drive --compile-only <envelope.json>` freezes the objective, original goal, and source
fingerprint without contacting TAVI. The GUI presents that envelope in a copyable confirmation dialog
(path, point count, target branch, energy range, tolerance, and budget), then executes the same envelope;
confirmed fields cannot be overridden on that handoff. Resume continues to read the frozen
`campaign_start.objective` and never recompiles through the LLM.

The objective carries additive `target_branch`, `target_label`, and `energy_search_meV` fields. Older
objectives default to acoustic and `[-2, 7]` meV. An optic objective defaults to `[-2, 12]` meV; that
range drives the cold survey, while the acoustic-only near-Γ and Fisher advisory paths stay disabled for
the optic slice. Branch assignment, prior seeding, pooled delivery, and stopping certify only the
requested branch. Clean pooled folds may seed that branch; ambiguous forks still may not. Terminal
reporting uses master diagnostics such as model ambiguity, pool inconsistency, analysis failure,
resolution-limited linewidth, and interval above tolerance. The active target is copied
only into machine-owned `card.state.json`; `card.policy.toml` remains human-owned.

### 7.3 Instrument-limit-aware batching (implemented, 2026-07-18)

At campaign start the driver snapshots TAVI's advertised `max_points`,
`max_neutrons_per_point`, `queue_neutron_budget`, and `max_queued` values from `/state`, then fills
missing fields from `/schema`. Each field retains its source; only `max_points` has a legacy-safe
fallback (200) when discovery is unavailable. The frozen objective remains human intent and keeps its
fingerprint unchanged.

Proposal generation and the final pre-validation checkpoint apply the smaller policy/instrument
ceiling. The queue neutron budget is converted to a per-point ceiling using the emitted point count;
it constrains one serial submission and is not charged as campaign spend. A larger statistical request
therefore becomes repeated legal batches whose evidence is pooled. Clamps are recorded as
`proposal_ncount_clamped_to_limit`, while a live ceiling below the applicable quicklook/production
floor terminates explicitly as `instrument_limit_below_minimum_batch`. Campaign starts and summaries
carry the discovered/effective envelope. Logical acquisition IDs use a separately resumed proposal
counter, so validation refusals are distinct acquisitions while physical recenter retries still share
one ID.

### 7.4 Live observability and resumable certification (implemented, 2026-07-19)

The append-only session log now carries durable activity boundaries for scanning, fast/full fitting,
pooled updates, and final certification. Progress records are advisory and non-fsynced; starts and ends
are durable. Resume emits an `interrupted` end for every unmatched start before new work begins. The GUI
joins the active activity to the already-persisted `model_update.per_q` snapshot, displays fixed
activity-specific units (`fits` or `points`) without an ETA, and renders the human feed newest-first
without changing chronological JSONL storage. Spend and resume readers ignore all activity kinds.

Final certification passes a Qt-free `isar.certification.request/1` through the runner seam. The
session side resolves it against the workspace into an immutable `isar.certification/2` manifest,
so the decision-only drive process never imports fit/session internals. The manifest contains exactly
one unit per requested pooled master in objective-path order—never one unit per physical scan. Its
fingerprint closes over the pooled spectrum and ordered membership, policy/model
space, resolved calibration/resolution, frozen prior/branch/target state, fit tier/settings, effective
seeds, and algorithm contract. Parsing, QC, and collation reconstruct and validate the frozen pools;
physical fitting and physical-ledger mutation are skipped.

An exact ordinary master or valid checkpoint is reusable (`bayes` satisfies `full`; `full` cannot
satisfy `bayes`), including a deterministic scientific non-result. Corrupt, missing, or exception-
aborted units rerun. Missing masters dispatch in worker-sized waves, checkpoint individually in
completion order, and reduce by immutable canonical index. `progress.json` records durable completed,
reuse, fitted, active, phase, and publication state; advisory activity events mirror it for the live
GUI. Canonical `session.json`, `card.state.json`, and `results.csv` are staged only after every unit
exists, then atomically replaced and covered by a hash-bearing current-publication marker written
last. A restart therefore changes neither fit inputs nor the published generation. Changed inputs
create a new revision. Checkpoints remain under
`report/certification/<fingerprint>/`; diagnostics report their size. They are retained by default and
may be manually pruned only when no published result refers to the revision; ISAR never deletes them.
Ordinary deadline-bounded analysis uses the same pure-unit contract under
`report/analysis/<fingerprint>/`: dispatch is limited to one worker-sized wave, completed units are
checkpointed, and no scan/master generation publishes until its canonical worklist is complete.

### 7.5 Master-authoritative steering and dual clocks (implemented, 2026-07-19)

The scientific reducer now has one authority per requested `(q,T)`: the latest published
combine-before-divide master revision. Physical rows update acquisition counts, exposure, raw-QC and
cost provenance only. They cannot provide a fallback ω, interval, branch assignment, convergence vote,
decline counter, or prior point. The target tracker likewise sees only consistent pooled folds; an
absent master remains an explicit gap.

Each merged pool carries a seeded exposure-scaled Poisson-deviance homogeneity result. Failure at the
default α=0.01 blocks certification and requests review without excluding a member or ending the
q-point. A usable master also publishes an `isar.master_predictive/2` artifact containing bounded full
parameter draws and weights for every retained plausible model/branch, together with the fitted grid,
the frozen evaluation context and the per-energy exposure basis those draws were fitted against. That
artifact is *physical only*: which mode is the campaign's target is resolved after branch triage and
the connected tracker into a separate `isar.target_projection/2` bound to the authority basis that
produced it (ADR-019; [`EIG_STEERING_DESIGN.md`](EIG_STEERING_DESIGN.md)).
Nested draws win, then full-fit
bootstrap draws, then a trusted multivariate HESSE/Laplace approximation. The model-weighted ω interval
is conditional on target-present retained models and can therefore mix three different parameter
measures. It remains the legacy certification and stopping interval; therefore Γ may be
`resolution_limited` without blocking an otherwise identified ω. It is not a Bayesian
model-averaged credible interval, does not carry no-target mass, and is ineligible for EIG and
acquisition. Missing simulation-grade draws trigger a single full-tier escalation for that revision.

STAT-1 records those facts additively in `isar.predictive_semantics/1`, including exact
offered/scored/weight-denominator/retained/projection set fingerprints, per-model estimator and
constraint eligibility, particle and draw measures, Bayesian prior dimensions, and named authority
scopes. `construction_complete` and the legacy `predictive_complete_for_eig` describe artifact
construction only; every `/2` artifact has `homogeneous_design_eligible=false` because its contract
permits model-dependent fallback. `target_projection_complete` is an explicit alias for projection
construction completeness, not target-presence support.

STAT-1 deliberately neither bumps `PREDICTIVE_CONTRACT` nor regenerates cached masters. STAT-2 must
introduce or bump the selection contract used by both the master-fit cache fingerprint and the
unchanged-master carry gate. STAT-3 then refuses pre-STAT-2 or semantics-incomplete inputs when it
builds the homogeneous shadow-design artifact. Carrying a pre-STAT-1 cached artifact can therefore
never make a workspace eligible for EIG-3.

Acquisition estimates information about the joint target-ω/model/branch latent state from incremental
Poisson counts. Deterministic mode ranks per predicted analysis second; measurement-dominated tiers use
the active measurement cost. ω-width and identity-entropy reductions remain separately reported.
Uncertain or low utility is diagnostic, not a reason to abandon q. `isar.master_predictive/2` carries
the exposure basis and evaluation context, so `isar.predictive.evaluate_master_predictive` re-evaluates
its draws on an arbitrary requested energy grid at an arbitrary uniform per-point exposure — the
capability the superseded `/1` artifact could not support, because it stored counts on the fitted
master grid alone. `/1` emission was removed rather than kept alongside `/2` (DECISIONS O33): every
reader requires `/2`, and a leftover `/1` artifact is refused as a non-result with
`artifact_schema_unsupported`, never guessed into `/2`. How that evaluator is consumed by scan-window
scoring is specified in [`EIG_STEERING_DESIGN.md`](EIG_STEERING_DESIGN.md).

The outer limits are independent. `budget` is optional measurement spend. `analysis_limit_seconds`
charges monotonic wall time around serialized fitting, pooling, consistency, predictive scoring and
certification operations, with one enclosing charge for parallel workers. Deterministic campaigns
default to 3600 analysis seconds and no measurement budget; measurement-dominated campaigns default to
eight measurement hours and no analysis limit. Durable checkpoints make both ceilings cumulative over
resume. Limits stop dispatching new work; acquired data finish or checkpoint their active wave.

---

## 8. Open questions and risks

- **Misspecification under adaptivity** is the dominant danger (§6). The guards (model-averaged EIG,
  generous priors, falsification gate, descriptor as ground truth) are designed but unproven at low counts.
- **Low-count information limits** — honest reporting, not heroics; the loop's job is to say "more counts
  / move / abandon."
- **Novelty vs precedent** — no model-based EIG for inelastic TAS exists; Ratcliff 2026 and Nabika 2023 are
  closest. Validate on phonons against known truth (Tier 1/2) before trusting it on beam.
- **Nested-model degeneracy** (1 vs 2 peaks) needs the total-entropy criterion, not pure discrimination.
- **1/E acoustic intensity** is acoustic-only and ignores structure-factor detail — keep A soft with loose
  σ; never let anchors cross a branch or a mode-crossing.
- **The fit-vs-decide contract (§4.4) is a correctness invariant, not just style** — the prior must not
  leak into model selection or goodness-of-fit, the crossed-scan intensity must be normalisation-portable,
  and constrained/demoted fits must not feed their own prior. These need explicit regression tests, not
  only review.
- **Prior-forcing circularity (§4.5)** — a hypothesis prior expressed as pseudo-data feeds the
  classifier features the prior itself painted in, and the hypothesis starts forcing instead of
  weighting. The parameter-prior form plus data-only classification features is the structural
  defence; the regression tests must include the observed failure (a rival's zone-centre feature
  absorbed into the acoustic belief).
- **Hypothesis mismatch under adaptivity** — a wrong declared hypothesis steers the opening moves
  before any posterior exists to correct it. The sanity-check opening move, deterministic band
  widening, and the posterior-predictive disagreement flag (§4.5) are designed but unproven;
  Tier-1/Tier-2 campaigns with deliberately wrong hypotheses are the validation.
- **Rival-spend runaway** — with no hard caps, the λ term must genuinely decay: λ falls with label
  confidence, the per-q classification term zeroes at the differentiation tolerance, and the
  budget stop is the outer bound. The synthetic two-branch campaign must show rival spending
  peaking early and dying off, not tracking the target to the end.

---

## 9. References

Grouped by topic. Confidence: items are **verified** unless flagged **[v]** (bibliographic detail
reconstructed from memory / secondary indexing — confirm before formal citation) or **[paywall]** (only
metadata seen). No references were fabricated.

**Scattering autonomy (prior art)**
- Teixeira Parente, Brandl, Franz, Stuhr, Ganeva, Schneidewind (2023). *Active learning-assisted neutron
  spectroscopy with log-Gaussian processes.* Nat. Commun. 14:2246. doi:10.1038/s41467-023-37418-8 ·
  arXiv:2209.00980. **(ARIANE)**
- Teixeira Parente, Brandl, Franz, Schneidewind, Ganeva (2021). *Autonomous Experiments for Neutron TAS
  with Log-Gaussian Processes.* arXiv:2105.07716.
- Teixeira Parente, Schneidewind, Brandl, Franz, Noack, Boehm, Ganeva (2022). *Benchmarking Autonomous
  Scattering Experiments Illustrated on TAS.* Front. Mater. 8:772014. doi:10.3389/fmats.2021.772014.
  **(in `examples/`)**
- Ratcliff (2026). *Accelerating Quantum Materials Characterization: Hybrid Active Learning for Autonomous
  Spin Wave Spectroscopy.* arXiv:2604.23821.
- Noack, Yager, Fukuto, Doerk, Li, Sethian (2019). *A Kriging-Based Approach to Autonomous Experimentation
  with Applications to X-Ray Scattering.* Sci. Rep. 9:11809.
- Noack, Doerk, Li, Streit, Vaia, Yager, Fukuto (2020). *Autonomous materials discovery driven by GP
  regression with inhomogeneous measurement noise and anisotropic kernels.* Sci. Rep. 10:17663.
- Noack et al. (2020). *Advances in Kriging-Based Autonomous X-Ray Scattering Experiments.* Sci. Rep.
  10:1325. doi:10.1038/s41598-020-57887-x.
- Noack, Zwart, Ushizima, et al. (2021). *Gaussian processes for autonomous data acquisition at
  large-scale synchrotron and neutron facilities.* Nat. Rev. Phys. 3:685–697. doi:10.1038/s42254-021-00345-y.
- Durant, Wilkins, Butler, Cooper (2021). *Determining the maximum information gain and optimising
  experimental design in neutron reflectometry using the Fisher information.* J. Appl. Cryst. 54:1100–1110.
  doi:10.1107/S160057672100563X · arXiv:2103.08973.
- Durant, Wilkins, Cooper (2022). *Optimising experimental design in neutron reflectometry.* J. Appl.
  Cryst. 55 · arXiv:2108.05605.
- Nelson, Prescott (2019). *refnx: neutron and X-ray reflectometry analysis in Python.* J. Appl. Cryst.
  52:193–200.
- McCluskey, Cooper, Arnold, Snow (2020). *A general approach to maximise information density in neutron
  reflectometry analysis.* Mach. Learn.: Sci. Technol. 1:035002. **[v]**
- *Optimization of counting time using count statistics on a diffraction beamline.* Nucl. Instrum. Methods
  A (2016). **[paywall]**
- Tung, Ding, Shinohara, Huang, Carrillo, Chen, Do (2025). *Universal Convergence Metric for Time-Resolved
  Neutron Scattering.* arXiv:2505.13512.
- Morris, Rakitin, et al. (2024). *A general Bayesian algorithm for the autonomous alignment of beamlines.*
  J. Synchrotron Rad. · arXiv:2402.16716.

**Physics-model spectroscopy / BOED applications**
- Nabika, Nagata, Katakami, Mizumaki, Okada (2023). *Sequential Experimental Design for Spectral
  Measurement: Active Learning Using a Parametric Model.* arXiv:2305.07040.
- Chen, … Chitturi, et al. (2023). *Bayesian experimental design and parameter estimation for ultrafast
  spin dynamics (XPFS).* Mach. Learn.: Sci. Technol. 4:045056. doi:10.1088/2632-2153/ad113a · arXiv:2306.02015.
- Zhang, Xie, Long, Günzing, Wende, Ollefs, Zhang (2023). *Autonomous atomic Hamiltonian construction and
  active sampling of XAS by adversarial Bayesian optimization.* npj Comput. Mater. 9:46.
  doi:10.1038/s41524-023-00994-w · arXiv:2203.07892.
- Ueno, Hino, Hashimoto, Takeichi, Sawada, Ono (2018). *Adaptive design of an XMCD experiment with GP
  modelling.* npj Comput. Mater. 4:4. doi:10.1038/s41524-017-0057-4.
- Ito, Takeichi, Hino, Ono (2025). *Optimal spectroscopic measurement design: Bayesian framework for
  rational data acquisition.* Mach. Learn.: Sci. Technol. doi:10.1088/2632-2153/add0f6. **[v, paywall]**
- Melendez, Furnstahl, Grießhammer, McGovern, Phillips, Pratola (2021). *Designing Optimal Experiments: An
  Application to Proton Compton Scattering.* Eur. Phys. J. A 57:81. doi:10.1140/epja/s10050-021-00382-2 ·
  arXiv:2004.11307.
- Loredo (2004). *Bayesian Adaptive Exploration.* arXiv:astro-ph/0409386. **[v]**
- *Physics-Informed Gaussian Process Inference of Liquid Structure from Scattering Data* (2025). J. Phys.
  Chem. B. doi:10.1021/acs.jpcb.5c05024 · arXiv:2507.07948.

**BOED foundations & reviews**
- Lindley (1956). *On a Measure of the Information Provided by an Experiment.* Ann. Math. Stat.
  27(4):986–1005. doi:10.1214/aoms/1177728069.
- Chaloner, Verdinelli (1995). *Bayesian Experimental Design: A Review.* Statist. Sci. 10(3):273–304.
  doi:10.1214/ss/1177009939.
- Ryan, Drovandi, McGree, Pettitt (2016). *A Review of Modern Computational Algorithms for Bayesian
  Optimal Design.* Int. Stat. Rev. 84(1):128–154. doi:10.1111/insr.12107.
- Rainforth, Foster, Ivanova, Bickford Smith (2023). *Modern Bayesian Experimental Design.* Statist. Sci.
  39(1):100–114. doi:10.1214/23-STS915 · arXiv:2302.14545.

**EIG estimation**
- Ryan (2003). *Estimating Expected Information Gains … Random Fatigue-Limit Model.* JCGS 12(3):585–603.
- Rainforth, Cornish, Yang, Warrington, Wood (2018). *On Nesting Monte Carlo Estimators.* ICML ·
  arXiv:1709.06181.
- Barber, Agakov (2003). *The IM Algorithm: A Variational Approach to Information Maximization.* NeurIPS.
- Foster, Jankowiak, Bingham, Horsfall, Teh, Rainforth, Goodman (2019). *Variational Bayesian Optimal
  Experimental Design.* NeurIPS · arXiv:1903.05480.
- Foster, Jankowiak, O'Meara, Teh, Rainforth (2020). *A Unified Stochastic Gradient Approach to Designing
  Bayesian-Optimal Experiments.* AISTATS, PMLR 108:2959–2969 · arXiv:1911.00294. **(ACE/PCE)**
- Foster, Ivanova, Malik, Rainforth (2021). *Deep Adaptive Design: Amortizing Sequential Bayesian
  Experimental Design.* ICML, PMLR 139:3384–3395 · arXiv:2103.02438. **(DAD, sPCE)**
- Ivanova, Foster, Kleinegesse, Gutmann, Rainforth (2021). *Implicit Deep Adaptive Design.* NeurIPS ·
  arXiv:2111.02329.
- Long, Scavino, Tempone, Wang (2013). *Fast estimation of expected information gains … Laplace
  approximations.* CMAME 259:24–39.
- Goda, Hironaka, Iwamoto (2018). *Multilevel Monte Carlo estimation of EIG.* arXiv:1811.07546. **[v]**
- Blau, Bonilla, Chades, Dezfouli (2022). *Optimizing Sequential Experimental Design with Deep RL.* ICML,
  PMLR 162:2107–2128 · arXiv:2202.00821.
- Shen, Huan (2021). *Bayesian Sequential OED for Nonlinear Models Using Policy Gradient RL.*
  arXiv:2110.15335.

**Goal-oriented / marginal EIG**
- Attia, Alexanderian, Saibaba (2018). *Goal-Oriented OED for Large-Scale Bayesian Linear Inverse
  Problems (GOODE).* Inverse Problems 34(9):095009 · arXiv:1802.06517.
- Elfving (1952). *Optimum Allocation in Linear Regression Theory.* Ann. Math. Stat. 23(2):255–262.
  **(c-optimality)**

**Model discrimination**
- Box, Hill (1967). *Discrimination Among Mechanistic Models.* Technometrics 9(1):57–71.
- Cavagnaro, Myung, Pitt, Kujala (2010). *Adaptive Design Optimization … Model Discrimination.* Neural
  Comput. 22(4):887–905.
- Drovandi, McGree, Pettitt (2014). *A SMC Algorithm to Incorporate Model Uncertainty in Bayesian
  Sequential Design.* JCGS 23(1):3–24.
- Borth (1975). *A Total Entropy Criterion for the Dual Problem of Model Discrimination and Parameter
  Estimation.* JRSS-B 37. **[v]**
- Atkinson, Fedorov (1975). *The design of experiments for discriminating between two rival models.*
  Biometrika 62. **(T-optimality)**
- Dickey (1971). *The weighted likelihood ratio … (Savage–Dickey density ratio).* Ann. Math. Stat. 42.
  **[v]**

**Poisson, cost, stopping, multi-fidelity**
- Wang, Myers, Smith, Ye (2006). *D-optimal designs for Poisson regression models.* JSPI 136(8):2831–2845.
- Wang, Smith, Ye (2006). *Sequential designs for a Poisson regression model.* JSPI 136(9):3187–3202.
- Snoek, Larochelle, Adams (2012). *Practical Bayesian Optimization of ML Algorithms.* NeurIPS ·
  arXiv:1206.2944. **(EI per cost)**
- Lee, Perrone, Archambeau, Seeger (2020). *Cost-aware Bayesian Optimization (CArBO).* arXiv:2003.10870.
- Lee, Eriksson, Perrone, Seeger (2021). *A Nonmyopic Approach to Cost-Constrained BO.* UAI, PMLR 161.
- Xie et al. (2025). *Cost-aware Stopping for Bayesian Optimization.* arXiv:2507.12453. **[v]**
- Wilson et al. (2024). *Stopping Bayesian Optimization with Probabilistic Regret Bounds.* arXiv:2402.16811.
- Wald (1945). *Sequential Tests of Statistical Hypotheses.* Ann. Math. Stat. 16(2):117–186. **(SPRT)**
- Chen, Lin, Yücesan, Chick (2000). *Simulation Budget Allocation … (OCBA).* Discrete Event Dyn. Syst.
  10(3):251–270.
- Audibert, Bubeck, Munos (2010). *Best Arm Identification in Multi-Armed Bandits (fixed-budget).* COLT.
- Gabillon, Ghavamzadeh, Lazaric (2012). *Best Arm Identification: A Unified Approach (UGapE).* NeurIPS.
- Kandasamy, Dasarathy, Schneider, Póczos (2017). *Multi-fidelity Bayesian Optimisation with Continuous
  Approximations (BOCA).* ICML, PMLR 70.
- Kandasamy et al. (2016). *GP Bandit Optimisation with Multi-fidelity Evaluations.* NeurIPS.
- Folch et al. (2022). *SnAKe: Bayesian Optimization with Pathwise Exploration.* NeurIPS. **(travel cost)**

**Exploration / uncertainty sampling / submodularity**
- MacKay (1992). *Information-Based Objective Functions for Active Data Selection.* Neural Comput.
  4(4):590–604.
- Seo, Wallat, Graepel, Obermayer (2000). *Gaussian Process Regression: Active Data Selection and Test
  Point Rejection.* IJCNN.
- Krause, Singh, Guestrin (2008). *Near-Optimal Sensor Placements in Gaussian Processes.* JMLR 9:235–284.
- Nemhauser, Wolsey, Fisher (1978). *An Analysis of Approximations for Maximizing Submodular Set
  Functions—I.* Math. Program. 14:265–294.

**Misspecification / robust design / calibration**
- Sloman, Oppenheimer, Broomell, Shalizi (2022). *Characterizing the robustness of Bayesian adaptive
  experimental designs to active learning bias.* arXiv:2205.13698.
- Overstall, McGree (2022). *Bayesian Decision-Theoretic Design of Experiments Under an Alternative Model.*
  Bayesian Anal. doi:10.1214/21-BA1286.
- Overstall (2023). *Gibbs optimal design.* arXiv:2310.17440. **[v]**
- Catanach, Das (2023). *Metrics for Bayesian OED under Model Misspecification.* arXiv:2304.07949. **[v]**
- Go, Isaac (2022). *Robust Expected Information Gain for Optimal Bayesian Experimental Design.* UAI,
  PMLR 180.
- DuMouchel, Jones (1994). *A simple Bayesian modification of D-optimal designs to reduce dependence on an
  assumed model.* Technometrics 36(1):37–47.
- Kennedy, O'Hagan (2001). *Bayesian calibration of computer models.* JRSS-B 63(3):425–464.
  doi:10.1111/1467-9868.00294.
- Wiens (2002). *Robust minimax designs for regression.* JSPI. **[v]**

**Classical optimal design (texts)**
- Fedorov (1972). *Theory of Optimal Experiments.* Academic Press.
- Atkinson, Donev, Tobias (2007). *Optimum Experimental Designs, with SAS.* Oxford.
- Pukelsheim (1993/2006). *Optimal Design of Experiments.* SIAM.

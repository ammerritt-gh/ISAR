# Smarter Steering — workstream design

> **Lifecycle:** Active plan
> **Provenance:** hand-written
> **Verified against code revision:** `d0ea1df` (2026-07-25)
> **Owner:** @ammerritt-gh · **Exit criteria:** in the status block below

**Status: design approved 2026-07-19; Phase 0 correctness repair in progress 2026-07-20;
sub-phases A → B → C remain Planned and sequenced third,** after the speedup sprint
(`docs/SPEEDUP_DESIGN.md`) and the background workstream (`docs/BACKGROUND_DESIGN.md`) —
both of which they build on: fast campaigns make validation cheap, and honest backgrounds
make the physics nudges trustworthy. Phase 0 is an urgent repair to the current controller,
not an early start on A, B, or C. **Owner: @ammerritt-gh.** Motivating finding — "counting targets
are absolute-meV only", with no signal-to-background awareness, from the 2026-07-19
statistics-contract audit: `docs/STATS_PRIMER.md` §7.

**Exit criteria.** Phase 0 is discharged: it shipped as O27 (the same-q budget / bridge-geometry
separation), O28 (the action-inventory control plane) and O29 as amended by O31 (the terminal
taxonomy). What remains open from it are the campaign replays that keep O25/O26 at Partial in
`DESIGN.md` §15 — Phase 0 closes when those replays pass. Sub-phases A → B → C each close when
their own acceptance campaign runs against TAVI with the counting target expressed relative to
background rather than in absolute meV, and `DESIGN.md` §15 records the status change.

Companions:
`docs/MEASUREMENT_STEERING.md` (the research foundation this workstream executes slices
of), `docs/HYPOTHESES.md` (the Tier-2 extensibility seam sub-phase B fills).

Owner intent: the Bayesian *skeleton* is right — soft priors on parameters, entropy-honest
branch assignment, fold-only admission — but the physics *content* is thin. ISAR should
incorporate as much physics information as possible (dispersion shape, Brillouin zones,
structure factors, transverse vs longitudinal character) **as nudges to probabilities and
priors, never hardcoded rules**, and spend beam time with maximal efficiency. Three
planned sub-phases: **A. a curvature-capable dispersion prior**, **B. physics content as
Bayesian nudges**, **C. scan economy**.

A `DECISIONS.md` entry opens each sub-phase when its implementation starts. O25 opens only
the Phase 0 repair and corrects O24's false implementation claim.

---

## 1. Baseline (verified 2026-07-19, checkup §2 and §4)

- **The dispersion prior is a straight line.** `prior_for()` pools same-sample, same-T
  points (0.5 K tolerance) and fits a **1D linear polyfit on the dominant-varying Q
  component**, σ = residual std floored at a constant **0.3 meV**
  (`isar/session/session_prior.py:84-128`). It cannot represent curvature, zone-boundary
  flattening, or anticrossing, and it never tightens below the floor no matter how much
  data exists. Fold-only admission is enforced at lines 48-50.
- **Zone awareness is acoustic-only.** `reduced_q()` folds by the acoustic period
  (`isar/session/branch_assignment.py:66-70`); there is no general zone-folding model, no
  cross-zone identification, and the prior pool never spans zones deliberately.
- **No structure factors, no polarization.** A code sweep found zero S(Q), form-factor,
  or LA/TA content anywhere; the acoustic 1/ω intensity law
  (`isar/session/branch_model.py:52-56`) is the only intensity physics, and
  MEASUREMENT_STEERING §8 already flags it as structure-factor-blind. HYPOTHESES' Tier-2
  registry names "polarization" and "intensity-vs-Q" as intended future classification
  features — the seam exists, nothing fills it.
- **Acquisition costs are neutrons, not seconds.** The whole-scan EIG ranking divides by
  measurement cost = `n_points × ncount` (`isar/drive/acquisition/acquire.py:624-625`,
  `eig.py`); no per-point overhead, no motor/travel cost (`acquire.py:22`: movement-aware
  cost waits on TAVI's virtual instrument clock). Per-second Fisher information and an
  OCBA/value-of-information stay-vs-move rule are the **Planned remainder of DECISIONS
  O13**. The adaptive survey-window logic (`acquire.py:274-327`) is Implemented and
  sound.
- **Owner clarification (recorded in checkup §1):** ISAR runs scan-by-scan and re-decides
  after every scan; statistics accumulate through pooled masters. Counting floors are
  per-request; there are no count-until-target loops. When modes blend at one q, scanning
  neighboring q where they separate and returning with tight priors is a first-class
  alternative to more dwell.

**Workstream target:** on synthetic campaigns with curved two-branch truth and planted
structure factors, the prior tracks the true dispersion (including curvature and
cross-zone folds), branch identity resolves in measurably fewer scans with physics
features on than off, and counts-to-tolerance drops against the O13 baseline — with
wrong-hypothesis campaigns still recovering (nudge, never force).

---

## 2. Phase 0 — AP1/AP2/AP3 branch-controller correctness repair (in progress)

AP1 and AP2 falsified O24's claim that the connected tracker had bounded ambiguous-frontier
revisits. AP1 surveyed successfully and then repeated one ambiguous point. AP2 started with
three classification probes but then submitted H=2.10 for production thirteen times before
moving to H=2.00. This is a controller correctness failure, not a request for more production
dwell and not evidence that acoustic campaigns require a separate acquisition architecture.

The repair keeps four states distinct:

- the persisted off-centre **acquisition seed**, chosen because the target is expected to be
  resolvable there;
- the physical **reduced-q origin and period** declared by the branch hypothesis, which define
  how an acoustic energy approaches zero at the zone centre;
- **unordered fitted candidates**, which are evidence even when no target identity is confirmed;
- **confirmed target delivery**, the only state allowed to publish target `omega0`, certify,
  seed the target prior, or join the target curve.

Branch identity is recomputed jointly over accumulated path evidence only after an authoritative
pooled-master revision. The controller must never assign an acoustic identity from the locally
lowest or only candidate, move the acquisition seed to such a candidate, or treat an ambiguous
maximum-posterior assignment as delivered truth. Target/rival roles are symmetric: the declared
target determines which branch is delivery and which is rival in both acoustic and optic
campaigns.

Each proposal persists the authoritative evidence revision, a deterministic proposal fingerprint,
and a concrete action role beneath the canonical confidence-ladder stage (Discovery, Inference,
or Discrimination). An equivalent same-q identity revisit is permitted only after newer fitted
neighboring evidence exists and only within the existing `maximum_bridge_steps` allowance. Phase 0
adds no competing revisit cap. With unchanged evidence, local ambiguity must instead invoke the
existing O6/O9 branch-information ranking and buy the best feasible unvisited differentiation
point. Exhausted bridge and saturation routes end honestly as `incomplete` with
`branch_ambiguity` / `frontier_blocked`.

> **Superseded as shipped (O27, 2026-07-21; terminal taxonomy O28/O29/O31).** The paragraph above
> is the Phase 0 design as approved on 2026-07-19. What was actually built separates the two knobs
> the design conflated: **`maximum_production_batches_per_q` is the single same-q production
> budget** — every production at a q charges to it, whether the campaign calls it scan statistics,
> identity work, or a rival reconnection — while **`maximum_bridge_steps` is only a path-geometry
> allowance**, not a revisit counter. Further same-q exposure stays available while identity work
> is authorized and unsaturated.
>
> The terminal rule also tightened. `branch_ambiguity` / `frontier_blocked` is legitimate only
> after the *whole registered action inventory* is provably exhausted (O28), and a proposal-less
> frontier that still has authorized same-q work is a `ControllerInvariantError` — a controller
> fault, not a scientific result (`isar/drive/loop.py`). Current contract:
> `docs/MEASUREMENT_STEERING.md` and DECISIONS O27–O31. Sub-phases A/B/C below are unaffected.

The objective and persisted-state changes are additive from v2. Existing frozen envelopes retain
their replay semantics; newly compiled named acoustic and optic objectives share the corrected
controller. Target-free discovery remains Planned.

Phase 0's acceptance gate is a deterministic AP2 replay with the actual 0.5 meV objective and
proposal sequence, plus AP1's two-candidate deadlock, acoustic geometry on both sides of the
physical zone centre, acoustic/optic role symmetry, provisional-fit non-authority, unchanged-
evidence repeat rejection, and honest exhausted-frontier stopping. Until those tests pass, this
section remains **in progress**, not Implemented.

AP3 adds two acceptance conditions. Branch character is a trajectory-level posterior: the connected
0.45 → 0.87 → 1.38 → 1.80 meV sequence must remain acoustic as it approaches the zone boundary,
and accumulated authoritative evidence may reverse an earlier optic character. O6/O9 broadening
discounts identity information by uncertainty propagated across unmeasured path links, without a
hard step limit. At exact Γ, a connected low-q acoustic trajectory may establish an additive
`inferred_unresolved` structural endpoint. That endpoint publishes no fitted `omega0`, enters no
dispersion prior, and is excluded from precision coverage. Automatic Bravais/space-group zone
derivation remains Planned; AP3's declared period already places H=3 at the zone boundary.

---

## 3. Sub-phase A — Curvature-capable dispersion prior

### A.1 Functional form

Replace the linear polyfit with a **low-order periodic basis in reduced q** that respects
zone symmetry by construction: for a branch with period p, a small sine-like expansion
(e.g. ω(u) = Σ c_k sin(kπu/p)-type terms for acoustic — ω=0 at Γ, zero slope at the zone
boundary — and the cosine-side analogue for optic: free gap intercept at Γ, zero slope at
both Γ and the zone boundary) fit by the same weighted least squares the branch beliefs
already use. Which form applies comes from the branch's **declared hypothesis character**
(`dispersion_type`/preset) — never inferred mid-fit. Order grows with fold count (start
linear-equivalent, admit curvature terms only when enough clean folds exist to constrain
them — the same "earn your parameters" discipline AIC applies to lineshapes, applied to
the prior). A "clean fold" throughout this document means exactly what the existing
admission gate admits: a `resolved`, fold-modality fit; n_folds is the count of admitted
prior points for that branch.

*Alternative considered:* a GP with periodic kernel — strictly more flexible, but brings
hyperparameter choices, non-determinism risk, and O(N³) cost for no present need at
tens-of-points scale. The sine basis is deterministic, cheap, and symmetric by
construction. Revisit GP only if real dispersions defeat the low-order basis.

### A.2 Uncertainty

σ remains residual-based but the **floor decays with clean-fold count** (e.g.
floor = max(σ_min, 0.3 meV / √n_folds) — exact schedule fixed at implementation and
validated in A's synthetic campaigns) so the prior can actually tighten with evidence.
A prior-only belief (declared hypothesis, no data) keeps its honest declared width
unchanged.

### A.3 Keying: general folds and zone labels

Prior points gain a **general reduced-q key** (fold by the branch's declared period, not
the acoustic-only special case) plus an explicit **zone label**, so a clean fold measured
in zone (2,0,0) informs the prediction in zone (3,0,0). ω is zone-periodic and crosses
freely; *intensity is not* — amplitudes never cross zones in sub-phase A (that is
sub-phase B's structure-factor feature). Existing single-zone behavior is the degenerate
case; existing campaigns replay unchanged.

### A.4 Unchanged invariants

Fold-only admission; ambiguous forks never enter; priors enter fits as parameter
precision (never pseudo-data); the data-cost / posterior-cost split; branch beliefs still
never span a branch crossing.

---

## 4. Sub-phase B — Physics content as Bayesian nudges

Everything in this sub-phase lands through the **HYPOTHESES Tier-2 registry seam** — a
`dispersion_type`/feature plugin supplying prior contributions, likelihood features, and
steering defaults — and everything is **confidence-scaled**: a feature with low declared
confidence barely moves the posterior; nothing is an if/else rule.

### B.1 Structure-factor / intensity-vs-Q expectation

A hypothesis may declare a coarse expected intensity profile over Q — as simple as
"stronger in zone (2,0,0) than (1,0,0)", a |F(Q)|²-proportional table, or the default
"unknown" (feature off, exactly today's behavior). It enters in two places:

- as a **likelihood feature for branch assignment**: the assignment posterior weighs
  whether an observed normalized intensity is consistent with each candidate branch's
  expectation — using the provenance-tagged normalized intensity discipline
  (MEASUREMENT_STEERING §4.4) that already exists;
- as an **expected-counts factor in acquisition**: predicted λ for a candidate scan in a
  given zone scales by the declared profile, so the driver prefers measuring a branch
  where it is bright — without forbidding anywhere. "Without forbidding" is structural,
  not aspirational: the scaling ratio is clamped to a bounded range (fixed at
  implementation, e.g. [1/R, R] with R ~ 10) so no candidate's expected counts — and
  hence no candidate's EIG — is driven to zero by a declaration.

With A.3's zone keys this *explains* cross-zone intensity differences instead of being
confused by them: same ω fold, different amplitude, one consistent belief.

### B.2 Polarization (LA/TA) expectation

For a branch hypothesis, a declared polarization character implies an intensity
proportional to |Q·ê|² — longitudinal branches bright where Q ∥ propagation, transverse
where Q ⊥. The declared *character* (ê relative to propagation) is a zone-invariant
property of the branch; the intensity *prediction* varies zone-to-zone and
direction-to-direction because Q does — |Q·ê|² is evaluated per candidate from the
geometry already available through `isar/geometry`. Implemented as the same kind of
likelihood feature as B.1 (it *is* a structure-factor statement, specialized). This is what lets ISAR check transverse vs longitudinal by *choosing
where to look*, Bayesianly: two branch hypotheses differing in polarization make opposite
intensity predictions in different zones/directions, and Stage-C discrimination
(MEASUREMENT_STEERING §2) buys the distinguishing scan on ordinary EIG grounds.

### B.3 Discipline (the non-negotiables)

- Features are **data-side likelihood terms and prior precisions** — never injected as
  pseudo-anchors, never consulted by AIC/goodness-of-fit/falsification (fit-vs-decide).
- **Classification features come from real data only** where the §4.5 anti-circularity
  rule requires it; each new feature ships with a regression test reproducing the
  observed historical failure mode (a rival's zone-centre feature absorbed into the wrong
  belief) and demonstrating the feature does not recreate it.
- A wrong declared profile must lose to data: the posterior-predictive disagreement check
  (§4.5 third guard) covers intensity expectations exactly as it covers energy priors,
  and its outcome is defined, not discretionary — sustained disagreement **decays that
  feature's confidence weight toward zero and raises the flag** (recorded in machine-owned
  `card.state.json` and stated in the report); it never silently keeps steering on a
  discredited profile, and it never deletes the declaration (the human closes the loop).
  Wrong-hypothesis validation campaigns are acceptance criteria, not nice-to-haves.

---

## 5. Sub-phase C — Scan economy (the O13 remainder, made concrete)

- **Per-second information.** Rank candidates by EIG **per second**, not per neutron:
  Poisson information is linear in time (`I = t·(∂λ/∂θ)²/λ`), so per-second information
  is analytic; the cost model gains an explicit **per-point overhead constant** (setup +
  readout per point), making few-long-points vs many-short-points a real tradeoff instead
  of an invisible one. Overhead constants live in instrument config / are measured from
  TAVI timing when available.
- **Stay-vs-move as value-of-information.** An OCBA-style rule at scan granularity —
  **one-step myopic** (consistent with MEASUREMENT_STEERING §6: build myopic first), over
  the existing candidate set (objective-path grid plus purpose-tagged driver-inserted
  classification q), with the marginal EIG **including cross-q prior tightening** (a
  neighbor scan's value counts the sharpening it feeds back through the sub-phase-A
  prior — that inclusion is precisely what makes breadth competitive with depth): after
  each scan, compare the marginal EIG-per-second of another request here vs the best
  alternative q — honoring the owner clarification structurally: per-request floors,
  pooled accumulation, re-decide every scan, and **neighbor-q breadth as a first-class
  move** (a blend at q gains more from a neighboring separated q, whose fold tightens the
  prior, than from more dwell — the EIG landscape must be allowed to say so, which
  sub-phase A's curvature-capable prior is what makes possible).
- **Movement-aware cost** (Planned-dependency): once TAVI exposes a clock/travel model,
  add axes-travel time to the cost denominator and order the per-iteration worklist by
  travel; until then the seam is the cost function, already isolated at the ranking site.
  The **second-best-point trick** for fast instruments (choose the second-ranked point to
  overlap analysis with motor movement; MEASUREMENT_STEERING §3 Tier-3 note) is recorded
  as design intent behind the same seam.
- **Cheap diagnostic first:** a window-width-vs-iteration plot (survey-mode decay curve)
  and an EIG-rank trace in the campaign diagnostic, so economy claims are read off
  reports, not re-audited.

---

## 6. Validation

Synthetic-first, on the fast campaigns the speedup sprint delivers:

1. **A:** curved-dispersion truth (quadratic-through-zone-boundary and anticrossing-free
   sine truth): prior tracks curvature within its stated σ; floor decays with folds;
   cross-zone folds inform prediction; linear-truth and single-zone regression campaigns
   byte-identical decisions.
2. **B:** two-branch LA/TA truth with planted structure factors: assignment entropy at
   fixed scan count measurably lower with features on; a campaign with a deliberately
   wrong intensity profile still converges to truth (nudge-not-force), with the
   disagreement flag raised; per-feature anti-circularity regression tests pass.
3. **C:** counts-to-tolerance and wall-time vs the O13 baseline on the reference
   campaigns; the blend scenario (two phonons merged at one q) resolved via neighbor-q
   moves without any count-until loop; overhead constant demonstrably shifts the
   few-long vs many-short choice in the planted regime that favors each.
4. Full-suite regression: `pytest -m "not slow"` green at each sub-phase boundary.

Phase 0 is gated separately and immediately by its AP1/AP2 replays in §2. Passing that gate does
not advance the status of A, B, or C.

## 7. Guardrails

- Fold-only prior admission; forks never enter; constrained/demoted fits never feed the
  prior that constrained them.
- Priors and features are parameter-precision / likelihood terms — never pseudo-data,
  never visible to AIC, goodness-of-fit, or falsification.
- Branch beliefs never span a branch crossing; driver-inserted q never seat convergence.
- Scan-by-scan re-decide discipline: no closed counting loops against a target.
- All schema/report additions additive; AGENTS.md hard rules untouched; no LLM in the
  runtime path of a number.

## 8. Out of scope (explicitly deferred)

- The GP intensity-field survey map (full L0) and non-phonon physics plugins (magnon,
  crystal-field `level`, `continuum` completion) — behind the same Tier-2 seam, later.
- Amortised / non-myopic acquisition policies (unnecessary while decisions are cheap
  relative to a count).
- **Within-scan point-by-point adaptivity** and full motor path planning — a future phase
  once movement-aware cost exists; sub-phase C deliberately stops at scan granularity.
- The Bayesian credible-interval rung (STATS_PRIMER Planned item) — orthogonal.

## 9. Sequencing within the workstream

After the independent Phase 0 repair, A → B → C remains the approved sequence behind the
speedup and background prerequisites, each validated before the next: B's cross-zone intensity
features need A's zone keying; C's neighbor-q economy needs A's curvature-capable prior to value breadth
correctly, and benefits from B's identity features to know *which* neighbor is
informative. A is independently shippable (better prior, no new physics declarations
required). Each sub-phase ends with its validation evidence committed under
`docs/campaigns/` and its DECISIONS entry recorded.

---

*Approved by Adrian 2026-07-19 (single-document workstream; internal split
Prior → Physics → Economy). Motivated by checkup concerns 2 and 4; executes the
corresponding Planned slices of MEASUREMENT_STEERING §4.1/§7 and DECISIONS O13.*

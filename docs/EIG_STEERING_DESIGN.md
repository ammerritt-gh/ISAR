# Same-action scan-window EIG — design contract

> **Lifecycle:** Active plan
> **Provenance:** hand-written
> **Verified against code revision:** `9d77631` (2026-08-04)
> **Owner:** @ammerritt-gh
> **Exit criteria:** the restricted authoritative selector exists; Slice 10 completes the deterministic
> TAVI EIG-4 demonstration; exact eligible selection and ineligible non-EIG return behavior are
> verified; accepted behavior is folded into `MEASUREMENT_STEERING.md`; this plan is then removed and
> tombstoned according to policy.

## Status and boundary

**EIG-4 is complete as of 2026-08-05; restricted Slice 9 runtime authority is implemented and opt-in.
The O42 release review is retired by H1: the selector becomes default-on once the SCENE world suite
exercises it, and that flip lands at SCENE-4. Authoritative selection is demonstrated, not merely implemented:
the first real target-core grant executed in deterministic simulation on 2026-08-05 (O41 Test 10
iter 28: Δ/pt 0.0069 nats at 3.31× the paired SE, a 17-point core against the 50-point legacy), and
a cold-start campaign now selects the core on every scored decision.**

**The polarity question is settled, and not by switching a default.** The intended end state was
described as legacy the survey instrument for the first pass and EIG the follow-up, with a polarity
switch deferred. What landed instead (O43) is a *symmetric* comparison: the two candidates are
compared on information per neutron and the winner is taken, with ties broken by cost rather than by
retaining the survey. Neither is a default. The survey still wins the first pass, but structurally —
before a q has been measured there is no prediction to centre a core on, so no core candidate
exists — rather than by a rule that prefers it.

**Two limitations to read the grant rate against.** The QoI partition is `bin_width = 0.5 meV` while
the design prediction sigma in a converging campaign has a median of 0.0182 meV, so once a fit
exists both candidates answer "which bin?" identically and the comparison is very often a tie
decided on cost rather than on information. That was accepted deliberately at EIG-4 close: narrowing
the partition would churn every policy and variant fingerprint and the frozen Slice-8 fixtures, and
the physics says the centred core is the right scan anyway. It remains the honest next improvement
if the comparison is ever required to do real work. Separately, the eligibility route check owes only
the containment `scored ⊆ offered`; whether the surviving normalisation is sound is delegated to the
producer's own `selection_eligibility`, and the reason a label went unscored is not itself
authenticated per label.
The pure full-vector Poisson score kernel, the controlled offline Slice 5 counterfactual, Slice 6
scored shadow inventories, and Slice 7 exact realized-request rescoring are implemented. The coherent
Slice 8 V3 gate is owner-accepted under O41 and authorizes the implemented Slice 9 selector, which
stays opt-in until the SCENE world suite exercises it (H1 retired the O42 release review). Candidate generation, predictive `/2`, arbitrary-grid evaluation, executed-grid identity, target
projection, Slice 2 eligibility/QoI adaptation, the Slice 3 homogeneous design measure, the Slice 4
caller-supplied score set, and Slice 5 completed-campaign comparison are implemented.
Current acquisition ranks scientific actions and Q
positions using expected counts already stored on each master's fitted grid. The
`ScanDesignContext → ScanDesignInventory` seam now generates the bounded family of conventional
uniform windows for the selected action and persists it under `report/scan-design/<sha256>.json`.
In `shadow` mode it may attach diagnostic scores for exactly `legacy_uniform/1` and `target_core/1`;
the `legacy_uniform/1` candidate is still generated with the exact historical arithmetic and is
always the selected, finalized, and executed one.

The `scan_window_eig_mode` steering knob (typed on `SteeringPolicy`, declared in the hand-edited
policy card, frozen into `ScanDesignContext`) chooses the regime:

- `off` — only the legacy candidate is designed; `selection_basis = legacy_control_off`.
- `shadow` — the whole family is generated. Exact eligible source evidence scores only legacy versus
  target-core and records `selection_basis = shadow_legacy_control_scored`; every unavailable or
  scoreless comparison records `shadow_legacy_control_unscored`. The emitted request is identical to
  `off` in either case.
- `authoritative` — accepts only the exact O41 profile, authenticates the final legacy/target pair,
  durably records the decision, and may submit `target_core/1`. Invalid profiles fail loudly; absent,
  ineligible, stale, malformed, or unauthenticated evidence returns to the bounded legacy/non-EIG path.
  This implementation is opt-in until the SCENE world suite exercises it; H1 retired the O42
  release review that previously gated it.

`scan_window_eig` is optional in a targeted step payload: the reader treats an
omitted block as the frozen no-policy/default-scoring configuration. This is a schema default, not
card supplementation — frozen payloads, programs, and resume records remain
self-contained. New writers always serialize the block, while explicit null, malformed records,
unknown fields, and card authoring or scope-binding markers refuse. A retained `shadow` objective
without a policy therefore remains a valid unscored legacy control.

### The scored grid is the instrument's, not ISAR's (ADR-018)

A candidate is designed as a uniform request `(e_lo, e_hi, e_step)`, but the instrument does not
execute that triple — it executes the point list its own parser derives from the submitted command
**string**, at its own numeric precision. Scoring a design against energies the instrument will
never visit is a silent correctness bug, because the scored grid and the executed grid look equally
plausible in isolation.

An **instrument grid adapter** (`isar/drive/acquisition/grid_adapters.py`) is therefore the single
authority on three things for one instrument: how a request is rendered onto the wire, how many
points that rendering produces, and where those points sit. `UniformGrid` delegates all three, so
every grid identity — candidate fingerprints, dedup keys, target-support counting — is taken over
the **adapter-canonical executed energies** rather than a precision ISAR invented for itself. The
grid fingerprint is `isar.uniform_scan_grid/2` and carries an
`isar.instrument_grid_adapter/1` identity block (`adapter_id`, `adapter_version`) inside the hash:
the same `(lo, hi, step)` executed under different instrument semantics is a different scoring
object, and a stored candidate says which semantics it was scored under.

The shipped `tavi_delta_e` adapter (v1) reproduces `tavi.utilities.parse_scan_steps` exactly
**without importing TAVI** — ISAR must not grow a runtime dependency on the instrument server to
know what it asked for. Two properties of that reproduction are load-bearing:

- **the `%g` wire round-trip is part of the contract.** TAVI receives the command as a string and
  parses it back with `float()`, so a designed bound of `3.3999999999999995` reaches the instrument
  as `3.4`. Every value goes through the same `%g` rendering ISAR submits *before* the point-count
  and point-position arithmetic is applied.
- **the rounding is numpy's round-half-to-even**, not Python's `round`. Executed values are
  `np.round(np.linspace(start, last, n), 3)`; a value landing exactly on `x.xxx5` goes to the even
  neighbour, and reimplementing this with `round` would disagree on exactly those ties.

Cross-repo agreement is held by golden parity fixtures in `tests/drive/test_grid_adapters.py` plus
one `live`-marked round-trip test asserting the adapter-predicted grid equals TAVI's returned
`scan_values_1` exactly. A second instrument declares its own adapter id/version and is threaded
through `resolve_uniform_grid`; nothing outside the adapter module is TAVI-specific.

**The legacy control's point count goes through the adapter too.** For one release it did not: the
legacy and generator-declared window paths kept a private `floor(span/e_step + 0.5) + 1` on the raw
*designed* span, plus a clamp to the point cap. The adapter counts over the **wire** span, so
whenever the `%g` rendering moved a bound the two disagreed, and ISAR recorded, priced and scored a
point TAVI never visits — measured live, `deltaE -5.44493 7 0.162679` was recorded at 78 points and
executed at 77, the 78th naming an energy of 7.081 meV outside the declared window. It is a near-tie
regime (a few windows in 10⁵), which is precisely why a second arithmetic survived review.

Both paths now take their count from `resolve_uniform_grid`, so ADR-018's claim covers every family.
What stays frozen is the window *geometry*: the legacy step is rendered verbatim into the submitted
`deltaE`, so when the cap coarsens it, it is still derived from the designed span
(`COARSEN_FROM_DESIGNED_SPAN`) rather than the wire span every other family uses. That choice is
named in `scan_grid.py` and is the reason the emitted command is byte-identical across this change —
only `n_points`, `scoring_grid` and `nominal_cost` move, and only where ISAR was previously wrong.
The count is never clamped: where the two coarsening rules disagree (reachable only far outside the
meV energy range ISAR drives), the legacy candidate records the over-cap count the instrument will
really execute, and the loop's existing point-count guard coarsens the request.

**A window the instrument cannot accept is not a candidate.** TAVI refuses any single-point request
outright (`HTTP 400 … Only 1 scan point!`), and `resolve_point_count` normalizes an inverted window
to its single starting point, so both degeneracies present as `n_points == 1`. A non-legacy family
that resolves to fewer than two points, or to inverted bounds, is refused with `degenerate_window`
rather than emitted. It is reachable from ordinary state: `broad_recovery/1` on a narrow authorized
domain such as `(6.9, 7.0)` resolves to exactly one point at any resolution-derived step wider than
the domain. The check runs *after* the target and support gates, because it is the least informative
diagnosis — a family that also lost its core must say that instead. The legacy control is not
gated (§ legacy parity); a degenerate legacy request is refused by TAVI's validator as
`would_queue: false`, which the loop already classes as a structural `validation_rejected`.

The families are `legacy_uniform`, `target_core`, `target_paired` (only when anti-Stokes is
admissible under the existing `positive_only` / detailed-balance gates), `target_low_anchor`,
`target_high_anchor`, `target_plus_rival` (only with a declared rival band, always contiguous with
the core so a canonical target action can never receive a rival-only window), and `broad_recovery`.
Their construction constants live in one frozen, separately versioned
`PhysicalWindowGeneratorConfig` serialized into every inventory, now at
`isar.physical_window_generator_config/2`. The `/2` bump **removes `grid_energy_decimals`**: energy
precision is the instrument adapter's to declare, not a caller-supplied construction constant, so
carrying it here let a design be scored at a precision the instrument does not use. The generator
itself lives in `isar/drive/acquisition/physical_windows.py`
(`physical_window_generator`, version 2); `scan_design.py` keeps only the generic
context/inventory/audit envelope, so the adapter seam touches window physics without touching the
envelope that will later carry a differently grounded generator.

Non-legacy candidates are clipped to the authorized energy domain, must hold the policy's minimum
target support, and are pre-clamped to the policy/runtime/budget limits already carried by the
context. A family that cannot satisfy its invariants is recorded as a refused candidate with a
structured reason code, never silently omitted; families landing on the same canonical grid and
exposure deduplicate to one candidate carrying the others as alias provenance.

Post-clip target gates are three distinct promises with three distinct codes, because the earlier
single gate refused only *complete* disjointness and so let a core of `[3.4, 6.6]` clipped to
`[6.5, 7.0]` survive as a 0.1 meV **sliver** presented as a target candidate:

- `target_core_truncated_after_domain_clip` — the clip left too little of the core to be that core;
- `rival_only_after_domain_clip` — the surviving window covers only the rival band;
- `declared_rival_window_is_authoritative` — a declared rival window would displace the target.

Alongside them, `empty_after_domain_clip`, `insufficient_target_support`, and
`nonfinite_window_arithmetic` (degenerate instrument state becomes a structured refusal on the one
family, never an exception that destroys the whole inventory) complete the refusal vocabulary.

New production inventories use `isar.scan_design_inventory/3`; `/2` is an explicit historical read
contract and is never migrated or reminted. Candidates remain `isar.scan_design_candidate/2` — there
is no candidate `/1` compatibility. Every `/3` inventory carries a mandatory authenticated shadow
envelope and still selects exactly the unique legacy control. Alongside the generator config the inventory carries the generic provider
identity (`predictive_provider_id`, `predictive_provider_schema`, `generator_id`,
`generator_version`, `objective_projection_schema`, `steering_regime`) so a later, differently
grounded provider can slot in without changing this envelope; `candidate_family` and `generator_id`
are versioned strings, never a closed enum.

The immutable shadow envelope, status constants, seed evidence, variant records, and structural
fingerprint checks live in `isar.schema.eig_shadow`. Ordinary scan-design construction therefore
depends only on schema records and does not import the numerical EIG stack or SciPy. Public inventory
validation and content-addressed loading cross a lazy seam into `isar.eig.shadow_inventory` only when
a `/3` record contains scored evidence; that deep gate replays policy joins and Slice 4 arithmetic.

Before either predictive evaluation, the live pair gate reports one ordered diagnosis: aliasing or
equal candidate IDs/grids is `candidate_pair_degenerate`; an explicit target-core generator refusal
retains its exact reason; a missing or duplicate family is respectively `candidate_family_missing`
or `candidate_family_ambiguous`; unequal per-point exposure is `candidate_exposure_mismatch`; and
remaining action, Q, context, or generator incoherence is `candidate_pair_invalid`. An incomplete
Slice 4 result may attach fingerprinted `score_refusal_evidence` to that variant. It contains only
the score-set status/reasons and the two authenticated candidate refusal identities and codes—never
score estimates—and its codes are also aggregated into the variant reason list. The field is omitted
when absent, preserving historical variant payloads and fingerprints.

Materializing `scan_window_eig_mode` in the resolved steering dict reminted every
`hypothesis_fingerprint` once, and with it every derived scientific-action and content-addressed
audit identity. The *physical* behavior — action class, Q sequence, window, step, count request, and
emitted request — is unchanged in both `off` and `shadow`.

Two evidence revisions remain intentionally distinct. Proposal metadata and the finalized request
fingerprint use the established transition-first `authoritative_evidence_revision` chain. The
in-memory `ShadowScoringSource` instead puts the exact selected completed master's
`master_revision` (or null) into prospective action provenance. Eligibility joins on that exact
master identity and never substitutes the proposal-wide transition revision, a same-Q master, or a
later revision.

The five-second shadow allowance is cooperative. Each variant checks the deadline before calling the
synchronous scorer and again immediately after it returns, before score-set validation. A post-call
overrun discards that returned numerical result, records `shadow_scoring_deadline`, marks the remaining
variants likewise, and still submits the unchanged legacy request.

The future work described here is narrower than the physics-content work in `STEERING_DESIGN.md`.
It may compare different uniform windows/counts for the **same already-selected scientific action**.
It may not invent branch hypotheses, change evidence routing, fit data, alter QC, or move scientific
selection out of the existing action inventory.

## Locked EIG-4 authority envelope

O36 authorizes only a clean, confirmed target at an already-selected scientific action and Q, using
canonical target production or confirmation. The frozen eligible comparison is
`legacy_uniform/1` versus `target_core/1`, with equal per-point exposure. Rank candidates by
conditional target-QoI information per executed point; paired uncertainty must resolve the
comparison before `target_core/1` may win.

O41 accepts the coherent Slice 8 V3 gate as sufficient evidence to implement this selector. The
positive envelope contains exactly one clean case. Its `delta - 2 * paired_SE > 0` threshold is a
pragmatic engineering margin, not calibrated 95% coverage. Here `delta` is the mean of the eight
paired block target-minus-legacy per-point score deltas and `paired_SE` is their sample standard
deviation (`ddof=1`) divided by `sqrt(8)`. The factor 2.0 is the sole selector safety margin; the
profile does not apply a second advantage multiplier. Slice 9 implements this contract; H1 retired the
separate O42 release decision, so the selector goes default-on once the SCENE world suite exercises it.

The exact restricted profile is 128 simulations, 8 paired blocks, the two candidates
`legacy_uniform/1` and `target_core/1`, equal finalized per-point `ncount`, resolved score status,
strictly fewer target points, and no candidate or score refusal. Independent adapter validation of
both final clones adds one extra validation call per authoritative selection; this traffic is
negligible compared with executing the scan and does not affect `off` or `shadow`.

### Slice 9 bounded selection lifecycle

The refresh allowance is one shared campaign-iteration budget across every cause. It permits at
most two selector calls and never a third materialize/reselect loop.

| State | Trigger | Permitted next selection | Refresh left | Required action |
|---|---|---|---:|---|
| Initial pair | Both independently validated clones authenticate | Legacy or target | 1 | Select once, materialize that exact request, and authenticate it. |
| Initial pair | Target validation/refinalization changes or refuses the target | Legacy or target after rebuilding both clones | 0 | Consume the shared refresh and reselect from the refreshed pair. |
| Selected request | Materialization or immediate pre-submit authentication changes any bound request field | Legacy or target after rebuilding both clones | 0 | Consume the same shared refresh and reselect; either direction is allowed. |
| Refreshed selection | The second selected request changes, refuses, or fails authentication | Legacy only | 0 | Rebuild and authenticate the legacy request, submit it, and record the fallback; no third selector pass. |
| Any state | Evidence is unavailable, stale, malformed, or unauthenticated | Legacy/non-EIG path | 0 | Grant no EIG authority and preserve the ordinary recovery semantics. |

The decision artifact uses `isar.scan_design_authority_decision/o41.1` and is durably written under
`report/scan-design-authority/` before the durable `submission_intent` event and before the TAVI
submit. The submission record binds the exact decision hash. Recovery reauthenticates that hash and
the selected request rather than inferring authority from a role label.

Returned-grid mismatch invalidates EIG applicability only for that acquisition/decision lineage.
It never permanently disables the target band, master scan, or campaign: a later independent
acquisition may build fresh evidence and try again. The mismatched acquisition is retained and
analyzed, is never automatically resubmitted, and grants no realized EIG comparison authority.
The campaign reducer counts failed selector predicates and displays them with the latest selected
role, so a persistently legacy-only campaign is visible rather than a silent no-op.

Absolute target-QoI information noninferiority is not required: a narrower design may deliberately
contain less total information. `legacy_uniform/1` is the eligible comparison control, not a
universal recovery rule. Outside eligibility, EIG returns to the existing non-EIG design/recovery
path and does not alter that path's choice; it may use the legacy window, a broad/recovery window,
or another already-established action-specific behavior.

This authority excludes target-presence calibration, AICc, STAT-2c completion, formal Bayesian
model averaging, general phenomenon support, TAVI virtual wall-clock completion, and McStas campaign
completion. It must not choose Q, scientific action, target identity, fit model, hypothesis,
physical constraint, interval method, certification rule, or model-selection authority.
Target-presence, model-identity, and branch-identity values may be calculated as diagnostics only.
Current production AIC weights remain the input source and are not validated by the held-out campaign.

Slice 2 freezes the pre-comparison eligibility seam. Its action predicate admits only the selected
`MeasurementIntent.action_class` values `production` and `confirm`, both with
`acquisition_action_role = "production"`, `evidence_sink = "canonical_master"`, no driver insertion,
and the same canonical nine-decimal HKL as the target. A projection authority `action_role` describes
fit/refit authority (for example `certification`), not acquisition intent, and never decides this gate.

The derived Slice 2 evidence and eligibility revision is `o36.2.1`, with omega0 adapter revision `3`;
predictive `/2` and target-projection `/2` payloads remain unchanged. An action is authenticated by
reconstructing the persisted `MeasurementIntent` and
recomputing its scientific fingerprint. The content-addressed inventory evaluation, proposal metadata,
and action provenance must all carry that same value; class, sink, canonical Q, and authoritative master
revision must agree as well. Target Q is normalized in the single order `hkl`, then `target_hkl`, then
producer-owned `q`. Any missing or inconsistent action identity is `action_context_unknown`, never a
plausible canonical action.

The runtime supplies the contemporaneous selected intent and finalized-proposal provenance. A completed
member acquisition is historical input to a master, never a substitute for the pending action whose
window is under consideration. The census may audit an unresolved persisted `propose` snapshot joined to
its content-addressed action inventory; otherwise it reports `action_context_unknown` rather than
retrospectively inferring production or confirmation.

Eligibility is before candidate generation and comparison. It neither inspects window families nor
checks paired exposure, scores, or selection. Those belong to Slices 4–6. Source predictive/projection
artifacts retain `eig_shadow = eig_authoritative = ineligible`; derived eligible evidence is only an
`eig_shadow = candidate` input to later shadow work and cannot enable authority.

The frozen pure runtime surface is `isar.eig.evaluate_eig_eligibility()` over an immutable
`EligibilityInput`, returning `eligible`, `ineligible`, or `unassessable` with deterministic ordered
scientific exclusions and observability gaps. Its public reason taxonomy is intentionally compact;
reader-specific validation detail remains diagnostic. It performs no workspace I/O or persistence, so
the census is only one adapter of the same contract rather than a second policy implementation.

### Generic QoI seam

`isar.eig` now exposes a pure generic QoI interface. Its first adapter,
`adapt_omega0_target_projection()`, converts authenticated `isar.master_predictive/2` plus
`isar.target_projection/2` evidence into immutable target-QoI evidence for `omega0`. It requires strict
predictive and projection semantics, positional model/draw alignment, source EIG-ineligible scopes, and
the current authority record. A target-present draw must have a finite QoI; a target-absent draw must have
`null`. Invalid combinations fail closed as projection-record invalidity, not as target absence.

The immutable evidence retains the actual projection authority and its canonical fingerprint; predictive
basis, retained-set, statistical-semantics, and source-model-selection identities; target-projection-set
and projection-semantics identities; and a derived QoI-evidence fingerprint over the complete semantic
envelope. That envelope includes fixed contract/QoI metadata, status, ordered scientific and
observability codes, source and derived authority scopes, the source identities and authority, and
normalized model/draw evidence. Diagnostics are deliberately excluded. `target_projection_set_fingerprint`
is recomputed from actual projection records by the shared predictive-contract helper before a stored
value is accepted. The old
`projection_fingerprint` spelling remains a read/constructor compatibility alias only; serialized evidence
uses the explicit target-projection-set name.

`validate_qoi_projection_evidence()` is the strict public read gate for that derived evidence. It accepts
only a real `QoIProjectionEvidence`, verifies the frozen `o36.2.1` contract/adapter/QoI metadata, and
recomputes both authority and evidence fingerprints. The constructor and validator share one payload
helper and one invariant validator. Status is exactly `missing`, `invalid`, `qualified`, or `valid`;
both code fields are unique non-empty-string tuples; only `valid` may have no codes; and only `valid`
has the exact derived `eig_shadow = candidate` scope. Every other status has all three EIG scopes
ineligible. Present source-scope records must retain all three source denials (extra diagnostic fields
are allowed), while valid evidence requires the predictive-semantics, projection-semantics, and
projection records. An invalid upstream source-scope record is omitted from the derived source view so
its attempted elevation cannot propagate. Supplying a stale authority or evidence hash raises rather
than silently blessing altered verdicts, target-presence, or QoI values.

Particle mass is a measure, not a draw count. Equal-weight draws publish `1/n`; explicit predictive draw
weights are validated and normalized by their positive total, exposed read-only in QoI diagnostics, and
used for present/absent mass. A missing linkage produces `projection_join_unknown`; any predictive-basis,
retained-set, semantics, projection-set, model identity/weight, or draw-alignment disagreement produces
`projection_join_mismatch`. Both outcomes publish no joined QoI models and no authenticated particle mass.
The validated fit-selection model-selection fingerprint must equal the QoI evidence's predictive source
identity; absence is an observability gap and disagreement is a scientific join mismatch.

Eligibility validates the complete QoI envelope before consuming any of its codes or join identities.
An unauthenticated or old-revision object is logged and represented only as
`projection_record_invalid`; its supplied reason codes and source-model-selection fingerprint are ignored,
and the semantic eligibility input records no QoI fingerprint. Slice 3 retains its own defensive
re-validation after the eligibility gate.

Eligibility's persisted `input_fingerprint` is semantic-only: authenticated action/Q, target/master/tracker
predicate state, checked selection comparison state, derived QoI identity, standard shadow-selection (or
the neutral-skip identity), and scan-design inventory/context/recovery identities. It deliberately excludes
paths, diagnostics, timing, and shadow runtime increments. The census delegates every operational predicate
to this shared evaluator, uses `FitOutcome.q` before legacy provenance aliases for its target Q, treats past
member acquisitions as diagnostics only, and aggregates particle masses only from joined QoI evidence.

The scorer core remains independent of omega0/DHO implementation details. The adapter is the sole place
where the current target-energy producer layout is known.

### Homogeneous predictive-scenario design measure (Slice 3)

`isar.eig.build_scan_window_design_measure()` is the pure, non-persisting constructor for
`isar.scan_window_design_measure/o36.3`. It recomputes Slice 2 eligibility over the exact
`EligibilityInput` it consumes, passes the accompanying `isar.master_predictive/2` through the shared
strict validator, and authenticates the public QoI-model view against that artifact. An ineligible,
unassessable, malformed, stale, or mismatched source returns an immutable unavailable measure with
separate scientific, observability, and construction codes and no partial model or scenario output.

A **source draw** is one producer-owned parameter draw. A **predictive scenario** joins one retained
fit model, one identified source draw, one authenticated target-QoI value, and one statistical weight.
The normalized set of those scenarios is the **design ensemble**; the complete **design measure** also
records semantics, provenance, exclusions, mass scales, authority, and fingerprints. New `o36.3`
fields use this vocabulary rather than statistical “particle,” which is ambiguous in a scattering
codebase. Slice 2's older `particle_source_masses` diagnostic is read only by a private compatibility
translator and compared with independently recomputed values at its published 12-decimal precision;
that spelling never enters `o36.3` serialization.

The design ensemble is explicitly a frequentist, Akaike-weighted parametric-bootstrap empirical
measure, not a Bayesian model average. Slice 3 inspects every retained model's declared draw basis.
Slice 2 remains authoritative for material nested-sampling, Laplace/HESSE, unknown-basis, retention,
and dropped-model vetoes. After an eligible result, Slice 3 defensively requires those source masses
to agree; an impossible disagreement is `design_measure_input_mismatch`. Permitted sub-material
non-bootstrap or unknown-basis retained models are omitted and reported, and the remaining published
bootstrap weights are transparently renormalized. Current predictive semantics authenticate
equal-weight empirical draws only; an otherwise structurally valid explicit-weight artifact is held
as `source_draw_weighting_unsupported` until an additive source-semantics revision can describe it.
The measure also authenticates source and retained draw counts against the actual aligned draw arrays,
keeps those counts distinct in each model summary, and fingerprints the producer's model/draw pruning
limits and completeness totals as a separate source-completeness diagnostic.

Within each retained bootstrap model, preconditioning mass is its renormalized model weight times its
normalized source-draw weight. The final ensemble conditions globally on authenticated target-QoI
availability: only target-present draws with finite QoI enter, and their mass normalizes to one.
Target-unavailable mass is diagnostic only—there is no target-presence latent state, threshold, score,
or authority. If no positive conditional mass remains, construction returns
`conditional_target_qoi_ensemble_empty` without models or scenarios. Source-AIC, retained-bootstrap,
and conditional-design scales remain structurally separate in the audit record.

The strict read gate independently rechecks those landed mass semantics rather than treating a valid
fingerprint as sufficient: retained bootstrap model weights sum to one before target conditioning;
equal-weight draws carry `1 / retained_source_draw_count`; each preconditioning scenario mass is its
retained model weight times that source-draw weight; and every conditional scenario weight is its
preconditioning mass divided by the globally included preconditioning mass. All four relations use the
same recorded `1e-12` normalization tolerances as construction.

Every consumed model requires aligned, unique, non-empty string draw IDs. The
`source_draw_identity_fingerprint` is the first content-addressed binding of producer-ordered draw IDs
into the EIG chain; `/2`'s predictive-basis fingerprint does not cover those IDs. A scenario ID is the
derived join identity of predictive-basis fingerprint, model label, and source-draw ID, so changing only
the QoI leaves it stable. The enclosing design-measure fingerprint also binds selection, projection,
QoI and eligibility identities, draw-ID binding, source bases and weights, exclusions, QoI values,
conditional weights, materiality constants, and measure semantics. All fingerprints use strict canonical
finite JSON. Model labels, global `(model_label, source_draw_id)` join keys, and resulting scenario IDs
must each be unique; any collision is `scenario_identity_mismatch` and publishes no partial ensemble.

An available measure grants only `eig_shadow = candidate`; `eig_authoritative` and
`acquisition_selection` remain `ineligible`. It does not generate or score a window, touch an inventory,
write campaign state, or produce a request. Retained Slice 1 campaigns contain legacy predictive `/1`
rather than current authenticated `/2` plus projection evidence, so they do not demonstrate observed
campaign availability of this measure.

Slice 4 preserves the source join across serialization by matching recomputed predictive-basis and
source-draw-identity fingerprints before scoring expected counts. Requiring the same Python object
instance is neither necessary nor a valid persistence contract. A future additive `/2` revision may
instead include draw IDs in its own fingerprint, but Slice 3 does not change that source contract.

The first comparison is local to one selected Q and fit-model context. Information transfer across Q
or energy, a phenomenon registry, continuum support, magnon support, and a general model refactor
are post-SVP work.

### Pure full-vector Poisson information scorer (Slice 4)

`isar.eig.score_scan_window_candidates()` is the pure, non-persisting scorer for
`isar.scan_window_eig_score_set/o36.4`, under
`isar.full_vector_poisson_mixture_scorer/o36.4`. It accepts one strictly re-authenticated
`ScanWindowDesignMeasure`, an explicit `isar.scalar_qoi_discretization/o36.4`, and caller-supplied
`PredictiveDesignEvaluation` values. It does not call the evaluator, read campaign or instrument
state, construct a candidate, rank a result, write an inventory, or issue a request. The new
`validate_scan_window_design_measure()` read gate accepts only the real frozen `o36.3` class and
reproduces its complete semantic fingerprint. Constructor, validator, and scorer share the same
source-draw and scenario-identity functions, so their payload recipes cannot drift while merely
retaining the same domain string.

The evaluation join first reproduces the complete producer-ordered model/source-draw identity. Every
conditional design scenario then joins exactly once by model label and source-draw ID, with matching
identity label, predictive weight, draw basis, draw weighting, and scenario ID. Source-authenticated
rows outside the conditional ensemble are legitimate only when Slice 3 excluded their model or
conditioned that retained-model draw out because its target QoI was unavailable. Their join keys and
category enter the scorer-evaluation fingerprint, but their expected-count values do not: those values
cannot affect the score. Changing an ignored row's source identity still changes the complete source
fingerprint. The scorer authenticates these identities, joins, the exact grid/exposure, and the count
vectors it consumes; it does **not** rederive whether those count vectors are numerically correct for
the claimed physical model. That remains `evaluate_master_predictive()`'s responsibility behind the
frozen `/2` seam.

The discretization declares a finite scalar QoI origin, positive bin width, and at least one unique
offset in `[0, bin_width)`. For offset (a), scenario value (q) belongs to
`floor((q - origin - a) / bin_width)`. For simulated observation (i), the conditional mixture is
the bin containing the **selected scenario's** QoI under that offset; no bin is inferred from the
future counts. Partition contributions are averaged equally. A partition containing every scenario
in one bin is exactly zero by definition, avoiding a subtractive log-sum-exp roundoff. No distribution-
derived quantile, omega0 rule, campaign objective, or default physical tolerance enters the scorer.
Origin and offsets canonicalize either spelling of signed zero to `+0.0` before validation, ordering,
serialization, and fingerprinting, so mathematically identical policies have one identity.

For candidate (c), scenario (s), and its complete executed count vector (Y_c), the observation
model is conditionally independent Poisson counts at every point. The scorer evaluates

\[
\log p_c(Y_c\mid Q=q_s)-\log p_c(Y_c)
\]

with the full scenario-by-point expected-count matrix and stable log-sum-exp mixtures. It never reduces
the observation to total counts, centroid, a fitted peak, or another summary. The common
`-log(y_i!)` term cancels. Exact zero-rate rules are `lambda=0,y=0 -> 0` and
`lambda=0,y>0 -> -infinity`; simulation likewise returns zero directly for a zero rate rather than
delegating the degenerate distribution to SciPy. Positive rates use `scipy.stats.poisson.ppf`, whose
result must be finite, non-negative, and exactly integer-valued as a float; validated values remain in
the floating observation matrix rather than making a no-op trip through `int64`. PPF exceptions or
invalid returned counts are `poisson_simulation_failed`. A non-finite result from the later mixture
calculation is instead `information_evaluation_failed`, keeping simulation and information-evaluation
faults attributable to their actual numerical subsystem. The expensive likelihood path is vectorized
over simulation, scenario, and point matrices.

Common random numbers use `isar.sha256_energy_keyed_poisson_crn/1`. Domain-separated SHA-256 payloads
bind the design-measure fingerprint, signed seed, and simulation index; the count stream also binds the
selected scenario ID and exact canonical energy identity, never candidate ID. Signed zero is normalized
before `float.hex()`, so `-0.0` and `+0.0` are one executed energy. The stream retains the high 52 bits
of the first hash word and maps them to `(m + 0.5) / 2**52`, exactly spanning
`[2**-53, 1 - 2**-53]` in binary64 without an endpoint repair. Candidate order and later candidate
addition therefore leave existing scenario selections, shared-energy streams, candidate results,
candidate fingerprints, and existing pairwise records unchanged; the enlarged score-set collection
and score-set fingerprint change.

Each simulation averages its configured offset contributions, and contiguous equal-size blocks produce
the candidate estimate and uncertainty. Standard errors use the `ddof=1` sample standard deviation of
block means divided by the square root of the block count; two blocks are legal but weak evidence.
Absolute information and information per executed point are reported in nats. Negative Monte Carlo
estimates are retained rather than clamped. Pairwise deltas are always candidate B minus candidate A,
and their errors come from shared blockwise differences on the absolute and per-point scales, never from
independently combined errors.

Candidate-score fingerprints independently bind scorer, likelihood, CRN, design-measure,
discretization, evaluation, seed, simulation/block, numerical-backend, output, and authority semantics.
Pairwise fingerprints additionally bind both candidate fingerprints and their paired block differences;
neither depends on unrelated candidates. NumPy and SciPy versions enter these fingerprints deliberately:
numerically different environments must not publish indistinguishable evidence. Scores sharing a
design-measure and discretization fingerprint describe the same information quantity, but reported
paired uncertainty is comparable only within one common execution basis (revisions, seed, simulation
count, block structure, and numerical backend). Slice 5 must therefore compare candidates from one
score set rather than joining independent estimates after the fact.

Diagnostic prose remains visible in candidate and score-set serialization but is excluded from both
candidate-score identity and the enclosing score-set identity, including diagnostics nested inside
candidate records. Structured status, refusal/source-reason codes, authority, and numerical evidence
remain fingerprinted. The enclosing recipe was corrected before any score set was persisted or consumed,
so existing in-memory pre-closure score-set hashes are deliberately reminted without a contract bump.

An individually malformed evaluation becomes an unavailable candidate record without a partial number;
valid candidates in the same call continue. Global evidence/configuration faults return an unavailable
score set. Refusal provenance records a design-measure, source-draw, or discretization fingerprint only
after the corresponding object has passed its strict read gate; a plausible-looking hash supplied by the
object that failed validation is represented as `null`. Complete and partial sets carry
`eig_shadow = diagnostic`; `eig_authoritative` and
`acquisition_selection` remain `ineligible`. Within the EIG chain, `candidate` means authenticated input
eligible for later shadow computation, `diagnostic` means computed shadow evidence with no selection
power, and `ineligible` grants neither. An unavailable set has all three scopes ineligible. No Slice 4
record contains a winner, recommendation, authority threshold, selected candidate, or acquisition act.

### Controlled offline counterfactual (Slice 5.1)

Slice 5 is an offline transformation from a completed campaign in `off` mode to a diagnostic
`shadow` counterfactual. It neither replays the campaign nor changes any persisted acquisition:
`isar.eig_offline_corpus_index/o36.5.1` discovers completed decision snapshots, strict loaders open
the artifacts they name, and `isar.eig_offline_comparison_report/o36.5.1` scores the frozen eligible
pair `legacy_uniform/1` and `target_core/1` at equal per-point exposure. The pure
`isar.offline_shadow_context_transform/o36.5.1` is I/O-free; corpus indexing, strict loading,
stale-artifact rejection, and report serialization are tools-layer adapters around it. No
tools-layer result is a new production decision.

Discovery is complete only when its decision snapshots are coherent: a snapshot must name one
completed decision context whose inventory, action identity, predictive/QoI evidence, and policy
provenance join under their recorded fingerprints. The generic core consumes an opaque, versioned
`campaign_configuration` applicability identity. The current tools-layer producer,
`isar.current_objective_comparison_scope/1`, fingerprints only the narrow validated objective payload:
objective schema, ordered Q path, tolerance, target identity/selector/anchor, target and
identity-search domains, target temperature, hypotheses, target hypothesis, and hypothesis
fingerprint. It excludes name, metadata, budget, tier, runtime/source provenance, and all steering.
`case_identity` is the population identity over reviewed campaign, master, detector, cohort, and
evidence tier; `decision_snapshot_identity` separately records the scientific semantic content.
Case-scoped policies target the population identity. Neither identity claims a global discovery or
distinction scale. Variants remain separate diagnostics: their fingerprints and refusal reasons are
never collapsed into one inferred scientific category.

The distinction-policy set `isar.qoi_distinction_policy_set/o36.5` and each
`isar.qoi_distinction_policy/o36.5` are hand-authored, exactly scoped scientific intent. A policy
matches only the policy identity and completed decision context it declares, and each retained or
refused record carries that provenance. It is not a learned classification, an implicit campaign
default, or a general model of physical phenomena. The sole currently supported policy kind,
`isar.scalar_absolute_partitions/o36.5`, is the first compiler for the conditional target-QoI
calculation, not a universal phenomenon model; its identity stays explicit in the score set.

The TOML authoring loader computes omitted variant, policy, and policy-set fingerprints, but every
supplied fingerprint must authenticate exactly. JSON artifact readers remain strict. Supported
policy parameters compile while the file is loaded, so malformed scientific configuration fails
before report writing. Unknown versioned policy kinds remain authenticated and are reported as
`qoi_policy_unsupported` without evaluating candidates.

Loaders fail closed. Missing, malformed, stale, mismatched, or mutually incoherent artifacts are
reported as discovery/loader diagnostics and cannot enter the retained corpus. The public refusal
surface includes `nonterminal_or_unclassified`, `corpus_index_stale`, `qoi_policy_missing`,
`qoi_policy_ambiguous`, `qoi_policy_unsupported`, `offline_scientific_case_missing`,
`offline_scientific_case_mismatch`, `score_set_incomplete`, and `comparison_unavailable`. Missing or
unauthenticated source evidence is `unassessable`; an authenticated eligible case whose
counterfactual construction fails is `comparison_unavailable`. The retained corpus may legitimately contain
zero ready comparisons: a zero-ready result is evidence that no completed state met the narrow
contract, not a request to relax it. The retained evidence basis presently covers 12 reviewed roots,
233 master rows, and 230 pooled outcomes, with zero authenticated predictive `/2` plus projection
evidence. The repaired `o36.5.1` runner classified those 230 comparison snapshots as 64
`nonterminal_or_unclassified`, 74 `scientifically_ineligible`, 92 `unassessable`, zero
`comparison_unavailable`, and zero `ready`. Its corpus fingerprint is
`b234c33ddfdc1389d367c348f247bcf974808e5192271bcc39ddfa40a013de95`; the corresponding
no-policy score report completed with zero scored results and fingerprint
`ebfd2056608bf3ae90a5a0339516489813c0ba5f82e4e0e6edfef1a698133888`. The dated Slice 5.1
historical-evidence record under `docs/campaigns/` indexes the retained artifacts. These are observed
availability counts, not an observed successful comparison, and they do not authorize relaxing the
contract.

For every ready record the transform authenticates and rehydrates the inventory's exact generator
configuration, applies it to both regenerations, and verifies the candidate pair, canonical executed
grids, and equal per-point exposure. Discovery performs no arbitrary-grid evaluation. After stale
corpus and policy refusals clear, one lazy scoring adapter evaluates legacy and target-core exactly
once and reuses those two evaluations across every policy variant. The
comparison is `target_core_minus_legacy` (candidate B minus candidate A) on the absolute and
per-executed-point target-QoI scales; a candidate mismatch or exposure mismatch is a diagnostic
refusal, never a substituted control or normalized estimate. In particular,
`persisted_legacy_candidate_mismatch`, `shadow_legacy_candidate_mismatch`,
`candidate_pair_degenerate`, `candidate_family_missing`, and `candidate_exposure_mismatch` remain
separate diagnoses. Each result embeds `isar.offline_score_evidence/o36.5.1`: exact policy and
partition identities, candidate-pair and candidate-score identities, absolute and per-point scores,
the original Slice 4 pair orientation, the signed Slice 5.1 deltas, and all score-set/candidate/source
refusal codes. `isar.eig_offline_scoring_config/o36.5.1` binds the simulation configuration.

Slice 5 grants only `eig_shadow = diagnostic`; `eig_authoritative` and
`acquisition_selection` remain ineligible. It does not write an inventory, produce a recommendation,
choose a request, change `off` mode, or grant authority to the `shadow` mode. Slice 6 is the separately
gated live scored-shadow step described by O38; it preserves those same authority denials.

### Slice 0–10 execution sequence

1. **Slice 0 — decisions and plan.** Record S8; record restricted EIG authority; amend the active
   statistical audit; update active plans; disambiguate SVP terminology; make no runtime changes.
2. **Slice 1 — completed-campaign eligibility census.** Inspect full local campaign artifacts; count
   every proposed eligibility rejection; distinguish shadow deadline skips from real shadow failures;
   make no behavior changes.
3. **Slice 2 — freeze eligibility and generic QoI adapter.** Define stable eligibility reason codes
   and a generic QoI view over `target_projection/2`; `o36.2.1` authenticates verdict semantics and
   fail-closed joins without breaking either source schema.
4. **Slice 3 — EIG-local homogeneous design measure (implemented).** Build the immutable
   Akaike-weighted parametric-bootstrap predictive-scenario measure, inspect actual per-model source
   basis, condition on authenticated target-QoI availability, and make no production selection switch.
5. **Slice 4 — pure full-vector Poisson scorer (implemented).** Use conditional target-QoI
   information, the full count vector, common random numbers, paired uncertainty, and information per
   executed point, with no candidate construction or selection authority.
6. **Slice 5.1 — repaired offline legacy versus target_core scoring (implemented).** Transform
   coherent completed `off` decisions into controlled `shadow` counterfactual diagnostics; use the
   persisted generator configuration, evaluate lazily after policy resolution, retain complete score
   evidence, require the exact pair and equal per-point exposure, and grant no inventory, execution,
   or selection authority.
7. **Slice 6 — scored shadow inventories (implemented).** Authenticate the exact selected completed
   source master; score `legacy_uniform` and `target_core` once per candidate and reuse those
   evaluations across bounded policy variants; persist diagnostic `/3` evidence; execute legacy in
   every outcome. Existing non-EIG behavior remains responsible outside eligibility.
8. **Slice 7 — exact realized-request rescoring (implemented).** Score or reuse evidence for the
   adapter-exact grid and equal per-point exposure relevant at submission; retain candidate →
   realization → submission provenance and verify the returned TAVI grid.
9. **Slice 8 — bounded EIG-3 shadow gate (owner-accepted).** The tools-only `o36.8.2` V3 evaluator,
   frozen six-case/fault manifest, conditional eligibility evidence rules, self-contained `/3`
   inventory plus realized-comparison retention, deterministic-TAVI analysis, exact production
   comparison-to-execution join, and byte-reproducible passed gate are implemented and accepted
   under O41. The positive envelope contains one clean case. The gate cannot select or submit a
   request.
10. **Slice 9 — opt-in authoritative selector (implemented; O42 release review retired by H1,
    default-on once the SCENE world suite exercises it, at SCENE-4).**
    `target_core` may win on authenticated, resolved EIG-per-point superiority; unresolved or
    inferior eligible comparisons retain the eligible control; ineligible states return to the
    existing non-EIG design/recovery path. The bounded lifecycle, durable decision binding,
    predicate telemetry, and lineage-scoped grid-mismatch invalidation above are implemented.
11. **Slice 10 — deterministic TAVI EIG-4 SVP.** Execute at least one narrower EIG-selected scan;
    show exact scored/executed grid agreement, fewer executed points, normal fit/campaign rejoining,
    and return to the existing non-EIG path on an ineligible state.

### Serial review workflow

Implement one slice, run local targeted verification, commit, write a short exact-anchor handoff,
review the landing, then begin the next slice. Slices proceed serially. Parallel work is allowed
only when it does not touch the active slice's contracts or implementation surfaces. GitHub CI
certification is not part of this workflow. Slice 0 ends after this documentation landing.

## Predictive artifact

`isar.master_predictive/2` is the only published predictive artifact; `/1` emission is deleted and
every reader requires `/2` (ADR-006/013, DECISIONS O33). **`/2` is physical only** (ADR-019): it
carries what the fit knows, and no statement about which mode is the campaign's target. It retains
the model weights, labels, draw basis, parameter names/draws, model manifests, ω draws, seed, and
uncertainty diagnostics `/1` carried, adds per-draw `mode_energies` — the full physical menu of
fitted mode energies a later projection selects from without a refit — and adds the information
needed to evaluate those same draws away from the fitted grid:

- `energy_meV`: the ordered fitted master grid on which the stored expected counts were made;
- `evaluation_context`: `center_meV`, Gaussian/Lorentzian resolution FWHM, temperature, and optional
  quasielastic FWHM;
- `exposure_basis`: `unit` (`monitor_counts`), `request_unit` (`number_neutrons`),
  `request_to_exposure_factor` (`1.0`), `fit_reference_exposure`, `reference_incremental_exposure`,
  `n_members`, `per_energy_monitor` (ΣM per bin, aligned to `energy_meV`), `stored_exposure_ratio`
  (the per-bin multiplier the stored matrices were actually made with),
  `scalar_exposure_eligible`, `exposure_homogeneous`, `full_member_support`,
  `exposure_ratio_span`, and `kept_point_count`.

`fit_reference_exposure` is the amplitude basis of the pooled fitted model: the value
`normalized_exposure` returned for that exact fit (`median(ΣM)` over the fit's kept mask). It must be
persisted from the fit, never recomputed — recomputing it from a different kept set can move it by
tens of percent. For requested per-point exposure \(E\), evaluation multiplies the direct model mean
by `E × request_to_exposure_factor / fit_reference_exposure`. The factor converts the **request
unit** into the **exposure unit** and is now applied rather than merely declared: it is `1.0` on
TAVI, so omitting it was invisible there and would have silently mis-scaled every score on the first
instrument whose monitor is not its request. This candidate-evaluation ratio is valid on every pool,
including heterogeneous ones, and is not gated on `exposure_homogeneous`.

The stored expected counts keep `/1`'s per-member incremental meaning — "the counts one further
member-equivalent scan would see" — so the multiplier they were made with is the per-bin
`stored_exposure_ratio_i = ΣM_i / (n_members · fit_reference_exposure)`, and that vector is
published rather than left to be re-derived. The fitted grid's self-consistency is therefore
checked through the published ratio against the producer's own stored matrices. The scalar
`reference_incremental_exposure = fit_reference_exposure / n_members` is the **parity point** of
that vector: it reproduces the same grid **iff `exposure_homogeneous`** is true. It is a mean of
per-member exposure, not any one member's request, and that diagnostic identity is its only role.

`scalar_exposure_eligible` (equivalently `exposure_homogeneous`, `max(ΣM_kept)/min(ΣM_kept) - 1 <=
1e-12`) is published as a declaration and diagnostic. It gates only the scalar fitted-grid parity
assertion described above — it does **not** gate candidate (forward) evaluation, which uses the
scalar ratio on every pool regardless of support profile.

`full_member_support` is **nullable**, not a boolean: it is `true` for a single-member pool, which
by construction supports every kept bin, and `null` otherwise. Per-bin member support is not
carried on `MergedSpectrum`, so it is reported only where it is known rather than guessed, and the
operative eligibility predicate is the ΣM profile itself, which separates the partial-support cases
as well. A `null` here is an absence of knowledge, never a `false`.

A master fitted on the legacy uniform path (no usable monitor; the pooled merge has no time
fallback) still publishes a `/2` artifact — with null `fit_reference_exposure`,
`reference_incremental_exposure`, `per_energy_monitor` and `full_member_support`, and
`scalar_exposure_eligible: false`. It is the arbitrary-grid evaluator, not the producer, that
refuses such a master, with `missing_exposure_basis`; that code stays reserved for this genuine
no-monitor case.

`/1` is never guessed into `/2`: it lacks the original grid, evaluation context, and explicit
exposure basis. A leftover `/1` side artifact refuses where it is read — the drive loop logs
`predictive_artifact_unsupported` with the refusal's reason code and takes its ordinary
no-predictive path, and the workspace facade raises rather than opening it — but it never blocks
opening a workspace or aborts a campaign.

### The contract revision constant

`PREDICTIVE_CONTRACT` (`isar/predictive.py`, currently
`isar.master_predictive/2+physical_only.1`) is the schema label **plus** the revision of the payload
shape carried under it. It is defined once and re-exported — `isar.session.certification` and
`isar.session.driver` bind to it, never re-declare it — and it joins the master-fit cache
fingerprint. Bumping it therefore makes every cached master regenerate its predictive artifact under
the new shape without refitting the science, which is how the ADR-019 reshape reaches existing
workspaces as a clean break rather than a migration.

## Target projection — a separate, authority-bound artifact

`/2` used to carry a target projection resolved from the fit's own local best mode. That is the
defect ADR-019 repairs: the fit's local best can disagree with the target the campaign actually
stands behind once branch triage and the connected tracker have run, so a scored design could aim at
an energy the campaign no longer believes in.

`isar.target_projection/2` is therefore a separate artifact, resolved **after** triage and the
tracker, one per pooled master. It projects the QoI `omega0` under
`isar.omega0_authoritative_mode/2`: for each draw it selects the mode nearest the **authoritative**
reference energy — the post-triage, post-tracker scalar, never the fit's local ω₀ — and publishes
`target_present_by_draw` and `target_qoi_draws` per retained model.

It is **bound to the authority basis that produced it**. The `authority` block records
`action_role`, `master_revision`, `track_revision`, `track_state`, `candidate_id`,
`candidate_state`, `selected_candidate_index`, `calibration_fingerprint`, and `policy_fingerprint`,
plus the `predictive_basis_fingerprint` of the `/2` it projects. A consumer whose current state
differs on any of these holds a stale projection and must re-resolve it rather than score against
it. `action_role` was published *beside* the block rather than inside it, which made that staleness
test blind to a role change — the same master re-resolved as a rival probe compared equal to its
master projection and the stored one was reused. It is inside the block now; there is no `/1`
compatibility for the old placement.

### Selecting a mode is resolution-relative, and bounded

Nearest-energy selection with no distance gate is unconditional: it returns the closest mode however
far away it is. An authoritative ω₀ of 99.0 meV against modes at {2.0, 5.0} was therefore published
as `complete: true` naming the 5.0 mode — a confident QoI about a target the model does not contain.
Two constants close that, both derived from the artifact's own `evaluation_context` Gaussian
resolution FWHM and both recorded in the projection's `mode_selection` block so a verdict can be
re-derived from the record rather than from whatever the constants say today:

- **`MODE_SUPPORT_FWHM_MULTIPLE = 3.0`** — the reference must lie within 3 × FWHM of the selected
  mode. That is the same "outside the resolution function" line the fitting side uses to call two
  features separate: inside it the reference and the mode are one feature the instrument cannot
  split, outside it they are different physics.
- **`MODE_AMBIGUITY_FWHM_FRACTION = 1e-6`** (floored at the absolute `MODE_AMBIGUITY_TOLERANCE_MEV`
  `= 1e-9`) — the equidistance tolerance. An absolute 1e-9 meV decided "these two modes are the same
  distance away" at a precision no measurement has, so a genuine numerical tie at a 0.4 meV
  resolution was resolved by a coin flip.

The artifact is **always published, never raised**: when a projection cannot be resolved it carries
`complete: false` and a structured `reason`, and publishes no target draws at all — a
partially-populated projection would invite a consumer to score the half it happens to like. The
reasons are stable codes, routed on rather than read:

- `descriptor_only` — the descriptor localised real signal the phonon model cannot fit, so by
  machine contract there is no ω₀ measurement to project onto;
- `withheld_delivery` — triage or the tracker declined to deliver a target (a published withholding
  reason such as `auxiliary_only_selection` is republished verbatim);
- `ambiguous_track` — a track exists but its candidate is neither `confirmed` nor `individual`, or
  no candidate is selected;
- `no_reference_target_energy` — no finite authoritative energy;
- `reference_beyond_mode_support` — the authoritative energy is further than
  `MODE_SUPPORT_FWHM_MULTIPLE × gaussian_fwhm_meV` from the nearest mode a draw offers, so no mode
  here is the target;
- `no_resolution_basis_for_mode_support` — the artifact declares no usable `gaussian_fwhm_meV`, so
  the support distance has no scale to be measured in. This fails **closed**: a published `/2`
  always carries a positive one (the strict validator refuses otherwise), so the state means the
  basis is invalid, and disabling the gate would restore the unconditional nearest-mode rule for
  exactly the artifacts least entitled to it;
- `no_unique_mode` — two modes are equidistant from the authoritative energy within the
  resolution-relative ambiguity tolerance, so a nearest-energy rule would publish a coin flip;
- `no_retained_model_carries_the_target_mode` — no retained model has the target mode on any draw.

Support outranks ambiguity when a draw trips both: "the reference is nowhere near any mode this draw
offers" is the more fundamental failure, and reporting the tie would hide it.

Where the predictive side produced nothing at all for an otherwise-successful master, the driver
publishes `target_projection_construction_failed` instead of an absent record.

Both builders live in `isar/session/predictive_artifact.py`; the driver no longer constructs either
inline.

## One strict read gate (ADR-020)

`validate_master_predictive_artifact` in `isar/predictive.py` is the **single** strict validator, and
all three readers run it: the workspace facade, the arbitrary-grid evaluator, and the drive loop.
The loop previously checked only the schema label, so a malformed or stale-fingerprinted basis
reached the loop that the other two readers would have refused — the same artifact was usable or not
depending on who opened it.

The gate validates the exposure basis field-by-field, the evaluation context, the per-model
structure and draw shapes, the peak bounds, and then **recomputes `predictive_basis_fingerprint`
from the artifact's own content** and refuses a mismatch with
`predictive_basis_fingerprint_mismatch`. The fingerprint hashes exactly the scientific and
evaluation content a score depends on — models, draws, grid, context, exposure basis, the pruning
constants, the evaluator version, and the negative-prediction tolerance — and deliberately excludes
diagnostics, timings, identity stamps, and the ensemble seed, so two bases that predict the same
counts from the same draws fingerprint the same. Since ADR-019 the target projection is *not* hashed
here; the per-draw `mode_energies` it selects from are.

Refusals raise `PredictiveArtifactError` carrying a stable `reason_code`; structural faults with no
more specific code report `predictive_artifact_invalid`.

## Pure evaluator interface

The Qt-free interface is:

```python
evaluate_master_predictive(
    artifact,
    energy_meV,
    exposure_per_point,
) -> PredictiveDesignEvaluation
```

An available result has schema `isar.predictive_design_evaluation/1`, echoes the requested ordered
grid and exposure, and contains each retained model's label, normalized weight, draw basis, ω draws,
and an `expected_counts[draw][point]` matrix. Values are finite and non-negative, model/draw order
follows the artifact, and the evaluator is deterministic and pure — it neither reads the workspace
nor refits.

The evaluator reconstructs every registered component from `model_manifest`, substitutes each
parameter draw **by name**, evaluates the component functions directly at every requested energy,
sums them, and applies the scalar exposure ratio `exposure_per_point / fit_reference_exposure`. It
never interpolates stored expected counts. The producer and the evaluator share one evaluation path
(`isar.analyze.model_manifest`), so they agree on component structure by construction rather than
by parallel code.

Expected unsupported inputs return `available=false` with a stable `reason_code` and message. As
shipped:

- `artifact_schema_unsupported` — a non-`/2` schema (including `/1`), or a structural fault in the
  artifact or a model manifest: a missing evaluation context, no retained models, an unusable model
  weight, or a malformed manifest;
- `unknown_component_kind` — a manifest naming a component kind the evaluator does not register;
- `malformed_draw_shape` — a parameter-name/draw mapping fault, including a model with no draws;
- `predictive_basis_incomplete` — the artifact's own `completeness.predictive_complete_for_eig` is
  not `true` (ADR-014);
- `missing_exposure_basis` — no finite positive `fit_reference_exposure`, i.e. the genuine
  no-monitor master described above;
- `invalid_energy_grid`, `invalid_requested_exposure`, `nonfinite_prediction` — the requested grid,
  the requested exposure, or a produced value is unusable;
- `negative_prediction` — a produced expected count below `-NEGATIVE_PREDICTION_EPS` (`1e-9`) **per
  unit exposure**. A small negative value is float noise on a genuinely zero mean and clamps to
  zero; anything beyond the tolerance is a real negative rate and refuses rather than being scored
  as if physical.

  The tolerance is stated at unit exposure and the threshold is multiplied by the exposure ratio
  actually applied (`NEGATIVE_PREDICTION_EPS_RULE = "exposure_scaled"`), which makes the verdict a
  property of the **model** rather than of the request. Applied absolutely to the already-scaled
  value it was not: expected counts scale linearly with exposure, so one broken model refused at
  `reference × 1e6` and passed at `reference / 1e6`, and a campaign could make an impossible
  prediction disappear by asking for fewer neutrons. Both the value and the rule are hashed into
  `predictive_basis_fingerprint` alongside `EVALUATOR_VERSION` (now
  `isar.model_manifest_evaluator/2`), so a score computed under one cannot be mistaken for one
  computed under another;
- `predictive_basis_fingerprint_mismatch` and `predictive_artifact_invalid` — from the shared read
  gate above.

The evaluator owns prediction only. It must not read TAVI presets, generated background truth,
instrument rates, or request-time rate assumptions; fit or mutate scientific state; calculate EIG or
utility; generate candidates; or select a window.

## Realized scoring and future selection audit

The EIG phase adds scoring and selection behind the existing scan-design interface. Multiple
uniform `ScanDesignCandidate`s already share one frozen context. A separate scorer consumes only candidate
grid/exposure plus `PredictiveDesignEvaluation`; a selector records score components, cost, refusal
reason, and the selected candidate in the same content-addressed inventory. The outer action/Q ranking,
instrument finalization, validation, and existing request fingerprint remain separate.

The selected candidate remains immutable when instrument finalization or target-support adjustment
changes the request. Slice 7 constructs an isolated `legacy_uniform/1`/`target_core/1` realized pair
after each such mutation, gives both candidates the finalized legacy per-point exposure, and either
references the authenticated Slice 6 score when candidate identities, ordered executed grids, and
exposure are identical or scores the complete changed pair together. The separate five-second Slice 6
and five-second Slice 7 allowances can therefore consume roughly ten seconds in the worst case. The
Slice 7 cumulative per-proposal allowance is enforced in memory; cooperative deadline expiry publishes
per-variant unavailability without changing the exact selected legacy request.

Only `isar.scan_design_realized_comparison/o36.7` is independently persisted, under
`report/scan-design-realizations/<sha256>.json`. Its nested realized requests, pair, variants, and
seed bind the immutable `/3` inventory, source shadow evidence, policy/design identities, exact
rendered commands, adapter-canonical ordered grids, equal exposure, and diagnostic-only authority.
A rescored variant nests the existing `isar.scan_window_score_set/o36.5` internals; `/o36.7` does not
redefine that numerical score contract. The physical submission and acquisition provenance carry
seven realized fields: five identities (comparison reference and fingerprint, ordered variant
fingerprints, selected request fingerprint, and selected grid fingerprint), applicability, and
invalidation reason. The original `/3` inventory is never rewritten.

Missed-window recentering or widening retains the parent comparison reference but clears its variant
and selected request/grid claims and records `eig_realization_invalidated_by_recovery`; the recovery
request is not relabeled as either EIG candidate. On completion, the TAVI-returned `scan_values_1`
must normalize to ordered adapter-canonical equality with the selected grid; equality is not raw
floating-point bit equality. `executed_grid_mismatch`, or an unreadable comparison sidecar during
recovery, makes the case ineligible for later authority evidence but does not block ordinary shadow
analysis. Refresh/persistence failure or a failed pre-submit authentication (including its single
forced refresh) removes all five current EIG identities and marks the evidence unavailable while the
ordinary legacy request remains executable.

Each changed realization is also recorded as
`isar.scan_design_realization/1`, chaining the inventory reference, candidate id, and context
fingerprint to the previous and realized request fingerprints plus the exact emitted window/count.
Physical-submission and acquisition-provenance records carry both identities so crash recovery retains
the join without loading the derived inventory as controller state. This `/1` event is additive-open:
readers must ignore unknown additive fields.

Slice 8 evaluates those records only after acquisition. Its primary distinction policy is one
`excitation.energy` absolute-partition variant with 0.5 meV bins and half-bin-shifted offsets
`[0.0, 0.25]`; runtime scoring remains the existing 128-simulation/eight-block configuration. The
frozen engineering rule is target-core-minus-legacy information per executed point with
`delta - 2 * paired_SE > 0`. The multiplier is a pragmatic margin, not a calibrated coverage
statement. A win additionally requires equal per-point exposure and strict containment/reduction
of both the rendered request and adapter-canonical executed support.

Evidence is authenticated by branch. An authenticated ineligible or unassessable eligibility
result returns to the existing non-EIG path and does not need a realized sidecar. An eligible case
must authenticate the exact source `/3` inventory, realized comparison, primary score variant,
submission provenance, applicability, and returned grid; missing or mismatched branch-required
evidence is unavailable for selection. The retained bundle carries both content-addressed files
under their original relative paths so the existing strict loader, rather than a Slice 8 duplicate,
reproduces their join.

The near-elastic authority case is measured at the pre-selection snapshot. Current O36 eligibility
has no generic near-elastic exclusion, so the frozen case remains eligible and must safely retain
legacy under the ordinary structural/margin rule. Recovery invalidation remains separately covered
and must not be misreported as pre-selection near-elastic exclusion. The planned and realized
scoring paths remain duplicated implementation seams held to focused parity tests; consolidating
them is technical debt after Slice 8, not an authority granted to a test file.

The normative fixture `tests/fixtures/eig_predictive_v2.json` contains competing null/one-phonon,
one-mode/two-mode, and acoustic/optic branch hypotheses on original, shifted, narrower, and coarser
grids. Expected matrices live under each retained model, use nonuniform weights and fixed
model/draw/parameter order, and were produced by direct evaluation of the current registered ISAR
components with the declared exposure ratio. It was regenerated for the ADR-019 reshape: it carries
per-draw `mode_energies` and **no** `target_projection`, `target_present_by_draw`, or
`target_qoi_draws`, and every case states a `predictive_basis_fingerprint` that the shared validator
reproduces from the fixture's own content. Prep tests validate the contract and fixture integrity;
the first EIG implementation must reproduce those matrices without importing TAVI.

# Same-action scan-window EIG — design contract

> **Lifecycle:** Active plan
> **Provenance:** hand-written
> **Verified against code revision:** `b543f6f` (2026-08-02)
> **Owner:** @ammerritt-gh
> **Exit criteria:** the restricted authoritative selector exists; Slice 10 completes the deterministic
> TAVI EIG-4 demonstration; exact eligible selection and ineligible non-EIG return behavior are
> verified; accepted behavior is folded into `MEASUREMENT_STEERING.md`; this plan is then removed and
> tombstoned according to policy.

## Status and boundary

**EIG-4 is the approved execution roadmap and current SVP, not implemented runtime authority.**
Alternative-window EIG candidate scoring, scored shadow selection, and authoritative selection are
not implemented. Candidate generation, predictive `/2`, arbitrary-grid evaluation, executed-grid
identity, target projection, Slice 2 eligibility/QoI adaptation, and the Slice 3 homogeneous design
measure are implemented.
Current acquisition ranks scientific actions and Q
positions using expected counts already stored on each master's fitted grid. The
`ScanDesignContext → ScanDesignInventory` seam now generates the bounded family of conventional
uniform windows for the selected action and persists it under `report/scan-design/<sha256>.json`, but
nothing scores or ranks them: the `legacy_uniform/1` candidate is always generated with the exact
historical arithmetic and is always the selected, executed one.

The `scan_window_eig_mode` steering knob (typed on `SteeringPolicy`, declared in the hand-edited
policy card, frozen into `ScanDesignContext`) chooses the regime:

- `off` — only the legacy candidate is designed; `selection_basis = legacy_control_off`.
- `shadow` — the whole family is generated and persisted unscored;
  `selection_basis = shadow_legacy_control_unscored`. The emitted request is identical to `off`.
- `authoritative` — refuses at campaign setup with the stable error code
  `scan_window_eig_authoritative_unavailable`; it is never silently degraded to shadow or off.

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

`isar.scan_design_inventory/2` and `isar.scan_design_candidate/2` are used in **all** modes — there
is no `/1` compatibility. Alongside the generator config the inventory carries the generic provider
identity (`predictive_provider_id`, `predictive_provider_schema`, `generator_id`,
`generator_version`, `objective_projection_schema`, `steering_regime`) so a later, differently
grounded provider can slot in without changing this envelope; `candidate_family` and `generator_id`
are versioned strings, never a closed enum.

Materializing `scan_window_eig_mode` in the resolved steering dict reminted every
`hypothesis_fingerprint` once, and with it every derived scientific-action and content-addressed
audit identity. The *physical* behavior — action class, Q sequence, window, step, count request, and
emitted request — is unchanged in both `off` and `shadow`.

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

The derived Slice 2 evidence revision is `o36.2`; predictive `/2` and target-projection `/2` payloads
remain unchanged. An action is authenticated by reconstructing the persisted `MeasurementIntent` and
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
and projection-semantics identities; and a derived QoI-evidence fingerprint over those identities plus
normalized model/draw evidence. `target_projection_set_fingerprint` is recomputed from actual projection
records by the shared predictive-contract helper before a stored value is accepted. The old
`projection_fingerprint` spelling remains a read/constructor compatibility alias only; serialized evidence
uses the explicit target-projection-set name.

Particle mass is a measure, not a draw count. Equal-weight draws publish `1/n`; explicit predictive draw
weights are validated and normalized by their positive total, exposed read-only in QoI diagnostics, and
used for present/absent mass. A missing linkage produces `projection_join_unknown`; any predictive-basis,
retained-set, semantics, projection-set, model identity/weight, or draw-alignment disagreement produces
`projection_join_mismatch`. Both outcomes publish no joined QoI models and no authenticated particle mass.
The validated fit-selection model-selection fingerprint must equal the QoI evidence's predictive source
identity; absence is an observability gap and disagreement is a scientific join mismatch.

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
as `design_measure_input_mismatch` until an additive source-semantics revision can describe it.
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

Every consumed model requires aligned, unique, non-empty string draw IDs. The
`source_draw_identity_fingerprint` is the first content-addressed binding of producer-ordered draw IDs
into the EIG chain; `/2`'s predictive-basis fingerprint does not cover those IDs. A scenario ID is the
derived join identity of predictive-basis fingerprint, model label, and source-draw ID, so changing only
the QoI leaves it stable. The enclosing design-measure fingerprint also binds selection, projection,
QoI and eligibility identities, draw-ID binding, source bases and weights, exclusions, QoI values,
conditional weights, materiality constants, and measure semantics. All fingerprints use strict canonical
finite JSON.

An available measure grants only `eig_shadow = candidate`; `eig_authoritative` and
`acquisition_selection` remain `ineligible`. It does not generate or score a window, touch an inventory,
write campaign state, or produce a request. Retained Slice 1 campaigns contain legacy predictive `/1`
rather than current authenticated `/2` plus projection evidence, so they do not demonstrate observed
campaign availability of this measure.

Slice 4 must preserve the source join across serialization by matching recomputed predictive-basis and
source-draw-identity fingerprints before expected-count evaluation. Requiring the same Python object
instance is neither necessary nor a valid persistence contract. A future additive `/2` revision may
instead include draw IDs in its own fingerprint, but Slice 3 does not change that source contract.

The first comparison is local to one selected Q and fit-model context. Information transfer across Q
or energy, a phenomenon registry, continuum support, magnon support, and a general model refactor
are post-SVP work.

### Slice 0–10 execution sequence

1. **Slice 0 — decisions and plan.** Record S8; record restricted EIG authority; amend the active
   statistical audit; update active plans; disambiguate SVP terminology; make no runtime changes.
2. **Slice 1 — completed-campaign eligibility census.** Inspect full local campaign artifacts; count
   every proposed eligibility rejection; distinguish shadow deadline skips from real shadow failures;
   make no behavior changes.
3. **Slice 2 — freeze eligibility and generic QoI adapter.** Define stable eligibility reason codes
   and a generic QoI view over `target_projection/2`; `o36.2` strengthens derived identities and
   fail-closed joins without breaking either source schema.
4. **Slice 3 — EIG-local homogeneous design measure (implemented).** Build the immutable
   Akaike-weighted parametric-bootstrap predictive-scenario measure, inspect actual per-model source
   basis, condition on authenticated target-QoI availability, and make no production selection switch.
5. **Slice 4 — pure full-vector Poisson scorer.** Use conditional target-QoI information, the full
   count vector, common random numbers, paired uncertainty, and information per executed point.
6. **Slice 5 — offline legacy versus target_core scoring.** Use completed campaign states at equal
   per-point exposure; grant no inventory or execution authority.
7. **Slice 6 — scored shadow inventories.** Score `legacy_uniform` and `target_core`; execute legacy
   exactly within eligible same-action comparisons. Existing non-EIG behavior remains responsible
   outside eligibility.
8. **Slice 7 — exact realized-request rescoring.** Score the grid and exposure actually submitted;
   retain candidate → realization → submission provenance.
9. **Slice 8 — bounded EIG-3 shadow gate.** Add exact mathematical fixtures, existing predictive
   fixtures, and a small full-pipeline authority-envelope set.
10. **Slice 9 — opt-in authoritative selector.** `target_core` may win on resolved EIG-per-point
    superiority; unresolved eligible comparisons retain the eligible control; ineligible states
    return to the existing non-EIG design/recovery path.
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

## Future selection flow and audit

The EIG phase adds scoring and selection behind the existing scan-design interface. Multiple
uniform `ScanDesignCandidate`s already share one frozen context. A separate scorer consumes only candidate
grid/exposure plus `PredictiveDesignEvaluation`; a selector records score components, cost, refusal
reason, and the selected candidate in the same content-addressed inventory. The outer action/Q ranking,
instrument finalization, validation, and existing request fingerprint remain separate.

The selected candidate remains immutable when instrument finalization, target-support adjustment, or
missed-window recovery changes the request. Each changed realization is recorded as
`isar.scan_design_realization/1`, chaining the inventory reference, candidate id, and context
fingerprint to the previous and realized request fingerprints plus the exact emitted window/count.
Physical-submission and acquisition-provenance records carry both identities so crash recovery retains
the join without loading the derived inventory as controller state.

The normative fixture `tests/fixtures/eig_predictive_v2.json` contains competing null/one-phonon,
one-mode/two-mode, and acoustic/optic branch hypotheses on original, shifted, narrower, and coarser
grids. Expected matrices live under each retained model, use nonuniform weights and fixed
model/draw/parameter order, and were produced by direct evaluation of the current registered ISAR
components with the declared exposure ratio. It was regenerated for the ADR-019 reshape: it carries
per-draw `mode_energies` and **no** `target_projection`, `target_present_by_draw`, or
`target_qoi_draws`, and every case states a `predictive_basis_fingerprint` that the shared validator
reproduces from the fixture's own content. Prep tests validate the contract and fixture integrity;
the first EIG implementation must reproduce those matrices without importing TAVI.

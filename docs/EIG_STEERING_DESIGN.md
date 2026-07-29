# Same-action scan-window EIG — design contract

> **Lifecycle:** Active plan
> **Provenance:** hand-written
> **Verified against code revision:** `6935ec5` (2026-07-29)
> **Owner:** @ammerritt-gh
> **Exit criteria:** `isar.master_predictive/2` is published, the pure arbitrary-grid evaluator passes
> the normative fixtures, multiple same-action scan candidates are ranked and audited, and the shipped
> behavior is folded into `MEASUREMENT_STEERING.md`; this plan is then removed.

## Status and boundary

**Alternative-window EIG scoring and selection are not implemented; candidate generation, the
`isar.master_predictive/2` artifact, and the pure arbitrary-grid evaluator are.**
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
`master_revision`, `track_revision`, `track_state`, `candidate_id`, `candidate_state`,
`selected_candidate_index`, `calibration_fingerprint`, and `policy_fingerprint`, plus the
`predictive_basis_fingerprint` of the `/2` it projects. A consumer whose current state differs on
any of these holds a stale projection and must re-resolve it rather than score against it.

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
- `no_unique_mode` — two modes are equidistant from the authoritative energy within
  `MODE_AMBIGUITY_TOLERANCE_MEV`, so a nearest-energy rule would publish a coin flip as a QoI;
- `no_retained_model_carries_the_target_mode` — no retained model has the target mode on any draw.

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
- `negative_prediction` — a produced expected count below `-NEGATIVE_PREDICTION_EPS` (`1e-9`). A
  small negative value is float noise on a genuinely zero mean and clamps to zero; anything beyond
  the tolerance is a real negative rate and refuses rather than being scored as if physical. The
  tolerance is hashed into `predictive_basis_fingerprint` alongside `EVALUATOR_VERSION`, so a score
  computed under one tolerance cannot be mistaken for one computed under another;
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

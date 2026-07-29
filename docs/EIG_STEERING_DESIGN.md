# Same-action scan-window EIG — design contract

> **Lifecycle:** Active plan
> **Provenance:** hand-written
> **Verified against code revision:** `753dab3` (2026-07-29)
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

The families are `legacy_uniform`, `target_core`, `target_paired` (only when anti-Stokes is
admissible under the existing `positive_only` / detailed-balance gates), `target_low_anchor`,
`target_high_anchor`, `target_plus_rival` (only with a declared rival band, always contiguous with
the core so a canonical target action can never receive a rival-only window), and `broad_recovery`.
Their construction constants live in one frozen, separately versioned
`PhysicalWindowGeneratorConfig` serialized into every inventory. Non-legacy candidates are clipped
to the authorized energy domain, must hold the policy's minimum target support, and are pre-clamped
to the policy/runtime/budget limits already carried by the context. A family that cannot satisfy its
invariants is recorded as a refused candidate with a structured reason code, never silently omitted;
families landing on the same canonical grid and exposure deduplicate to one candidate carrying the
others as alias provenance.

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
every reader requires `/2` (ADR-006/013, DECISIONS O33). It retains the model weights, labels, draw
basis, parameter names/draws, model manifests, ω draws, seed, and uncertainty diagnostics `/1`
carried, and adds the information needed to evaluate those same draws away from the fitted grid:

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
by `E / fit_reference_exposure`; this candidate-evaluation ratio is valid on every pool, including
heterogeneous ones, and is not gated on `exposure_homogeneous`.

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
`predictive_artifact_unsupported` / `artifact_schema_unsupported` and takes its ordinary
no-predictive path, and the workspace facade raises rather than opening it — but it never blocks
opening a workspace or aborts a campaign.

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
  the requested exposure, or a produced value is unusable.

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
components with the declared exposure ratio. Prep tests validate the contract and fixture integrity;
the first EIG implementation must reproduce those matrices without importing TAVI.

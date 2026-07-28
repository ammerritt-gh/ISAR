# Same-action scan-window EIG — design contract

> **Lifecycle:** Active plan
> **Provenance:** hand-written
> **Verified against code revision:** `181a807` (2026-07-28)
> **Owner:** @ammerritt-gh
> **Exit criteria:** `isar.master_predictive/2` is published, the pure arbitrary-grid evaluator passes
> the normative fixtures, multiple same-action scan candidates are ranked and audited, and the shipped
> behavior is folded into `MEASUREMENT_STEERING.md`; this plan is then removed.

## Status and boundary

**Alternative-window EIG is not implemented.** Current acquisition ranks scientific actions and Q
positions using expected counts already stored on each master's fitted grid. The maintenance prep has
only introduced the `ScanDesignContext → ScanDesignInventory` seam: it emits one
`legacy_uniform/1` candidate, selects it as `legacy_only_candidate`, and persists the derived inventory
under `report/scan-design/<sha256>.json`.

The future work described here is narrower than the physics-content work in `STEERING_DESIGN.md`.
It may compare different uniform windows/counts for the **same already-selected scientific action**.
It may not invent branch hypotheses, change evidence routing, fit data, alter QC, or move scientific
selection out of the existing action inventory.

## Future predictive artifact

`isar.master_predictive/2` will be additive to `/1`. It retains the current model weights, labels,
draw basis, parameter names/draws, model manifests, ω draws, seed, and uncertainty diagnostics, and
adds the information needed to evaluate those same draws away from the fitted grid:

- `energy_meV`: the ordered fitted master grid on which the retained `/1` expected counts were made;
- `evaluation_context`: `center_meV`, Gaussian/Lorentzian resolution FWHM, temperature, and optional
  quasielastic FWHM;
- `exposure_basis`: `unit`, `fit_reference_exposure`, `reference_incremental_exposure`, and
  `n_members`.

`fit_reference_exposure` is the amplitude basis of the pooled fitted model. For requested per-point
exposure \(E\), evaluation multiplies the direct model mean by
`E / fit_reference_exposure`. Evaluating the original grid at
`reference_incremental_exposure = fit_reference_exposure / n_members` must reproduce `/1`
`expected_counts` within the fixture tolerance.

No `/2` artifact is published during this prep cycle, and `/1` is never guessed into `/2`: it lacks
the original grid, evaluation context, and explicit exposure basis.

## Future pure evaluator interface

The Qt-free interface will be:

```python
evaluate_master_predictive(
    artifact,
    energy_meV,
    exposure_per_point,
) -> PredictiveDesignEvaluation
```

An available result has schema `isar.predictive_design_evaluation/1`, echoes the requested ordered
grid and exposure, and contains each retained model's label, normalized weight, draw basis, ω draws,
and an `expected_counts[draw][point]` matrix. Values must be finite and non-negative, model/draw order
must remain stable, and the evaluator must be deterministic.

The evaluator reconstructs every registered component from `model_manifest`, substitutes each
parameter draw, evaluates the component functions directly at every requested energy, sums them, and
applies the exposure ratio. It must never interpolate stored `/1` expected counts.

Expected unsupported inputs return `available=false` with a stable `reason_code` and message:

- `artifact_schema_unsupported` for `/1` or an unknown artifact version;
- `missing_exposure_basis`;
- `malformed_draw_shape`;
- `unknown_component_kind`;
- `nonfinite_prediction`.

The evaluator owns prediction only. It must not read TAVI presets, generated background truth,
instrument rates, or request-time rate assumptions; fit or mutate scientific state; calculate EIG or
utility; generate candidates; or select a window.

## Future selection flow and audit

The EIG phase replaces the legacy-only generator behind the existing scan-design interface. Multiple
uniform `ScanDesignCandidate`s will share one frozen context. A separate scorer consumes only candidate
grid/exposure plus `PredictiveDesignEvaluation`; a selector records score components, cost, refusal
reason, and the selected candidate in the same content-addressed inventory. The outer action/Q ranking,
instrument finalization, validation, and existing request fingerprint remain separate.

The normative fixture `tests/fixtures/eig_predictive_v2.json` covers phonon-plus-background,
background-only/null, and two-mode-plus-background models on original, shifted, narrower, and coarser
grids. Its expected matrices were produced by direct evaluation of the current registered ISAR
components with the declared exposure ratio. Prep tests validate the contract and fixture integrity;
the first EIG implementation must reproduce those matrices without importing TAVI.

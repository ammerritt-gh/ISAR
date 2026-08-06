# Post-EIG-4 architectural cleanup (Pass 2)

> **Lifecycle:** Active plan
> **Provenance:** hand-written
> **Verified against production anchor:** `b9ae4907a9aa3dc0afc356bc6235b957dc2d9d03` (2026-08-06)
> **Owner:** @ammerritt-gh
> **Status:** P2.0–P2.6a landed; P2.6b–P2.8 not started.
> **Exit criteria:** the Pass-2 exit criteria at the end of this document are met and verified;
> accepted ownership boundaries are folded into `DESIGN.md` and `AGENTS.md`; this execution plan
> is then deleted and tombstoned in `docs/DOC_INVENTORY.md`.

## Purpose

Reduce the cost of understanding and changing ISAR after the EIG/STAT development phase — not
fewer lines for their own sake, but clearer ownership:

```text
analyze owns numerical fitting machinery
session owns analysis assembly and persistence
drive owns campaign orchestration
instrument adapter owns request realization
EIG modules own EIG runtime adaptation
schema owns durable scientific identity
phonon modules own phonon-specific assumptions
```

The high-level boundaries are good; several files have accumulated too many responsibilities.
`isar/drive/loop.py` (~6,360 lines) coordinates acquisition, TAVI realization, feasibility
trimming, EIG scoring and authority, analysis refresh, stopping, certification, persistence, and
replay. `isar/session/driver.py` (~5,060 lines) combines the batch pipeline with fit delivery,
master delivery, branch evidence, statistical semantics, predictive publication, and persistence,
and its flat `FitOutcome` serves as the common container for nearly every scientific subsystem.

Governance: per S10 (`../DECISIONS.md`), behavior-preserving refactors merge on ordinary review
and behavior tests. No slice in this pass creates frozen scientific evidence; the baseline below
is a development baseline, not a formal gate.

## Program baseline

Program baseline is `24ff041` (merge of PR #9 to `main`). Pass-2 work proceeds on
`EIG-4-Cleanup` from `b9ae490`, which additionally carries the Pass-3 policy cutover (P3.0 =
`98844a8` recording S10; P3.1 = `b9ae490` deleting the source-closure engine and retaining the
held-out verifier).

## Global invariants (every slice)

Every slice must preserve:

```text
existing public command behavior
existing session/results wire shapes
existing content fingerprints unless the slice explicitly owns that identity
existing deterministic ordering
existing request and executed-grid joins
existing fallback behavior
existing replay and resume behavior
existing scientific outputs within exact or established numerical tolerance
```

Testing is proportional: during a slice, run changed-module tests and one relevant integration
path; at the slice boundary, run the broad suite once. If a slice accidentally changes a
scientific number or physical request, it stops being cleanup and gets its own review.

## Non-goals

This pass does **not**: add level/continuum/magnon/tabulated-S(Q,E) fitting; implement a generic
plugin system; change AIC or model-selection authority; change EIG-4 scoring or selection;
change certification, interval precedence, or stopping semantics; introduce a workflow engine,
service container, event bus, or database; migrate historical campaign evidence; redesign the
GUI; or change persisted schemas to make internal code prettier. The acoustic/optic tracker
remains explicitly acoustic/optic — the goal is to stop that assumption spreading into reusable
layers, not to build N-branch tracking now.

## Ownership map

Current owner and intended owner of every responsibility this pass moves. Line references are
against the production anchor above.

| Responsibility | Current owner | Intended owner |
|---|---|---|
| Lineshape library | root `Fitting_Functions.py`, reached via the `sys.path`-mutating shim `isar/synth/_ff.py` | `isar/analyze/lineshapes.py` (P2.1) |
| Fit backends / estimators | `isar/synth/backends.py`, `isar/synth/estimators.py` (imported by 7 production modules: `analyze/errors/{bayes,profile,bootstrap}.py`, `analyze/{loop,ic}.py`, `calibrate/{tas,elastic}.py`) | `isar/analyze/backends.py`, `isar/analyze/estimators.py`; synth becomes a consumer (P2.1) |
| `lmfit` dependency | mandatory (`pyproject.toml`), imported only by `isar/synth/backends.py` | benchmark/validation extra (P2.1) |
| Scientific action identity | duplicated: `isar/schema/action_identity.py` and `isar/drive/acquisition/inventory.py` (`canonical_hkl` :156, `scientific_fingerprint` :174, `MeasurementIntent` :231) | `isar/schema/action_identity.py` only; `inventory.py` re-exports (P2.2) |
| TAVI request realization (limit discovery, validate/scan bodies, feasibility masks, feasible-run selection, target-preserving trims, executed-point reconciliation) | `isar/drive/loop.py` | `isar/drive/instrument/request_realization.py`, beside the existing `tavi_client.py` (P2.3) |
| EIG runtime adaptation (exact-source resolution, shadow scoring, realized comparison, authority decision, apply, submission metadata, replay authentication) | `isar/drive/loop.py` glue over `acquisition/{eig_shadow,realized_comparison,authority}.py` | `isar/drive/acquisition/eig_runtime.py` coordinating those same modules (P2.4) |
| Analysis refresh mechanics (subprocess/worker invocation, forced masters, certification manifests, results.csv / card.state.json / timing reads, completion decision) | `isar/drive/loop.py` over `isar/drive/isar_io/{runner,readers}.py` | one high-level `refresh_analysis(...)` operation on the existing `isar_io` seam (P2.5) |
| `FitOutcome` persistence DTO | `isar/session/driver.py:84` | `isar/session/outcome.py` with to/from-dict and CSV projection helpers (P2.6a) |
| Scan delivery / master delivery | `isar/session/driver.py` (`run_batch` :2045) | `isar/session/scan_delivery.py`, `isar/session/master_delivery.py`; driver keeps stage ordering and deadlines (P2.6b) |
| Evidence updates / persistence (prior admission, branch beliefs, connected tracker, target projection, predictive references; ledger, results.csv, atomic state, report inputs) | `isar/session/driver.py` | `isar/session/evidence_update.py`, `isar/session/persistence.py` (P2.6c) |
| DHO/phonon assumptions (parameter bounds, Ω_d IC grid, ω₀/Γ conversion and propagation, mode extraction and ordering, primary selection, Γ-bound diagnostics, admissibility helpers) | spread through `isar/analyze/{ic,structures,loop,select}.py` | `isar/analyze/phonon.py`; generic component/manifest/backend machinery stays generic (P2.7) |

## Slices

- **P2.1 — Numerical ownership.** Move `Fitting_Functions.py` → `analyze/lineshapes.py`, synth
  `backends.py`/`estimators.py` → `analyze/`, invert the dependency so synth exercises analyze.
  Thin compatibility re-exports only where a current importer needs them; delete
  `isar/synth/_ff.py` when unused; move `lmfit` to an extra. No backend interface redesign.
  Accept: fits numerically unchanged, no production import of `isar.synth`, no runtime
  `sys.path` mutation, wheel imports the moved modules.
- **P2.2 — One action identity.** `MeasurementIntent`, `canonical_hkl`,
  `scientific_fingerprint(_from_mapping)` live in `schema/action_identity.py`; inventory
  re-exports. Fingerprint algorithm and payload bit-for-bit unchanged; frozen vectors cover
  normal/auxiliary/cold actions, negative zero, nearby HKL, optional window centre, mapping
  reconstruction, historical persisted action. Do not consolidate unrelated canonical-JSON
  helpers.
- **P2.3 — Instrument realization.** `realize_and_validate(proposal, instrument_context)` in
  `drive/instrument/request_realization.py` using the existing `MeasurementIntent` /
  `FinalizedProposal` / `ValidatedAttempt` lifecycle types. Accept: `drive.loop` no longer knows
  TAVI field names, feasibility-mask structure, or blocker-text conventions.
- **P2.4 — EIG runtime adapter.** `drive/acquisition/eig_runtime.py` coordinates the existing
  shadow/realized/authority functions behind one bounded operation returning the selected
  request, artifact references, structured fallback reason, and submission metadata. Pure scorer
  and selector unchanged; EIG fingerprints and persisted schemas unchanged; no stage registry.
- **P2.5 — Analysis refresh.** A high-level `refresh_analysis(...)` on the `isar_io` seam
  returning only drive-relevant data (completed, timings, fresh rows, fresh card state,
  forced-master outcomes, structured failure). Process boundary intact.
- **P2.6a — Outcome DTO.** `FitOutcome` + serialization + tolerant historical reader + CSV
  projection to `session/outcome.py`. Session files and CSV byte- or field-equivalent;
  historical ledgers load.
- **P2.6b — Scan/master delivery.** Extract `scan_delivery.py` (context, candidates, selection,
  intervals, verdict, scan outcome) and `master_delivery.py` (pool, master fit, consistency,
  target delivery, certification payload, predictive inputs, master outcome).
- **P2.6c — Evidence/persistence.** Extract `evidence_update.py` and `persistence.py`;
  `run_batch()` becomes orchestration. The pre-merge-repaired admission predicate remains the
  one source for independent fit evidence.
- **P2.7 — Phonon quarantine.** `analyze/phonon.py` owns the DHO-specific machinery; cheap
  renames (`structure_ic` → `phonon_structure_ic`, `enumerate_structures` →
  `enumerate_phonon_structures`) with temporary aliases. A spectral-model registry is built only
  when a second production model family actually needs a different initialization, bounds,
  derived QoI, enumeration, or manifest component.
- **P2.8 — Closure.** Remove compatibility re-exports with no supported consumer; update
  architecture/module docs; fold conclusions into `DESIGN.md`/`AGENTS.md`; delete and tombstone
  this plan. Verify with focused moved-module tests, one full parallel suite, one full serial
  suite (package/test structure changed), one deterministic mock-TAVI campaign path, one
  persisted workspace replay.

## Development baseline evidence (P2.0)

Recorded at the production anchor above. Ordinary development record per S10 — not a frozen
gate, never renewed.

- **Full parallel suite** (`python -m pytest -n 16 --dist worksteal`): 3854 passed, 8 skipped,
  in 8 m 17 s (2026-08-06).
- **Wheel/import smoke:** `python -m pip wheel . --no-deps` builds; from an install of that
  wheel outside the repo, `isar`, `isar.analyze`, `isar.session`, `isar.drive`, `isar.schema`,
  `isar.synth`, `isar.synth.backends`, and `isar.analyze.components` all import — the
  `Fitting_Functions` shim path currently survives packaging, and P2.1 must keep that true
  without the `sys.path` mutation.
- **Deterministic driver integration reference:** `tests/session/test_driver_smoke.py` (S30
  synthetic path — held-status, idempotency, timer stages). Existing test; no new fixture.
- **Representative persisted fingerprint fixtures:** `tests/session/test_fit_persistence.py`
  (`master_fit_fingerprint`, scan `content_hash`, `published_hashes` manifest) and the
  byte-compared `tests/data/golden/` set. P2.2 adds frozen identity vectors as part of that
  slice, not here.

## Ledger

| Slice | Commit | Notes |
|---|---|---|
| P2.0 | `c6d1625` | Baseline, ownership map, active plan |
| P2.1 | `648d738` | Lineshapes/backends/estimators under `analyze`; `phonon.py` seeded; `_ff.py` shim deleted; `lmfit` → `[test]` extra. Included repair: the held-out verifier hashed the *live* `stat2b/campaign.py` as the historical evaluator (a selection-critical live file, contra S10); its frozen source is now preserved as evidence at `tools/stats/stat2b1-heldout-evaluator.frozen` (extracted at `b9ae490`, same frozen SHA256) and authenticated there |
| P2.2 | `19f8613` | One action identity: `MeasurementIntent` + `scientific_fingerprint` consolidated into `schema/action_identity.py`; inventory re-exports; frozen digest vectors (incl. a historical fixture payload) captured pre-consolidation and unchanged after |
| P2.3 | `8fc8057` | TAVI wire knowledge (validate/scan bodies, limit discovery, feasibility interpretation, run selection) out of `drive/loop.py` into `drive/instrument/request_realization.py`; `FeasibilityDecision` seam replaces the inlined trim branching; loop keeps EIG refresh, budget, session-log side effects |
| P2.4 | `704ddb0` | EIG runtime adaptation (shadow scoring, realized comparison, authority preparation, pre-submit authentication, submission-metadata assembly) out of `loop.py` into `drive/acquisition/eig_runtime.py`; loop passes `finalizer=_finalize_proposal` explicitly (no cycle). Scope notes: no frozen input record (existing signatures suffice); exact-source session-log joins (`_exact_pooled_master_sources`/`_read_master_rows`) stay in loop pending the P2.5 `isar_io` seam — partial delivery of the ownership-map row |
| P2.5 | `8a1e4ea` | `refresh_analysis(...)` on `isar_io/runner.py` owns runner-signature inspection, kwarg assembly, and the completion decision; the three `runner.run` sites become explicit seam calls; P2.4-deferred readers land in `readers.py` (`read_run_timings`, `exact_pooled_master_sources`, `read_master_rows` with injected row-interpretation functions). Documented exception: one `runner_accepts` survives at the predictive-promotion degrade site (loop.py:1147) — it changes pre-run logging/targeting, not kwargs; replacing it with a declared `supports_targeted_masters` runner attribute is deferred |
| P2.6a | (this commit) | `FitOutcome` + `to_dict` + tolerant reader (`_coerce_fit_outcome`) + `_CSV_COLUMNS` + `_write_results_csv` and projection helpers moved to `session/outcome.py` (stdlib+schema imports only, purity-probed); driver imports the DTO; ledger-drift warning logger renamed to `isar.session.outcome` |

Deferred findings (things noticed but out of scope for the slice that noticed them):

- `isar/calibrate/tas.py` imports `isar.synth.resolution` (Cooper–Nathans resolution) — production
  calibration genuinely depends on synth machinery that P2.1 did not move; candidate for a later
  ownership decision. Until then the acceptance criterion "no production import of `isar.synth`"
  carries this one named exception.
- `isar/synth/stat2b/campaign.py` `CAMPAIGN_SOURCE_PATHS` rows for the three moved files were
  deleted (the `isar/analyze` glob already covers their new homes); ordinary development under
  S10, no re-freeze.
- Pre-existing (moved verbatim in P2.1, flagged by review, not fixed in a move slice):
  `isar/analyze/phonon.py` `omega0_value_error` divides by zero when `omega_d = gamma = 0` with
  a covariance supplied; `isar/analyze/backends.py` `lmfit_minimize` accepts finite-but-non-PSD
  covariance where the scipy path requires positive eigenvalues. Candidates for P2.7 when the
  phonon module is worked anyway.
- Pre-existing, carried through P2.2 unchanged: `canonical_hkl` / the energy fields coerce
  numeric strings via bare `float()`, so `["2","0","0"]` and `"1.25"` are accepted at the
  persisted-artifact trust boundary. Tightening would move digests — only ever do it behind a
  named milestone, never as cleanup.
- After P2.4, `authenticate_realized_submission` / `load_scan_design_realized_comparison` /
  `authenticate_authority_submission` are imported into both `loop.py` and `eig_runtime.py`
  (same objects, two namespaces) — two independent monkeypatch seams for the same callable.
  Current tests patch the correct side; future test authors must patch the namespace whose
  caller they exercise. Collapses naturally if later slices remove loop's remaining direct
  calls (`loop.py:3160,3174,3445`).
- `isar/synth_format/run_builder.py` imports `isar.synth.{config,generate,noise}` and is
  reachable from `isar/reduce/adjudicate_eyeball.py` — correct ownership (synth_format emits
  synthetic data), but the "no production import of `isar.synth`" criterion holds only with
  `synth_format` counted as synthetic tooling.

## Pass-2 exit criteria

- production analysis no longer depends on `isar.synth`;
- scientific action identity has one implementation;
- `drive.loop` does not own TAVI wire details, EIG artifact assembly, or analysis CLI mechanics;
- `session.driver` primarily orders stages rather than implementing every stage;
- `FitOutcome` is clearly a persistence DTO;
- DHO assumptions are visibly owned by phonon modules;
- runtime and persisted scientific behavior remain unchanged;
- no speculative plugin or orchestration framework was introduced.

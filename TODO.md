# ISAR — backlog

A work queue, not a project diary. Completed work leaves this file; git history and
`docs/DECISIONS.md` are the record of what shipped.

Every live item has a **stable ID** and a one-line **Acceptance** condition. Owner, governing
decision, and dependencies are optional and present only where they carry information.

Design references: [DESIGN.md](DESIGN.md) (architecture + per-capability status §15),
[docs/READING_GUIDE.md](docs/READING_GUIDE.md) (the documentation map), and the closed-loop
capstone in the sibling TAVI repo (`..\TAVI\docs\CLOSED_LOOP_DESIGN.md` — TAVI = instrument,
ISAR = analysis authority). Forward workstreams live in their own plans:
[SPEEDUP](docs/SPEEDUP_DESIGN.md), [STEERING](docs/STEERING_DESIGN.md),
[BACKGROUND](docs/BACKGROUND_DESIGN.md).

Last updated: 2026-07-25.

---

## Defects

Things that are wrong now, as opposed to things not yet built.

- [ ] **DEFECT-1 — GUI test crashes the interpreter, hiding ~691 tests.**
      `tests/gui/test_browser.py::test_load_workspace_twice_no_menu_leak` is `@pytest.mark.skip`
      (2026-07-21): it exits 139 (access violation) even run alone, and took the whole GUI suite
      down with it, so the other ~691 GUI tests were never executing. Needs diagnosis, not
      deletion — it guards against a menu leak on repeated workspace loads, and loading a
      workspace twice is a routine operator action, so this may be a genuine `load_workspace`
      teardown bug rather than a test artefact. Suspect the test's global
      `QApplication.sendPostedEvents(None, DeferredDelete)`, which drains deferred deletes for
      every object in the process.
      **Acceptance:** the test runs unskipped in the full GUI suite without crashing, and the
      root cause is identified as either a product bug (fixed) or a test artefact (documented).

- [ ] **DEFECT-2 — Matched-descriptor rescue can deliver confident-wrong.**
      `verdict.classify()` lets a family-wise-significant descriptor peak that the winning model
      happens to explain bypass the no-excursion gate straight to `resolved` (the deliberate
      high-q rescue). A reachable false-positive path: AIC is permissive and confidence carries no
      peak-significance factor. No `classify()`-level test covers flat-but-matched → resolved.
      **Acceptance:** a characterization test covers the path, and either a minimum
      peak-significance floor gates the rescue or the risk is explicitly accepted in writing.

- [ ] **DEFECT-3 — `stokes_only` gate is prior-only.**
      `structures.antistokes_mode()` decides to drop the anti-Stokes side purely from
      `ctx.prior_omega` (populated only when branch σ ≤ 0.6 meV, so a *confidently wrong*
      posterior triggers it). No data-driven cross-check once dropped, and no test with a wrong
      prior. Interacts with Hard Rule 2 — anti-Stokes admissibility is a detailed-balance
      question, not a prior question.
      **Acceptance:** a test with a deliberately wrong prior, plus a data-driven cross-check
      before the anti-Stokes side is dropped.

- [ ] **DEFECT-4 — `build_beliefs()` doesn't filter by `fit_kind`.**
      Contract 4 ("constrained fits never seed a branch") is enforced for Γ/A via
      `_is_clean_anchor` but *not* for the ω₀ assignment belief: a `branch_conditional` delivery
      (gated only by the weaker `constrained_ok` bar — p_acoustic ≥ 0.6, no dominance or entropy
      check) feeds later assignment posteriors. Narrow but real self-reinforcement channel.
      **Acceptance:** the ω₀ assignment belief applies the same `fit_kind` filter, with a test
      that a `branch_conditional` delivery cannot seed a later posterior.

- [ ] **DEFECT-5 — `branch_continuation` mislabeling loses the recenter/widen retry.**
      Fit-derived features get `float("inf")` significance in `features_from`, so a garbage fit on
      an out-of-window scan can be triaged `branch_continuation` (not counted missed) and the q
      burns its 3-decline budget instead of recentering. The right fix is honest significance for
      fit-derived features — *not* re-including `branch_continuation` in `_rows_missed`.
      **Acceptance:** an out-of-window garbage fit triggers recenter/widen rather than consuming
      the decline budget.

---

## Validation gates

- [ ] **VAL-1 — Restore the IN8 gold CSV.** `test_data/ILL IN8/IN8_fit_results.csv` is missing
      from disk, gitignored, and cannot be restored from git; only `055182 ≈ 4.2 meV` and
      `055183-REJECT` are vetted rows. Blocks `docs/phase3_gold_in8_check.py`,
      `tests/real_data/test_in8_gold_smoke.py`, and every IN8 gate.
      **Acceptance:** the CSV is restored and `tests/real_data/test_in8_gold_smoke.py` passes.

- [ ] **VAL-2 — Full S30 statistical comparison at M=200.** End-to-end ω₀ comparison against
      `results_gold.csv` through the CLI; a multi-minute run deferred for a fast machine. Gate on
      dispositions + safety (0 gold-REJECT resolved) + ω₀ residuals, **not** confidence (risk R4).
      This is the validation `README.md` describes as still pending.
      **Acceptance:** the run completes and the scorecard records 0 safety violations, with the
      result promoted into `DESIGN.md` §15.

---

## GUI roadmap

Specs in [GUI_DESIGN.md](docs/GUI_DESIGN.md) §16. Designed 2026-07-16, not built.

- [ ] **GUI-1 — PUFFIN masking port.** `CustomViewBox` → `isar_gui/views/mask_viewbox.py`; first
      consumer is the Sandbox pane. View-layer only; PUFFIN's σ≥1 floor never crosses (Hard Rule 3).
      **Acceptance:** masking works in the Sandbox pane with no variance-convention leak, proven
      by a test asserting ISAR's σ=√N convention survives.
- [ ] **GUI-2 — PUFFIN handoff contract** (§16.2). Scan out (x/y/σ/meta) → masks + model params
      back. **Blocked on:** a PUFFIN-side headless surface.
      **Acceptance:** a round-trip moves a scan to PUFFIN and returns masks that ISAR applies.
- [ ] **GUI-3 — Combine-scans ("same spot") UX** (§16.3). Pooled preview overlay now, persistence
      via a collation-policy directive. **Blocked on:** Phase C.
      **Acceptance:** an operator can preview a pooled combination and persist it as card policy.
- [ ] **GUI-4 — S30 energy-zero carry-forward** (§16.4). Step-function most-recent-preceding-elastic
      semantics in `isar/calibrate/energy_zero.py`; later elastics never retroactive. Needs a
      D-series decision plus calibrate characterization updates.
      **Acceptance:** the D-series ruling is recorded and characterization tests pin the
      step-function semantics.
- [ ] **GUI-5 — Import window** (§16.5). Detect-preview dialog replacing the compact Files dock.
      **Acceptance:** an operator can preview a detected instrument and column map before ingest.
- [ ] **GUI-6 — Per-q live campaign state** (§16.6). Additive event-contract change unlocking the
      per-q half of the drive dashboard.
      **Acceptance:** per-q state updates live during a campaign, with the contract change additive.
- [ ] **GUI-7 — Reclaimable in-flight scans** (§16.7). Resume re-attaches via the run event's
      `job_id` instead of re-proposing. **Blocked on:** TAVI job-query support.
      **Acceptance:** a resumed campaign re-attaches to an in-flight scan rather than re-proposing it.

---

## Calibration

- [ ] **CAL-1 — Calibration curve v2.** Re-run the σ-calibration campaign with the N-mode fitter
      and wide windows; derive per-configuration inflation. **Blocked on:** interval calibration
      for γ-at-bound fits — 2-mode fits with γ at its bound report absurdly tight intervals
      (halfwidth ~1e-3 meV) that dominate the inflation metric.
      **Acceptance:** a v2 calibration artifact exists with per-configuration inflation factors.
- [ ] **CAL-2 — Bayes-interval coverage campaign.** The `--errors bayes` tier ships the posterior
      as diagnostics-only (DECISIONS S5); before it can enter `_pick_interval` precedence it needs
      its own truth-scored coverage artifact (`isar score --truth-* --interval bayes`). Includes
      settling `nlive`: `docs/stats/bench_error_methods.py` showed 250 ≈ 400 on 9/11 scans but 250
      markedly narrower on two very-high-count scans despite ESS > 1000.
      **Acceptance:** a coverage artifact exists; S5's "promotion pending coverage validation" is
      either discharged or the tier is confirmed diagnostics-only for good.
- [ ] **CAL-3 — γ-at-bound calibration v2 must cover the HESSE trust gate.** SVP_RECERT Finding 3:
      q=0.45 mis-certified via `hesse_trusted=True` — certified halfwidth 0.002 meV, truth 0.070
      meV away (~35 halfwidths). γ=0.0134 passed the at-bound heuristic. The trust gate needs the
      same calibration scrutiny as the bootstrap flag, and the confident-wrong scorecard check
      must test certified-interval *coverage*, not only pull on unflagged rows.
      **Acceptance:** the scorecard tests certified-interval coverage and the q=0.45 case no
      longer certifies.
- [ ] **CAL-4 — Worker path ignores the bayes CLI knobs.** `--bayes-nlive/--bayes-dlogz/
      --bayes-maxiter` thread through direct `isar run` only; the persistent worker resolves
      `_resolve_bayes_opts(None)` = env/defaults (`ISAR_BAYES_*` still work). Needs a
      worker-protocol field — do it alongside the next protocol change.
      **Acceptance:** the knobs take effect through the worker path.

---

## Fitting and resolution

- [ ] **TAS-1 — 4-D resolution convolution (`tas4d`).** Cooper–Nathans tilted-ellipsoid + local
      dispersion slope (ResLib/Tobyfit lineage). Unblocks intrinsic Γ on TAS, which is currently
      always `gamma_provisional`. Evidence of need from the first closed-loop scan: measured AS/S
      intensity ratio ≈ 0.52 vs Bose-tied ≈ 0.92 — a focusing asymmetry the 1-D width cannot
      model. The fit correctly lands `model_incomplete/flagged` and can do no better until this.
      **Governing:** Phase 3 D5 / risk R8. **Acceptance:** Γ on TAS is reported as intrinsic with
      a validated resolution deconvolution.
- [ ] **TAS-2 — Decide the `tas4d` engine.** Wrap an existing engine vs implement the
      Cooper–Nathans projection against TAVI ellipsoids directly. Still open (`DESIGN.md` §13 Q3).
      **Acceptance:** a D-series decision is recorded.
- [ ] **FIT-1 — LLM guideline proposer.** Follows the `adjudicate.py` pattern: on
      flagged/model_incomplete/unconstrained fits, propose `{structures, peaks, exclude_regions,
      fit_range, reason}`; write `card.state.corrections`; apply only via a guidelines-hash re-fit
      (P1 — no LLM in the runtime path of a number). The fields it edits already exist.
      **Acceptance:** a proposal round-trips into a re-fit without the LLM touching a fitted number.
- [ ] **FIT-2 — Multi-branch dispersion prior.** `PriorPoint` has no branch/mode field, so the
      prior carries the primary mode only.
      **Acceptance:** the prior distinguishes branches and a secondary mode can seed one.
- [ ] **FIT-3 — N=3 modes.** Mechanical extension once 2-mode is field-proven.
      **Acceptance:** 3-mode enumeration passes the synthetic gates without regressing 1/2-mode.

---

## Driver and steering

Speedup work is governed by [SPEEDUP_DESIGN.md](docs/SPEEDUP_DESIGN.md); steering physics by
[STEERING_DESIGN.md](docs/STEERING_DESIGN.md). Only items outside those plans live here.

- [ ] **DRV-1 — Supervisor-model auto-fallback.** `gpt-oss-120b` throws transient 503s; today the
      operator pins `LLMM_SUPERVISOR_MODEL` by hand (`deepseek-v4-pro:thinking` is the proven
      backup). Fall back automatically down a probed shortlist.
      **Acceptance:** a 503 from the primary supervisor model does not require operator action.
- [ ] **DRV-2 — Objective-aware Γ exclusion.** Acoustic ω₀ → 0 at zone centers is structurally
      unmeasurable; parse-time exclusion beats spending ~4 scans per campaign discovering it.
      **Acceptance:** a zone-center q is excluded at objective-compile time with the reason recorded.
- [ ] **DRV-3 — The SVP, remaining rungs.** (a) Realistic clocks — counting time reported as the
      instrument would report it (TAVI-side virtual clock), exercising wall-clock budget semantics
      (§8, O2); today `budget.spent` sums TAVI's *compute* eta, which is the root cause of the
      MVP16 "10.6 of 28 800" (a semantics gap, not units). (b) The McStas rung. The deterministic
      rung ran 2026-07-10.
      **Governing:** T1/T2, `DESIGN.md` §9. **Acceptance:** a campaign runs against a virtual
      clock with honest budget accounting; `DESIGN.md` §15 SVP row moves off Partial.
- [ ] **DRV-4 — Confirm-churn intent confirmations** (SVP_RECERT, still open): `n_uncertifiable`
      never resets, so a chronically flaky q is never abandoned; and triage should show confidence
      beside `(unreliable)`. Related: a q with its half-width right at tolerance re-confirms
      forever (first run: H=2.1 took 22 confirm fits, H=2.15 took 12, vs 1 each for well-inside
      q). Capping confirms per q is the cheap half of the fitting-bound problem — the per-fit
      half is [SPEEDUP_DESIGN.md](docs/SPEEDUP_DESIGN.md).
      **Acceptance:** a chronically flaky q is abandoned by policy, and triage output shows both.

- [ ] **DOC-1 — Move the primary evidence out of disposable `out/**` paths.** `README.md`,
      `READING_GUIDE.md`, `STATS_PRIMER.md` and `WORKED_EXAMPLE.md` cite gitignored `out/**`
      artifacts as their headline evidence, so a clean checkout cannot see them. Commit a defined
      bundle derived from `test_data/` under `docs/assets/` with a provenance manifest; the raw
      facility data stays gitignored. The rest of the 2026-07-09 docs-consolidation item (the
      vocabulary sweep and status tags) was completed in the 2026-07-25 documentation pass.
      **Acceptance:** the cited evidence is viewable from a clean checkout, and no doc claims
      reproducibility that `test_data/` absence would falsify.

---

## Performance and internals

None of these change a reported number; all are cost or hygiene.

- [ ] **PERF-1 — Acquisition hot-path caching.** Per iteration `fisher()` makes one blocking TAVI
      `get_resolution` HTTP call plus 11 forward-model evals per candidate with no memoization
      (~21 candidates); `_grid_state` fully re-reads `results.csv` and re-scans all observations
      twice per iteration (`match_grid_index` is a linear scan). Memoize resolution by (hkl, ΔE),
      cache parsed `results.csv` by mtime, index the grid path.
      **Acceptance:** per-iteration acquisition time drops measurably with identical decisions.
- [ ] **PERF-2 — `build_beliefs` O(n²) rebuild.** Reconstructed from the full dispersion prior on
      every scan and master fit; cache per (sample, t_bin) within a `run_batch`.
      **Acceptance:** belief construction is O(n) per batch with unchanged outputs.
- [ ] **PERF-3 — Worker keeps no state resident.** The persistent worker round-trips full
      `card.state.json` + `session.json` per job (O(n²) I/O over a campaign). Residency needs an
      explicit single-writer contract or a workspace lock first — today's always-reload is what
      makes mid-campaign human card edits safe.
      **Acceptance:** a single-writer contract exists and residency is enabled behind it.
- [ ] **INT-1 — Centralize steering string constants.** `fit_kind` literals ("unconstrained",
      "soft_prior", "branch_conditional", "stokes_only", "descriptor_only") are spread over 8
      files, and "acoustic"/"optic" is duplicated as two independent `BRANCH_ID` constants
      (`branch_model.py`, `branch_view.py`) plus literals in 4 more. Home: `isar/schema/steering.py`.
      Also: the `f"{scan_id}#{branch}{i}"` banked-anchor convention needs a shared construct/parse
      helper before anything else consumes `dispersion_prior[*].scan_id`.
      **Acceptance:** one definition per constant, imported everywhere.
- [ ] **INT-2 — `steering.py` dataclasses on the generic `_json` codec.** Needs parameterized
      annotations first (`tuple[DescriptorPeak, ...]`), then drop the hand-rolled
      `to_dict`/`from_dict`.
      **Acceptance:** the hand-rolled codecs are gone with round-trip tests passing.
- [ ] **INT-3 — `summarize_grid` None→acoustic fallback is untested.** Deliberate (posterior-off /
      legacy ledgers) but nothing locks down either mode.
      **Acceptance:** tests cover `branch_id` = None vs "optic" vs "acoustic".
- [ ] **INT-4 — Small shared-code cleanups.** Share `_linfit` (`branch_model`) with
      `session_prior.prior_for`'s inline polyfit; unify `session_log.timed()` /
      `StageTimer.stage()` if the sink semantics ever converge; `_errors_arg_for` is
      production-dead (test-only callers); worker `_reconfigure_stdio` duplicates `cli/main`'s
      (blocked by the worker import boundary — needs a lower-layer home).
      **Acceptance:** each is either done or explicitly dropped.

---

## Platform and instruments

- [ ] **PLAT-1 — Query API for downstream consumers.** The driver reads `report/results.csv` /
      `report.json` / `card.state.json` directly (the stable file contract). Revisit an
      `isar query` or HTTP surface once the file contract chafes. Deliberately not now.
      **Acceptance:** revisited once a second consumer exists.
- [ ] **PLAT-2 — Watch/daemon mode.** Sugar wrapping `run_batch` (U1). At real-instrument scale
      per-scan cadence may also motivate incremental parsing; the batch re-parse is fine at
      simulation scale.
      **Acceptance:** a watched folder triggers incremental runs without a re-parse of the batch.
- [ ] **PLAT-3 — Per-analyzer S30 resolution** (risk R1). Measured widths exist only for Ana7;
      others fall back to `default_resolution` with a provenance flag.
      **Acceptance:** per-analyzer widths are measured and used, with provenance recorded.
- [ ] **PLAT-4 — New instrument cards: ID28, BL43LXU.** Registry slots exist; authoring the cards
      is a small later task.
      **Acceptance:** both parse a real file end-to-end.
- [ ] **PLAT-5 — Batch re-analysis driver.** Re-run everything post-beamtime from frozen config at
      better resolution (`DESIGN.md` §14 item 8).
      **Acceptance:** a frozen campaign re-runs and produces a comparable report.
- [ ] **PLAT-6 — Migrate the check scripts onto the driver.** Only after step-5 equivalence is
      proven; until then they are the validation record.
      **Acceptance:** the `docs/*_check.py` drivers run through the CLI with identical results.
- [ ] **TAVI-1 — 2D scans.** Currently a clear refusal; support when a use case appears (path
      scans / campaigns on the TAVI side may produce them).
      **Acceptance:** a 2D scan parses, or the refusal is confirmed permanent.
- [ ] **TAVI-2 — Per-sample card sections / sample-keyed collation.** `sample_key` is recorded in
      `raw_header` only; collation still keys on the card `experiment_id`.
      **Acceptance:** two samples in one workspace collate separately.

---

## LLM layer

`DESIGN.md` §9. Everything here stays off the critical numerical path (P1).

- [ ] **LLM-1 — Structured-output IC / peak-structure proposal** (T2/T3), seeded by the dispersion
      prior first, propose-and-freeze per P1.
      **Acceptance:** proposals freeze into config before the deterministic fitter runs.
- [ ] **LLM-2 — Residual-diagnosis pass on failed fits.** Vision tier, off the critical path,
      after deterministic attempts.
      **Acceptance:** a failed fit gets a recorded diagnosis that changes no number.
- [ ] **LLM-3 — Richer review surface / correction channel** beyond card-edit + re-run
      (`DESIGN.md` §10). The v1 channel works — exercised live 2026-07-03 with a temperature fix
      plus seeded expectation, re-fitting exactly the affected scans.
      **Acceptance:** a correction can be made without hand-editing the card.

---

## Wishlist

Smaller items, mostly GUI polish; fold into other work where convenient. No acceptance
conditions — these are wants, not commitments.

- [ ] **WISH-1** — Show a bounded box for the last scan area.
- [ ] **WISH-2** — Error bars in the master fit, showing the width of the phonon energy error.
- [ ] **WISH-3** — Better symbols with clear movement from individual (resolved/not) to master
      scans:
    1. Small green circle for a valid individual fit
    2. Larger hollow green circle for the current master fit center that matches target conditions
    3. Hollow red diamond for the current master fit that does NOT match target conditions
    4. Hollow blue square for a master fit that is certified
- [ ] **WISH-4** — Master scans report the total neutrons spent, i.e. what the result actually cost.
- [ ] **WISH-5** — User-facing log cleanup, e.g. no separate "start"/"completed" lines.
- [ ] **WISH-6** — Connecting to TAVI is slow with a new workspace, though it was connected before.
- [ ] **WISH-7** — If a folder doesn't exist when saving a workspace, create it.
- [ ] **WISH-8** — Data-point error bars should be Poisson-correct and asymmetric: a count of 1
      gives far smaller error bars on the negative side than the positive side.
- [ ] **WISH-9** — Odd black-and-white colour palette on some scans — possibly the first one?
- [ ] **WISH-10** — Use crystallographic energy to get Brillouin-zone sizes (not small).
- [ ] **WISH-11** — Keep the GUI updated as scans finish, not only as fits finish.

---

## Notes

**GUI test isolation.** GUI tests stub the backend health probe offline via an autouse fixture in
`tests/gui/conftest.py`. Kept deliberately, though it was not the cause of DEFECT-1: without it,
constructing any `MainWindow` in a test fires real `urlopen` calls at the operator's live
LLMM/TAVI ports, so results would depend on which services happen to be running.

# ISAR — backlog

> **Status:** live
> **Authority:** canonical for the single backlog
> **Verified against code revision:** `d0ea1df` (2026-07-25)

A work queue, not a project diary. Completed work leaves this file; git history and
the owning design docs' dated rulings are the record of what shipped.

Every live item has a **stable ID** and a one-line **Acceptance** condition. Owner, governing
decision, and dependencies are optional and present only where they carry information.

Design references: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (architecture + per-capability status §15),
[docs/READING_GUIDE.md](docs/READING_GUIDE.md) (the documentation map), and the closed-loop
capstone in the sibling TAVI repo (`..\TAVI\docs\CLOSED_LOOP_DESIGN.md` — TAVI = instrument,
ISAR = analysis authority). Forward workstreams live in their own plans:
[SPEEDUP](docs/plans/speedup.md), [STEERING roadmap §10](docs/design/steering.md),
[BACKGROUND](docs/plans/background.md), [EIG](docs/design/eig.md).

Last updated: 2026-10-23.

---

## Defects

Things that are wrong now, as opposed to things not yet built.

- [ ] **DEFECT-4 — Float-exact assert fails in branch-persist test.**
      `tests/session/test_branch_persist.py::test_branch_belief_preserves_complete_posterior_and_legacy_fallback`
      asserts `label_probabilities == branch_posterior` with exact float equality and fails on
      round-trip drift (`0.7000000000000001 != 0.7`; observed 2026-08-20 in the rebuilt `isar`
      env, unrelated to any code change — the diff was markdown-only). Either the store now
      renormalizes and the test should use `pytest.approx`, or a real renormalization slipped
      into the persist path and needs a look.
      **Acceptance:** test passes deterministically in the current env, with the tolerance choice
      justified in the test.

- [ ] **DEFECT-2 — Matched-descriptor rescue can deliver confident-wrong.**
      `verdict.classify()` lets a family-wise-significant descriptor peak that the winning model
      happens to explain bypass the no-excursion gate straight to `resolved` (the deliberate
      high-q rescue). A reachable false-positive path: AIC is permissive and confidence carries no
      peak-significance factor. No `classify()`-level test covers flat-but-matched → resolved.
      **Acceptance:** a characterization test covers the path, and either a minimum
      peak-significance floor gates the rescue or the risk is explicitly accepted in writing.

- [ ] **DEFECT-3 — `stokes_only` gate is prior-only.**
      `phonon.antistokes_mode()` decides to drop the anti-Stokes side purely from
      `ctx.prior_omega` (populated only when branch σ ≤ 0.6 meV, so a *confidently wrong*
      posterior triggers it). No data-driven cross-check once dropped, and no test with a wrong
      prior. Interacts with Hard Rule 2 — anti-Stokes admissibility is a detailed-balance
      question, not a prior question.
      **Acceptance:** a test with a deliberately wrong prior, plus a data-driven cross-check
      before the anti-Stokes side is dropped.

- [ ] **DEFECT-6 — Convolution-grid centring bias in `convolute_voigt_dho` (fix deferred).**
      The internal `arange(-span, span, dx)` grid contains exact 0 only when the span is a dx
      multiple, so `fftconvolve(mode='same')` carries a sub-dx (≤ dx/2 = 0.025 meV), fit-window-
      dependent centring offset on every convolved model, biasing fitted ω₀ by up to ~0.03 meV.
      The symmetric odd-grid fix was implemented during the PR #10 review response and reverted:
      the corrected (honestly flat) landscape makes degenerate k≥7 candidates fail MIGRAD often
      enough that shadow selection reports `unavailable` — which `isar/eig/eligibility.py` treats
      as a science blocker — and it invalidates the pending Slice-8 replacement candidate and the
      predictive/golden fixtures. Requires its own slice: fit robustness for degenerate candidates
      (or a shadow-availability contract that tolerates a non-converged superfluous candidate),
      fixture/golden regeneration, and Slice-8 candidate renewal validated by the ordinary
      deterministic world-suite lanes (H1 retired the O42 release review).
      `tests/analyze/test_lineshapes.py` pins the defect as a strict xfail that flips to XPASS
      when the grid is fixed.
      **Acceptance:** the xfail in `tests/analyze/test_lineshapes.py` passes un-marked, with no
      shadow-availability regression on the SHADOWREG synthetic probe and renewed Slice-8
      candidate evidence.

- [ ] **DEFECT-5 — `branch_continuation` mislabeling loses the recenter/widen retry.**
      Fit-derived features get `float("inf")` significance in `features_from`, so a garbage fit on
      an out-of-window scan can be triaged `branch_continuation` (not counted missed) and the q
      burns its 3-decline budget instead of recentering. The right fix is honest significance for
      fit-derived features — *not* re-including `branch_continuation` in `_rows_missed`.
      **Acceptance:** an out-of-window garbage fit triggers recenter/widen rather than consuming
      the decline budget.

- [ ] **DEFECT-7 — Parallel and serial master fits disagree.**
      `tests/session/test_master_fit.py::test_parallel_masters_match_serial_end_to_end` fails on
      `main` (reproduced at `be0c907`, 2026-10-20): the pooled master's detector outcome differs
      between the parallel and serial paths. Slow-marked, so only the full lanes see it.
      **Acceptance:** the test passes in both the full parallel and full serial lanes.

- [ ] **DEFECT-8 — Tests still need gitignored `test_workspaces/`.**
      Three routine tests read `test_workspaces/AP7_Failed` and `AP8_Failed`, which exist only in
      the main checkout; they skip in every worktree. Same problem the `real_data/` move fixed
      (AGENTS.md hard rule 10): give them committed artificial inputs or move them to `proofs/`.
      **Acceptance:** no test reads `test_workspaces/`, and the routine lane's skip count in a
      fresh worktree drops by three.

- [ ] **DEFECT-9 — PR #46 fails the fcc campaign test at its wall ceiling.**
      `tests/drive/test_fcc_battery.py::test_full_range_discovery_finds_and_maps_both_phonons` hits its
      closed-loop wall ceiling (698 s serially) on PR #46's lineage; it passes on `main` in 118 s.
      Bisected to `1f58dfe` (a narrow mode keeps its area on the grid): narrow modes go
      resolution-limited and spawn negligible delta-twin rows, and the scene `presence` factor has no
      score bound (`isar/scene/factors.py:524-533`), so margin pruning is off and the cluster passes the
      4096-alternative cap after 5 scans. Cause as stated in `docs/HANDOFF-letacon-fitting.md`.
      **Acceptance:** the presence factor has a cluster-size-dependent bound, and the test passes within
      its ceiling (a later job; the scene presence bound is out of scope for the fast-pass job).

---

## Validation gates

- [ ] **VAL-3 — Deletion of user data must be *tested impossible*, not merely gated.**
      Operator ruling (2026-08-20 walkthrough): "I should be able to stand up and say that ISAR
      doesn't even have the capability to delete user data — it just doesn't exist in there."
      Not a review item — a structural test in the spirit of `tests/test_import_contract.py`:
      walk the AST of `isar/` and `isar_gui/` for every deletion-capable call
      (`os.remove/unlink`, `Path.unlink`, `shutil.rmtree`, `os.rmdir`, rename-over, truncating
      writes) and assert each call site is provably confined to ISAR-owned outputs (workspace
      internals, `out/`, caches) — never a path derived from an input-data argument. Input-data
      paths must flow only into reads.
      **Acceptance:** the test enumerates every deletion call site in the tree, fails on any new
      one that is not allow-listed to an ISAR-owned output root, and a deliberate seeded
      violation (temporarily deleting from a data dir) is caught by it.

- [ ] **VAL-4 — Raw-counts-only storage must be *tested impossible* to violate.**
      Sibling of VAL-3, same operator ruling: monitor is recorded per point and rides along;
      division by monitor happens only at consumption time; stored scans are raw counts, ever.
      The law exists (ARCHITECTURE §6, combine-before-divide) — this makes it structural: a test
      asserting every persisted `ScanRecord`/unit-data counts array is integer-valued Poisson
      counts (a normalized rate is the thing that cannot survive that check), that per-point
      monitor is present wherever counts are, and that no ingest/reduce/collate path writes a
      counts-divided-by-monitor array into any persisted artifact.
      **Acceptance:** the checks run on synthetic + real-data fixtures; a seeded violation
      (persisting a normalized array) fails loudly.

- [ ] **VAL-5 — Cosmic-ray thresholds set by measured error rates.**
      Operator ruling (2026-10-20): the obvious ray (hundreds of counts, then tens of thousands)
      is easy; the hard case is a pass only a little high, and good data must not be thrown away.
      Draw rays at random magnitudes and positions into simulated data, count true/false
      positives and negatives for each rule, and tune the thresholds conservatively, with
      options for the user. The beamline's analyzer-4 3σ divergence (`proofs/`) is a question
      for the beamline scientist.
      **Acceptance:** a committed study reports the error rates per rule and threshold, and the
      card defaults cite it.

- [ ] **VAL-6 — Review committed real-data material before going public.**
      Operator ruling (2026-10-20): the repo will be public; headers may stay, measured data
      must not leak. Already committed: `docs/assets/` (19 figures, 3 CSVs from real data),
      `docs/campaigns/` evidence, the four `docs/*_check.py` scripts' values, and git history of
      the old `tests/real_data/` expectations. The evidence manifest's data fingerprint is also
      stale (records 234 files; `real_data/` now holds 870), though `--check` passes.
      **Acceptance:** each item is kept, replaced or removed by an operator decision, recorded
      here, before the repository is made public.

- [ ] **VAL-2 — CLI/session equivalence gate at M=200.** End-to-end ω₀ comparison of the 83-scan
      S30 set against `results_gold.csv` **through the CLI/session path**; a multi-minute run
      deferred for a fast machine. Gate on dispositions + safety (0 gold-REJECT resolved) + ω₀
      residuals, **not** confidence (risk R4). Distinct from the **analysis-core 83-scan gold
      gate**, which drives the analysis modules directly and is already passed and committed under
      `docs/assets/phase3_gold/` — passing that one does not close this.
      **Acceptance:** the run completes and the scorecard records 0 safety violations, with the
      result promoted into `docs/ARCHITECTURE.md` §15.

---

## GUI roadmap

Specs in [design/gui.md](docs/design/gui.md) §16. Designed 2026-07-16, not built.

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
- [ ] **CAL-5 — Resolution lineshape by the degenerate-limit rule.**
      Owner ruling (2026-08-20 walkthrough): the elastic-line resolution fit should offer
      {Voigt(free), Gaussian(`lorentz_fwhm`=0), Lorentzian(`gauss_fwhm`=0)} as candidates and let
      evidence selection collapse to the degenerate case — "we checked the Voigt, the evidence
      supports the degenerate case" — instead of always carrying the full Voigt's parameters.
      Never compare free-Voigt against Gaussian as independent rivals when the free fit sits at
      the boundary: near the limit the evidence difference is prior-volume noise, and the honest
      outcome is the pinned candidate (STATS_PRIMER §2, the degenerate-limit rule).
      Touches `isar/calibrate/elastic.py` (always-Voigt today) and the `gauss_fwhm`/
      `lorentz_fwhm` provenance in `isar/schema/steering.py`.
      **Acceptance:** on a synthetic pure-Gaussian resolution, selection reports the Gaussian
      candidate with the Voigt checked-and-collapsed on the record; on a genuine Voigt it keeps
      both widths; no boundary-pinned free fit is ever reported as the winner.

- [ ] **CAL-4 — Γ non-resolvability as a candidate structure, not a failure mode.**
      Owner ruling (2026-08-20 walkthrough): the accepted Γ-at-bound collapse "has become a
      target... reported like a mark of pride, but it's a failure — scientists don't get hung up
      on this, we route around." Add a zero-width (delta ⊗ resolution) candidate to the
      admissible structure set and let evidence selection decide; when it wins, report "Γ not
      resolvable at this resolution" with an upper limit as a positive result. Alternative
      considered (operator's previous program): a repulsive term pushing Γ off the bound.
      Interacts with CAL-3 (the trust gate) and STATS_PRIMER §4.
      **Acceptance:** on the wall exemplars (scan 518 class), the delta candidate is fit,
      selection picks it, and the row reports a Γ upper limit instead of a degenerate interval;
      finite-Γ scans (scan 529 class) are unaffected.

- [ ] **CAL-2 — Bayes-interval coverage campaign.** The `--errors bayes` tier ships the posterior
      as diagnostics-only (ruling S5); before it can enter `_pick_interval` precedence it needs
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
- [ ] **CAL-6 — Worker path ignores the bayes CLI knobs.** `--bayes-nlive/--bayes-dlogz/
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
      Cooper–Nathans projection against TAVI ellipsoids directly. Still open (`docs/ARCHITECTURE.md` §13 Q3).
      **Acceptance:** a D-series decision is recorded.
- [ ] **FIT-1 — LLM guideline proposer.** Follows the `adjudicate.py` pattern: on
      flagged/model_incomplete/unconstrained fits, propose `{structures, peaks, exclude_regions,
      fit_range, reason}`; write `card.state.corrections`; apply only via a guidelines-hash re-fit
      (P1 — no LLM in the runtime path of a number). The fields it edits already exist.
      **Acceptance:** a proposal round-trips into a re-fit without the LLM touching a fitted number.
- [ ] **FIT-2 — Multi-branch dispersion prior.** `PriorPoint` has no branch/mode field, so the
      prior carries the primary mode only.
      **Acceptance:** the prior distinguishes branches and a secondary mode can seed one.

- [ ] **FIT-4 — Amplitude prior constants: sensitivity check not run.** `AMPLITUDE_SCALE_PER_MONITOR = 1.0`
      and `AMPLITUDE_SCALE_PER_SECOND = 1.0e5` (`isar/analyze/prior.py`) are agent-proposed. Their check (the
      example set refitted with one alternative prior width) has not been run, so a count could move with the
      constant unseen before the beamtime. **Acceptance:** the check runs on the example set; any count that moves
      is reported with its spectra, and the constant goes to the operator before the beamtime.
      **Governing:** `docs/STATS_PRIMER.md` §2.1.
- [ ] **FIT-5 — A `[fitting] structures` restriction that matches nothing is silently widened.**
      `scan_delivery.fit_one` keeps every admissible candidate and records one reason ("kept all"), so the scan
      is fitted on the full model set and the scientist's restriction is not applied. The same behaviour is
      written in `Guidelines.filter_structures` (`isar/session/guidelines.py`), which has no caller.
      **Acceptance:** a test with a mistyped label shows the scan is refused or held with its reason, not fitted
      on all models; the choice of refusal or hold is the operator's.
- [ ] **FIT-6 — Pooled masters run rung 2 unsliced.** `master_delivery.py` calls `fit_one` without a deadline,
      so a campaign refresh waits for a master's whole rung 2. **Acceptance:** masters take the rung-2 deadline and
      resume as per-scan fits do, and the refresh's wait is measured.
- [ ] **FIT-7 — A started rung-2 contender is never cut off.** The 120 s campaign slice is checked between
      contenders, so one nested-sampling run can overrun it (measured 2026-10-23: about 250 s median per spectrum at
      ceiling 3). **Acceptance:** the overrun is measured on the example set and either bounded or stated in
      `STATS_PRIMER.md` §2.2.
- [ ] **FIT-8 — Rung 2 at 11 to 14 free parameters is not reliable at 128 live points.** Nested lnZ lay 31 to 54
      nats from its 400-point value while reporting ±1 to 1.5 nats (`count_ladder.RUNG2_N_LIVE`, a shortcut).
      `logzerr ≤ 5` does not bound this bias, so a count decided at high mode numbers may be wrong.
      **Acceptance:** the live-point count at the 6-mode ceiling is measured against a 400-point reference and the
      constant set from it, or contenders at high counts are marked untrusted.
- [ ] **FIT-9 — Undercounts that no trigger sees.** Laplace prices the count above the best 15 to 18 nats too low
      (tuning stream, four priced cases), and three of six wrong counts at ceiling 3 (T0010, T0011, T0012) fire no
      trigger, so rung 2 never sees them. **Acceptance:** a trigger or rule that sends these cases to rung 2,
      tested on the tuning stream, with the figures reported as targets (R15), not tuned to a pass rate.
- [ ] **FIT-10 — Rung-2 cost and escalation rate at the 6-mode ceiling are unmeasured.** Both were measured at
      ceilings 3 and 4, before the background-free twin existed; at ceiling 6 no spectrum finished rung 1 plus
      rung 2 within 576 s, before the fit-core speed-up. **Acceptance:** both measured on the tuning stream at
      ceiling 6, with timings and rates reported.
- [ ] **FIT-11 — Candidate-count record beside the deciding rung.** The per-count record shows each count's
      representative: an unsampled variant, or a partial rung-2 lnZ, can sit beside the deciding rung's
      probability; and a settled rung-2 runner-up probability can include a loser whose refit did not converge,
      which `select_models` leaves out (the count itself is guarded). **Acceptance:** settled with the analyzer
      inspector design; the record names which rung priced each number.
- [ ] **FIT-12 — Rung-2 selection method name.** The rung-2 selection records
      `selection_method = "laplace_evidence_selection"` while its candidates carry `evidence_method = "dynesty"`.
      **Acceptance:** `isar/schema/statistics.py` names a nested-sampling selection method, and the rung-2
      selection records it.
- [ ] **FIT-13 — Row states outside the six ladder states.** `not fitted` and `not recorded`
      (`isar/session/outcome.py`) are not among the six states. **Acceptance:** the operator rules whether they stay
      or map onto the six; `STATS_PRIMER.md` §2.2 and `USING_ISAR.md` follow the ruling.
- [ ] **FIT-14 — The shadow lane and the structure-label filter skip the degenerate-limit rule.**
      `fit_shadow_candidates` does not apply it, and a `structure_labels` filter that drops a twin leaves the railed
      candidate priced as itself. **Acceptance:** both route through the rule, or the gap is stated in
      `STATS_PRIMER.md` §2.1.
- [ ] **FIT-15 — Benchmark figures not yet measured.** The three figures (`STATS_PRIMER.md` §2.3) have no run at
      the design's 200 draws, and the default reference is Laplace, which shares rung 1's blind spot.
      **Acceptance:** a characterisation run on request at a stated draw count, with reference agreement reported
      beside the figures and each figure marked reached or not reached.
- [ ] **FIT-16 — Dead lane and a duplicate.** `evidence.candidate_set_evidence(escalate=True)` (pruning, budget,
      parallel batch) has no production caller, and `evidence.candidate_bundle` repeats
      `select._candidate_evidence`. **Acceptance:** both deleted, with the `select.py` change in the same commit.
- [ ] **FIT-17 — Elastic-line phantom at the endpoint world, H = 2.10.** Operator ruling 2026-10-23, option 3: it
      stays under the central-residual flag (R13) and is revisited after the beamtime. The strict `xfail` in
      `tests/scene/test_worlds_scene.py` stays until then. **Acceptance:** after the beamtime the test passes, or a
      new ruling reclassifies the case.
- [ ] **FIT-18 — Deferred rename, `omega0` to `steer_omega0`.** The column and the `FitOutcome` field `omega0` are
      read by about fourteen modules across analyze, session, drive, report and the GUI; a partial rename breaks
      `isar score` and the campaign diagnostic. **Acceptance:** one packet covers those files, or a ruling makes the
      CSV column alone the rename with its two extra readers. It comes back with the GUI overview work or any change
      that reshapes `results.csv` readers.
- [ ] **FIT-19 — `docs/stats/figures/akaike_example.json` is stale.** It still carries the key `secondary_omega0`;
      `docs/stats/make_figures.py` now writes `second_mode_omega0`. **Acceptance:** regenerated from the archived
      workspace, and the primer's Fig. 3 still matches it.
- [ ] **FIT-20 — A campaign refresh has no refresh-wide rung-2 limit.** Each pending spectrum gets its own 120 s
      rung-2 slice (`isar/session/driver.py`), so that every refresh advances rung 2; with more pending spectra than
      workers the refresh runs in waves (56 pending on 12 workers: up to about ten minutes). **Acceptance:** the
      operator rules between a refresh-wide cap with guaranteed per-spectrum progress and the per-spectrum slice,
      and the refresh wait is measured on the example set.
- [ ] **FIT-21 — A railed Ω_d with a broad Γ resolves to the elastic line.** The degenerate-limit rule
      (`loop._resolve_degenerate`) reads Ω_d at its lower bound as "merge into the elastic line" without checking Γ;
      since ω₀ = √(Ω_d² + Γ²), such a mode is a broad central feature, not a sharp one. Sent up for a ruling
      (it changes amendment 7 and touches the R13 ruling of FIT-17). **Acceptance:** the ruling names the twin
      (elastic, quasi-elastic or none) and the Γ condition, and a test pins it.
- [ ] **FIT-22 — Boundary-mass correction size.** `evidence._boundary_mass_correction` returned +17 to +62 nats in
      4 of 15 seeds when Γ̂ sat just above its floor but was flagged at the limit (repair scan, 2026-10-23), which
      could push count decisions upward. **Acceptance:** the correction is checked against nested sampling on
      such cases and bounded or corrected.
- [ ] **FIT-23 — Retired master-hint leftovers.** `loop.fit_structures_two_stage_hinted` returns an always-False
      flag, `route_out` is a constant, `master_delivery.py` still stamps `structure_skipped`, and comments describe
      a skip-and-fall-back path that no longer exists. **Acceptance:** the hint folds into
      `fit_structures_two_stage` and the rest is deleted (H1), with the next change to `loop.py` or
      `master_delivery.py`.
- [ ] **FIT-24 — Suite time after the count ladder.** The not-slow suite runs 94 to 136 s against the one-minute
      budget, and `tests/session/test_master_fit.py::test_parallel_masters_match_serial_end_to_end` alone takes
      about 9.4 min (rung 2 inside masters). **Acceptance:** the not-slow suite back near a minute and the masters
      test under a few minutes, without weakening what they assert.

---

## Driver and steering

Speedup work is governed by [SPEEDUP_DESIGN.md](docs/plans/speedup.md); steering physics by
the steering roadmap ([design/steering.md](docs/design/steering.md) §10). Only items outside those plans live here.

- [ ] **DRV-1 — Supervisor-model auto-fallback.** Largely closed by moving the supervisor onto the
      LLMM `scientific_interpreter` role (2026-09-01): the role store owns the shortlist
      (glm-5.2 → deepseek-v4-pro → kimi-k2.5, `fallback_behavior: warn`) and LLMM walks it, so no
      hand-pinning is needed. `LLMM_SUPERVISOR_MODEL` survives only as a debugging override.
      **Remaining:** observed rather than assumed — no real primary outage has exercised the chain
      yet, and ISAR discards LLMM's `fallback_level` / `routing_warning` instead of recording which
      model actually designed the campaign.
      **Acceptance:** a 503 from the primary supervisor model does not require operator action, and
      the session log names the model that answered.
- [ ] **DRV-2 — Objective-aware Γ exclusion.** Acoustic ω₀ → 0 at zone centers is structurally
      unmeasurable; parse-time exclusion beats spending ~4 scans per campaign discovering it.
      **Acceptance:** a zone-center q is excluded at objective-compile time with the reason recorded.
- [ ] **DRV-3 — TAVI runtime and McStas validation rungs.** (a) Realistic clocks — counting time reported as the
      instrument would report it (TAVI-side virtual clock), exercising wall-clock budget semantics
      (§8, O2); today `budget.spent` sums TAVI's *compute* eta, which is the root cause of the
      MVP16 "10.6 of 28 800" (a semantics gap, not units). (b) The McStas rung. The deterministic
      rung ran 2026-07-10.
      **Governing:** T1/T2 as amended by O36, `docs/ARCHITECTURE.md` §9. **Acceptance:** a campaign runs
      against a virtual clock with honest budget accounting; the McStas fidelity rung completes;
      the corresponding TAVI validation row in `docs/ARCHITECTURE.md` §15 reflects completion.
- [ ] **DRV-4 — Confirm-churn intent confirmations** (SVP_RECERT, still open): `n_uncertifiable`
      never resets, so a chronically flaky q is never abandoned; and triage should show confidence
      beside `(unreliable)`. Related: a q with its half-width right at tolerance re-confirms
      forever (first run: H=2.1 took 22 confirm fits, H=2.15 took 12, vs 1 each for well-inside
      q). Capping confirms per q is the cheap half of the fitting-bound problem — the per-fit
      half is [SPEEDUP_DESIGN.md](docs/plans/speedup.md).
      **Acceptance:** a chronically flaky q is abandoned by policy, and triage output shows both.

- [ ] **DRV-5 — EIG-4 Slice 10 deterministic TAVI demonstration.** Slice 9 implements the
      O41-restricted authority path but does not itself supply the production demonstration. Run the
      maintained GUI-seeded positive case through deterministic TAVI and exercise an ineligible
      return to the ordinary non-EIG path. H1 retired the O42 release review, so this is validated
      by the ordinary deterministic world-suite lanes, with no release-review ceremony.
      **Acceptance:** one narrower target-selected scan proves exact scored/executed grid agreement,
      fewer executed points, normal fit/campaign rejoining, and a separate ineligible case returns
      to non-EIG behavior, all under the ordinary deterministic lanes.

- [ ] **DOC-1 — Promote the remaining disposable-path evidence.** The three validation reports were
      committed under `docs/assets/` on 2026-07-25 with a provenance manifest, and `README.md`,
      `READING_GUIDE.md` and `WORKED_EXAMPLE.md` now cite them there.
      One citation remains unpromotable and is the residue of this item: `STATS_PRIMER.md` points
      at `out/mvp_zone_test16` ("ask the maintainer for its blessed location"), the frozen v1
      characterization baseline ruling W1 archives but which is not in this checkout.
      (the retired unification plan's step-0 baseline — git history — also needs the never-generated
      `out/phase3_gold_in8/`, but that file is now `Archived` — a historical design record may
      name the paths it used, so it is no longer part of this item.)
      **Acceptance:** it is either promoted into `docs/assets/` with a manifest entry, or
      rewritten to stop presenting an unavailable artifact as the reader's next step.

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
      `<workspace>/report/report.json` / `card.state.json` directly (the stable file contract). Revisit an
      `isar query` or HTTP surface once the file contract chafes. Deliberately not now.
      **Acceptance:** revisited once a second consumer exists.
- [ ] **PLAT-2 — Watch/daemon mode.** Sugar wrapping `run_batch` (U1). At real-instrument scale
      per-scan cadence may also motivate incremental parsing; the batch re-parse is fine at
      simulation scale.
      **Acceptance:** a watched folder triggers incremental runs without a re-parse of the batch.
- [ ] **PLAT-3 — Per-analyzer S30 resolution** (risk R1). Measured widths exist only for Ana7;
      others fall back to `default_resolution` with a provenance flag.
      **Acceptance:** per-analyzer widths are measured and used, with provenance recorded.
- [ ] **PLAT-4 — New instrument card: ID28.** Registry slot exists; authoring the card is a small
      later task. (BL43LXU landed 2026-10-21, PRs #44 and #45.)
      **Acceptance:** it parses a real file end-to-end.
- [ ] **BL43-1 — SIXCIRCLE geometry: HKL rungs 2 and 3.** Per-analyzer HKL computed from sample
      motor angles plus orientation (rung 2) and from the arm-centre HKL plus array geometry (rung 3),
      following the beamline's SIXCIRCLE code once the operator has it. The 2017 FeSe `pca6`
      printout (OneDrive `2017_05_FeSe_SP8\Lab Notebook\DSC00174.JPG`) is its test case.
      **Acceptance:** the 24 HKLs of that printout are reproduced from its angles.
- [ ] **BL43-2 — Momentum resolution in the fit.** Slit and `dq` are recorded today; fold the
      per-analyzer Q spread into the fit after BL43-1.
      **Acceptance:** a steep branch's fitted width accounts for slope × `dq`.
- [ ] **BL43-3 — Pooled masters with the tabulated resolution.** Individual analyzers fit today;
      a pooled master needs one curve and zero valid for every member.
      **Acceptance:** a BL43 master fits with a member-consistent curve and zero.
- [ ] **BL43-4 — Shared parts adopted by other instruments.** S30, IN8 and TAVI opt into the
      resolution library, fact sources and count recovery (operator, pinned 2026-10-21).
      **Acceptance:** an S30 card uses the resolution library with no code change.
- [ ] **PLAT-7 — Scan identity and deduplication across instruments.** Project → experiment →
      scan hierarchy with the experiment given per batch by the operator; duplicates checked as
      bit-identical files, then identical values; ISAR combines individual scans itself (operator,
      2026-10-21). Must keep the growing-file resend case that ingest's last-wins rule serves today.
      **Acceptance:** the same scan arriving by two paths is recognised, and a collision is never
      silently merged or superseded.
- [ ] **PLAT-5 — Batch re-analysis driver.** Re-run everything post-beamtime from frozen config at
      better resolution (`docs/ARCHITECTURE.md` §14 item 8).
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

`docs/ARCHITECTURE.md` §9. Everything here stays off the critical numerical path (P1).

- [ ] **LLM-1 — Structured-output IC / peak-structure proposal** (T2/T3), seeded by the dispersion
      prior first, propose-and-freeze per P1.
      **Acceptance:** proposals freeze into config before the deterministic fitter runs.
- [ ] **LLM-2 — Residual-diagnosis pass on failed fits.** Vision tier, off the critical path,
      after deterministic attempts.
      **Acceptance:** a failed fit gets a recorded diagnosis that changes no number.
- [ ] **LLM-3 — Richer review surface / correction channel** beyond card-edit + re-run
      (`docs/ARCHITECTURE.md` §10). The v1 channel works — exercised live 2026-07-03 with a temperature fix
      plus seeded expectation, re-fitting exactly the affected scans.
      **Acceptance:** a correction can be made without hand-editing the card.

---

## Wishlist

Smaller items, mostly GUI polish; fold into other work where convenient. No acceptance
conditions — these are wants, not commitments.

- [ ] **WISH-4** — Master scans report the total neutrons spent, i.e. what the result actually cost.
- [ ] **WISH-5** — User-facing log cleanup, e.g. no separate "start"/"completed" lines.
- [ ] **WISH-6** — Connecting to TAVI is slow with a new workspace, though it was connected before.
- [ ] **WISH-7** — If a folder doesn't exist when saving a workspace, create it.
- [ ] **WISH-8** — Data-point error bars should be Poisson-correct and asymmetric: a count of 1
      gives far smaller error bars on the negative side than the positive side.
- [ ] **WISH-10** — Use crystallographic energy to get Brillouin-zone sizes (not small).
- [ ] **WISH-11** — Keep the GUI updated as scans finish, not only as fits finish.

---

## Known better shapes

- `isar/session/calibrations.py:42` — the shared `tabulated_elastic` method calls the BL43 SARF
  reader directly and fit references carry two shapes; move the generic library code into
  `isar/analyze/resolution.py`. Revisit when the first non-BL43 card sets `tabulated_elastic`.
- `isar/session/calibrations.py:284` — slit and zero alerts appear only in the inspector; print one
  context line per calibrated scan in `isar run`. Revisit on the next calibration-output edit.
- `isar/calibrate/tabulated.py:188` — the elastic-zero fit and slit ladder read spectrum exposure
  directly instead of `exposure_for`. Revisit when another instrument enables the method.
- `isar/session/ingest.py:188` — the summed-set rule has two implementations with different
  experiment keys; leave ledger-held singles to `supersede_summed`. Revisit with PLAT-7.
- `isar/parsers/sp8_bl43.py:174` — trailing `#Q=`/`#T=` values are folded into `header`; return
  them as their own `trailer` source. Revisit when the macro first writes leading `# T_K`/`# Q_nm`.
- `isar/parsers/sp8_bl43.py:517` — `points_per_bin` is stored as a per-bin fact in every ledger
  entry; carry it beside the counts. Revisit if a BL43 session.json passes a few MB.

---

## Notes

routine suite 2.0 min, over its 1-min budget

**GUI test isolation.** GUI tests stub the backend health probe offline via an autouse fixture in
`tests/gui/conftest.py`. Kept deliberately: without it, constructing any `MainWindow` in a test
fires real `urlopen` calls at the operator's live LLMM/TAVI ports, so results would depend on which
services happen to be running.


User idea: some sort of Brier-score certification of how well targeted campaigns run, given a known intensity distribution.
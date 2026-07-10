# ISAR — ongoing TODO

Living list. Grouped by theme, roughly dependency-ordered within each group.
Design references: `DESIGN.md` (master), `docs/UNIFICATION_DESIGN.md` (current
phase, §10 gates / §11 risks / §12 non-goals), `docs/READING_GUIDE.md`, and the
closed-loop system capstone in the TAVI repo
(`..\TAVI\docs\CLOSED_LOOP_DESIGN.md` — TAVI = instrument, ISAR = analysis
authority, driver = future). Last updated: 2026-07-07.

## Deferred validation gates (unification §10)

- [ ] **Restore the IN8 gold CSV** (`test_data/ILL IN8/IN8_fit_results.csv`) — missing
      from disk, gitignored, cannot be restored from git; only 055182 ≈ 4.2 meV and
      055183-REJECT are vetted rows. Blocks `docs/phase3_gold_in8_check.py`,
      `tests/real_data/test_in8_gold_smoke.py`, and every IN8 gate. (§10 step 0.)
- [ ] **Full S30 statistical comparison at M=200** — end-to-end ω₀ comparison against
      `results_gold.csv` through the CLI; multi-minute run deferred for a fast
      machine. Gate on dispositions + safety (0 gold-REJECT resolved) + ω₀ residuals,
      not confidence (risk R4). (§10 step 5.)

## σ-calibration (closed-loop Phase 0 — highest-value near-term)

- [x] **Interval-coverage scoring against known truth** — home built: `isar score
      --workspace DIR --truth-fn phonon_dft` (or `--truth-csv PATH`), mutually exclusive
      with `--gold` (`isar/analyze/truth.py` + `isar/cli/score_cmd.py::run_truth`). Reports
      68% interval coverage, ω₀ pull, median |Δ|, the suggested INFLATION FACTOR k (68th
      percentile of |Δ|/halfwidth), and — separately — the signed center-pull mean that
      inflation cannot fix; `--bucket-by COL` groups by a results.csv column, `--json PATH`
      dumps the full dict for the campaign. Truth is analytic (no safety concept → exit 0).
      `phonon_dft` mirrors TAVI PhononSQW (acoustic E = 6·sqrt(Σ sin²(πq_i/2)) meV, period-2
      fold, zone centers at even integers).
- [x] **First-pass campaign run (2026-07-06)** — `docs/sigma_calibration_campaign.py`, 99
      known-ω deterministic scans; artifact `calibration/phonon_dft_deterministic.json` +
      findings in `calibration/README.md`. Coverage68 = 0.02, inflation ~14 (unstable by
      ncount tier — systematic-dominated), center pull +0.12 mean rising to +0.5 at the zone
      boundary. High-q pull diagnosed: ~40% optic-branch window leakage (fixed by N-mode +
      masking below), ~60% 1-D resolution asymmetry (tas4d ceiling).
- [ ] **Calibration curve v2** — re-run the campaign with the N-mode fitter + wide windows;
      derive per-configuration inflation. Known issue: 2-mode fits with γ at its bound report
      absurdly tight intervals (halfwidth ~1e-3 meV) that dominate the inflation metric —
      interval calibration for γ-at-bound fits is a prerequisite.

## TAS resolution

- [ ] **4-D resolution convolution (`tas4d`, Phase 3 D5 / risk R8)** — Cooper–Nathans
      tilted-ellipsoid + local dispersion slope (ResLib/Tobyfit-style). Unblocks
      intrinsic Γ on TAS (currently `gamma_provisional` always). Evidence of need
      from the first closed-loop scan: measured AS/S intensity ratio ≈ 0.52 vs
      Bose-tied ≈ 0.92 — a focusing asymmetry the 1-D width cannot model; the fit
      correctly lands `model_incomplete/flagged` and can do no better until this.
- [ ] Decide: wrap an existing engine vs implement the CN projection ourselves
      (DESIGN.md §13 Q3, still open).

## N-mode fitting + fit masking (built 2026-07-06) — fast-follows

The deterministic core landed (1527d25/611e74c/65f90ce): instance-prefixed multi-DHO
hypotheses, two-stage data-gated enumeration (GATE_COST=2.0 OR runs-test) with AIC/BIC
disposing, greedy residual IC seeding, card guideline fields `fit_range`/`exclude_regions`/
`peaks` masking the fit (P5: mask only), additive results.csv columns (n_modes,
secondary_omega0[_err]). Live-verified: wide-window zone-boundary Δω₀ +1.0 → +0.14/+0.06,
data-driven, secondary lands on the optic; exclude_regions reproduces narrow-window fits via
card edit with exact-scan re-fit; S30 gold unregressed.

- [ ] **LLM guideline proposer** — adjudicate.py pattern; on flagged/model_incomplete/
      unconstrained fits proposes {structures, peaks, exclude_regions, fit_range, reason};
      writes card.state.corrections; applies only via guidelines-hash re-fit (P1). The
      fields it edits now exist.
- [x] **Greedy-IC seed robustness** — DONE. Diagnosis: the greedy seed was never the problem
      (it placed both modes correctly); the k=7 two-mode fit reached a good small-EDM minimum but
      exhausted MIGRAD's DEFAULT call budget (~1145 at k=7) first, so `has_reached_call_limit`
      fired, `m.valid` went False, and the (good) 2-mode fit was dropped from selection → fell
      back to 1-mode. Fix: `backends._migrad_ncall` = a modest 4× of MINUIT's default ceiling —
      certifies the well-constrained k=7-8 fits (need ~1400-2000 calls) while the cutoff still
      rejects non-identifiable overfits (the IN8 `stokes2+qe+bg` limps ~13k calls). All 3 failing
      seeds now select 2-mode (primary Δ +0.003/+0.003/−0.056 meV), the 6 good seeds stay 2-mode,
      IN8 anchor still recovers 4.49, single-peak stays 1-mode.
- [x] **γ-at-bound interval calibration** — DONE (honest-flag route). A phonon Γ railed to its
      floor (within 5×) is a resolution-limited δ-spike → the bootstrap ω₀ interval collapses to a
      spurious ~1e-3 meV halfwidth. `loop._gamma_at_bound` flags it; `StructureFitResult
      .gamma_at_bound` → `FitOutcome.interval_reliable` → results.csv `interval_reliable` column;
      `isar score --truth` excludes flagged rows from coverage/inflation but still scores their
      center pull. An honestly-flagged unreliable bar beats a silently-repaired one.
- [ ] **Multi-branch dispersion prior** — PriorPoint has no branch/mode field; prior gets
      primary mode only today.
- [ ] **N=3** — mechanical extension once 2-mode is field-proven.

## `tavi` plugin follow-ups

- [x] Per-scan payload truth consumed (2026-07-06, 3b8b8e2): temperature, ETAM/ETAA/ETAS,
      senses, BET1..4, sample_key — payload-wins with card fallback + provenance flag.
- [ ] **2D scans** — currently a clear refusal; support when a use case appears
      (path scans / campaigns on the TAVI side may produce them).
- [ ] Per-sample card sections / sample-keyed collation (sample_key is recorded in
      raw_header only; collation still keys on the card experiment_id).

## LLM layer (DESIGN.md §9, build order 6)

- [ ] **Structured-output IC / peak-structure proposal** (T2/T3, seeded by the
      dispersion prior first; propose-and-freeze per P1).
- [ ] **Residual-diagnosis pass on failed fits** (vision tier, off the critical path,
      after deterministic attempts).
- [ ] Richer review surface / correction channel beyond card-edit + re-run
      (DESIGN.md §10; v1 channel works — exercised live 2026-07-03 with a
      temperature fix + seeded expectation re-fitting exactly the affected scans).

## Driver (Phase 4)

Decisions settled 2026-07-06:

- [x] Driver lives in `isar/drive/` (`isar drive` CLI subcommand), not a separate
      repo. Consumes analysis only via the `isar run` subprocess + file contract
      (`report/results.csv`, `report.json`, `card.state.json`); never imports fit
      internals.
- [x] σ-calibration first pass runs on the TAVI deterministic tier
      (`calibration/phonon_dft_deterministic.json`, v2 2026-07-06).
- [x] MVP targets: ω₀ only, acoustic branch only.
- [x] Supervisor: cloud LLM via LLMM pinned routing.
- [x] **First end-to-end autonomous run (2026-07-07)** — NL goal → 21-point campaign →
      calibrated stop decision, zero human numbers. Five live-run defects found + fixed
      (lazy `isar init`, latin-1 header, anti-Stokes recenter, quicklook HESSE model seed,
      per-q stuck semantics). Full writeup: `docs/campaigns/2026-06_MVP_FIRST_RUN.md`.
- [ ] **Production-fit latency — NEXT TEST.** Scans are near-instant on the deterministic
      tier; each production/confirm fit runs the full bootstrap layer (~1–2 min), so a
      campaign (~50+ production-tier fits) is fitting-bound. Two levers, cut fits before
      you speed up each fit:
      1. **Confirm churn.** A q with its half-width right at tolerance re-confirms forever
         (first run: H=2.1 = 22 confirm fits, H=2.15 = 12, vs 1 each for well-inside q).
         Cap confirms per q / abandon a q once *n* confirms fail to seat.
      2. **Per-fit speed.** Fewer bootstrap replicates gated on the tolerance actually
         needed, profile likelihood instead of bootstrap, warm-start production fits from
         the quicklook result, batch fits across pending scans.
- [ ] **Supervisor-model auto-fallback** — gpt-oss-120b throws transient 503s; today the
      operator pins `LLMM_SUPERVISOR_MODEL` by hand (deepseek-v4-pro:thinking is the proven
      backup). Fall back automatically down a probed shortlist.
- [ ] **Objective-aware Γ exclusion** — acoustic ω₀→0 at zone centers is structurally
      unmeasurable; parse-time exclusion beats spending 4 scans discovering it per campaign.
- [ ] **The SVP: TAVI-as-backend campaign with realistic clocks** (T1/T2; DESIGN.md §9 —
      "next milestone"). Same objective and driver as the analytic certification, TAVI
      answering scans, counting time reported as the instrument would report it, so the
      wall-clock budget semantics (§8, O2) are exercised before a real night. Prereq:
      verify the counting-seconds accounting first (MVP16 defect: 10.6 s of 28 800 after
      85 scans looks like a units bug).

## Docs follow-ups (consolidation drop 2026-07-09)

- [ ] **READING_GUIDE.md refresh + vocabulary sweep** (V1–V4) — review→collate in prose,
      status tags in USING_ISAR etc., and update the out/-path narrative (gold report now
      regenerated via `docs/*_check.py`; blessed baselines live outside the repo at
      `C:\Users\AMM\Documents\ISAR_baselines\`). Pair with whichever agent builds the
      campaign-diagnostic renderer — the diffs overlap.

## Deferred findings from the pre-merge branch review (2026-07-09)

Verified by the model-free-foundation review but deliberately NOT fixed pre-merge —
each changes campaign behavior that the v16 scorecard PASS validated, so they need a
re-run of the certification campaign (or targeted tests) alongside the change.

- [ ] **Matched-descriptor rescue can deliver confident-wrong** — `verdict.classify()`
      lets a family-wise-significant descriptor peak that the winning model happens to
      explain bypass the no-excursion gate straight to `resolved` (the deliberate high-q
      rescue). Reachable false-positive path (AIC is permissive; confidence has no peak-
      significance factor); no classify()-level test covers flat-but-matched → resolved.
      Add a characterization test + consider a minimum peak-significance floor on the rescue.
- [ ] **`stokes_only` gate is prior-only** — `structures.antistokes_mode()` decides to
      drop the anti-Stokes side purely from `ctx.prior_omega` (populated only when branch
      σ ≤ 0.6 meV, so a *confidently wrong* posterior triggers it); no data-driven
      cross-check once dropped, and no test with a wrong prior.
- [ ] **`build_beliefs()` doesn't filter by fit_kind** — contract 4 ("constrained fits
      never seed a branch") is enforced for Γ/A via `_is_clean_anchor` but NOT for the
      ω₀ assignment belief: a `branch_conditional` delivery (gated only by the weaker
      `constrained_ok` bar: p_acoustic ≥ 0.6, no dominance/entropy check) feeds later
      assignment posteriors. Narrow but real self-reinforcement channel.
- [ ] **`branch_continuation` mislabeling loses the recenter/widen retry** — fit-derived
      features get `float("inf")` significance in `features_from`, so a garbage fit on an
      out-of-window scan can be triaged `branch_continuation` (not counted missed) and the
      q burns its 3-decline budget instead of recentering. Right fix is honest significance
      for fit-derived features, not re-including branch_continuation in `_rows_missed`.
- [ ] **`summarize_grid` None→acoustic fallback is untested** — deliberate (posterior-off /
      legacy ledgers) but nothing locks down either mode; add tests for branch_id=None
      vs "optic" vs "acoustic" rows.
- [ ] **Centralize steering string constants** — fit_kind literals ("unconstrained",
      "soft_prior", "branch_conditional", "stokes_only", "descriptor_only") spread over
      8 files, and "acoustic"/"optic" duplicated as two independent `BRANCH_ID` constants
      (branch_model.py, branch_view.py) + literals in 4 more files. Home: `isar/schema/steering.py`.
      Also: the `f"{scan_id}#{branch}{i}"` banked-anchor convention needs a shared
      construct/parse helper before anything else consumes `dispersion_prior[*].scan_id`.
- [ ] **Acquisition hot-path caching** — per iteration: `fisher()` does one blocking TAVI
      `get_resolution` HTTP call + 11 forward-model evals per candidate with no memoization
      (~21 candidates); `_grid_state` fully re-reads results.csv + re-scans all observations
      2×/iteration (`match_grid_index` is a linear scan). Memoize resolution by (hkl, ΔE),
      cache parsed results.csv by mtime, index the grid path.
- [ ] **`build_beliefs` O(n²) rebuild** — reconstructed from the full dispersion_prior on
      every scan and master fit; cache per (sample, t_bin) within a `run_batch`.
- [ ] **Worker keeps no state resident** — persistent worker still round-trips full
      card.state.json + session.json per job (O(n²) I/O over a campaign). Residency needs
      an explicit single-writer contract or a workspace lock first (today's always-reload
      is what makes mid-campaign human card edits safe).
- [ ] **`steering.py` dataclasses on the generic `_json` codec** — needs parameterized
      annotations first (`tuple[DescriptorPeak, ...]`), then drop the hand-rolled
      to_dict/from_dict.
- [ ] Minor: share `_linfit` (branch_model) with `session_prior.prior_for`'s inline polyfit;
      unify `session_log.timed()` / `StageTimer.stage()` if the sink semantics ever converge;
      `_errors_arg_for` is production-dead (test-only callers); worker `_reconfigure_stdio`
      duplicates cli/main's (blocked by the worker import boundary — needs a lower-layer home).

## Platform

- [ ] **Query API for downstream consumers** — the driver currently reads
      `report/results.csv` / `report.json` / `card.state.json` directly (the stable
      file contract). Revisit an `isar query` or HTTP surface once the driver exists
      and the file contract chafes. (Deliberately not now.)
- [ ] **Watch/daemon mode** — sugar wrapping `run_batch` (U1); after the driver
      exists, per-scan cadence at real-instrument scale may also motivate
      incremental parsing (the batch re-parse is fine at simulation scale).
- [ ] **Per-analyzer S30 resolution** (risk R1) — measured widths only for Ana7;
      others fall to `default_resolution` with provenance flag.
- [ ] **Migrate the check scripts onto the driver** — only after step-5 equivalence
      is proven; until then they are the validation record (§12).
- [ ] **New instrument cards: ID28, BL43LXU** — registry slots exist; authoring the
      cards is a small later task.
- [ ] **Batch re-analysis driver** — re-run everything post-beamtime from frozen
      config on better resolution (DESIGN.md §14 item 8).

## Environment note

`tavi-dev` (micromamba) now carries `iminuit`, `lmfit`, `dynesty` (installed
2026-07-03); full not-slow suite passes there (469). Invoke via
`micromamba run -n tavi-dev python -m pytest -m "not slow"` — the bare
interpreter misses DLL paths on this machine.

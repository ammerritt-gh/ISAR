# ISAR — ongoing TODO

Living list. Grouped by theme, roughly dependency-ordered within each group.
Design references: `DESIGN.md` (master), `docs/UNIFICATION_DESIGN.md` (current
phase, §10 gates / §11 risks / §12 non-goals), `docs/READING_GUIDE.md`, and the
closed-loop system capstone in the TAVI repo
(`..\TAVI\docs\CLOSED_LOOP_DESIGN.md` — TAVI = instrument, ISAR = analysis
authority, driver = future). Last updated: 2026-07-03.

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

- [ ] Driver lives in `isar/drive/` (`isar drive` CLI subcommand), not a separate
      repo. Consumes analysis only via the `isar run` subprocess + file contract
      (`report/results.csv`, `report.json`, `card.state.json`); never imports fit
      internals.
- [ ] σ-calibration first pass runs on the TAVI deterministic tier.
- [ ] MVP targets: ω₀ only, acoustic branch only.
- [ ] Supervisor: cloud LLM via LLMM pinned routing.

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

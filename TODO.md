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

- [ ] **Interval-coverage scoring against known truth** — the TAVI closed loop provides
      analytic ground truth (Phonon_DFT: acoustic E = 6·sin(πq/2) meV, T = 200 K);
      run known-ω scans, measure empirical coverage of `interval_lo/hi`, derive the
      inflation factor/calibration curve. First live data point already exists:
      ω₀ = 1.494 ± 0.035 vs truth 1.400 — truth outside the 68% interval.
      Possible home: an `isar score` mode that takes a truth function/table instead
      of a gold CSV. Directly addresses the statistical-only error-bar limitation
      (READING_GUIDE "when not to trust ISAR"); separates bar-optimism from any
      resolution pull on the fitted center.

## TAS resolution

- [ ] **4-D resolution convolution (`tas4d`, Phase 3 D5 / risk R8)** — Cooper–Nathans
      tilted-ellipsoid + local dispersion slope (ResLib/Tobyfit-style). Unblocks
      intrinsic Γ on TAS (currently `gamma_provisional` always). Evidence of need
      from the first closed-loop scan: measured AS/S intensity ratio ≈ 0.52 vs
      Bose-tied ≈ 0.92 — a focusing asymmetry the 1-D width cannot model; the fit
      correctly lands `model_incomplete/flagged` and can do no better until this.
- [ ] Decide: wrap an existing engine vs implement the CN projection ourselves
      (DESIGN.md §13 Q3, still open).

## `tavi` plugin follow-ups (as TAVI's payload grows)

Each is a card constant today; switch to per-scan payload truth when TAVI exposes it
(tracked on the TAVI side in `..\TAVI\TODO.md`):

- [ ] Sample temperature (enables real T-collation + sensor-divergence QC).
- [ ] Mono/ana/sample mosaics (ETAM/ETAA/ETAS — directly set CN width).
- [ ] Scattering senses (SM/SS/SA).
- [ ] Vertical collimations (BET1..4).
- [ ] **2D scans** — currently a clear refusal; support when a use case appears
      (path scans / campaigns on the TAVI side may produce them).
- [ ] Sample selection will appear in the payload once TAVI exposes it — consider
      per-sample card sections or sample-keyed collation then.

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

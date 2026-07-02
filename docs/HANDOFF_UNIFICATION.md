# ISAR Unification — Handoff for the build session

This briefs a fresh Claude Code session (full repo access, can run Python) to build the
unification phase: the `isar` CLI + session layer that assembles the validated library into an
end-to-end product. Keep it short; the spec lives in the design doc, which is authoritative.
**When this doc and `docs/UNIFICATION_DESIGN.md` disagree, the design doc wins** — tell the human
about the conflict.

## Read first (authoritative, in order)
- `docs/UNIFICATION_DESIGN.md` — **the spec for this build. Follow it.** §10 is the build order,
  §9 the settled decisions (U1–U8), §0 the five framing corrections that shape everything,
  §11 the known risks (R1–R8) with their mitigations.
- `DESIGN.md` — master design, for where this sits: it realizes §3 (architecture spine), §4
  (event stream), §7.1 (ingestion), the accumulated-state half of §6, and the minimal §10
  correction channel. It is build-order items 5–7 of §14.
- `docs/phase3_gold_check.py` and `docs/phase3_gold_in8_check.py` — **the recipes the driver
  generalizes.** Read their per-scan pipelines closely (`run_pipeline`, the zero/resolution
  handling, the LOO `predict_omega` prior, the IN8 `tas_context`/monitor/undersampled guards).
  The driver makes the same calls in the same order; only the *inputs* change (card/state/derived
  instead of hardcoded).
- `docs/USING_ISAR.md` — the user-facing contract being built toward; the fresh-eyes gate (§10
  step 7) is "this document is true."

## The one-sentence task
Build `isar init / run / status / report / score` over a new `isar/session/` package (workspace,
ledger, ingest, event router, calibration wiring, driver, prior builder, guidelines) so that both
`test_data/APS S30` and `test_data/ILL IN8` flow end-to-end through the SAME driver on card policy
alone — and so a new user with a new file gets a triage report following only what `isar init`
prints.

## What already exists (build on it, do NOT duplicate or modify)
The entire pipeline exists as validated library code. **This phase adds no new physics and no new
statistics** (design §0.1). Call these; never fork them:
- Parse: `isar.parsers.registry.detect/plugin_for`, plugins `spec_s30`, `spec_s30_raw`, `ill_in8`;
  `plugin.parse_file(path, card) → list[ScanRecord]`; `card_template()`.
  **Caveat:** `registry.REGISTRY` starts empty — nothing in the package registers the real plugins
  today (the check scripts import parsers directly; only a test registers a dummy). Wiring
  registration (imports + `register()` calls, e.g. in `isar/parsers/__init__.py`) is part of build
  step 2 and is an allowed additive change; until then `plugin_for()` raises `KeyError` on
  everything and only `detect()` works standalone.
- Reduce: `isar.reduce.qc.qc_record(record, card)` (never raises),
  `isar.reduce.combine.reduce_record(record, card)` (no-op where N/A),
  `isar.reduce.collate.collate(records, card)` (self-filters to measurements).
- Adjudication (opt-in only): `isar.reduce.adjudicate.adjudication_config(card)`,
  `make_client(cfg)`, `adjudicate_records(records, card, client)`. Note the name — there is no
  `adjudicate_run`.
- Calibrate (**currently orphaned — wiring it in is the point**):
  `isar.calibrate.bragg.read_elastic_scans(spec_path, analyzer=..., k_max=...)`,
  `isar.calibrate.energy_zero.build_energy_calibration(scans, key=...)` → `.zero(key)` (returns
  `None` out of coverage — never fabricate 0.0) and `.resolution(slit)`.
  Cooper-Nathans lives in **`isar/synth/resolution.py`** (`cn_energy_fwhm`); add the
  `isar/calibrate/tas.py` re-export rather than moving it.
- Analyze (the validated spine — treat as read-only):
  `ScanContext.from_scan(record, detector_id, card, *, center, gauss_fwhm, lorentz_fwhm,
  prior_omega=None)` → `structures.enumerate_structures(ctx)` →
  `loop.fit_admissible_structures(ctx, minos=True)` → `errors.attach(result, hyp, ctx, M=, seed=,
  with_profile=True)` on survivors → `select.select_models(results)` →
  `verdict.classify(results, ctx, sel, prior=)` (+ `verdict.held_for_calibration(reason)` for the
  held bucket) → confidence. `PhononPrior` is in `isar/analyze/prior.py` — consumed there, built
  ONLY in the new `session_prior.py`.
- Report: `FindingsDoc` (`isar/report/findings.py`), `captions.save_captioned`,
  `figures.coverage_figure`, `build.generate_report` (keep as the reduction-detail companion).
  The fit-overlay/dispersion figures exist only inside the check scripts — factor them into
  `isar/report/fit_figures.py`, don't rewrite them.

## Real test data (the gates run against these)
Note `test_data/` is entirely gitignored (facility data is not committed) — everything below is
machine-local and cannot be restored from git.
- **S30:** `test_data/APS S30/` — `FeSe_1-xS_x_1.spec_supplementary.dat` (measurements) **and**
  `FeSe_1-xS_x_1.spec` (companion raw SPEC holding the K≈0 elastics the zero calibration needs);
  `FeSe_1_fit_results.csv` (gold, **used only by `isar score` and the step-5 comparison**).
  **Trap:** the directory ALSO holds a second experiment (`FeSeS_1.spec_supplementary.dat`, ~56
  scans, same detected format) and two more raw `.spec` files. The step-5 gate must therefore run
  on **explicit files** with `companion_glob = "FeSe_1-xS_x_1.spec"`, not on the bare directory —
  see design §10 step 5.
- **IN8:** `test_data/ILL IN8/` — numor files (`0551xx`/`0552xx`); `.nxs` twins must be *skipped
  gracefully, listed not fatal*. **Currently missing:** `IN8_fit_results.csv` (anchor 055182
  vetted-recover ≈ 4.2 meV, 055183 vetted-reject; the other rows were Phase-1 placeholders) is not
  on disk, so `docs/phase3_gold_in8_check.py` and `tests/real_data/test_in8_gold_smoke.py` cannot
  run and `out/phase3_gold_in8/` cannot be generated. **Ask the human to restore it before
  starting** — every IN8 gate depends on it (design §10 step 0).
- Reference outputs to compare against: `out/phase3_gold/results_gold.csv` (present),
  `out/phase3_gold_in8/results_in8_gold.csv` (absent until the CSV above is restored; regenerate
  via the check scripts — `out/` is gitignored).

## Build order (from design §10; gate each step before the next)
1. **Schema + workspace + ledger** — `CardPolicy` += `calibration`/`fitting`/`sample` dicts;
   `CardState` += `resolution_versions`/`energy_zero_series`/`dispersion_prior`/`sample_map`;
   `session/workspace.py`, `session/state.py`. Gate: TOML sections survive the round-trip (the
   `_json.from_dataclass` silent-drop trap), old state files load, unknown-section warning fires.
2. **Ingest + router + CLI skeleton** (`init`, `status`; `[project.scripts]` registered; project
   renamed `isar-synth` → `isar`). Gate: `isar init` scaffolds correct cards for both instruments;
   dedup/ordering/gap detection proven on `spec_s30.emit_file` synthetics.
3. **Calibration layer** (`session/calibrations.py`, `[calibration]` card sections). Gate: matches
   the gold check's own numbers; **record-derived slit (`raw_header["P"]["17"][4]`) == gold-CSV
   slit for all 83 scans**; IN8 `cn_energy_fwhm` matches recorded `res_gauss_fwhm`.
4. **Prior builder + guidelines** (`session_prior.py`, `guidelines.py`). Gate: unit tests per
   design §10.4 (fork exclusion, LOO reproduction on gold rows, never-empty structure filter,
   user-beats-auto).
5. **Driver + `isar run`** — THE gate: S30 (explicit files, see data section) same held set /
   **0 safety violations** / ω₀ median |Δ| ≲ 0.05 meV; IN8 anchors; idempotency (re-run fits 0;
   two-half == one-shot; deleted session.json rebuilds); bootstrap gate (no companion `.spec` →
   all held_for_calibration; add it → re-examined and fitted); corrections gate (add/remove one
   guideline override → exactly that scan refits, via the ledger `guidelines_hash`).
6. **Run report + `report`/`status`/`score`.** Gate: both instruments render with zero instrument
   branches; `isar score` reproduces the gold confusion counts.
7. **Fresh-eyes walkthrough + regression.** All four `docs/*_check.py` run unmodified; `pytest`
   green; the `README.md`/`docs/USING_ISAR.md` quickstart is followed literally and works.

## Load-bearing constraints (settled — do NOT re-litigate)
- **Nothing inside `isar/analyze/` changes.** If assembly seems to need it, stop — that's a design
  smell (§0.1). Same for parsers, qc, combine, collate, calibrate internals.
- **The four `docs/*_check.py` scripts keep running unmodified.** They are the validation record.
  Everything you add must be additive (new packages, defaulted fields, ignored-by-old-code card
  sections).
- **Masks/holds, never deletion; a missing zero is a held non-result, never E0 = 0.**
- **Card discipline (Phase 2 D8, `PHASE_2_DESIGN.md` §11):** the machine never writes
  `card.policy.toml`. New machine state goes in `card.state.json` (experiment-owned) or
  `session.json` (bookkeeping, deletable) — see U3. (Do not confuse with Phase 3's D8, the
  energy-zero-never-defaults decision.)
- **Deterministic by default; LLM opt-in (`--llm` / card), adjudication only, degrade-don't-stall**
  (U5).
- **Guidelines narrow, never widen** (U8): structure filters subset the physics-gated enumeration;
  no card knob may weaken the validated fit policy (bootstrap M stays a CLI flag).
- **Prior hygiene:** only `resolved` + `fold` outcomes enter the dispersion prior; forks never do.
- **Provenance on every number** (P6): resolution version, zero source, prior size/source, M, seed
  — in the ledger, `results.csv`, and the report.
- **Windows-first paths:** test data dirs contain spaces; `pathlib` everywhere; UTF-8 stdout
  reconfigure + `matplotlib.use("Agg")` in the CLI entry (the check scripts show the pattern).
- **Seeds:** `seed_base + scan_number`, reproducible across re-runs.

## Definition of done
- Both test datasets flow through `isar init` → `isar run` → report with **zero instrument
  branches outside parsers/cards**, meeting every step-5/6 gate above.
- A brand-new user reaches a triage report following only printed instructions (§0.3).
- `pytest` green, check scripts unmodified and passing, README quickstart true.
- `docs/USING_ISAR.md` describes the tool as built — update it if reality diverged during the
  build (and tell the human why).

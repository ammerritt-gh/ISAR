# ISAR Phase 2 — Handoff for the build session

This briefs a fresh Claude Code session (full repo access, can run Python) to build Phase 2.
Keep it short; the spec lives in the design docs, which are authoritative. **When this doc and a
design doc disagree, the design doc wins** — tell the human about the conflict.

## Read first (authoritative, in order)
- `docs/archive/PHASE_2_DESIGN.md` — **the spec for this build. Follow it.** §10 is the build order, §11 lists
  the settled decisions (D1–D7), §0 the five framing corrections that shaped everything.
- `DESIGN.md` — master design, for context on where Phase 2 sits (it realizes §5 schema, §6 card,
  §7.1–7.3 QC, §7.6 collate, §10 correction channel). Note the §8 refinement on the three-state energy
  axis.
- `docs/SCHEMA_LANDSCAPE.md` — the field-survey evidence base behind the schema's extension points.
  (Two of its claims were corrected by real files; the corrections are marked inline and consolidated
  in `PHASE_2_DESIGN.md` §6.)
- `docs/archive/PHASE_1_DESIGN.md` — what the existing synthetic core does, since Phase 2 builds on it.

## What already exists (build on it, don't duplicate)
- `isar/synth/` — **Phase 1, built and working.** The DHO fitter, estimators, scorer, and crucially the
  **synthetic-scan generator** Phase 2 reuses:
  - `isar/synth/generate.py::make_ground_truth(...)` → a `config.GroundTruth` manifest (ω₀, Γ, amplitudes,
    FWHMs, T, energy-grid, target_peak_counts, seed) built on the production lineshape.
  - `isar/synth/generate.py::energy_grid(...)`, `isar/synth/noise.py` (counts scaling + Poisson draw).
  - `isar/synth/_ff.py` / `Fitting_Functions.py` — the production `convolute_voigt_dho`. **Import; do not
    fork.**
  - The Phase-2 `synth_format/run_builder.py` consumes these to build a facility "run" (then serializes
    to real formats). Do **not** re-implement scan generation — wrap Phase 1's.
- `isar/synth/` already uses headless matplotlib (`Agg`) — match that for Phase 2 report figures (D6).

## Reference code (read for structure; the design says how to do it right — do NOT copy wholesale)
- `C:\Users\AMM\Documents\Github\2026 APS S30 Frank Parsing\Data_Parsing_MnTe.py` — a real S30 SPEC
  parser. **Read it for the SPEC mechanics** (`#S/#V2/#V8/#L` handling, monitor normalization), but note
  its two bugs the design fixes: it keeps only the central analyzer (Ana5) and discards the other eight
  (Phase 2 captures all nine, §0.3/§3), and it uses a 1-count error floor (Phase 2 uses **no floor**, D3).
- `C:\Users\AMM\Documents\Github\LangGraph Test 1\` — the abandoned first pass. **Read `clean.py`,
  `screen.py`, `catalogue.py` as anti-patterns**: `clean.py` physically deletes points/scans (Phase 2
  **never deletes — masks only**, §0.4); `catalogue.py` puts an LLM where a deterministic groupby belongs
  (Phase 2's collate is deterministic, §7.5). Do not port these.
- `Scan_Combining.py` (in the FeSeS processing repo) — a working reference for the deterministic collate/
  combine step (§7.5). Honor **combine-before-divide**.

## Real test data (the fidelity guard — §4.1)
`test_data/` holds real files from four instruments. **Phase 2 builds parsers for S30 and IN8 only**;
ID28 and BL43LXU are later cards (do not build them now, but the SPEC base reader should be written so
they slot in as cards).
- **S30 (Phase 2):** `test_data/APS S30/FeSe_1-xS_x_1.spec_supplementary.dat` — SPEC, Mar 2025, nine
  analyzers in `#V4`–`#V12`, `HerixE` energy column.
- **IN8 (Phase 2):** `test_data/ILL IN8/055177` (and siblings) — numor ASCII, Sep 2025. `055177` is an
  *alignment* scan (`COMND: sc a3 …`), shows a real disconnected sensor (`TT= 0.00, RT= 221.57`), and has
  both monitor and time columns. `.nxs` NeXus twins sit beside each numor (a real second format).
- **Later (cards, not now):** `test_data/ESRF ID28/` (SPEC + per-analyzer `_A*.dat`, 2019 **and** 2024),
  `test_data/SP8 BL43LXU/` (SPEC `specixs_*`, Nov 2022).

## Task
Build the Phase 2 parsing + reduction pipeline exactly as `docs/archive/PHASE_2_DESIGN.md` lays out: the
generalized `ScanRecord` schema and experiment card, a shared SPEC base reader + S30 and IN8 parsers
(each with a paired emitter), deterministic QC that flags-and-survives, LLM adjudication via LLMM,
deterministic collate, Markdown+PNG reports, and the card-as-state correction loop. Output is clean
normalized JSON records + human-readable reports. **No fitting** — that is Phase 1's job, run later.

## Build order (from §10; pause where noted)
1. `schema/` — JSON `ScanRecord` (§5.7) + card (§6). D1 (JSON), D3 (no floor), and D8 (card =
   `policy.toml` human-authored read-only + `state.json` machine-written) baked in.
2. `parsers/spec_base.py` + `parsers/spec_s30.py` + its emitter — shared SPEC reader + HERIX card over
   it, all nine analyzers from `#V4`–`#V12` into one record.
3. **Both S30 gates, then PAUSE for human eyeball:** synthetic round-trip `parse(emit(truth)) == truth`
   (`tests/roundtrip/`) **and** the real-file smoke test against the S30 file (`tests/real_data/`). A
   parser is not done until both pass.
4. `reduce/qc_points.py` + `reduce/qc_scans.py` + the quirk injectors. Score masking against injected
   truth: zero false deletes; every check wrapped to emit `uncheckable` rather than throw.
5. `parsers/ill_in8.py` + emitter, with its own round-trip + real-file smoke test against `055177`.
   If the core needs special-casing for IN8, the card schema is wrong — fix the card, not the core.
6. `reduce/collate.py` + `reduce/report.py` — deterministic groupby + combine-before-divide; per-scan/
   per-spectrum figures (matplotlib PNG) with masked points **shown, not hidden**.
7. `reduce/adjudicate.py` — LLM decision + shape-judgment + narration via LLMM. **First** run a Prompter
   probe (local 9B and a NanoGPT tier) on a few synthetic quirked scans to confirm the local tier decides
   sensibly and doesn't invent flags (§7.3). Structured output; off the critical path.
   **DONE (2026-06-28)** — built, TDD-tested, live-verified; probe + evidence recorded in
   **`docs/ADJUDICATION.md`**. (Local 9B is the offline default; `nanogpt/openai/gpt-oss-120b` the cloud
   fallback. The CPU 2B was ruled out as destructive.)
8. `driver/reduce_run.py` honoring `card.state.exclusions`/`.corrections` — close the loop; verify a
   re-run is idempotent.

## Load-bearing constraints (settled — do NOT re-litigate; §11 and §0)
- **Masks, never deletion.** Reduction emits raw arrays + a per-point `mask` + flags. Nothing is ever
  physically removed. This is what makes the human-review loop possible. (§0.4, P5)
- **Three-stage QC: deterministic flags → LLM decides → human is final.** The deterministic layer is
  allowed to over-flag and *must survive messy input* (wrap every check; emit `uncheckable` on failure,
  never crash the batch). The LLM **makes** the QC call using context/shape. No LLM in the path of a
  fitted number. (§0.5, §7)
- **Capture all detectors into one multi-spectrum `ScanRecord`** addressed by `(experiment, scan,
  detector)`; per-detector mask/flags/resolution along the detector dimension. Read per-analyzer Q from
  the file (`#V4`–`#V12`); card offset is a fallback only. (§0.3, §3, D2)
- **JSON canonical store**, arrays inline, NeXus-aligned field names; CSV/NeXus are formatter outputs,
  not the store. (D1, §5.7)
- **Card = two files (D8, §6).** `<exp>.policy.toml` is human-authored, comment-bearing, and the machine
  **never writes it**; `<exp>.state.json` is the only card file the machine rewrites (QC decisions +
  human accept/reject, appended). The human edits policy in TOML by hand; per-run review happens through
  the report's review surface and the tool persists it to `state.json` — the human never hand-edits that
  JSON. Don't default to one all-JSON card; it strips the comments the policy half needs.
- **No zero-count floor.** Store variance honestly (0 for 0 counts); guard any `1/σ²` use-site against
  zero-variance. (D3, §5.5)
- **Generalized schema, constant-Q-energy build only.** Build on the `scan_axes` list but populate only
  the one-scanned-energy-axis case; do not build constant-E / maps / polarization (§5.6, D7). The schema
  must not need a rewrite to add them later; parsers/QC may.
- **Versioned parser registry** keyed on `(facility, instrument, format_version)`, dispatched by
  `detect(path)` reading embedded dates + headers; stamp every record's `source` with plugin/format/
  schema versions. A drifted format is a new registry entry, never an edit to a working parser. (§6)
- **Both validation gates required per parser:** synthetic round-trip (coverage) + real-file smoke test
  (byte fidelity). Round-trip alone is the inverse-crime trap. (§4.1, D5)
- **LLMM for all model calls** (structured output, profiles, telemetry); **Prompter MCP** for the
  feasibility probe. Local 9B or NanoGPT tiers.

## Definition of done for Phase 2
- S30 and IN8 parsers each pass round-trip **and** real-file smoke tests.
- Deterministic QC scores zero false deletes against injected-quirk truth; survives messy input without
  crashing.
- A synthetic run flows end-to-end: emit → parse → QC → adjudicate → collate → JSON records + Markdown/
  PNG report, with masked points visible and a review surface that feeds `card.state`.
- A re-run honoring an edited card is idempotent.
- The LLM-adjudication Prompter probe is documented (does the local tier do the job, or do we escalate?).
  **DONE** — `docs/ADJUDICATION.md` §7: the GPU 9B clears the adequacy bar (zero destructive errors),
  `gpt-oss-120b` is the cloud fallback; full model-by-model evidence recorded.

## One operational note for the human (not a build task)
At the start of each beamtime, the human points the session at a freshly captured file to confirm the
parser/card/`format_version` match what the instrument emits *now*, and re-confirms which analyzers carry
signal for the current sample. The registry makes this a check + card review, not a rewrite. (§6, §11)

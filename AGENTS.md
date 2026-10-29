# ISAR — Agent Floor

> **Status:** live
> **Authority:** canonical for contributor rules and area routing
> **Verified against code revision:** `5cf3408` (2026-08-15)

Automated **triage** pipeline for inelastic scattering (IXS + triple-axis): during a beamtime it
parses incoming scans, QCs and calibrates them, fits DHO ⊗ resolution models with physics-gated
model selection, and issues a per-scan verdict so a human knows where to spend review time. It is
fast feedback for steering, **not** the system of record for published numbers. **Nothing is ever
deleted**; a human reviews everything afterward. This repo is doc-dense — the rules below are the
wrong-work preventers; everything else is a routing pointer. Read the routed doc before touching an
area.

## Quick Reference

- **Runtime:** Python ≥ 3.11. Deps: numpy, scipy, matplotlib, iminuit, lmfit, dynesty (`pyproject.toml`).
- **Environment:** **micromamba, not venv.** Env name `isar`, created from `environment-isar.yml`.
  micromamba lives at `%USERPROFILE%\AppData\Local\micromamba\micromamba.exe`; envs under
  `…\Local\micromamba\envs\` **or** `%USERPROFILE%\AppData\Roaming\mamba\envs\`.
- **Setup (operator):** double-click `setup-isar-env.bat` (creates env, `pip install -e ".[gui]"`).
- **CLI entrypoint:** `isar` → `isar.cli.main:main`. Subcommands: `init detect ingest run status
  report score profile drive view diagnose`.
- **GUI:** `run-isar-gui.bat` → the environment's `pythonw.exe -m isar_gui` (no console);
  falls back to env `tavi-dev` when `isar` isn't built yet.
- **Tests:** markers `slow`, `live`, `golden`; `testpaths` = `isar/synth/tests`, `tests`
  (`pyproject.toml`). **The routine suite needs nothing outside git** — no test reads `real_data/`
  (hard rule 10), so it passes in any worktree or fresh checkout. `live` needs a running TAVI at
  `127.0.0.1:8642` and opts in through `ISAR_LIVE_TAVI=1` (`ISAR_LIVE_LLMM=1` for the LLMM test);
  a live run must report **passed**, not skipped — a skipped live test is a failure to investigate
  (a live test also marked `slow`, such as the LLMM bake-off, needs `--run-slow` too).
  `isar/synth/tests/` = Phase-1 synthetic core; `tests/` = Phase 2+. Parallel runs need
  `pytest-xdist>=3.5` (in the `[test]` extra).
- **Proofs — checks against real data, run on purpose.** `proofs/` sits outside `testpaths`, so the
  routine suite never collects it. Run it on the **main checkout**, where the real data lives:
  `python -m pytest proofs`, or double-click `tools\run-isar-proofs.bat`. A missing real file or
  expectation file is an **error** naming it, never a skip. `setup-isar-env.bat` installs `.[gui]`
  only, so pytest in the `isar` environment comes from `pip install -e ".[test]"`. The `golden`
  marker (S12) = **sealed held-out real-data gates**, `proofs/test_golden_scenarios.py`: the repo
  carries only the generic runner; every expectation lives in gitignored
  `real_data/golden/<scenario>/scenario.json`, which is **never read, run, or consulted during
  development** — the gate runs only when the owner names it (`ISAR_GOLDEN=1`, or
  `tools\run-isar-proofs.bat golden`). The le Tacon fast-pass proof (`proofs/test_letacon_fast_pass.py`, one to
  two hours: two 28-spectrum batches and the prior sensitivity check) is gated the same way and runs
  only on `ISAR_LETACON=1`, or `tools\run-isar-proofs.bat letacon`.
  A job worktree runs a proof on the main checkout's data with `ISAR_REAL_DATA=<main checkout>\real_data` (read only).
- **Data folders.** `real_data/` — gitignored, main checkout only: the real facility files, each
  proof's measured expectations in `<proof stem>.expect.json` beside its data, and the sealed
  golden bundles. `artificial_data/` — committed: copies of real facility files with every data
  number replaced (headers kept, but a measured number in a header or trailer comment line is replaced too, e.g. the BL43LXU analyzer files' trailing `#Q=` / `#T=` and the SARF eshift), made once with `python tools/artificial_copy.py <real file> --out
  <folder>`, each with a truth file beside it, never regenerated; the parser tests read them.
  Behaviour tests that depend on the numbers use data generated at test time (`isar/synth_format/`).
- **Committed curves.** `resolution_curves/` — verbatim copies of the measured BL43LXU resolution curves,
  the one measured data the repository carries (`isar/synth/bl43_curves.py` reads them; the folder's
  README has provenance).
- **Running the suite — a gate and two deliberate checks** (operator ruling 2026-10-29: slow tests
  are off by default; the big slow tests and the serial run are done deliberately). Tests marked
  `slow` are deselected unless `--run-slow` is given, even when named (`conftest.py`, the same hook
  as `--run-statistical`).
  1. *Targeted / debugging (serial):* `python -m pytest <path>::<test> -s` (add `--run-slow` for a
     slow test). Keep debugging serial — `-s`, `--pdb`, and the stall recipe
     `-o faulthandler_timeout=60` do not work meaningfully under xdist, which captures and
     multiplexes worker output.
  2. *The gate (~90 s):* `python -m pytest -n 16 --dist worksteal` — slow tests deselected by
     default; ~90 s on the 32-core box (measured 2026-10-29, 6214 tests). On a smaller box use
     `-n auto` instead of `-n 16`: a 4-core box thrashes at `-n 16`.
  3. *Slow check (~6 min):* `tools\run-isar-checks.bat`, i.e. `python -m pytest --run-slow -n 16
     --dist worksteal` — before a release, when a change touches drive, scene or session
     delivery, or on the operator's request. One 219 s FCC battery test sets its wall time.
  4. *Full serial check (~25 min):* `tools\run-isar-checks.bat serial`, i.e. `python -m pytest
     --run-slow` — only after test-infrastructure changes (conftest, fixtures, xdist settings) or
     on the operator's request: serial and parallel expose different ordering and shared-state bugs.
  A check reports; a failing check is a finding to file, not a blocker of the change that ran it.
- **Parallel-run rules (each names the failure it prevents):** parallel must reproduce serial
  pass/skip/deselect counts exactly — a divergence is a test-isolation bug, not xdist noise.
  Do not switch to `--dist loadfile` or sprinkle `xdist_group`: `loadgroup`
  schedules ungrouped tests like `load` rather than worksteal (~19% slower measured), and
  `tests/session/test_driver_smoke.py`'s ~20 s tests must never be pinned to one worker.
- **Nested-pool guard:** the repo-root `conftest.py` `setdefault`s `ISAR_WORKERS=2` /
  `ISAR_BOOTSTRAP_WORKERS=2` under pytest (a pre-set environment still wins); without it each xdist
  worker spawns its own `min(30, cpu−2)` driver pool and fork-bombs the box. It does **not** reach
  `isar/synth/score.py` / `demo.py`, whose `n_workers` defaults to `cpu−2` with no env knob — every
  current test passes `n_workers` explicitly, and a new one must too.
- **Escalation sampler cap:** the same conftest `setdefault`s `ISAR_ESCALATION_N_LIVE=32`. Under
  evidence selection the dynesty escalation rung is ~90% of a campaign test's wall clock (measured
  2026-08-09: one shadow-selection test, 348 s at `n_live=128` vs 35.6 s at 8, passing either way).
  Production leaves the variable unset and samples at `ESCALATION_N_LIVE=32` (128 until
  2026-09-03; the cap and the default now coincide, the knob stays). It travels by
  *environment*, not a patched constant, because a spawned pool child re-imports
  `isar.analyze.evidence` — a fixture would leave the parallel arm sampling at 128. An explicit
  `n_live=` argument still wins over both. See the conftest docstring for why capping it does not
  weaken what the suite asserts.
- Offscreen GUI runs: pytest sets the platform via `tests/gui/conftest.py`; a test that leaves a
  modal path unmocked will hang the suite silently — debug that stall serially with
  `-o faulthandler_timeout=60`. Any interpreter with the dependencies installed runs the suite from
  the repo root (it imports the repo-root `isar` package directly), so a micromamba env is not a
  prerequisite for testing.

## Hard Rules (each names the silent failure it prevents)

1. **ISAR should not overwrite an operator's values or settings on its own.** Writing policy from code
   silently overwrites the scientist's intent; hand-editing state silently corrupts machine bookkeeping.
   It holds because `card.policy.toml` is **hand-edited human intent** — detectors, column maps,
   collation/merge policy, fitting guidelines — that **code must never write**; `card.state.json`
   (calibration versions, dispersion prior, exclusions) and the `intake/` records are **machine-written**;
   ISAR's only own write to a setting is a discovered resolution folder when none is saved.
   (`isar/schema/`; ARCHITECTURE §5, GLOSSARY.)

2. **Anti-Stokes amplitude is always derived, never fit.** It is Stokes × Bose factor
   `exp(−ℏω/k_BT)` — a *functional* tie (`isar/analyze/components.py:11-12`, `structures.py`). Freeing
   it as a parameter fits fine and yields physically meaningless amplitudes. Detailed balance also
   gates whether an anti-Stokes peak is *admissible* before it is fit (ARCHITECTURE §7).

3. **Poisson variance, no zero-count floor (convention D3).** σ=√N with `VARIANCE_CONVENTION="none"`
   (`isar/schema/scan.py:22`); σ=0 for 0 counts is intentional — **code must handle σ=0**, not clamp
   it. Adding a floor silently biases every fit (the Baker–Cousins likelihood is only valid on raw
   counts). **Cross-repo trap:** sibling repo PUFFIN uses the *opposite* convention (σ≥1 floor). Never
   port variance handling between the two.

4. **Energy calibration is 3-state, and scans are held until it exists.** `raw_abscissa_kind` status
   is `absolute` / `needs_zero` / `needs_conversion_and_zero` (`scan.py:28`). A no-elastic-line scan on
   a `needs_zero` instrument with no zero is a *held* non-result — **never default E0 = 0.0.**
   Instrument-agnostic code that assumes a 2-state (absolute-or-not) world breaks TAS data. (ARCHITECTURE §5.)
   The display may draw such a scan on its file axis, labelled beamline zero or not zeroed; no number is
   computed from it (`isar/workspace_api/spectra.py`).

5. **QC masks, never deletes; the dispersion prior accepts only folds.** Rejection marks a per-point
   flag and keeps the data (`miniscan_reject`); deleting anything breaks the trust contract. Only clean
   single-answer **fold** fits enter the running dispersion prior — ambiguous **fork** (multi-branch)
   results never do, or the prior learns a phantom dispersion. (ARCHITECTURE §6–7, GLOSSARY.)

6. **Layer boundaries are Qt-free.** `isar/workspace_api` (the read-only workspace facade),
   `isar/geometry` (vendored from TAVI and TAS_MCP; sources and adaptations in
   `isar/geometry/VENDORED.md`), and `isar/plotspec` must import no Qt. A Qt import leaking
   into the facade or geometry breaks the notebook/headless consumers. Correspondingly, **no LLM in
   the runtime path of a number** (P1): LLM output freezes into config before the deterministic
   fitter runs.

7. **What the user sees is what ISAR uses, and no science happens in the GUI.** The GUI computes, fits
   and interprets nothing. It shows what ISAR has recorded and sends the user's actions down to ISAR (a
   value, a condition, a setting, starting or stopping a run). A value shown as saved is the value every
   later fit uses, and every fit records the values it used. Today that holds because ISAR keeps the only
   copy, the GUI sends changes and never a snapshot, a refresh only reads, and `workspace_api` (the read
   layer the GUI and notebooks share) never writes. The failure this prevents: a group reads one value on
   screen while ISAR fits with another, as when a GUI refresh overwrote an entered value in LLMM.

8. **A pooled master's f(E) is counts per reference monitor, not raw counts.** Rescale before
   comparing intensities across masters; comparing them as they stand silently mixes scales.

9. **A new float column in `report/results.csv` joins `_NUMERIC_COLUMNS`**
   (`isar/drive/isar_io/readers.py`), or it silently reads back as text.
10. **No test reads `real_data/`.** It is gitignored and exists only in the main checkout, so a
    test that opens a file there fails with `FileNotFoundError` in every worktree and fresh
    checkout — about forty did until 2026-10-20. A check against real files is a *proof*
    (`proofs/`, run on purpose); a test uses `artificial_data/` or data generated at test time.
    `tests/docs/test_no_real_data_in_tests.py` fails on any test that names `real_data` or
    `test_data` as a path. Never commit a measured data value: an artificial copy replaces every
    data number, and no truth file, test expectation or comment may carry one. One exception: the
    measured resolution curves in `resolution_curves/` (operator, 2026-10-25: "Resolutions are much
    safer for the public to read, so you can make a copy for the tests."). Nothing else measured may go
    there, and `tests/docs/test_no_real_data_in_tests.py` checks that the folder holds only curves and sidecars.

## Before You Touch X

| Task area | Read first |
|---|---|
| Orientation / why trust it | `docs/READING_GUIDE.md` → `docs/ARCHITECTURE.md` |
| Running it on data / the CLI contract | `docs/USING_ISAR.md`, `docs/WORKED_EXAMPLE.md` |
| Workspace, session, and the CLI surface | `docs/WORKSPACE_AND_CLI.md` |
| Fit/runtime performance and speedup gates | `docs/plans/speedup.md` |
| Statistics (cost, evidence/AIC, interval ladder, coverage) | `docs/STATS_PRIMER.md`, `docs/GLOSSARY.md` |
| Verdicts — meaning & what to do | `docs/INTERPRETING_A_VERDICT.md`, GLOSSARY |
| Card / schema across facilities | `docs/SCHEMA_LANDSCAPE.md` |
| Flag adjudication (opt-in LLM) | `docs/ADJUDICATION.md` |
| The autonomous driver (`isar drive`) | `docs/design/steering.md` |
| The EIG planner (targeted-lane placement — Q, energy window and exposure, Implemented; it is the only thing that places a targeted scan) and scene-EIG (discovery-lane placement, Implemented from birth since SCENE-5, H23 — status: ARCHITECTURE §15) | `docs/design/eig.md`, then `docs/design/steering.md` |
| Declaring expected excitations (branch hypotheses Implemented; level/continuum Partial) | `docs/HYPOTHESES.md` |
| What ISAR is for — the master objectives | `docs/DESIGN_GOALS.md`: user-owned, never AI-edited; read it at every planning session and surface a contradiction to the operator instead of resolving it; its Pause/Failure States section is the law for stopping a campaign (steering.md §7.10 implements it) |
| The SCENE program — scene layer, catalogue, target-free discovery (status: ARCHITECTURE §15) | `docs/design/scene.md` |
| Campaign diagnostic report | `docs/design/reporting.md` |
| Why a decision was made | the owning design doc's dated rulings. Ruling IDs (O·, H·, S·, G·, ADR-NNN, …) are stable historical names — see GLOSSARY §Ruling IDs; the retired DECISIONS ledger is in git history |
| The frozen campaign evidence bundles (data, never contract) | `docs/campaigns/README.md` |

## Module map (ownership boundaries that matter)

`schema` (ScanRecord + card; `schema/action_identity.py` is the one implementation of scientific
action identity, and `drive/acquisition/inventory.py` re-exports those names for its callers so no
second implementation appears)
· `parsers` (plugin registry: S30 SPEC, IN8 numor, TAVI, BL43LXU; card
templates in `parsers/cards/*.toml` — in8, s30, s30_raw, tavi, bl43lxu (also its format file,
read at every load)) · `reduce` (QC mask-not-delete,
combine-before-divide, collate) · `calibrate` (elastic fits, versioned energy-zero series;
`calibrate/tas.py` importing `isar.synth.resolution` is the one sanctioned production→synth
dependency) · `analyze` (fitting, errors, model selection, verdict, confidence; the DHO physics —
IC grids, admissibility gates, structure enumeration, Γ bounds — is owned by `analyze/phonon.py`,
and `analyze/structures.py` holds only the generic `Hypothesis` recipe) · `session` (`driver.py`
orders the stages; `outcome.py` owns the FitOutcome persistence DTO and `results.csv`,
`scan_delivery.py` the per-scan fits, `master_delivery.py` the pooled masters and certification,
`evidence_update.py` the cross-q evidence and branch identity; `analysis_service.py` the one long-lived analysis process per open experiment, which runs the queue's items and saves their results through one writer; `analysis_queue.py` the queue's scheduler, which the GUI owns; `file_fit.py` one analyzer file, fast or deep, from the experiment's saved inputs; `writer.py` `save_results`, the one writer, under the workspace write lock) · `cli` · `drive` (`loop.py`
orchestrates the targeted campaign and `program_runner.py` the discovery step — `run_program` is
the one seam that runs a `discover_and_map` step end to end, and the consultation channel
(`drive/consultation.py`) and cold-start producer (`drive/steering/cold_start.py`) hang off it;
TAVI wire details live in `drive/instrument/request_realization.py`. `acquisition/eig_planner.py`
is the ONE place a Q, an energy window or an exposure is chosen — no survey, no frontier, no
opening book, no scan-window comparison (`docs/design/eig.md`); `acquisition/classify_eig.py` is
the drive-side read of the hypothesis map; `loop.<CONST>` re-imports are
deliberate, production reads them) ·
`report` · `synth` (Phase-1 synthetic core) / `synth_format` (synthetic-run tooling) ·
`workspace_api` / `geometry` / `plotspec` (Qt-free; `workspace_api/config_gates.py` is the G16
facade) ·
`intake` (Qt-free; the BL43 append-only user-value and settings store in `isar/intake/`, whose writers
the GUI calls in process; `workspace_api` only reads it) ·
`isar_gui` (reads science through `workspace_api`, saves the user's values and settings in process
through `isar.intake`, controls campaigns by subprocess + control files, and runs the analysis queue through the `isar.session.analysis_service` subprocess).
The Stage II ↔ III boundary is a **frozen file contract**
(`report/results.csv`, `report/measurements.jsonl` — the counts-authoritative
`isar.measurement-arena/1` log, one line per measured arena written before any fit is trusted to
exist, `<workspace>/report/report.json`, `card.state.json`) — the driver never imports fit
internals, and the campaign's per-delivery scene build goes through the supervised
`isar scene-build` subprocess for that reason, not through an import.
The one pre-release v1→v2 clean break was authorized on 2026-07-17 (ruling O12). During the
SCENE transition, H1 supersedes the additive-only rule for contracts SCENE rebuilds:
`isar.campaign-program/1` replaces the objective envelope outright with no compatibility reader,
and contract changes are judged against the SCENE world suite, not serialization compatibility
(ARCHITECTURE §4, ruling H1). Do not build compatibility shims or dual-path readers for retired
formats — H1 classifies them as defects.

## Verification (cheapest first)

1. **Parse:** `python -m py_compile` the changed files (or your editor's syntax check).
2. **Import:** import the changed public surface (e.g. `import isar.analyze.components`); for GUI work
   confirm no Qt leaked into `workspace_api`/`geometry`/`plotspec`.
3. **Tests — targeted while iterating, broad once per slice.** Verification fan-out, not
   implementation, is the historical bottleneck: the failure this rule prevents is "rerun the world
   after every manifest tweak." The gate (~90 s) is a *slice-boundary* tool, not a per-commit
   reflex, and the two checks (slow ~6 min, full serial ~25 min) are run deliberately:
   - **During active repair work** run only: parse/import checks on changed files, the tests that
     directly cover the changed modules, and the specific case/fault harness affected. Do **not**
     run the gate after every repair. A manifest/hash/path correction gets its
     bundle verifier only; an evaluator fix gets evaluator tests plus retained cases; a fixture
     change reruns that case, not the campaign; live TAVI reruns only when its raw evidence was
     actually invalidated.
   - **At each slice boundary** (final candidate of a slice, before reporting it done) and at
     `land`: the gate, `python -m pytest -n 16 --dist worksteal`, once. The slow check runs too
     when the slice touches drive, scene or session delivery; the full serial check only after
     test-infrastructure changes or on the operator's request. Reproduce any failure
     serially before debugging it. Proofs (`proofs/`, real data) are run on purpose on the main
     checkout, never as part of a slice boundary; `live` needs TAVI at `127.0.0.1:8642`.
   - **Dependency-aware invalidation:** retained artifacts form a chain — raw acquisition →
     analysis result → score evidence → eligibility evidence → gate classification →
     manifest/index. A repair invalidates only its *descendants*: a manifest-pointer change
     regenerates the manifest and gate result, never the scan, fits, or scores; an evaluator change
     reclassifies existing authenticated evidence rather than regenerating it. Unchanged cases
     remain authenticated inputs.
   - **Blocker threshold for review findings:** repair now only what can affect the physical
     request, the score or its uncertainty, the gate classification, evidence authentication,
     fallback behavior, or crash/replay correctness. Naming imperfections, redundant hashes,
     duplicated evidence, optional diagnostics, and documentation elegance are deferred unless they
     make evidence materially ambiguous — a review must not turn "this could be cleaner" into
     "rerun the world."
4. **Status discipline:** ARCHITECTURE §15 marks every capability Implemented / Partial / Planned. Do not
   describe Planned work in the implemented voice, and match that table when you change status.
5. **Evidence is frozen once, at milestones, and never renewed (S10).** A completed formal gate
   (the STAT-2b.1 held-out campaign) is authenticated at its frozen commit, inputs, and
   interpretation — never refreshed to match current source. New formal freezes happen only when
   the owner names a milestone (selection-criterion change, new acquisition authority,
   real-instrument deployment, publication-grade claim): designed before the run, executed once,
   kept at their commit. Everything else is ordinary development — normal review plus behavior
   tests; no drift closure, no promotion commits, and no permanently "selection-critical" files.
   Internal acquisition authority granted during the SCENE program is "everything else": it is
   validated by the ordinary deterministic world-suite lanes, never by a new freeze (H1).

## Documentation policy (what a document is allowed to be)

*(A documentation rebuild to this policy is in progress — frontier and rationale in
[`docs/HANDOFF-doc-rebuild.md`](docs/HANDOFF-doc-rebuild.md).)*

Every maintained `.md` declares a **status** in a banner under its title — exactly one of:

- `draft` — being designed; do not rely on it.
- `live` — current and authoritative; maintained with the code.
- `terminal` — its terminal trigger has fired (work landed, system replaced, record absorbed);
  it awaits absorption into a living document and then **deletion**. Nothing stays terminal long.

An optional **Authority** line marks the single canonical source for a topic; explanatory
documents have none, and inventing one for them is how two documents end up claiming the same
ground. A generated file says `generated by <command>` — edit the generator, not the output.

**`docs/READING_GUIDE.md` is the single map.** Every maintained document is reachable from it,
directly or through one linked index. There is no separate register; **git history is the
archive** — deleted documents are recovered with `git log --diff-filter=D -- <path>`, never kept
as tombstones, stubs, or an `archive/` of things nobody dared delete.

Rules, each naming the failure it prevents:

1. **A `live` document may not take a `terminal` document as its normative source.** Citing one
   for *rationale* is fine while it still exists — the test is whether deleting the citation would
   leave a claim unsupported. Without this, a repaired historical failure silently governs
   current behaviour.
2. **A plan describes work not yet landed, lives in `docs/plans/`, and needs an owner and exit
   criteria.** When the work lands, its durable content moves to the owning design doc and the
   plan is deleted in the same change. A completed plan sitting in the tree is a documentation
   bug.
3. **Living design docs own their systems.** `docs/design/<system>.md` states current behaviour,
   invariants, code locations, and that system's roadmap. Decisions are recorded there as dated
   amendments in place — there is no separate decision ledger, and no ID-series queue of pending
   rulings. An open question goes to the rolling handoff *with its context written out*, or it
   was not ready to be asked.
4. **One rolling handoff per active workstream** (`docs/HANDOFF-<workstream>.md`), rewritten to
   the present frontier, deleted when the objective closes. No dated session logs.
5. **Deletion is preferred to a growing archive**, on an irreplaceability test: does this record
   reasoning or evidence that exists nowhere else? Absorb what survives the test into the living
   owner, then delete the source in the same commit.

When you change behaviour, commands, schemas, or capability status, update the governing document
in the same change — and if the claim is machine-checkable, add it to `tests/docs/`.

`tests/docs/test_doc_drift.py` enforces the mechanical half:

- every tracked `.md` declares a valid status, and every plan under `docs/plans/` declares an
  owner and exit criteria;
- every tracked `.md` is reachable from `docs/READING_GUIDE.md`, directly or via one linked
  index — an orphan file fails the build;
- relative links resolve case-sensitively;
- no maintained document reintroduces a phrase that was corrected once (`STALE_PHRASES`);
- a command list marked `<!-- isar-commands: exhaustive -->` matches
  `isar.cli.main.build_parser()`;
- the `docs/assets/` bundle matches its recorded hashes (`python tools/docs/collect_evidence.py
  --check`).

**Adding a new document therefore requires a banner and a place on the map** — the test fails
otherwise, by design. Rule 1's *normative source* judgement is not machine-checkable and stays a
review item.

---

*Last updated: 2026-08-20. Canonical file is `AGENTS.md`; `CLAUDE.md` is the `@AGENTS.md` stub.*

## Memory — promote or die

- 2026-10-27 · role: operator wants the planning coordinator's context gauge at 300k or more; at 200k it handed back mid-integration of a 14-unit plan (job ja0cd202ecb67).
- 2026-10-27 · GUI threshold (operator): a physicist new to the GUI, at 3 AM, nobody to ask; labels name their object, every control has a tooltip. Plain offscreen Qt has no fonts; render with QT_QPA_FONTDIR=C:\Windows\Fonts.
- 2026-10-27 · pytest-qt closes leftover windows after per-test monkeypatches are undone; never let a GUI path read the profile lazily. The root conftest now walls the session off from %APPDATA%.
- 2026-10-27 · floor: an acs-job --test-command with single quotes reaches pytest literally on Windows (every xdist worker exits 4, "no active workers"); use double quotes, passed via python.

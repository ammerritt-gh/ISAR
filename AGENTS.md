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
- **GUI:** `run-isar-gui.bat` → `micromamba run -n isar python -m isar_gui`; falls back to env
  `tavi-dev` when `isar` isn't built yet.
- **Tests:** markers `slow`, `live`, `golden`; `testpaths` = `isar/synth/tests`, `tests`
  (`pyproject.toml`). `live` needs a running TAVI at `127.0.0.1:8642`. `tests/real_data/` needs
  the gitignored local `test_data/`. `golden` (S12) = **sealed held-out real-data gates**: the
  repo carries only the generic runner; every expectation lives in gitignored
  `test_data/golden/<scenario>/scenario.json`, which is **never read, run, or consulted during
  development** — the gate runs only when the owner names it (`ISAR_GOLDEN=1`). `isar/synth/tests/` = Phase-1 synthetic core; `tests/` = Phase 2+. Parallel runs
  need `pytest-xdist>=3.5` (in the `[test]` extra).
- **Running the suite — three commands, pick by intent:**
  1. *Targeted / debugging (serial):* `python -m pytest <path>::<test> -s`. Keep debugging serial —
     `-s`, `--pdb`, and the stall recipe `-o faulthandler_timeout=60` do not work meaningfully
     under xdist, which captures and multiplexes worker output.
  2. *Day-to-day (~1 min):* `python -m pytest -m "not slow" -n 16 --dist worksteal` — ~55 s on an
     idle 32-core box against 347 s serial. On a smaller box use `-n auto` instead of `-n 16`: the
     pinned count is tuned for that idle 32-core machine, and a 4-core box thrashes at `-n 16`.
  3. *Full validation (~10 min):* `python -m pytest -n 16 --dist worksteal`, against ~24 min
     serial. Run a full **serial** `python -m pytest` periodically anyway (pre-release, after
     test-infra changes): serial and parallel expose different ordering and shared-state bugs.
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

1. **The experiment card is two files with opposite ownership.** `card.policy.toml` is
   **hand-edited human intent** — detectors, column maps, collation/merge policy, fitting guidelines;
   **code must never write it.** `card.state.json` is **machine-written** — calibration versions,
   dispersion prior, exclusions. Writing policy from code silently overwrites the scientist's intent;
   hand-editing state silently corrupts machine bookkeeping. (`isar/schema/`; ARCHITECTURE §5, GLOSSARY.)

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

5. **QC masks, never deletes; the dispersion prior accepts only folds.** Rejection marks a per-point
   flag and keeps the data (`miniscan_reject`); deleting anything breaks the trust contract. Only clean
   single-answer **fold** fits enter the running dispersion prior — ambiguous **fork** (multi-branch)
   results never do, or the prior learns a phantom dispersion. (ARCHITECTURE §6–7, GLOSSARY.)

6. **Layer boundaries are Qt-free.** `isar/workspace_api` (the read-only workspace facade),
   `isar/geometry` (vendored from TAVI), and `isar/plotspec` must import no Qt. A Qt import leaking
   into the facade or geometry breaks the notebook/headless consumers. Correspondingly, **no LLM in
   the runtime path of a number** (P1): LLM output freezes into config before the deterministic
   fitter runs.

7. **The GUI reads science, it never writes science.** `isar_gui` (PySide6 + pyqtgraph) is
   read-only *with respect to scientific workspace interpretation*: everything it displays comes
   through `workspace_api`, which never re-fits, re-collates from raw, or writes
   (`isar/workspace_api/__init__.py`). Operational control is a separate channel and is allowed —
   the GUI owns subprocesses (`isar init/run/drive` via `isar_gui/process.py`) and requests a
   cooperative stop by atomically writing `report/control/stop-<token>.json`
   (`isar_gui/operation_coordinator.py`). Stating this as "read-only viewer" is what previously led
   contributors to think launching or stopping a campaign broke the architecture; interpreting a
   number outside the fitter is what actually would.

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
| Same-action scan-window EIG (targeted-lane realization, Implemented, authoritative default-on at compile time since SCENE-4.4) and scene-EIG (discovery-lane placement, Implemented from birth since SCENE-5, H23 — status: ARCHITECTURE §15) | `docs/design/eig.md`, then `docs/design/steering.md` |
| Declaring expected excitations (branch hypotheses Implemented; level/continuum Partial) | `docs/HYPOTHESES.md` |
| The SCENE program — scene layer, catalogue, target-free discovery (status: ARCHITECTURE §15) | `docs/design/scene.md` |
| Campaign diagnostic report | `docs/design/reporting.md` |
| Why a decision was made | the owning design doc's dated rulings. Ruling IDs (O·, H·, S·, G·, ADR-NNN, …) are stable historical names — see GLOSSARY §Ruling IDs; the retired DECISIONS ledger is in git history |
| The frozen campaign evidence bundles (data, never contract) | `docs/campaigns/README.md` |

## Module map (ownership boundaries that matter)

`schema` (ScanRecord + card; `schema/action_identity.py` is the one implementation of scientific
action identity — `drive/acquisition/inventory.py` re-exports those names for its callers and
`tests/schema/test_action_identity.py` pins them as re-exports so no second implementation appears)
· `parsers` (plugin registry: S30 SPEC, IN8 numor, TAVI, BL43LXU; card
templates in `parsers/cards/*.toml` — in8, s30, s30_raw, tavi) · `reduce` (QC mask-not-delete,
combine-before-divide, collate) · `calibrate` (elastic fits, versioned energy-zero series;
`calibrate/tas.py` importing `isar.synth.resolution` is the one sanctioned production→synth
dependency) · `analyze` (fitting, errors, model selection, verdict, confidence; the DHO physics —
IC grids, admissibility gates, structure enumeration, Γ bounds — is owned by `analyze/phonon.py`,
and `analyze/structures.py` holds only the generic `Hypothesis` recipe) · `session` (`driver.py`
orders the stages; `outcome.py` owns the FitOutcome persistence DTO and `results.csv`,
`scan_delivery.py` the per-scan fits, `master_delivery.py` the pooled masters and certification,
`evidence_update.py` the cross-q evidence and branch identity) · `cli` · `drive` (`loop.py`
orchestrates the targeted campaign and `program_runner.py` the discovery step — `run_program` is
the one seam that runs a `discover_and_map` step end to end, and the consultation channel
(`drive/consultation.py`) and cold-start producer (`drive/steering/cold_start.py`) hang off it;
TAVI wire details live in `drive/instrument/request_realization.py` and
EIG runtime adaptation in `drive/acquisition/eig_runtime.py`; `loop.<CONST>` re-imports are
deliberate, production reads them) ·
`report` · `synth` (Phase-1 synthetic core) / `synth_format` (synthetic-run tooling) ·
`workspace_api` / `geometry` / `plotspec` (Qt-free; `workspace_api/config_gates.py` is the G16
facade) ·
`isar_gui` (reads science through `workspace_api`; controls campaigns by subprocess + control
files). The Stage II ↔ III boundary is a **frozen file contract**
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
   after every manifest tweak." The three lanes (not-slow parallel ~1 min, full parallel ~10 min,
   full serial at release gates) are *slice-boundary* tools, not per-commit reflexes:
   - **During active repair work** run only: parse/import checks on changed files, the tests that
     directly cover the changed modules, and the specific case/fault harness affected. Do **not**
     run the not-slow or full suite after every repair. A manifest/hash/path correction gets its
     bundle verifier only; an evaluator fix gets evaluator tests plus retained cases; a fixture
     change reruns that case, not the campaign; live TAVI reruns only when its raw evidence was
     actually invalidated.
   - **At each slice boundary** (final candidate of a slice, before reporting it done): one
     parallel not-slow run and one parallel full run — once. Full **serial** `python -m pytest`
     only before merging to `main` or after test-infrastructure changes. Reproduce any failure
     serially before debugging it. Real-data subset needs local `test_data/`; `live` needs TAVI at
     `127.0.0.1:8642`.
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

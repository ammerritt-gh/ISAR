# ISAR — Agent Floor

> **Lifecycle:** Current
> **Authority:** canonical for contributor rules and area routing
> **Provenance:** hand-written
> **Verified against code revision:** `9914eb7` (2026-07-26)

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
- **Tests:** markers `slow`, `live`; `testpaths` = `isar/synth/tests`, `tests` (`pyproject.toml`).
  `live` needs a running TAVI at `127.0.0.1:8642`. `tests/real_data/` needs the gitignored local
  `test_data/`. `isar/synth/tests/` = Phase-1 synthetic core; `tests/` = Phase 2+. Parallel runs
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
   hand-editing state silently corrupts machine bookkeeping. (`isar/schema/`; DESIGN §5, GLOSSARY.)

2. **Anti-Stokes amplitude is always derived, never fit.** It is Stokes × Bose factor
   `exp(−ℏω/k_BT)` — a *functional* tie (`isar/analyze/components.py:11-12`, `structures.py`). Freeing
   it as a parameter fits fine and yields physically meaningless amplitudes. Detailed balance also
   gates whether an anti-Stokes peak is *admissible* before it is fit (DESIGN §7).

3. **Poisson variance, no zero-count floor (convention D3).** σ=√N with `VARIANCE_CONVENTION="none"`
   (`isar/schema/scan.py:22`); σ=0 for 0 counts is intentional — **code must handle σ=0**, not clamp
   it. Adding a floor silently biases every fit (the Baker–Cousins likelihood is only valid on raw
   counts). **Cross-repo trap:** sibling repo PUFFIN uses the *opposite* convention (σ≥1 floor). Never
   port variance handling between the two.

4. **Energy calibration is 3-state, and scans are held until it exists.** `raw_abscissa_kind` status
   is `absolute` / `needs_zero` / `needs_conversion_and_zero` (`scan.py:28`). A no-elastic-line scan on
   a `needs_zero` instrument with no zero is a *held* non-result — **never default E0 = 0.0.**
   Instrument-agnostic code that assumes a 2-state (absolute-or-not) world breaks TAS data. (DESIGN §5.)

5. **QC masks, never deletes; the dispersion prior accepts only folds.** Rejection marks a per-point
   flag and keeps the data (`miniscan_reject`); deleting anything breaks the trust contract. Only clean
   single-answer **fold** fits enter the running dispersion prior — ambiguous **fork** (multi-branch)
   results never do, or the prior learns a phantom dispersion. (DESIGN §6–7, GLOSSARY.)

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
| Orientation / why trust it | `docs/READING_GUIDE.md` → `DESIGN.md` |
| Running it on data / the CLI contract | `docs/USING_ISAR.md`, `docs/WORKED_EXAMPLE.md` |
| Workspace, session, and the CLI surface | `docs/WORKSPACE_AND_CLI.md` |
| Fit/runtime performance and speedup gates | `docs/SPEEDUP_DESIGN.md` |
| Statistics (cost, AIC, interval ladder, coverage) | `docs/STATS_PRIMER.md`, `docs/GLOSSARY.md` |
| Verdicts — meaning & what to do | `docs/INTERPRETING_A_VERDICT.md`, GLOSSARY |
| Card / schema across facilities | `docs/SCHEMA_LANDSCAPE.md` |
| Flag adjudication (opt-in LLM) | `docs/ADJUDICATION.md` |
| The autonomous driver (`isar drive`) | `docs/MEASUREMENT_STEERING.md` |
| Same-action scan-window EIG (candidate generation and exact realized scoring Partial; authoritative selection Partial — symmetric per-neutron comparison, O43; opt-in, pending O42 release review) | `docs/EIG_STEERING_DESIGN.md`, then `docs/MEASUREMENT_STEERING.md` |
| Declaring expected excitations (branch hypotheses Implemented; level/continuum Partial) | `docs/HYPOTHESES.md` |
| Campaign diagnostic report | `docs/CAMPAIGN_DIAGNOSTIC_SPEC.md` |
| Why a decision was made | `docs/DECISIONS.md` (IDs P*/D*/V/R/S/O/T/W are stable, safe to cite) |
| What happened in a run (historical, never load-bearing) | `docs/campaigns/` |

## Module map (ownership boundaries that matter)

`schema` (ScanRecord + card) · `parsers` (plugin registry: S30 SPEC, IN8 numor, TAVI, BL43LXU; card
templates in `parsers/cards/*.toml` — in8, s30, s30_raw, tavi) · `reduce` (QC mask-not-delete,
combine-before-divide, collate) · `calibrate` (elastic fits, versioned energy-zero series) · `analyze`
(structure enumeration, fitting, errors, model selection, verdict, confidence) · `session` (workspace/
prior/branch-model assembly) · `cli` · `drive` (autonomous loop, action/Q EIG, unscored scan design,
TAVI I/O) ·
`report` · `synth` (Phase-1 synthetic core) · `workspace_api` / `geometry` / `plotspec` (Qt-free) ·
`isar_gui` (reads science through `workspace_api`; controls campaigns by subprocess + control
files). The Stage II ↔ III boundary is a **frozen file contract**
(`report/results.csv`, `<workspace>/report/report.json`, `card.state.json`) — the driver never imports fit internals.
The one pre-release v1→v2 clean break was authorized on 2026-07-17 (DECISIONS O12); from v2 onward,
contract changes are additive only (DESIGN §4).

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
4. **Status discipline:** DESIGN §15 marks every capability Implemented / Partial / Planned. Do not
   describe Planned work in the implemented voice, and match that table when you change status.
5. **STAT-2b history is immutable:** never refresh the completed held-out gate or aggregate to match
   current production. Authenticate the aggregate's raw bytes and reproduce its complete semantic
   fingerprint; maintain current production closure in the separate candidate/active cycle governed
   by S9. Bootstrap uses history only when no active closure exists; renewal authenticates and keeps
   the active file in force while writing a candidate against its source and graph identities.
6. **Close protected drift before merge:** a protected-semantic mismatch requires selection or
   evidence-contract review with proportionate proof; protected-adjacent drift requires bounded
   source review and focused parity. A branch may be temporarily red while the boundary is updated,
   but the mismatch must be closed before its statistical slice or containing PR merges.
7. **No automatic source-closure approval:** passing tests and verification establish eligibility
   for review, never owner approval. Promotion requires an explicit owner decision and a separate
   minimal commit that changes no protected production source. The active manifest must bind the
   exact preceding candidate commit, candidate bytes, and immutable review bytes; Git-backed
   verification proves all three.
8. **The whole driver is selection-critical:** protect all of `isar/session/driver.py`. Even a
   harmless change needs source review and focused parity until authority-seam extraction lands as a
   separately reviewed refactor.

## Documentation policy (what a document is allowed to be)

Every maintained `.md` declares three **orthogonal** fields in a banner under its title. Conflating
them is what previously made the tree unreadable — a document's origin is not its status, and
neither is its authority.

- **Lifecycle** — exactly one of `Current`, `Active plan`, `Historical evidence`, `Archived`.
- **Authority** — *optional*. Only a document that is the single canonical source for a topic
  declares one. Explanatory and historical documents have none, and inventing one for them is how
  two documents end up claiming the same ground.
- **Provenance** — `hand-written`, or `generated by <command>`. A generated file is owned by its
  generator; edit the generator, not the output.

The register of every file and its fields is [`docs/DOC_INVENTORY.md`](docs/DOC_INVENTORY.md).

Rules, each naming the failure it prevents:

1. **A `Current` document may not take an `Archived` or `Historical evidence` document as its
   normative source.** Citing one for *rationale* or *provenance* is fine and expected — the test
   is whether deleting the citation would leave a claim unsupported. Without this, a repaired
   historical failure silently governs current behaviour.
2. **Campaign notes are evidence, never contract** (DECISIONS D3). They record what happened on a
   date, including things later found wrong. `docs/READING_GUIDE.md` must not route a reader
   through them to learn current behaviour.
3. **A handoff is temporary.** Once its receiving work starts it converts to an active plan, folds
   into a canonical reference, or is deleted. There is no long-lived handoffs directory, because a
   finished handoff that looks active becomes a phantom work queue.
4. **An execution plan leaves the tree when its work lands.** Git history keeps the task list.
5. **An `Active plan` needs an owner and exit criteria.** Without them it is a wish, and nobody can
   say whether it is finished.
6. **Deletion is preferred to a growing archive**, on an irreplaceability test: does this record
   reasoning or evidence that exists nowhere else? If not, delete it and record it in the
   `DOC_INVENTORY.md` tombstone table with the last commit that contained it.

When you change behaviour, commands, schemas, or capability status, update the governing document
in the same change — and if the claim is machine-checkable, add it to `tests/docs/`.

`tests/docs/test_doc_drift.py` already enforces the mechanical half:

- the register in `DOC_INVENTORY.md` is a **one-to-one mapping** with the non-exempt half of
  `git ls-files '*.md'` — a tracked file with no row fails, a path with two rows fails, and a row
  naming a file that does not exist fails;
- every tracked `.md` declares a valid lifecycle and provenance, and every `Active plan` declares
  an owner and exit criteria (rule 5);
- relative links resolve case-sensitively, and tombstoned files stay deleted at repository-relative
  paths that `git show <commit>:<path>` accepts verbatim;
- no maintained document reintroduces a phrase that was corrected once (`STALE_PHRASES`);
- a command list marked `<!-- isar-commands: exhaustive -->` matches
  `isar.cli.main.build_parser()`;
- `DECISION_INDEX.md` matches a fresh render of the ledger, and the `docs/assets/` bundle matches
  its recorded hashes — the same checks as `python tools/docs/make_decision_index.py --check` and
  `python tools/docs/collect_evidence.py --check`.

**Adding a new document therefore requires a register entry and a banner** — the test fails
otherwise, by design. Rule 1's *normative source* judgement is not machine-checkable and stays a
review item.

---

*Last updated: 2026-07-26. Canonical file is `AGENTS.md`; `CLAUDE.md` is the `@AGENTS.md` stub.*

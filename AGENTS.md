# ISAR — Agent Floor

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
- **Tests:** `pytest -m "not slow"` (markers `slow`, `live`; `testpaths` = `isar/synth/tests`, `tests`
  in `pyproject.toml`). `live` needs a running TAVI at `127.0.0.1:8642`. `tests/real_data/` needs the
  gitignored local `test_data/`. `isar/synth/tests/` = Phase-1 synthetic core; `tests/` = Phase 2+.
- **Verification status (2026-07-12, evening):** the **base Python** (`C:\Utilities\Python\Python312`)
  has every dependency (PySide6 6.11, pytest-qt, numpy/scipy/iminuit/lmfit/dynesty) and imports the
  repo-root `isar` package directly — `python -m pytest -m "not slow"` from the repo root RUNS and
  was green this session (1494 passed / 3 skipped; GUI subset 277). No `isar` micromamba env exists
  yet; do not build one just to test. Offscreen GUI runs: pytest sets the platform via
  `tests/gui/conftest.py`; a test that leaves a modal path unmocked will hang the suite silently —
  use `-o faulthandler_timeout=60` when debugging a stall.

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
   `isar/geometry` (vendored from TAVI), and `isar/plotspec` must import no Qt. `isar_gui`
   (PySide6 + pyqtgraph) is a **read-only viewer** over `workspace_api`. A Qt import leaking into the
   facade or geometry breaks the notebook/headless consumers. Correspondingly, **no LLM in the runtime
   path of a number** (P1): LLM output freezes into config before the deterministic fitter runs.

## Before You Touch X

| Task area | Read first |
|---|---|
| Orientation / why trust it | `docs/READING_GUIDE.md` → `DESIGN.md` |
| Running it on data / the CLI contract | `docs/USING_ISAR.md`, `docs/WORKED_EXAMPLE.md` |
| Current build phase (CLI + session assembly) | `docs/UNIFICATION_DESIGN.md` |
| Statistics (cost, AIC, interval ladder, coverage) | `docs/STATS_PRIMER.md`, `docs/GLOSSARY.md` |
| Verdicts — meaning & what to do | `docs/INTERPRETING_A_VERDICT.md`, GLOSSARY |
| Card / schema across facilities | `docs/SCHEMA_LANDSCAPE.md`, `docs/DATA_SCHEME_RESEARCH.md` |
| Flag adjudication (opt-in LLM) | `docs/ADJUDICATION.md` |
| The autonomous driver (`isar drive`) | `docs/MEASUREMENT_STEERING.md` |
| Campaign diagnostic report | `docs/CAMPAIGN_DIAGNOSTIC_SPEC.md` |
| Why a decision was made | `docs/DECISIONS.md` (IDs P*/D*/V/R/S/O/T/W are stable, safe to cite) |
| What happened in a run (historical, never load-bearing) | `docs/campaigns/` |

## Module map (ownership boundaries that matter)

`schema` (ScanRecord + card) · `parsers` (plugin registry: S30 SPEC, IN8 numor, TAVI, BL43LXU; card
templates in `parsers/cards/*.toml` — in8, s30, s30_raw, tavi) · `reduce` (QC mask-not-delete,
combine-before-divide, collate) · `calibrate` (elastic fits, versioned energy-zero series) · `analyze`
(structure enumeration, fitting, errors, model selection, verdict, confidence) · `session` (workspace/
prior/branch-model assembly) · `cli` · `drive` (autonomous loop, EIG acquisition, TAVI I/O) ·
`report` · `synth` (Phase-1 synthetic core) · `workspace_api` / `geometry` / `plotspec` (Qt-free) ·
`isar_gui` (read-only viewer). The Stage II ↔ III boundary is a **frozen file contract**
(`report/results.csv`, `report.json`, `card.state.json`) — the driver never imports fit internals;
contract changes are additive only (DESIGN §4).

## Verification (cheapest first)

1. **Parse:** `python -m py_compile` the changed files (or your editor's syntax check).
2. **Import:** import the changed public surface (e.g. `import isar.analyze.components`); for GUI work
   confirm no Qt leaked into `workspace_api`/`geometry`/`plotspec`.
3. **Tests:** `pytest -m "not slow"` from repo root in the `isar` env. Real-data subset needs local
   `test_data/`; `live` needs TAVI at `127.0.0.1:8642`.
4. **Status discipline:** DESIGN §15 marks every capability Implemented / Partial / Planned. Do not
   describe Planned work in the implemented voice, and match that table when you change status.

---

*Last updated: 2026-07-12. Canonical file is `AGENTS.md`; `CLAUDE.md` is the `@AGENTS.md` stub.*

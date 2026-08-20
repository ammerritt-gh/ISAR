# ISAR

> **Status:** live
> **Authority:** canonical for product overview and quickstart
> **Verified against code revision:** `9914eb7` (2026-07-26)

**I**nelastic **S**cattering **A**utomated **R**— (the final R is deliberately floating: the tool
*reduces* data at ingest, *resolves* phonons in analysis, *reviews* scans for the human, and — in a
future phase — acts in *resolution* mode to solve a measurement problem). Also, fittingly, the
river through Munich.

ISAR is an automated **triage** pipeline for inelastic scattering data (IXS and triple-axis):
during a beamtime it parses incoming scans, quality-checks and calibrates them, fits every
measurement (DHO ⊗ resolution, physics-gated model selection, honest uncertainties), collates by
Q, and produces a report that says — per scan — *use this number*, *glance at this*, or *look at
this yourself*. It is fast feedback for steering an experiment, not the system of record for
published numbers. Nothing is ever deleted; a human reviews everything afterward.

> **Status: development.** The analysis pipeline is built and validated on real data from two very
> different instruments (APS Sector-30 HERIX and ILL IN8). The `isar` CLI runs it end-to-end on new
> data ([docs/USING_ISAR.md](docs/USING_ISAR.md)); one validation (the CLI/session equivalence gate
> at M=200) is still pending. An autonomous driver (`isar drive`) closes the loop against a
> simulated instrument (TAVI): model-free spectral descriptor, Bayesian branch assignment,
> cost-normalised expected-information-gain acquisition. The analytic synthetic tier is certified,
> and TAVI's **deterministic** rung has run; realistic clocks and the McStas rung remain
> Planned, and no campaign has run on a real instrument. See
> [docs/design/steering.md](docs/design/steering.md) for why it steers the way it does.
>
> **Per-capability status — Implemented / Partial / Planned — lives in one place:
> [docs/ARCHITECTURE.md §15](docs/ARCHITECTURE.md). That table is the authority; this paragraph is orientation.**

## The pipeline

Three stages, the same three [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) uses throughout:

1. **Parse** — per-instrument parser plugins (auto-detected) emit one normalized `ScanRecord`, and
   every per-experiment decision lives in a human-editable **experiment card**, not in code. Then
   deterministic QC that flags and masks but never deletes, combine-before-divide, and energy-zero
   and resolution calibration from your elastic/Bragg scans, versioned.
2. **Fit & Verdict** — scans classified (measurement / alignment / calibration) and collated by
   (sample, Q, T) per card policy, with coverage and gaps surfaced; then physics-gated structure
   enumeration → deterministic fits → AIC/model selection → bootstrap/profile (and optionally
   Bayesian) errors → a computed **verdict + confidence** per scan, seeded by a running dispersion
   prior and your own guidelines in the card. LLM adjudication of QC flags is optional and off the
   critical path — the pipeline is fully deterministic by default.
3. **Steer** — `isar drive` closes the loop: from a measurement objective it proposes the next
   scan, drives the instrument, fits what comes back, and decides where to measure next and when
   to stop.

The workflow (the contract in [docs/USING_ISAR.md](docs/USING_ISAR.md)):

```
isar init "path\to\first\scan" --out my_experiment   # detect instrument, scaffold the card
# edit my_experiment\card.policy.toml                 # detectors, expectations, known-bad scans
isar run "path\to\data" --workspace my_experiment     # parse -> fit & verdict -> report
# re-run the same command as new scans land; only new work is done
```

The full command set:

<!-- isar-commands: exhaustive -->
```
init  detect  ingest  run  status  profile  report  diagnose  score  view  scene-build  drive  answer
```
<!-- /isar-commands -->

`isar <command> --help` documents the flags for each.

## Where to read what

Five doors. **[docs/READING_GUIDE.md](docs/READING_GUIDE.md) is the full map** — every other
document is indexed there, with its lifecycle, so there is one place to look rather than three.

| Question | Where |
|---|---|
| What is ISAR for / why trust it? | [docs/READING_GUIDE.md](docs/READING_GUIDE.md) |
| How do I run it on my data? | [docs/USING_ISAR.md](docs/USING_ISAR.md) |
| What does a verdict mean / what do I do with it? | [docs/INTERPRETING_A_VERDICT.md](docs/INTERPRETING_A_VERDICT.md) |
| The architecture, principles, and per-capability status | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| Everything else, indexed | [docs/READING_GUIDE.md](docs/READING_GUIDE.md) |

## Repository layout

```
isar/
  schema/         # ScanRecord + experiment card (policy TOML + state JSON)
  parsers/        # registry + plugins (S30 SPEC supplementary/raw, IN8 numor) + card templates
  reduce/         # QC (mask-not-delete), combine, collate, LLM adjudication (opt-in)
  calibrate/      # elastic fits, time-keyed energy-zero series, Bragg reader
  analyze/        # structure enumeration, ICs, fitting loop, errors, selection, verdict, confidence
  session/        # workspace/session assembly: driver, priors, branch model, worker, timing
  cli/            # the isar command (see the full command set above)
  drive/          # autonomous loop: acquisition (EIG), TAVI I/O, supervisor, session log
  report/         # figures, findings, markdown/html renderers
  synth/          # Phase-1 synthetic core: generator, DHO fitter, estimators, MC scoring
  workspace_api/  # read-only facade over a workspace (fits, scans, ledger, evaluate_model,
                  #   watch) — the GUI's data layer and the notebook interface, Qt-free
  geometry/       # metric/UB + feasibility layer vendored from TAVI/TAS_MCP, Qt-free
  plotspec/       # shared plot-spec layer (matplotlib + pyqtgraph backends) so report figures
                  #   and GUI views share rendering semantics
isar_gui/         # optional GUI package (`pip install -e .[gui]`, `isar view <workspace>`) —
                  #   reads science only through isar/workspace_api (never re-fits or writes);
                  #   controls campaigns by owning subprocesses + control files
docs/             # design docs, guides, and the *_check.py validation drivers
docs/assets/      # the committed validation evidence the docs cite (derived only — see its README)
test_data/        # real facility files (S30, IN8, ID28, BL43LXU) — LOCAL-ONLY, gitignored
tests/            # unit + real-data smoke tests (real-data tests need the local test_data/)
out/              # working output (gitignored; run a docs/*_check.py to produce a report)
```

## Validation

Two deliberately separate tiers: **synthetic ground truth** (known parameters → pull/coverage of
the recovered values and error bars) and **real-data reasonableness** against human expert fits —
where the load-bearing property is *safety*: ISAR must never confidently resolve a scan the expert
rejected. Current evidence, committed under [docs/assets/](docs/assets/) so it can be read without
running anything: [phase3_gold](docs/assets/phase3_gold/FINDINGS.md) — the **analysis-core 83-scan
gold gate**, passed on the S30 gold set with 0 safety violations, median |Δω₀| = 0.16 meV, and a
usable energy delivered for 62 of the 79 scans the expert accepted — and
[in8_phase1](docs/assets/in8_phase1/FINDINGS.md) (same core on a triple-axis instrument). That gate
drives the analysis modules directly; the matching **CLI/session equivalence gate at M=200**, which
runs the same 83 scans end-to-end through `isar run`, is still open (`VAL-2` in [TODO.md](TODO.md)).
The bundle is derived evidence only — fit results, summary tables, and figures. Regenerating it
needs the `docs/*_check.py` scripts and the local-only `test_data/`, which is **not** committed, so
a clean checkout can *see* this evidence but cannot reproduce it.

## Setup

Python ≥ 3.11. `pip install -e .` (test extras: `pip install -e .[test]`; run `python -m pytest -m
"not slow" -n 16 --dist worksteal` — about a minute, and the `tests/real_data/` subset requires the
local-only `test_data/`; `-n 16` is tuned for an idle 32-core box — on a smaller machine use
`-n auto`; drop `-n 16 --dist worksteal` to run serially when debugging). To also run
the GUI tests under `tests/gui/`, install `pip install -e .[test,test-gui,gui]` (the `test-gui` extra is
`pytest-qt`, kept out of the base `test` extra so it doesn't require a Qt binding). LLM features are
optional and require a configured LLM endpoint: local LLMM at `127.0.0.1:8766` is the default
offline profile, and an OpenAI-compatible endpoint is also supported — see
[docs/ADJUDICATION.md](docs/ADJUDICATION.md) for the profiles, configuration, and fallback contract.

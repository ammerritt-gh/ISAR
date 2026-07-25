# ISAR

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
> data ([docs/USING_ISAR.md](docs/USING_ISAR.md)); one validation (the full 83-scan S30 statistical
> gate at M=200) is still pending. An autonomous driver (`isar drive`) closes the loop against a
> simulated instrument (TAVI): model-free spectral descriptor, Bayesian branch assignment,
> cost-normalised expected-information-gain acquisition. This is certified on the analytic
> synthetic tier and on TAVI's **deterministic** rung; realistic clocks and the McStas rung remain
> Planned, and no campaign has run on a real instrument. See
> [docs/MEASUREMENT_STEERING.md](docs/MEASUREMENT_STEERING.md) for why it steers the way it does.
>
> **Per-capability status — Implemented / Partial / Planned — lives in one place:
> [DESIGN.md §15](DESIGN.md). That table is the authority; this paragraph is orientation.**

## The pipeline

1. **Parse** — per-instrument parser plugins (auto-detected) emit one normalized `ScanRecord`;
   every per-experiment decision lives in a human-editable **experiment card**, not in code.
2. **Reduce** — deterministic QC that flags and masks but never deletes; combine-before-divide;
   energy-zero and resolution calibration from your elastic/Bragg scans, versioned.
3. **Review** — scans classified (measurement / alignment / calibration), collated by (sample, Q,
   T) per card policy, coverage and gaps surfaced; optional LLM adjudication of QC flags (off the
   critical path — the pipeline is fully deterministic by default).
4. **Fit** — physics-gated structure enumeration → deterministic fits → AIC/model selection →
   bootstrap/profile (and optionally Bayesian) errors → a computed **verdict + confidence** per
   scan, seeded by a running dispersion prior and your own guidelines in the card.

The workflow (the contract in [docs/USING_ISAR.md](docs/USING_ISAR.md)):

```
isar init "path\to\first\scan" --out my_experiment   # detect instrument, scaffold the card
# edit my_experiment\card.policy.toml                 # detectors, expectations, known-bad scans
isar run "path\to\data" --workspace my_experiment     # parse -> reduce -> review -> fit -> report
# re-run the same command as new scans land; only new work is done
```

The full command set:

<!-- isar-commands: exhaustive -->
```
init  detect  ingest  run  status  profile  report  diagnose  score  view  drive
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
| The architecture, principles, and per-capability status | [DESIGN.md](DESIGN.md) |
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
                  #   read-only viewer against isar/workspace_api, isar/geometry, isar/plotspec
docs/             # design docs, guides, and the *_check.py validation drivers
test_data/        # real facility files (S30, IN8, ID28, BL43LXU) — LOCAL-ONLY, gitignored
tests/            # unit + real-data smoke tests (real-data tests need the local test_data/)
out/              # generated reports (gitignored; run a docs/*_check.py to produce one)
```

## Validation

Two deliberately separate tiers: **synthetic ground truth** (known parameters → pull/coverage of
the recovered values and error bars) and **real-data reasonableness** against human expert fits —
where the load-bearing property is *safety*: ISAR must never confidently resolve a scan the expert
rejected. Current evidence: `out/phase3_gold/FINDINGS.md` (83-scan S30 gold set — 0 safety
violations, median |Δω₀| ≈ 0.16 meV) and `out/in8_phase1/FINDINGS.md` (same core on a triple-axis
instrument), regenerated by the `docs/*_check.py` scripts — which need the local-only `test_data/`
(facility data is not committed).

## Setup

Python ≥ 3.11. `pip install -e .` (test extras: `pip install -e .[test]`; run `pytest -m "not
slow"` — the `tests/real_data/` subset requires the local-only `test_data/`). To also run the GUI
tests under `tests/gui/`, install `pip install -e .[test,test-gui,gui]` (the `test-gui` extra is
`pytest-qt`, kept out of the base `test` extra so it doesn't require a Qt binding). LLM features are
optional and require a local [LLMM](docs/archive/LLMM_INTEGRATION_HANDOFF.md) endpoint.

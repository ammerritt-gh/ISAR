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
> different instruments (APS Sector-30 HERIX and ILL IN8). The `isar` CLI
> (`init/run/status/report/score/profile`) runs it end-to-end on new data
> ([docs/UNIFICATION_DESIGN.md](docs/UNIFICATION_DESIGN.md), [docs/USING_ISAR.md](docs/USING_ISAR.md));
> one validation (the full 83-scan S30 statistical gate at M=200) is still pending. An autonomous
> driver (`isar drive`) closes the loop against a simulated instrument (TAVI): model-free spectral
> descriptor, Bayesian branch assignment, cost-normalised expected-information-gain acquisition. It
> has passed its first certification scorecard on the synthetic zone re-test; two physics zones
> (near-Γ, zone boundary) remain open. See
> [docs/MEASUREMENT_STEERING.md](docs/MEASUREMENT_STEERING.md) (why it steers the way it does) and
> [docs/MVP_ZONE_RETEST.md](docs/MVP_ZONE_RETEST.md) (current test state).

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

The intended workflow once the CLI lands (the contract in
[docs/USING_ISAR.md](docs/USING_ISAR.md)):

```
isar init "path\to\first\scan" --out my_experiment   # detect instrument, scaffold the card
# edit my_experiment\card.policy.toml                 # detectors, expectations, known-bad scans
isar run "path\to\data" --workspace my_experiment     # parse -> reduce -> review -> fit -> report
# re-run the same command as new scans land; only new work is done
```

## Where to read what

| Question | Where |
|---|---|
| What is ISAR for / why trust it? | [docs/READING_GUIDE.md](docs/READING_GUIDE.md) |
| How do I run it on my data? | [docs/USING_ISAR.md](docs/USING_ISAR.md) |
| The master design (architecture, principles) | [DESIGN.md](DESIGN.md) |
| The current build phase (CLI + session assembly) | [docs/UNIFICATION_DESIGN.md](docs/UNIFICATION_DESIGN.md) |
| What a verdict means / what to do with it | [docs/INTERPRETING_A_VERDICT.md](docs/INTERPRETING_A_VERDICT.md) |
| Term definitions | [docs/GLOSSARY.md](docs/GLOSSARY.md) |
| Why these statistics | [docs/STATS_PRIMER.md](docs/STATS_PRIMER.md) |
| One scan, start to finish | [docs/WORKED_EXAMPLE.md](docs/WORKED_EXAMPLE.md) |
| The autonomous loop — research foundation & design | [docs/MEASUREMENT_STEERING.md](docs/MEASUREMENT_STEERING.md) |
| Autonomous campaign results (first run, zone re-test) | [docs/MVP_FIRST_RUN.md](docs/MVP_FIRST_RUN.md), [docs/MVP_ZONE_RETEST.md](docs/MVP_ZONE_RETEST.md) |
| Where did a campaign spend its time? | `isar profile` in [docs/USING_ISAR.md](docs/USING_ISAR.md) |
| Phase design rationale | docs/archive/PHASE_1…3_DESIGN.md, docs/PHASE_4/5_NOTES.md |

## Repository layout

```
isar/
  schema/     # ScanRecord + experiment card (policy TOML + state JSON)
  parsers/    # registry + plugins (S30 SPEC supplementary/raw, IN8 numor) + card templates
  reduce/     # QC (mask-not-delete), combine, collate, LLM adjudication (opt-in)
  calibrate/  # elastic fits, time-keyed energy-zero series, Bragg reader
  analyze/    # structure enumeration, ICs, fitting loop, errors, selection, verdict, confidence
  session/    # workspace/session assembly: driver, priors, branch model, worker, timing
  cli/        # the isar command (init/run/status/report/score/profile/drive)
  drive/      # autonomous loop: acquisition (EIG), TAVI I/O, supervisor, session log
  report/     # figures, findings, markdown/html renderers
  synth/      # Phase-1 synthetic core: generator, DHO fitter, estimators, MC scoring
docs/         # design docs, guides, and the *_check.py validation drivers
test_data/    # real facility files (S30, IN8, ID28, BL43LXU) — LOCAL-ONLY, gitignored
tests/        # unit + real-data smoke tests (real-data tests need the local test_data/)
out/          # generated reports (gitignored; run a docs/*_check.py to produce one)
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
slow"` — the `tests/real_data/` subset requires the local-only `test_data/`). LLM features are
optional and require a local [LLMM](docs/archive/LLMM_INTEGRATION_HANDOFF.md) endpoint.

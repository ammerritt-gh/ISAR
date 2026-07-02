# Using ISAR — the command-line workflow

How to run ISAR on your own data: set up an experiment once, then re-run one command as scans land.
Written for a scientist at (or preparing for) a beamtime. For what ISAR *is* and why to trust it,
start with [READING_GUIDE.md](READING_GUIDE.md); for what a verdict means, see
[INTERPRETING_A_VERDICT.md](INTERPRETING_A_VERDICT.md).

> **Status: this documents the v1 CLI specified in [UNIFICATION_DESIGN.md](UNIFICATION_DESIGN.md),
> currently being built.** It is written first, deliberately, as the contract the build must meet:
> when the unification phase lands, everything on this page must be literally true. If you're
> reading this before then, the `docs/*_check.py` scripts are still the only way to run ISAR.

## The idea in one paragraph

You describe your experiment **once**, in a human-editable *experiment card* (a TOML file ISAR
scaffolds for you). After that, `isar run` does everything: it parses whatever new files appeared,
classifies each scan (measurement / alignment / calibration), quality-checks and masks points
(never deleting anything), keeps the energy-zero and resolution calibration up to date from your
elastic scans, fits every new measurement with physics-gated model selection and honest errors,
collates results by Q and temperature, and writes a triage report. Run it again after the next
batch — it only processes what's new. Your corrections go in the card; the next run applies them.

## Quickstart

```
pip install -e .

# 1. Point ISAR at a data file (or directory) — it detects the instrument and scaffolds a workspace
isar init "D:\beamtime\numors\055182" --out my_experiment

# 2. Edit my_experiment\card.policy.toml
#    - check the detector list (which analyzers carry signal THIS experiment)
#    - add anything you already know: expected phonon energies, known-bad scans (see below)

# 3. Run the pipeline on the data
isar run "D:\beamtime\numors" --workspace my_experiment

# 4. Read the report
#    my_experiment\report\report.html   (or report.md)
#    my_experiment\report\results.csv   (machine-readable, one row per scan x detector)

# ...more scans arrive during the night...
isar run "D:\beamtime\numors" --workspace my_experiment    # same command; only new scans are fitted
```

`isar init` prints these next steps, tailored to the instrument it detected.

## The workspace

`isar init` creates one directory per experiment:

| File | Who writes it | What it is |
|---|---|---|
| `card.policy.toml` | **You** (ISAR scaffolds it once, then never touches it) | Every per-experiment decision: detectors, column names, collation rules, calibration method, and your fitting guidelines. Commented — the comments are the documentation. |
| `card.state.json` | ISAR | Accumulated experiment state: energy-zero series, resolution versions, the running dispersion prior, sample map, exclusions, corrections audit trail. Don't hand-edit; don't delete. |
| `session.json` | ISAR | Bookkeeping: which files were seen, which scans were processed and how. Safe to delete — a re-run rebuilds it (refitting everything once). |
| `report/` | ISAR | The product: `report.html`/`.md`, `results.csv`, figures, plus `reduction.*` (the QC-level detail report). |

## The commands

- **`isar run <data>... --workspace DIR`** — the whole pipeline, idempotent. Useful flags:
  `--force` (refit everything), `--llm` (enable LLM adjudication of QC flags — off by default;
  needs LLMM, the co-located local LLM service, running — see
  [LLMM_INTEGRATION_HANDOFF.md](LLMM_INTEGRATION_HANDOFF.md)), `--M N` (bootstrap size; default
  200, use ~60 for a fast first look), `--detectors ana7` (restrict which analyzers are fitted
  this run).
- **`isar status --workspace DIR`** — where things stand, without reprocessing: how many scans
  fitted / held / excluded, *why* each held scan is held, gaps in the scan-number sequence and in
  Q coverage, current calibration versions.
- **`isar report --workspace DIR`** — re-render the report from stored results (fast; no refits).
- **`isar score --workspace DIR --gold your_fits.csv`** — optional: if you have your own by-hand
  fits for (some of) the same scans, score ISAR against them (the validation harness the gold
  reports use). Never required — `isar run` works on data nobody has fit before.

## Telling ISAR what you know — the `[fitting]` guidelines

The card's `[fitting]` section is how you steer the fitter without touching code. Everything here
is optional; with none of it, ISAR runs fully automatic.

```toml
[fitting]
detectors = ["ana7"]              # fit only these analyzers (default: all enabled detectors)
omega_window = [1.0, 20.0]        # sanity window (meV): a "resolved" fit outside it gets flagged

[[fitting.expectations]]          # what you expect the dispersion to do — seeds the fit
q = {component = "k", min = 0.10, max = 0.35}
omega0 = 5.2                      # expected phonon energy (meV) in that Q range
omega0_sigma = 0.5                # how sure you are
structures = ["elastic+stokes+bg"]  # optionally restrict which models are tried

[[fitting.overrides]]             # per-scan control
scan = 231
exclude = true                    # known-bad scan: never fitted, listed in the report with reason

[[fitting.overrides]]
scan = 245
omega0 = 4.0                      # per-scan starting guess when the automation picks wrong
omega0_sigma = 0.3
```

Three things to know about how guidelines behave:

- **They narrow, never widen.** An expectation seeds the initial conditions and the prior; a
  `structures` list can only remove hypotheses the physics gates already admitted. You cannot make
  ISAR fit a model the physics (detailed balance, temperature, energy window) forbids.
- **Your expectation beats the automatic prior** where it applies; elsewhere ISAR uses the running
  dispersion prior it builds from its own resolved fits (neighbors in Q at the same temperature).
- **This is also the correction channel.** ISAR mis-fits a scan → add an override or exclusion →
  `isar run` again. Only the affected scans are refitted. The report's provenance column always
  says whether a fit was seeded by *you* or by the *auto prior*.

## During a beamtime

The loop: batch lands → `isar run` → skim the report → act.

- **Triage table** — every scan × detector with its verdict (see the
  [glossary](GLOSSARY.md#what-isar-decided--verdicts-and-what-you-should-do)), ω₀ ± interval,
  confidence, and provenance. "Delivered" = resolved + flagged: numbers you can use now.
- **Dispersion summary** — ω₀ vs Q per (sample, temperature), with the running prior overlaid.
  This is the "is the experiment on track" figure.
- **Coverage & gaps** — Q points measured, holes in the scan sequence (a scan that never arrived),
  and Q-coverage gaps. Worth a glance every batch.
- **Held scans** — each with its reason. The common one early in a run is `held_for_calibration`:
  a measurement arrived before any elastic/Bragg scan established the energy zero. **Held scans
  are re-examined automatically on every later run** — measure the elastic, re-run, and they fit.
  ISAR never guesses a zero (a wrong assumed zero silently shifts every phonon energy).

Calibration is versioned: if you re-measure resolution mid-run, subsequent fits use — and record —
the new version. Nothing is ever silently re-interpreted.

## What ISAR does and doesn't do here (honest limits)

- The fitted **ω₀ is the product**; error bars are statistical-only (see
  [STATS_PRIMER.md](STATS_PRIMER.md)). On triple-axis instruments **Γ is provisional**
  (`gamma_provisional` in `results.csv`) until a 4-D resolution treatment exists — trust ω₀, not Γ.
- The LLM (when enabled with `--llm`) only *adjudicates QC flags* and writes explanations. It never
  sits in the path of a fitted number; with LLMM down, scans are stamped "no LLM consult" and the
  run proceeds.
- ISAR is a **triage** pass, not your final analysis — verdicts tell you where to spend manual
  effort, and nothing is ever deleted on your behalf.

## New instrument, no parser?

`isar init` recognizes the instruments with registered parsers (currently Sector-30 HERIX SPEC —
supplementary and raw — and ILL IN8 numor ASCII; the shared SPEC/numor readers are built for ID28
and BL43LXU to slot in as cards + thin plugins). If your format isn't recognized, a parser plugin
plus a card template is what's needed — see `DESIGN.md` §6 and `isar/parsers/base.py`; the schema
(`ScanRecord`) is fixed, only the reader varies.

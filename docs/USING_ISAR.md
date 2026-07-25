# Using ISAR — the command-line workflow

How to run ISAR on your own data: set up an experiment once, then re-run one command as scans land.
Written for a scientist at (or preparing for) a beamtime. For what ISAR *is* and why to trust it,
start with [READING_GUIDE.md](READING_GUIDE.md); for what a verdict means, see
[INTERPRETING_A_VERDICT.md](INTERPRETING_A_VERDICT.md).

> **Status: the v1 CLI described here is built** (`isar init/ingest/run/status/report/score/profile`, plus
> the autonomous `isar drive`, tested in `tests/session/`, `tests/cli/` and `tests/drive/`). It runs end-to-end on IN8
> today and on S30 once a companion `.spec` supplies the
> energy zero. One validation is still pending — the full 83-scan S30 statistical comparison at M=200
> (a multi-minute run) — so treat the S30 accuracy numbers as expected-not-yet-confirmed until that
> gate is run (see [UNIFICATION_DESIGN.md](UNIFICATION_DESIGN.md) §10 step 5).

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
| `card.policy.toml` | **You** (ISAR scaffolds it once, then never touches it) | Every per-experiment decision: detectors, column names, collation rules, calibration method, fitting guidelines, and declared branch hypotheses for autonomous campaigns ([HYPOTHESES.md](HYPOTHESES.md)). Commented — the comments are the documentation. |
| `card.state.json` | ISAR | Accumulated experiment state: energy-zero series, resolution versions, the running dispersion prior, resolved hypothesis snapshot and mismatch evidence, sample map, exclusions, corrections audit trail. Don't hand-edit; don't delete. |
| `session.json` | ISAR | Bookkeeping: which files were seen, which scans were processed and how. Safe to delete — a re-run rebuilds it (refitting everything once). |
| `report/` | ISAR | The product: `report.html`/`.md`, `results.csv`, figures, plus `reduction.*` (the QC-level detail report). |

## The commands

- **`isar run <data>... --workspace DIR`** — the whole pipeline, idempotent. Useful flags:
  `--force` (refit everything), `--llm` (enable LLM adjudication of QC flags — off by default;
  needs LLMM, the co-located local LLM service, running — see
  [ADJUDICATION.md](ADJUDICATION.md)), `--M N` (pooled-master and
  certification bootstrap cap; default 200), `--scan-M N` (provisional scan bootstrap cap; default
  50), `--detectors ana7` (restrict which analyzers are fitted
  this run), and `--workers N` (the total fitting CPU budget; default `min(30, cpu-2)`, also settable
  with `ISAR_WORKERS`). ISAR assigns that budget across scans for a large batch or within a promoted
  full/Bayes fit for a small workorder; it does not nest the two kinds of process pool.
  `--errors auto` performs HESSE-first uncertainty estimation and promotes the same point-estimation
  candidates to full errors when trust, covariance, parameter-bound, or model-weight gates require
  it. `full`, `fast`, `cheap`, and `bayes` retain their existing meanings. Drive campaigns continue
  using the validated legacy router until the Phase-2 shadow gate is recorded; final certification
  always requests `full` or `bayes` explicitly.
  Full-tier bootstrap is adaptive but deterministic for a fixed seed: draws are evaluated in blocks
  of 32, with stopping allowed only after at least 64 draws and two consecutive changes below 5% in
  the ω₀ interval half-width. The requested cap is never exceeded; a provisional `--scan-M 50` run
  therefore executes exactly 50 draws. Requested and effective draw counts are recorded in timing
  and fit provenance.
- **`isar ingest <data>... --workspace DIR [--json]`** — parse data into the ledger **without
  fitting**. It discovers and classifies the files, parses and dedups the scans, and upserts one
  `ingested` ledger entry per `(exp, scan, detector)` — so parsed scans are visible (through
  `workspace_api`, the GUI, `isar status`) before you commit to the expensive `isar run` fit. It
  never fits, never deletes, and never writes `card.policy.toml`; a later `isar run` re-queues every
  `ingested` entry. Idempotent — re-ingesting unchanged files is a no-op, and it never downgrades a
  scan already fitted/held/excluded. With `--json` it prints one machine-readable object on stdout:

  ```jsonc
  {
    "workspace": "my_experiment",
    "files": [                         // one row per classified file
      {"path": "...", "class": "measurement",   // measurement | companion | foreign | unrecognized
       "n_scans": 12,                  // measurement scans ledgered from this file (== len(scan_ids))
       "scan_ids": ["exp:1", "..."],   // scan-level ids entered from this file
       "n_context": 3}                 // in-file calibration/alignment/environment scans routed out
    ],
    "scans": {"added": ["exp:1"], "updated": [], "unchanged": []},  // scan-level "exp:scan" ids
    "deduped": [{"scan": "exp:7", "kept": "...", "superseded": ["..."]}],  // growing-file resends
    "gaps": [["exp", 4, 7]]            // (experiment, after, before) scan-number gaps
  }
  ```

  (No workspace at `DIR` prints `{"error": {"kind": "no_workspace", "message": "..."}}` and exits
  non-zero.) The GUI shells exactly this verb for its drop-to-ingest and watched-folder flows.
  Alongside the ledger, ingest writes one per-unit data file (`data/ingest/ingest-<exp>-<scan>-<det>.json`,
  schema `isar.unit_data/1`) for every `(exp, scan, detector)` — raw abscissa, honest counts/variance
  (σ²=0 kept, never energy-zero-shifted) and the 3-state calibration — so the GUI inspector can draw a
  parsed-not-fitted unit before any `isar run`. Re-ingesting also backfills a missing unit file for an
  otherwise-unchanged entry (repairs a pre-Phase-2 workspace) without ticking the ledger.
- **`isar status --workspace DIR`** — where things stand, without reprocessing: how many scans
  fitted / held / excluded, *why* each held scan is held, gaps in the scan-number sequence and in
  Q coverage, current calibration versions.
- **`isar report --workspace DIR`** — re-render the report from stored results (fast; no refits).
- **`isar score --workspace DIR --gold your_fits.csv`** — optional: if you have your own by-hand
  fits for (some of) the same scans, score ISAR against them (the validation harness the gold
  reports use). Never required — `isar run` works on data nobody has fit before.
- **`isar drive --goal "..." --workspace DIR`** — the autonomous measurement loop: from a
  natural-language goal it proposes scans, drives a virtual instrument (TAVI), fits each scan with
  the same `isar run` pipeline, and decides where to remeasure and when to stop — writing an
  audited `report/session_log.jsonl` and `report/campaign_summary.json`. Needs LLMM (goal-parsing)
  and a running TAVI instrument. Use `--spec spec.json` for an LLM-free run. **Currently drives the
  TAVI simulator only** (development tier), not a live beamline. See
  [campaigns/2026-06_MVP_FIRST_RUN.md](campaigns/2026-06_MVP_FIRST_RUN.md).
  Final certification is authoritative. If it widens one or more provisional intervals beyond the
  objective, drive automatically resumes acquisition while budget remains; no operator click is
  needed. The next certification fits only failed, changed, or previously unassessed q revisions,
  while already-passing unchanged revisions remain authoritative.
- **`isar profile --workspace DIR`** — where an autonomous campaign spent its time. Reads the
  campaign's `report/session_log.jsonl` timing events and prints a stage breakdown (propose /
  validate / submit / wait_scan / isar_run / fold / analyze …), the embedded `isar run` subprocess
  sub-stages (including per-scan interpreter/matplotlib startup overhead), and — with
  `--iterations` — a per-iteration table. Current campaigns also show each certification pass:
  total grid points, selectively carried points, newly fitted points, elapsed pass wall, and the
  slowest parallel unit with its method split. Summed certification-worker seconds are deliberately
  not presented as elapsed wall. `--log PATH` points at a different log; `--top N` shows only the N
  slowest stages. Logs written before timing instrumentation degrade to approximate per-kind gap
  estimates from event timestamps.

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

Autonomous campaigns additionally read human-owned `[hypotheses.<name>]` tables from
`card.policy.toml`. Branch hypotheses are implemented: they seed Bayesian branch beliefs, opening
moves, classification, and scan selection without forcing a fit. Zone-centre energies use
lower-truncated-normal priors (the zero-mean acoustic case is half-normal). `level` and `continuum`
remain declared plugin surfaces rather than implemented acquisition models; see
[HYPOTHESES.md](HYPOTHESES.md) for the complete contract.

For new goals such as “map the acoustic phonon along H”, ISAR uses connected `tracked_branch`
selection by default. It begins at the nearest point where the acoustic mode is predicted to clear the
local energy resolution, then walks adjacently toward and away from the zone centre. Say “lowest at
each q”, “highest at each q”, or “nearest to X at each q” only when you intentionally want independent
local selection. Resuming an existing campaign keeps its frozen selector.

`[fitting]` also carries one performance toggle, `reuse_master_structure` (default `false`):

```toml
[fitting]
reuse_master_structure = true     # opt-in speed-up for repeated (Q, T) master fits
```

When a merged "master scan" for a `(Q, T)` group is refit (a new member arrived, or `--force`),
this reuses the group's *previous* winning structure as a hint: ISAR fits only that one structure
plus a no-phonon null, skipping the full model enumeration, and accepts the shortcut only if the
same gates the enumeration relies on still pass (cost/dof within tolerance, no structured residual,
and the phonon still beats fitting nothing). On any gate failure it silently falls back to the full
enumeration, so a genuinely changed spectrum is never mis-served. It is **statistically-equivalent,
not bit-identical**: the selection layer sees a smaller candidate set, so a master's `modality`,
`confidence` and `best_label` can differ from a full re-enumeration. Off by default; it only ever
affects master (pooled) fits, never per-scan fits. Skipped fits are marked
`structure_skipped` in the ledger provenance.

## During a beamtime

The loop: batch lands → `isar run` → skim the report → act.

- **Triage table** — every scan × detector with its verdict (see the
  [glossary](GLOSSARY.md#what-isar-decided--verdicts-and-what-you-should-do)), ω₀ ± interval,
  confidence, and provenance. "Delivered" = resolved + flagged: numbers you can use now.
- **Dispersion summary** — ω₀ vs Q per (sample, temperature), with the running prior overlaid.
  This is the "is the experiment on track" figure.
- **Ambiguous candidate markers** — open markers are real fitted modes from a measured pooled
  spectrum whose target identity is unresolved. They deliberately have no certifying whisker and do
  not join the confirmed dispersion curve. They remain useful steering evidence; they are not missing
  scans and cannot seed the target prior or certify that q.
- **Coverage & gaps** — Q points measured, holes in the scan sequence (a scan that never arrived),
  and Q-coverage gaps. Worth a glance every batch.
- **Held scans** — each with its reason. The common one early in a run is `held_for_calibration`:
  a measurement arrived before any elastic/Bragg scan established the energy zero. **Held scans
  are re-examined automatically on every later run** — measure the elastic, re-run, and they fit.
  ISAR never guesses a zero (a wrong assumed zero silently shifts every phonon energy).

Calibration is versioned: if you re-measure resolution mid-run, subsequent fits use — and record —
the new version. Nothing is ever silently re-interpreted.

`maximum_production_batches_per_q` limits one same-q counting strategy; it does not declare a branch
absent. On reaching it, the campaign tries an adjacent differentiation probe. Ending as `incomplete`
with `branch_ambiguity` and `frontier_blocked` takes more than running out of bridge and revisit moves:
the driver must first exhaust its **whole registered action inventory** — every kind of measurement it
knows how to propose, including the narrow identity probe that scans a predicted rival mode rather than
counting longer at the frontier. Only when that inventory is provably empty, *and* the q is genuinely
done (dwell requests clamped to the same live instrument limit, or saturation independently shown),
does the campaign stop, preserving every candidate and naming the unresolved region.

The campaign always ends itself rather than pausing for you, but the terminal tells you which of three
things to do. A `controller_fault` is a software defect, not a data limit: checkpoints are kept and the
campaign **is** resumable once it is repaired. A stop naming a `policy_forbidden` action means the one
move left would have to leave the energy search band you declared — authorise it (`identity_search_meV`
in the objective, or re-compile the goal) and resume; more beam time alone will not help. Anything else
is genuinely data- or budget-limited, and resuming without changing something will reproduce it.

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

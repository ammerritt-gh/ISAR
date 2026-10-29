# Using ISAR — the command-line workflow

> **Status:** live
> **Authority:** canonical for the operator workflow
> **Verified against code revision:** `df7dac9` (2026-10-28)

How to run ISAR on your own data: set up an experiment once, then re-run one command as scans land.
Written for a scientist at (or preparing for) a beamtime. For what ISAR *is* and why to trust it,
start with [READING_GUIDE.md](READING_GUIDE.md); for what a verdict means, see
[INTERPRETING_A_VERDICT.md](INTERPRETING_A_VERDICT.md).

> **Status: the v1 CLI described here is built** (`isar init/ingest/run/status/report/score/profile`, plus
> the autonomous `isar drive`, tested in `tests/session/`, `tests/cli/` and `tests/drive/`). It runs end-to-end on IN8
> today and on S30 once a companion `.spec` supplies the
> energy zero. One validation is still pending — the **CLI/session equivalence gate at M=200**, the
> 83-scan S30 comparison run end-to-end through `isar run` (a multi-minute run) — so treat the S30
> accuracy numbers as expected-not-yet-confirmed until that gate is run (tracked as `VAL-2` in
> [TODO.md](../TODO.md)). The workspace/session/CLI contract itself is
> [WORKSPACE_AND_CLI.md](WORKSPACE_AND_CLI.md).

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
#    my_experiment\report\modes.csv     (one row per fitted mode, ordered by energy)

# ...more scans arrive during the night...
isar run "D:\beamtime\numors" --workspace my_experiment    # same command; only new scans are fitted
```

`isar init` prints these next steps, tailored to the instrument it detected.

## The workspace

`isar init` creates one directory per experiment:

| File | Who writes it | What it is |
|---|---|---|
| `card.policy.toml` | **You** (ISAR scaffolds it once, then never touches it) | Every per-experiment decision: detectors, column names, collation rules, calibration method, fitting guidelines, and declared branch hypotheses for autonomous campaigns ([HYPOTHESES.md](HYPOTHESES.md)). Commented — the comments are the documentation. For BL43 the format keys come from the shipped file and per-scan values are entered in ISAR, never here ([BL43's settings](#where-bl43s-settings-live)). |
| `card.state.json` | ISAR | Accumulated experiment state: energy-zero series, resolution versions, the running dispersion prior, resolved hypothesis snapshot and mismatch evidence, sample map, exclusions, corrections audit trail. Don't hand-edit; don't delete. |
| `session.json` | ISAR | Bookkeeping: which files were seen, which scans were processed and how. Safe to delete — a re-run rebuilds it (refitting everything once). |
| `report/` | ISAR | The product: `report.html`/`.md`, `results.csv` (one summary row per spectrum, with its peak-count state), `modes.csv` (every fitted mode of the selected count, ordered by energy), figures, plus `reduction.*` (the QC-level detail report). `report/exports/` holds the folders `isar export` writes (see *Exporting results* below). |

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
  Every spectrum is searched by the fast pass (a seed, then every peak count to the mode ceiling with its
  audits; there is no second pass). `--errors` chooses only what is attached to the selected model
  afterwards, and defaults to `fast` (the delta-method ω₀ interval and its trust flag, no bootstrap).
  `cheap` gives covariance errors only; `auto` is `fast`, promoted to `full` when the trust, covariance,
  parameter-bound, or model-weight gates require it; `full` adds MINOS, bootstrap and profile intervals;
  `bayes` adds the nested-sampling interval. Final certification always requests `full` or `bayes`
  explicitly. `isar drive` fits through the same `isar run` path.
  `--deep-pass` (with `--deep-budget-minutes` and `--deep-workers`) sends the spectra this run fits to the
  slow deep pass; see [The deep pass](#the-deep-pass-a-closer-look-at-spectra-you-choose).
  `--profile` runs the whole batch in one process with one worker under cProfile and writes
  `report/run_profile.prof` and `report/run_profile.txt`; add `--detectors` and `--force` to profile one
  spectrum. Per-spectrum and per-stage times are in `report/run_timings.json`.
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
    "passed_over": [{"set": "...", "scans": [58, 59], "reason": "..."}],  // summed sets whose scans are present
    "gaps": [["exp", 4, 7]]            // (experiment, after, before) scan-number gaps
  }
  ```

  (No workspace at `DIR` prints `{"error": {"kind": "no_workspace", "message": "..."}}` and exits
  non-zero.) The GUI shells exactly this verb for its drop-to-ingest and watched-folder flows.
  Alongside the ledger, ingest writes one per-unit data file (`data/ingest/ingest-<exp>-<scan>-<det>.json`,
  schema `isar.unit_data/1`) for every `(exp, scan, detector)` — raw abscissa, honest counts/variance
  (σ²=0 kept, never energy-zero-shifted), per-point monitor and 3-state calibration — so the GUI
  inspector can draw a parsed-not-fitted unit before any `isar run`. Re-ingesting also backfills a missing unit file for an
  otherwise-unchanged entry (repairs a pre-Phase-2 workspace) without ticking the ledger.
- **`isar status --workspace DIR`** — where things stand, without reprocessing: how many scans
  fitted / held / excluded, *why* each held scan is held, gaps in the scan-number sequence and in
  Q coverage, current calibration versions.
- **`isar report --workspace DIR`** — re-render the report from stored results (fast; no refits).
- **`isar export --workspace DIR`** — write one new folder of the stored results under `report/exports/`,
  for use outside ISAR. Nothing is refitted or reparsed. The desktop window's *Export…* runs this;
  see *Exporting results* below.
- **`isar score --workspace DIR --gold your_fits.csv`** — optional: if you have your own by-hand
  fits for (some of) the same scans, score ISAR against them (the validation harness the gold
  reports use). Never required — `isar run` works on data nobody has fit before.
- **`isar drive --goal "..." --workspace DIR`** — the autonomous measurement loop: from a
  natural-language goal it proposes scans, drives a virtual instrument (TAVI), fits each scan with
  the same `isar run` pipeline, and decides where to remeasure and when to stop — writing an
  audited `report/session_log.jsonl` and `report/campaign_summary.json`. Needs LLMM (goal-parsing)
  and a running TAVI instrument. Goal-parsing asks LLMM for the **`scientific_interpreter` role**,
  so which model reads your goal — and what it falls back to when that model is unhealthy — is an
  LLMM role-store setting, not an ISAR one: change it in LLMM (`PUT /v1/roles`, or the Roles tab),
  never in ISAR. `LLMM_SUPERVISOR_MODEL=<exact ref>` bypasses the role store for one run when you
  need to test a specific model. Use `--program program.json` for an LLM-free run — an
  `isar.campaign-program/1` file, which `--goal … --compile-only program.json` writes for you without
  contacting TAVI. **Currently drives the
  TAVI simulator only** (development tier), not a live beamline.
  The **budget is the only stop condition** — there is no iteration cap. A goal or program compiled by
  `isar drive` that declares no budget gets the canonical default for its tier: **1 hour** of counting time on the
  `deterministic` tier, **8 hours** on `mcstas` (`isar.drive.objective.DEFAULT_BUDGET_HOURS_BY_TIER`,
  overridable with `--budget 8h` / `--budget 1e10n`). No neutron budget is set by default, so a
  counting-time campaign is unconstrained in neutrons. On the deterministic tier the analysis
  guardrail (`--analysis-limit`) is clamped to the counting-time budget, so the stated budget is the
  whole campaign's wall time.
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

### Reading the `eig_plan` line in the campaign feed

Every iteration, before a scan is submitted, the planner writes one `eig_plan` event and the feed
renders it. It is the whole account of why this scan and not another:

- the **chosen design** — Q, energy window and step, point count, neutrons per point, its expected
  utility (prize points it should complete), its price, and its score (utility per neutron);
- the **top ten** ranked designs behind it, so the runner-up is visible;
- **per grid point** — the half-width of that point's target-energy posterior, whether it is inside
  the bar, and how many fitted peaks it has;
- the **prize count** — points inside the bar, of the points the objective asks for;
- the **intensity map** in operator units (counts per 10⁷ neutrons for each branch, the elastic
  line, and background), shown again whenever it changes;
- a **warning note** when no design is expected to complete a point: the planner takes the cheapest
  probe and says so, rather than showing a silent zero.

The bar is the objective's `tolerance_meV`, **±0.1 meV** by default — the precision ISAR can honestly
deliver on a peak centre once resolution focusing is accounted for (`docs/STATS_PRIMER.md`).
Declare a different one in the goal ("… to ±0.02 meV") or in the spec.

There is no scan-window mode to choose and no survey to switch off: the planner generates every
window from the beliefs and picks the best one per neutron. A wide window early means a wide
belief, not a survey.

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

`[fitting]` also carries one toggle, `reuse_master_structure` (default `false`):

```toml
[fitting]
reuse_master_structure = true     # seed repeated (Q, T) master fits with the previous winner
```

When a merged "master scan" for a `(Q, T)` group is refit (a new member arrived, or `--force`),
this hands the group's *previous* winning structure to the fit as one extra starting point for that
structure, like a declared peak. It saves nothing and skips nothing: the peak-count ladder still
tries every count, so a refitted master can still gain a peak. Off by default; it only ever affects
master (pooled) fits, never per-scan fits. `structure_skipped` stays `false` in the ledger
provenance.

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

A campaign never stops because one q was hard. There is no same-q ceiling and no frontier: the
planner re-ranks every candidate scan on the whole path after every result, so if counting longer at
one q stops paying, a scan somewhere else simply scores higher and wins. Ending as `incomplete`
means the planner ran out of *admissible* designs — every remaining candidate was refused by the
instrument or priced out of the budget — and the campaign says which points are still above the bar,
preserving every candidate.

The campaign always ends itself rather than pausing for you, but the terminal tells you which of three
things to do. A `controller_fault` is a software defect, not a data limit: checkpoints are kept and the
campaign **is** resumable once it is repaired. A stop naming a `policy_forbidden` action means the one
move left would have to leave the energy search band you declared — authorise it (`identity_search_meV`
in the objective, or re-compile the goal) and resume; more beam time alone will not help. Anything else
is genuinely data- or budget-limited, and resuming without changing something will reproduce it.

## The desktop window

`run-isar-gui.bat` opens the window with no console. Double-click it to open the most recent experiment,
or drag an experiment folder onto it. A start-up failure shows a message box and is written to the GUI
log (*Help ▸ Open GUI Log Folder* opens it). *Help ▸ Quick guide* is a one-page walk through the first use
(drop data, choose the curves, run, read the list, export), and *Help ▸ What the status words mean* lists the
plain status words with the fitting code's word and meaning for each; both open in one reusable window that
does not block the main window. The guide is `isar_gui/resources/quick_guide.html`, shipped with ISAR; the
words page is generated from the table the scan list displays from.

With no experiment open, the window says *No experiment open*, with *New experiment…*, *Open…*,
*Recent ▾* and a labelled drop zone. The arrangement is the same from then on, on a laptop and on a
desktop: the experiment's setup, what needs attention and the scan list down the left, the display in the centre, *Processing* and *Campaign* on the
right, and the Log and Output along the bottom. On a laptop the right panel starts collapsed to a
*Processing ‹* rail. The centre has four pages: *Scan(s)*, *Plane (q, E)*, *Dispersion ω(q)* and
*Sandbox*. With the *Auto* switch on (the default), selecting a spectrum or recalling a group switches to *Scan(s)*;
starting a campaign switches to *Plane*. Manually choosing a centre page turns *Auto* off, so later
selection changes leave that page in place. *View ▸ Reset layout* returns to the default for the current screen.

### Dropping data

Drop data files or a folder anywhere in the window. With no experiment open, ISAR creates one, named
after the folder you dropped (or the files' common name), in `Documents\ISAR\`, reads the files into it,
and says so in the Log. Nothing asks you for a name first; a one-time note in the experiment line offers
*Rename*. With an experiment open, a drop is read into it. The experiment is a real folder from the
start, and what ISAR computes is written there as it arrives. There is no temporary workspace and no
*Save As* step.

*Rename* changes the folder's name and the experiment's name together. It is refused, with the reason
shown, while an ingest, an analysis, a campaign or an export is writing to the folder, and when the
experiment holds its own source data files, since its manifest names them inside the folder and a rename
would leave ISAR unable to find them. When the experiment has fits and its resolution curves sit inside
its folder, the Rename dialog says that adding those spectra to the queue again will refit them, because ISAR
recognises a curve set by its folder's path.

### The files strip

The strip under the experiment line counts what has been read (for example "58 files · 2 scan sets"),
and offers *Add data ▾* (files or a folder) and *Watch a folder…*. *Watch a folder…* opens the watched-folders
dialog: add folders, stop watching them (files already read stay in the experiment), pause watching, and see the
last check and how many new files it found. Watching works
as it did before. Files that could not be read are listed under *Files not read* in the scan list, each
with its reason.

### The setup section

Under the experiment line, for a BL43LXU experiment, the setup section says what ISAR read and where it
found the resolution curves.

- **What was read**: for example "2 scan sets · 56 analyzers read · 1 incomplete set · 3 files not read".
  *Incomplete set* and *files not read* open, in place, the list behind them: each incomplete set with the
  analyzers it lacks and why (and the state of its sidecar), and each unread file with its reason (hover
  shows its full path). Before the first import it says "Nothing read yet".
- **Resolution curves**: "Resolution curves: <folder> (found beside the data)", "(chosen by you)", or, in the warning colour, "No resolution curves yet. Choose
  the folder that holds them." (the backend's longer text, with the folder it looked beside, is on hover).
  *Choose curves folder…* opens a folder chooser and saves the folder you pick at once as this experiment's
  resolution folder; the analysis uses it. The same button, named *Choose resolution-curve folder…*, sits in the
  needs-attention group and the readiness list that ask for it. A saved folder is checked at the next import or
  when an analysis starts, and the section says so until then: a curve file added or replaced in the folder is
  seen at that point, not before. If another `rf/` folder was found beside newly dropped data, the section names
  it with *Use this curves folder*; ISAR never switches to it by itself. Hover the counts line for what a scan
  set, an analyzer and a spectrum are.

*Experiment info…* on the experiment line opens the experiment's name (with *Rename…*), its folder, and six optional
fields: title, proposal, sample, people, dates and notes. They are kept with the experiment and no fit reads
them. *Save details* keeps every other value already stored.

### What needs attention

The *Needs attention · N* list sits under the setup section. It shows the groups the engine reports for the
experiment, in its order and its words: what is missing or uncertain, which steps it affects (read, reduce,
calibrate, fit, steer) and what resolves it. It opens the first time an experiment has gaps and stays as you
leave it after that; it scrolls inside a fixed height so the scan list below keeps its room. A group closes
when the engine's re-read says its gap is gone, never because the window decided so.

A group about a missing temperature or slit takes a value:

1. Open the group and tick the scan sets it concerns (a set row opens to its analyzers).
2. Type the value (temperature in K; slit horizontal and vertical in mm) and, if you like, a reason. The line
   under the entry says which scan sets it applies to, and "Not saved yet; press Save" shows while typed text is unsaved.
3. Press *Save my value*. One save writes the value to exactly the ticked scan sets or to none of them. A refused value
   shows the engine's reason under the entry and keeps your ticks and text.

Until a temperature exists the fit uses the zero-temperature limit, and the group says "stand-in value used" (hover for what the stand-in is).

**Your values are only ever added.** ISAR never edits a value a file supplies and never changes a data file. A
later value for the same scan set supersedes the earlier one; both stay on record, and the file's own value
is still shown beside yours. After the engine's groups the list keeps up to three of its own, which are not
counted: *Your temperature values* and *Your slit values* (every scan set carrying a value of yours, so *Use
the file's value* stays reachable after the gap has closed) and *Your accepted counts*. User values are taken
for temperature and slit only; Q and the other facts are shown, not entered.

Groups of analyzers (for example one with no Q) offer *Leave out analyzer*: the analyzer is then left out of the fits
of that scan until you press *Use analyzer again* on it in the *declared* group. A group whose remedy is a button
carries it: *Choose resolution-curve folder…*, *Add data…* (when files must be added again) or *Fast-pass all ready*.

A **"Counts may not be raw"** group lists analyzers whose counts do not recover to whole numbers; they are
held out of fitting. *Accept counts as they are* (on the group, or *Accept counts as they are…* in the inspector) declares the counts
raw: it lifts the hold and changes no count and no flag; every fit made that way records it. It is offered
with an optional reason, and it lifts the hold for an `_i` analyzer only (an `_lr` analyzer stays held
whatever is saved). The analyzer then appears in *Your accepted counts*, where *Take back acceptance*
takes it back. The other ways out are to mark the analyzer dead or to correct the data and import again.

A save is refused, and nothing is written, while an export is running ("an export is running") and when
another ISAR process is saving to the same experiment ("the intake store is busy (another ISAR process is
saving); try again"). Press *Save* again once it is free.

### The scan list

Each top-level row is a scan set. Its label is the scan number when its files carry exactly one, and
otherwise the set's name (for example `CsV3Sb5_97K_0p5_0p5_3_sum`). Expand it to see its analyzers. The
columns *Scan*, *T*, *Q* and *Parsed* always show; *Columns ▾* adds *Fast pass*, *Deep pass*, *Slit* and
*File name*. *Fast pass* and *Deep pass* switch on by themselves the first time a result of that pass exists in
the experiment; if you switch one off (or on) from *Columns ▾*, ISAR remembers your choice and no longer
overrides it. A set row's summary counts spectra, or files received for *Parsed*.

Every status is a coloured dot with a word. The list and the inspector show a plain word; hover it to see the
fitting code's own word and one line on what it means (the filter box matches either word). The export keeps the fitting code's words (`count_state` and the rest).

- **Parsed** is `ok`, a gap (`no T`, `no Q`), or `not read`. It says the file was read and nothing more:
  not that the spectrum is calibrated, matched to a resolution curve or ready to fit.
- **Fast pass** and **Deep pass** show the state each pass stored for that spectrum. Fast pass: *clear*
  (`resolved`), *close call* (`close_call`), `unresolved` or *search limit reached* (`search_limited`). Deep
  pass: *clear*, *close call*, *search limit reached* or `refused`. A spectrum with nothing stored shows `not
  run`, or *held: needs resolution curves* (or an energy zero, a slit setting, its counts accepted; the hold's
  reason is on hover), `not fitted`, or `not recorded` for a fit stored before the fast pass existed.
- **`not run` and `fitting`.** A spectrum that has been read but not fitted shows `not run` in these
  columns, while `report/results.csv` and the export's `fits.json` say `fitting` for the same entry.
  Both mean there is no stored fit.

**Removing a scan from the list.** Right-click the selected scans and choose *Remove from list*. The scans are
hidden from this list, from the Scans page and from the selection, and the list's footer says how many are
removed. Their data, fits, saved groups and the analysis queue are unchanged. *Show removed* (in the list's top row)
lists them again, greyed and named "(removed)"; select them and choose *Restore to list*. The choice is saved with
the experiment.

**Reading a source file again.** Right-click the selected scans and choose *Read source file again* to send each
analyzer's own data file back through the same route a drop takes. If a file is missing, or ISAR has no record of
one, the status bar and the Log say so and tell you to drag the file into the window to add it again. The same
message appears if ISAR cannot read the file. A removed scan read again stays removed.

A dash (—) marks a missing temperature or Q; hover over it for the reason. Both passes are shown side by
side and neither is picked as the result.

**Out of date.** When an input a pass used has changed since the pass ran (a temperature or slit you saved, the
resolution folder, a dead analyzer, changed data), the *Fast pass* or *Deep pass* word keeps its stored text
but is drawn in the warning colour, and hovering names each changed input with its old and new value, for
example "temperature: 97 K (file) → 100 K (yours)". The two passes are judged on their own; a pass the engine
has no finished record of (fitted before ISAR recorded its inputs, or still running) is not judged and never
marked. A set row carries the mark when any of its passes does. The mark says the stored fit is out of date,
not wrong: nothing refits until the analyzer is added to the queue again (*Fast pass*, *Deep pass* or *Fast-pass all ready*), and the mark leaves when the input is the old value
again.

The filter box narrows the list: comma-separated terms, all of which must match, such as an analyzer
(`a06`), a temperature (`97K`), a file name or a status word. The list says how many of how many spectra
are visible. *Sort ▾* orders the scan sets by name (the default) or by temperature, or flips the list to
analyzer first, with every scan set beneath each analyzer.

The scan list and the Scan(s) page, inspector, saved groups and Processing queue share one selection.
Click a spectrum row to select it; Ctrl-click adds or removes it, and Shift-click selects the visible range
from the anchor. Click the row gutter to expand or collapse a scan set without changing selection; click a
set row to select its analyzers. The rows show partial-selection counts. Empty space clears the selection.
A background update keeps membership and scroll position. Filtering can hide a selected spectrum but does
not remove it; the list says how many selected spectra are outside the filter. Selection alone never queues
work. Double-click an analyzer, or press *Inspect*, to open the scan inspector.

### The Scan(s) display

On Scan(s), ISAR draws at most ten selected spectra; with more than ten it shows a read-only list
grouped by scan set and reads no spectra. Change the set in the scan list or recall a saved group. Up to
ten spectra are drawn in selection/list order. With the *Auto* switch on (the default), list selection and group
recall switch to Scan(s). Manually choosing another centre page turns *Auto* off, so later selection
changes leave that page open.

*Fit* is *None* by default. Choose *Fast* or *Deep* to show that stored pass; no fit runs from this page.
Fast total/component curves are solid, Deep curves dashed, and background curves dotted. The active
spectrum is marked in the legend. Outdated input values remain visible beside the trace. The Curves header marks displayed Deep curves
that include an untrusted result, and the Fit tooltip gives its reason. When deep currentness cannot be
judged, the trace says *currentness not recorded*.

*Layout* is *Offset* or *Overlay*. Offset stacks the spectra using an automatic vertical step or the number
you enter, in the current intensity unit. Overlay compares measured data with distinct colours and symbols,
without fit curves; the Fit control is disabled. Selecting one spectrum always uses the single-spectrum
view. If a stored fit curve is available, that view also shows a residual panel: measured minus the chosen
total fit on the measured energy grid, with data-error bars. The residual is in the displayed intensity unit,
not divided by the uncertainty.

*Scale* is *Raw* or *Normalised*. Raw shows counts N and Poisson error √N, including zero counts as
0 ± 0. Normalised shows N R/M, where M is the point's monitor and R is one reference monitor saved for
the experiment. R is the nearest power of ten to the median of valid positive monitors across its spectra;
selection changes do not recalculate it. The propagated uncertainty is
(R/M) √(V + N²/M), where V is the stored count variance; with V=N and N>0 this is
y √(1/N + 1/M). At N=0, the display keeps 0 ± 0 and adds no floor. Normalised display omits points
without positive monitors. If a spectrum has no usable monitor it is counted as not drawn with the reason;
Raw can still show its counts if its energy axis is calibrated.

No zero or curve is guessed. On BL43LXU a spectrum with no fitted zero yet is drawn at once on the file's own
calibrated energy axis, labelled *beamline zero*, and moves to its fitted zero when its own fast pass is saved.
Any other instrument's raw axis is drawn labelled *Energy, not zeroed*; it never shares a plot with zeroed
spectra, so in a mixed selection it waits for the fast pass to set the energy zero and is named in the
not-drawn summary. No fit curve is drawn until its pass exists. Pooled masters are named as not drawn, not replaced by their member spectra. Missing data or monitor exposure is named in the not-drawn summary; an unavailable pass or curve is named on its spectrum trace. An older result whose exposure was not
stored needs its source files read again before it can be shown in Normalised scale; Raw can show the
existing counts. If the source monitor itself is absent, read the data again with it present.

A new Deep pass stores the actual chosen model manifest beside the chosen parameters, and Scan(s) evaluates
that model for its curve. It does not use a guessed Fast-pass model to draw a Deep result. For older or
ambiguous Deep records with no recoverable structure, the curve is named unavailable while the stored
state and mode table remain. An outdated curve is still shown with its warning; Normalised scale places it
in the current unit's recorded exposure frame and the experiment's frozen R, without refitting.

The inspector stays beside the scan list. It follows the active spectrum and shows “k of n selected” with
previous/next controls; stepping changes the active spectrum without changing the selected set. Its Fit
section reports each stored pass's state, trust/currentness, model label and mode count, then the stored
energy E₀, linewidth Γ (DHO damping, HWHM) and amplitude h with their symmetric or asymmetric intervals.
Missing uncertainty stays visible as “± —” or “—”. Interval ladder, Model candidates, Verdict and Provenance
remain in their existing collapsed sections below. The inspector has no spectrum plot. Pooled masters still
show their recorded numbers and fit tables there.

The group chips save or recall up to nine selections per experiment. Click a numbered chip or press 1–9 when no text editor has focus to
recall it; Ctrl+1–Ctrl+9 saves or replaces that slot, while + saves to the lowest free slot. Chip Save/Undo, + and Ctrl+number can replace or add a
saved group after selection membership changes; ordinary selection does not rewrite
the saved group. Missing group members are named, and recall drops them with a notice. These groups are
view controls, not card collation settings.

The Processing queue remains on the right. It consumes the same selection, adds one item for each selected
eligible analyzer when you press *Fast pass* or *Deep pass*, and excludes pooled masters. Merely selecting
spectra adds nothing. The Scan(s) page does not change queue order, writer ownership or saved-result rules.

### The scan inspector

The inspector opens beside the scan list, inside the left panel, and ✕ closes it. For the selected
analyzer it shows the file; the scan numbers (a summed set says so, and lists its scans when the files
do); the analyzer, temperature and Q, each with where it came from; the slit, or "not in the file"; the
number of points and the energy range; the parse status; the scan's QC flags; the target Q, the campaign
command and the counting mode, each only when the scan has one; each pass's state; a "Scan metadata"
section with the rest of the unit's recorded values (status, calibration, the file's facts, and so on),
one row per value the unit has; and a Fit section with the stored pass summaries and mode rows. The inspector has no plot; spectrum plotting is on Scan(s).
On a laptop, opening the inspector collapses *Processing* to its rail so the plot keeps its width; expand
*Processing* again from the rail and it stays open while the inspector is open. Closing the inspector
does not expand it.

For BL43LXU spectra the inspector has a **Facts** table: one block each for temperature, slit, Q, the
counts' origin and the analyzer, with three cells per fact.

- **From the files**: the value with its source (such as `header` or `filename`), or "— missing".
- **Yours**: the value you saved, with its reason, or empty.
- **Used**: the value the fits use and whose it is (`file` or `yours`), or the named fallback when there is none.
  A value of yours that differs from the file's is drawn in the warning colour, and hovering gives both numbers.

Temperature and slit apply to the whole scan set (the table says which). Each has *Enter my value…* (a value in K
or mm and an optional reason; a later one supersedes the earlier) and *Use the file's value* (withdraws yours;
available only when you have one). The analyzer block has *Leave out analyzer…* / *Use analyzer again…*, and *Accept counts as they are…* /
*Take back acceptance…* when its counts may not be raw. Q and the counts' origin are shown only. The counts' origin
reads like "N = (y/e)², 60 bins tested, none off an integer": how the counts were recovered and how many bins
were off a whole number. When the files carry no hkl, Q shows the file's |Q| in nm⁻¹ and says campaign planning needs the
hkl. Under the table, **What can be done with this analyzer** gives each step's state (ready, fallback, pending, held) with the engine's
reason. Everything here is the engine's read; the window computes none of it.

### The analysis queue and the Campaign tab

The *Processing* tab on the right is the analysis queue. It fits analyzer files (one spectrum each), one file at
a time per file and several files at once, on the number of cores you give ISAR: *Config ▸ Cores for analysis…*
in the menu bar, which defaults to this machine's cores less two. A lower number takes effect as running analyzers
finish. Once work is queued, the only thing you do is add more or stop something.

**Adding work.** Select one or more analyzer files in the scan list, then press *Fast pass* or
*Deep pass* under *Add to queue:* in *Processing*. Selecting alone queues nothing; pooled masters are named and excluded. *Fast-pass all ready (N)* is the
button at the top of the left column; it is also in *Processing*, in the *Run* menu, and in the needs-attention and
readiness lists. It adds a fast pass for every ready analyzer file that has no current one, and its label says how
many. With *Fast-pass new scans as they arrive* on (the default for a new experiment), every analyzer file read in
from then on joins the fast queue by itself. A deep pass is only ever added by you. An analyzer whose result is
already current, or already queued or running for that pass, is not added; the Log says which and why ("Not added:
… already have a current fast pass"). Adding from the panel or that button first asks about an unsaved card draft.

**What the panel shows.** Each queue has a heading with its counts. Under it, each scan set, then each analyzer with
a dot and a word: *waiting*, *running*, *saving*, *done*, *done with a warning*, *held*, *failed* or *stopped*. A
running analyzer shows the stage or step it is on and how long that step has run. The fit reports only the steps it
has completed, so there is no percentage. A step that runs far longer than the earlier ones may mean a stuck fit;
*■ Stop* ends it. *Finished* lists what has finished since you opened the experiment: one line per scan set and
pass with its counts, then each analyzer that needs a look (held, failed, or done with a warning, each with its
reason), then each finished deep pass with *Show*, which replaces the shared selection with that analyzer. With
*Auto* on, it switches to Scan(s); with *Auto* off, the centre page stays put. *Clear list* tidies
that list only. The strip beside *Processing*
says what is running and what needs a look when the panel is collapsed.

- **Held** means ISAR will not fit the file without something it needs: a missing energy zero, a missing resolution
  curve for its slit, or counts not accepted as raw. Held files are not retried by themselves. Add the file again
  once the missing value exists. For a missing energy zero, the reason says that the energy-zero series is built by
  `isar run`, which the queue does not do.
- **Done with a warning** means the result is saved, but the engine reports a limit on it, for example "the search
  reached the peak-count ceiling" or "comparison incomplete, 1 model unresolved after restarts". The warning stays
  with the stored result.
- **Provisional deep results.** While the deep pass's sampler calibration is provisional, one line under the *Deep
  queue* heading says so, and the scan list marks deep counts *(provisional)*. See *The deep pass* below.

**Stop, pause, remove, leave.** *✕ Remove* takes a waiting analyzer out and asks nothing. *■ Stop* ends one running
analyzer and the others carry on. Stopping a fast pass asks nothing; stopping a deep pass asks first and says what
the stop discards (its fast-pass result stays once it is saved). *Pause* lets running analyzers finish and starts
nothing new until you press *Resume*; new arrivals still join the queue and wait. Closing ISAR, *Open…*, *Recent* or
*New experiment…* asks first when anything is running, saving or waiting, and says what would be lost: running
analyzers stop and their work is discarded, finished results are saved first, and waiting ones are forgotten.
Nothing from one experiment lands in another. To rename the experiment, press *Pause* and wait for the running
analyzers to finish; then *Rename* works and keeps the waiting ones.

**Saving.** A result counts as done only once it is saved in the experiment folder; until then it reads *saving*.
*Save copy as…* and *Export…* stay available while analysis runs. Each waits for a result being saved to finish, holds
the saving until it has read the folder, and then goes on. A copy of a large experiment took seconds on the
development machine; a laptop has not been measured. *Run ▸ Cancel active operation* stops a file read, an export or
a campaign. It does not stop analyzers; use *■ Stop* for those.

**Campaigns.** A campaign does not start while analyzers are running, saving or waiting in the experiment; *Start*
says why. The queue starts nothing while a campaign runs. Otherwise the *Campaign* tab is the launcher and monitor it
has always been. `isar run` and `isar drive` run by hand are not coordinated with the window's queue; do not run
them on an experiment while the window is analysing it (`WORKSPACE_AND_CLI.md`).

The full set of states, what each control affects and the open items are in `design/gui.md` §0sexies.

### Log and Output

The Log, bottom left, holds what the window reports. The Output, bottom right, names the experiment folder and when it last changed. It has two actions:

- *Save experiment copy…* copies the whole experiment folder to a new one. The original stays the active
  experiment and new data keeps going to it. The copy is offered as *Open copy*, and the Log says what the
  copy still reads from outside its folder (its source data files, its resolution curves).
- *Export results…* runs `isar export` on the experiment and names the new folder when it is done, with
  *Open folder*. See *Exporting results* below.

Both actions are unavailable, with the reason beside them, while an ingest, a campaign or an export is
writing to the experiment, and while nothing has been read into it yet. A running analysis does not make
them unavailable: they wait for a result being saved and hold the queue's saves until they have read the folder.

A save dialog that is offered a plain file name (*File ▸ Save view as image*, the campaign log's save) opens in
the experiment's folder, not in the folder the last dialog was used in; with no experiment open it opens where
the last one did.

### Exporting results

`isar export` writes one new folder, `report/exports/export-<timestamp>/`, from the stored results. It
refits nothing, reparses nothing and saves nothing, and an earlier export is never overwritten. The
folder holds:

- `modes.csv`: one row per fitted mode per pass. The `pass` column says `fast` or `deep`. `trust` is the
  deep record's own flag on deep rows; fast rows say `not assessed`, because the fast pass stores no trust
  flag. `count_state` is that row's own pass's state. A deep row leaves empty the cells its record does
  not store (amplitudes, heights, peak positions and the per-peak errors). An empty cell means missing,
  never zero. For a BL43LXU experiment `inputs_current` is `true` or `false` for the row's own pass, and empty
  where that pass was not judged.
- `fits.json`: one record per spectrum, fitted or not, with both stored pass records in full. A spectrum
  with no mode row (zero modes, held, never fitted) is still listed with its state. A deep pass that
  contributes no rows says why in its `deep` block. For a BL43LXU experiment each record also has an
  `inputs` block with one entry per pass, `fast` and `deep` (null where the spectrum has no such pass):
  `current` (true when every input the pass used still has its value, false when one changed, null when it
  could not be judged), `used` (the inputs the pass used, as recorded) and `changed` (each input that
  differs now: `input`, `from`, `to` and, for temperature and slit, `to_source` of `user`, `file` or
  `missing`). The top-level `currentness.assessed` is true only for a BL43LXU experiment that holds
  fit-input records, and false for any other instrument.
- `spectra/`: one CSV per spectrum with the data ISAR holds now. Its `#` header lines give the exposure
  the stored fit used, or `exposure: not stored` where ISAR stored none. The columns are the energy,
  counts, sigma (the square root of the counts, zero at zero counts, no floor) and the QC mask.
- `resolution/`: one CSV per resolution curve a fit used.
- `README.txt`: units and conventions.

Nothing picks one pass over the other: Scan(s) draws no fit by default, and its Fit control lets you choose the stored Fast or Deep pass; the export carries
both passes with their labels. Currentness: for a BL43LXU experiment each pass is judged by the inputs it recorded when it ran (data,
temperature, slit, Q, exclusions, dead analyzers, counts accepted, the resolution folder and curve, and the
calibration), as the `inputs` blocks above say. A fit that is out of date keeps its stored result until the
next time its analyzer is added to the queue; the export labels it and refits nothing. `fit_data_mismatch` still flags a spectrum
whose data file was read again with changed content after the fit. Other instruments are not judged: their
`currentness.assessed` is false.

## Reading the peak count

Each scan × detector row of `report/results.csv` is one spectrum. Its fitted peak count is `n_modes`, and
the fast pass's verdict on that count is in the count columns:

| Column | Meaning |
|---|---|
| `count_state` | the fast pass's state (table below), or `held`, `fitting`, `not fitted`, `not recorded` |
| `count_state_reason` | why, in words: the count probabilities, the close-call alternatives, the counts with no evidence and why, the search limit |
| `count_probability` | the probability of the shown count (`n_modes`) |
| `count_probabilities` | every count's probability, as JSON, e.g. `{"3": 0.91, "4": 0.09}` |
| `probabilities_conditional` | `True` when a count has no evidence, so the probabilities cover only the counts that have it |
| `numerically_marginal` | `True` when the numerical error of the evidence could move the count, or cross the 0.80 close-call line |
| `search_limit_reason` | `ceiling` (the mode ceiling was reached). `safety_budget` appears only on records made before 2026-10-27, when a per-spectrum time budget could stop the search; the queue sets no time limit |
| `n_confident`, `n_tentative` | how many of the shown peaks are confident, and how many are tentative |
| `flags` | warnings kept apart from the state, with their energy interval, e.g. `ambiguous decomposition 5.00-7.00 meV` |
| `deep_pass_candidate` | `True` when the state is not `resolved`: the spectrum is worth a closer look (`--deep-pass` runs it; five more `deep_*` columns, below) |

`report/modes.csv` has one row per mode of the selected count, ordered by energy (`mode_index` 0 is the
lowest). Each row repeats its spectrum's `count_state` and carries the mode's own fields: `confident`
(`False` for a tentative peak), `delta` (resolution-limited: a delta function with its width fixed), `regime`
(`near_elastic`, `underdamped` or `overdamped`, recorded apart from the parameter statuses), `omega0_status`,
`gamma_status`, `height`, `area_stokes` with its error and `area_status`, `corr_omega0_gamma`,
`deletion_impact` and `manual_fit` (flagged for manual fitting). `Q_nm` is the analyzer's |Q| when the data
provides it. `omega0` is the undamped mode energy E0, the fitted parameter, and `peak_position` the displayed
one, the damped frequency √(E0² − Γ²), empty when the mode is overdamped (Γ ≥ E0). `uncertainty_kind` is
`hesse` when the mode has an error bar and empty when it has none. An empty cell means missing, never zero.

| `count_state` | What it means | What to do |
|---|---|---|
| `resolved` | the preferred count has probability 0.80 or more, and every contending count has evidence | use it |
| `close_call` | the preferred count is below 0.80; the alternatives named in the reason carry the rest | read the alternatives; the shown fit is the most probable count |
| `unresolved` | a contender's evidence could not be computed, so no winner is declared; the probabilities are conditional on the counts that have evidence | read it with care; the comparison is recorded as incomplete, and the spectrum is a deep-pass candidate |
| `search_limited` | the mode ceiling stopped the search | the count is the best found, and the spectrum may hold more modes |
| `held` | the scan is held, for example because its energy zero is missing; the reason names it | nothing until the hold is cleared; it is re-examined on every run |
| `fitting` | ingested, not yet fitted | nothing to do until the next run |
| `not fitted` | excluded, undersampled, or the fit failed | the reason says which |
| `not recorded` | a detector outcome with no fast-pass result (written before the fast pass existed) | a re-run refits it |

**Confident and tentative peaks.** Only confident peaks are reported as found. A tentative peak stays in the
fit record, counts toward the close call and the flags, and has `confident` set to `False` in `modes.csv`.

**Flags.** Two kinds, each with its energy interval. `structured residual`: the fit leaves a residual pattern a
clean spectrum would not show. `ambiguous decomposition`: a forced broader or shoulder alternative fits about as
well and moves intensity to a different energy. That says the assignment of intensity is ambiguous, not that a
hidden component exists. Until the benchmark calibrates its threshold the structured-residual flag is off.

**Pinned limit.** The mode ceiling is 6 (the card's `[fitting] max_modes`), a known limit set by the operator
rather than a measured one. A best count at the ceiling reads `search_limited`.

The search and the states are decided as `STATS_PRIMER.md` §2.2 describes.

## The deep pass: a closer look at spectra you choose

The fast pass settles most spectra. When a row reads `close_call`, `unresolved` or `search_limited` (its
`deep_pass_candidate` is `True`), or you simply want the count checked, send that spectrum to the deep pass: nested-sampling
evidence for every model of its fast-pass record, then profile intervals and bootstrap errors on the chosen
model. It never runs on its own: from the command line you ask for it with `--deep-pass`; in the window, *Deep pass* in the analysis queue adds it for a selected analyzer, and the window sets no time limit on it.

```
isar run <data file> --workspace DIR --detectors ana7 --deep-pass [--deep-budget-minutes MIN] [--deep-workers K]
```

- `--deep-pass` runs the fast pass on the spectra this run fits, then the deep pass on each. Pick the spectra
  by data file and `--detectors` (the analyzers); add `--force` to refit a spectrum that is already current.
  It cannot be combined with a certification request, a certification manifest or an analysis allowance.
- `--deep-budget-minutes` sets a start budget per spectrum, in minutes. The default is none: every model is
  compared, however long it takes. When you type one, once it is spent no new chunk of models starts (a chunk is
  as many models as there are workers), but a chunk already running runs to completion, restarts of a suspect run
  included. At five or six modes that can take hours: one 6-mode run took 53 min at the provisional
  settings, and a restart roughly doubles the cost (an estimate). A spectrum that runs out reports `search_limited`
  with the models it did not reach. The profile and bootstrap step after sampling is not counted in it, and `--M`
  sets its bootstrap draws.
- `--deep-workers K` (default the smaller of 6 and `--workers`) is the number of processes for the sampler runs
  and the bootstrap. A deep run fits its spectra one after another and spends the workers inside each spectrum.
  Pooled masters are not deep-passed.
- **Cost.** Hours of single-thread sampling per spectrum, measured on one le Tacon spectrum at the provisional
  settings (400 live points, step factor 4): one sampler run takes 9 s (no peaks), 5 min (1 peak), 11 min (2), 16 min
  (3), 29 min (4), 43 min (5) and 53 min (6 peaks), and the exploratory runs at 128 live points about a third of
  that. One run of each of that spectrum's seven core models is 2.6 CPU-hours (measured). The deep pass does not stop
  at the core set: it samples every model of the fast-pass record, and that spectrum's record holds 143. At the
  defaults (no budget, 6 workers) a spectrum like it runs for many hours until every model is sampled (an estimate
  from these timings, not a measured run); a typed budget of 60 minutes ends it earlier, as `search_limited`. A suspect run's restarts (twice the
  walk, then twice the live points) add cost on top. Every run (seed, settings, wall time, likelihood calls, best
  log-likelihood) is recorded.
- **Carried, not rerun.** A spectrum that already holds a deep pass under the same settings, budget, seed and
  `--M` is skipped. Changing the budget reruns it, and a refused deep pass is retried.

**What you get.** Five more columns on the spectrum's `report/results.csv` row, empty when no deep pass ran:

| Column | Meaning |
|---|---|
| `deep_state` | `resolved` (the preferred count has 0.80 or more of the probability, every model sampled), `close_call` (below 0.80), `search_limited` (the budget ended, or a model stayed suspect after restarts; the probabilities cover only the models sampled) or `refused` (the deep pass could not run; the reason says why) |
| `deep_state_reason` | why, in words, with the preferred count and its probability |
| `deep_best_count` | the preferred count by nested-sampling evidence |
| `deep_p_best` | its probability |
| `deep_numerically_marginal` | `True` when the sampling error could move the preferred count or cross the 0.80 line |

The full record (every model's evidence, every run with its seed, settings, information H, likelihood calls and
best log-likelihood, and the errors on the chosen model) is the spectrum's `deep_pass` entry in `session.json`;
per-stage and per-run wall times are in `report/run_timings.json`. The fast-pass columns, the shown fit and
`modes.csv` are not changed by the deep pass; read the two side by side. A model's `status` of `reference` in the
record means sampled at the installed accepted settings (400 live points, step factor 4), not at the calibration's
1600-live reference level.

**Untrusted for now.** The sampler settings are provisional: one calibration pass on a single spectrum missed its
accuracy aims, and at the installed settings three seeds span 1.07 nats for the count-4 model but 17.6 and 10.5 nats for the
count-5 and count-6 models, so every
count decision's `deep_state_reason` ends "provisional sampler settings: untrusted until calibration". Treat the deep pass's
preferred count as a second opinion, not a verdict, until that note is gone. How the pass works is
`STATS_PRIMER.md` §2.4.

## What ISAR does and doesn't do here (honest limits)

- The fitted **ω₀ is the product**; error bars are statistical-only (see
  [STATS_PRIMER.md](STATS_PRIMER.md)). On triple-axis instruments **Γ is provisional**
  (`gamma_provisional` in `results.csv`) until a 4-D resolution treatment exists — trust ω₀, not Γ.
- The LLM (when enabled with `--llm`) only *adjudicates QC flags* and writes explanations. It never
  sits in the path of a fitted number; with LLMM down, scans are stamped "no LLM consult" and the
  run proceeds.
- ISAR is a **triage** pass, not your final analysis — verdicts tell you where to spend manual
  effort, and nothing is ever deleted on your behalf.

## Parsing SPring-8 BL43LXU (IXS) folders

BL43LXU is supported for **parsing** (`isar init`, `isar ingest` and the inspector read its processed
per-analyzer files) and for **fitting** with the beamline's measured resolution curves (see *Resolution,
slit and zero* below). Two processed layouts are read. The raw SPEC files the beamline writes (`specixs_*`, `spects_*`) are
not: `isar init` on one stops with "has no shipped card template yet".

Point ISAR at the folder, as for any instrument:

```
isar init "D:\beamtime\leTacon_SP8_CsV3Sb5" --out my_bl43
isar ingest "D:\beamtime\leTacon_SP8_CsV3Sb5" --workspace my_bl43
```

`isar init` detects the instrument from the first per-analyzer file and writes
`my_bl43\card.policy.toml` with the analysis settings from the shipped format file,
`isar/parsers/cards/bl43lxu.policy.toml`. The format is read from that file at every load, so a fix there
reaches every workspace at its next load. `isar ingest` makes one scan per set of files,
with one spectrum per analyzer, and ledgers each analyzer as its own `(scan, analyzer)` entry. `isar run`
then sets each scan's slit and each analyzer's energy zero. An analyzer that cannot be fitted is held on
its own, with the reason named; the other analyzers of the scan still fit.

### The two layouts

- **CsV3Sb5 layout** (the beamline's processed output): one file per analyzer, named
  `<sample>_<T>K_<h>_<k>_<l>[_<tag>]_aNN` (NN is the analyzer number: 28 analyzers, a03 to a41), with a
  `<stem>.var` sidecar beside them. The HKL in the stem is the scan's **nominal arm-centre HKL**: it
  becomes the scan fact `hkl_nominal`, never an analyzer's Q. The bare `<stem>` summed file and the
  `<stem>.pkpos` peak lists are ignored, not parsed.
- **Ba122 layout** (the group's export macro): one three-column file per analyzer,
  `spec1_T<T>K__scan_<n>_A<a>_Q_<h>_<k>_<l>_inel.dat`. The scan, temperature, analyzer and that
  analyzer's own HKL are all in the name. It carries **no monitor**, so its exposure is recorded as
  missing; ISAR does not deduce a monitor from the intensity/count ratio.

Counts are raw in both layouts: N = (intensity/error)² is recovered from the printed numbers, the
variance is N, and a zero count stays zero with σ = 0 (no floor). The monitor and counting time of each
analyzer are stored beside its counts, never divided in. A bin whose intensity is negative (it cannot be
a count), or whose error is 0 with a non-zero intensity, is masked and flagged `invalid_point`, never
dropped. Analyzers with different bins share one energy
axis; a bin that an analyzer lacks is masked and flagged `bin_absent`.

### Which scan is which

A scan's identity is **(experiment, scan number, analyzer)**. The experiment is the SPEC file name when a
header line `specfile` or the file name carries it (the Ba122 `spec1_...` files give `spec1`, so `spec1`
and `spec2`, which both count scans from 1, stay apart), otherwise the `[sample]` name or `experiment_id`
you give in the card, otherwise the file stem (the CsV3Sb5 layout). The scan number is the one the set
lists; a set that lists several, or none, gets a large hashed number and keeps the list in the
`scan_numbers` fact, with the stem in `scan_label`. Two sets that land on the same identity are never
merged: the second is not ingested and the ingest report names both.

ISAR does its own combining, so it does not want scans that were already summed. A set whose file name
lists several scans (`..._scan_58_59_...`) is **passed over**, and named under `passed_over` in the ingest
report, when every scan it lists is in the same batch as its own set of the same SPEC file, or already in
the workspace as one. When they are not (or the set lists no scans, as in the CsV3Sb5 layout), it is read
and flagged `pre_combined_input`, and so is any set whose `.var` reports more than one merged scan. If such
a set was read first and its individual scans arrive later (in any folder), the next `isar ingest` or
`isar run` marks its ledger entries `excluded`, with a reason naming the set and the scans; they are not
fitted again and stay listed under "Held / declined". The files are never touched.

### Where BL43's settings live

Three homes, and each setting is in exactly one of them.

**1. The shipped format file** (`isar/parsers/cards/bl43lxu.policy.toml`), read at every load and never
copied into the workspace, so a fix to it reaches every workspace at its next load. The agent edits it when
the format changes. Its tables:

- `[files]`: `measurement` holds regexes with named groups. Each must name `set` (the scan's key,
  shared by its analyzer files) and `analyzer`; the other groups (sample, `T_K`, `hkl_nominal`,
  `specfile`, `scan_numbers`, `hkl`) become filename facts. `sidecar` is `{set}.var`. `ignore` lists
  globs that are never read as data. Subfolders are walked, and a set is the files of one folder: the
  same stem in two folders is two sets (the second is refused by name if it lands on the first's
  identity, see below). The `rf/` folder of SARF resolution files is never read as data (the template's
  `ignore` lists `sarf_*`); `[calibration]` reads it as the resolution library.
- `[reader]`: `intensity_family` is `"_i"` (the default: integer counts, raw) or `"_lr"`. The `_lr`
  counts split some counts between neighbouring bins, so they are **not raw**. They are kept unrounded and
  every `_lr` spectrum is flagged `counts_not_integer` (a warning, whatever its bins recover to). For `_i`,
  a few bins that do not recover to an integer (print rounding) raise the same flag as a notice; when more
  than 10% of the valid bins do, it is a warning, because the columns are then probably not counts. The
  calibration holds a spectrum carrying the warning, and it is not fitted: the counts may not be raw.
  For an `_i` spectrum you can lift the hold by saving *counts accepted* for that scan and analyzer
  (with a reason if you like); the counts and the flag are not changed, and every fit made that way
  records it. An `_lr` spectrum stays held whatever is saved. A notice is print rounding of raw counts,
  holds nothing, and that spectrum is fitted. Where each analyzer's counts came from, and the result of
  the integer check, is recorded with every fit.
- `[calibration]`: `method = "tabulated_elastic"` (see *Resolution, slit and zero*), and
  `resolution_pattern`, which matches the SARF file names and picks out the analyzer and the slit. No
  folder is named here: the resolution folder is the experiment's saved one.
- `[[detectors]]`: `value_index` and `signal_column` are not used for this instrument.

**2. The workspace card** (`card.policy.toml`): the analysis settings only, meaning collation, fitting
guidelines, priors, `[sample]` and the QC thresholds. It is edited in the GUI's card panel. A format key
in it (for example `[files]`, `[reader]` or `[facts]`) is ignored with a warning, and the shipped value is used.

**3. Values, conditions and the resolution folder**: entered in ISAR (the GUI), never in a card. A
temperature or slit for one scan, an excluded scan, a dead analyzer and counts accepted are saved there, and
the resolution folder is the one ISAR saved for the experiment. A dead analyzer is a condition, not an
`enabled = false` line: it is still read and imported, and it is left out of the fits of its scan. See
*Resolution, slit and zero* and WORKSPACE_AND_CLI's *Values, conditions and what each fit used*.

### Header lines the group's macro can write

A `# key value` line before the data (the column-label line is skipped) gives the same fact that the file
name would, and it wins over it. The reader takes:

- scan level: `specfile` (the SPEC file name), `scan` (scan number or numbers), `T_K`, `si_order`, `energy_keV`, `agaph_mm` and `agapv_mm`
  (the analyzer slit, combined into the generic fact `slit` as `<h>x<v>`), `lattice` (a, b, c, α, β, γ)
  and `time`;
- analyzer level: `hkl` (the analyzer's own HKL, which gives its Q: rung 1), `dq`, and `Q_nm` (|Q| in
  1/nm: a magnitude only, never a Q).

The analyzer itself is named by its file name, not by a header line. The CsV3Sb5 analyzer files already
end with `#Q=` (|Q|) and `#T=` (temperature) lines; they are read as `Q_nm` and `T_K` where no leading
header line gives them. If a trailing value disagrees with the file's own leading header, the header
value is kept and the trailing value is listed as a conflict under source `trailer`, with a
`fact_conflict` flag.

A comment line shaped like a fact, `#Key= value`, whose key ISAR does not read here raises an
`unrecognised_line` flag, and the import record lists it with its analyzer and line: a renamed `#T=`
(say `#Temp=`) shows up this way instead of silently leaving the temperature unread. Its value is not
used. This covers a trailing `#Key=` line other than `#Q=`/`#T=`, and any `#Key=` line before the data;
a bare `#T=` or `#Q=` with no value is reported as an unparsed line (`trailer_unparsed`). Plain comments
and the column-label line are never flagged, nor is the leading `# key value` form (it cannot be told from
a plain comment), nor are the `.var` sidecar's keys (ISAR reads only a few of its many).

The `.var` sidecar supplies `si_order`, `bin_meV` (the bin size) and `n_merged_scans`, and for each
analyzer `efficiency`, `polarisation_factor`, `elastic_centroid_meV` and `elastic_area`. A `.var` that
is missing or unreadable raises `missing_sidecar`; one that is empty or has no analyzer list (for
example still being copied) counts as unreadable, and importing the scan's folder or files again once it
is whole completes the scan. A re-import reads every file again (a file is never skipped as unchanged), so
it also picks up a corrected reader. Moving or renaming the data folder and importing it from its new place
marks no scan changed, because the data revision ignores where the files sit; the import record keeps the
new paths. A resolution folder saved inside the moved folder (the usual `rf/` beside the data) is not
followed: ISAR keeps the old path, the next import reports its curves as missing, and any fit that used them
shows Values changed since last fit. Save the new folder in the GUI (ISAR never switches a
saved folder by itself), and the mark clears once the affected scans are fitted again.

### Reading a fact's source label

The inspector shows every fact with its source:

| label | meaning |
|---|---|
| `header` | a `# key value` line, or a trailing `#Q=` / `#T=` line, in the file itself |
| `trailer` | a trailing line that disagrees with the leading header (kept as a conflict) |
| `sidecar` | the `.var` file beside the analyzer files |
| `filename` | the file-name pattern (the `[files]` groups of the shipped format file) |
| `inferred` | derived by ISAR: `energy_keV` from `si_order` (1.977 keV per silicon order) when nothing else gives it, the count-recovery deviation, and `points_per_bin` (read from the `npts` data column) |
| `missing` | nothing supplied it. The value is empty, and nothing downstream may invent one |

The precedence is header, then trailer, sidecar, filename; the first found wins. A value you enter in ISAR
is kept beside the file's value, not in this order: it is what the fit uses (see WORKSPACE_AND_CLI's
*Values, conditions and what each fit used*, and GLOSSARY's *file value / user value*).

A spectrum's `q_source` says which rung gave its Q. `header` or `filename` is rung 1 (the analyzer's own
HKL). `none` is rung 4: no Q, `q = None`. Rungs 2 and 3 are planned, not built. For CsV3Sb5 every analyzer
is rung 4, and the scan's `hkl_nominal` is the only HKL in the record.

### Resolution, slit and zero

The per-analyzer files carry two energy columns. `meV` is the beamline's calibrated energy transfer: each analyzer's zero comes from its resolution-mode offset (`elpk_mk` in the `.var`). `meV_sft` is `meV` minus the half-maximum midpoint of the biggest peak in the summed spectrum; the same number is `apk_pk_meV` in the `.var` and the `.pkpos` peak list. It is not the elastic line and must never be taken as a zero. The `.var` `elpk_centroid_meV` (fact `elastic_centroid_meV`) is the beamline's elastic centroid on `meV`; ISAR uses it only as a cross-check of its own zero.

A scan's slit sets which resolution curve each analyzer uses, and each analyzer's energy zero is fitted
on its elastic line with that curve. To set this up:

1. Put the `rf/` folder of SARF files inside the imported data folder (or beside an imported file). ISAR
   finds it at the first import or run when no folder is saved for the experiment, saves it as the experiment's
   resolution folder and says where it found it. The folder is an experiment setting that ISAR saves and
   changes only through the GUI, never by editing a file. ISAR never switches a saved folder by itself: an
   `rf/` it finds beside a later import is listed in `report/curves.json` as another folder that also holds
   curves.
2. Declare the slit if you know it: the header lines `agaph_mm` and `agapv_mm`, or enter the slit in ISAR
   (the GUI). A slit you give is always used.
3. ISAR checks the folder's curves at every import and every `isar run`, by their content: a curve replaced
   at the same path is noticed, and the calibration is rebuilt at the next run.
   `isar run` builds the calibration. The inspector shows each scan's slit, its candidate deviances and
   the alerts; the numbers are kept under `tabulated_calibration` in `card.state.json`, and the policy is
   never written.

With no slit declared, ISAR picks the slit whose curves best fit the scan's elastic lines and says so.
Alerts on that choice and on the zeros:

| Alert | Meaning | What to do |
|---|---|---|
| `slit_inferred` | No slit was declared; ISAR chose one from the elastic lines. | Enter the slit in ISAR if you know it. |
| `slit_ambiguous` | The runner-up slit fits within 10 in summed deviance, so the data do not pick one. | Check the slit and enter it in ISAR. |
| `slit_mismatch` | The slit you gave is kept, but another fits the elastic lines clearly better. | Check the slit you gave and the curve files. |
| `pipeline_zero` | The analyzer has no significant elastic line. Its zero is the file's own axis, labelled. | Compare that analyzer's ω₀ with its neighbours before trusting it. |
| `zero_disagrees_with_beamline` | The analyzer's zero (`elastic_fit` or `pipeline_zero`) is more than 0.3 meV from the beamline's elastic centroid; the reason gives both numbers. The centroid includes background and phonon tails, so the flag is a cross-check and ISAR's fit stays the zero. It is raised when a scan is calibrated, so an existing workspace shows it only once its data are run into a fresh one. | Inspect that analyzer's elastic fit. The flag does not change the zero. |
| `missing_resolution` | No curve for that analyzer at that slit, or no folder: the analyzer is held and not fitted. | Add the curve to the experiment's resolution folder (the one ISAR saved), or correct `resolution_pattern` in the shipped format file. |
| `resolution_range_exceeded` | The fit range runs past the measured curve; the curve is used as given. | Read the fit with that in mind. |

Held analyzers and scans are listed under "Held / declined" in the report, with their reason.

To check the fast pass on the le Tacon CsV3Sb5 example set (batch times, the analyzer at the M point beside
the published phonons, and how far the prior moves the counts), run `run-isar-proofs.bat letacon` from the
main checkout. It takes one to two hours and writes its workspaces, logs and `summary.json` under
`out\letacon_fast_pass\<timestamp>\`.

### Limits for BL43LXU

- An analyzer's zero is its fitted elastic line, or the labelled pipeline zero. ISAR never defaults an
  energy zero to 0.0 on an uncalibrated axis.
- The in-plane momentum spread of the resolution is not modelled. Q rungs 2 and 3 are not built, so an
  analyzer's Q stays absent unless its file gives an HKL.
- `_lr` counts are parsed and shown, never fitted (see `[reader]` under *Where BL43's settings live*).
- The raw SPEC files are not read.
- The Ba122 layout has no monitor, and ISAR does not deduce one.

## New instrument, no parser?

`isar init` recognizes the instruments with registered parsers (currently Sector-30 HERIX SPEC —
supplementary and raw —, ILL IN8 numor ASCII and SPring-8 BL43LXU processed per-analyzer files, above;
the shared SPEC/numor readers are built for ID28 to slot in as cards + thin plugins). If your format isn't recognized, a parser plugin
plus a card template is what's needed — see `ARCHITECTURE.md` §6 and `isar/parsers/base.py`; the schema
(`ScanRecord`) is fixed, only the reader varies.

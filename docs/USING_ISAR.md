# Using ISAR — the command-line workflow

> **Status:** live
> **Authority:** canonical for the operator workflow
> **Verified against code revision:** `d483af1` (2026-08-08)

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
| `card.policy.toml` | **You** (ISAR scaffolds it once, then never touches it) | Every per-experiment decision: detectors, column names, collation rules, calibration method, fitting guidelines, and declared branch hypotheses for autonomous campaigns ([HYPOTHESES.md](HYPOTHESES.md)). Commented — the comments are the documentation. |
| `card.state.json` | ISAR | Accumulated experiment state: energy-zero series, resolution versions, the running dispersion prior, resolved hypothesis snapshot and mismatch evidence, sample map, exclusions, corrections audit trail. Don't hand-edit; don't delete. |
| `session.json` | ISAR | Bookkeeping: which files were seen, which scans were processed and how. Safe to delete — a re-run rebuilds it (refitting everything once). |
| `report/` | ISAR | The product: `report.html`/`.md`, `results.csv` (one summary row per spectrum, with its peak-count state), `modes.csv` (every fitted mode of the selected count, ordered by energy), figures, plus `reduction.*` (the QC-level detail report). |

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
    "passed_over": [{"set": "...", "scans": [58, 59], "reason": "..."}],  // summed sets whose scans are present
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

## Reading the peak count

Each scan × detector row of `report/results.csv` is one spectrum. Its fitted peak count is `n_modes`, and
its state is in the count columns:

| Column | Meaning |
|---|---|
| `count_state` | the spectrum's one state (table below), or `not fitted` / `not recorded` (below the table) |
| `count_state_reason` | why, in words: the trigger, the limit that stopped the search, the calculation that failed, or the hold reason |
| `deciding_rung` | `1` (Laplace evidence) or `2` (nested sampling): the rung whose result is shown |
| `count_probability` | the probability of the selected count |
| `runner_up_count`, `runner_up_probability` | the next most probable count and its probability |
| `count_ambiguous` | `True` when the runner-up is close (probability at least 0.20): read both counts |
| `count_flags` | quality warnings, kept apart from the state: `structured residual`, `central residual, quasi-elastic not tested` |

`report/modes.csv` has one row per mode of the selected count, ordered by energy (`mode_index` 0 is the
lowest). Each row repeats its spectrum's `count_state`, so a provisional mode is never mistaken for a
settled one. `omega0` is the intrinsic mode energy and `peak_position` the displayed one (the damped
frequency). `uncertainty_kind` is `hesse` when the mode has an error bar and empty when it has none. An
empty cell means missing, never zero.

| State | What it means | What to do |
|---|---|---|
| `settled` | the evidence picks a count, or the ambiguity is stated | use it; check `count_ambiguous` |
| `provisional` | rung 2 is still sampling: `count_state_reason` reads "rung 2 pending: k of n contending models sampled" | the shown count is rung 1's; it becomes `settled` when rung 2 completes |
| `search limited` | the mode ceiling or the 30-minute per-spectrum budget stopped the search | the count is the best found; the reason names the limit, and the spectrum may hold more modes |
| `unresolved` | a contender's evidence could not be computed | the rung-1 count is shown; it stops nothing, but read it with care |
| `held` | not fitted, because the energy zero could not be set | the reason names what is missing; the scan is re-examined on every later run |
| `fitting` | ingested, not yet fitted | nothing to do until the next run |

Two row states are not ladder states: `not fitted` (the scan is excluded, or undersampled and so not fitted)
and `not recorded` (a detector outcome written before the count ladder existed; a re-run fills it in).

**"Rung 2 pending"** means the spectrum is `provisional`. In `isar run` rung 2 runs to completion within the
same invocation, unless the 30-minute per-spectrum budget is reached first, which makes the spectrum
`search limited`. During a campaign each refresh spends at most two minutes on rung 2, so a spectrum can stay
`provisional` for several refreshes. It resumes where it stopped and becomes `settled` once its contenders are
sampled. A campaign does not stop on a provisional spectrum, and only a settled, unambiguous count enters the
dispersion prior. Pooled masters run rung 2 to completion inside their own fit, so they are never provisional
for that reason. How the rungs and states are decided is in `STATS_PRIMER.md` §2.2.

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
per-analyzer files) and for **fitting** with the beamline's measured resolution curves, once the card
names its resolution folder (see *Resolution, slit and zero* below). Two processed layouts are read. The raw SPEC files the beamline writes (`specixs_*`, `spects_*`) are
not: `isar init` on one stops with "has no shipped card template yet".

Point ISAR at the folder, as for any instrument:

```
isar init "D:\beamtime\leTacon_SP8_CsV3Sb5" --out my_bl43
isar ingest "D:\beamtime\leTacon_SP8_CsV3Sb5" --workspace my_bl43
```

`isar init` detects the instrument from the first per-analyzer file and writes
`my_bl43\card.policy.toml` from the shipped template. `isar ingest` makes one scan per set of files,
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

### What the card's tables do

- `[files]`: `measurement` holds regexes with named groups. Each must name `set` (the scan's key,
  shared by its analyzer files) and `analyzer`; the other groups (sample, `T_K`, `hkl_nominal`,
  `specfile`, `scan_numbers`, `hkl`) become filename facts. `sidecar` is `{set}.var`. `ignore` lists
  globs that are never read as data. Subfolders are walked, and a set is the files of one folder: the
  same stem in two folders is two sets (the second is refused by name if it lands on the first's
  identity, see below). The `rf/` folder of SARF resolution files is classified unrecognized and never
  read as data; `[calibration]` reads it as the resolution library.
- `[reader]`: `intensity_family` is `"_i"` (the default: integer counts, raw) or `"_lr"`. The `_lr`
  counts split some counts between neighbouring bins, so they are **not raw**. They are kept unrounded and
  every `_lr` spectrum is flagged `counts_not_integer` (a warning, whatever its bins recover to). For `_i`,
  a few bins that do not recover to an integer (print rounding) raise the same flag as a notice; when more
  than 10% of the valid bins do, it is a warning, because the columns are then probably not counts. The
  calibration holds a spectrum carrying the warning and never fits it ("non-raw counts (_lr) are not
  fitted"); a notice is print rounding of raw counts, and that spectrum is fitted.
- `[facts]`: values you declare, for example `slit = "60x60"` or `lattice = [...]`. They rank lowest: a
  header, trailer, sidecar or filename value wins, and a disagreement raises `fact_conflict`.
- `[calibration]`: `method = "tabulated_elastic"` (see *Resolution, slit and zero*). `resolution_folder`
  is the folder of SARF files, relative to the card's folder unless absolute; `resolution_pattern` matches
  their file names and picks out the analyzer and the slit.
- `[[detectors]]`: `enabled = false` leaves an analyzer out of every scan. `value_index` and
  `signal_column` are not used for this instrument.

### Header lines the group's macro can write

A `# key value` line before the data (the column-label line is skipped) gives the same fact that the file
name or the card would, and it wins over them. The reader takes:

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

The `.var` sidecar supplies `si_order`, `bin_meV` (the bin size) and `n_merged_scans`, and for each
analyzer `efficiency`, `polarisation_factor`, `elastic_centroid_meV` and `elastic_area`. A `.var` that
is missing or unreadable raises `missing_sidecar`; one that is empty or has no analyzer list (for
example still being copied) counts as unreadable, and re-ingesting once it is whole completes the scan.

### Reading a fact's source label

The inspector shows every fact with its source:

| label | meaning |
|---|---|
| `header` | a `# key value` line, or a trailing `#Q=` / `#T=` line, in the file itself |
| `trailer` | a trailing line that disagrees with the leading header (kept as a conflict) |
| `sidecar` | the `.var` file beside the analyzer files |
| `filename` | the file-name pattern (the card's `[files]` groups) |
| `card` | a value you declared in `[facts]` |
| `inferred` | derived by ISAR: `energy_keV` from `si_order` (1.977 keV per silicon order) when nothing else gives it, the count-recovery deviation, and `points_per_bin` (read from the `npts` data column) |
| `missing` | nothing supplied it. The value is empty, and nothing downstream may invent one |

The precedence is header, then trailer, sidecar, filename, card; the first found wins.

A spectrum's `q_source` says which rung gave its Q. `header` or `filename` is rung 1 (the analyzer's own
HKL). `none` is rung 4: no Q, `q = None`. Rungs 2 and 3 are planned, not built. For CsV3Sb5 every analyzer
is rung 4, and the scan's `hkl_nominal` is the only HKL in the record.

### Resolution, slit and zero

A scan's slit sets which resolution curve each analyzer uses, and each analyzer's energy zero is fitted
on its elastic line with that curve. To set this up:

1. Keep the `rf/` folder of SARF files beside the card (the template's `resolution_folder = "rf"`), or
   give its full path as `resolution_folder`.
2. Declare the slit if you know it: the header lines `agaph_mm` and `agapv_mm`, or `slit = "60x60"`
   under `[facts]`. A declared slit is always used.
3. `isar run` builds the calibration. The inspector shows each scan's slit, its candidate deviances and
   the alerts; the numbers are kept under `tabulated_calibration` in `card.state.json`, and the policy is
   never written.

With no slit declared, ISAR picks the slit whose curves best fit the scan's elastic lines and says so.
Alerts on that choice and on the zeros:

| Alert | Meaning | What to do |
|---|---|---|
| `slit_inferred` | No slit was declared; ISAR chose one from the elastic lines. | Declare the slit if you know it. |
| `slit_ambiguous` | The runner-up slit fits within 10 in summed deviance, so the data do not pick one. | Check the slit and declare it. |
| `slit_mismatch` | The declared slit is kept, but another fits the elastic lines clearly better. | Check the declared slit and the curve files. |
| `pipeline_zero` | The analyzer has no significant elastic line. Its zero is the file's own axis, labelled. | Compare that analyzer's ω₀ with its neighbours before trusting it. |
| `missing_resolution` | No curve for that analyzer at that slit, or no folder: the analyzer is held and not fitted. | Add the curve to `rf/`, or correct `resolution_pattern`. |
| `resolution_range_exceeded` | The fit range runs past the measured curve; the curve is used as given. | Read the fit with that in mind. |

Held analyzers and scans are listed under "Held / declined" in the report, with their reason.

### Limits for BL43LXU

- An analyzer's zero is its fitted elastic line, or the labelled pipeline zero. ISAR never defaults an
  energy zero to 0.0 on an uncalibrated axis.
- The in-plane momentum spread of the resolution is not modelled. Q rungs 2 and 3 are not built, so an
  analyzer's Q stays absent unless its file gives an HKL.
- `_lr` counts are parsed and shown, never fitted (see `[reader]`).
- The raw SPEC files are not read.
- The Ba122 layout has no monitor, and ISAR does not deduce one.

## New instrument, no parser?

`isar init` recognizes the instruments with registered parsers (currently Sector-30 HERIX SPEC —
supplementary and raw —, ILL IN8 numor ASCII and SPring-8 BL43LXU processed per-analyzer files, above;
the shared SPEC/numor readers are built for ID28 to slot in as cards + thin plugins). If your format isn't recognized, a parser plugin
plus a card template is what's needed — see `ARCHITECTURE.md` §6 and `isar/parsers/base.py`; the schema
(`ScanRecord`) is fixed, only the reader varies.

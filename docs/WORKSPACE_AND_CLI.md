# Workspace, session, and CLI — the as-built contract

> **Status:** live
> **Authority:** canonical for the workspace, session and CLI contract
> **Verified against code revision:** `d483af1` (2026-08-08)

What `isar` is from the outside: a directory per experiment, a ledger inside it, and a command
set that reads and extends both. This document is the current contract. The design that produced
it is the retired unification plan — deleted 2026-08-20; source docstrings that used to cite its
section numbers now name it explicitly and point here plus git history for *why*, never for
*what is true now*.

## The workspace

One directory per experiment (`isar/session/workspace.py`):

```
<workspace>/
  card.policy.toml    # human-owned; the machine NEVER writes it
  card.state.json     # machine-owned accumulated state — experiment-critical, not deletable
  session.json        # machine bookkeeping (manifest + ledger) — deletable, rebuildable
  report/             # report.md/.html/.json, reduction.*, results.csv, modes.csv, figures/,
                      # measurements.jsonl (counts-authoritative arena log), scene/ (revisions),
                      # curves.json (BL43 resolution-curve check: folder, curves by content),
                      # intake_scans.json (BL43 import record: the file values of every scan read;
                      # machine-written, rebuilt by a re-import), fit_inputs.jsonl (what each pass
                      # used; append-only), fit_history.jsonl (every ledger entry a re-import or
                      # refit replaced, kept in full; append-only)
    scene/            # scene revisions: <digest>.json blobs + current.json pointer
  intake/             # BL43 intake records, ISAR-written only: settings.jsonl (experiment
                      # settings), entries.jsonl (user values and conditions; experiment-critical like
                      # card.state.json, not deletable), .lock (held for one append)
```

The `intake/` records are append-only: a save adds a line, a later line supersedes an earlier one for
the same setting, and nothing is rewritten or erased. Only ISAR's own writers add to them. The
experiment's resolution-curve folder is the latest `resolution_folder` line in `settings.jsonl`; it is
saved by the GUI (or discovered once, at the first import or run, when none is saved) and never changed by
an import, a run or a fit. `report/curves.json` records what the last check found: the folder in force, its
fingerprint, each curve's content hash, and any other `rf/` folder that also holds curves. Imports and run
starts write it; nothing else does.

### Values, conditions and what each fit used (BL43)

For a BL43LXU workspace the colleague can save, per scan, a temperature or a slit that overrides the one
the files give, and can declare an excluded scan, a dead analyzer or accepted counts (see the glossary).
Each save is one line of `intake/entries.jsonl`, checked before it is written (a refusal gives its reason
and writes nothing); a damaged line is skipped, logged and reported by the reads, and never stops a save,
an import or a run. `report/intake_scans.json` holds what the files said (the file values, each analyzer's
Q and count record), written by `isar ingest` and by `isar run`; it never holds a user value.

- **Start point.** A run lists every unit of its scans and decides nothing about freshness up front. When
  it reaches a scan's first unit it reads `entries.jsonl` from disk, builds the effective record and
  freezes it for that scan's calibration and all its analyzers' fits; there it decides, per unit, carry or
  fit. A save made after a scan's start point is used from the next run, and the reads mark the results
  meanwhile. A save never requeues anything by itself.
- **Effective record.** The temperature and slit in force replace the file's (a missing temperature stays
  missing and falls back, never 0 K); dead analyzers are absent; the analyzers with accepted counts are
  named. The parsed record is never changed.
- **Calibration reuse.** A scan's tabulated calibration state carries an `inputs_key`, the hash of its data
  revision, resolution folder, pattern, library fingerprint, declared slit, analyzers and accepted counts.
  It is reused only when that key is unchanged; otherwise the scan is recalibrated before its units are
  fitted. An excluded or dead unit becomes an `excluded` ledger entry carrying the declaring entry in its
  reason; it is never fitted and has no fit-inputs line.
- **Count hold.** A spectrum with a warning-level `counts_not_integer` is held (`held_for_calibration`)
  unless its counts are accepted and it is an `_i` spectrum; the notice level holds nothing.
- **Fit inputs.** `report/fit_inputs.jsonl` gets a run start line, a started line per unit and pass at the
  scan's start point (the frozen inputs, their hash and their fallbacks), a finished line as each unit
  completes, and an end line after the workspace save. A run that died, failed or was cut by a deadline
  saved nothing to the ledger, so its lines do not count, except a unit the ledger holds by its stamp (a
  checkpoint). A GUI Cancel kills the process and writes no end line: a run with no end line whose process
  is gone is not running. Each ledger entry processed in a run carries
  `provenance["fit_inputs"] = {run, hash, fallbacks}`, and a later start point refits a unit whose frozen
  inputs hash differs from it. The hash covers exactly the values the reads compare: data revision,
  temperature, slit, Q, the conditions, whether the counts override applied, and the calibration's folder,
  slit setting and basis, zero and its source, held reason and the unit's own curve (the library only when
  the slit was chosen by comparing curves).
- **Reads.** `Workspace.fact_rows(scans=None)` and `Workspace.fit_inputs(units=None)`
  (`isar/workspace_api/intake_view.py`) are read-only, Qt-free and scipy-free. Fact rows give, per scan,
  the temperature and slit and, per analyzer, Q and counts: the file value and source, the user value,
  the value used, any disagreement and any fallback. Fit inputs give, per analyzer, the latest fast and
  latest deep pass with the inputs it used and its *changes since*, judged on values against the inputs in
  force now: the saved values and conditions, the calibration state in `card.state.json`, the folder in
  force, and the library and curve identity from the last `report/curves.json` (never hashed at read
  time, so a replaced curve is marked from the next import or run start). With none of the files, both
  return `[]`. They take no lock and never block a writer: the import record is replaced atomically and
  the two logs are append-only, so a read during a run or a save sees the last committed state.
  Each parses its files once per `refresh()`; `fit_inputs` parses the whole log, which grows by about
  1.4 MB per fast pass at 30 scans x 28 analyzers (about 0.4 s at 20 runs, 30 ms at one), so call it from a
  worker thread. Each later call re-judges liveness and the latest passes over the parsed log (about 35 ms
  at 20 runs), so a run killed after a read stops counting at once. `Workspace.watch` reports `intake_entries`,
  `intake_settings`, `intake_scans`, `fit_inputs` and `curves` among its change names.
- **Readiness and conditions.** `needs_attention(root)` gives readiness per scan, per analyzer and per use
  (read, reduce, calibrate, fit, steer: ready, fallback, pending, held or unreadable) and the grouped
  needs-attention list, each group with the action that resolves it and whether analysis can start;
  `import_summary(root)` gives the scans and analyzers read, the incomplete sets, the files not read and where
  the curves were found; `Workspace.conditions(scans=None)` lists the conditions in force (excluded scan, dead
  analyzer, counts accepted) with the reason saved with each. All three are read-only, Qt-free and scipy-free
  (`isar/intake/readiness.py`, `isar/workspace_api/intake_view.py`) and for BL43LXU workspaces only
  (`evaluated=False` with a note otherwise). Readiness has no file of its own: it is computed on every read from
  the import record, the entries and settings, the curve check, the calibration state and the fit-inputs log,
  using the stages' own checks (the fit stage's count hold, the curve coverage, the calibration's inputs key), so
  it cannot disagree with a run; it never raises, and a record it cannot read is reported, not guessed. An
  analyzer whose file the latest import recorded as failed or refused is held as unreadable (`import_again`)
  whatever its earlier row says; the next import or `isar run` that reads the file clears that entry. A group
  whose items are all running reads `wait`.
  `import_summary` (about 3 ms) and `conditions` are GUI-thread safe; `needs_attention` parses the fit-inputs
  log for its running and changed marks, so call it from a worker thread (about 0.33 s with a 20-run log of
  19 MB at 30 scans x 28 analyzers, of which the readiness itself is about 15 ms).
- **`Change.field`** (the *changes since* rows) is one of a closed list: `data` (the imported revision),
  `temperature`, `slit`, `q`, `excluded`, `dead`, `dead_in_scan` (a sibling analyzer declared dead), `counts_accepted`,
  `resolution_folder`, `curve` (the unit's curve content), `resolution_library` (the curves compared when the slit
  was chosen by comparison), and five fields of the calibration the pass was made with, compared with the
  calibration now so that a recalibration is never silent: `calibration_setting` (the slit setting chosen),
  `calibration_basis` (how it was chosen: `declared`, `inferred`, `broadest_curve_fallback` or `none`), `calibration_zero` (the energy zero),
  `calibration_zero_source` (elastic fit or file-axis fallback) and `calibration_held_reason` (why the
  analyzer is held, if it is). A recalibration moves several of these at once, and a dead declaration moves
  its own analyzer's calibration as well as `dead`. `now_source` is set only for `temperature` and `slit`.

**The two card files have opposite ownership, and that is the point.**
`card.policy.toml` is hand-edited human intent — detectors, column maps, collation and merge
policy, fitting guidelines. Code reads it and never writes it. `card.state.json` is
machine-written — calibration versions, the energy-zero series, the dispersion prior, exclusions,
corrections. Writing policy from code silently overwrites the scientist's intent; hand-editing
state silently corrupts machine bookkeeping.

The split between `card.state.json` and `session.json` is a survivability split, not a tidiness
one: `card.state.json` holds what the *experiment* owns and must survive; `session.json` holds
what the *machine* owns and can be deleted and rebuilt from the data files plus a re-run.

Saves are atomic (write-temp + `os.replace`) and happen once, at the end of a successful run.

## The ledger

`SessionState` in `session.json` (`isar/session/state.py`) holds the ingest manifest (`files`)
and the ledger (`scans`).

- **Key:** `scan_id = f"{experiment_id}:{scan_number}:{detector_id}"` — the per-detector identity
  both ingest and the driver write. The scan-level `exp:scan` form appears only in report output,
  never as a ledger key.
- **Status vocabulary:** `ingested`, `fitted`, `held_for_calibration`, `held_for_sample`,
  `excluded`, `context_applied`, `non_result`. Every `held_*` entry re-enters the fit queue on a
  later run — that is how a scan held for a missing energy zero is picked up once the calibration
  arrives.
- **Provenance per entry:** resolution version, zero source, prior size and source, `M`, seed,
  code versions, and (BL43) `fit_inputs`: the run, the inputs hash and the fallbacks of the fit.

### Idempotency: re-parse everything, re-fit selectively

Every `isar run` re-parses every manifest file, and re-*fits* only scans whose ledger entry is
new, previously held, whose `content_hash` changed, or whose `guidelines_hash` changed, or (BL43)
whose fit inputs (data revision, user values, conditions, calibration) changed.

- `content_hash` covers the parsed record — it makes a growing-file resend a re-parse plus dedup
  rather than a duplicate.
- `guidelines_hash` covers the `[fitting]` guidelines that applied to *that* scan. This is what
  makes a card edit a real correction channel: the edit changes the hash for exactly the scans it
  touches, those re-enter the fit queue, and nothing else refits.

Files are the truth, so a crash before the end-of-run save is a clean no-op.

## Commands

<!-- isar-commands: exhaustive -->
```
init  detect  ingest  run  status  profile  report  diagnose  score  view  scene-build  drive  answer
```
<!-- /isar-commands -->

| Command | What it does |
|---|---|
| `isar init <data> [--out DIR]` | Detect the instrument and scaffold a workspace: the matching card template copied verbatim as text (its comments are the documentation), empty state, printed next steps. |
| `isar detect <data>` | Report the instrument detected from a data path. Writes nothing. |
| `isar ingest <data>... --workspace DIR` | Parse into the ledger **without fitting** — one `ingested` entry per `(exp, scan, detector)`, so parsed scans are visible through `workspace_api` before any run. Never fits, never deletes, never writes `card.policy.toml`. |
| `isar run <data>... --workspace DIR` | The whole pipeline: ingest → parse/classify → route context events → QC/combine → optional LLM adjudication → calibration-gated fit with prior → verdict/confidence → collate → report. Idempotent; re-run per batch. |
| `isar status --workspace DIR` | Ledger summary without reprocessing: counts by status and verdict, held scans with reasons, scan-number and coverage gaps, calibration versions, prior population; for a BL43LXU workspace also the needs-attention list (the groups the GUI shows). |
| `isar profile --workspace DIR` | Where a campaign spent its time, from the session log; `--compare-workspace` diffs scientific decisions and certified outputs against another workspace. |
| `isar report --workspace DIR` | Re-render `report.md`/`.html`/`.json` and the dispersion figure. **Reads the ledger only — no reparse, no refit** (`isar/cli/report_cmd.py`). `--scene` additionally rebuilds the scene revision from the persisted ledger and publishes it under `report/scene/` — read-only in the same sense (no re-fit, no re-collation); a workspace the scene adapter cannot read faithfully exits **3** rather than writing an empty scene. |
| `isar diagnose --workspace DIR` | Render the architect's `CAMPAIGN_DIAGNOSTIC.md` for a campaign — see `docs/design/reporting.md`. |
| `isar score --workspace DIR` | Score a run against a human gold CSV (`--gold`) or known analytic truth (`--truth-fn`/`--truth-csv`). Gold CSVs appear **nowhere** in the `run` path. |
| `isar scene-build --workspace DIR` | Build **one** scene revision from persisted state and print a single JSON line — `{schema, digest, pointer_flipped, unchanged}`. The same canonical build `isar report --scene` and `isar run` compose, with a machine-readable surface: the campaign parent runs it as a supervised subprocess once per delivery and pins the digest it returns (`docs/design/scene.md`, SCENE-4.3). Exit **0** means a durable revision exists at `digest` — including when a concurrent publisher won the pointer flip; exit **3** means there is none. |
| `isar view [workspace]` | Open the desktop GUI on a workspace (needs the `[gui]` extra) — see `docs/design/gui.md`. |
| `isar drive` | Run the autonomous measurement campaign from a natural-language `--goal`, an `isar.campaign-program/1` file (`--program`), or `--resume-latest` — see `design/steering.md`. |
| `isar answer --workspace DIR` | The operator's side of the consultation channel (SCENE-4.3): `--list` shows the prompts a running campaign has open — id, kind, subject, options, declared default, and how many deliveries remain before that default is taken — and `isar answer <prompt_id> <option> [--note TEXT]` writes the answer atomically to `report/control/prompt-<id>.answer.json`. An unknown id or an option the prompt does not offer is refused with exit **2** and nothing is written. The campaign never waits on it: the answer takes effect on the next delivery, and one that arrives after the default was applied is recorded stale, not applied. |

`isar <command> --help` is the authority on flags; this table is deliberately not a flag
reference, because a hand-maintained duplicate of argparse is a guaranteed drift source.
`tests/docs/test_doc_drift.py` pins the command list above to `isar.cli.main.build_parser()`.

The operating loop during a beamtime is: a batch of files lands → `isar run` (same command, same
workspace) → read the report → correct the card if needed → next batch. Re-running the batch CLI
*is* the live driver; there is no watch/daemon mode.

## The Stage II ↔ III boundary is a frozen file contract

Stage III (`isar drive`) consumes Stage II (`isar run`) through files only —
`report/results.csv`, `report/measurements.jsonl` (the counts-authoritative
`isar.measurement-arena/1` log: one line per measured arena, written independently of fit
success, SCENE-4 PR 4.0 #26), `<workspace>/report/report.json`, and `card.state.json`. The driver never imports fit
internals. The one pre-release v1→v2 clean break was authorized on 2026-07-17 (ruling O12);
from v2 onward, contract changes are additive only (`ARCHITECTURE.md` §4) — except for the contracts the
SCENE program rebuilds, where ruling H1 supersedes that rule. The campaign contract is one of
them: `isar.campaign-program/1` replaced the objective envelope outright, and the retired schema ids
are refused rather than read (H2).

`isar drive` exits **0** when the campaign's stop state is `met`, **3** `incomplete` (budget
exhausted), **4** `operator_stopped`, **5** `pending_recovery`, **6** `infeasible`, and **2** on a
parse or configuration error. Exit **7** is separate and means the campaign program parsed, validated
and froze cleanly but this build cannot execute it — a discovery step or a multi-step program, both
of which land in SCENE-4.

## Validation status

The CLI/session path's outstanding gate is the **CLI/session equivalence gate at M=200** — the
83-scan S30 comparison run end-to-end through `isar run` against `results_gold.csv`. It is open
and tracked as `VAL-2` in `TODO.md`.

It is a different gate from the **analysis-core 83-scan gold gate**, which is passed and
committed under [assets/phase3_gold/](assets/phase3_gold/FINDINGS.md): that one runs
`docs/phase3_gold_check.py`, which calls the analysis modules directly rather than through the
session layer. The calibration layer is separately proven to reproduce the gold check's per-scan
inputs to machine precision (`tests/session/test_calibrations.py`).

# Workspace, session, and CLI — the as-built contract

> **Lifecycle:** Current
> **Authority:** canonical for the workspace, session and CLI contract
> **Provenance:** hand-written
> **Verified against code revision:** `389f571` (2026-07-25)

What `isar` is from the outside: a directory per experiment, a ledger inside it, and a command
set that reads and extends both. This document is the current contract. The design that produced
it is `UNIFICATION_DESIGN.md` — archived, kept at its path because ~40 source docstrings cite its
section numbers; read it for *why*, never for *what is true now*.

## The workspace

One directory per experiment (`isar/session/workspace.py`):

```
<workspace>/
  card.policy.toml    # human-owned; the machine NEVER writes it
  card.state.json     # machine-owned accumulated state — experiment-critical, not deletable
  session.json        # machine bookkeeping (manifest + ledger) — deletable, rebuildable
  report/             # report.md/.html/.json, reduction.*, results.csv, figures/
```

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
  code versions.

### Idempotency: re-parse everything, re-fit selectively

Every `isar run` re-parses every manifest file, and re-*fits* only scans whose ledger entry is
new, previously held, whose `content_hash` changed, or whose `guidelines_hash` changed.

- `content_hash` covers the parsed record — it makes a growing-file resend a re-parse plus dedup
  rather than a duplicate.
- `guidelines_hash` covers the `[fitting]` guidelines that applied to *that* scan. This is what
  makes a card edit a real correction channel: the edit changes the hash for exactly the scans it
  touches, those re-enter the fit queue, and nothing else refits.

Files are the truth, so a crash before the end-of-run save is a clean no-op.

## Commands

<!-- isar-commands: exhaustive -->
```
init  detect  ingest  run  status  profile  report  diagnose  score  view  drive
```
<!-- /isar-commands -->

| Command | What it does |
|---|---|
| `isar init <data> [--out DIR]` | Detect the instrument and scaffold a workspace: the matching card template copied verbatim as text (its comments are the documentation), empty state, printed next steps. |
| `isar detect <data>` | Report the instrument detected from a data path. Writes nothing. |
| `isar ingest <data>... --workspace DIR` | Parse into the ledger **without fitting** — one `ingested` entry per `(exp, scan, detector)`, so parsed scans are visible through `workspace_api` before any run. Never fits, never deletes, never writes `card.policy.toml`. |
| `isar run <data>... --workspace DIR` | The whole pipeline: ingest → parse/classify → route context events → QC/combine → optional LLM adjudication → calibration-gated fit with prior → verdict/confidence → collate → report. Idempotent; re-run per batch. |
| `isar status --workspace DIR` | Ledger summary without reprocessing: counts by status and verdict, held scans with reasons, scan-number and coverage gaps, calibration versions, prior population. |
| `isar profile --workspace DIR` | Where a campaign spent its time, from the session log; `--compare-workspace` diffs scientific decisions and certified outputs against another workspace. |
| `isar report --workspace DIR` | Re-render `report.md`/`.html`/`.json` and the dispersion figure. **Reads the ledger only — no reparse, no refit** (`isar/cli/report_cmd.py`). |
| `isar diagnose --workspace DIR` | Render the architect's `CAMPAIGN_DIAGNOSTIC.md` for a campaign — see `CAMPAIGN_DIAGNOSTIC_SPEC.md`. `--eig` prints the scan-window EIG decision table to stdout instead (latest decision per Q, its outcome, reason codes, legacy/target point counts and information margin, plus a reason-code histogram) and writes nothing at all. |
| `isar score --workspace DIR` | Score a run against a human gold CSV (`--gold`) or known analytic truth (`--truth-fn`/`--truth-csv`). Gold CSVs appear **nowhere** in the `run` path. |
| `isar view [workspace]` | Open the desktop GUI on a workspace (needs the `[gui]` extra) — see `GUI_DESIGN.md`. |
| `isar drive` | Run the autonomous measurement campaign — see `MEASUREMENT_STEERING.md`. |

`isar <command> --help` is the authority on flags; this table is deliberately not a flag
reference, because a hand-maintained duplicate of argparse is a guaranteed drift source.
`tests/docs/test_doc_drift.py` pins the command list above to `isar.cli.main.build_parser()`.

The operating loop during a beamtime is: a batch of files lands → `isar run` (same command, same
workspace) → read the report → correct the card if needed → next batch. Re-running the batch CLI
*is* the live driver; there is no watch/daemon mode.

## The Stage II ↔ III boundary is a frozen file contract

Stage III (`isar drive`) consumes Stage II (`isar run`) through files only —
`report/results.csv`, `<workspace>/report/report.json`, and `card.state.json`. The driver never imports fit
internals. The one pre-release v1→v2 clean break was authorized on 2026-07-17 (DECISIONS O12);
from v2 onward, contract changes are additive only (`DESIGN.md` §4).

## Validation status

The CLI/session path's outstanding gate is the **CLI/session equivalence gate at M=200** — the
83-scan S30 comparison run end-to-end through `isar run` against `results_gold.csv`. It is open
and tracked as `VAL-2` in `TODO.md`.

It is a different gate from the **analysis-core 83-scan gold gate**, which is passed and
committed under [assets/phase3_gold/](assets/phase3_gold/FINDINGS.md): that one runs
`docs/phase3_gold_check.py`, which calls the analysis modules directly rather than through the
session layer. The calibration layer is separately proven to reproduce the gold check's per-scan
inputs to machine precision (`tests/session/test_calibrations.py`).

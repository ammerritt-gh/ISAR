# Workspace, session, and CLI — the as-built contract

> **Lifecycle:** Current
> **Authority:** canonical for the workspace, session and CLI contract
> **Provenance:** hand-written
> **Verified against code revision:** `d483af1` (2026-08-08)

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
  report/             # report.md/.html/.json, reduction.*, results.csv, figures/,
                      # measurements.jsonl (counts-authoritative arena log), scene/ (revisions)
    scene/            # scene revisions: <digest>.json blobs + current.json pointer
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
init  detect  ingest  run  status  profile  report  diagnose  score  view  scene-build  drive  answer
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
| `isar report --workspace DIR` | Re-render `report.md`/`.html`/`.json` and the dispersion figure. **Reads the ledger only — no reparse, no refit** (`isar/cli/report_cmd.py`). `--scene` additionally rebuilds the scene revision from the persisted ledger and publishes it under `report/scene/` — read-only in the same sense (no re-fit, no re-collation); a workspace the scene adapter cannot read faithfully exits **3** rather than writing an empty scene. |
| `isar diagnose --workspace DIR` | Render the architect's `CAMPAIGN_DIAGNOSTIC.md` for a campaign — see `CAMPAIGN_DIAGNOSTIC_SPEC.md`. `--eig` prints the scan-window EIG decision table to stdout instead (latest decision per Q, its outcome, reason codes, legacy/target point counts and information margin, plus a reason-code histogram) and writes nothing at all. |
| `isar score --workspace DIR` | Score a run against a human gold CSV (`--gold`) or known analytic truth (`--truth-fn`/`--truth-csv`). Gold CSVs appear **nowhere** in the `run` path. |
| `isar scene-build --workspace DIR` | Build **one** scene revision from persisted state and print a single JSON line — `{schema, digest, pointer_flipped, unchanged}`. The same canonical build `isar report --scene` and `isar run` compose, with a machine-readable surface: the campaign parent runs it as a supervised subprocess once per delivery and pins the digest it returns (`SCENE_DESIGN`, SCENE-4.3). Exit **0** means a durable revision exists at `digest` — including when a concurrent publisher won the pointer flip; exit **3** means there is none. |
| `isar view [workspace]` | Open the desktop GUI on a workspace (needs the `[gui]` extra) — see `GUI_DESIGN.md`. |
| `isar drive` | Run the autonomous measurement campaign from a natural-language `--goal`, an `isar.campaign-program/1` file (`--program`), or `--resume-latest` — see `MEASUREMENT_STEERING.md`. |
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
internals. The one pre-release v1→v2 clean break was authorized on 2026-07-17 (DECISIONS O12);
from v2 onward, contract changes are additive only (`DESIGN.md` §4) — except for the contracts the
SCENE program rebuilds, where DECISIONS H1 supersedes that rule. The campaign contract is one of
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

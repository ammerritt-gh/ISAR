# Reporting — campaign diagnostic (originally "Campaign diagnostic spec")

> **Status:** live
> **Authority:** canonical campaign-diagnostic / reporting design
> **Verified against code revision:** `8fa1219` (2026-07-25)

## As built

**Implemented (2026-07-10).** The spec below was agreed 2026-07-09 against a hand-assembled
prototype; it has since shipped and this section states what the code actually does. Where the
design body below and this section disagree, this section wins.

| | |
|---|---|
| **Command** | `isar diagnose --workspace DIR` (`isar/cli/diagnose_cmd.py`) |
| **Automatic** | Yes — `isar/drive/loop.py` calls `_render_campaign_diagnostic(cfg)` at the end of every campaign, before the terminal `stop` event, whatever the outcome |
| **Renderer** | `isar/report/campaign_diagnostic.py`, entry point `render_campaign_diagnostic(workspace)` |
| **Output** | `<workspace>/report/CAMPAIGN_DIAGNOSTIC.md`, figures under `report/figures/` (dossier figures in `figures/dossier/`, buried-scan figures in `figures/scans/`) |
| **Inputs** | `campaign_summary.json`, `session_log.jsonl`, `report/results.csv`, `score.json` (when a truth source exists), `card.state.json`, plus a deterministic Stage-I re-parse of `<workspace>/data` for raw counts behind the figures |
| **Determinism** | Held. No refitting and no LLM in the assembly path. The re-parse crosses the same card+ingest boundary `isar run` uses; any failure degrades the figures to empty rather than raising |
| **Sections** | The fixed eight-section order in §2 below is what the renderer emits |
| **Golden test** | `tests/data/golden/CAMPAIGN_DIAGNOSTIC.mvp16.md`, exercised by `tests/report/test_campaign_diagnostic_characterization.py` |

**Known gaps.** The optional LLM narrative layer described as a later addition (§0 principle) has
not been built, and the separate *user-facing* campaign report — as opposed to this architect-facing
diagnostic — remains Planned (`../ARCHITECTURE.md` §15).

---

## Original design spec

**Status:** design, agreed 2026-07-09 — retained for the reasoning behind the contract above.
Prototype (hand-assembled against `out/mvp_zone_test16`): `MVP16_DIAGNOSTIC_REPORT.md`, since
superseded by the generated artifact. This spec turned that prototype into a generated artifact.

**Problem.** A campaign workspace contains the entire story of a run — objective, decisions,
per-scan verdicts, truth scores — and no artifact tells it. `report.md` is a triage table for the
*during-beamtime* user; the summary a supervisor emits ("passed, some problems") is a verdict
without a trial. During development, the missing audience is the **architect**: what did the
campaign do, where did it fail, *why*, and what should change. A user-facing campaign report is
a later, separate artifact; this one is diagnostic.

**Principle (inherits P1/P5/P6).** The report is assembled **deterministically** from files the
run already writes. No refitting, no LLM in the assembly path. An optional LLM narrative layer
may *later* be added on top, clearly marked, off the critical path.

---

## 1. Trigger & placement

- Generated at the end of every `isar drive` campaign (met, incomplete, infeasible, operator stop, or crash
  — a partial report from a crashed campaign is more valuable, not less).
- Written to `<workspace>/report/CAMPAIGN_DIAGNOSTIC.md` with figures under
  `<workspace>/report/figures/`.
- Renderer lives in `isar/report/` and consumes **only** the file contract:
  `campaign_summary.json`, `session_log.jsonl`, `report/results.csv`, `score.json` (when a truth
  source exists), `card.state.json`. It must not import fit or drive internals.

## 2. Report structure (fixed section order)

1. **Objective** — the NL goal, formalized path/tolerance/tier, optional measurement budget, analysis
   limit, and pass criteria.
2. **Outcome at a glance** — one table: certified masters n/N, outcome, both configured clocks, wall time split
   (fitting vs instrument vs overhead), center accuracy vs truth (when scored), interval health.
3. **What the driver did** — the loop narrative in ≤ 10 numbered steps: seeding, model updates,
   escalations, retries, the stop decision. Derived from `session_log.jsonl` event counts and
   first/last exemplars, not prose invention.
4. **Delivered dispersion vs truth** — table + dispersion figure; physical points are provenance,
   while pooled masters and their model-robust intervals are decision-bearing (see §4).
5. **Failure dossier** — one subsection per failure *zone* (contiguous q sharing a mechanism),
   each with: the claim, the evidence rows, the figure(s), the diagnosis, the TODO/fix mapping.
   This is where the reader's attention budget is spent.
6. **Defects uncovered** — machinery defects (logging, accounting, presentation), numbered.
7. **Verdict & recommended actions** — priority-ordered, each mapped to an existing TODO item or
   explicitly marked new.
8. **Provenance** — table: section → source file. Every number re-presentable from the workspace.

## 3. Figure policy

Figures are the natural record of what happened; the *report* stays within a human attention
budget by tiering them:

- **Always rendered, always inline:** dispersion vs truth (physical provenance points, pooled master
  intervals, truth curve, certification/diagnostic markers); per-q measurement-spend bar; separate
  measurement and analysis-clock summaries.
- **Dossier figures, inline:** for every scan cited as evidence in §5 — data + fit components +
  residual, in the style of a per-scan quicklook. Failures are *always* figured; successes only
  as representative contrast (1–2 per zone).
- **Full per-scan set, buried:** every fitted scan gets a data+fit+residual PNG under
  `figures/scans/<scan_id>.png`. The triage table links to them; the diagnostic report links only
  the dossier subset. (Per-scan rendering at fit time also serves the future LLM
  residual-diagnosis pass, which wants the same image.)
- Every figure carries a drawn-on caption plus a `<figure>.caption.md` sidecar (existing
  convention from the `*_check.py` reports).

## 4. Interval honesty in presentation

- A campaign certification interval is the pooled master's model-weighted predictive ω interval.
  A narrow individual-fit interval is never substituted for it.
- Any interval with `interval_reliable == False` is rendered as `(unreliable)` — never as a
  numeric range, never `[3.15, 3.15]`, and never with a confidence that implies certification.
- Collate-only rows (`branch_continuation`) are **not** listed row-by-row in this report; they
  appear as per-q counts ("6 collate-only") with a link into `results.csv`.
- Reject/junk rows do not display fitted ω₀ values in summary tables (the number exists in
  `results.csv`; presenting it beside "junk" is confusing).

## 5. Logging requirements (new events — prerequisites for honest reporting)

The MVP-16 audit could not determine whether tier escalation at the boundary q *fired and failed
to seat* or *never fired*. That is a logging failure. Required additions to `session_log.jsonl`:

1. **`escalate_fit`** — emitted whenever a scan is promoted from a cheap tier to a fuller error
   tier. Payload: `scan_id`, `from_tier`, `to_tier`, `reason`, and on completion an outcome
   record (`seated` / `failed` / `superseded`) so the promotion and its result are both in the log.
2. **Master assessment taxonomy.** Per-q state is `unmeasured`, `uncertified`, `certified`, or
   independently established `infeasible`. Separate diagnostics include `diminishing_returns`,
   `interval_nonshrinking`, `model_ambiguous`, `pool_inconsistent`,
   `linewidth_resolution_limited`, and `analysis_failed`. Diagnostics do not autonomously stop q.
3. **`recenter` guard.** Recenter proposals log the proposed new center and the guard decision:
   recentering onto |ΔE| ≲ the resolution FWHM is refused and logged as
   `recenter_refused_elastic` (the MVP-16 log shows the driver re-aiming at ΔE = 0.009 meV — on a
   real instrument that is beam spent pointing at the elastic line).
4. **`decide` payloads get an `action` field** (the audit had to fingerprint payload strings to
   count actions).
5. **`analysis_clock`** — durable start/checkpoint/end records with stable operation ID and cumulative
   monotonic elapsed seconds. Diagnostics replay the maximum per operation and report unmatched spans.
6. **`model_update.per_q`** — each latest row carries master revision, assessment, exact continue/stop
   reason, consistency, model/branch and Γ state, expected next information, and diagnostics.
7. **Certification status** — `report/certification/<fingerprint>/progress.json` records durable
   master units, reuse source, active count, phase, remaining guardrail allowance, and publication
   state. A current marker is complete only when its hashes match `session.json`, `card.state.json`,
   and `results.csv`; version-1 physical-scan checkpoints are retained history, not reusable work.

## 6. Convergence-rule requirement (report-adjacent, logged here so it isn't lost)

Convergence consumes only a published pooled master revision. It requires a valid certifying method,
a model-robust ω interval within tolerance, no unresolved ω identity ambiguity, confirmed target track
where applicable, and a passing pool-consistency check. Γ at its resolution boundary is a diagnostic,
not a blocker when ω remains identifiable after marginalization. No individual fit, decline count,
candidate rejection, low-gain estimate, or production count can provide a fallback or abandon q.

## 7. Acceptance criteria

Generated against the MVP-16 workspace, the report must let a reader answer, without opening any
other file:

1. What was the objective, and did it end `met`, `incomplete`, or `infeasible`?
2. For every uncertified q: which master state and diagnostics, with evidence and a master figure.
3. Were delivered centers accurate? (median |Δω₀| vs truth, when a truth source exists.)
4. Which intervals are certified vs unreliable, at a glance.
5. Where did measurement spend and cumulative analysis time go, and does each configured clock replay
   consistently from its durable ledger?

A characterization test pins the renderer against a frozen copy of the MVP-16 workspace: same
inputs → same report (modulo timestamps).

## 8. Deliberate non-goals

- No user-facing / publication-facing campaign report (later artifact, different audience).
- No LLM narration in v1 (the deterministic skeleton must stand alone first).
- No refitting or re-scoring at render time — the report is a *presentation* of the run's own
  record. If the record can't answer a question, that is a logging defect to fix at the source
  (§5), not something the renderer papers over.

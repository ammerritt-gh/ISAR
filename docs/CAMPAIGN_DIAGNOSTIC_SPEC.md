# CAMPAIGN_DIAGNOSTIC — design spec

**Status:** design, agreed 2026-07-09. Prototype (hand-assembled against `out/mvp_zone_test16`):
`MVP16_DIAGNOSTIC_REPORT.md`. This spec turns that prototype into a generated artifact.

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

- Generated at the end of every `isar drive` campaign (success, unmeetable, budget-stop, or crash
  — a partial report from a crashed campaign is more valuable, not less).
- Written to `<workspace>/report/CAMPAIGN_DIAGNOSTIC.md` with figures under
  `<workspace>/report/figures/`.
- Renderer lives in `isar/report/` and consumes **only** the file contract:
  `campaign_summary.json`, `session_log.jsonl`, `report/results.csv`, `score.json` (when a truth
  source exists), `card.state.json`. It must not import fit or drive internals.

## 2. Report structure (fixed section order)

1. **Objective** — the NL goal, the formalized spec (path, tolerance, budget, tier), pass criteria.
2. **Outcome at a glance** — one table: converged n/N, stop state, budget spent, wall time split
   (fitting vs instrument vs overhead), center accuracy vs truth (when scored), interval health.
3. **What the driver did** — the loop narrative in ≤ 10 numbered steps: seeding, model updates,
   escalations, retries, the stop decision. Derived from `session_log.jsonl` event counts and
   first/last exemplars, not prose invention.
4. **Delivered dispersion vs truth** — table + the dispersion figure; flagged-interval values
   visibly marked (see §4).
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

- **Always rendered, always inline:** dispersion vs truth (delivered points, calibrated
  halfwidths, truth curve, converged/uncertifiable/stuck markers); per-q scan-spend bar
  (production/confirm/declined per q); budget & timing summary.
- **Dossier figures, inline:** for every scan cited as evidence in §5 — data + fit components +
  residual, in the style of a per-scan quicklook. Failures are *always* figured; successes only
  as representative contrast (1–2 per zone).
- **Full per-scan set, buried:** every fitted scan gets a data+fit+residual PNG under
  `figures/scans/<scan_id>.png`. The triage table links to them; the diagnostic report links only
  the dossier subset. (New work: the campaign path currently renders **one** figure per run —
  `dispersion.png`. Per-scan rendering at fit time is a prerequisite; it also serves the future
  LLM residual-diagnosis pass, which wants the same image.)
- Every figure carries a drawn-on caption plus a `<figure>.caption.md` sidecar (existing
  convention from the `*_check.py` reports).

## 4. Interval honesty in presentation

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
2. **Stop-reason taxonomy.** Per-q stop reasons become an enum, not free text. Minimum states:
   - `no_signal` — descriptor finds no significant peak in repeated scans;
   - `signal_unmodeled` — signal present, model cannot account for it;
   - `uncertifiable_interval` — fits deliver centers but every interval is `interval_reliable=False`
     (γ-at-bound); *this is not "no signal" and must never be reported as such*;
   - `interval_floored` — certified interval exists but floors above tolerance (the confirm-churn
     case);
   - `fast_tier_only` — no fit ever reached the certifying tier (should co-occur with
     `escalate_fit` records explaining why);
   - `infeasible` — the point is unreachable by the instrument.
   The human-readable stop message is *generated from* the enum + counts, not hand-written.
3. **`recenter` guard.** Recenter proposals log the proposed new center and the guard decision:
   recentering onto |ΔE| ≲ the resolution FWHM is refused and logged as
   `recenter_refused_elastic` (the MVP-16 log shows the driver re-aiming at ΔE = 0.009 meV — on a
   real instrument that is beam spent pointing at the elastic line).
4. **`decide` payloads get an `action` field** (the audit had to fingerprint payload strings to
   count actions).

## 6. Convergence-rule requirement (report-adjacent, logged here so it isn't lost)

The `interval_reliable` flag must be enforced **uniformly** in the convergence rule: a flagged
interval never seats convergence *and* never counts toward the decline budget as "no signal" —
it routes the q to the `uncertifiable_interval` state. In MVP 16 the flag was fatal at the zone
boundary and ignored mid-zone, producing both fictional mid-zone precision and falsely-labeled
boundary failures from one inconsistency. (Fix lives in `isar/drive/`; the report simply makes
the inconsistency impossible to hide.)

## 7. Acceptance criteria

Generated against the MVP-16 workspace, the report must let a reader answer, without opening any
other file:

1. What was the objective, and was it met? (10/21, `unmeetable`)
2. For every unconverged q: *which* enum state, with evidence rows and at least one figure.
3. Were delivered centers accurate? (median |Δω₀| vs truth, when a truth source exists.)
4. Which intervals are certified vs unreliable, at a glance.
5. Where did the time and budget go, and do the two ledgers (wall clock, counting seconds) look
   sane against each other? — the MVP-16 budget ledger (10.6 of 28 800 s spent) is flagged
   **verify-units** until confirmed.

A characterization test pins the renderer against a frozen copy of the MVP-16 workspace: same
inputs → same report (modulo timestamps).

## 8. Deliberate non-goals

- No user-facing / publication-facing campaign report (later artifact, different audience).
- No LLM narration in v1 (the deterministic skeleton must stand alone first).
- No refitting or re-scoring at render time — the report is a *presentation* of the run's own
  record. If the record can't answer a question, that is a logging defect to fix at the source
  (§5), not something the renderer papers over.

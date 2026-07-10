# HANDOFF — Pooled-fit member provenance (chain of custody)

**Repo:** `C:\Users\AMM\Documents\GitHub\ISAR` · **Scope:** small, surgical · **Priority:** high
(blocks the CAMPAIGN_DIAGNOSTIC failure-dossier evidence chain)

## Problem (verified 2026-07-09 against `out/mvp_zone_test16`)

Pooled "master" fits (synthetic scan IDs `9000001+`) record `pooled=True` and `n_members=<count>`
but **never name their member scans**. Verified absent from: `report/results.csv` (count only),
`card.state.json` (master fits absent entirely; grep for `9000004` → 0 hits), and
`campaign_summary.json` (`dispersion[].n_members` is a count). The chain of custody
*delivered ω₀ ← pooled scans ← kept/rejected passes* therefore breaks exactly at pooling.
Every other layer already carries provenance (reject masks, point flags, append-only corrections).

## Design rulings (agreed, do not revisit)

1. **Fit membership is binary and explicit.** A scan is either a member of a pooled fit or it is not.
2. **Branch influence is decision provenance, not data provenance.** Scans that shaped the branch
   model/priors without joining the pool are traced via `session_log.jsonl`, NOT via the members
   field. Do not add "indirect influence" lists to results.
3. Vocabulary: user-facing wording for `branch_continuation` rows is pending (Topic 5 spec);
   do not rename dispositions in this task.

## Task

1. **Locate the pooling site.** Likely `isar/analyze/loop.py` or `isar/analyze/select.py` — wherever
   the master pooled fit is assembled and assigned its `9000xxx` ID. The member scan IDs are in hand
   there (the pool is built from specific ScanRecords/results).
2. **Record membership on the pooled result object:** `members: list[str]` — the verbatim scan IDs
   (native scan numbers as strings; include detector/experiment qualifiers only if scan number alone
   is ambiguous in-workspace).
3. **Propagate to every serialization:**
   - `results.csv`: new column `members`, semicolon-joined, empty for unpooled rows.
   - `report.json`: `members` array on pooled entries.
   - `campaign_summary.json`: `dispersion[]` and `stop.per_q[]` entries gain `members` where a
     pooled/master value is reported.
   - Invariant everywhere: `n_members == len(members)`; assert at write time.
4. **Triage-view fix (report.md renderer):** pooled master rows currently render in the junk/reject
   section with a fitted ω₀ beside a "junk" label (observed for scan 9000004, H=2.2, reject via
   γ-at-bound). Pooled rows must render as `pooled (master, N members)` with the member list, never
   in the junk section; a rejected master is `pooled — uncertifiable`, distinct from junk.
5. **Renderer guard:** any delivered dispersion point lacking `members` fails report generation
   loudly (this field is now load-bearing for the diagnostic report's evidence chain).

## Constraints

- The drive↔analysis boundary is the frozen file contract (`results.csv`, `report.json`,
  `card.state.json`) consumed via `isar run` subprocess — extend the contract additively (new
  column/field), never break existing consumers; `drive` must tolerate files with and without the
  new field during transition.
- No refitting behavior changes. This is bookkeeping only; fit numerics must be bit-identical.
- Nothing here deletes or rewrites history: new fields only.

## Acceptance

1. Unit test: synthetic pool of 3 scans → pooled result carries the 3 IDs; CSV round-trips them;
   `n_members` invariant holds.
2. Characterization: re-run the analysis pass against a copy of `out/mvp_zone_test16/data/`;
   every `9000xxx` row in the regenerated `results.csv` has a non-empty `members` list whose length
   equals the old `n_members`; all fit numerics unchanged vs the frozen baseline.
3. `report.md` regenerated from the same workspace shows no pooled row in the junk section.

## Out of scope

- The `interval_reliable` convergence-rule unification (separate task, see CAMPAIGN_DIAGNOSTIC spec §6).
- Stop-reason enum / new log events (CAMPAIGN_DIAGNOSTIC spec §5).
- Any GUI or user-facing campaign report.

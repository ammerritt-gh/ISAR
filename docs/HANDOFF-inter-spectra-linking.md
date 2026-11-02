# Handoff — inter-spectra mode linking

> **Status:** live
> **Authority:** the frontier of the inter-spectra mode-linking job (j2eac6dfd06c3), rewritten at every closeout

**End condition:** job j2eac6dfd06c3's slice S4 (the inter-spectra pass uses the links) has landed or been cancelled by the operator, and the job is closed.
**Board entry:** none. The repository has no `WIP.md` board; this file is linked from `docs/READING_GUIDE.md`, Door 1.

## Frontier
- Landed: PR #87, job ja847b237965f (T0, outside this job; the link display cleanup): every mode of a linked spectrum is a phonon named P<n> on screen (stored names stay `B<n>`); a mode on no branch gets a lone phonon name from the same experiment counter, saved in the link record's new `lone` field; link notes, splits and markers are no longer shown on any view (they stay in the record); Dispersion shows only the analyzer path on an analyzer-layout experiment; Scans menu "Label modes in energy order". At `4198351d`.
- Landed: PR #86, slice S3 (showing the links: link read facade, branch names and link notes in the views, Analyzer array path gestures, the Dispersion view's Analyzer path) at `ff9af597`.
- Landed: PR #85, slice S2 (mode linking: the Qt-free linking core, the saved `link` record and queue kind, the Link modes button and the automatic switch) at `b171345d`.
- Landed: PR #84, slice S1 (the per-mode width ceiling min(4 meV, ω0) in every fitting path; prior recipe 7) at `9b44a605`.
- Frontier at `4198351d` (main).
- Verified: the routine gate passed on each slice's landed commit; the slow check passed on S1's and, one commit before landing, on S2's. S3's slice-end linking run on a copy of the operator's 97 K experiment met design bound (b) and did not meet bound (a).

## Next
The operator decides how to proceed with the linking thresholds before S4 opens: S3's linking run, at the uncalibrated starting values of `link_threshold`, `tie_margin` and the tolerances, marks clean branches as changes (design bound (a), section 6) and reports many near ties and contradictions. The agent's recommendation is a short bounded tuning job against the two bounds on the experiment copy, returning with counts and a method change if bound (a) still fails.

## Where
- Job: j2eac6dfd06c3, handoff `956e0887e74d64f811bdb6d803bf2667219ff13d54f97ec931ebb145ae2b347c`, bound with `acs-job.bat bind --handoff 956e0887e74d64f811bdb6d803bf2667219ff13d54f97ec931ebb145ae2b347c --session <new> --host <claude|codex>`; S4 opens with `acs-job open --slice-of j2eac6dfd06c3 --slice S4`.
- Branch: none open (S1-S3 branches merged; `acs/j2eac6dfd06c3` is merged into main).
- Worktree: `C:\Users\AMM\Documents\Github\Science\.jobs\ISAR\inter-spectra-fitting` (S1's, clean).
- PR: none open.

## Verify before trusting
1. `git fetch --prune`, then `gh pr view 84 --json state`, `gh pr view 85 --json state`, `gh pr view 86 --json state`: all MERGED.
2. `git merge-base --is-ancestor 4198351d origin/main` (PR #87, which contains `ff9af597`).
3. `acs-job.bat show --job j2eac6dfd06c3` names stage build with S1-S3 ticked and S4 unopened.

## Decisions since the plan
- The operator's answers of 2026-11-01 are built in S2 and recorded as ruling IS28 in `docs/design/inter_spectra.md`: intensity is the phonon's intrinsic intensity (Bose factor and recorded analyzer efficiency removed), and the named removals (screens, promotions, lookahead, `full_search`, `full_search_max_count`, `stop_margin_nats`, three developer tools, the `width_cap_40` arm) are approved; the S4 removals are not yet built.

## Open items
- S4's plan refresh must know the link record changed in PR #87: `LinkResult.lone` (unit -> mode -> name) holds lone phonon names, allocated after the branches from the same `next_name` counter and renumbered by the writer with them; lone names feed no prediction, change, marker or tie. Link records saved before PR #87 have no `lone` and read as out of date until Link modes is run again.
- S4's plan text still says S4U5 waits on the efficiency answer; it is answered (S4's plan refresh picks it up, with the connection-text mismatches S3's refresh drafter named for `matching_intensity`, `intensity_scale` and the stage payload's scoping).
- Every stored fast pass is on an older prior recipe and is stale until re-run.
- Deferred to S4: `fits_in_use` duplicating the fit-in-use rule (S4U1), `LinkView.branches` members unfiltered (S4U4), the link item's repeated workspace reads (S4U1).
- The automatic linking switch, like the fast-pass switch, does not survive the experiment's pending-to-ready rebind (a follow-up job).

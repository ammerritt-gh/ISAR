# Handoff — inter-spectra mode linking

> **Status:** live
> **Authority:** the frontier of the inter-spectra mode-linking job (j2eac6dfd06c3), rewritten at every closeout

**End condition:** job j2eac6dfd06c3's slice S4 (the inter-spectra pass uses the links) has landed or been cancelled by the operator, and the job is closed.
**Board entry:** none. The repository has no `WIP.md` board; this file is linked from `docs/READING_GUIDE.md`, Door 1.

## Frontier
- Landed: PR #88, job jc50670152313 (T0, outside this job; the linking tuning the previous Next asked for): linking has its own q-link energy slope (`link_slope_meV_per_rlu` 3, `link_slope_meV_per_invA` 2.3; the refit's q-slope untouched), `width_tolerance_ln` 0.3, change markers at two sigma, each link matched as a whole (least total mismatch minus the threshold) before the grid-wide joining, and a contradiction only for a matched, mutual-best pair the joining refuses. On a copy of the operator's 302 K experiment: 93 of 139 modes settled (was 48), 6 near ties (was 44), 17 contradictions (was 34); `docs/design/inter_spectra.md` section 10. At `71ef8e04`.
- Landed: PR #87, job ja847b237965f (T0, outside this job; the link display cleanup): every mode of a linked spectrum is a phonon named P<n> on screen (stored names stay `B<n>`); a mode on no branch gets a lone phonon name from the same experiment counter, saved in the link record's new `lone` field; link notes, splits and markers are no longer shown on any view (they stay in the record); Dispersion shows only the analyzer path on an analyzer-layout experiment; Scans menu "Label modes in energy order". At `4198351d`.
- Landed: PR #86, slice S3 (showing the links: link read facade, branch names and link notes in the views, Analyzer array path gestures, the Dispersion view's Analyzer path) at `ff9af597`.
- Landed: PR #85, slice S2 (mode linking: the Qt-free linking core, the saved `link` record and queue kind, the Link modes button and the automatic switch) at `b171345d`.
- Landed: PR #84, slice S1 (the per-mode width ceiling min(4 meV, ω0) in every fitting path; prior recipe 7) at `9b44a605`.
- Frontier at `71ef8e04` (main).
- Verified: the routine gate passed on each slice's landed commit; the slow check passed on S1's and, one commit before landing, on S2's. S3's slice-end linking run on a copy of the operator's 97 K experiment met design bound (b) and did not meet bound (a). PR #88's routine gate passed at `71ef8e04` (7917 passed); its acceptance run was waived by the operator for the one flaky GUI test named in `TODO.md`.

## Next
S4 opens with its plan refresh, which must read PR #88's linking changes: the record's format is unchanged, and its ties, contradictions and markers now follow the new rules. Link records saved before PR #88 are not made out of date by the settings change; they show the old linking until Link modes runs again.

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
- Deferred from PR #88's review: the per-link matching can prefer two moderate pairs over one near-perfect pair by a hair (a-x 0.2 against a-y and b-x 4.5 each, threshold 9), and near-tie detection has no kind for one pair against two, so that choice is never reported as a near tie. Not a regression: the old greedy joining reported no tie for this shape either.
- PR #88's tolerances come from one temperature of one experiment; temperature links still use the refit's temperature tolerance, which no linking run has tested.
- S4's plan refresh must know the link record changed in PR #87: `LinkResult.lone` (unit -> mode -> name) holds lone phonon names, allocated after the branches from the same `next_name` counter and renumbered by the writer with them; lone names feed no prediction, change, marker or tie. Link records saved before PR #87 have no `lone` and read as out of date until Link modes is run again.
- S4's plan text still says S4U5 waits on the efficiency answer; it is answered (S4's plan refresh picks it up, with the connection-text mismatches S3's refresh drafter named for `matching_intensity`, `intensity_scale` and the stage payload's scoping).
- Every stored fast pass is on an older prior recipe and is stale until re-run.
- Deferred to S4: `fits_in_use` duplicating the fit-in-use rule (S4U1), `LinkView.branches` members unfiltered (S4U4), the link item's repeated workspace reads (S4U1).
- The automatic linking switch, like the fast-pass switch, does not survive the experiment's pending-to-ready rebind (a follow-up job).

# Handoff — catalogue-driven intake

> **Status:** live
> **Authority:** the frontier of the catalogue-driven intake workstream, rewritten at every closeout

**End condition:** job jc54d8ce88c0c lands all three slices (BL43 on the catalogue intake with its GUI;
S30, IN8 and TAVI onto it with the old card path removed; automatic re-read on a catalogue correction)
and is closed.
**Board entry:** none (this repository has no `WIP.md`)

## Frontier
- Landed: nothing on main yet; frontier at 3e8600c.
- Built, not accepted: S1U1P1 on branch `acs/jc54d8ce88c0c` (a29edc3, 9ceb873): catalogue entries,
  loader, per-file recognition, the BL43 entry with its two layouts. Its packet check passed (130
  tests), but the runtime marked it red because it touched 6 files outside the plan's `files` list.
- Verified: nothing on main.

## Next
No plan revision is needed any more. By operator ruling on 2026-10-23, the ACS packet `files` check is
advisory (Workflow 58ff2ff55b, PR #162): a packet whose check exits 0 is green, and files outside its
list are only shown to reviewers. Bind the handoff, re-claim S1U1P1 on the existing work
(`acs-job claim --seat executor --packet S1U1P1 --job jc54d8ce88c0c`) so its old red record is
rechecked, then launch the S1 slice coordinator with the packet `next` prints.

## Where
- Job: jc54d8ce88c0c (t3), handoff e05139875e70cbad0a5c5c549ba690463a95190409741314c47c1937544ced81,
  bound with `acs-job.bat bind --handoff e05139875e70cbad0a5c5c549ba690463a95190409741314c47c1937544ced81 --session <id> --host claude`
- Branch: `acs/jc54d8ce88c0c`; upstream ahead 2, behind 0
- Worktree: `C:\Users\AMM\Documents\Github\Science\.jobs\ISAR\unified-catalogue-driven`
- PR: none
- Design: `C:\Users\AMM\AppData\Local\ACS\state\v1\working\jc54d8ce88c0c\design.md` (approved,
  receipt rb9d4ae7ab74c); plan artifact `9ae741a0c6f2`; transcript
  `docs/transcripts/2026-10-22-unified-catalogue-based-data-intake-70651390.md`

## Verify before trusting
1. The branch is still open: `git fetch --prune`, then
   `gh pr view acs/jc54d8ce88c0c --json state`. Without `gh`,
   `git branch -r --merged origin/main` can prove merged, never open: an
   absence there is unknown, so ask the operator before resuming. If it
   merged since this was written, do not resume it: close the job
   (`acs-job.bat close --job jc54d8ce88c0c`) and continue from main.
2. `acs-job.bat next --job jc54d8ce88c0c` shows S1U1P1 red on 9ceb8732d059 for files outside its list.
3. `git log --oneline 507df82..acs/jc54d8ce88c0c` shows a29edc3 and 9ceb873.

## Decisions since the plan
- Plan amendment 1 (orchestrator, made while the operator was Away): for S2U2, a TAVI scan with no
  temperature in its payload takes it from the drive's recorded request for that scan, labelled with
  that source, instead of the card template's 300 K default; only when neither exists is it a listed
  gap. The undo is to drop the amendment.

## Open items
- **The BL43 group-export format.** It is a catalogue edit only if the preprocessing script writes
  per-analyzer files in one of the two existing layouts (CsV3Sb5 or Ba122). A combined multi-analyzer
  file needs reader code before the BL43 experiment the week of 2026-10-26.
- **Hard rule 7 wording, for the operator to check:** "Operator input is a third channel: the GUI
  appends the user's values and conditions to `operator_input.jsonl` and records intake settings in
  `intake.settings.json` through `isar/intake/`, never through `workspace_api`."
- **Whether the `_lr` column-family choice belongs in the BL43 catalogue entry**, as S1U1 placed it,
  or is an analysis choice. S1U1's unit review is to decide.
- **Coordination with job j313eac923389** (BL43 fitting, session isar-92): this job's BL43 slice lands
  first. That job adds a `label` entry kind to the operator-input record this job builds.

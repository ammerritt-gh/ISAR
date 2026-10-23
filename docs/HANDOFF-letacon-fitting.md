# Handoff — le Tacon BL43LXU Layer I fitting

> **Status:** live
> **Authority:** the frontier of the le Tacon multi-analyzer fitting workstream, rewritten at every closeout

**End condition:** job j313eac923389's slices are landed or cancelled, and the operator has replanned the paused ones.
**Board entry:** none. The repository has no `WIP.md` board; this file is linked from `docs/READING_GUIDE.md`, Door 1.

## Frontier
- Landed: nothing on `main` from this job. Slice S1 (open-ended peak count on a two-rung evidence ladder, fit-core speed-up, mode-per-row `modes.csv`, background-free and other degenerate twins) is complete on branch `acs/j313eac923389` at `047a6a7`, PR #46, held unlanded by operator ruling (job amendment 12).
- Verified at `047a6a7`:
  - The routine suite (`python -m pytest -m "not slow" -n 16 --dist worksteal`) gave 4899 passed, 11 skipped, 1 xfailed.
  - The full parallel suite gave 1 failed, 4979 passed, 12 skipped, 3 xfailed in 1277 s (measured 2026-10-23). The failure is `tests/drive/test_fcc_battery.py::test_full_range_discovery_finds_and_maps_both_phonons`: its closed-loop wall ceiling is hit (698 s serially alone). The same test passes on `main` in 118 s and on the branch at `bf33ef5` in 116 s.
- Cause, bisected to commit `1f58dfe` (a narrow mode keeps its area on the grid):
  1. Narrow modes now go resolution-limited instead of dropping to zero amplitude.
  2. That spawns negligible but evidence-bearing delta-twin candidate rows.
  3. The scene's `presence` factor has no score bound (`isar/scene/factors.py:524-533`), so margin pruning is off.
  4. The cluster therefore passes the 4096-alternative cap after 5 scans. Planning cost grows about quadratically, and every ConfirmPresence answer is withdrawn.

## Next
The replan's first item, ruled by the operator: give the scene `presence` factor a bound that depends on cluster size, so margin pruning removes negligible alternatives honestly. Then re-run the fcc test on PR #46's branch. Before that, check whether `isar run` on the 56 le Tacon spectra hits the same scene blow-up; that decides how urgent the bound is for the beamtime.

## Where
- Job: j313eac923389 (t3), handoff c8162beb6039, bound with `acs-job.bat bind --handoff c8162beb6039`
- Branch: `acs/j313eac923389`; upstream ahead 67, behind 6 of origin/main
- Worktree: `C:\Users\AMM\Documents\Github\Science\.jobs\ISAR\automated-multi-analyzer`; unit lane `...\automated-multi-analyzer-S1U2` (clean)
- PR: #46, open
- Approved design: `C:\Users\AMM\AppData\Local\ACS\state\v1\working\j313eac923389\design.md`; transcript `docs/transcripts/2026-10-22-le-tacon-dataset-isar-fitting-328afc0b.md`

## Verify before trusting
1. The branch is still open: `git fetch --prune`, then `gh pr view 46 --json state`. If it merged since this was written, close the job (`acs-job.bat close --job j313eac923389`) and continue from main.
2. The failure still reproduces: in the worktree, `python -m pytest "tests/drive/test_fcc_battery.py::test_full_range_discovery_finds_and_maps_both_phonons" -q`. Expect a wall-ceiling failure at `047a6a7`, and a pass on main.

## Decisions since the plan
- **Hold S1 and bound the scene first** (2026-10-23).
  - Question, as put: land PR #46 despite the campaign regression, or hold it while the scene presence factor gets a bound.
  - Ruling: "(d) then (a) sounds good."
  - Ruled by: operator.
  - Constrains: PR #46 stays unlanded until the presence bound exists; the replan starts there.
- **Background railed at zero resolves to a background-free twin** (2026-10-23). Ruling: "This is important to handle, because at BL43 for the experiment next week the background is really close to 0 (or basically 0). So 2->3, again like the others where a degenerate parameter drops that element (0 background -> backgroundless model)". Ruled by: operator. Built in S1.
- **Keep the plan as a draft and replan later** (2026-10-23). Ruling: "So we keep the plan as it stands right now, and we'll look at modifying it with new information later." Ruled by: operator. Slices S2–S6 stay paused.

## Open items
- FIT-21: a railed Ω_d with a broad Γ resolves to a sharp elastic line. This needs an operator ruling on the twin (elastic, quasi-elastic or none) and on the Γ condition, since ω₀ = √(Ω_d² + Γ²).
- Method findings, reported and not tuned:
  - FIT-9: rung 1 undercounts some spectra with no trigger firing.
  - FIT-8: rung-2 nested sampling at 128 live points is 31–54 nats off at 11–14 parameters.
  - FIT-22: the boundary-mass correction gives +17 to +62 nats near the Γ floor.
- FIT-4: the amplitude-prior sensitivity check on real le Tacon data has not run (slice S6). Beamtime risk: the prior assumes `norm_i` is in raw-count units.
- FIT-20: campaign rung-2 budget is 120 s per pending spectrum with no refresh-wide cap.
- The A/B benchmark criterion as designed is unreachable by construction (94% classifier success on a well-counted case). Redefine it as a comparison against a fit made with the true peak count on the same data.
- The benchmark figures measured before S1's last repair are stale.
- The job goal's clause "operator-declared excitation hypotheses" is outside the approved design (ruling R3) and is not to be reported as delivered.

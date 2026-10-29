# Handoff — le Tacon BL43LXU fitting

> **Status:** live
> **Authority:** the frontier of the le Tacon multi-analyzer fitting workstream, rewritten at every closeout

**End condition:** job j313eac923389's paused slices (S2 overview and inspector, S3 export, S4 labels and tracking, S5 HTML report, S6 real-data checks) are landed or cancelled after the operator replans them, and the pinned fitting follow-ups below are opened or dropped.
**Board entry:** none. The repository has no `WIP.md` board; this file is linked from `docs/READING_GUIDE.md`, Door 1.

## Frontier
- Landed: PR #66 (job jfa578b93ec71, closed): a phenomenological quasi-elastic central component (QE) plus an always-present elastic line in every BL43LXU model, fast pass, deep pass and campaign path; QE width prior flat from 1 to 2 resolution FWHM, then 1 nat per FWHM; BL43LXU-only currency id `isar.central-qe-recipe/1`. a06 (slit 40x40): the elastic line carries the central peak (h 137), the QE takes the extra width, no phonon below 1/2 FWHM. Merged at `ccd3aa0`.
- Landed: PR #61 (job j68178d5e3fbc, closed): a linear near-zero log-prior penalty on DHO mode energies (0 at 1 FWHM, 5 nats at 1/2, ceiling 7.5 at 1/4), prior recipe /6.
- Landed: PR #49 (job j6420291926ab slice S2, deep pass: calibrated dynesty engine, staged coverage, profile and bootstrap batch option, one calibration pass) at `048ddc5`.
- Landed: PR #48 (job j6420291926ab slice S1: full DHO lineshape of the paper's Eq. 2 in the height coordinate, prior recipe v5 with data-derived amplitude priors, the fast pass with delta-mode twins wired into `isar run`, the count benchmark, the le Tacon proof) at `0b59dca`; it carries PR #46 (job j313eac923389's S1, the open-ended count ladder) by operator ruling, with the recorded fcc campaign-test regression.
- Frontier at `ccd3aa0` (main).
- Verified: the full suite `python -m pytest -n 16 --dist worksteal -o timeout=1500` passed on `048ddc5` (5128 passed, 13 skipped, 2 xfailed, 472 s, measured by the S2 coordinator); the le Tacon proof ran once (3 passed, 1 failed on the a06 slit, since fixed by declaring `slit = "40x40"`, not rerun).

## Next
The operator runs his own ChatGPT review of PR #48 and PR #49; each finding becomes a follow-up job (`acs-job open --follow-up j6420291926ab` for a fix inside the approved design, a new job otherwise).

## Where
- Job: j313eac923389 (t3), stage land, its S1 landed inside PR #48; slices S2–S6 paused for replanning. Job j6420291926ab is closed.
- Branch: `acs/j313eac923389` merged into main; nothing unlanded.
- Worktree: `C:\Users\AMM\Documents\Github\Science\.jobs\ISAR\automated-multi-analyzer` (and `...-S1U2`), clean, tips in main; kept for the operator's word on closing job j313eac923389.
- PR: #46, #48 and #49 merged.

## Verify before trusting
1. `git fetch --prune`, then `gh pr view 46 --json state`, `gh pr view 48 --json state`, `gh pr view 49 --json state`: all MERGED.
2. `git log --oneline -1 origin/main` contains `048ddc5`.
3. `acs-job.bat show --job j313eac923389` names its stage; if the operator closed it, the paused slices are cancelled and this file's end condition reads only the follow-ups.

## Decisions since the plan
none

## Open items
- Next, by the operator's order (2026-10-29): deep-pass speed work, starting with the cost breakdown and a promotion replay on saved records (`docs/ChatGPT_DeepPass_Handoff.md` was the brief; the operator has since removed the root handoffs); each BL43LXU model now carries two more parameters (QE height and width).
- After PR #66, not yet investigated: the le Tacon 97 K batch's outcomes moved (reject 14 to 10, resolved 14 to 18, measured before the 1-FWHM amendment) and its timing was not re-run after the amendment (a06 fast pass 79 s against 73 s).
- Accepted known limit (operator, 2026-10-29): with an elastic line, a QE and a low phonon all present, the fast pass widens the phonon instead of adding a QE (synthetic case 4, a strict xfail).
- Deferred from PR #66: the fast pass's Laplace step underprices a QE whose width sits in the rounded knee at 2 FWHM by about 1-2 nats (deep pass exact); the campaign's predictive artifact uses a Gaussian resolution for the QE as for phonons; a phonon-less BL43LXU winner reads "no signal", never "elastic only".
- Pooled masters do not go stale on a prior change (`isar/session/master_delivery.py:364`); STATS_PRIMER names the gap.
- ISAR inferred slit 20x20 for the CsV3Sb5 experiment where the beamtime used 40x40 (`isar/calibrate/tabulated.py` `choose_setting`); the experiment needs the slit declared in the GUI.
- Pinned follow-ups (operator rulings, 2026-10-26): deep-pass warm start; a cheaper model curve (precomputed per-analyzer convolution matrix, batched evaluation, numba on the hot path); prior tuning (count decision stable under alternative priors on 6 of 56 le Tacon spectra); a method change for deep-pass accuracy (seed spreads 10–18 nats at 5–6 modes; every deep count is labelled untrusted).
- Fast pass misses two aims (reported, not chased): median ~80–90 s per spectrum against the 60 s aim; false flags on clean spectra 7.1% against 5%; the hidden-component flag caught 13 of 71 planted cases.
- The a06 slit (`40x40`) and the confident-peak anchor (back to 50) are config changes not yet run on real data.
- About 25 lesser findings deferred, listed as known issues in the bodies of PR #48 and PR #49.
- No ChatGPT review ran on job j6420291926ab (requests too large; fix is Workflow job j01326cc9bce3).
- The fcc campaign-test regression from PR #46 stays open until the scene `presence` bound exists.
- For the replan of job j313eac923389: its S3 export slice was built by job ja0cd202ecb67 (PR #52): `isar export` writes both passes, `modes.csv` with `pass` and `trust` columns; there is no rule picking one pass as "the" result (operator, 2026-10-27: "give both fast and deep results, and just label them clearly"), so its display reads `FitView.fast_pass` / `deep_pass` itself. The centre fit page's metadata panel moved to the left-side scan inspector.

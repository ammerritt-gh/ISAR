# HANDOFF — the live EIG demo (week of 2026-09-07)

> **Status:** live
> **Owner:** Adrian
> **Purpose:** the run-sheet for showing ISAR choose its own scans in front of a room, and the
> evidence that the shown behaviour is real. Delete after the talk.

## 1. What is shown

One sentence, typed into the Drive dock:

```
map the acoustic phonon from (2,0,0) to (3,0,0)
```

The compiler turns it into a targeted campaign: 21 grid points at 0.05 r.l.u., acoustic branch as
the target with the optic branch as a rival to be identified and then ignored, a **±0.1 meV** bar,
an 18 meV search window. No information about the phonon itself enters: the hypotheses are the
shipped class presets (an acoustic branch goes to zero at the zone centre with a zero-mean slope
prior; an optic branch has a finite energy there), which the design goals name as the legitimate
starting point.

What the room sees, in order:

1. **A scan within seconds.** The planner ranks every `(Q, window, exposure)` the class priors
   generate and submits the best one. Nothing surveys the line first.
2. **Wide because the belief is wide.** The first windows run to ~100 points, because a cold
   acoustic belief far from the fold predicts "somewhere in the band". That is the prior showing,
   not a survey rule.
3. **Narrow as the map firms up.** The first fits anchor the belief; by iteration 4 the windows are
   15–18 points, centred on the predicted energy.
4. **More exposure where the branches approach.** The exposure ladder climbs from 5M to 160M
   neutrons per point toward the zone boundary, where the acoustic lobe is dimmest and the optic
   sits closest — an EIG outcome, not a rule, because a scan that cannot tell the two branches
   apart carries less information about the prize.
5. **Certification and stop.** Each q certifies a pooled ω₀ interval at or inside the bar; the
   planner's prize is met and the campaign stops `met`.

The rule an expert will ask for, in one sentence: *every candidate scan is scored on the expected
number of grid points it would bring inside the bar, per neutron, by simulating its counts under
the current hypothesis map and the measured intensity map — and the best one is taken.*
(`design/eig.md`.)

**The live full path, 2026-09-03:** 82 scans, 1.45e11 neutrons, 24.9 minutes, terminal `met` on
certified intervals, 20/20 prize points. `isar score --truth-fn phonon_dft` on it: median |Δω₀|
0.028 meV, inside the bar. Be ready for the honest caveat if a statistician asks — the published
intervals under-cover by about 1.9× (coverage 0.444 against a nominal 0.68), which is a fitter
σ-calibration matter and not a placement one; it makes the planner stop slightly early.

## 2. How to run it

- Start TAVI (`..\TAVI\run-tavi-dev.bat`), Remote API dock set to **Allow control**.
- Start ISAR with `run-isar-gui.bat`, as always. The evidence sampler's escalation rung now runs
  at 32 live points by default (128 before 2026-09-03): about one minute per fit instead of
  five, same structures selected, same points certified on the reference campaign. The
  rationale is in `STATS_PRIMER.md` §Evidence if anyone asks.
- In the Drive dock: paste the sentence, set **analysis limit to `none`** (the compiler freezes
  one hour, and fifty fits at a minute each would brush it). Start.
- Stop is cooperative: the GUI's stop writes a control file and the loop finishes the current
  iteration and exits with the operator-stopped state, within one iteration.
- Rehearse once in the GUI yourself before the day. Every rehearsal below ran the same program
  through the CLI, not the dock.

## 3. Measured on 2026-09-02 (code `0b02b98`)

> **These rehearsals predate the EIG planner.** Everything below that names a survey pass, a
> centred "core", an O36 eligibility gate or a scan-window decision describes the machinery retired
> on 2026-09-02 (`design/eig.md` §8). It is kept as the timing and accuracy evidence it is; the
> current behaviour is §1 and the 2026-09-03 live run.

Short reference program, (2,0,0) to (2.3,0,0), three measurable points:

| Run | Sampler | Outcome | Wall | Scans | Cores granted after the survey pass |
|---|---|---|---|---|---|
| old default | 128 live points | stopped by hand after 2 fits | 11 min | 2 | none reachable |
| new default | 32 | met, 3 of 3 certified | 7.5 min | 8 | 0 of 4 (veto, see §4) |
| new default, vetoes fixed | 32 | met, 3 of 3 certified | 12.6 min | 9 | 3 of 3 scored, all 17-point cores |
| final code, no overrides (`be8b34d`) | 32 | met, 3 of 3 certified | 10.0 min | 8 | 2 cores; zero recovery re-scans |
| fit speed-ups (`4187374`) | 32 | met, 3 of 3 certified | 5.5 min | 8 | 2 cores; fits 7 to 28 s, mean 20 s; first fit done 29 s after go |

Delivered against TAVI's analytic truth: 0.926, 1.838, 2.650 meV at h = 2.1, 2.2, 2.3, each
within 0.04 meV. Centres are accurate to a median 0.025 meV over the scored rows.

Full demo path, (2,0,0) to (3,0,0), rehearsal F on `0b02b98`, sampler capped:

| Quantity | Value |
|---|---|
| Survey pass across 20 measurable q | 20 scans, 33 min, windows shrinking 153 to 78 points as the prediction tightened |
| Narrowing phase | 31 further scans, 13 of them 17-point cores; every decision that was scored granted the core (13 of 13) |
| Decisions not scored | 11 bootstrap-mass, 3 non-canonical action, 4 other gates |
| Points certified | 20 of 20 at 88 min; the 90-minute rehearsal wall limit cut the certification pass and `--resume-latest` finished it, stop state "met" |
| Accuracy against truth | median 0.033 meV; 68 percent bars cover about a fifth of the scored rows |
| Delivered curve | 0.47 meV at h=2.05 rising to 6.05 meV at the zone boundary, 20 certified points |
| Per-scan cadence | about 90 s per scan including its fit on that run; about 25 s after the 2026-09-03 fit speed-ups (short path), full path not yet re-measured |

For a talk: start it at the beginning and it will be mid-narrowing by the time you reach the
slide, with the survey pass and the first cores visible in the log; the completed run is the
deck. A full campaign is an hour and a half at this cadence; the compiled one-hour budget is
counting seconds and is nowhere near touched (a few seconds of virtual beam).

## 4. What had to change to make it true

- The O36 eligibility gate refused a centred core whenever the fit's AIC-ranked shadow selection
  disagreed with the delivered selection. Since SCENE-1 (ruling H4) the delivered selection is
  posterior-driven and AIC is a diagnostic, so they disagreed by design and no core could ever
  win. Retired in `9fa11d4`; the record is still persisted.
- An executed core scan failed provenance the next time its q was scored, because the inventory's
  selected candidate is the survey by construction. Fixed in `0b02b98`.
- Two expected refusals printed tracebacks on the console (`fa87059`).
- The missed-window recovery fired on scans whose peak was already inside the window: an
  "unmodeled" fit that had the phonon to the milli-eV but no second structure for the optic, or a
  quicklook that saw the phonon and called no signal at that exposure. It widened to 200 points
  and bought a scan that could not help, eleven times in rehearsal F, 28 percent of all points.
  An unmodeled row is no longer a miss, and a peak inside the window no longer re-centres.
- The evidence sampler's escalation rung now defaults to 32 live points (`02cc2cc`).
- A scan was invisible in the GUI until its fit finished, because the ledger was saved only at
  the end of the run; it is now registered as ingested and saved the moment its data lands
  (`a645e93`). The feed names an alternative window being weighed as such, and an expected
  low-severity event as a note, not a problem (`f15c654`).
- One quicklook's fit spent 136 of 140 seconds in nested sampling, four structures escalated
  one after another and then again for the single-member master. The resolution kernel is now
  cached per fit (`3d63cdc`), a first-visit master takes its scan's winner as a hint
  (`9593236`), and the escalations run concurrently through the session pool at every tier
  (`4187374`). The same quicklook went from 85 seconds to 23.

## 5. Honest caveats, in case they come up

- **Interval coverage.** Scored against truth, the certified 68 percent bars undercover: the
  scorer suggests inflating them by about 1.8. Centres are right; the bars are optimistic. The
  σ-calibration artifact is the mechanism that fixes this and it has not been re-run since the
  fit-selection change.
- **Not every decision grants a core.** The "insufficient bootstrap mass" check sends a revisit
  to the survey when the master's predictive rests on cheap- or fast-tier fits with no bootstrap
  behind them, and a non-canonical target action (a confirm or bridge) is never scored. Both
  gates behaved the same way in August. On the full path about two in seven post-pass decisions
  were scored, and every scored one granted the core.
- **The first pass is always the survey**, structurally. Around a third of the campaign's scans
  are the survey pass on a 21-point line.
- **Discovery is not demoed.** The scene-EIG discovery lane takes tens of minutes per decision
  at full scene size and its first placement was shown to be seed noise; that is the repair
  program's slice 5, not this week's work.

## 6. Backup deck

Completed workspaces under `test_workspaces/demo-rehearsal/` (gitignored): `D-veto-fixed`
(short path, met), `F-final` (full path). For the truth overlay and the diagnostic:

```
isar score --workspace <ws> --truth-fn phonon_dft --json <ws>/score.json
isar diagnose --workspace <ws>
```

`report/figures/campaign_dispersion_truth.png` is the dispersion against the analytic curve;
`report/report.html` is the full report; the `eig_plan` events in `report/session_log.jsonl` say,
per iteration, which design won and what it beat
was not narrowed.

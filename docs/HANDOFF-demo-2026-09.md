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
the target with the optic branch as a rival to be identified and then ignored, 0.5 meV tolerance,
an 18 meV search window, scan-window EIG authoritative. No information about the phonon itself
enters: the hypotheses are the shipped class presets (an acoustic branch goes to zero at the zone
centre; an optic branch has a finite energy there), which the design goals name as the legitimate
starting point.

What the room sees, in order:

1. **Survey pass.** Every unmeasured q gets the full search window, because an unmeasured q has
   no prediction to centre a core on. The scans march outward from (2.05,0,0) along the line.
2. **Identity.** The tracker establishes the acoustic branch against the optic rival and
   connects the frontier point by point.
3. **Centred cores.** Once a q has a confirmed prediction, the window EIG compares the survey
   against a 17-point core centred on the predicted energy, on information per neutron, and
   takes the winner. Ties go to the cheaper scan. From that point most scans are 17 points
   instead of 120 to 150.
4. **Certification and stop.** Each q certifies a pooled ω₀ interval at or below tolerance; the
   campaign stops "met".

The rule an expert will ask for, in one sentence: *two candidate windows are scored on expected
information per neutron about the excitation energy; the winner is taken, and a statistical tie
is broken by cost.* (O41/O43 in `design/eig.md`.)

## 2. How to run it

- Start TAVI (`..\TAVI\run-tavi-dev.bat`), Remote API dock set to **Allow control**.
- Start ISAR with `run-isar-gui.bat`, as always. The evidence sampler's escalation rung now runs
  at 32 live points by default (128 before 2026-09-03): about one minute per fit instead of
  five, same structures selected, same points certified on the reference campaign. The
  rationale is in `STATS_PRIMER.md` §Evidence if anyone asks.
- In the Drive dock: paste the sentence, set **analysis limit to `none`** (the compiler freezes
  one hour, and fifty fits at a minute each would brush it), leave scan-window EIG at its
  default. Start.
- Stop is cooperative: the GUI's stop writes a control file and the loop finishes the current
  iteration and exits with the operator-stopped state, within one iteration.
- Rehearse once in the GUI yourself before the day. Every rehearsal below ran the same program
  through the CLI, not the dock.

## 3. Measured on 2026-09-02 (code `0b02b98`)

Short reference program, (2,0,0) to (2.3,0,0), three measurable points:

| Run | Sampler | Outcome | Wall | Scans | Cores granted after the survey pass |
|---|---|---|---|---|---|
| old default | 128 live points | stopped by hand after 2 fits | 11 min | 2 | none reachable |
| new default | 32 | met, 3 of 3 certified | 7.5 min | 8 | 0 of 4 (veto, see §4) |
| new default, vetoes fixed | 32 | met, 3 of 3 certified | 12.6 min | 9 | 3 of 3 scored, all 17-point cores |

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
| Per-scan cadence | about 90 s per scan including its fit |

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
isar diagnose --workspace <ws> --eig
```

`report/figures/campaign_dispersion_truth.png` is the dispersion against the analytic curve;
`report/report.html` is the full report; the `--eig` table says, per q, why the window was or
was not narrowed.

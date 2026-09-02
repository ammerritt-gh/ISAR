# HANDOFF — EIG, the one objective

> **Status:** live
> **Owner:** Adrian
> **Purpose:** the single objective for the next session and every session until it is met, the
> owner's definition of it in his own words, an honest map of what the code does instead today,
> and the measured facts of 2026-09-02/03 so nothing is re-derived. Supersedes the "what next"
> of every other handoff; delete when the objective is met.

## 1. The objective, verbatim

Owner, 2026-09-03:

> EIG is: we have a hypothesis map and an intensity map, we select scans to help prove
> hypotheses according to EIG based on the intensity map. No fallbacks, no legacy, no survey,
> none of this patches or repairs or bullshit lying around.

> I say "acoustic phonon", our hypothesis map has weak priors, it does a scan and that really
> starts firming things up, and it should take it from there. Why is there even a survey option?

The concrete demonstration (owner, 2026-09-03, for the talk the week of 2026-09-07):

> I want to say "map the acoustic phonon from (2,0,0) to (3,0,0)" and have it measure the
> dispersion in an efficient manner. This means finding it and focusing scans on the actual
> phonon while rejecting the optic phonon. And it can't have prior information about the
> acoustic phonon.

What the room must see, in order:

1. Enter the sentence, hit go. A scan is submitted within seconds. If one does not appear, that
   is a failure.
2. Early scans are wide only because the hypothesis is wide; the first fit firms the map up.
3. Later scans are focused on the phonon itself.
4. Where two phonons interfere, the scan gets more time to differentiate them — as an EIG
   outcome, not a rule.
5. The campaign completes in a reasonable time.

And the standing rule for the GUI (owner, 2026-09-03): the GUI is how a human sees the inner
workings; it never does anything different from ISAR. A scan is shown the moment it lands; its
fit is drawn over it when the fit completes.

The design goals already say this (`DESIGN_GOALS.md`, Inbox): "the hypothesis map (our
scientific model) → the scientific question → the measured S(Q,ω) → fits derived on top";
"priors exist to be weakened … scan placement, window content, and when to stop measuring all
price out of that strength gradient against cost via EIG. Behaviour falls out of priors and
prices; it is never legislated per phenomenon"; "a first-look survey is not a uniform grid …
it looks where the hypothesis says the phenomenon is findable, and each fix feeds extrapolation
to the next placement."

## 2. What the code does instead, honestly

Two engines exist and neither is the objective end to end.

**The targeted lane (what the demo runs).** `isar drive --goal "map the acoustic phonon …"`
compiles to a *targeted* step and runs the tracked-branch tracker (`isar/drive/acquisition/
acquire.py`, `isar/session/branch_tracking.py`; `design/steering.md`). Its placement rules are
legislated: "establish identity at the resolvable anchor", "extend the connected target frontier
by one adjacent q", "revisit the ambiguous anchor", same-q production ceilings. Its energy window
on every first visit to a q is the full search band because of one line:

```
survey = not bool(getattr(pick, "best_resolved", False))   # acquire.py:2169
```

with the comment "survey the band until this q has a confirmed resolved fit". The model's own
prediction for that q is recorded alongside and is excellent (rehearsal F, 2026-09-02: predicted
1.34 / 1.91 / 2.31 / 2.75 / 3.11 / 3.54 meV at h = 2.15 … 2.40, measured within 0.05 meV, with
prediction sigmas of 0.01–0.05 meV) and is ignored for the window. The window arithmetic is
`physical_windows._energy_window` (`survey` → the full `energy_search_meV` band), the
`legacy_uniform/1` candidate family, which a `ScanDesignInventory` must keep as its selected
control by construction (`scan_design.py:577-610`).

The scan-window EIG bolted beside it (`design/eig.md`, O36/O41/O43) compares that survey against
a centred core on information per neutron — but only at a q that already has its own confirmed
fit (O36 "authorizes only a clean, confirmed target at an already-selected scientific action and
Q"). So on a 21-point line the first 20 scans are surveys by construction; the core wins only on
revisits. Rehearsal F: 20 survey scans, then 13 cores in the next 31.

Recovery machinery sits under all of it: missed-window recentre-and-widen (`supervisor/
exceptions.py`), decline widening, feasibility trims, "shadow" names surviving on the
authoritative path, exception events for expected refusals.

**The discovery lane (SCENE).** `discover_and_map` builds a scene (phenomenon belief over the
catalogue, `isar/scene/`) and selects by mutual information (SCENE-5, `scene/eig.py`). That is
the shape of the objective, but the audit of 2026-09-01/02 (`HANDOFF-campaign-pipeline-audit.md`)
measured it: 40–80 minutes per decision at full scene size (F18), first placement ordered by
Monte-Carlo seed noise over byte-identical predictions (F24), a 4.2× more expensive window
preferred at identical information (F23), steering from posterior mass the scene itself marks
unauthoritative (F16), declared hypotheses unreachable from real fits (F08), a frozen
deliverable with no completion semantics (F30), and its own cold-start "opening book" survey.
Its one live run needed six hours for one resolved point.

Neither lane starts from "acoustic phonon, weak class prior, first scan where the prior says it
is findable, then EIG from the intensity map". The targeted lane legislates placement and
surveys; the discovery lane scores by EIG but over a belief representation that does not yet
work at campaign scale and still opens with a survey.

## 3. What EIG means here, in the repo's own terms

- **Hypothesis map:** the catalogue class prior for the declared phenomenon. For "acoustic
  phonon": ω → 0 at the zone centre, linear nearby with a wide slope prior, weakening with
  distance from the fold; the optic branch as a rival with a finite zone-centre energy
  (`isar/analyze/catalogue.py`, the shipped presets, SCENE-5's phenomenon belief layer). "Can't
  have prior information about the acoustic phonon" means class priors only, never the truth
  curve or a calibration derived from it.
- **Intensity map:** the counts-authoritative measurement arena record (`report/
  measurements.jsonl`, `isar.measurement-arena/1`) — every scan's counts, before any fit. Fits
  are the human-readable layer, not the proof layer (`DESIGN_GOALS.md`).
- **EIG:** for each candidate action (Q, energy window, exposure), the expected information the
  measured intensity would give about the hypothesis map, priced per neutron; take the best. The
  first scan is chosen from the prior alone; every later one from the map updated by the
  intensity so far. A wide window is what EIG chooses when the prior is wide; a narrow one when
  it is not. "Interference" (two modes in one window) is where EIG spends exposure because that
  is where information is.
- **Stop:** the four reasons only (`DESIGN_GOALS.md`): the goal met (hypothesis map at the
  requested precision), user, budget, machine failure.

The pieces that already do their job and stay: the deterministic fitter and evidence selection
(Baker–Cousins, structure enumeration, per-structure evidence), the measurement arena record,
the catalogue and presets, SCENE-5's predictive and MI scoring engine (with the audit's
corrections owed: exact ties by rate hash, common random numbers, cost in the ranking,
authority-gated belief), the TAVI adapter and request validation, the consultation channel, the
GUI as a window on the workspace.

## 4. Measured this week, so it is not re-measured

All on TAVI, deterministic tier, acoustic reference program (2,0,0)→(2.3,0,0) unless stated.
Commits are on `main`.

| Fact | Value | Where |
|---|---|---|
| First scan after go | 0.3–0.6 s, every run | rehearsal logs |
| One quicklook's fit, 2026-09-03 morning | 85 s; 136 of 140 s in nested sampling, 8 dynesty runs (4 per fit, scan fit + single-member master refit), 268k likelihood evaluations | cProfile, `HANDOFF-demo-2026-09.md` §4 |
| Same quicklook after the day's work | 23 s (scan 11.6, master 10.8) | `4187374` |
| Sampler default | 32 live points (was 128; 5 min/fit) | `02cc2cc`, `STATS_PRIMER.md` |
| Convolution | kernel and elastic profile cached, np.interp; 2.5× per evaluation, bit-identical | `3d63cdc` |
| Escalations | concurrent through the session pool at every tier; penalised fits deferred, prune- and budget-exempt | `4187374` |
| Pruning margin | 30 nats (was 100); no measurable effect, these escalations are Laplace-invalid, not dominated | `5c5515d` |
| O36 veto retired | AIC shadow vs H4 posterior selection disagreed by design; 3 of 4 core decisions were vetoed by it | `9fa11d4` |
| Core-scan provenance | an executed core failed authentication as a member source | `0b02b98` |
| Missed-window recovery | fired 11× on peaks already inside the window, 28 % of a run's points | `be8b34d` |
| GUI | scan registered as ingested and session saved before the fit; feed labels honest | `a645e93`, `f15c654` |
| Short path, final code | met, 3/3 certified, 5.5 min, 8 scans, fits 7–28 s | rehearsal I |
| Full path, before the speed-ups | met, 20/20 certified, 51 first-attempt scans, 90 min, 13 cores after a 20-scan survey pass; median 0.033 meV from truth; 68 % bars cover ~22 % | rehearsal F, `test_workspaces/demo-rehearsal/F-final` |
| Interval coverage | undercovers by ~1.8×; σ-calibration not re-run since the selection rule changed | `isar score --truth-fn phonon_dft` |

Workspaces: `test_workspaces/demo-rehearsal/` (gitignored). The GUI's own campaign workspaces
live under `%LOCALAPPDATA%\isar_gui\temp_workspaces\`.

## 5. Operating rules the owner set this week

- The GUI launches exactly what the CLI launches and shows exactly what ISAR does; no special
  launchers, no staged paths, nothing the room has to be told about.
- Only the four reasons end a campaign; everything else routes around a blocker.
- Planner time is analysis time, one budget.
- Stop asking the owner engineering questions; the intent is in `DESIGN_GOALS.md` and this file.
  Decide, record the decision with its reason, move.
- No patches on top of the legacy machinery to make it look like EIG. The objective is the
  objective.

## 6. For the fresh session

Start from §1. Then decide, in order, and write each decision down where it lands:

1. Which belief representation carries the hypothesis map for the targeted objective — the
   scene's phenomenon belief with class priors, or a leaner ω(q) posterior per declared branch.
   It must be cheap enough to score many candidates per decision in seconds.
2. How one EIG scores (Q, window, exposure) jointly and per neutron, with exact ties and no
   seed-ordered noise (audit F23/F24 remedies), and how "two modes interfere" becomes a place
   EIG spends exposure rather than a rule.
3. The first scan from priors alone, and the update from the intensity map after each scan.
4. What replaces every legislated placement rule and every survey: the tracker's frontier
   rules, `survey`, `legacy_uniform/1` as the selected control, O36's same-q requirement,
   missed-window recovery, the discovery opening book. They are deleted, not routed around.
5. The stop: goal met when the hypothesis map reaches the requested precision along the path.

Acceptance is §1's five points, live, through the GUI, on "map the acoustic phonon from
(2,0,0) to (3,0,0)", with the optic rejected and no information about the acoustic phonon
beyond its class prior.

The campaign-authorization repair plan (`plans/campaign-authorization-repair.md`) and the demo
run-sheet (`HANDOFF-demo-2026-09.md`) are evidence and measurements; they are not the work.

## 7. Decisions of 2026-09-02 (owner-ruled in conversation; recorded here until the design docs absorb them)

1. **The prize.** "Map the <branch> from A to B" asks: the energy of that branch at every grid
   point of the path, to within the default bar, **±0.05 meV** (owner: "the centre of a peak is
   trivial to fix to that detail"; the compiler's default and example now say 0.05). Every grid
   point must carry its own measurement — interpolating between neighbours does not answer the
   question a paper would ask. The one exemption is the zone centre of a gapless target, whose
   energy the hypothesis fixes at 0 and the elastic line makes untestable.
2. **The price.** Neutrons requested (points × count per point). No per-scan overhead constant.
3. **EIG.** For every candidate scan inside the region and the energy domain, the expected progress
   toward the prize per neutron, judged by simulating the intensity under the current hypotheses
   and re-running the same belief update the real data goes through. Greedy, re-decided after
   every scan. Progress is measured as entropy of each grid point's target-energy posterior above
   the bar; a point within the bar is worth nothing more. Both branches are always in the forward
   model whichever is the target; a scan whose peak is ambiguous between branches is priced as less
   information, which is what buys exposure where the branches approach and buys optic
   measurements only as far as excluding it needs.
4. **The hypothesis map** stays the per-branch belief with its class prior entering as parameter
   precision (O8), persisted in `card.state.json["branch_belief_models"]`; the assignment of fitted
   modes to branches with soft probabilities stays. The acoustic class prior is only "zero at the
   zone centre, roughly linear nearby, bounded by the energy domain": the preset's slope mean of
   12 meV/r.l.u. was baking in the answer and is replaced by a zero-mean spread derived from the
   search band. Owner: "what phenomena are here" and "map this branch" are one question, a
   hypothesis map checked against an intensity map, answering whatever was asked — energy now,
   linewidth or an intensity change later, through the same comparison.
5. **The candidate space is not a ruling.** The sentence gives the region and the energy domain;
   windows and exposures are generated from the beliefs; nothing is labelled a survey. The owner
   refused to rule on Q, adjacency, windows or ceilings: "You have the space… if you use EIG there is
   no question here."
6. **Deleted, not routed around:** the tracker's frontier/anchor/bridge/same-q ceilings, the
   survey flag, `legacy_uniform/1` and the whole same-action scan-window comparison (O36/O41/O43
   machinery), missed-window recentre-and-widen, decline widening, predictive promotion, the
   targeted opening book, the `scan_window_eig_mode` knob. `evaluate_stop` no longer decides
   `met`; the planner's prize does. Certified pooled intervals remain the published numbers.
7. **Implementation shape:** `isar/drive/acquisition/eig_planner.py` (pure, deterministic, no RNG,
   Gauss–Hermite scenarios so equal predictions tie exactly), `tools/eig_sim.py` (closed loop
   against the analytic truth, the demo's storyline before a neutron is simulated), the loop
   calling `plan_next` at its one seam, an `eig_plan` event the campaign feed renders. The
   discovery lane (`discover_and_map`, `isar/scene/`) is untouched by this objective.
8. **Demo posture:** it does not have to be perfect; it must show the idea — scans placed
   autonomously where they are most efficient, narrow where they can be, resolving the two
   branches where they approach, no operator typing scans.
9. **The class prior weakens with distance from the fold (2026-09-02, found by the planner's
   simulator).** The per-branch belief applied its (gap, slope) prior at full precision to every
   local fit, so at u ≈ 0.9 the origin pin tilted the local line and predicted the acoustic
   ~1 meV high, reading the optic as its own. The survey pass had masked it (windows never
   depended on the prediction). Both belief implementations (`classify_eig.belief_predict`,
   `branch_assignment.BranchBelief`) now weight the prior precision by the anchors' locality
   kernel, `exp(−u²/2·LOCALITY_BW²)`: pinned at the fold, data-driven far away, which is the
   design goal's "linearity decays away from the fold" made literal. The decay alone was not
   enough: an unanchored branch far from the fold then had an unbounded spread, and the
   one-sided truncation at zero turned it into a mean of ~100 meV. The third clause of the
   class prior, "bounded by the energy domain", is now literal too: the predictive is
   truncated to [0, domain_hi], so a cold branch far out means "somewhere in the band".
10. **A null scan is evidence, not proof.** A scan that fitted no peak multiplies the weight of
    the scenarios it should have detected by a declared fitter miss probability (0.1); it never
    zeroes them. Absolute exclusion walled off the true band after one failed fit.
11. **Declared simplification (planner):** a grid point already within the bar is not offered as
    a scan site. Its own-point utility is zero by the prize; its cross-point utility is not, and
    without the exclusion the optic campaign re-scanned a solved point for the anchor. Marked in
    code with the ceiling and the upgrade path. **Known costs, not for the demo:** the optic-only
    campaign takes ~68 scans and the two-branch campaign ~95 in simulation against 20 for the
    acoustic; the real session update (Viterbi assignment) and the planner's joint matching are
    two implementations of one rule and should become one after the demo.
12. **Two phantoms found by tracing one wrong ranking (2026-09-02 evening).** (a) The 68% interval
    of a flat-dominated posterior came out four times too narrow because the level bisection ran
    in linear scale and lost the tiny excess over the floor; it now bisects the excess in log
    space (a uniform on the 9 meV band gives 3.07 meV, as it must). (b) Two peaks at one energy,
    or a peak at ω ≈ 0 whose lobes coincide with the elastic line, made the ridge-regularised
    Fisher inverse report σ = 0.000, so the planner bought that scan for a prize it could not
    deliver; identifiability is now judged on the correlation form of the Fisher matrix and an
    unidentifiable position is "not measured". Both are model corrections, not placement rules.
    With them the two-branch campaign reaches the prize in simulation (68 scans); the optic-only
    campaign still stalls because negative evidence was recorded per scan, not per branch — a scan
    that found the acoustic but no optic is evidence against the optic in that window — being
    fixed at the time of writing.
13. **Windows follow the surviving scenario mass (2a7aeef).** An unanchored branch's windows sat
    at the band midpoint even after negative evidence had pushed its surviving probability to
    the band's ends; each surviving lump of the reweighted scenario set now generates windows
    too. In simulation the optic-only campaign goes from never sighting the optic to finding it
    at scan 37 and converging on the right branch; the two-branch campaign halves to 60 scans.
14. **The prize is binary per point (cca78b5).** Log-progress scored a
    half-count scan that leaves a boundary point at ±0.052 as 99% of the value at half the cost,
    so the full-count scan that would finish it never won and the acoustic campaign ended with
    idle probes. The utility becomes expected completions (points brought within the bar) per
    neutron, with a small log-progress shaping term, and the planner applies a declared
    inflation of 1.25 to the Cramér–Rao σ (a real fit sits above the bound at these counts).
    Result in simulation: acoustic 22 scans, every boundary point finished on its first
    scan; optic-only 21 scans (one per point); both branches 44. At h = 2.85 the acoustic
    campaign spends one probe and one optic sighting because the acoustic peak's identity
    cannot be settled until the optic 2.3 meV above it is located — the "more time where the
    branches approach" outcome, as a consequence of the mutually exclusive prize, not a rule.
15. **First live run through the wired loop (2026-09-03 00:40, short path (2,0,0)→(2.3,0,0)):**
    10 scans, 5 min, terminal `met` — but three wiring defects, none in the planner: the belief
    rows are stamped with the ingest's sample id ("tavi") while the loop filtered on a config
    default, so the planner got prior-only beliefs every iteration (windows of 69–104 points
    where the simulation uses 12) and the temperature fell to 300 K; fast-tier master rows carry
    no `certifiable` flag, so the prize read the raw fit σ (0.0003) instead of the published
    calibrated halfwidth (0.053–0.064) and declared points done that the published intervals
    miss; and certification only reported the disagreement instead of feeding back. All three
    are being fixed in the wiring; the prize is judged on the published interval, and `met` is
    declared only after certification and a re-plan agree.
16. **Fit accuracy at the 0.05 bar:** `isar score --truth-fn phonon_dft` on that run: median
    |Δω₀| 0.040 meV, centre pull −0.035 meV (systematic, fits low). The DHO model is interpolated
    linearly on a 0.05 meV grid (`lineshapes.py`, predates today) and at low counts the fitted
    centres snap to 0.05 multiples (0.5003, 0.8985, 1.4000, 1.8001, 2.3006, 2.5999 on that run);
    h = 2.3 came out 0.12 meV low. Measured by refitting the same ten scans on a 0.02 meV grid:
    the snapping disappears (0.4601, 0.9505, 1.4396 … instead of 0.5003, 0.8985, 1.4000) and
    the mean pull goes from −0.025 to −0.001 meV, but the per-point scatter stays at a median
    0.04 meV (statistics at 5e6 counts per point in wide windows) and h = 2.3 stays 0.08 low.
    **Decision for the owner:** `dx = 0.02` in `lineshapes.py` is a statistics change that
    moves every fit and its frozen tests; recommended, not applied. The planner's feedback on
    the published interval buys the counts the bar needs regardless. The
    planner's declared Cramér–Rao inflation is 2.0 (published calibrated halfwidths sit 2–3×
    above the bound at these counts).
17. **Second live run (067babe, 2026-09-03 02:20):** with the beliefs reaching the planner the
    windows go from 109 points on the class prior to 12–13 points from iteration 6 — the demo
    behaviour, live. It took 82 scans for six points because the intensity map handed to the
    planner was ~1e5 too dim (`phonon.A / effective_exposure` gave 7e-5 counts·meV where the
    ingested counts show ~8–17), so no design was expected to complete a point and the cheapest
    5M probe was bought every time, five of them identical at h = 2.3 with utility exactly 0.
    Ruling: the intensity map is read from the counts (peak area above background per neutron,
    pre-Bose, per branch; elastic; background), never from the fit amplitudes whose convention
    was not what the docstring assumed. The planner now says on its decision when no design is
    expected to complete a point. Declared Cramér–Rao inflation raised to 3.0 (d3d6f2b).
18. **Prices from the realistic world (2026-09-03 04:30, commit after 6e2c8e9).** The live counts
    put the acoustic's pre-Bose area at ~0.6 counts·meV per 1e7 (flat in energy; the lobe seen is
    that × (n+1), so ~13 at 0.9 meV and ~0.9 at 6 meV). The simulator's world and the planner's
    class prior had carried the Bose-weighted lobe and were 20× too bright, which is why the
    simulation finished at 5M per point and the instrument did not. Corrected, four prices were
    needed: pre-Bose class prior 1.0; exposure ladder to 16× base (a 6 meV acoustic needs ~1e8
    per point for the bar); a fixed per-scan cost of 5e7 neutron-equivalents (the move and the
    fit) so sixteen tiny repeats no longer price like one scan; shaping 1e-3 so a cheap probe
    never outbids a completion. Realistic simulation: acoustic 21 scans with the exposure
    climbing 2e7→1.6e8 toward the zone boundary; optic-only 22; both 56; null recovery 21.

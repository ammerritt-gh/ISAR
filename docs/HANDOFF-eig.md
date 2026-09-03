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
   *Amended 2026-09-03 (item 18, PR #37 review):* a scan also carries a fixed cost — the
   instrument's move and the fit's analysis time — whose honest currency is time; the planner
   prices it at a declared exchange rate of 5e7 neutron-equivalents. The requested dose and the
   price are reported separately; the budget ledger charges the dose only.
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
   **Amendment 2026-09-03 (PR #37 review):** the extrapolation term
   `extrapolation_model_sigma` is a DECLARED SMOOTHNESS ASSUMPTION, not a derived bound —
   periodicity plus a bounded band do not bound curvature. What is assumed is that the
   dispersion's curvature is set by its lowest harmonic, |ω''| ≲ domain_hi·(π/period)², the
   quantitative form of the class prior's "roughly linear nearby" clause and wrong for a
   branch with structure finer than one zone.
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
19. **The full demo path, live, on the final planner (2026-09-03 00:50, branch `eig-planner`,
    PR #37).** `map the acoustic phonon from (2,0,0) to (3,0,0)`: 82 scans, 1.45e11 neutrons,
    24.9 min, terminal `met` on certified intervals, 20/20 points; windows 15–18 points from the
    fourth scan; the exposure ladder walks 5M → 160M per point as the acoustic dims toward the
    zone boundary; certification pushed the campaign back out twice (4 then 2 points above the
    bar) and it bought its way through. `isar score --truth-fn phonon_dft`: median |Δω₀|
    0.028 meV, but interval coverage 0.444 and the fits sit systematically low on the rising
    branch (worst −0.10 meV at h = 2.50 against a ±0.035 interval). That is the fitter's
    calibration, not placement: the published intervals under-cover ~1.9× (the handoff table
    already recorded ~1.8× before this work) and the 0.05 meV grid snaps centres (§7.16).
    **Owner decisions pending:** apply `dx = 0.02` in `lineshapes.py`; re-run the σ-calibration
    on the new selection rule and apply its inflation to the published intervals (then the
    0.05 bar costs ~3–4× the neutrons); or present the demo at a 0.1 meV bar.
    Also found: this instrument has no elastic line (zero counts at ΔE = 0 away from the fold);
    the intensity map's elastic term is ~0.03–0.26 per 1e7 and steady.
20. **PR #37 review round and the deletion slice (2026-09-03).** The external review's four P1s
    and five P2s are dispositioned on the PR (comment of 2026-09-03) and fixed in `6a4a372`
    (planner: completion needs identified mass ≥ 0.85; cross-point anchors conditional on
    admission; per-branch soft delivery evidence; whole ranking to the loop; nodes clipped to
    the band; lexicographic shaping; price named apart from neutrons; the simulator learns its
    intensity map from its own counts) and `e36e220` (loop: only the certifying halfwidth
    carries the prize; soft `seen`; a geometry refusal condemns a window at every exposure).
    The deletion slice landed as `38fc4a7`/`570a22d`: 35 source files and 22 test files gone,
    `design/eig.md` rewritten, steering's frontier contract replaced. Not-slow lane 4389/0.
    Live short path after the round: `met` in 19 scans, 5.9 min, every point identified ≥ 0.91.
    Open owner decisions: the intervals (§7.19) and whether the curvature smoothness clause
    (§7.9 amendment) stays.
21. **Grid experiment on the full path (2026-09-03 03:45, evidence for §7.16).** The 82 scans of
    the final full-path run refitted on a 0.02 meV convolution grid in a detached worktree, against
    the live fits on 0.05, per grid point against `phonon_dft`:

    | grid | median \|Δω₀\| | mean Δω₀ | max \|Δω₀\| | within ±0.05 |
    |---|---|---|---|---|
    | 0.05 (live) | 0.021 meV | −0.027 | 0.094 (h = 2.50) | 16/20 |
    | 0.02 (refit) | 0.012 meV | −0.013 | 0.061 (h = 2.50) | 18/20 |

    The finer grid halves the error and removes half the pull; h = 2.50 remains the outlier on both.
    The σ-calibration inflation (`isar score` suggests k ≈ 1.9 on the live fits) is the other half.
22. **The default bar is 0.1 meV on a 0.01 meV convolution grid (owner ruling, 2026-09-03
    morning).** `isar score` on the live full-path run showed the published intervals under-covering
    the analytic truth by ~1.9×, and the scorer decomposed the miss as roughly half random scatter
    and half a systematic centre pull. Inflation cannot fix a pull — widening an interval that is
    centred in the wrong place buys coverage by being wrong more loosely — so two changes land
    together. (a) The DHO convolution grid in `isar/analyze/lineshapes.py` drops from 0.05 to
    **0.01 meV** (`CONVOLUTION_DX`), removing the share of the pull the grid itself caused: §7.21
    measured 0.021 → 0.012 meV median error and −0.027 → −0.013 mean pull on the same 82 scans at
    0.02, and 0.01 continues that direction. Measured cost on the real lineshape: **0.159 ms per
    evaluation at 0.01 against 0.086 ms at 0.05, 1.85×**, which a 25-minute campaign absorbs.
    (b) The default per-point bar becomes **±0.1 meV** (it was 0.05 this week and 0.5 before that),
    which honestly covers the residual rather than claiming a precision ISAR cannot certify. The
    residual's physical cause is resolution focusing — the instrument's resolution ellipsoid is not
    the 1D energy Gaussian ISAR's fitter convolves with — and the proper fix is deconvolution,
    **deliberately deferred**, not attempted for the demo.

    **The σ-calibration campaign is NOT being run.** It would estimate an inflation factor k and
    multiply every published interval by it. That is the right instrument for a random-scatter miss
    and the wrong one here: half of this miss is a pull, k would absorb it into wider bars, and the
    campaign's cost would buy a number that hides a bias instead of removing it. Deconvolution, or
    a fitter that models the focusing, is what closes §7.16 — not a calibration constant.

23. **The convolution grid at 0.01 is not safe, and a presence defect it exposed (2026-09-03
    morning).** Live short path at `CONVOLUTION_DX = 0.01` and the 0.1 meV bar did not finish:
    93 scans and 50 minutes on the six-point path that had taken 19 scans and 6 minutes, with 24
    scans sunk into h = 2.05. Two causes, both measured:

    - **The grid.** At the near-fold point the acoustic sits at 0.47 meV inside a 0.8 meV
      resolution width, and on the finer grid the pooled fit oscillates between a phonon and
      elastic-plus-background (the same flip the refit showed: at 0.02 that point fits a phonon,
      at 0.01 it abstains `elastic_only`). Refitting the 82 full-path scans on all three grids:
      0.05 → median 0.021 meV, 16/20 within 0.05; 0.02 → 0.012, 18/20; 0.01 → 0.016, 17/19 with
      one point lost. The whole accuracy gain is 0.05 → 0.02; 0.01 buys nothing and costs a point.
    - **Presence could only decay.** `point_presence` multiplied `miss_factor ≤ 1` per fruitless
      look, so a detection contributed exactly 1.0 and could never restore a lost claim. A point
      whose fit intermittently delivers nothing therefore lost its own branch permanently:
      identified mass 0.00 beside a candidate at 0.475 ± 0.006, the prize count oscillating
      4/6 → 1/6. Fixed in `f4ea622` as posterior odds — each qualifying scan contributes the
      likelihood ratio of present against absent, with `FALSE_IDENTIFICATION = 0.05` the declared
      mirror of `NULL_MISS_PROBABILITY`, and an unexamined branch at prior odds 9 because the
      hypothesis map declaring a branch *is* the assertion that it exists (the class prior says
      nothing about where, which is the scenarios' business). Missed 3 then seen 3 recovers to
      0.98; missed 5 and never seen falls to 1.2e-4; the posterior is order-independent.

    The 0.01 campaign carried both faults at once, so the grid was re-tested on the fixed code.
    **This decision is now closed by measurement (§7.24): 0.05 stays, and the 0.1 meV bar carries
    the accuracy.** Neither 0.01 nor 0.02 completes a live campaign, so the offline refit gain is
    not purchasable at any exposure budget the demo has.

24. **The convolution grid reverts to 0.05, closed by three live campaigns (2026-09-03).** The same
    six-point path (`map the acoustic phonon from (2,0,0) to (2.3,0,0)`, 0.1 meV bar) was run three
    times, identical code apart from `CONVOLUTION_DX`:

    | grid | outcome |
    |---|---|
    | 0.01 | did not finish — 93 scans, then 49 scans after the `f4ea622` planner fix; both timed out at 30–50 min |
    | 0.02 | did not finish — 77 scans, timed out |
    | 0.05 | **`met` in 60 scans and 22.6 minutes**, every point within 0.041 meV of truth |

    The fine grids also return verdicts the coarse one never produces — `no_signal`,
    `signal_unmodeled`, and at the near-fold point an `elastic_only` abstention on a real peak — so
    a finer grid degrades the fit's *model selection*, not merely its arithmetic. The accuracy
    argument that motivated §7.22 (the 82-scan refit: median error 0.021 → 0.012 meV at 0.02) does
    not survive contact with a live campaign, and the 0.1 meV per-point bar the owner chose already
    covers the residual error.

    **What reverts:** `CONVOLUTION_DX` and the test/fixture repairs that existed only to keep the
    suite green on the finer grid (the H8 railed-Γ damping, the two-railed-Γ damping, the sparse
    two-peak seed, the predictive-evaluator fixture, the C2 characterization goldens, the DEFECT-6
    `dx/2` statement). **What stays:** everything §7.22 landed for the 0.1 meV bar — the compiler
    default and prompt text, `docs/STATS_PRIMER.md` §6.1, the docs, and the `goal_to_spec` golden.
    The named constant stays too, with the three campaign results written at its definition so the
    experiment is not re-run blind.

25. **Paused 2026-09-03 midday at `1d81d11`, PR #37, branch `eig-planner`.** Working tree clean,
    no executors running, no live campaign running, experiment worktrees removed. The day's arc:
    the owner set the bar at 0.1 meV (kept) and the convolution grid at 0.01 (reverted by
    measurement, §7.24). Applying them exposed four real faults, none of them the settings:

    | Fault | Fix | State |
    |---|---|---|
    | Presence could only decay, so a point whose fit intermittently failed lost its own branch for ever | posterior odds, a detection pushes back up | `f4ea622` |
    | The planner judged a scan able to measure a peak while the fitter has its own 3σ rule and disagreed, so it bought scans the fitter discarded (20 of 26 at one point) | mirror `isar.analyze.verdict.DETECT_FLOOR` | `85efa81` |
    | The campaign re-read its whole session log every scan, and 87 % of that log was a 413 KB `predictive` blob with no consumer, left by the retired promotion machinery | incremental read, blob not written; 300 ms → 6 ms, log 4.4× smaller, per-iteration time flat | `f1522fa` |
    | Beliefs extrapolated a fitted slope past their anchors as though it were a measurement, crossing acoustic and optic at the zone boundary so the rival could never be excluded | continuity: the mean reverts over a correlation length, and the group-velocity limit **bounds** the sigma rather than adding to it | `279fb77` |

    **Open, mid-thought, and the next thing to do.** The owner's correction that detectability is
    a property of the pooled master, not of one scan, landed for the FORWARD question (`1d81d11`,
    with both directional tests). It has not landed for the BACKWARD one: a brief was dispatched
    and stopped unstarted. The reason it must land is measured — `--target optic --tolerance 0.05`
    now loops at h = 2.75, where the master has 4.5e7 counts covering where the optic is believed
    to be and shows nothing, but no single scan could have seen it alone, so presence never decays
    and the planner tops up a pool looking in the wrong place for ever. The fix is one question
    asked once per point: at or above the floor on POOLED exposure the master would have seen the
    branch, so apply the presence likelihood ratio once with `p` the probability the master
    delivered it, and drop the product over scans; the scenario reweighting uses the same pooled
    test. Brief text is in the session log; it is a small change to `point_presence` and
    `_scenarios`.

    **Not yet done after that:** a full-path live campaign to `met`, twice, before anything is
    called ready (the last full attempt predates all four fixes above and reached 17/20); refresh
    the PR body and post the dispositions; the GUI rehearsal, which the owner wants to attend.

26. **The compiler refuses a target the planner cannot map (2026-09-03).** `ObjectiveSpec` carried
    four `target_selector` values — `tracked_branch`, `nearest_energy`, `lowest_energy`,
    `highest_energy` — inherited from the retired tracker. Since the EIG planner replaced it the
    value reaches nothing that plans a scan (its one remaining use in `loop.py` was a diagnostic
    field on an event payload); the planner's prize is defined per DECLARED BRANCH
    (`target_branch`, with that branch's class prior and belief). A campaign compiled with
    `nearest_energy` therefore had a prize the planner could never satisfy and ran until its budget
    died — measured as a `tests/drive/test_loop.py` case that did not terminate in 500 s. An
    accepted input that hangs is worse than a refusal, so `tracked_branch` is now the only
    selector: `ObjectiveSpec` and the goal compiler both refuse any other value and name the branch
    route, and the supervisor prompt and `LLM_GOAL_SCHEMA` enum no longer teach the retired names.
    `target_energy_meV` went with them — nothing read it once the per-q selectors were gone; an
    energy that merely identifies a branch is a zone-centre anchor (`target_zc_energy_meV`), which
    the compiler still accepts under the old key as an input alias. Per-q feature selection remains
    implemented downstream in `session/scan_delivery.py` / `session/branch_assignment.py`
    (`selected_branch_qoi`), now unreachable from any compiled campaign; deleting it is a separate
    slice.

27. **Per-q feature selection is deleted (2026-09-03).** The separate slice item 26 named. The
    reachability argument, established by grep over source, tools and tests before anything was
    removed: `scan_delivery._assign_and_triage` read the selector from
    `card.state.campaign_target["selector"]`, written only by `loop._persist_campaign_target` from
    `ObjectiveSpec.target_selector`, which item 26 narrowed to `("tracked_branch",)`; the reader
    also defaults to `tracked_branch` when the key is absent. The function therefore always took
    its `selector == "tracked_branch"` early return, and *everything after it* was unreachable —
    not only the `target_branch != acoustic` selection block but the acoustic triage below it,
    since the early return precedes both. Deleted with it, each having had no other caller in
    `isar/` or `tools/`: `branch_assignment.assignment_posterior`, `condition_on_target_energy`,
    `acoustic_qoi`, `branch_qoi`, `selected_branch_qoi`, `deliver_branch`, `deliver_acoustic`,
    `secondary_branch_points`, `assign_label`, `BranchBelief.deliverable`, and the constants only
    they used (`OCC_PSEUDO`, `W_MIN`, `P_ACOUSTIC_MIN`, `ENTROPY_MAX`, `DOMINANCE_MIN`,
    `EXTRAP_MARGIN`). `NEW_LEVEL` is kept though now unread here: the EIG planner mirrors it by
    value and names this as the canonical definition. `evidence_update._tracked_selector` was a
    constant `True`, so its two gates went: `_apply_tracked_branch` proceeds unconditionally, and
    `_rebuild_pooled_prior` — whose whole body sat behind `if _tracked_selector(card): return` —
    is gone along with its two `driver.py` calls and the now-unused `session_prior` parameter of
    `_apply_and_finish`.

    **What was kept, and why:** `build_beliefs`, `BranchBelief.predict` / `predict_data_only`,
    `features_from`, `reduced_q`, `probability_semantics`, `SIGMA_SLACK`,
    `RESOLVABLE_FWHM_FACTOR` — the belief and assignment machinery the planner, `session_prior`
    and the cross-q evidence update read every iteration. `_apply_branch_triage` was left intact:
    its `deliver is False` branch is now unreachable in conjunction with a non-null ω₀, but
    removing it nulls nine return values across two consumers, which is the delivery-path refactor
    this slice deliberately did not become.

    Docs: `design/steering.md` §7.9 went from four disambiguation rules to two — rival-branch
    banking and quicklook prior seeding were remedies for *per-scan* assignment and die with it;
    the anti-Stokes fold and the flat two-anchor extrapolation are still implemented.
    `docs/plans/stats-audit.md` §5 still describes the deleted QoI/delivery quantities; its line
    references were already stale before this slice and repairing that table is its own job.

    Tests: `tests/session/test_branch_assignment.py` 26 → 17 (nine deleted, five rewritten onto
    the surviving beliefs), `tests/session/test_branch_assignment_wiring.py` 18 → 16 (four
    selector cases replaced by two on the tracked path). Everything else was untouched.
    `tests/session/test_master_fit.py::test_parallel_masters_match_serial_end_to_end` fails —
    verified failing on a clean tree at `fe00c81` as well, so it predates this slice.

28. **Session handed off 2026-09-03 afternoon. Read this item first.**

    **State.** Branch `eig-planner`, PR #37, main untouched at 32b8a23 (never commit to main).
    Not-slow lane green. One pre-existing, unrelated failure:
    `tests/session/test_master_fit.py::test_parallel_masters_match_serial_end_to_end`, confirmed
    failing identically on clean `fe00c81` before any of this work — do not chase it here.

    **The finding that matters, and the reason today looked like tuning.** Repeat scans at one q
    never pooled. On the full-path run in `scratchpad/demo-C`, h = 2.90 took 65 of 110 scans; its
    master lists `n_members: 65` yet reports `modality: unpooled` and `effective_exposure: 1e7`,
    where those scans represent ~3e8. The 65 submissions used ~40 windows differing by hundredths
    of a meV — (2.24, 8.52), (2.25, 8.53), (2.23, 8.51) … all at step 0.19 — whose sample points
    are mutually offset, so counts cannot be summed bin by bin. The planner's model (owner ruling:
    detectability is a property of the pooled master) says a repeat adds counts; the reducer can
    only pool a shared grid; nothing enforced it. **Every "buy more counts here" decision at a hard
    point was spent on a pool that never grew.** That is why three earlier fixes each moved the
    sink to a different q instead of removing it.

    **In flight when this was written:** executor `pooling`, one commit "Drive: a repeat scan must
    land on the grid it is pooling with" — establish the real pooling criterion from the code
    first (`session/master_delivery.py`, `reduce/`, whatever writes `exposure_provenance` and
    `modality`), then snap emitted windows to a common lattice (quantise `e_lo` to a multiple of
    `e_step` anchored at zero) so repeats coincide. Acceptance: a same-q pair whose designed
    windows differ by a fraction of a step must pool; short path still `met`; full path live with
    scans, wall time, terminal, scans per point, `isar score` errors. **Collect its result first.**

    - If it lands and the full path reaches `met`: run the full sentence live twice, refresh the
      PR body (it still quotes the ±0.05 bar and pre-pooling numbers), then the GUI rehearsal,
      which the owner wants to attend.
    - If it does not: the SHORT path is demo-ready as of this session — (2,0,0)→(2.3,0,0), 10
      scans, 98 s, `met` on certified intervals, every point inside the bar, wide first window
      narrowing to 12 points, ladder climbing, rival excluded. **Owner decision pending:** demo the
      short sentence live and name the zone-boundary region as the frontier, rather than show the
      long one stalling.

    **Method note for the next session, learned the hard way.** Three defects were diagnosed from
    run summaries and each fix moved the symptom. What actually found the causes was instrumenting
    the campaign's own state: a probe reading `card.state.json` beliefs and anchors, a faulthandler
    stack dump, and a per-point trace of `eig_plan`. Do that at the FIRST stuck point, not the
    third. Related: the Cramér–Rao inflation was moved 1.25 → 2.0 → 3.0 → 1.5 across the day,
    most of which was compensating for the pooling defect rather than calibrating anything; do not
    read those constants as settled evidence.

    **Concurrency rule, learned the hard way too.** Two executors sharing this checkout lost their
    in-flight slices to a `git reset --hard` mid-run. Give concurrent executors their own
    worktrees, and never run checkout/reset/clean on a tree another agent is writing.

29. **Pooling fixed and confirmed (`bbbae9b`); one new, different stall at the zone boundary.**

    **The criterion, established from the code not guessed.** `isar.reduce.collate._merge_group`
    pools same-(sample, Q, T) scans by concatenating members' EXECUTED energies and binning at ¼
    the median spacing of that concatenated set (`_auto_energy_tol`). Members add counts to one bin
    only when their executed points coincide; `modality` and `effective_exposure` (the peak-local
    MEDIAN of the summed monitor) are downstream of that binning. Measured on the forensic
    workspace: h = 2.90's master had 65 members but 1419 bins at 0.001 meV median spacing, so the
    summed 1.4e10 monitor spread so thin that the peak-local median was still one scan's 1e7.

    **The fix.** `scan_grid.snap_to_lattice()` quantises a window's step to the adapter's executed
    precision and its bounds to whole multiples of that step, moving OUTWARD so the requested band
    is never trimmed; wired into `loop._finalize_proposal`, the one normalization seam every
    proposal passes, so it covers feasibility-trimmed repeats too, not only the planner's.
    `proposal_snapped_to_lattice` is logged when it moves a window. A test pins the pre-fix defect
    so it cannot silently return.

    **Confirmed live.** `pooled_master_update` at h = 3.0 shows `n_members` and `effective_exposure`
    climbing monotonically (114 members, 1.285e9 by the last sample) — impossible under the defect.

    **Short path is demo-ready:** (2,0,0)→(2.3,0,0), 13 scans, 4.2 min, `met`, 6/6 within the bar.

    **NEW and different: an EIG convergence stall at the zone boundary.** Full path, 208 scans,
    49.6 min, 17/20; h = 2.85, 2.95 and 3.00 never close. Scans per point are 1–4 everywhere except
    h = 2.7 (23, the acoustic/optic crossing) and h = 3.0 (**149**, half-width flat at ≈1.02 meV
    across the last ~40 iterations while exposure repeatedly doubled). This is NOT pooling and not
    precision: more counts cannot move it.

    **Hypothesis for the next session, to verify before acting.** 1.02 meV is close to half the
    2.0 meV gap between the acoustic (6.00) and the optic (8.00) at the boundary, and an earlier
    trace showed exactly this shape — `u=1.00 hw=1.007 ident=0.98 cands=2`. The identity gate asks
    whether the target is one of the fitted peaks (Σ w_j ≥ 0.85) but not whether the mass sits on
    ONE of them; the HDI correctly spans both and so refuses the point, but nothing resolves which
    peak is the target, because that is decided by the beliefs and not by counts. Verify first with
    a per-point trace at u = 1.00 — `identified_mass`, `n_candidates`, both candidate energies and
    their posterior weights, and `belief_predict` for both branches — then decide whether the fix
    is in the assignment (the joint matching should prefer the injective assignment acoustic→6.0,
    optic→8.0) or in the prize (require the mass to be concentrated on one peak, which would make
    the deficit finite and let EIG price the scan that separates them). Do not add exposure rungs;
    exposure is demonstrably not the lever here.

30. **The zone-boundary stall was anchor starvation, and the full path now reaches `met`
    (2026-09-03 evening).** Verified as §7.29 asked, with a per-point probe on a fresh live run that
    reproduced the stall in 15 minutes (22 identical designs at h = 3.0, half-width 1.07 meV,
    identified mass 0.90, two clean peaks at 5.99 and 7.99 meV). Neither hypothesis in §7.29 was the
    cause: the identity gate and the injective matching were correct on their inputs. The inputs
    were wrong. `belief_predict("acoustic", u = 1.0)` had no anchor nearer than u = 0.5 and
    extrapolated to σ = 1.54 meV; the optic had no anchor anywhere (σ = 2.60, the class prior). With
    both beliefs that wide the joint matching genuinely cannot say which peak is which, and counts
    sharpen the peaks, not the beliefs.

    **Why the anchors were missing.** Every master from h = 2.55 to 2.95 was `track_state =
    confirmed` at target probability 0.92–0.9998 — and `errors_tier = fast`, `modality =
    unpooled`. Only a `fold` master enters the dispersion prior (`_rebuild_tracked_prior`,
    `session_prior.add_point(require_fold=True)`, Hard Rule 5), and a fast fit is never a fold: no
    bootstrap samples, so nothing to pool. The full tier was requested only on a re-scan with an
    untrusted HESSE interval (`_q_needs_full_errors`) or by the certify pass — and the planner never
    re-scans a `within_bar` point, while the certify pass runs only after `plan_decision.stop`, which
    the stalled point prevents. Two correct mechanisms, never reconciled. Owner chose the pragmatic
    repair over the EIG-native one (pricing "re-analyse at full tier" as a design).

    **The fix (`isar/drive/loop.py`).** After each plan, `_pending_anchor_escalations` names every
    prize point that is `within_bar` but whose pooled master is not a `fold`, and the loop refits
    exactly those (`refresh_analysis(force_masters=…)`, `escalate_fit` reason `belief_anchor`) and
    re-plans before proposing a scan. Idempotent: a folded master is no longer named. A first version
    named only UNtrusted HESSE fits and the chain broke again at the first *trusted* one (h = 2.50,
    2.60): trusted or not, a fast fit is not a fold — that is why the predicate reads `modality`.

    | run | scans | wall | prize | h = 3.0 scans | anchors (ac / op) at u = 1.0 |
    |---|---|---|---|---|---|
    | before (`bbbae9b`) | 208 | 49.6 min | 17/20 | 149 | 3 / 0 |
    | escalate untrusted only | 255 | 65 min, analysis limit | 17/20 | 155 | 10 / 0 |
    | **escalate any non-fold** | **92** | **15.2 min** | **20/20 `met`** | **3** | **20 / 5** |

    Final run: 3.1e10 neutrons; 18 escalations cost 214 s of analysis against 488 s of fits;
    `isar score --truth-fn phonon_dft` median |Δω₀| 0.016 meV, centre pull −0.013, 68 % coverage
    0.90 on the bootstrap intervals; every point's certified half-width ≤ 0.093 meV. Short path:
    `met`, 10 scans, 2.8 min (was 13 / 4.2). Residual hot spot h = 2.50 (24 scans) — not a stall, not
    chased. Tests: `tests/drive/test_loop.py` 148/148 with two new cases pinning the predicate.

    **Next:** run the full sentence live once more, refresh the PR #37 body (still quotes the ±0.05
    bar and pre-pooling numbers), then the GUI rehearsal the owner wants to attend.

31. **Second external review of PR #37 at `21ceed6` (five P1, six P2), dispositioned and landed
    (2026-09-03 night).** Every finding was checked against source before a line changed; the
    verifications and remedies are in `design/eig.md`'s 2026-09-03 amendment. In one line each:

    | # | Finding | Disposition |
    |---|---|---|
    | P1 | fit-local `model_interval` certifies a secondary / session-ambiguous candidate | FIX, and it was live on the demo path: at h = 3.0 the selected target is the acoustic at 5.99 meV and the model interval is the optic's (7.95–8.00) — the point had certified on the rival's interval. `_master_obs_from_row` refuses a model interval that does not describe the row's `omega0` and certifies the selected mode on its own bootstrap / profile / trusted-HESSE chain; `_planner_candidates` binds that width to the ONE candidate at `omega0`. Two cuts failed live first: gating on the tracker's `confirmed` state stalled h = 2.05 (the session's per-peak `assign` stays ambiguous where the planner's joint matching settles it); binding to the model interval's own mode stalled h = 3.0 (104 scans), which exposed the optic-interval certification |
    | P1 | candidates, exposure, intensity, escalation matched by Q alone across (sample, T) | FIX — `_slice_rows` filters master rows and observations to the campaign slice; `_master_obs_from_row` / `_observation_from_row` now carry `sample`, `T`. DEFER: stopping and certification indices still seat by hkl |
    | P1 | banked exposure at the branch mean credited to every scenario | FIX — per scenario energy in `_designs_at` |
    | P1 | averaged anchor installed in outcomes that admit none | FIX — own-point posterior selects the anchored belief per (design, scenario, branch) by `admitted`. DEFER: `reach` stays a product of marginals |
    | P1 | every mode in a physical fit labelled with the row's branch for the intensity map | FIX — `_candidate_probabilities` labels only `selected_candidate_index` |
    | P2 | presence judged at the predictive mean, delivery as max over peaks, fixed miss ratio | DEFER — design-level, named in `eig.md` |
    | P2 | negative evidence from the requested window, not the executed/QC'd grid; multi-detector double count | DEFER — named; TAVI has one detector |
    | P2 | continuity rule is a dispersion prior; the truth's fold slope (3π ≈ 9.4) exceeds the 6 meV/r.l.u. bound | DECLARED in `eig.md`; value kept, not tuned to the truth |
    | P2 | checkpointed anchor escalation treated as complete | FIX — `enforce_completion=True`, once per master per run, `anchor_escalation_unresolved` logged; only `unpooled` masters are named (a `fork` cannot be refit into a fold) |
    | P2 | price omits the variable full-tier reanalysis | DECLARED in `eig.md`: the optimised price excludes analysis time (~5 % of fit time on the reference run) |
    | P2 | lattice is per proposal step; point-cap coarsening after the snap | DEFER — same-Q resolution is fixed; needs the 220-point cap to bite |

    Tests: `tests/drive/test_loop.py` +4 (interval binding, selected-mode labelling, slice
    filtering, fork exclusion); `tests/drive/test_eig_planner.py` 31/31 with the seeded campaign
    golden re-pinned — it now opens at h = 2.05 with a 14-point window instead of h = 2.10 with 33,
    still 21 scans to the prize. The PR body and a review-response comment carry the same table.

    **Live after the round (`7503d13`):** short path `met`, 16 scans, 2.5 min. Full path `met`,
    100 scans, 21.2 min, 6.2e10 neutrons, 20/20, h = 3.0 in 18 scans; every certified half-width
    ≤ 0.089 meV; `isar score --truth-fn phonon_dft` median |Δω₀| 0.037 meV, centre pull −0.019,
    68 % coverage 0.64. These are the honest figures: §7.30's 0.016 meV / 0.90 were partly the
    optic's precision certifying the acoustic at the boundary, and h = 3.0 now pays for its own
    interval. Analysis time: fits 794 s, anchor escalations 224 s, certify 64 s.

    **Seats.** The `review_files` seat ran on `0d82d47` (one finding, rejected with the reason in
    code); NanoGPT's limit was reached afterwards, so `7503d13` carries no external seat — the
    loop suite (153) and the live full path are its verification. Codex remains available. The
    goal compiler on the local 35B model emitted a stray `target_zc_energy_sigma_meV` on four of
    four compiles of the demo sentence and `goal_to_spec` refused each (a pinned contract). Decided
    before the GUI rehearsal: a sigma with no energy to qualify carries no operator intent — the
    sentence never said it — so it is now dropped with a logged warning instead of refused
    (`test_a_sigma_without_an_energy_is_dropped_not_refused`); two compiles on the local model
    then produced the same program as the earlier compiler, byte for byte on every field that
    matters. A retry would not have helped (4/4), and a pre-compiled program is not "enter the
    sentence, hit go".

32. **GUI rehearsal, 2026-09-03 evening — two changes from the owner at the screen.** The
    rehearsal itself ran as specified (sentence in, scan within seconds, windows narrowing,
    anchors escalating, on the same trajectory as the CLI runs). (a) *The scan must be seen
    before "fitting at q" is said of it.* The drive logged the fit activity right after saving the
    scan file, and the ledger only learned of the scan inside the worker's `run` (ingest, then fit)
    — so the feed read "Started fast fit at q" over an empty plot. The worker now has an
    `ingest` command (the same step as `isar ingest`), the runner seam an `ingest_new` that no-ops
    for doubles without it, and the loop parses the landed scan into the ledger and logs
    `scan_landed` (rendered "Scan landed at q, N points, E lo…hi meV") *before* it opens the fit
    activity. Best effort: a failed early ingest is logged and the fit's own ingest still runs.
    (b) *The dispersion view's green dots and grey line were invisible.* The pyqtgraph backend
    mirrors the matplotlib palette (mid greys, CSS green) on pyqtgraph's black canvas. A first
    cut switched the canvas to white; the owner rejected it ("blinding", and not what was asked)
    and it was reverted the same night — the canvas is black as before. **Open, owner's
    instruction pending:** which colours to change for the dots and the line; nothing else in the
    palette is to be touched. (a) needs a GUI restart to show.

33. **The tracker scored identity on the bare class prior; it now reads the campaign's belief
    (GUI rehearsal, 2026-09-03 late).** The owner's rehearsal campaign (saved as `eig-1-test`)
    ran cleanly to h ≈ 2.75 and then "went weird": one q skipped, scans at 2–4 meV and −1–1.5 meV
    where nothing is, and 42 scans at h = 3.0. Traced from the workspace, not the summaries:
    every two-peak master from h = 2.75 on (5.7 / 8.2, 5.9 / 7.9, 5.9 / 8.0 meV) carried
    55 / 45 for *both* peaks in the session's assignment and went `ambiguous` — delivering
    nothing, anchoring nothing — while the planner's joint matching said 0.97 for the acoustic at
    the same points. The session's `branch_tracking._emission` located each branch from the class
    prior alone (mean = zone-centre energy + slope × distance, sigma = √(zc² + (slope σ ×
    distance)²)): at u = 0.8 that is "acoustic 0 ± 14 meV, optic 4.5 ± 6.8" — flat, and blind to
    the fourteen acoustic anchors the campaign had banked to u = 0.7. The optic never anchored at
    all, so its belief stayed the class prior and the planner hunted it across the band (the
    low-energy probes were its surviving scenario lumps); h = 2.85 lost on price to 5M probes at
    h = 3.0 whose predicted gain the prize could never confirm. Cause and effect all the way down
    from one flat emission.

    **The fix:** `track_path(..., beliefs=)` — the tracker's emission takes each anchored branch's
    (mean, sigma) at the path point from `BranchBelief.predict(u, domain_hi)`, the same belief the
    planner reads (`_tracked_beliefs` builds it from `dispersion_prior` exactly as
    `_store_branch_beliefs` persists it); a branch without anchors still uses the class prior,
    which says more than "somewhere in the band". The continuity transition is unchanged. The
    track's cache key does not include the anchors: they change only through the track's own
    rebuild, i.e. only when observations do, and hashing them re-scored every repeat call. Test:
    `test_anchored_beliefs_decide_identity_where_the_class_prior_cannot`. This is the "two
    implementations of one rule" item from §7.11, now unified on the session side because the
    demo path depended on it.

    **Live (`edd0373`), full path:** `met`, **64 scans, 11.8 min**, 2.8e10 neutrons, 20/20; no
    point above 7 scans (h = 3.0: 7); h = 2.6–2.75 `confirmed` at P(acoustic) = 1.00 where the
    rehearsal had 55/45; anchors at the end acoustic 20, optic 3; every certified half-width
    ≤ 0.089 meV; `isar score` median |Δω₀| 0.046 meV, pull −0.018, coverage 0.73. Analysis time
    fits 315 s, escalations 199 s, certify 78 s. The best full-path run so far, and the first in
    which both branches are anchored to the boundary by the session's own verdicts.

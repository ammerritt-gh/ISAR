# EIG — the planner that chooses every targeted scan

> **Status:** live
> **Authority:** canonical design for targeted-lane scan placement (Q, energy window, exposure)
> **Verified against code revision:** `isar/drive/acquisition/eig_planner.py` (2026-09-03)
> **Owner:** @ammerritt-gh

## 1. What EIG is here

There is a **hypothesis map** and an **intensity map**, and scans are chosen to prove the
hypotheses according to expected information gain priced against the intensity map. Nothing else
places a scan.

- **Hypothesis map** — the per-branch belief with its catalogue class prior entering as parameter
  precision, persisted in `card.state.json["branch_belief_models"]` and read drive-side by
  `isar/drive/acquisition/classify_eig.py::belief_predict`. For "acoustic": ω → 0 at the zone
  centre, roughly linear nearby, bounded by the declared energy domain. The class prior's
  precision decays with distance from the fold through the anchors' locality kernel
  `exp(−u²/2·LOCALITY_BW²)`, and the predictive is truncated to `[0, domain_hi]`, so a cold branch
  far from the fold predicts "somewhere in the band" rather than a confident wrong answer.
- **Intensity map** — the count scale a scan will actually deliver, read from the measured counts
  (`IntensityScale`: per-branch integrated Stokes-lobe counts·meV **pre-Bose**, the elastic line,
  and background per point, each per neutron of `ncount`). It is read from the counts the arena
  recorded, never from a fit amplitude.
- **EIG** — for each candidate action `(Q, energy window, exposure)`, the expected progress toward
  the prize per neutron, obtained by simulating the intensity the scan would produce under the
  current hypotheses and re-running the same belief update the real data goes through. Greedy,
  re-decided after every scan.
- **The prize** — the target branch's energy at every grid point of the declared path, known to
  within the requested bar (default **±0.1 meV**), and identified. One exemption: the zone centre
  of a gapless target, whose energy the hypothesis fixes at 0 and the elastic line makes
  untestable.
- **The price** — neutrons requested (`n_points × ncount`) plus a declared fixed per-scan price
  (`scan_overhead_price`, default 5e7 neutron-equivalents) standing for the instrument's move and
  the fit. Without that fixed price sixteen tiny repeats cost the same as one real scan.

A wide window happens only because a belief is wide. A narrow one happens because it is not.
Exposure climbs where the branches approach, because that is where the information is — an EIG
outcome, not a rule.

## 2. The algorithm, as implemented

`isar/drive/acquisition/eig_planner.py::plan_next(PlannerInput) -> Decision` — pure,
deterministic, I/O-free, no RNG, and it may not import `isar.session`.

1. **Predict.** For every branch and every grid point, `belief_predict` gives `(mean, sigma)` of
   that branch's energy, truncated to the domain.
2. **Assign and score the prize.** Two different questions are asked of the same fitted peaks and
   they normalise differently. `assign` gives a distribution over the modelled branches plus a flat
   `new` escape (`NEW_LEVEL`), computed independently per peak — it decides which belief a peak
   anchors. The **target-energy posterior** is one mutually exclusive mixture over the point's
   peaks plus "none of them", summing to one — it decides the prize. The prize measure is the
   half-width of that posterior's 68.27% highest-density interval, so a sharp bimodal posterior
   (two rival peaks, 50/50) fails the bar however narrow each mode is. A point's deficit is
   `max(log(hw/τ), 0)`; the prize is met when every non-exempt deficit is zero.
3. **Generate candidates.** Windows come from the belief moments, from the hulls over the
   surviving scenarios, and from **each surviving lump of a branch's reweighted scenario set** —
   so an unanchored branch whose negative evidence has pushed its mass to the band's ends generates
   windows there, not at the band midpoint. Widths are `WINDOW_K_FACTORS` multiples of the
   predictive sigma; the step is `step_fraction` of the resolution FWHM; exposure is the
   `ncount_ladder` (0.5× … 16× `base_ncount`). A grid point already within the bar is not offered
   as a scan site (see §4).
4. **Simulate the outcome.** `SCENARIO_ORDER`-node Gauss–Hermite scenarios over each branch's
   predictive replace Monte Carlo entirely: equal predictions produce byte-identical scenario sets
   and therefore tie **exactly**, with no seed-ordered noise.
5. **Preposterior.** For each scenario the expected counts follow from the intensity map (with the
   Bose factor at the campaign temperature, the elastic profile where the window sees it, and
   background), and the Fisher information of the DHO ⊗ resolution model gives the σ the fit would
   report. Identifiability is judged on the **correlation form** of the Fisher matrix: two peaks at
   one energy, or a peak whose lobes coincide with the elastic line, are *not measured* rather than
   measured to σ = 0. The reported σ is inflated by `FIT_SIGMA_INFLATION` — a declared model of the
   fitter's reliability at the counts we run, applied in the planner only, so the planner must earn
   its margin rather than assume it.
6. **Utility.** Expected **completions**: how many prize points this design is expected to bring
   within the bar. Binary per point, because a weighted log-progress term let a half-count scan
   that leaves a boundary point at ±0.052 score ~99% of the value of the full-count scan that would
   finish it, at half the price — so the point never got finished. The log-deficit removed survives
   as `shaping_nats`, used only to order designs that complete nothing.
7. **Rank.** `score = utility_nats / price`. Ties break on `(−score, −utility, price, grid_index,
   e_lo, e_hi, ncount)` — a total order over values that are themselves exactly reproducible.
8. **Stop.** `plan_next` returns `stop_reason="goal_met"` when every non-exempt deficit is zero.
   That is the one stop that means the objective was answered; `no_admissible_window` means the
   planner ran out of moves, and the loop reports it as `incomplete`.

**A null scan is evidence, not proof.** A scan that fitted no peak multiplies the weight of the
scenarios it should have detected (expected counts above `NULL_DETECTION_COUNTS`) by a declared
fitter miss probability (`NULL_MISS_PROBABILITY = 0.1`); it never zeroes them. Negative evidence is
recorded **per branch**, not per scan: a scan that found the acoustic but no optic is evidence
against the optic in that window at that exposure. Absolute exclusion walled off the true band
after one failed fit.

## 3. Invariants

- No RNG anywhere on the path. Equal predictions tie exactly.
- No candidate is refused for what it is. There is no survey flag, no opening book, no legacy
  control that must be kept as the selected candidate, no same-q ceiling and no frontier rule.
- Nothing is legislated per phenomenon: both branches are always in the forward model, whichever is
  the target.
- The zone-centre exemption is geometry, taken through `GridQ.precision_coverage_eligible`
  (`isar/drive/supervisor/stopping.py`) and `PointState.exempt`, and applies to a gapless target
  only — an optic branch has a finite energy at the zone centre and is perfectly measurable there.
- The planner is pure. Every number it reads arrives in `PlannerInput`; every number it produces
  leaves in `Decision`.
- The prize is judged on the **published, certified interval**, never a raw fit σ. `met` is
  declared only after certification and a re-plan agree.

## 4. Declared simplifications (ceilings, with their upgrade path)

- **A within-bar point is not offered as a scan site.** Its own-point utility is zero by the prize;
  its cross-point utility is not, and without the exclusion the optic campaign re-scanned a solved
  point for the anchor. Lift it by pricing the anchor's cross-point gain against a scan that does
  not re-measure a solved point. Marked in code at `plan_next`.
- **The planner's joint matching and the session's Viterbi assignment are two implementations of
  one rule.** They should become one; it is deferred past the demo.
- **`FIT_SIGMA_INFLATION` is a calibration knob, not a derivation.** It is set from measured live
  runs and must be re-checked when the fitter's interval calibration changes.

## 5. Code locations

| Concern | Where |
|---|---|
| The planner | `isar/drive/acquisition/eig_planner.py` (`plan_next`, `PlannerInput`, `Decision`) |
| Belief predictive, branch MI | `isar/drive/acquisition/classify_eig.py` |
| The request the planner's design becomes | `isar/drive/acquisition/acquire.py::ScanProposal` |
| Grid arithmetic shared by design and submission | `isar/drive/acquisition/scan_grid.py`, `grid_adapters.py` |
| The one seam that calls it | `isar/drive/loop.py::run_campaign` |
| Closed-loop simulator (no instrument) | `tools/eig_sim.py` |
| Prize / stop bookkeeping | `isar/drive/supervisor/stopping.py` |

## 6. The `eig_plan` event

One event per decision, written by the loop at the planner seam and rendered by the campaign feed
(`isar_gui/log_render.py::_render_eig_plan`). It carries:

- the chosen design (Q, window, step, points, ncount, utility, cost, score);
- the **top ten** ranked designs, so the runner-up is visible;
- per grid point: `u`, `hkl`, `exempt`, `halfwidth_meV`, `deficit_nats`, `within_bar`,
  `n_candidates`;
- the prize count (`prize_points_within_bar` of `prize_points_total`);
- the intensity map in operator units (counts per 1e7 neutrons) plus whether it changed;
- `note` — set when **no design is expected to complete a point** and the cheapest probe is being
  bought, so the feed says so instead of showing a silent zero;
- `stop` / `stop_reason`.

## 7. Status

**Implemented and authoritative.** The full demo path ran live on TAVI on 2026-09-03 with the final
planner: (2,0,0)→(3,0,0), 82 scans, 1.45e11 neutrons, 24.9 minutes, terminal `met` on certified
intervals, 20/20 prize points. Windows narrowed from ~109 points on the class prior to 15–18 points
from iteration 4; the exposure ladder climbed 5M → 160M per point toward the zone boundary, which
is where the acoustic lobe is dimmest.

Closed-loop simulation against analytic truth (`tools/eig_sim.py --target acoustic --seed 1`,
realistic prices): `goal_met`, 27 scans, 6.17e10 neutrons, 20/20 points inside the bar, every
measured centre within 0.07 meV of truth (measured 2026-09-03 at `6a4a372`; the price and
identity-gate work of that day moved it from 21 scans at the previous revision — the count is
sensitive to the declared prices, and the campaign completing is what is being pinned).

**Known, and outside this design:** `isar score --truth-fn phonon_dft` on the live run reports a
median |Δω₀| of 0.028 meV — inside the 0.05 meV bar that run used — but an interval coverage of 0.444 against a
nominal 0.68, i.e. the published intervals under-cover by roughly 1.9×. That is a fitter σ-calibration
matter, not a placement matter: the planner buys against the interval the fitter publishes, so an
under-covering interval makes it stop slightly early. It is recorded here honestly and owned by the
statistics layer (`docs/STATS_PRIMER.md`).

## 8. Amendments

**2026-09-02 — the same-action scan-window comparison is retired; the planner chooses Q, window and
exposure.** The O36 / O41 / O43 machinery compared a legislated "legacy" window against a centred
core *at a q that already had its own confirmed fit*, on information per neutron. Because the
targeted lane surveyed the full search band on every first visit (one line: `survey = not
best_resolved`), the core could only ever win on a revisit: on a 21-point line the first 20 scans
were surveys by construction. The comparison was answering a question — "of these two windows for
this one action, which is better?" — that presupposes the action was already placed by something
else. Placing the action *is* the problem, so the comparison, its policy compilation, its authority
records, its offline corpus and the `--scan-window-eig-mode` knob were deleted rather than routed
around. The ruling IDs survive as history only: **O36** (authoritative comparison restricted to a
clean confirmed target), **O41** (the restricted authoritative profile), **O42** (release review,
retired by H1), **O43** (realized-comparison authentication), **H22** (default-on at compile time).
Nothing in the current program reads any of them.

**2026-09-02 — the class prior weakens with distance from the fold.** The per-branch belief applied
its (gap, slope) prior at full precision to every local fit, so at u ≈ 0.9 the origin pin tilted the
local line and predicted the acoustic ~1 meV high, reading the optic as its own. The survey pass had
masked it, because windows never depended on the prediction. Prior precision is now weighted by the
anchors' locality kernel and the predictive truncated to the domain.

**2026-09-03 — a point the planner is finished with is escalated to the full tier so it can anchor
the belief.** Only a `fold` master enters the running dispersion prior (Hard Rule 5;
`session_prior.add_point(require_fold=True)`), and a fast-tier fit is never a fold — it has no
bootstrap samples to pool, whether or not its HESSE interval is trusted. The session's identity
tracker, meanwhile, confirms a point off that same fast fit and the planner moves on. Nothing
reconciled the two: the full tier was requested only when a q was re-scanned with an untrusted
HESSE interval, or at the end-of-campaign certify pass. Measured live on the full acoustic path,
every point from u = 0.55 to 0.95 was `confirmed` at target probability ≥ 0.92 and never became an
anchor; the acoustic belief at u = 1.0 extrapolated half a zone from its last anchor to σ ≈ 1.5 meV,
the optic had no anchor at all, and the two fitted peaks 2 meV apart could not be told apart at any
exposure (149 and then 155 scans at h = 3.0). The loop now escalates every prize point that is
`within_bar` but not yet a `fold` master (`loop._pending_anchor_escalations`, one `force_masters`
refit, logged as `escalate_fit` with reason `belief_anchor`) before proposing the next scan;
idempotent, since a folded master is no longer named. Full path after the change: `met`, 92 scans,
15.2 min, 20/20, h = 3.0 in 3 scans, both branches anchored to u = 1.0 (acoustic 6.29 ± 0.30,
optic 8.07 ± 0.32 meV), 18 escalations costing 214 s of analysis time. The EIG-native form —
pricing "re-analyse this point at the full tier" as a design beside "measure this point" — is the
principled successor and is not built.

**2026-09-03 — an underpowered abstain is not a null.** The fitter's `underpowered` verdict says
"I could not see it at this exposure", not "there is nothing here". Read as a null, a 20M first
look at h = 2.30 — ten counts at the peak, the intensity map exactly right — pushed the acoustic's
scenario mass out of its own window and the planner bought an 8 meV, 58-point scan to find it
again. `PlannerInput.abstained` (from `loop._abstained_points`, the master's own verdict) names
those points; their counts still bank and their silence is not evidence in `_scenarios` or
`point_presence`. The gap underneath — the planner's 3σ mirror of the fitter's detection floor
against the fitter's evidence-based model selection — is the "fitter response surface" of the
review's question 2 and stays open. Also from the same campaign: a plan is logged only when it is
acted on; the anchor-escalation check runs first, so a plan set aside for an escalation no longer
draws a phantom "planning a 58-point scan" line in the feed.

**2026-09-03 — the session's tracker reads the campaign's belief.** The session's identity
tracker (`branch_tracking.track_path`) located each branch at each path point from the class prior
alone — zone-centre energy plus slope × distance, sigma from the slope prior — which is flat away
from the fold by construction (acoustic 0 ± 14 meV at u = 0.8) and blind to the anchors the
campaign has banked. The planner located the same branches from the belief (anchors under the
class prior, band-truncated). Two implementations of one rule, and they disagreed exactly where
it mattered: every two-peak master past h = 2.75 on the reference path went 55 / 45 in the
session (ambiguous, no delivery, no anchor) while the planner said 0.97 — the chain of anchors
broke as the optic entered view, the optic never anchored, and the planner spent the rest of the
campaign hunting it. `track_path` now takes the campaign's beliefs and scores each anchored
branch's emission from `BranchBelief.predict(u, domain_hi)`; an unanchored branch keeps the class
prior (which still says more than "somewhere in the band"); the path-continuity transition is
unchanged. The remaining duplication — the planner's `belief_predict` and the session's
`BranchBelief.predict` as two arithmetically-mirrored functions — is the last of that item.

**2026-09-03 — the PR #37 second review round (five P1, six P2), dispositioned.** Fixed, each
with a test: (1) the certifying interval belongs to the SELECTED mode. The model-averaged
interval is fit-local — it describes the fit's primary mode — and at h = 3.0 on the reference
path that is the optic (7.95–8.00 meV) while the tracker's selected target is the acoustic at
5.99: the point had been certified on the rival's interval. `_master_obs_from_row` now refuses the
model interval when it does not describe the row's `omega0` (within a resolution width) and
certifies the selected mode on its own chain — bootstrap, profile, trusted HESSE — which
`_select_mode` copies onto the row; `_planner_candidates` then binds that half-width to the ONE
candidate at `omega0` and a secondary mode carries "detected, precision unknown". Not gated on the
tracker's `track_state`: a first cut was, and it re-created a stall at h = 2.05 live (23 scans,
nothing within the bar) because the session's per-peak `assign` reads a lone near-fold peak as
ambiguous under the optic's broad class prior where the planner's joint matching settles it; a
second cut bound the width to the model interval's own mode and stalled h = 3.0 (104 scans),
which is how the optic-interval certification was found; (2) the planner's world is
one (sample, temperature bin): master rows, scan exposure, the intensity map and anchor
escalations are filtered to the campaign slice before any Q match (`loop._slice_rows`), where they
had matched by hkl alone; (3) banked exposure is asked per scenario energy, not at the branch mean
(`_designs_at`), so a deep old window over an excluded gap no longer pays for a top-up at a lump it
never covered; (4) the own-point preposterior uses the anchored belief only in the outcomes that
admit the anchor — an outcome that admits nothing sees the belief as it was; (5) a physical row's
`branch_id` labels only its selected mode for the intensity map, never a two-mode fit's second
lobe; (6) an anchor escalation requires a completed runner result (`enforce_completion`), names only
`unpooled` masters (a `fork` is the full tier's own answer and cannot be refit into a fold), and is
attempted once per master per run.

Declared rather than fixed: the continuity rule's group-velocity bound (`MAX_GROUP_VELOCITY`,
6 meV/r.l.u., shared with `branch_assignment.ACOUSTIC_MAX_SLOPE`) and reversion length
(0.25 r.l.u.) are a **dispersion prior**, not a consequence of continuity — and the reference
truth ω = 6 sin(πu/2) has slope 3π ≈ 9.4 meV/r.l.u. at the fold, above the bound. The bound acts
only beyond the anchored range, as a cap on the extrapolation sigma, so it cannot narrow an
interpolation between anchors and the fold is anchored from the first scan; where it does act, at
an unanchored far end, a branch still rising at 9 meV/r.l.u. would be extrapolated more confidently
than it deserves. Kept at 6 rather than tuned to the truth (the class prior may not know the
acoustic's slope); a value derived from the band alone is the honest replacement and is not
built. And the planner's price is the neutron dose plus the fixed per-scan cost; the
full-tier reanalysis an anchor escalation buys is **not** in the price. The optimised objective
therefore excludes variable analysis time, which the reference run shows is ~5 % of the fits'
analysis time (214 s against 488 s + 3146 s of scans' own fits across two runs); pricing it belongs
to the EIG-native successor above.

Deferred by name: presence as a joint location-and-delivery likelihood (it is judged at the
predictive mean with a fixed miss ratio; the delivery probability takes the max over peaks where
the mutually exclusive marginals should be summed); negative evidence from the executed grid after
QC and point-cap coarsening rather than the requested window; one lattice quantum across step
changes (the snap is per proposal step, and a point-cap coarsening after the snap can leave a
window off the pool's lattice — same-Q resolution is fixed, so it needs the 220-point cap to bite);
`reach` as a joint admission probability rather than a product of marginals; the second `met` ask
cross-checked against the session-side certified grid; the stopping layer and certification
indices still seat masters by hkl alone.

**2026-09-02 — two model corrections found by tracing one wrong ranking.** The 68% interval of a
flat-dominated posterior came out four times too narrow because the level bisection ran in linear
scale; it now bisects the excess in log space. And an unidentifiable position (two peaks at one
energy, or a peak whose lobes coincide with the elastic line) is now judged on the correlation form
of the Fisher matrix and reported as *not measured*, instead of σ = 0.000.

**2026-09-03 — the intensity map is read from the counts.** The planner had been scaling from
`phonon.A / effective_exposure`, whose convention was not what the docstring assumed; it gave
7e-5 counts·meV where the ingested counts show 8–17, so no design was ever expected to complete a
point and the cheapest probe was bought every iteration. The map is now the peak area above
background per neutron, pre-Bose, per branch, plus the elastic line and background — read from the
measured counts.

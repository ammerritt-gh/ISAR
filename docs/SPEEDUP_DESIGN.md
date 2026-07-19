# Fast Test Campaigns — speedup sprint design

**Status: design approved 2026-07-19; implementation not started.**
Owner intent: this sprint is judged on **development iteration velocity** — synthetic/analytic
test campaigns must get several times faster so that design iteration on everything else
(backgrounds, steering physics, scan economy) speeds up. It is *not* judged on beam-time
economy or final-number accuracy; certification honesty is explicitly preserved unchanged.

Companion documents: `docs/handoffs/HANDOFF_statistics_contract_checkup.md` (the audit that
motivated this), `docs/STATS_PRIMER.md` §3 (the coverage evidence that licenses Phase 2),
`docs/MEASUREMENT_STEERING.md` §7.5 (master-authoritative semantics Phase 3 must preserve).

---

## 1. The measured baseline

Profiled on the archived campaign `out/svp_recert1` (78 iterations, 81 scans, deterministic
tier, `errors_tier="full"`, bootstrap M=200, 30 boot workers) via `isar profile` plus a
per-task aggregation of the session log's embedded `run_timings`:

| Bucket | Seconds | % of 1772 s wall |
|---|---|---|
| Error-bar attachment — scan fits (`errors_attach_seconds`) | ~452 | 26% |
| Error-bar attachment — master refits | ~408 | 23% |
| Scan-fit remainder (structure enumeration, IC grid, optimization) | ~501 | 28% |
| Two-stage fitting (scans + masters) | ~368 | 21% |
| Everything else (propose/submit/wait/collate/I/O/orchestration) | ~43 | 2% |

(The buckets are disjoint by construction: `errors_attach_seconds` and `two_stage_seconds`
are recorded per-task fields; the remainder is each stage total minus those two. Values are
approximate at the few-percent level; Phase 1 makes this table a built-in report so future
numbers are exact and reproducible.)

Two structural facts:

1. **Error bars are ~49% of wall.** The project's own coverage experiment (STATS_PRIMER §3,
   Figs. 4–6) shows HESSE ≈ profile ≈ bootstrap on ω₀ whenever the fit is healthy, and the
   per-scan `hesse_trusted` flag identifies the unhealthy cases. Most of this 49% is
   therefore *evidence-established* redundancy in the live loop.
2. **Every physical scan triggers a full pooled-master refit** (81 master refits for 81
   scans; `fit_masters` = 35% of wall including its error bars). Most scans change the pool
   only incrementally.

Reproduce the baseline:

```
isar profile --workspace out/svp_recert1
```

(plus the per-task `errors_attach_seconds` aggregation, which Phase 1 turns into a built-in
report column). Any future campaign used as the reference must record the same table before
and after each phase.

**Sprint target:** reference-campaign wall time ≤ one third of baseline by the end of Phase 3,
with **zero steering-decision changes** on shadow replays (defined in §3). Phase 4 is
upside beyond that, gated on its own safety evidence.

---

## 2. Phase 1 — Instrument + free wins

*Risk: none (no steering behavior change). Ships first; everything later is measured
against its output.*

### 2.1 What ships

1. **Per-method timing table in campaign diagnostics.** The data already exists
   (`method_seconds` per fit, `errors/__init__.py`; `run_timings` embedded per `isar_run` in
   the session log). Surface it in two places:
   - `isar profile` gains an errors-split section: per campaign, total seconds and % wall
     for `errors_attach` (scans), `errors_attach` (masters), `two_stage`, fit remainder,
     and per-method (`bootstrap` / `minos` / `profile` / `bayes`) totals.
   - `CAMPAIGN_DIAGNOSTIC.md` (the per-campaign report) gains the same table, so every
     campaign self-reports where its time went.
2. **Adaptive bootstrap early-stop on by default.** `boot_adaptive` exists as a parameter
   (`fit_one`); document its stopping rule (stability of the interval endpoints across
   batches), give it a floor (never fewer than ~48 draws), and enable it by default. The
   exact stopping statistic, batch size, endpoint tolerance, and never-stabilizes behavior
   (run to the M cap) are fixed at implementation time by reading the existing
   `bootstrap.py` implementation first; requirements: deterministic under the seed
   contract, floor respected, cap respected, decision recorded in provenance.
3. **Role-aware bootstrap M.** M becomes a function of role: provisional/live-loop fits
   default to M=50; prior-seeding, pooled-master, and certification fits keep M=200. Roles
   are already distinguishable at the call sites (`isar/session/driver.py` tier plumbing) —
   thread the role explicitly rather than inferring it from incidental state, and record
   the effective M in the fit's provenance so replays and reports can see it.

### 2.2 Done criteria

- The timing table appears in `isar profile` output and in a fresh campaign's diagnostic.
- A rerun of the reference campaign records the new baseline (expect a modest win from
  adaptive M alone).
- Shadow check: the rerun makes the identical scan-proposal and stopping decisions as the
  archived campaign (determinism contract makes this a strict comparison).
- `pytest -m "not slow"` green; new tests for the adaptive stopping rule and role-M routing.

---

## 3. Phase 2 — Escalate-on-distrust error tiers in the live loop

*Risk: low, and bounded by a validation gate. This is the highest-leverage phase (attacks
the 49% bucket).*

### 3.1 Policy

A new effective tier `errors_tier="auto"` (the sprint's default for drive campaigns):

- **Default:** every live-loop fit runs the fast tier — HESSE plus the `hesse_trusted`
  agreement flag. No bootstrap, no profile.
- **Escalate to full** (MINOS + ω₀ profile + bootstrap) when *any* of:
  1. `hesse_trusted` is False;
  2. Γ (or any parameter) is within its agreement-breaking zone of a bound (the Γ-wall
     regime, STATS_PRIMER §4);
  3. the fit's modality is fork / ambiguous, or model selection is close (runner-up Akaike
     weight above a fixed threshold — set at implementation, e.g. 0.1, and recorded in
     provenance so replays see the same rule);
  4. the result will **seed the dispersion prior** or **enter a pooled master /
     certification pool** — "pooled master" meaning any combine-before-divide pooled fit
     publishable as a master revision (the objects consumed by stopping, prior seeding,
     and certification; `isar/session/session_prior.py:add_point()`,
     `isar/drive/supervisor/stopping.py`), not every intermediate pooled quantity.
- **New hard rule (introduced by this phase):** nothing enters `dispersion_prior` or a certification pool
  carrying only a HESSE bar. Rule 4 makes that structural, not advisory.
- **Provenance:** every fit records which tier actually ran and why (escalation reason
  enum), alongside the existing `fit_kind` tag, so reports and replays can distinguish
  cheap-steered from fully-certified numbers.
- **Deferred-bootstrap queue:** when escalation fires for reason 4 (certification-bound)
  and the number is not needed for the *next* steering decision, the full computation is
  queued and executed asynchronously (batch end / iteration boundary) instead of blocking
  the loop. Queue contents are part of the durable checkpoint state — a resume must not
  lose pending escalations. If a queued result is *demanded* early (e.g. a stopping
  evaluation needs it), it is computed synchronously at that point — the queue is an
  optimization, never a correctness hole.

Final certification is untouched: it already re-fits frozen pools at full honesty
(MEASUREMENT_STEERING §7.4); this phase changes only what the live loop computes.

### 3.2 The validation gate (blocks enabling `auto` by default)

Shadow-mode replay on 2–3 archived campaigns (`svp_recert1` plus at least one
`mvp_zone_test`-family workspace): recompute every live-loop fit both ways and compare the
**decisions**, not the intervals — scan proposals chosen, windows, ncounts, stopping
verdicts, prior admissions. Acceptance: zero decision divergences, or every divergence
individually reviewed and judged benign. The coverage study validated parameter agreement;
this gate validates decision agreement, which is the thing that can actually mis-steer a
campaign.

### 3.3 Done criteria

- `errors_tier="auto"` implemented with the four escalation rules + provenance + queue.
- Shadow gate run and its result committed as a short evidence note (per-campaign decision
  diff counts) under `docs/campaigns/`.
- Reference campaign rerun with `auto`: expected large wall-time drop; timing table proves
  where.
- Tests: unit tests per escalation rule; a regression test asserting prior-seeding fits are
  never HESSE-only; resume test covering a non-empty deferred queue.

---

## 4. Phase 3 — Stop refitting the master on every scan

*Risk: moderate, contained (no optimizer changes). Attacks the 35% `fit_masters` bucket.*

### 4.1 Design

- **Pool-delta significance gate.** After a new physical scan folds into a pool, compute a
  cheap delta measure — added exposure fraction plus the predicted change in the pooled
  interval (both available without a refit). Below threshold → defer the master refit;
  above → refit now. The predictor's exact form is an implementation decision, but two
  requirements are design-level: it must be validated against always-refit shadow runs
  before the gate defaults on, and it must be **conservative at low counts** — a scan
  whose pool is below the routine count floor (~20–30 peak counts, per the synth
  calibration) always counts as significant and triggers a refit.
- **Mandatory refit points.** A deferred master MUST be refit before it is consulted for:
  stopping/convergence evaluation at its q, prior seeding, certification manifest
  construction, or the acquisition's master-predictive artifact when that q is a candidate.
  Deferral therefore only skips refits whose outputs nothing would have read.
- **Bounded staleness.** A master may not defer across more than N consecutive deferrals
  (default N=3): the counter increments each time a physical scan folds into that q's pool
  and the gate defers, resets to zero on any refit (gated or mandatory), and is evaluated
  after the delta gate — so the N-th consecutive deferral becomes a refit regardless of
  delta size.
- **Semantics preserved.** Master-authoritative steering (MEASUREMENT_STEERING §7.5) is
  unchanged: physical rows still never provide ω/intervals/votes; the tracker still sees
  only published masters. A deferred master is simply the *previous published revision*
  remaining current slightly longer — a state the system already handles.
- **Validation:** shadow comparison on archived campaigns — deferred-vs-always-refit runs
  must produce identical certified results and identical steering decisions (the mandatory
  refit points guarantee this by construction; the shadow run proves it).

### 4.2 Done criteria

- Gate + mandatory-refit-point enforcement implemented; deferral events logged
  (never silent — each records its delta and reason).
- Shadow validation evidence committed.
- Reference campaign rerun: `fit_masters` share of wall collapses; decisions unchanged.
- Tests: unit tests for the gate, staleness bound, and each mandatory refit point; a resume
  test with a deferral pending.

---

## 5. Phase 4 — Fit-path efficiency (feature-flagged, last)

*Risk: highest — wrong-basin correctness bugs are silent. Everything here ships behind a
flag, defaulting OFF, with automatic fallback. Attacks the 28% scan-fit remainder.*

### 5.1 Design

1. **Warm-starting.** Seed a fit's initial conditions from (a) the same q's previous
   iteration, (b) the branch belief's prediction at this q. A warm-started fit runs a
   **basin-stability check**: if it fails to converge, hits a bound the cold grid would
   explore away from, or its cost exceeds a sampled cold-start's by more than a tolerance,
   fall back to the full IC grid automatically and log the fallback.
2. **IC-grid pruning.** When a prior (dispersion prior / branch belief) is tight, restrict
   the Ω_d scan of the initial-condition grid to the prior's support (± k·σ) instead of the
   full window. Never prunes when the fit is `unconstrained` provenance or the prior is
   floor-width.
3. **Structure-enumeration pruning.** Skip enumerated structures whose admissibility is
   already excluded by physics gates for this scan (work the gates already imply, computed
   once instead of per-fit), and optionally skip high-complexity structures when the
   running campaign has never selected them at neighboring q — but **never prune the
   simplest structures** (null/elastic-only/1-mode): AIC must always see the simple side of
   the ladder, or model selection silently biases complex.
4. **Safety net:** a periodic audit fraction (e.g. every 10th fit) runs cold-start
   full-enumeration regardless and compares; divergence disables the flag for the rest of
   the campaign and flags the report.

All Phase-4 numeric thresholds — the cold-start sampling plan and cost tolerance for the
basin check, k for prior-support pruning, the neighbor radius and history depth for
enumeration pruning, and the audit divergence criteria (different selected structure, or
ω₀ shift beyond the STATS_PRIMER §3.2 agreement band) — are fixed by the regression suite
before the flag may be enabled, and recorded in provenance. Prior-support pruning applies
only to priors with a well-defined Gaussian-equivalent width (the Normal ω₀ prior and
branch-belief predictions); any other prior shape disables pruning for that fit. Audit
divergence additionally flags all since-last-audit fits for review in the report — the
campaign is not rolled back (nothing is ever deleted), it is flagged.

### 5.2 Done criteria

- All behavior behind `fit_fastpath` flag, default off; regression suite comparing
  warm-vs-cold on archived campaigns (identical selected structures and ω₀ within
  tolerance) passes before the flag may default on for dev campaigns.
- Fallback and audit events visible in diagnostics.
- Reference campaign rerun with flag on: remainder bucket shrinks; audit divergences zero.

---

## 6. Guardrails (unchanged invariants this sprint must not erode)

- **The AGENTS.md hard rules** — D3 variance convention, derived anti-Stokes, card
  ownership split, QC-masks-not-deletes, Qt-free layers — none are touched.
- **Fit-vs-decide contract** (MEASUREMENT_STEERING §4.4): tier choice and warm-starting
  affect *how fast* a number is computed, never which cost AIC/goodness-of-fit reads.
- **Fold-only prior admission** and "nothing HESSE-only feeds the prior" (new, Phase 2).
- **Determinism and replay:** every new decision (escalation, deferral, fallback, audit) is
  seeded/derived from persisted state and logged; shadow comparisons rely on this.
- **Frozen Stage II↔III contract:** all new fields (tier provenance, deferral events,
  timing tables) are additive.
- **Certification honesty is constant:** no phase changes what certification computes.

## 7. Out of scope (explicitly deferred, in queue order after this sprint)

1. **Backgrounds** (checkup concern 3) — the committed next priority after this sprint;
   the largest real-data risk. Design directions in the checkup handoff §3.
2. **Scan economy** (fewer/shorter scans; per-second EIG, OCBA dwell) — cut from this
   sprint because it changes scientific behavior, not compute cost; it compounds with
   these speedups when it lands (O13 planned remainder).
3. **Bayesian interval rung, curvature-capable dispersion prior, structure-factor /
   polarization physics** — later workstreams per the checkup's sequencing.

## 8. Sequencing note

Phases land in order 1 → 2 → 3 → 4; each phase ends with the reference-campaign rerun and
its timing table committed, so the speedup narrative is a measured series, not an
assertion. Phase 4 may be dropped entirely if, after Phase 3, the profiling table shows the
remainder bucket no longer dominates — the flag architecture makes it severable.

A routing pointer for this document should be added to the AGENTS.md "Before You Touch X"
table when implementation starts.

---

*Approved by Adrian 2026-07-19 after design discussion; phase order reviewed by a
multi-model council (consensus: order as above, scan economy cut, shadow-decision gate and
deferred-bootstrap queue added).*

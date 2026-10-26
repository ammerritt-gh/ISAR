# Fast Test Campaigns — speedup sprint design

> **Status:** live
> **Verified against code revision:** `d0ea1df` (2026-07-25)
> **Owner:** @ammerritt-gh · **Exit criteria:** in the status block below

**Status: Phase 1 complete and merged (`8fa1219`, 2026-07-20); Phase 2 Partial; Phases 3 and 4
conditional and unstarted. Later phases remain governed by the gates below.**
**Owner: @ammerritt-gh.**

**Exit criteria for the sprint.** (1) Reference-campaign wall time ≤ one third of baseline with
**zero** steering-decision changes on shadow replays (§3 defines the comparison). (2) Phase 2's
remaining work lands — crash-safe asynchronous scheduling and fresh-master `auto` drive
enablement. The fast pass is now the one search of every spectrum and `--errors` defaults to
`fast` (`isar/cli/main.py`); the `auto` default waits on this criterion. (3) Phases 3 and 4 are entered only if their numeric gates below trigger;
if the target is met without them, they are closed as unnecessary, not left open.
Owner intent: this sprint is judged on **development iteration velocity** — synthetic/analytic
test campaigns must get several times faster so that design iteration on everything else
(backgrounds, steering physics, scan economy) speeds up. It is *not* judged on beam-time
economy or final-number accuracy; certification honesty is explicitly preserved unchanged.

**Owner: @ammerritt-gh.** Motivating finding — "the error-tier choice is static", from the
2026-07-19 statistics-contract audit: `docs/STATS_PRIMER.md` §7.

Companion documents: `docs/STATS_PRIMER.md` §3 (the coverage evidence that licenses Phase 2),
`design/steering.md` §7.5 (master-authoritative semantics Phase 3 must preserve).
The Phase 1–2 timing evidence log (2026-07-20) is in git history; its load-bearing numbers are
in §1 below. Tier mix from the first full autonomous run (2026-06): analyze-tier counts over
the run were cheap 59 / fast 22 / full 16.

---

## 1. The measured baseline

Profiled on the archived campaign `out/svp_recert1` (78 iterations, 81 scans, deterministic
tier, `errors_tier="full"`, bootstrap M=200, 30 boot workers) via `isar profile` plus a
per-task aggregation of the session log's embedded `run_timings`:

| Bucket | Seconds | % of 1772 s wall |
|---|---|---|
| Error-bar attachment — scan fits (`errors_attach_seconds`) | 452.373 | 25.5% |
| Error-bar attachment — master refits | 407.690 | 23.0% |
| Fit remainder (structure enumeration, IC grid, optimization) | 501.479 | 28.3% |
| Two-stage fitting (scans + masters) | 367.984 | 20.8% |
| Everything else (propose/submit/wait/collate/I/O/orchestration) | 42.574 | 2.4% |

(The buckets are disjoint by construction: `errors_attach_seconds` and `two_stage_seconds`
are recorded per-task fields; the remainder is each stage total minus those two. Phase 1 made
this decomposition a shared implementation used by the profiler and diagnostic report.)

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

## 2. Phase 1 — Instrument + controlled baseline

*Status: code complete 2026-07-20; the prescribed M=50 versus M=200 drive-control evidence remains
open. Risk: none (no intended steering behavior change). Everything later is measured against it.*

### 2.1 What ships

1. **Per-method timing table in campaign diagnostics.** The data already exists
   (`method_seconds` per fit, `errors/__init__.py`; `run_timings` embedded per `isar_run` in
   the session log). Surface it in two places:
   - `isar profile` gains an errors-split section: per campaign, total seconds and % wall
     for `errors_attach` (scans), `errors_attach` (masters), `two_stage`, fit remainder,
     and per-method (`bootstrap` / `minos` / `profile` / `bayes`) totals.
   - `CAMPAIGN_DIAGNOSTIC.md` (the per-campaign report) gains the same table, so every
     campaign self-reports where its time went.
2. **Adaptive bootstrap is preserved and documented.** It runs in deterministic blocks of 32,
   checks only after at least 64 draws when the cap permits, and stops after two consecutive
   endpoint-half-width changes below 5%. It never exceeds M and is deterministic under the seed
   contract. A provisional cap of 50 therefore runs exactly 50 draws; requested and effective
   counts are recorded.
3. **Role-aware bootstrap M.** `run_batch(..., M=200, scan_M=None)` keeps M for pooled masters
   and certification while provisional physical-scan fits default to `scan_M=50`. The CLI exposes
   `--scan-M`; requested and effective counts are recorded in fit provenance.
4. **Canonical shadow comparison.** The reusable comparator checks proposals, windows, ncounts,
   escalation decisions, prior admissions, stopping state, and final certified outputs after
   removing timestamps and timing-only fields. `isar profile --compare-workspace OTHER` exposes it.

### 2.2 Done criteria

- The timing table appears in `isar profile` output and in a fresh campaign's diagnostic.
- A rerun of the reference campaign records the new baseline (expect a modest win from
  adaptive M alone).
- Shadow check: the rerun makes the identical scan-proposal and stopping decisions as the
  archived campaign (determinism contract makes this a strict comparison).
- `pytest -m "not slow"` green; new tests for the adaptive stopping rule and role-M routing.

---

## 3. Phase 2 — Escalate-on-distrust error tiers in the live loop

For analytic campaigns, scan acquisition is nearly instantaneous, so fitting, error attachment,
and certification will naturally dominate the percentage breakdown. The optimization target is
absolute end-to-end campaign wall time; percentages only locate the elapsed-time critical path.

*Status: Partial 2026-07-20. HESSE-first `auto`, in-place error attachment, provenance, the atomic
promotion-record API, lossless scan/master tier splitting, pending-work recovery, supersession
publication checks, and terminal/certification barriers are implemented. Crash-safe asynchronous
scheduling and fresh-master `auto` drive enablement remain open and blocked by the shadow gate.
The synchronous path is authoritative until reusable candidates have a durable work artifact; a
crash never causes an older revision to publish. The OP7 checkpoint and role-split evidence log
(2026-07-20) is in git history; the measured numbers above are the record.*

### 3.1 Policy

Extend the existing drive-level HESSE-first router rather than introducing a second policy:

- quicklooks remain `cheap`;
- provisional production scans remain `fast`; they are non-authoritative and never receive full
  errors merely because their changed pooled master requires promotion;
- changed master revisions run `auto` after the shadow gate; until then the lossless compatibility
  router preserves the lagged escalation decision but applies `full` only to the master role;
- certification remains explicitly `full` or `bayes`.

This is the OP7 evidence amendment to the original scan-and-master `auto` proposal. In the frozen
controlled batch, 80/81 provisional scans promoted because individual-scan HESSE was untrusted.
Those full scan intervals cannot seat convergence or refresh the prior, so promoting them spent
time without serving an authoritative consumer. Generic `run_batch(errors_tier="auto")` retains
its existing meaning for compatibility; the drive opts into role-specific tiers additively.

`auto` performs structure enumeration and point estimation once, publishes a fresh HESSE master,
and promotes that frozen revision to full errors when any of these deterministic conditions holds:

1. the best-fit HESSE interval is untrusted;
2. Ωd or Γ lies within two HESSE standard errors of a bound, including the existing Γ-bound rule;
3. the runner-up model weight (the evidence posterior since H4) is at least 0.10;
4. no usable best fit or covariance exists.

Promotion reuses the candidates already computed by point estimation; it does not repeat structure
enumeration. A trusted HESSE **fold** may refresh the dispersion prior (O21). Fork-only exclusion and
the fit-vs-decide boundary remain unchanged. Final certification independently refits frozen pools
at an explicit full tier.

Promotion manifests/results are durable and keyed by master content hash, fit fingerprint, tier,
M, and seed. A completed promotion publishes only if its source revision is still current;
superseded results remain provenance and never overwrite newer state. At most one promotion worker
runs, using no more than one third of the worker budget. With fewer than four workers it runs
synchronously at the deterministic iteration barrier. Otherwise one other-q foreground iteration
may run; the promotion becomes logically due at the next boundary and the loop waits there if
needed. Terminal stopping and certification wait for all required promotions.

Every result adds `fit_role`, requested/effective tier, ordered escalation reasons, promotion state,
requested/effective bootstrap M, source revision, and publication outcome without changing any
existing Stage II↔III field.

The profiler and campaign diagnostic report certification separately as a parallel critical path:
pass wall, selected/carried point counts, and the slowest unit/methods. Summed certification-unit
seconds are work, not elapsed campaign wall, and must never be added to the disjoint live-loop table.

Final certification is untouched: it already re-fits frozen pools at full honesty
(MEASUREMENT_STEERING §7.4); this phase changes only what the live loop computes.

### 3.2 The validation gate (blocks enabling `auto` by default)

Shadow-mode replay covers `svp_recert1` plus deterministic healthy-fold and Γ-bound/ambiguous
synthetic campaigns. It compares proposals, windows, ncounts, escalation decisions, prior
admissions, stopping states, and final certified outputs—not timestamps or timing metadata.
Phase 2 becomes the drive default only after zero canonical decision divergences.

### 3.3 Done criteria

- `errors_tier="auto"` implemented with the four escalation rules, reusable error attachment,
  provenance, durable promotion state, supersession guard, and deterministic barrier.
- Shadow gate run and its result committed as a short evidence note (per-campaign decision
  diff counts) under `docs/campaigns/`.
- Reference campaign rerun with `auto`: expected large wall-time drop; timing table proves
  where.
- Tests: unit tests per escalation rule, prior admission from trusted HESSE folds, superseded
  promotion handling, and resume with a pending promotion.

---

## 4. Phase 3 — Stop refitting the master on every scan

*Conditional: implement only when Phase 2 remains above 590.7 seconds and fresh-master point
estimation still exceeds 20% of campaign wall. Compatibility default remains `always`.*

### 4.1 Design

- **Policy.** `[fitting] master_refit_policy = "always" | "gated"`, with
  `master_refit_max_deferrals = 3`, `master_refit_exposure_fraction = 0.25`,
  `master_refit_interval_improvement = 0.10`, and
  `master_refit_min_peak_excess_counts = 30`. Estimate interval improvement as
  `1 - sqrt(C_old / C_new)` and refit when either configured delta threshold is met.
- **Conservative overrides.** Always refit low-count pools, missing/non-result masters, changed
  calibration/policy/branch identity, or a pool without a usable prior revision.
- **Dirty state.** Persist the dirty-pool content hash, exposure, predicted improvement, and
  consecutive-deferral count in machine-owned session state. The previous master may rank
  acquisition while visibly stale, but cannot newly seat convergence or refresh the prior.
- **Mandatory refresh.** Refresh after three deferrals, before certification or a terminal
  decision, and when no clean useful candidate remains.
- **Bounded staleness.** A master may not defer across more than N consecutive deferrals
  (default N=3): the counter increments each time a physical scan folds into that q's pool
  and the gate defers, resets to zero on any refit (gated or mandatory), and is evaluated
  after the delta gate — so the N-th consecutive deferral becomes a refit regardless of
  delta size.
- **Semantics preserved.** Master-authoritative steering (MEASUREMENT_STEERING §7.5) is
  unchanged: physical rows still never provide ω/intervals/votes; the tracker still sees
  only published masters. A deferred master is simply the *previous published revision*
  remaining current slightly longer — a state the system already handles.
- **Validation:** enable `gated` in new development templates only after deferred-vs-always runs
  produce identical canonical decisions and certified outputs. Any unexplained divergence leaves
  it off and ends Phase 3 without weakening fresh-master semantics.

### 4.2 Done criteria

- Gate + mandatory-refit-point enforcement implemented; deferral events logged
  (never silent — each records its delta and reason).
- Shadow validation evidence committed.
- Reference campaign rerun: `fit_masters` share of wall collapses; decisions unchanged.
- Tests: unit tests for the gate, staleness bound, and each mandatory refit point; a resume
  test with a deferral pending.

---

## 5. Phase 4 — Fit-path efficiency (feature-flagged, last)

*Conditional: implement only if the target remains unmet after Phase 3 and fit remainder remains
above 20% of wall. Wrong-basin correctness bugs are silent, so the flag defaults off.*

### 5.1 Design

1. **One compatibility flag.** Consolidate the existing tight-prior IC narrowing and
   `reuse_master_structure` behavior under opt-in `fit_fastpath`, while retaining the old toggle.
2. **Warm starts.** Seed from persisted same-q candidate parameters. Fall back to the complete cold
   grid when convergence/covariance fails, a physics parameter reaches a bound, or a canonical cold
   seed improves deviance by more than `max(2.0, 0.001 × |cost|)`.
3. **Prior pruning.** Keep the existing eligibility gate exactly: only Gaussian-equivalent priors
   with σ≤0.6 meV narrow Ωd to ±1.5 meV.
4. **Enumeration.** Precompute existing admissibility gates once per scan. Do not add neighbor-history
   pruning; null, elastic-only, and one-mode structures always remain available to selection.
5. **Deterministic audits.** Select by revision hash, averaging one full audit per ten accepted fast
   paths. Divergence means a different best structure/modality/verdict, or `|Δω₀|` above 25% of the
   cold authoritative half-width (0.05 meV fallback). Divergence disables the fast path for the
   campaign, retains all results, and flags every fit since the previous audit for review.

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
  affect *how fast* a number is computed, never which cost selection or goodness-of-fit reads.
- **Fold-only prior admission.** Trusted HESSE folds may refresh it under O21; forks never do.
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

Phases are evaluated in order 1 → 2 → conditional 3 → conditional 4; each executed phase ends with
the same-machine reference rerun, timing table, and canonical shadow comparison. A skipped or
rejected conditional phase gets an evidence note stating the measured gate or divergence. The
`AGENTS.md` routing table points performance work here.

---

*Approved by Adrian 2026-07-19 after design discussion; phase order reviewed by a
multi-model council (consensus: order as above, scan economy cut, shadow-decision gate and
deferred-bootstrap queue added).*

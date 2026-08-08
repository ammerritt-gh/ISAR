# ISAR — Design

> **Lifecycle:** Current
> **Authority:** canonical for the architecture and per-capability status table (§15)
> **Provenance:** hand-written
> **Verified against code revision:** `d0ea1df` (2026-07-25)

**Name.** ISAR = **I**nelastic **S**cattering **A**utomated **R**—, with the final R left
deliberately undeclared. The tool *reduces* data at ingest, *resolves* phonons in analysis,
*reviews* scans, and acts in *resolution* mode to solve a measurement problem. Pinning the R to
one word would make the name narrower than the tool, so the floating R is intentional, not
undecided. Because the R is overloaded, **no R-word is used as a stage name in user-facing
material** — see §2 for the canonical vocabulary. Canonical machine name is `isar` (package,
repo, command). Also, fittingly, the river through Munich.

**Status:** Living design, consolidated 2026-07-09 (refinement patches merged; decision ledger
split out to `docs/DECISIONS.md`). Stages I–II (parse/reduce, analysis) are built and validated;
the drive loop (Stage III) is built and has passed its first certification campaign against the
**analytic synthetic tier** (`fn:phonon_dft`; see `docs/campaigns/`). Deterministic TAVI backend
validation ran on 2026-07-10; realistic-clock and McStas validation remain Planned backend-runtime
and fidelity-validation rungs (§9, §15). EIG-4 is the current SVP; same-action scan-window scoring
is implemented through diagnostic scored-shadow inventories, while exact realized-request rescoring
and authority remain Planned. Statuses in this document follow the discipline:
every capability is **Implemented**, **Partial**, or **Planned**, and nothing is described in the
implemented voice unless it is.

**Scope.** Two products in one pipeline: (a) a during-beamtime *triage* tool — ingest scans in
batches, classify, QC, fit phonons, collate by Q, report fast enough to steer the experiment by
hand; and (b) an *autonomous driver* that, given an objective and a budget, runs the triage loop
in closed loop against an instrument or simulator. Each stage is independently useful; together
they are the autonomous workflow. A human reviews everything afterward; ISAR is not the system of
record for published numbers.

---

## 1. Purpose & operating context

**What this is.** Fast, trustworthy-enough feedback during a beamtime — "this Q point looks
done," "this scan is junk," "this peak is off the dispersion, re-measure" — so beam is spent
wisely; and, in drive mode, the spending of that beam itself.

**Operating facts (fixed by us):**
- Runs on a **separate box** from the beamline. Scans arrive in **batches**, and **only when
  finished** (no mid-acquisition truncation to defend against).
- LLMM is **co-located** on the processing box; reachability is not a concern.
- Resolution is measured or computed **early** and ingested before any phonon fit is trusted.
- A **human reviews** every batch later. The tool flags; the human decides.

**Goals**
1. Per-batch turnaround fast enough to be useful between scans.
2. Every reported number carries provenance (resolution version, dispersion context, and — for
   pooled results — the member scans; see §7).
3. Generalizes across instruments — proven against **Sector 30 (HERIX, IXS)** and **IN8 (ILL,
   TAS)**, which differ in almost everything except the DHO core.
4. Conservative by construction: when unsure, **flag, don't delete; hold, don't guess.**

**Non-goals**
- Final, paper-grade fits (done later, deterministically, from frozen config).
- Replacing human judgment on data inclusion or peak assignment.
- A single universal parser (impossible; see §5).

## 2. The three stages (canonical vocabulary)

The top-level narrative is the user's workflow, in three stages. Repo modules map beneath them:

| Stage (user-facing) | What it does | Modules |
|---|---|---|
| **I. Parse** | raw facility formats → unified records, QC'd, calibrated | `parsers`, `reduce`, `calibrate` |
| **II. Fit & Verdict** | collate, fit, certify intervals, sign off per scan | `reduce/collate`, `analyze`, `report` |
| **III. Steer** | objective + budget → autonomous campaign | `drive`, `session` |

Vocabulary rulings (full ledger in `docs/DECISIONS.md`):
- **"Review" = ISAR's own per-scan sign-off**, one of *fit-and-use* / *collate-only* /
  *not-a-measurement* (alignment, calibration). It never means human review in these docs. The
  dispositions realize it: `resolved`/`flagged` = fit-and-use; `branch_continuation` =
  collate-only; `non_result`/held = not-a-measurement or not usable alone.
- **"Reduction" = everything raw→useful**: parsing *and* collation (matches `isar/reduce/`).
- The module historically called `review` is referred to as **collate** in documentation.

## 3. Core principles

**P1 — LLM proposes, deterministic code disposes, decisions freeze, results are scored.**
No LLM sits in the runtime path of a number that matters. LLM proposals freeze into config
artifacts; the deterministic fitter runs from frozen config and is re-runnable forever without
any model. Fallbacks are honest: a down tier degrades to deterministic-only with the affected
scans flagged, never a stalled batch.

**P2 — The experiment card is the central object.** One artifact holds every per-experiment
policy and all accumulated state; the core pipeline is instrument-agnostic and reads everything
from the card (§5).

**P3 — Per-experiment parser → normalized scan → instrument-agnostic core.** The byte format
varies; the interface does not. A parser plugin emits a normalized `ScanRecord`; nothing
downstream knows the instrument.

**P4 — A scan is an event that either produces a result or mutates the session.** Measurements
yield fit results. Alignment, temperature changes, and Bragg/resolution scans are
*context-updates* that mutate session state — they are not noise; they carry the state the
measurements depend on.

**P5 — Conservative cleaning.** A human reviews later, so over-inclusion is the cheaper error.
Borderline points are flagged, not removed. Refusing to report a number we can't stand behind is
safer than emitting a plausible-but-wrong one.

**P6 — Provenance on every result.** Each fit records which resolution version and how much
dispersion context it used; pooled results enumerate their member scans; rejections are marks,
never deletions. The full chain — delivered number ← pooled scans ← kept/rejected passes — must
be walkable from the artifacts alone.

## 4. Architecture overview

```
                ┌──────────────────────────────────────────────────────────┐
  batches  ──▶  │ INGESTION    id-keyed, idempotent, gap-detecting          │
                └───────────────────────────┬──────────────────────────────┘
                ┌───────────────────────────▼──────────────────────────────┐
   Stage I      │ PARSE + CLASSIFY (per-experiment plugin)                  │
                │    → measurement | → context-update (mutates session)     │
                ├───────────────────────────────────────────────────────────┤
                │ QC / REDUCE   flag-not-delete; combine-before-divide      │
                └───────────────────────────┬──────────────────────────────┘
                ┌───────────────────────────▼──────────────────────────────┐
   Stage II     │ COLLATE by card key  →  FIT (DHO ⊗ resolution)            │
                │ model selection · interval ladder · branch posterior      │
                │ per-scan verdict + disposition (+ provenance)             │
                ├───────────────────────────────────────────────────────────┤
                │ REPORT   triage table · campaign diagnostic                │
                └───────────────┬───────────────────────┬──────────────────┘
                                │ file contract          │
                ┌───────────────▼──────────┐   ┌─────────▼─────────────────┐
   Stage III    │ DRIVE  propose→validate→ │   │ HUMAN REVIEW              │
                │ run→analyze→decide→stop  │   │ corrections re-enter as   │
                │ (via `isar run` subproc) │   │ session mutations          │
                └──────────────────────────┘   └───────────────────────────┘
```

**Persistence.** All state lives in a per-experiment *workspace* as plain JSON, files-as-truth:
the card's policy half is human-authored TOML (the machine never writes it); accumulated state is
`card.state.json` (experiment-owned) plus `session.json` (machine ledger, deletable/rebuildable).
Restart-safety comes from idempotency: every run re-parses everything and re-fits only
new/held/changed scans. No database, no checkpointer dependency. Growing-file batch payloads
(S30) are handled by full re-parse + dedup by `(experiment_id, scan_number)`, keeping the last
occurrence. *(Implemented; see `docs/WORKSPACE_AND_CLI.md` for the full treatment.)*

**The Stage II ↔ Stage III boundary is a frozen file contract.** The driver consumes analysis
only via the `isar run` subprocess and its products — `report/results.csv`, `<workspace>/report/report.json`,
`card.state.json` — and never imports fit internals. The pre-release v1→v2 objective/campaign reset
was authorized on 2026-07-17 (O12); additive-only evolution resumed from v2, and is superseded for
contracts the SCENE program rebuilds (DECISIONS H1): `isar.campaign-program/1` replaces the
objective envelope outright, with compatibility judged against the SCENE world suite until the
program closes. This boundary is what lets the stages be independently useful and independently
testable, and it is what makes the asynchronous loop (§8) possible.

## 5. Data model

**Normalized scan (`ScanRecord`, `isar/schema/`).** What the parser emits; downstream sees only
this. Every load-bearing field carries a provenance tag — reported-vs-computed Q, setpoint-vs-
sensor temperature, monitor-vs-time counting — because the silent-corruption modes we have
actually hit (stale geometry, dead sensor) are exactly the ones the tags let QC reason about.

**Raw-energy validity is a per-card policy, not an inference**, with three states in
`raw_abscissa_kind`: `absolute` (fixed-kf TAS — IN8, HB-3), `needs_zero` (raw column is energy
but the zero is uncalibrated — S30 HERIX), and `needs_conversion_and_zero` (raw abscissa is
monochromator temperature — ESRF ID28). A no-elastic-line scan on a `needs_zero` instrument
without a zero is a flagged non-result, never E0 = 0.0 by default.

**The experiment card (centerpiece).** One artifact per experiment: policy authored at beamtime
start (parser plugin, patterns, Q source, energy policy, counting/normalization, temperature
policy, resolution mode + convolution operator, collation key + merge policy, lattice, column
maps) and state accumulated during the run (resolution versions, dispersion prior, sample map,
energy-zero series, exclusions, append-only corrections). The test of the abstraction is that
both S30/HERIX and IN8/TAS fit without special-casing the core — they do; the worked two-column
example lives in `docs/SCHEMA_LANDSCAPE.md` and the card files themselves. Everything once
hardcoded in per-instrument scripts (temperature snap lists, excluded-scan sets, monitor
references, path conventions) is a card policy, not code.

**Collation** is a deterministic groupby on the card's `collation.key` ((sample, Q, T) on S30;
(Q, T, field) on IN8) with per-card merge policy: `separate` keeps duplicates as distinct points;
`merge` uses the combine-before-divide primitive — sum kept counts AND kept monitors, then divide
downstream if chosen, never average already-normalized rates, no zero-count floor.

## 6. Stage I — Parse

**Ingestion.** The unit is "a scan with a stable id." Sequence/gap detection (a hole in the
dispersion nobody notices for hours is the worst failure for a speed tool), idempotent dedup by
id, ordering by scan number not arrival. *(Implemented.)*

**Classification** uses whatever the instrument declares (IN8's `COMND` is the classification;
S30's is inferred) and never leaks downstream — it is the parser's job. Sample identity is a
state variable set by the most recent alignment event; a measurement arriving with no sample
context is held, never guessed. Resolution is a bootstrap gate: phonon fitting is not trusted
until a resolution version exists for the analyzer; recalibration is first-class and versioned.
*(Implemented.)*

**QC / reduce — the trust story.** This paragraph is the answer to "how do I know it isn't
destroying data," and it is enforced in code, not aspirational:
- Rejection only ever **marks** (`miniscan_reject` masks); nothing is deleted. Zero-flux points
  are masked-but-kept. Every rejection leaves a per-point flag with the count and magnitude of
  what was removed.
- The default cosmic-ray rule is **Poisson-aware leave-one-out**: a pass is rejected only if it
  exceeds the *other* passes by k·√level AND those others are at a non-trivial level. At
  near-empty points it rejects nothing — it refuses to delete what it cannot distinguish from a
  weak real feature, deferring to adjudication. (The blind σ-clip exists only to reproduce
  beamline behavior for validation.)
- The stored signal is **raw counts with Poisson variance**; the monitor rides along and flux
  normalization is a reversible downstream choice, never baked in. This is the same decision as
  the Baker–Cousins cost (§7): the likelihood is only valid on raw Poisson counts.
- **Energy zero (S30)** is keyed by time, interpolated to the scan's timestamp; a missing zero is
  a flagged non-result. **Resolution** is versioned, this-beamtime's-values, never archival
  constants — Γ is the linewidth we report and a wrong resolution biases it directly.
*(Implemented; module docstrings in `isar/reduce/` carry the details.)*

**Flag adjudication (LLM, off the critical path).** The deterministic layer flags; the LLM
adjudicator *decides* each flag (mask / keep / drop_detector / hold_for_human) with a reason and
a per-scan narrative — but decisions are **recorded only** (`record.adjudication` plus the
append-only `card.state.corrections` audit); nothing is applied until the correction loop under
human review. Ambiguity is held for the human; a flagged scan is never silently dropped.
*(Implemented, opt-in; see `docs/ADJUDICATION.md`.)*

## 7. Stage II — Fit & Verdict

Every fit answers three separate questions, in order. (The persuasion-grade treatment with
reproducible demonstrations is `docs/STATS_PRIMER.md`.)

**Question 1 — what do we minimize: the Baker–Cousins Poisson likelihood cost.** Not Neyman
(σ²=N biases peak intensities low and cannot handle empty bins) and not Pearson (σ²=μ biases
high). Baker–Cousins is exact at low counts and converges to χ² at high counts, so
`cost_per_dof ≈ 1` keeps its meaning. Valid only because Stage I preserved raw counts.
*(Implemented.)*

**Question 2 — which model wins: the evidence posterior (H4).** Candidate structures (peak
count, elastic vs anti-Stokes forms) are fit and compared by per-candidate Bayesian evidence
(Laplace with escalation to nested sampling, uniform prior over admissible structures); an extra
peak must buy its parameters in marginal likelihood — never raw χ², which systematically fits
noise as phantom phonons. AIC and Akaike weights persist per candidate as the recorded
diagnostics lane. *(Implemented: `best_model`, `n_modes`, `best_model_posterior`,
secondary-peak columns.)*

**Physics constraints are encoded, not rediscovered per scan.** Detailed balance (I_AS/I_S =
exp(−ℏω/k_BT)) decides whether an anti-Stokes peak is even admissible before one is fit; symmetry
about zero disambiguates given a trustworthy zero (coupling assignment to calibration on
`needs_zero` instruments); dispersion smoothness makes neighbors a strong prior and flags peaks
off the curve. Resolution differs in *dimensionality*: near-1D Voigt convolution is legitimate on
IXS; a TAS needs the 4D tilted-ellipsoid treatment (`resolution.convolution = tas4d`) because the
apparent linewidth depends on the dispersion slope cutting the ellipsoid. *(Voigt path
Implemented; `tas4d` Planned — TAS Γ is carried as provisional until it lands; ω is trustworthy.)*

**Question 3 — how wide: the interval ladder.** Hessian (curvature, symmetric-parabola
assumption — the quick-look and fast tiers; `hesse_trusted` exists because the assumption fails
constantly; **never certifies**) → profile likelihood (asymmetric, analytic) → **bootstrap**
(frequentist-by-simulation; the certifying tier) → Bayesian posterior credible intervals
(**Implemented, diagnostics-only** under `--errors bayes`; certification and confidence never
read them, and promotion to a certifying rung waits on coverage validation). Terminology
discipline: bootstrap/profile give confidence intervals; "credible" belongs to the Bayesian
tier alone.

**The γ-at-bound pathology.** Near a parameter bound the Hessian lies (its parabola crosses the
wall); *on* the bound the bootstrap may degenerate. That makes Γ resolution-limited, but does not by
itself make ω unknowable. A pooled master may certify when its model-robust ω interval remains
identified after marginalizing Γ and model uncertainty; the GUI reports
`linewidth_state=resolution_limited`. Physical-scan interval failures remain visible diagnostics and
never vote on convergence or abandon a q. Escape hatches include the branch-conditional Γ freeze
(Implemented) and the Bayesian tier. *(Master-authoritative rule Implemented 2026-07-19 — O18.)*

**Where "Bayesian" is earned.** The branch layer: soft priors from the dispersion model
(`prior_omega`/`prior_sigma`) make the fit a MAP estimate, and acoustic-vs-optic assignment is a
genuine posterior over branches with `assignment_entropy` as the honesty meter (near ln 2 =
coin flip, do not deliver). Nothing else in the pipeline is called Bayesian. *(Implemented.)*

**Verdict & disposition.** Computed confidence (agreement across model attempts, parameter
errors, distance from the running dispersion, provenance-weighted), never LLM-self-reported.
Each scan gets a verdict and a disposition realizing the review taxonomy (§2). Pooled "master"
fits carry synthetic scan ids and must enumerate their `members`. *(Confidence Implemented;
member enumeration Implemented 2026-07-10.)*

**Residual-driven refit (LLM).** On a failed/poor fit, a vision-capable tier reads the fit +
residual figure and proposes a structural fix — after deterministic attempts, off the critical
path, batched. *(Planned; the per-scan figure rendering it needs is a prerequisite shared with
the diagnostic report.)*

## 8. Stage III — Steer

Given an objective and a budget, the driver runs propose → validate → run → analyze → decide in
a loop against a backend (§9), consuming analysis only through the file contract (§4).

**Objective (v1): per-point tolerance** — each grid q to a calibrated 68% halfwidth ≤ tol,
compiled from a natural-language goal into a frozen `isar.campaign-program/1` whose one `targeted`
step carries that objective as its payload (`goal_to_program`). The model-level
objective (ARIANE-style: maximize improvement of the dispersion model, interpolation allowed) is
a named future tier. *(Per-point Implemented; model-level Planned.)*

**Two clocks, two limits.** `budget` is an optional measurement budget (`counting_seconds` or
`neutrons`); `analysis_limit_seconds` is a cumulative monotonic-wall guardrail around serialized
top-level analysis operations. Deterministic campaigns default to no measurement budget and one hour
of analysis. Measurement-dominated tiers default to eight measurement hours and no analysis limit.
Durable ten-second `analysis_clock` checkpoints replay by stable operation ID, so worker time is not
multiplied and resume neither resets nor double-charges the ceiling. An acquired scan always finishes
or checkpoints its current reduction wave. *(Implemented 2026-07-19 — DECISIONS O19.)*

**Acquisition.** The combine-before-divide `(q,T)` master is the sole scientific decision unit.
Physical fits remain inspectable provenance but never vote on ω, branch identity, convergence, the
dispersion prior, or stopping. Each usable master retains a bounded full-parameter, model-weighted
predictive ensemble; candidates rank by expected information about target ω plus model/branch identity,
per predicted analysis second in deterministic mode (per measurement cost in measurement-dominated
tiers). Missing predictive draws trigger one full-tier master escalation before another scan. Low gain,
poor fits, ambiguity, and count caps remain visible strategy diagnostics but do not abandon q. Missed
windows recenter from the pooled spectrum rather than the last individual fit. *(Implemented
2026-07-19 — DECISIONS O18/O20.)*

**Branch-identity-aware acquisition** *(branch-hypothesis slice Implemented 2026-07-17;
`level`/`continuum` plugins Partial — DECISIONS O6–O12, MEASUREMENT_STEERING §4.5,
`docs/HYPOTHESES.md`)*: scan value is two-term (target-ω₀ EIG +
λ × branch-assignment-entropy reduction); user-declared excitation hypotheses in
`card.policy.toml` weight — never force — belief seeding, classification, opening moves, and
energy windows (which stop unconditionally excluding the rival branch); the compiled path
becomes first-guess (driver-inserted classification q, which never seat convergence); rivals
are measured only to a looser differentiation tolerance; hypothesis mismatch is detected and
flagged, and the resolved hypothesis is content-hashed into `card.state.json`.

**Campaign outcomes.** `met` means every requested master certified. `incomplete` means a measurement
budget, analysis limit, or global iteration limit ended the run. `infeasible` is reserved for an
independently established geometry/instrument/calibration impossibility or a tolerance below a
declared floor. Operator stop and pending recovery remain explicit. Per-q master states are
`unmeasured`, `uncertified`, `certified`, or `infeasible`; diagnostics include diminishing returns,
nonshrinking intervals, model ambiguity, pool inconsistency, resolution-limited linewidth, and
analysis failure. Diagnostics never become autonomous terminal states. *(Implemented 2026-07-19.)*

**Contract (v1): stop and report**, best-effort deliveries included. Mid-campaign renegotiation
("I can do ±0.15 meV at these four q within budget — accept?") is future work and the design
keeps the door open: the objective spec is data, so re-specification mid-run is a session
mutation like any other. *(Planned.)*

**Loop shape (target): asynchronous, one-step-stale.** While scan *n* counts, fit *n−1* and
queue proposal *n+1* for the instant the instrument idles. On real instruments counting dominates
fitting by 20–100×, so the compute pipeline hides entirely inside counting; "fitting-bound" is a
simulation-tier artifact only. *(Planned; the file contract already supports it.)*

## 9. Simulation tiers & TAVI

The certification ladder: **analytic synth** (`fn:phonon_dft` truth; instant, deterministic;
Stage III's first certification ran here) → **TAVI as backend** (McStas-faithful physics; the
driver cannot tell it from the instrument) → **instrument** (PUMA/MLZ, IN8/ILL, S30/APS).

**Backend-runtime and fidelity validation** — the TAVI campaign uses the same objective and driver
as the analytic certification, TAVI answering scans, and **realistic clocks**: counting time reported
as the instrument would report it, so the wall-clock budget semantics (§8) are exercised before they
meet a real night. TAVI will surprise in ways the analytic tier cannot (resolution tails,
backgrounds, count-rate reality); better simulated than actual. *(Partial — the deterministic rung ran
2026-07-10 as the S3 re-certification, `docs/campaigns/2026-07-10_SVP_RECERT.md`; realistic
clocks and the McStas rung remain Planned.)* EIG-4, not TAVI/McStas, is the current SVP.

**Backend-first integration.** Oracle use (simulate a proposed scan before spending beam) and
dry runs are the same interface invoked off-path; getting the backend right gets them free.

**TAVI is the single resolution authority**, owning both the fast Cooper–Nathans/Popovici
solution and the McStas gold standard; ISAR consumes versioned resolution artifacts
(`resolution_source`, `resolution_version` on every result) and grows no resolution code of its
own. The `tas4d` convolution (§7) consumes TAVI-supplied ellipsoids. *(Ruling final; wiring
Planned.)*

## 10. LLM role & routing

No LLM in the runtime path of a number (P1). Placement:

| Task | LLM? | Notes |
|---|---|---|
| Parse / classify / collate / QC removals | No | deterministic; LLM may explain flags, never decide removals |
| Flag adjudication | Yes, off-path | decide–record–defer, human-gated (§6) |
| IC + candidate peak-structure proposal | Yes | seeded by the dispersion prior first; escalate hard cases |
| Residual diagnosis on failed fits | Yes | vision tier, after deterministic attempts, async (Planned) |
| Triage / explanation prose | Yes | extract deterministic facts, narrate only |
| Goal → objective spec | Yes | frozen into the campaign config before the run |

Everything routes through LLMM (structured-output schemas, tier profiles with owned fallback,
budget + telemetry). Latency discipline: any LLM consult is async and never in the per-scan
critical path. Degraded mode: deterministic-only triage proceeds with "no LLM consult" flags;
the batch never stalls.

## 11. Human in the loop & corrections

The report is the product; the return channel is its other half. Human review actions ("223 is
bad," "that ω was wrong") re-enter as first-class session mutations — card edits + re-run with
ledger invalidation — so the next batch's prior learns from the expert. Adjudicated corrections
apply only through this channel (§6). *(Minimal form Implemented; richer review surface Planned.)*

## 12. Reporting & logging

**Two report layers.** The **triage table** serves the during-beamtime user: per-scan verdicts,
collate-only rows collapsed to counts, `interval_reliable=False` rendered as `(unreliable)` and
never as a numeric range, pooled masters labeled as such (a rejected master is "pooled —
uncertifiable," never junk). The **campaign diagnostic** serves the architect: objective, outcome
at a glance, loop narrative, failure dossier with per-zone evidence and figures, defect list,
provenance table — assembled deterministically from the workspace's own artifacts. Spec:
`docs/CAMPAIGN_DIAGNOSTIC_SPEC.md`. A user-facing campaign report is a later, separate artifact.
*(Triage Implemented with presentation fixes open; diagnostic Implemented 2026-07-10 —
`isar diagnose`, rendered at every campaign end, characterization-pinned against the frozen
MVP-16 workspace.)*

**Logging is part of the trust contract.** A question the logs cannot answer is a logging
defect, not a rendering problem. Required events: `escalate_fit` with outcomes, structured
`decide.action`, recenter guard decisions, the stop-reason enum. Figures are the natural record
of a run: per-scan fit+residual PNGs at fit time (full set buried; failures always surfaced;
representative successes only). *(Implemented 2026-07-10, with one deliberate refinement: the
per-scan PNGs are reconstructed at diagnostic-render time from stored params + a Stage-I re-parse
(`reconstruct_curve`, no refit) rather than drawn at fit time — same buried artifact, and the
campaign path stays matplotlib-free.)*

## 13. Validation & scoring

**The harness came first and stays first.** Gold standard: FeSeS_1, 79/83 human-validated fits;
every pipeline change is scored per-scan against it. Campaign certification adds the scorecard:
truth-source comparison, per-q buckets, pulls, **coverage** — observed 68% interval coverage
against known truth is both the internal health metric and the external persuasion instrument
(the seminar argument for practitioners raised on Neyman + Hessian is a reproducible coverage
experiment, not a lecture — the gallery that makes it is `docs/STATS_PRIMER.md` §3 and
`docs/stats/`). *(Implemented;
coverage currently exposes the γ-at-bound work as designed.)*

## 14. Workspaces & hygiene

Every entry point writes a `run_manifest.json` (purpose, command line, config hashes, date,
products, outcome). `out/` is disposable under a promote-or-archive policy; blessed baselines
(the frozen certification workspace, gold-check evidence) are archived outside it and pinned by
characterization tests. Pedagogical and validation figures live under `docs/`, generated by
script from frozen references — never stranded in `out/`. *(Manifest + policy Planned —
DECISIONS W1–W3.)*

## 15. Status & open questions

| Capability | Status |
|---|---|
| Stage I: parsers (S30, IN8), reduce, calibration state, card | Implemented |
| Adjudication (decide–record–defer) | Implemented (opt-in) |
| Stage II: Baker–Cousins fit, evidence selection (H4; Akaike diagnostics retained), branch posterior, verdicts | Implemented |
| Interval ladder through bootstrap; `interval_reliable` flag | Implemented |
| Uniform `interval_reliable` convergence rule | Implemented (2026-07-10) |
| Pooled-fit member provenance | Implemented (2026-07-10) |
| Persisted content-addressed pooled spectra + master/member browser | Implemented (2026-07-19; additive v2 fallback for legacy curve-only masters) |
| Master-authoritative assessment, prior, branch tracking, and stopping | Implemented (2026-07-19) |
| Pooled member homogeneity bootstrap | Implemented (2026-07-19; seeded exposure-scaled Poisson deviance, α=0.01) |
| Full-parameter master-predictive artifacts + model-discriminating information ranking | Implemented (2026-07-19) |
| γ-at-bound calibration v2; Γ prior | Planned |
| `tas4d` convolution (TAVI-supplied ellipsoids) | Planned |
| Stage III: per-point objective, EIG acquisition, stop-and-report | Implemented (analytic tier certified) |
| Same-action scan-window EIG | **Partial:** predictive `/2`, arbitrary-grid evaluation, candidate generation, executed-grid identity, target projection, pure candidate scoring, Slice 6 diagnostic scored-shadow inventories, Slice 7 exact realized-request rescoring/provenance, the accepted retained Slice 8 tools-only gate, and the O41-restricted Slice 9 opt-in authority path Implemented. Authoritative selection is **Partial**: demonstrated in deterministic simulation and opt-in today; the O42 release review is retired by H1, and the selector becomes default-on once the SCENE world suite exercises it (the flip lands at SCENE-4, not with this row). The selection rule is a **symmetric per-neutron comparison** (O43) — the two candidates are compared on information per neutron and the winner is taken, with ties and partition-offset disagreements broken by cost rather than by retaining the survey. There is no polarity to switch: neither candidate is a default. The survey still wins the first pass across Q, but structurally, because an unmeasured q has no prediction to centre a core on and so offers no core candidate. On a cold-start campaign every scored decision after the first pass selected the centred core. Known limitation: the QoI partition (0.5 meV) is far coarser than the design prediction sigma (median 0.0182 meV), so once a fit exists the comparison is frequently a tie decided on cost rather than on information — accepted deliberately at EIG-4 close, see `docs/EIG_STEERING_DESIGN.md`. EIG-4 is complete. |
| Acoustic zone-centre exclusion | **Implemented** (O44, 2026-08-05): an acoustic branch has ω(q→0) → 0, so at the reduced-q origin the excitation is degenerate with the elastic line and is not a measurable target at any exposure. That q is not precision-coverage eligible from the first iteration, on the branch type and the tracker's published `reduced_q_origin_index` alone. Branch-conditional: an optic branch has a finite ω at the zone centre and stays eligible there. An acoustic campaign's "N measurable grid q converged" is therefore one lower than its path length. |
| Exposure-aware pooling + tolerance-driven dynamic `ncount` | **Partial** (O13, 2026-07-18; raw counts/ΣN retained and deterministic interval scaling/brakes/replay Implemented, Fisher/sample-based dwell optimisation Planned) |
| Bayesian optic cold-start + selector-driven provisional evidence | **Partial** (2026-07-18; posterior assignment, neighboring-q evidence and probabilistic rejection implemented, sampled Stage-C MI Planned) |
| Separate measurement budget + cumulative analysis guardrail | Implemented (2026-07-19; durable ten-second checkpoints) |
| Stop-reason enum (full) + R4 log events (escalate_fit, recenter guard, decide.action) | Implemented (2026-07-10) |
| Live activity events, newest-first GUI feed, per-q status ribbon | Implemented (2026-07-19) |
| Master-only restart-invariant final certification | Implemented (2026-07-19; one requested pooled master/unit, completion-order checkpoints, complete-generation hash marker, append-only revisions) |
| `diminishing_returns` strategy diagnostic (nonterminal) | Implemented (2026-07-19; O18) |
| Async one-step-stale loop | Planned |
| TAVI backend-runtime and McStas fidelity-validation rungs | **Partial** (deterministic rung 2026-07-10; realistic clocks + McStas Planned) |
| Campaign diagnostic report (`isar diagnose`) | Implemented (2026-07-10) |
| Fit-free ingest to the ledger (`isar ingest`, `ingested` status) | Implemented (2026-07-12) |
| Per-unit data files (`data/ingest/`, `isar.unit_data/1`) + ingest provenance enrichment | Implemented (2026-07-12) |
| Bayesian interval tier (`--errors bayes`: nested-sampling ω₀/Γ credible intervals, posterior samples, three-method agreement) | Implemented, **diagnostics-only** (bootstrap still certifies; coverage validation and promotion to certifying authority Planned) |
| Renegotiation protocol | Planned (design door open) |
| Model-level (ARIANE-style) objective tier | Planned |
| SCENE program: target-free multi-phenomenon discovery — catalogue-driven identity-conditional evidence, cluster-factorized scene posterior, question layer, scene-EIG | **Partial** (H1 constitution 2026-08-08; `docs/SCENE_DESIGN.md` governs; synthetic world-suite foundation Implemented 2026-08-08: 12 seeded adversarial worlds + ensemble runner in `isar/synth/worlds.py`; SCENE-1 PR-a Implemented 2026-08-08 (H3): six-entry phenomenon catalogue driving enumeration, phonon-less nulls `elastic+bg`/`bg` as first-class candidates, `elastic_only` verdict with the 0.5 categorical-humility cap + nested-LRT significance gate, dormant transport/prior/constrained-variant declarations; Laplace evidence PR-b and persistence PR-c Planned) |
| Branch-identity-aware acquisition (two-term scan value; excitation hypotheses; first-guess path) | Implemented for branch hypotheses (O6–O9/O11–O12, 2026-07-17; level/continuum plugins remain O10-Partial) |
| Connected tracked-branch steering: identity vs precision adequacy, single same-q budget, rival-trajectory reconnection, controller invariant | Implemented for the tracked optic path (O27, 2026-07-21; OP10 replay reaches `met` 21/21). `identity_saturated` unit-tested but campaign-unproven; AP campaign replays outstanding, so O25/O26 remain Partial |
| Action-inventory control plane: registry-driven generate/evaluate/select, three-stage lifecycle, failure-isolated exhaustion proof, evidence-sink contract, target/identity search-domain split, predicted-rival identity probe | **Partial** (O28, 2026-07-23; control plane + `narrow_rival_probe` Implemented and AP6 replay passes at the `enumerate_actions` reconstruction level, strict/migrated modes. Full driver-level AP6 campaign replay outstanding; zone-aware boundary probe deferred pending a cross-zone structure-factor consumer) |

**Genuinely open:** wrap an existing 4D-convolution engine (ResLib/Tobyfit lineage) vs implement
the Cooper–Nathans projection against TAVI ellipsoids directly — decide when `tas4d` is
scheduled. Cross-T dispersion-prior pooling near transitions — revisit with data.

## 16. Document map

**The map lives in one place: [`docs/READING_GUIDE.md`](docs/READING_GUIDE.md)** — every document,
with its lifecycle, so a reader can tell a current contract from a plan from a historical record.
The per-file register behind it is [`docs/DOC_INVENTORY.md`](docs/DOC_INVENTORY.md).

This section used to carry a second, independently maintained table. It drifted from the reading
guide's, which is exactly the failure the single-map rule prevents.

---

## Appendix — section concordance (pre-consolidation references)

Code comments and older docs cite the pre-2026-07-09 numbering. Map:

| Old | New | Old | New |
|---|---|---|---|
| §1 purpose | §1 | §7.5 analyze | §7 |
| §2 principles P1–P6 | §3 (IDs unchanged) | §7.6 collate | §5 |
| §3 architecture | §4 | §8 physics | §7 |
| §4 event stream | §5, §6 | §9 LLM routing | §10 |
| §5 scan schema | §5 | §10 human loop | §11 |
| §6 card | §5 | §11 harness | §13 |
| §7.1 ingestion | §6 | §12 confidence | §7 |
| §7.2 parse/classify | §6 | §13 open questions | resolved items → `docs/DECISIONS.md`; live ones → §15 |
| §7.3 QC | §6 | §14 build order | superseded by §15 status table |
| §7.4 calibration | §6 | §15 future phase | promoted to §8 |

Decision IDs (P*, D*, U*, and the DECISIONS ledger's V/P/R/S/O/T/W series) are stable and safe to
cite from code. Docstrings citing old §-numbers (e.g. `§7.5`, `§0.4` from UNIFICATION_DESIGN's
numbering) remain resolvable via this table and that document; a reference sweep is optional
cleanup, not required.

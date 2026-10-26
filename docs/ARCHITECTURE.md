# ISAR — Architecture

> **Status:** live
> **Authority:** canonical for the architecture and per-capability status table (§15)
> **Verified against code revision:** `5cf3408` (2026-08-15)

**Name.** ISAR = **I**nelastic **S**cattering **A**utomated **R**—, with the final R left
deliberately undeclared. The tool *reduces* data at ingest, *resolves* phonons in analysis,
*reviews* scans, and acts in *resolution* mode to solve a measurement problem. Pinning the R to
one word would make the name narrower than the tool, so the floating R is intentional, not
undecided. Because the R is overloaded, **no R-word is used as a stage name in user-facing
material** — see §2 for the canonical vocabulary. Canonical machine name is `isar` (package,
repo, command). Also, fittingly, the river through Munich.

**Status:** Living design, consolidated 2026-07-09 (refinement patches merged; the decision
ledger of that era has since been retired into the owning design docs — originals in git history). Stages I–II (parse/reduce, analysis) are built and validated;
the drive loop (Stage III) is built and has passed its first certification campaign against the
**analytic synthetic tier** (`fn:phonon_dft`; see `campaigns/`). Deterministic TAVI backend
validation ran on 2026-07-10; realistic-clock and McStas validation remain Planned backend-runtime
and fidelity-validation rungs (§9, §15). The targeted lane's placement is the EIG planner: it
chooses Q, energy window and exposure by expected information per neutron over a hypothesis map and
an intensity map, and it is the only thing that places a scan (`design/eig.md`; §15). Statuses in
this document follow the discipline:
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

Vocabulary rulings:
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
The canonical store is a **lossless source, never a flat export target**: plain JSON chosen so
every downstream representation can be regenerated from it, and nothing is baked in that cannot
be reversed. Restart-safety comes from idempotency: every run re-parses everything and re-fits
only new/held/changed scans. No database, no checkpointer dependency. Growing-file batch payloads
(S30) are handled by full re-parse + dedup by `(experiment_id, scan_number)`, keeping the last
occurrence. *(Implemented; see `WORKSPACE_AND_CLI.md` for the full treatment.)*

**The Stage II ↔ Stage III boundary is a frozen file contract.** The driver consumes analysis
only via the `isar run` subprocess and its products — `report/results.csv`, `<workspace>/report/report.json`,
`card.state.json` — and never imports fit internals. The pre-release v1→v2 objective/campaign reset
was authorized on 2026-07-17 (O12); additive-only evolution resumed from v2, and is superseded for
contracts the SCENE program rebuilds (ruling H1): `isar.campaign-program/1` replaces the
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
example lives in `SCHEMA_LANDSCAPE.md` and the card files themselves. Everything once
hardcoded in per-instrument scripts (temperature snap lists, excluded-scan sets, monitor
references, path conventions) is a card policy, not code.

**Collation** is a deterministic groupby on the card's `collation.key` ((sample, Q, T) on S30;
(Q, T, field) on IN8) with per-card merge policy: `separate` keeps duplicates as distinct points;
`merge` uses the combine-before-divide primitive — sum kept counts AND kept monitors, then divide
downstream if chosen, never average already-normalized rates, no zero-count floor.

**Facts, Q and exposure.** A `ScanRecord` and each `Spectrum` carry `facts`: `{name: {"value",
"source"}}`, the source one of `header`, `trailer`, `sidecar`, `filename`, `card`, `inferred`,
`missing`. The first found wins in that order; a disagreeing value is kept under `conflicts` and
raises `fact_conflict`. The slit is the one generic fact `slit`, spelt `<h>x<v>` in mm. A spectrum is
identified by (experiment, scan number, detector); its Q is attached afterwards and records its rung:
`Q_SOURCE_RUNG` maps each `q_source` to rung 1 (`header`, `filename`, `reported`), rung 3
(`card_offset`) or rung 4 (`none`, `q = None`). A spectrum may carry its own `monitor` and `time`;
`scan.exposure_for(record, detector_id)` is the only place that applies them. A record whose spectra
carry their own exposure stores NaN at record level, so a consumer that missed the override gets no
number rather than a wrong one. *(Implemented.)*

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
  what was removed. This is what makes the human-review loop closable at all: a deletion that
  already happened cannot be reviewed or undone, while "subtraction" as a downstream fitter
  honoring a mask is fully reversible because the raw point is still there.
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

**Tabulated elastic calibration (`tabulated_elastic`; a card switches it on).** The card names a
folder of measured resolution curves, one per analyzer and slit. `calibrations.refresh_tabulated`
runs in `isar run` after routing and writes `card.state.tabulated_calibration["exp:scan"]`, never
the policy. Per scan it chooses the slit: a declared `slit` fact is kept, and another slit that fits
the elastic lines clearly better (summed Poisson deviance ahead by 10 or more) only raises
`slit_mismatch`. With none declared, the slit with the least summed deviance over the analyzers that
have a significant elastic line and a curve at every candidate is used, flagged `slit_inferred`, and
`slit_ambiguous` when the runner-up is within 10. Per analyzer, its curve at that slit is fitted to
the raw counts over ±3 meV of the elastic peak (located within ±1.5 meV of 0, the file's own zero) by
Baker–Cousins deviance, with no zero-count floor; the fitted centre is the energy zero (`elastic_fit`).
An analyzer with no significant elastic line (a peak at the edge of the search, a peak not 5 σ above
the median, or a fit pinned at its centre bound) takes the file's own axis, labelled `pipeline_zero`:
an uncalibrated axis is never assumed to be zero. An analyzer with no curve at the chosen slit, or
whose spectrum carries a `counts_not_integer` warning (every `_lr` spectrum), is held with its reason
and is not fitted. In the fit the curve
replaces the Voigt for the DHO convolution and the elastic line, its FWHM is the Gaussian width, and a
fit range beyond the table raises `resolution_range_exceeded`. The in-plane momentum spread is not
modelled. Each given zero is also cross-checked against the beamline's elastic centroid (`elpk_centroid_meV`): a difference above 0.3 meV raises `zero_disagrees_with_beamline` and never moves the zero. *(Implemented.)*

**Flag adjudication (LLM, off the critical path).** The deterministic layer flags; the LLM
adjudicator *decides* each flag (mask / keep / drop_detector / hold_for_human) with a reason and
a per-scan narrative — but decisions are **recorded only** (`record.adjudication` plus the
append-only `card.state.corrections` audit); nothing is applied until the correction loop under
human review. Ambiguity is held for the human; a flagged scan is never silently dropped.
*(Implemented, opt-in; see `ADJUDICATION.md`.)*

## 7. Stage II — Fit & Verdict

Every fit answers three separate questions, in order. (The persuasion-grade treatment with
reproducible demonstrations is `STATS_PRIMER.md`.)

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
diagnostics lane. The peak count is open-ended and decided by the fast pass
(`STATS_PRIMER.md` §2.2; `fit_one` runs it on every spectrum).
*(Implemented: `best_model`, `n_modes`, `best_model_posterior`, and the
count state `count_state`; every fitted mode is a row of `report/modes.csv`. The scalar `omega0` and `omega0_err`
columns of `results.csv` are the one steering choice (the primary phonon steering reads),
not the mode result; their rename to `steer_omega0` is deferred (TODO FIT-18).)*

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
or checkpoints its current reduction wave. *(Implemented 2026-07-19 — ruling O19.)*

**Acquisition.** The combine-before-divide `(q,T)` master is the sole scientific decision unit.
Physical fits remain inspectable provenance but never vote on ω, branch identity, convergence, the
dispersion prior, or stopping. Each usable master retains a bounded full-parameter, model-weighted
predictive ensemble; candidates rank by expected information about target ω plus model/branch identity,
per predicted analysis second in deterministic mode (per measurement cost in measurement-dominated
tiers). Missing predictive draws trigger one full-tier master escalation before another scan. Low gain,
poor fits, ambiguity, and count caps remain visible strategy diagnostics but do not abandon q. Missed
windows recenter from the pooled spectrum rather than the last individual fit. *(Implemented
2026-07-19 — rulings O18/O20.)*

**Branch-identity-aware acquisition** *(branch-hypothesis slice Implemented 2026-07-17;
`level`/`continuum` plugins Partial — rulings O6–O12, `design/steering.md` §4.5,
`HYPOTHESES.md`; placement rewritten 2026-09-02, `design/eig.md`)*: scan value is one number —
expected prize completions per neutron over a mutually exclusive target-energy posterior, so
"which branch is this peak" and "how precisely do we know the target here" are priced together
instead of mixed by a λ weight; user-declared excitation hypotheses in
`card.policy.toml` weight — never force — belief seeding, classification, and the windows the
planner generates (which stop unconditionally excluding the rival branch); rivals
are measured only as far as excluding them needs; hypothesis mismatch is detected and
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
2026-07-10 as the S3 re-certification; the recert log is in git history. Realistic
clocks and the McStas rung remain Planned.)*

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
`design/reporting.md`. A user-facing campaign report is a later, separate artifact.
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
experiment, not a lecture — the gallery that makes it is `STATS_PRIMER.md` §3 and
`stats/`). *(Implemented;
coverage currently exposes the γ-at-bound work as designed.)*

## 14. Workspaces & hygiene

Every entry point writes a `run_manifest.json` (purpose, command line, config hashes, date,
products, outcome). `out/` is disposable under a promote-or-archive policy; blessed baselines
(the frozen certification workspace, gold-check evidence) are archived outside it and pinned by
characterization tests. Pedagogical and validation figures live under `docs/`, generated by
script from frozen references — never stranded in `out/`. *(Manifest + policy Planned —
rulings W1–W3.)*

## 15. Status & open questions

| Capability | Status |
|---|---|
| Stage I: parsers (S30, IN8, SPring-8 BL43LXU processed per-analyzer files in both layouts), reduce, calibration state, card | Implemented |
| BL43LXU file sets at ingest (one scan per set, `parse_set`), facts with sources, per-analyzer exposure, count recovery to raw counts | Implemented |
| Q by rung: rung 1 (the analyzer's own HKL from a header or filename) and rung 4 (no Q) | Implemented |
| Q by rung: rung 2 (from the sample motor angles) and rung 3 (from the arm-centre HKL, approximate) | Planned (waits for the SIXCIRCLE geometry) |
| Tabulated per-analyzer resolution (SARF curve per analyzer and slit) in the fit, in place of the Voigt | Implemented |
| Slit ladder: declared slit, else inferred from the elastic lines; `slit_mismatch`, `slit_inferred`, `slit_ambiguous` alerts | Implemented |
| Per-analyzer elastic zero (`tabulated_elastic`) with labelled `pipeline_zero` fallback; real-data proof `proofs/test_bl43_resolution.py` | Implemented |
| Momentum (in-plane) resolution spread modelled inside the fit | Planned (after the SIXCIRCLE geometry) |
| Adjudication (decide–record–defer) | Implemented (opt-in) |
| Stage II: Baker–Cousins fit, evidence selection (H4; Akaike diagnostics retained), branch posterior, verdicts | Implemented |
| Full DHO lineshape (paper Eq. 2) in the one fitting path: E0 fitted directly, Bose factor at every energy point, height coordinate, overdamped modes representable | Implemented (2026-10-25; `STATS_PRIMER.md` §8, `tests/analyze/test_dho_full.py`) |
| Open-ended peak count by the fast pass: a pseudo-Voigt seed, then every count from 0 to the ceiling (card `[fitting] max_modes`, default 6, arbitrary) fitted in full with audits, delta-mode twins and box-rule Laplace evidence at the posterior mode, four count states, confident and tentative peaks, `report/modes.csv` with one row per mode, the `secondary_omega0` columns removed | **Implemented** (2026-10-26). Wiring: `fit_one` runs the fast pass on every path, in one pass, with no earlier rung 2 and no shadow search; `--errors` attaches its interval afterwards. Four thresholds are installed as provisional defaults: `seed_stop_gain` is not calibrated, and the other three were calibrated on the development streams, two of them on flat grids at their edge. Synthetic evaluation: 0 of 551 should-find peaks missed, 1 of 712 confident peaks false; median 79 s per spectrum against a 60 s aim (`STATS_PRIMER.md` §2–2.3, `isar/analyze/fast_pass.py`). |
| Peak-count benchmark (`isar.synth.count_bench`): should-find misses, false peaks, count states, flag rates and per-spectrum time, on synthetic truths built with the real resolution curves | **Implemented**, measured on the evaluation streams (292 spectra, 2026-10-26; `STATS_PRIMER.md` §2.3). Figures are aims, not gates; the full run is on request, and a one-peak case runs in the routine suite. |
| Interval ladder through bootstrap; `interval_reliable` flag | Implemented |
| Uniform `interval_reliable` convergence rule | Implemented (2026-07-10) |
| Pooled-fit member provenance | Implemented (2026-07-10) |
| Persisted content-addressed pooled spectra + master/member browser | Implemented (2026-07-19; additive v2 fallback for legacy curve-only masters) |
| Pooled master displayed in its fit's exposure frame; plane and waterfall columns are the master per reference monitor | Implemented (2026-09-05; design/gui.md heatmap item) |
| Campaign never stops on a survived failure; `campaign_alert` channel + GUI banner; retrying atomic swap for session writers | Implemented (2026-09-05; design/steering.md §7.10) |
| Outward alert connectors on `campaign_alert` (WhatsApp, e-mail, desktop) | Planned (design/steering.md §7.10) |
| Energy domain is the instrument's: card `max_energy_transfer_meV` above, elastic exclusion (optic) or zero below; no per-branch search ranges | Implemented (2026-09-05; design/steering.md §7.2, design/eig.md) |
| Master-authoritative assessment, prior, branch tracking, and stopping | Implemented (2026-07-19) |
| Pooled member homogeneity bootstrap | Implemented (2026-07-19; seeded exposure-scaled Poisson deviance, α=0.01) |
| Full-parameter master-predictive artifacts + model-discriminating information ranking | Implemented (2026-07-19) |
| γ-at-bound calibration v2; Γ prior | Planned |
| `tas4d` convolution (TAVI-supplied ellipsoids) | Planned |
| Stage III: per-point objective, EIG acquisition, stop-and-report | Implemented (analytic tier certified) |
| Same-action scan-window EIG (O36/O41/O43) | **Retired** 2026-09-02. The comparison scored a legislated survey window against a centred core *at a q that already had a confirmed fit*, so on a 21-point line the first 20 scans were surveys by construction — it answered which window to use for a placement something else had already made. Placement is now the EIG question outright. The package (`isar/eig/`), its policy compilation, authority records, offline corpus, the `--scan-window-eig-mode` knob and the card `scan_window_eig_policy_set` are deleted; git history is the archive (`design/eig.md` §8). |
| EIG planner (targeted placement: Q, window, exposure) | **Implemented** 2026-09-02 (`isar/drive/acquisition/eig_planner.py`, `design/eig.md`). Ranks every candidate action by expected prize completions per neutron over the per-branch hypothesis map and the counts-read intensity map; Gauss–Hermite scenarios, no RNG, exact ties; the prize is judged on the published certified interval and `plan_next`'s `goal_met` is the only stop that means the objective was answered. Live on TAVI 2026-09-03: (2,0,0)→(3,0,0), 82 scans, 24.9 min, 20/20 prize points, terminal `met`. Named limitation, outside the planner: `isar score` on that run gives a median |Δω₀| of 0.028 meV inside the 0.05 meV bar it used but an interval coverage of 0.444 against a nominal 0.68 — the published intervals under-cover by ~1.9×, half of it a systematic centre pull no inflation can fix (`STATS_PRIMER.md`). Owner ruling 2026-09-03: the DHO convolution grid drops to 0.01 meV, which removes the share of the pull the grid itself caused, and the default per-point bar becomes **±0.1 meV**, which honestly covers the residual — whose physical cause is resolution focusing that ISAR's 1D energy-resolution fitter does not model (deconvolution deferred). |
| Acoustic zone-centre exclusion | **Implemented** (O44, 2026-08-05): an acoustic branch has ω(q→0) → 0, so at the reduced-q origin the excitation is degenerate with the elastic line and is not a measurable target at any exposure. That q is not precision-coverage eligible from the first iteration, on the branch type and the tracker's published `reduced_q_origin_index` alone — pure geometry, and since 2026-09-02 the only route to the exemption (the tracker's earned `inferred_unresolved` endpoint state is deleted). Branch-conditional: an optic branch has a finite ω at the zone centre and stays eligible there. An acoustic campaign's "N measurable grid q converged" is therefore one lower than its path length. |
| Tracked-branch placement (frontier, anchor, bridge, same-q ceilings, opening book, survey) | **Retired** 2026-09-02. Deleted, not routed around: the connected frontier and its `frontier_blocked` terminal (O15), `maximum_bridge_steps`, the acquisition-seed anchor, `maximum_production_batches_per_q` (O27), the targeted `GENERATOR_REGISTRY` exhaustion proof (O28), decline widening (O11), missed-window recentre-and-widen, and the `survey` flag that made every first visit to a q scan the whole search band. `track_state` reduces to `confirmed` / `ambiguous`, which is branch *identity* and not placement. `design/steering.md`, "Placement is the EIG planner". |
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
| SCENE program: target-free multi-phenomenon discovery — catalogue-driven identity-conditional evidence, cluster-factorized scene posterior, question layer, scene-EIG | **Partial** (`design/scene.md` governs; the H-series rulings carry each slice's full record (originals in the retired ledger, git history). Implemented: SCENE-0 constitution + synthetic world-suite foundation (H1, 2026-08-08); SCENE-1 catalogue, evidence selection, and constrained variants (H3/H4/H9–H10, 2026-08-08/09) — phonon-less nulls first-class with the `elastic_only` verdict, per-structure evidence through the validity ladder with the posterior driving selection and AIC persisted as a diagnostic, universal Bose factor, `isar.fit_selection_semantics/2` replacing `/1` outright, records-only local identity axis; SCENE-2 `isar.campaign-program/1` sole contract (H2, 2026-08-08); SCENE-3 scene layer (H11–H14 incl. the PR #19 response waves, 2026-08-10) — typed adapter over persisted candidate rows, ambiguity clusters scored exactly with recorded mass bounds, four live scene factors on declared sample knowledge, content-addressed revision store with the `workspace_api` read surface + hypothesis map, the one canonical scene build reached from `isar report --scene` (H14(d)'s observe-only driver hook was superseded in 4.3 — the campaign parent owns the build now), tracker contracts ported as scene specifications. Deferred by H9(e): near-degenerate amplitude tie, out-of-window mode treatment. SCENE-4 is **complete** (H22, 2026-08-21): PR 4.0 "scene authority readiness" merged (H15/H16, 2026-08-13, PR #30) — the counts-authoritative `isar.measurement-arena/1` record with three trust lanes, the #21 Variant A constraint-origin scoring lanes, scene partitioning by (sample, ConditionSet, lattice-epoch frame token) with topology-role vocabulary, refiner rescue bounds + the transformation-ablation ledger, and the expected_two relaxes-not-forces gate; PR 4.1 "question + agenda core" merged (H17, 2026-08-14) — the three-object question layer with semantic stable identity, authority-aware predicates with log-space adversarial bound arithmetic (EstimateParameter deliberately never closes until the scheduled interval re-baseline; ZC endpoint is compatibility-only), the `isar.question-policy/1` profile with three authority postures frozen into the fully explicit `discover_and_map/2` step, content-addressed agenda snapshots with external typed transition history, and the beam-directive-free `AgendaVerdict` beside the untouched targeted `evaluate_stop` — offline (generator bindings land in 4.2, driver wiring in 4.3); PR 4.2a "interval re-baseline + question-policy/2" merged (H18, 2026-08-14) — per-candidate interval closure, producer-soundness interval authority, class-agnostic ConnectTrack, parameter-precision closure on retained mass; PR 4.2b "discovery generator layer + offline planner" merged (H19, 2026-08-15) — the seven question-bound generators (`isar/drive/steering/generators.py`), readiness gating and per-generator fault isolation, the exhaustion producer, and `plan_next_action`'s five-outcome offline controller with its scheduler declaration, DecisionBasis, and the `report/inventory/discovery/` / `report/program/` / `report/decisions.jsonl` stores, plus the `ws.agenda` read surface with five-comparand freshness — all offline (no `isar drive` wiring, no production durable-blocker emission; both are 4.3); PR 4.3 "live driver integration" merged (H20, 2026-08-18) — `isar drive` runs a `discover_and_map` step live through the `run_program` seam under the four-reasons stop law, with the scene build moved out of the analysis child into a supervised `isar scene-build` subprocess the campaign parent owns per delivery (digest-pinned planning, never a pointer read), campaign provenance stamped onto every submission and PHYSICAL path identity joining the partition key, the cold-start partition producer minting from declarations, the broken-scan quarantine/retry/offramp lane, production durable-blocker emission over the two-member taxonomy {instrument_geometry_infeasible, program_boundary}, and the consultation channel with its `isar answer` operator surface; PR 4.4 "authority flip, stop-law cleanup, acceptance closure" merged (H22, 2026-08-21) — scan-window EIG default-on at compile time for targeted objectives, shadow-only machinery deleted by call-site map, a zero-removal stop-law audit against the four-reasons law, the off-path context-probe seam plus real-scene-build partition evidence (world `context_probe`, seed 1013), the causally connected cold-start acceptance sequence under the ruled broad windows, the quicklook-floor discovery exemption, and the challenge run green end to end — **SCENE-4 is complete**; SCENE-5 is **complete** (H23, 2026-08-25) — scene-EIG is authoritative discovery placement from birth: the phenomenon belief layer (hypothesis-map predictives per declared model family), the MI scoring engine (priority-weighted mutual information on question-relevant variables, additive across clusters), controller integration (the deterministic lexicographic scheduler survives only as the software-fault fallback, D7), the organic on-path −E corroboration producer, and the FCC challenge-battery/closed-loop acceptance evidence (`design/scene.md`'s SCENE-5 closure amendment) are all landed. Named narrowing, not overstatement: cost/per-neutron pricing and the priced envelope stay Planned (SCENE-5.5, H21(a)); end-to-end declared-hypothesis ATTACHMENT to real fits is **Partial/open** — the mechanism is proven live at the scoring-engine unit level, but the production catalogue declares no identity-conditioned priors so no real fit's identity is ever non-None-matched, a permanent xfail tripwire (`tests/drive/test_fcc_battery.py::test_acoustic_priority_declared_hypothesis_moves_real_placements`); SCENE-6 and SCENE-7 remain Planned) |
| Unattended physical-scan authority — discovery and targeted recovery buying scans without a human approving the exact realized request | **Not certified** (campaign-pipeline audit, closed 2026-09-02, Gate 0): 16 of 18 production seams fail; supervised proposal review and deterministic post-acquisition analysis are the sanctioned uses until Gate 1 of [plans/campaign-authorization-repair.md](plans/campaign-authorization-repair.md) passes (request validation, replay/recovery, cooperative stop). Existing fitted results are unaffected; the limit is on what the controller may autonomously buy next. |
| Branch-identity-aware acquisition (two-term scan value; excitation hypotheses; first-guess path) | Implemented for branch hypotheses (O6–O9/O11–O12, 2026-07-17; level/continuum plugins remain O10-Partial) |
| Connected tracked-branch steering: identity vs precision adequacy, single same-q budget, rival-trajectory reconnection, controller invariant | Implemented for the tracked optic path (O27, 2026-07-21; OP10 replay reaches `met` 21/21). `identity_saturated` unit-tested but campaign-unproven; AP campaign replays outstanding, so O25/O26 remain Partial |
| Action-inventory control plane: registry-driven generate/evaluate/select, three-stage lifecycle, failure-isolated exhaustion proof, evidence-sink contract, target/identity search-domain split, predicted-rival identity probe | **Partial** (O28, 2026-07-23; control plane + `narrow_rival_probe` Implemented and AP6 replay passes at the `enumerate_actions` reconstruction level, strict/migrated modes. Full driver-level AP6 campaign replay outstanding; zone-aware boundary probe deferred pending a cross-zone structure-factor consumer) |

**Genuinely open:** wrap an existing 4D-convolution engine (ResLib/Tobyfit lineage) vs implement
the Cooper–Nathans projection against TAVI ellipsoids directly — decide when `tas4d` is
scheduled. Cross-T dispersion-prior pooling near transitions — revisit with data.

## 16. Document map

**The map lives in one place: [`READING_GUIDE.md`](READING_GUIDE.md)** — every document,
with its lifecycle, so a reader can tell a current contract from a plan from a historical record.
The map of every file is [`READING_GUIDE.md`](READING_GUIDE.md).

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
| §7.2 parse/classify | §6 | §13 open questions | resolved items → the owning design docs; live ones → §15 |
| §7.3 QC | §6 | §14 build order | superseded by §15 status table |
| §7.4 calibration | §6 | §15 future phase | promoted to §8 |

Decision/ruling IDs (P*, D*, U*, and the V/R/S/O/T/W/G/H series, plus the EIG workstream's
ADR-NNN labels) are stable historical names, safe to cite from code — see GLOSSARY §Ruling IDs;
the retired ledger that defined most of them is in git history. Docstrings now name the retired
plans explicitly with git history (e.g. `§7.5`, `§0.4` from the retired unification plan's
numbering) instead of citing UNIFICATION_DESIGN by path; the concordance table here still resolves
pre-2026-07-09 ARCHITECTURE numbering.

# ISAR — Design Draft

**Name.** ISAR = **I**nelastic **S**cattering **A**utomated **R**—, with the final R left
deliberately undeclared. The tool *reduces* data at ingest, *resolves* phonons in analysis,
*reviews* scans for the human, and (future phase) acts in *resolution* mode to solve a
measurement problem. Pinning the R to one word would make the name narrower than the tool, so
the floating R is intentional, not undecided. Canonical machine name is `isar` (package, repo,
command). Also, fittingly, the river through Munich.

**Status:** Draft for discussion. Supersedes the haphazard first pass, retained for reference at
`C:\Users\AMM\Documents\Github\LangGraph Test 1`. Phases 1–3 (validation harness, schema/parsers/
reduce, analysis) are built and validated; the assembly of it all into a runnable product — §14
items 5–7, the CLI + session layer — is designed in **`docs/UNIFICATION_DESIGN.md`**, which also
resolves §13 Q1/Q2/Q4/Q5.
**Scope:** A during-beamtime processing tool that ingests scans in batches on a separate
machine, classifies and QCs them, fits phonons, collates by Q, and produces review
reports fast enough to steer the experiment. A human checks everything afterward.

---

## 1. Purpose & operating context

**What this is.** A *triage* system. Its job is to give us fast, trustworthy-enough feedback
during a beamtime — "this Q point looks done," "this scan is junk," "this peak is off the
dispersion, re-measure" — so we spend beam wisely. It is **not** the system of record for
published numbers.

**Operating facts (fixed by us):**
- Runs on a **separate box** from the beamline. Scans arrive in **batches**.
- Scans appear **only when finished** (no mid-acquisition truncation to defend against).
- LLMM is **co-located** on the processing box, so reachability is not a concern.
- Resolution is measured **on-site, early** (Bragg / elastic scans) and ingested before any
  phonon fit is trusted.
- A **human reviews** every batch later. The tool flags; the human decides.

**Goals**
1. Per-batch turnaround fast enough to be useful between scans.
2. Every reported number carries provenance (which resolution, how much dispersion context).
3. Generalizes across instruments — proven here against **Sector 30 (HERIX, IXS)** and
   **IN8 (ILL, TAS)**, which differ in almost everything except the DHO core.
4. Conservative by construction: when unsure, **flag, don't delete; hold, don't guess.**

**Non-goals**
- Final, paper-grade fits (done later, deterministically, from frozen config).
- Replacing human judgment on data inclusion or peak assignment.
- A single universal parser (impossible — see §6).

---

## 2. Core principles

These are the load-bearing decisions. Everything downstream follows from them.

**P1 — LLM proposes, deterministic code disposes, decisions freeze, results are scored.**
No LLM sits in the runtime path of a number that matters. The LLM helps *author config*
(initial conditions, candidate peak structures, bad-scan candidates) during a fast pass;
those proposals freeze into the same kind of `SCAN_INITIAL_CONDITIONS` / `BAD_SCANS`
artifacts we already maintain by hand; the deterministic fitter then runs from the frozen
config and is re-runnable forever without any model. This dissolves reproducibility concerns
(remote models drift, temperature ≠ 0) and makes fallbacks honest.

**P2 — The experiment card is the central object.** One artifact holds every per-experiment
policy and all accumulated state. The core pipeline is instrument-agnostic and reads
everything from the card. Designing this schema (§6) is what makes the tool generalize.

**P3 — Per-experiment parser → normalized scan → instrument-agnostic core.** The byte format
varies wildly between instruments; the *interface* does not. A parser plugin (authored at
beamtime start, as we already do) emits a normalized scan object (§5); nothing downstream
knows what instrument it came from.

**P4 — A scan is an event that either produces a result or mutates the session.** Measurements
yield fit results. Alignment scans, temperature changes, and Bragg/resolution scans are
*context-updates* that mutate session state. Alignment scans are not noise to discard — they
carry the state the measurements depend on (sample identity, resolution, setpoint).

**P5 — Conservative cleaning.** A human reviews later, so over-inclusion is the cheaper error.
Borderline points are flagged, not removed. Deleting a sharp high-K phonon that a σ-detector
mistook for a spike changes ω; refusing to report a number we can't stand behind is safer
than emitting a plausible-but-wrong one.

**P6 — Provenance on every result.** Each fit records which resolution version and how much
dispersion context it used, because both sharpen over the experiment. A scan fit early is
weaker than the same scan fit late; the human (and a later re-run) needs to know.

---

## 3. Architecture overview

```
                 ┌─────────────────────────────────────────────────────────┐
   batches  ──▶  │  INGESTION   id-keyed, idempotent, gap-detecting          │
   (files /      │              orders by scan number, dedups by scan id     │
    fragments)   └───────────────────────────┬─────────────────────────────┘
                                              │  normalized scan stream
                 ┌────────────────────────────▼────────────────────────────┐
                 │  PARSE + CLASSIFY  (per-experiment plugin)               │
                 │     → measurement  | → context-update                    │
                 └───────────┬───────────────────────┬─────────────────────┘
                             │                        │
            (measurement)    │                        │ (context-update)
                 ┌───────────▼──────────┐   ┌─────────▼──────────────────────┐
                 │  QC / CLEAN          │   │  MUTATE SESSION                │
                 │  flag-not-delete     │   │   sample map, T setpoint,      │
                 └───────────┬──────────┘   │   resolution version, …        │
                             │              └─────────┬──────────────────────┘
                 ┌───────────▼──────────┐             │
                 │  ANALYZE             │◀────────────┘  reads calibration + prior
                 │  DHO ⊗ resolution    │
                 │  peak form, # peaks  │
                 │  ICs from prior+LLM  │
                 └───────────┬──────────┘
                             │  fit result (+ provenance, + confidence)
                 ┌───────────▼──────────┐
                 │  COLLATE by Q        │  updates running dispersion prior
                 └───────────┬──────────┘
                             │
                 ┌───────────▼──────────┐        ┌──────────────────────────┐
                 │  REPORT              │ ◀────▶  │  HUMAN REVIEW            │
                 │  per-scan figures,   │        │  corrections re-enter as │
                 │  exclusions, reasons │        │  session mutations       │
                 └──────────────────────┘        └──────────────────────────┘
```

**Persistent experiment session.** All of the above runs against a session store that survives
between batches *and* across restarts (a multi-day tool will be killed and restarted). State:
per-analyzer resolution (versioned), the running dispersion prior, the current-sample mapping,
the energy-zero time series, and the exclusions/corrections log. A LangGraph checkpointer keyed
to the session id makes a restart a non-event provided ingestion is idempotent.

> **Refinement (unification design, U2/U3/U4).** The checkpointer is realized as plain JSON in a
> per-experiment *workspace* — the card's accumulated-state half in `card.state.json`
> (experiment-owned) plus a `session.json` ledger (machine bookkeeping, deletable/rebuildable) —
> no LangGraph, no DB. Restart-safety comes from files-as-truth: every run re-parses everything
> and re-fits only new/held/changed scans. See `docs/UNIFICATION_DESIGN.md` §2.

**One graph, two drivers (later).** The per-scan node logic is shared. A *live* driver feeds
batches into the session; a *batch re-analysis* driver re-runs everything post-beamtime from
frozen config on better resolution. Build the live path first.

---

## 4. The event-stream model (P4 in detail)

Every ingested scan resolves to exactly one of:

| Event type | Examples | Effect |
|---|---|---|
| **Measurement** | energy scan at fixed Q (`bs qh …` on IN8; HERIX energy scan on S30) | produces a fit result |
| **Context-update: sample** | `ascan micro_x` (S30); sample change | sets current-sample state |
| **Context-update: calibration** | Bragg / elastic scan | adds/updates a resolution version |
| **Context-update: environment** | temperature setpoint change, field change | updates session environment |

Consequences:
- **Sample identity is a state variable**, set by the most recent alignment event, not read
  per-scan from a motor position (which we've established doesn't work). If a measurement
  arrives with no sample context yet established, it is **held/flagged**, never assigned to a
  guessed sample — otherwise scans land silently under the wrong sample's dispersion.
- **Resolution is a bootstrap gate.** Phonon fitting is not trusted until at least one
  resolution version exists for the relevant analyzer. A measurement arriving before
  resolution is held.
- **Recalibration is first-class.** Resolution is *versioned*, not one-and-done; a mid-run
  Bragg scan creates a new version, and every subsequent fit records which version it used.

---

## 5. Normalized scan schema

What the parser plugin emits. Downstream code sees only this. (Field names indicative.)

| Field | Meaning | Provenance tag |
|---|---|---|
| `scan_id` | stable unique id | filename (IN8) or `#S` index (S30) |
| `scan_type` | measurement / alignment / calibration / environment | how derived (declared vs inferred) |
| `Q` | (H, K, L) | **reported** (POSQE) vs **computed** (motor geometry) |
| `energy_axis` | the independent variable is energy transfer | `absolute` flag (true IN8, false HERIX) |
| `x` | energy-transfer array (meV) | — |
| `intensity` | normalized counts | counting mode: `monitor` (`mn`) vs `time` (`ti`) |
| `variance` | propagated Poisson error through the normalization | — |
| `temperature` | K | source: `setpoint` vs `sensor`; snap applied? |
| `field` | T | — |
| `instr` | instrument id + raw header blob | — |

The provenance tags matter: a `computed` Q with a stale geometry, or a `setpoint` temperature
diverging from a dead sensor, are exactly the silent-corruption modes we've hit. Carrying the
tag lets QC reason about trust instead of assuming it.

---

## 6. The experiment card schema (centerpiece)

One artifact per experiment. Authored at beamtime start (parser + policy), then accumulates
state during the run. **The two right-hand columns are worked examples — the test of the
abstraction is that both fit without special-casing the core.**

| Card field | Sector 30 (HERIX, IXS) | IN8 (ILL, TAS) |
|---|---|---|
| **`parser.plugin`** | `spec_s30` | `ill_ascii_in8` |
| **`parser.scan_unit`** | `#S` block in growing file | one file per scan, id = run number |
| **`parser.measurement_pattern`** | inferred from motor program | `COMND` matches `bs qh …` |
| **`parser.alignment_pattern`** | `ascan micro_x` → sample | `COMND` matches `sc a3 …` |
| **`Q.source`** | computed from motor geometry | reported (`POSQE`) + nominal from `COMND` |
| **`energy.absolute`** | **false** — needs elastic zero | **true** — fixed-kf (`FX=2`), take raw EN |
| **`energy.zero_model`** | time-keyed series, interpolate to scan time | n/a |
| **`counting.mode`** | flux/monitor | per-scan: `mn` or `ti` (read from `COMND`) |
| **`norm.reference`** | flux monitor | monitor ref (e.g. 160000), per card not hardcoded |
| **`temperature.source`** | sensor (+ FeSeS_3-style override path) | `RT` setpoint; sensor `TT` often unreliable |
| **`temperature.snap`** | — | snap to allowed setpoints within 5 K |
| **`resolution.mode`** | **measured** (Bragg/elastic fits) | **analytical** (Cooper-Nathans/Popovici) *or* measured |
| **`resolution.convolution`** | **`voigt1d`** (tight Q, near-1D in E) | **`tas4d`** (tilted ellipsoid, focusing effect) |
| **`resolution.params`** | per-analyzer widths from elastic fits | `ALF`, `BET`, `ETAM/ETAA`, `DM/DA`, `KFIX`, `FX` from header |
| **`collation.key`** | (sample, Q, T) | (Q, T, field) |
| **`collation.merge`** | keep duplicates as separate points | merge duplicates, energy-sort |
| **`baseline.lattice`** | a, c per sample | a, c (Fe₂Mo₃O₈) |
| **`sample.column_map`** | analyzer column carrying signal (verify by printing!) | detector `CNTS` column |
| --- *accumulated state* --- | | |
| **`state.resolution_versions`** | list, versioned, per analyzer | list, versioned |
| **`state.dispersion_prior`** | ω(K) per (sample, T) | ω(Q-path) per (T, field) |
| **`state.sample_map`** | current sample (from last alignment) | current sample |
| **`state.energy_zero_series`** | (scan_time → E0) | n/a |
| **`state.exclusions`** | bad-scan + bad-point log (was `BAD_SCANS`) | bad-scan log (was `EXCLUDED_SCANS={55217}`) |
| **`state.corrections`** | human review actions, append-only | human review actions |

**Why this is the first thing to build.** Everything currently hardcoded into the IN8 parser
— the allowed-temperature snap list, `EXCLUDED_SCANS`, the `160000` reference, the `bs qh`-only
filter, the user-specific base paths — is a per-experiment decision that belongs here, not in
code. The parser should *read* these from the card. The LLM's config-authoring pass (P1) is
how the card gets populated quickly; the human edits and freezes it.

---

## 7. Processing stages

### 7.1 Ingestion & batching
Define the unit as "a scan with a stable id." The adapter decides whether the id comes from a
filename (IN8: trivial, idempotent, out-of-order-safe) or a `#S` block in a re-sent growing
file (S30: reparse + dedup by scan number). Required regardless of source: **sequence/gap
detection** (a hole in the dispersion that nobody notices for hours is the worst failure for a
speed tool), **idempotent dedup by id**, and **ordering by scan number, not arrival**.

### 7.2 Parse & classify
Per-experiment plugin (§6). Emits the normalized scan (§5) and the event type (§4). Classification
uses whatever the instrument declares: on IN8 the `COMND` *is* the classification; on S30 it's
inferred. Never let classification leak downstream — it's the parser's job.

### 7.3 QC / clean (conservative)
Deterministic point-level checks (Poisson-aware spike detection, the HERIX stale-first-points
artifact, monitor dropouts). **Flag, don't delete.** Spike rules we've already established:
a point between its neighbors is never a spike; sub-10-count points are background, not spikes;
σ thresholds must respect the Poisson floor. The LLM may *explain* flags in the report; it does
not decide removals.

### 7.4 Calibration state
- **Energy zero (S30 only).** Keyed by **time** (scan number ≈ time), because mono drift is
  temporal — the same T measured hours apart needs different zeros. Interpolate to the scan's
  timestamp. The current `{T: E0}` keying is wrong for the exact drift it exists to correct.
  **A missing zero is a flagged non-result, not E0 = 0.0** — defaulting to zero silently
  asserts the IN8 case on a HERIX instrument and emits a hidden offset.
- **Resolution.** Versioned. `measured` (S30: Bragg/elastic widths per analyzer) or `analytical`
  (IN8: Cooper-Nathans/Popovici from header). This beamtime's values, never 2019 constants —
  Γ is the linewidth we report and a wrong resolution biases it directly.

### 7.5 Analyze — DHO ⊗ resolution
The deterministic core. The non-trivial parts are peak **form**, peak **count**, and **ICs**;
the physics that constrains them is §8.
- **ICs** come first from the **running dispersion prior** (ω(K) is smooth; neighbors are a
  strong prior), then from an **LLM proposal** only when the prior is sparse or the scan is hard.
- **Peak count is model selection**, decided by **AIC/BIC or a nested F-test — never raw χ²**.
  More peaks almost always lower χ², so a raw-χ² choice systematically fits noise as a phantom
  phonon. The current `fit_phonons` does exactly this; it must change.
- **Residual-driven refit.** On a failed/poor fit, the LLM looks at the *actual* profile (ideally
  the fit + residual figure via a vision-capable tier) and proposes a structural fix ("unfitted
  shoulder at +6 meV, add a Stokes component, start ω there"). This is the one place an LLM beats
  rules, and it runs **after** deterministic attempts, **off the critical path** (§9).

### 7.6 Collate by Q
Deterministic groupby on `collation.key`, with `collation.merge` policy from the card (S30 keeps
duplicates separate; IN8 merges + energy-sorts). Updates the running dispersion prior. This is
the stage the old `catalogue` LLM node should have been — `Scan_Combining.py` is already a working
reference implementation.

---

## 8. Physics encoding (the scientific heart)

The two cruxes — peak form and raw-energy validity — are **over-constrained**, not open
inferences. Encode the constraints; don't have a model rediscover them per scan.

**Peak form: elastic+Stokes vs Stokes+anti-Stokes.** Three constraints settle nearly every case:
- **Detailed balance.** I_AS / I_S = exp(−ℏω / k_BT). At 10 K a 5 meV phonon puts the anti-Stokes
  at ~0.3 % of Stokes — so a second peak on the gain side at low T **is the elastic line**.
  Stokes+anti-Stokes only becomes admissible as k_BT approaches ℏω. The classifier uses the
  **same Bose factor the DHO already ties the anti-Stokes amplitude to**, to decide whether an
  anti-Stokes peak is even on the table before fitting one.
- **Symmetry about zero.** ±ω vs 0 disambiguates instantly *given a trustworthy zero* — which on
  S30 couples peak assignment to energy-zero calibration. The pipeline must not treat these as
  cleanly sequential (calibrate, then fit) for no-elastic high-K scans.
- **Dispersion smoothness.** ω(K) is smooth; the neighbor points are a strong prior on this one.
  The running prior both seeds ICs and flags any peak landing far off the curve. This is
  deterministic and does more for IC quality and assignment sanity than any LLM call.

**Raw-energy validity is a per-instrument policy, not an inference.** It lives in the card as
`energy.absolute`: HERIX → axis not absolute, ω needs an elastic zero; IN8 (fixed-kf) → absolute,
peak center in EN is the energy transfer. A no-elastic-line scan on HERIX without a zero is a
flagged non-result.

> **Refinement (per the schema survey, `docs/SCHEMA_LANDSCAPE.md`; realized in Phase 2 §5.3).** The
> binary `energy.absolute` is too coarse for IXS in general. The honest model is a per-card
> `raw_abscissa_kind` + `status` with **three** states: `absolute` (fixed-kf TAS — IN8, HB-3),
> `needs_zero` (the raw column is already energy but the zero is uncalibrated — S30 HERIX, whose
> `HerixE` column is converted), and `needs_conversion_and_zero` (the raw abscissa is *monochromator
> temperature*, not energy — ESRF ID28). So S30 is not merely "not absolute"; it is "energy-but-zeroless,"
> a different case from ID28's "temperature-needing-conversion." The zero-fitting and T→E conversion stay
> deferred (Phase 3); only the field that distinguishes the three cases is needed up front.

**Resolution differs in dimensionality, not just value.** On IXS the resolution is tight in Q and
near-1D in energy → Voigt convolution is legitimate. On a TAS it's a tilted **4D ellipsoid** in
(Q,E), and the **apparent** linewidth depends on the dispersion slope cutting through it
(focusing/defocusing) — a steep mode in a defocused configuration broadens *instrumentally*.
Intrinsic Γ on IN8 requires convolving the DHO against the Cooper-Nathans/Popovici matrix and the
local dispersion (ResLib/Tobyfit-style), selected by `resolution.convolution = tas4d`. "One DHO +
resolution convolution" is too flat: the DHO core is shared, the convolution operator is per-card.

---

## 9. LLM role & routing

**Placement** — where the model is, and isn't:

| Task | LLM? | Tier | Notes |
|---|---|---|---|
| Parse / classify | No | — | parser plugin; deterministic |
| Collate by Q | No | — | groupby; deterministic |
| Review/flag fits | No | — | numeric thresholds + outlier-vs-dispersion test |
| Pick primary analyzer/column | No (assert) | — | verify by printing columns; LLM may explain only |
| QC removals | No | — | LLM explains flags in report; doesn't decide |
| Per-scan fit strategy (ambiguous) | Yes, batched | T2/T3 (9–13B GPU) | the one well-designed pattern in the old code |
| IC + candidate peak-structure proposal | Yes | T2/T3, escalate hard cases | seeded by dispersion prior first |
| Residual diagnosis on failed fits | Yes | T4/T5 (NanoGPT: DeepSeek v4 Thinking, GLM 5.2) | vision on the residual figure; off critical path |
| Triage / explanation reports | Yes | T1/T2 extract, T3 prose | the test already showed this works |
| Cross-scan / dispersion sanity narrative | Yes | T4/T5 | over a batch, not per scan |

**Route everything through LLMM.** This buys, for free:
- `structured_output_schema` replaces every ```json-strip + `json.loads` in the nodes with a
  guaranteed-shaped object — deletes most of the defensive parsing cruft.
- **Profiles** replace the `USE_LOCAL` / `get_llm(tier=…)` branching; the Dispatcher owns
  fallback, so per-node try/except tiering goes away.
- **Budget + telemetry** make the "every node silently degrades to fallback" failure visible —
  we can see which path each decision took and what a batch cost.

**Latency discipline.** The value proposition is speed. First-pass deterministic fit + figure in
seconds; **any LLM consult is async and only on the hard residual, never in the per-scan critical
path.** If LLM work exceeds the inter-scan interval the tool stops being fast.

**Degraded mode.** LLMM is co-located so the network worry is gone, but define behavior if a tier
is down: deterministic-only triage proceeds and the affected scans are flagged "no LLM consult,"
rather than the batch stalling.

---

## 10. Human-in-the-loop & the correction channel

The report is the product, and the test already produces the right artifact: per-scan
explanations, excluded points, figures, enough to see where to intervene. **The missing half is
the return channel.** When the human says "223 is bad" or "that ω guess was wrong," it must
re-enter the session as a **first-class mutation** so the next batch's prior excludes 223 and the
dispersion stops being distorted. In live mode the human's review actions **are** the config being
authored incrementally — the concrete form of P1. Without this channel we re-surface the same flag
batch after batch and the prior never learns from the expert sitting right there.

---

## 11. Validation harness (build this first)

We have a gold standard and aren't using it: **FeSeS_1, 79/83 human-validated fits with a curated
`fit_results.csv`.** Every agent decision — which scans it rejects, what ω it extracts — gets
scored per-scan against that CSV. Without this we cannot tell whether the LLM is helping or quietly
distorting the dispersion, and for physics the latter is worse than no automation. Build the harness
*before* adding agent capability, so every later change is measurable. Pick one dataset where we
hold both raw data and trusted outputs (FeSeS_1 is the obvious choice) and make it *the* benchmark
— don't develop on 2019 FeSe while validating against FeSeS in our heads.

---

## 12. Confidence & triage scoring

Replace the LLM's self-reported `high/medium/low` (uncalibrated) with a **computed** confidence:
agreement of ω across the 2-/3-peak attempts within error, the parameter errors themselves, and
distance from the running dispersion. That gives a trustworthy flag, and it's what routes a scan to
human review. Fold in the provenance (P6): a fit on sparse prior + provisional resolution is
inherently lower-confidence than the same fit late in the run.

---

## 13. Open questions / decisions to make

1. **Live-first or batch-re-analysis-first?** Shared node logic, two drivers. Recommendation:
   live first (it's the stated value), batch re-analysis once the card + harness exist.
   **RESOLVED (unification U1):** a re-runnable, idempotent batch CLI (`isar run`) run per batch
   *is* the live driver; a watch/daemon mode and the batch re-analysis driver come later on top.
2. **Batch payload for S30:** growing-file resend (reparse + dedup) vs new-scans-only (needs a
   manifest)? IN8 sidesteps this entirely (per-file). Pick the S30 convention explicitly.
   **RESOLVED (unification U4):** growing-file resend — every run re-parses everything, dedups by
   `(experiment_id, scan_number)` keeping the last occurrence, and re-fits only new/held/changed.
3. **TAS 4D convolution:** wrap an existing engine (ResLib/Tobyfit/Reslib) or implement
   Cooper-Nathans projection ourselves? Affects how soon IN8 Γ is trustworthy vs just ω. *(Still
   open — deferred; Γ carried as provisional on TAS, Phase 3 D5.)*
4. **Card format:** TOML/YAML for the policy half, with the state half in a small DB/JSON the
   session writes? Or one growing file? The state half is written every batch.
   **RESOLVED (D8 + unification U3):** policy = human-authored TOML (machine never writes it);
   state = two JSON files — `card.state.json` (experiment-owned accumulated state) and
   `session.json` (machine ledger, deletable/rebuildable).
5. **Checkpointer backend** for the session (SQLite is probably enough for a single box).
   **RESOLVED (unification U2):** plain JSON + files-as-truth idempotency; no checkpointer
   dependency.
6. **How much of the dispersion prior is shared across temperatures** for IC seeding — ω(K)
   shifts with T near transitions, so a naive cross-T prior could mislead. *(Partially resolved:
   the unification prior builder pools same-T first and falls back across T only when fewer than
   two same-T points exist — the gold check's rule; revisit near transitions with data.)*

---

## 14. Suggested build order

1. **Validation harness** against FeSeS_1 — so everything after is measurable. **DONE (Phase 1 +
   `isar/analyze/gold.py`; evidence in `out/phase3_gold/`).**
2. **Experiment card schema** — drafted against both S30 and IN8 columns (§6); this forces every
   policy difference into one explicit place. **DONE (Phase 2, D8; policy half — the accumulated-
   state half lands with unification §2.1).**
3. **Normalized scan schema + two parser plugins** (`spec_s30`, `ill_ascii_in8`) reading policy
   from the card; strip the hardcoded magic out of the IN8 parser. **DONE (Phase 2).**
4. **Deterministic core fixes**: AIC/BIC peak-count selection, time-keyed energy zero with
   "missing → flagged," per-card resolution. Score against the harness. **DONE (Phase 3) — except
   the per-card/versioned resolution *wiring*: the calibration functions exist but the card-driven
   `[calibration]` selection and `resolution_versions` state land with unification §3.**
5. **Session + ingestion + event model**: persistent store, idempotent batch ingestion, gap
   detection, alignment-as-context-update, sample/resolution bootstrap gates.
   **← CURRENT: designed in `docs/UNIFICATION_DESIGN.md` (with the CLI and run report), in build.**
6. **LLM layer via LLMM**: structured-output IC/peak-structure proposal and triage reports; then
   the async residual-diagnosis path. *(QC-flag adjudication built in Phase 2, opt-in in the
   unified driver (U5); IC-proposal and residual-diagnosis paths still future.)*
7. **Correction return channel**: human review actions as session mutations. *(Minimal form ships
   with unification — card edits + re-run with ledger invalidation, §6 there; richer review
   surface later.)*
8. Only then: the second (batch re-analysis) driver, and the TAS 4D convolution if IN8 Γ is needed.

---

## 15. Future phase — closing the measurement loop

Beyond triage: feed the running dispersion prior and per-Q confidence back into a *proposed
next-measurement plan* (which Q points are under-determined, where the dispersion is steep and
under-sampled, which scans the confidence score says need a repeat). Human-approved, never
auto-fired — this is the "act in resolution mode to solve a measurement problem" reading of the
floating R. The architecture already leans toward it: the dispersion prior, computed confidence
(§12), and the correction channel (§10) are exactly the inputs a planner needs. Deferred until the
triage loop is trustworthy and scored, since a planner is only as good as the confidence it acts on.

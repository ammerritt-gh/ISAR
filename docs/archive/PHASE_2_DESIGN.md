# ISAR Phase 2 — Parsing & Reduction (the front half of the R)

**Status:** Settled. Ready to hand off to a build session (`docs/archive/HANDOFF_PHASE_2.md`). All open
decisions D1–D8 are resolved (§11), and the design is grounded against real files from four of the five
instruments in `test_data/`.
**Relationship to the master design:** This is the concrete realization of `DESIGN.md`
§5 (normalized scan schema), §6 (experiment card), §7.1–7.3 (ingest / parse / QC-clean),
§7.6 (collate), and §10 (correction channel). It is the **Reduction** half of the floating R:
raw facility bytes → normalized, QC'd, provenance-tagged scan files + human-readable reports.
It deliberately stops *before* fitting — Phase 1's fitter is what consumes Phase 2's output later.

**Relationship to Phase 1:** Phase 1 built a synthetic core that starts data as *arrays* and
bypasses the format problem on purpose. Phase 2 picks up exactly that dropped problem — the bytes —
and adds a synthetic **format emitter** so the parsers can be validated against known truth the same
way the fitter was. The two synthetic generators share one ground-truth manifest (§4).

**Scope of ambition vs scope of build — read this before the schema sections.** The schema and parsers
here are *designed* to generalize across scan types, instruments, and sample environments; that design
is driven by the field survey in `docs/SCHEMA_LANDSCAPE.md`. But Phase 2 **builds and validates only the
case we actually run most often: constant-Q scans across energy transfer, with sample temperature and
(sometimes) magnetic field varying**, on two instruments (S30 HERIX, IN8). Everything else —
constant-energy Q-scans, angle / mesh / ramp scans, polarization channels, pump-probe, the other three
instruments we have data for — is a **designed-in extension point, not Phase 2 code**. The governing
rule, stated once and enforced throughout: *the schema must not have to be rewritten to add those cases;
the parsers and QC may grow to cover them.* This is what keeps Phase 2 small and shippable while the
representation stays future-proof. Wherever a field below looks heavier than constant-Q-energy needs,
that weight is buying a named future case (§5.6, §11-D7), not speculation.

---

## 0. Read this first — five corrections to the framing

The brief that kicked off Phase 2 had five places where the plan, as stated, would build the wrong
thing or over-trust the LLM. Each is cheap to fix now and expensive later. They shape the rest of
this doc, so they go up front.

**0.1 "Synthetic data to test parsing" needs synthetic *files*, not synthetic arrays.**
Phase 1's generator emits arrays — it can't test a parser, because a parser's whole job is bytes →
arrays. To test parsing you need the **inverse of the parser**: a *format emitter* that serializes a
known-truth scan into each facility's real byte format (SPEC blocks for S30, per-file ASCII for IN8),
headers, quirks and all. Then `parse(emit(truth)) == truth` is a round-trip test against exact
ground truth. This is the strongest parser test available and it becomes the spine of Phase 2 (§4).

**0.2 The "de-dupe by experiment / scan / detector" is an *identity key*, not de-duplication.**
Those three fields don't remove duplicates — they *name* a single 1-D spectrum uniquely. That is a
composite primary key, and it's load-bearing (§3). True de-duplication — the same scan re-sent inside
a growing SPEC file — is a separate concern that *uses* the key to be idempotent (`DESIGN.md` §7.1).
Keep the two ideas apart or the schema gets muddled.

**0.3 One IXS scan is *many* spectra — capture all detectors, but their usefulness is per-experiment.**
HERIX has nine analyzers; each sits at a slightly different Q, so one physical energy scan carries up
to **nine** spectra, one per analyzer. The real S30 parser (`Data_Parsing_MnTe.py`) hardcodes
`DATA_COL = "Ana5_3std"` and keeps the central analyzer **only**. Whether the other eight are worth
keeping is **an experiment-by-experiment question, not a blanket 9× win**: for many of our runs only
one analyzer sits on anything useful, sometimes the off-analyzers are *expected* to be junk, and
occasionally a supposedly-bad one is where something interesting is hiding. So the parser **captures
all detectors** (one scan → many spectra in one record, §3), each at its own Q from a per-analyzer
offset in the card, and **per-detector quality is flagged through to review** — the card marks which
analyzers carry signal for this experiment, QC flags the rest, and the human (or the LLM reviewer, 0.5)
decides per detector. The fix is *don't silently discard, and don't blindly trust either* — the old
parser asserted one answer for every experiment, which is the actual bug.

**0.4 "Initial subtraction" must be *masking*, not deletion.** The brief asks for spurious-point
subtraction with later review. The old `clean.py` did this literally — it wrote new files with points
physically removed and whole scans dropped, driven by an LLM's `recommendation` field. That violates
`DESIGN.md` P5 (flag, don't delete) and makes the human-review loop (goal 4) **impossible**: you can't
review or undo a deletion that already happened. Phase 2 never destroys a point. Reduction emits the
raw arrays **plus a per-point QC mask and a proposed-exclusion list**; "subtraction" is the downstream
fitter *honoring the mask*, fully reversible because the raw point is still there. This single change
is what makes the loop closable.

**0.5 The LLM *does* make the QC call — because deterministic solvers crash on messy scans.**
I initially argued the LLM should only narrate and let deterministic rules decide. That's wrong for
this data, and the reason is the one consistent pain point: **real scans are messy enough that
deterministic solvers crash or mis-fire** — a rigid spike rule chokes on a scan that's all spikes, a
"points < expected" test mislabels a legitimately short scan, a background estimate divides by a dead
monitor. So the division of labor is a **three-stage pipeline**, not rules-decide-then-LLM-narrates:

1. **Deterministic flag-for-review (allowed to be incomplete and trigger-happy).** Cheap numeric
   checks surface *candidates* — possible spikes, possible truncation, possible empties, possible dead
   detectors. This stage never decides and never deletes; its job is to not miss things, so
   over-flagging is fine and crashing is designed out (every check is wrapped; a check that can't run
   emits an "uncheckable" flag rather than throwing).
2. **LLM review and decision.** The LLM looks at the flagged scan *with its surrounding context and
   actual shape* and **makes a decision** — this spike is a cosmic ray, mask it; this "truncation" is
   a real short scan, keep it; this detector is dead, drop it; this one's genuinely ambiguous, hold for
   the human. Context and shape are exactly what a brittle rule can't weigh and what the LLM is good
   at, and routing through LLMM's structured output keeps the decision machine-readable.
3. **Human review, later, via the report.** Every LLM decision lands in the report with its reason and
   a stable id; the human accepts, rejects, or overrides, and that edit re-enters the card (§8). The
   human is the final authority — the LLM decides so the pipeline keeps moving during beamtime, the
   human corrects at leisure.

This still honors `DESIGN.md` P1 where it matters — **no LLM sits in the path of a fitted number** —
but it corrects my over-reading of it: QC adjudication on messy data is precisely where contextual
judgment beats rules, and the master's own §9 already files QC as flag-not-delete-with-human-review.
The new wrinkle is that the LLM, not a rule, chooses between flag and keep. The old `clean.py` failed
not because an LLM decided, but because it *deleted* on that decision with no review channel (0.4) and
put the LLM where it had no surrounding context to reason from.

What this means for the deterministic layer: build it to **flag and survive**, not to be complete or
correct on its own. Its failure mode must be a benign over-flag or an explicit "couldn't check," never
a crash that halts the batch.

One boundary, corrected from my first pass: **"this scan missed the peak" splits into two questions,
and one of them *is* answerable at reduction time.** Judging a peak against *where the dispersion says
it should be* needs the dispersion prior — an analysis-stage object that doesn't exist yet (Phase 3).
But judging the peak **by its shape, in isolation, is doable now and is a good LLM job**: a half-peak
falling off the edge of the window is obviously a half-peak, a flat trace obviously has no peak, a
clean centered feature obviously bracketed it. That shape judgment — did this scan capture a whole
feature, half a feature, or nothing — is exactly the contextual call stage 2 is for, and it doesn't
need to know the physics-expected position to make it. So the bad-scan template *does* cover
"missed / clipped / absent peak by shape"; what it defers to Phase 3 is only "peak present but off the
expected dispersion curve."

---

## 1. Purpose & the four goals, mapped

Phase 2 turns disparate facility output into one trustworthy, reviewable, re-runnable intermediate
representation, and closes the smallest honest version of the human-review loop. The four stated goals
map to concrete deliverables:

| Goal | Phase 2 deliverable |
|---|---|
| 1. Generate synthetic data to test parsing & reduction | **Format emitter** (§4): truth-manifest → real-format files, the parser's inverse |
| 2. Trial the reduction pipeline end to end | **Reduction driver** (§9): emit → parse → QC-mask → collate → write normalized files + report |
| 3. Initial catalogue of parsers from real data | **Parser plugins + experiment cards** for S30 SPEC and IN8 ASCII (§5, §6), read-from-card not hardcoded |
| 4. Close the flag → review → re-reduce loop | **Card-as-state correction channel** (§8): file-based, idempotent re-run honoring human edits |

**Two things named "catalogue" — keep them apart.** The brief's "catalogue so we don't rebuild parsers
every time" is the **parser catalogue**: a registry of per-experiment parser plugins plus their cards
(§6). The old LLM `catalogue.py` was a different object — a **scan catalogue**, the groupby of parsed
scans by Q/T, which `DESIGN.md` §7.6 says is a deterministic groupby, not an LLM node. Phase 2 builds
both; they are not the same thing.

**Out of scope (deferred to Phase 3), stated so it doesn't creep in:**
- No fitting. Phase 2 stops at clean normalized data + reports; Phase 1's fitter consumes it later.
- No persistent LangGraph session / checkpointer / event-stream driver. The loop here is file-based
  re-run, not a live session (§8 explains why that's enough for now).
- No LLM in the fit-strategy / IC-proposal / residual-diagnosis roles — those attach to the fitter.
- No energy-zero *calibration* and no resolution *measurement* as solved problems — the schema carries
  the fields and the flags (§5), but S30 zero-fitting and TAS 4-D resolution are Phase 3.
- No scan types beyond constant-Q-energy *built* — the schema represents them (§5.6), Phase 2 doesn't
  parse or QC them yet.

---

## 2. Scope

**In:**
- A **format emitter**: known-truth scan(s) → byte-faithful S30 SPEC and IN8 ASCII files (constant-Q
  energy scans), including alignment/calibration scans, multi-analyzer columns, and injectable quirks
  (disconnected sensor, monitor dropout, cosmic-ray spike, stale-first-points, early-truncated scan).
- A **normalized scan schema** (§5) built on the generalized scan-axis model but **exercised on the
  constant-Q-energy case**, finalized as a concrete on-disk format (§5.5), shared with Phase 1.
- An **experiment card schema** (§6), drafted against S30 and IN8 side by side, with the per-analyzer
  Q geometry, the energy-calibration/geometry policy, and all the currently-hardcoded magic moved into
  it; the **parser registry keyed on `(facility, instrument, format_version)`**.
- **Two parser plugins** reading policy from the card, capturing all detectors into one multi-spectrum
  record, emitting with provenance tags and the event-type classification (`DESIGN.md` §4).
- **Deterministic QC** producing per-point masks and per-scan/per-spectrum flags with machine-readable
  reasons (§7).
- A **deterministic scan catalogue** (collate/groupby by Q/T, combine-before-divide) (§7.5).
- **Reports** (the product): per-scan/per-spectrum figures + flag tables + an LLM-written narrative
  layer (§7.4).
- The **correction loop**: human edits the card's exclusions/corrections; re-running reduction honors
  them idempotently (§8).

**Out (deliberately):** everything in §1's "out of scope" list.

---

## 3. The identity key and the multi-spectrum record

A single 1-D spectrum is uniquely addressed by a composite key:

```
(experiment_id, scan_number, detector_id)
```

- `experiment_id` — the card/run this came from (different numbering per facility, so it must be
  explicit, never assumed global).
- `scan_number` — the facility's scan index (`#S n` on S30; run/file number on IN8).
- `detector_id` — which analyzer/detector channel. IN8 single-detector → one value (e.g. `det0`).
  HERIX → `ana1..ana9`.

**One physical scan is one record holding many spectra.** A parser returns one `ScanRecord` per physical
scan, not one object per detector:

```
parse_file(path, card) -> list[ScanRecord]      # one record per physical scan
ScanRecord.spectra      -> indexed by detector_id  # the N analyzer spectra share the scan's axes/monitor
```

This is the multi-spectrum container the schema landscape research points to (NeXus/Mantid both model a
scan as shared axes + a signal block with a detector dimension). The N analyzer spectra **share** the
scanned axis (the energy grid), the monitor/time series, the temperature/field, and the header — those
are stored once on the record — and each spectrum carries its **own** Q, its own intensity/variance, its
own mask and flags, and its own resolution slot (§5). **Per-analyzer Q is read from the file where the
facility logs it** — confirmed in the 2025 S30 data, where `#H4`–`#H12` define `Analyzer1_H/K/L` …
`Analyzer9_H/K/L` and the `#V4`–`#V12` value lines carry all nine (the old MnTe parser read only
`#V8`/Ana5 and discarded the rest) — and falls back to *central Q + a per-analyzer offset from the card*
only for formats that don't log it (D2). The `(experiment, scan, detector)` key selects one spectrum's
slice out of the record.

*Why one record, not N objects (the fork you OK'd):* storing N flat single-spectrum objects duplicates
the shared metadata N times and explodes the file count; one record keeps the per-detector QC of 0.3
(mask/flags/resolution are per-spectrum arrays along the detector dimension) while recording the shared
context once, and maps natively onto a future NeXus group. The key is the *address*; the record is the
*storage*.

**Idempotent de-dup (the real thing) uses the key.** Ingestion is keyed on `(experiment_id,
scan_number)` for the record and `detector_id` within it; a re-sent growing SPEC file reparses and
overwrites by key rather than appending. This is `DESIGN.md` §7.1, and it's why the key has to be stable
and detector-resolved, not per-file.

---

## 4. The synthetic format emitter — the parser's inverse (goal 1)

The emitter is the single most valuable new piece in Phase 2: it gives parsing and reduction the same
known-truth validation Phase 1 gave the fitter. For Phase 2 it emits **constant-Q energy scans only**;
the run-builder is structured so other scan types (§5.6) can be added later without reworking it.

**Pipeline:**

```
Phase-1 ground-truth scan(s)          (arrays + manifest: ω₀, Γ, T, x-grid, counts, seed …)
        │
        ▼
 build a facility "run":  interleave measurement scans with alignment + calibration scans,
                          assign scan numbers, fan signal across N analyzers at their own logged Qs,
                          attach monitor + time series, (optionally) inject quirks
        │
        ├─ emit_spec_s30(run)  ──▶  a growing #S/#V2/#V4..12/#L SPEC file (+ supplementary .dat)
        └─ emit_ill_in8(run)   ──▶  per-scan numor ASCII files with COMND/POSQE/RT/TT headers
        │
        ▼
 parse_<plugin>(file, card)  ──▶  list[ScanRecord]   (each with N spectra)
        │
        ▼
 ASSERT  parsed ≈ truth-manifest   (Q per detector, scanned axis, intensity/variance, monitor+time,
                                    T, scan_type, flags)
```

**What the round-trip proves, cleanly, that nothing else can:**
- Q is reconstructed correctly **per analyzer**, including the offset geometry (catches 0.3-class bugs).
- Counting-mode normalization (`monitor` vs `time`, monitor reference) reproduces the true rate within
  Poisson, and **both** monitor and time survive the round trip (§5.3).
- Event classification (`COMND` → measurement vs `sc a3` → alignment) lands the scan in the right bin.
- Quirks are caught by QC: an injected cosmic ray must be flagged at the channel we injected it; a
  truncated scan must trip "finished too early"; a disconnected sensor must surface as a temperature
  provenance flag — **the QC test set is generated, not hand-curated**, so coverage is exact.

**Injectable quirks (each maps to a real failure we've hit):**

| Quirk | Real origin | Expected QC outcome |
|---|---|---|
| single-channel spike | cosmic ray | point masked, `spike`, reason logged |
| leading run of equal-high points | HERIX stale-first-points bug | leading points masked, `stale_head` |
| early energy-window truncation | scan stopped early | scan flagged `truncated`, kept |
| flat-within-noise spectrum | beam down / wrong sample | scan/spectrum flagged `empty`, kept |
| monitor = 0 region | monitor dropout | affected points masked, `monitor_dropout` |
| `TT` ≈ 298 K vs `RT` setpoint | disconnected sensor (IN8) | temperature provenance = `setpoint`, flag `sensor_divergence` |
| one analyzer flat/noise, others clean | dead/off analyzer | that spectrum flagged `dead_detector`, others kept |

The emitter writes the truth manifest alongside the files, so the QC/reduction stages can be scored
exactly: did we mask the points we injected, and **only** those? Over-masking is as much a failure as
under-masking (P5: over-inclusion is the cheaper error, so the pass criterion is asymmetric — zero
false deletes tolerated, a few un-caught borderline points acceptable-with-flag).

### 4.1 Round-trip proves *consistency*; real files prove *fidelity* (the inverse-crime guard)
The round-trip has the same hazard Phase 1 §3 names for fitting, transposed to parsing: if the emitter
and the parser are written from the *same wrong assumption* about the real byte layout,
`parse(emit(truth)) == truth` stays green while the parser fails on actual facility output. Round-trip
proves the two are consistent **inverses of each other** — it does **not** prove either matches reality.
The guard is cheap because we hold real data: **each parser also gets a real-file smoke test** that parses
one actual file from `test_data/` and asserts the record is sane (right detector count, monotonic energy
axis, monitor and time populated, Q in range, scan_type correctly classified, no exception). The two
tests are complementary and both are required to call a parser done:
- **Synthetic round-trip** — known-truth *coverage* (did Q reconstruct per analyzer, did QC catch the
  injected cosmic ray). The emitter only needs to be **schema-faithful** for this (D5).
- **Real-file smoke test** — *byte-level fidelity* against genuine facility quirks, which the synthetic
  emitter cannot honestly provide. Real output *is* the brittleness test, by definition.

Concrete real files in `test_data/` (dates from the file headers, since formats drift — §6):
- **S30 HERIX:** `test_data/APS S30/FeSe_1-xS_x_1.spec_supplementary.dat` — `#D … Mar 2025`,
  GUP-277332, 9 analyzers in `#V4`–`#V12`, `HerixE` energy column.
- **IN8:** `test_data/ILL IN8/055177` — numor, `DATE_: 09-Sep-25`, DIR-397 (Fe₂Mo₃O₈). Note this scan is
  an *alignment* (`COMND: sc a3 …`) and shows a real disconnected sensor (`TT= 0.00, RT= 221.57`) and
  both monitor and time columns (`M1 M2 TIME CNTS`) — a live instance of three things QC must handle.
  `.nxs` (NeXus) twins sit beside each numor — a real second format for the same scan (§6, D-versioning).

---

## 5. Normalized scan schema (concrete)

Building on `DESIGN.md` §5 and the field survey in `docs/SCHEMA_LANDSCAPE.md`, fixed to a real on-disk
shape. **The schema is built on the generalized scan-axis model so it never has to be rewritten for a
different scan type; Phase 2 only ever populates the constant-Q-energy case.** One `ScanRecord` = shared
scan metadata + a signal block of N spectra + QC, addressed by the §3 key.

### 5.1 Record-level fields (shared across the scan's spectra)

| Field | Type | Meaning | Provenance / notes |
|---|---|---|---|
| `experiment_id` | str | card/run id | — |
| `scan_number` | int | facility scan index | — |
| `scan_type` | enum | measurement / alignment / calibration / environment | `declared` vs `inferred` |
| `scan_axes` | list | the scanned/derived/constant independent variables (§5.2) | **generalization point** |
| `normalization` | obj | monitor + time + counting mode (§5.3) | both always stored |
| `temperature` | obj | setpoint + named sensors + validity (§5.4) | `setpoint` vs `sensor`; snap? |
| `environment` | obj | typed `field` + open `logs` dict (§5.4) | arbitrary params, extensible |
| `geometry` | obj | TAS config: fixed-ki/kf, fixed value, mono/ana, collimation (§5.3) | null on pure-IXS-energy if N/A |
| `energy_calibration` | obj | raw-abscissa kind + transform + elastic-zero (§5.3) | per-card; zero may be null |
| `scan_flags` | list | per-scan {type, severity, reason} | — |
| `raw_header` | dict | the instrument header blob, verbatim | — |
| `source` | dict | {file, byte/line-range, plugin, plugin_version, format_version, schema_version} | reproducibility (§6) |

### 5.2 `scan_axes` — the generalization that makes constant-Q-energy a special case
A list of axis descriptors, each `{name, kind, role, units, values[nP]}`:
- `kind ∈ {energy, q_h, q_k, q_l, q_modulus, angle, environment}`
- `role ∈ {scanned, derived, constant}`

**For Phase 2's constant-Q energy scan there is exactly one `scanned` axis of `kind=energy`**, and the
Q components are recorded as `constant` axes (the nominal Q of the scan). That is the *only* shape Phase
2 produces. The list exists so that, later, a constant-E scan is simply *one `scanned` axis of `kind=q_h`
with energy `constant`*, a rocking curve is *one `scanned` `kind=angle`*, a T-ramp is *one `scanned`
`kind=environment`*, and a map is *≥2 `scanned` axes* — **none of which requires a schema change, only
a parser/QC that knows how to populate and judge them** (§5.6). This is the difference between "we
support energy scans" and "we hardcoded an energy column"; we are deliberately doing the former while
only *building* the latter.

### 5.3 Signal, normalization, geometry, energy calibration
- **`signal`** — the per-spectrum block, indexed by `detector_id`:

  | Per-spectrum field | Type | Meaning |
  |---|---|---|
  | `detector_id` | str | analyzer/detector channel |
  | `q` | [h,k,l] | this spectrum's actual Q (read from file where logged — S30 `#V4`–`#V12`; else central + card offset) |
  | `intensity` | float[nP] | the signal: **raw counts** (combined kept passes; monitor stored, NOT divided in — normalization is a reversible downstream choice, so √N states the error plainly). *Refined from "normalized rate" at implementation — see note below.* |
  | `variance` | float[nP] | Poisson variance = counts (σ=√N); no zero-count floor (D3, §5.5) |
  | `mask` | bool[nP] | per-point keep(true)/flag(false); **never** applied to `intensity` |
  | `point_flags` | list | per-point {index, type, reason} |
  | `spectrum_flags` | list | per-spectrum {type, severity, reason} (e.g. `dead_detector`) |
  | `resolution` | obj/null | **per-spectrum** resolution (null at reduction, set later) |

  `resolution` is per-spectrum, not per-record, because multi-analyzer IXS analyzers differ in
  resolution (ID28: 2.96 meV for analyzer 2 vs ~3 meV average). Carrying it per-spectrum now costs
  nothing and prevents a Phase-3 rework when the fitter needs it.
- **`normalization`** = `{counting_mode: monitor|time, monitor[nP], time[nP], preset, monitor_correction}`.
  Both monitor and time are **always** stored regardless of mode (the fixed one is the preset, the other
  is measured), because that is what the file actually contains and what later flux normalization and
  run-merging need. `monitor_correction` flags the thermal-TAS λ/2 subtraction so a clean monitor isn't
  assumed.
- **`geometry`** (TAS) = `{fixed_mode: ki|kf, fixed_value, mono:{crystal,reflection},
  ana:{crystal,reflection}, collimation, filter}`. On a fixed-kf TAS this is what makes the energy axis
  absolute. Null/sparse for the pure-IXS-energy case where it doesn't apply.
- **`energy_calibration`** (IXS, and the honest general case) =
  `{raw_abscissa_kind: energy|temperature|angle, transform, elastic_zero_offset, status}` where
  `status ∈ {absolute, needs_zero, needs_conversion_and_zero}`. **`raw_abscissa_kind` is per-card**:
  S30 exposes a converted energy column (`HerixE`) → `energy`; ESRF ID28 exposes monochromator
  temperature → `temperature`. The actual zero *fitting* and T→E *conversion* are Phase 3; Phase 2 only
  records the kind, the transform spec from the card, and leaves `elastic_zero_offset` null with the
  right `status`. This replaces the old single `energy_absolute` bool, which couldn't express "needs
  conversion."

### 5.4 Temperature and the extensible environment container
- **`temperature`** = `{setpoint[nP], sensors:{name → values[nP]}, valid_mask, snap_applied}`. Multiple
  sensors are first-class; the disconnected-sensor failure (IN8 `TT≈298`, the FeSeS_3 override) is a
  `valid_mask` on the offending sensor, not a silent wrong number.
- **`environment`** = a **hybrid**: a typed `field` (magnetic field, the common case, with the same
  setpoint/sensor/mask substructure) **plus an open `logs` dict** keyed by name, each entry
  `{values[nP] or time-series, units, setpoint, sensor_id, role(scanned|monitored|constant), valid_mask}`.
  This is the Mantid-sample-logs / NeXus-`NXsensor` model. **Your custom electric field, uniaxial or
  hydrostatic pressure, strain, illumination, gas loading all drop into `logs` with no schema change**,
  and any of them can be promoted to a `scanned` axis by reference from `scan_axes`. Phase 2 populates
  temperature and (sometimes) field; the `logs` container is built but mostly empty for our current runs.

### 5.5 Variance and the zero-count floor — **no floor** (D3 resolved)
The two old parsers used a 1-count floor (`err = sqrt(max(N, 1.0))/monitor`) to dodge zero-variance.
Phase 2 **drops the floor**: store variance honestly, including `variance = 0` for a 0-count channel,
and record the convention name (`none`) in the record. This is the right call *because* the downstream
fitter goes Baker–Cousins / Poisson likelihood (Phase 1's settled direction), where a 0-count channel
needs no hack — the floor only ever existed to keep a χ²/Neyman denominator from dividing by zero, and
that bias-in-the-wings is exactly what Phase 1 set out to remove. **One guard the build must honor:** a
stored `variance = 0` must never reach a χ²/Neyman weight `1/σ²` — the likelihood path consumes counts
directly and is fine, but any code that weights by inverse variance must skip or handle zero-variance
channels rather than divide. Storing the honest zero and handling it at use-time is the whole point;
baking a floor into the stored data would re-introduce the bias irrecoverably.

### 5.6 What the schema represents but Phase 2 does NOT build
Stated explicitly so the line is unambiguous. The schema above can *represent* each of these; Phase 2
writes no parser or QC for them. They are the dividend the generalization pays:
- **Constant-energy Q-scans** — `scanned` axis `kind=q_h|q_k|q_l|q_modulus`, energy `constant`.
- **Angle / rocking / alignment scans as data** — `scanned` `kind=angle`.
- **Environment ramps** (T or B scanned) — `scanned` `kind=environment`.
- **Mesh / map scans** — ≥2 `scanned` axes.
- **Polarization / spin channels** — reserved: a future `channel` coordinate `{spin_in, spin_out,
  flipper_state}` on the signal block, length-1 today.
- **Pump-probe / time-resolved** — a future axis `kind=delay` + a `delay` log.
- **Multi-sample / sample arrays** — a future optional `sample_id` coordinate.

Building these is gated on actually needing them (§11-D7); the point of §5 is that when we do, the
representation is already there.

### 5.7 On-disk format — **JSON record per scan** (D1 resolved)
The canonical store is **one JSON record per physical scan**, every §5.1–5.4 field inside it, the
per-spectrum arrays inline (our scans are ~80 points × ≤9 detectors — trivially small; spill arrays to a
sibling `.npy` only if a record ever gets large). The two old outputs (CSV `energy,intensity,error`;
space-delimited `E y err`) are rejected as *canonical* because both threw the metadata away — fatal for a
provenance-first design.

The deciding principle is "formatting for later consumption is a formatter call away": that argues **for**
JSON, not CSV, because **the canonical store must be the lossless source you down-convert *from*, never a
flat target you'd have to up-convert *to*.** A `ScanRecord` is a nested object (shared metadata + N
per-spectrum blocks + the `scan_axes` list + `normalization`/`environment.logs`/`energy_calibration`
objects + per-point flag lists); CSV can only hold that by denormalizing, splitting into join-keyed files,
or stuffing JSON into cells — and once the metadata is dropped, no formatter call brings it back. CSV,
Markdown, and (later) NeXus are all *formatter outputs* from the JSON, which is exactly the "one call
away" the principle promises.

Why JSON is also the right call for the LLM stage specifically:
- **The adjudicator never sees the raw store.** It needs the *shape* of a flagged scan — a downsampled
  trace, the flagged indices with neighbors, summary stats — not hundreds of raw floats. That compact
  view is itself a formatter call, so storage verbosity is fully decoupled from LLM token cost.
- **JSON is LLMM's native structured-output tongue**, so QC decisions come *back* in the same shape.

Field names are kept NeXus-aligned (§5.1) so a future `.nxs` export (`NXtas` probe=x-ray / `NXsample` /
`NXsensor` / `NXlog`) is a mechanical formatter, not a rewrite — NeXus stays a forward door, deferred
until interchange with collaborators is the point (D7-style trigger). Zero new dependencies, git-diffable,
human-inspectable (open a record, read why a point was masked), one file per physical scan.

---

## 6. Experiment card & the versioned parser catalogue (goal 3)

The card is `DESIGN.md` §6; Phase 2 makes it real and proves it against two instruments without
special-casing the core. The new content Phase 2 forces in:

- **`detectors`** — the per-analyzer registry. Per analyzer: the value-line / column it lives in (S30:
  the `#V4`–`#V12` index whose `#H` header names `AnalyzerN_H/K/L`, plus the `AnaN_*std` signal column),
  an enable/quality flag (which analyzers carry signal *this* experiment, per 0.3), and a Q-offset used
  **only as a fallback** for formats that don't log per-analyzer Q. IN8: a single `det0` entry. The 2025
  S30 data logs all nine analyzers' Q directly, so the offset is rarely needed there (D2).
- **`energy_calibration` / `geometry` policy** — `raw_abscissa_kind` (S30 `energy`, IN8 absolute-from-kf),
  the T→E transform spec where relevant, fixed-mode and fixed value, mono/ana crystals, collimation.
- **`normalization` policy** — counting reference, the monitor-correction flag, the floor convention.
- **Everything the real parsers hardcode** moves here, verbatim from the source files so nothing is
  lost: `ENERGY_COL/DATA_COL/MONITOR_COL`, `ZERO_COUNT_ERROR_FLOOR`, `MIN_POINTS`, `scan_exclusion_list`,
  the HKL rounding step (0.05), the temperature label rule — all S30; and the IN8 set already enumerated
  in `DESIGN.md` §6 (`ALLOWED_TEMPERATURES` snap, `EXCLUDED_SCANS`, `160000` monitor ref, `bs qh`-only
  filter, base paths).

**Card on disk = TOML policy + JSON state (D8).** The card has two parts with opposite lifecycles, and
they get two files:
- **`<exp>.policy.toml`** — the **human-authored** half (detectors, calibration/geometry, normalization,
  collation rules, exclusion lists). A scientist writes it before a beamtime and edits it rarely, so it
  needs **comments**: `excluded_scans = [55217]  # bad mono feedback, T drifted` is exactly the kind of
  annotation that must survive, which JSON cannot hold and YAML only holds at the cost of
  whitespace-sensitivity and type-coercion footguns (`no`→false) you don't want under a tired hand at a
  beamline. TOML, `tomllib` (stdlib ≥3.11), read-only to the machine.
- **`<exp>.state.json`** — the **machine-written** half (`state.exclusions`, `state.corrections`). The
  reduction run appends the LLM's QC decisions here every pass and the human accepts/rejects on review;
  each correction already carries a `reason` field, so the "comment" is data, not syntax. JSON: trivial
  to append programmatically, clean audit trail.

The decisive reason is **ownership**, which the correction loop (§8) forces: every run writes decisions
back, so a single shared file would make the machine either rewrite the human's commented policy
(comment-and-layout-preserving TOML round-tripping is a real, fiddly problem) or surgically touch part of
a file. The split removes the hazard outright — the machine **never opens `policy.toml` for writing**, and
`state.json` is the only thing it rewrites — so it can never clobber hand-authored policy. The cost is two
files and two loaders behind the one in-memory `card` object (whose `card.state` the rest of the design
already references, unchanged). This gives the repo one rule that predicts every format choice:
**machine-written artifacts are JSON** (ScanRecords §5.7, state), **human-authored config is TOML**
(policy).


**Parser catalogue = a *versioned* plugin registry.** A new beamtime picks a plugin, copies its card
template, edits the policy, and reduction runs. Critically — because our data spans years and **formats
drift** (ILL numor revisions, APS SPEC macro changes 2004–2012, IN8's ThermES rebuild) — the registry is
keyed on `(facility, instrument, format_version)`, not just facility:

```
registry[(facility, instrument, format_version)] = plugin
detect(path) -> (facility, instrument, format_version)   # from header / content / collection date
plugin = (name, version, parse_file, emit_file, card_template)
parse_file(path, card) -> list[ScanRecord]   # capture all detectors, classify, tag provenance
emit_file(run,  card)  -> bytes/path         # the §4 inverse, for synthetic validation
```

This is Mantid's valid-from-date dispatch idea, transferred: detect which format a file is, dispatch the
matching parser, and **stamp every record's `source` with `plugin_version`, `format_version`, and
`schema_version`** so a re-reduction years later is reproducible and a format change is a *new registry
entry*, not an edit to a working parser. Authoring `emit_file` next to `parse_file` is what makes each
plugin self-validating via §4.

**The five instruments we have data for, what the card must encode, and the dates we actually hold**
(S30 + IN8 are Phase 2; the rest are why the card abstraction has to hold). The `test_data/` files
revised two things the research got wrong — see notes. Detail in `docs/SCHEMA_LANDSCAPE.md`:

| Instrument | Probe | Native format | Detectors | Energy axis | `test_data/` dates | Phase 2? |
|---|---|---|---|---|---|---|
| **S30 HERIX** (APS) | IXS | **SPEC** ASCII | 9 analyzers | `HerixE` column (converted) → `energy`, `needs_zero` | Mar 2025 | **yes** |
| **IN8** (ILL) | TAS | numor ASCII **+ `.nxs`** | single (or 9 IMPS) | absolute (fixed-kf) | Sep 2025 | **yes** |
| **ID28** (ESRF) | IXS | **SPEC** ASCII + per-analyzer `_A*.dat` | 9 analyzers | mono **temperature** → `needs_conversion_and_zero` | 2019 **and** 2024 | later |
| **BL43LXU** (SPring-8) | IXS | **SPEC** ASCII (`specixs`) | array (≤28) | derived | Nov 2022 | later |
| **HB-3** (HFIR/ORNL) | TAS | SPICE ASCII (TAVI/HDF5) | single | absolute (fixed-kf) | (none yet) | later |

**Insight from the real files — three of four IXS facilities share SPEC, so build a SPEC *base* parser.**
S30, ID28, and BL43LXU all write SPEC ASCII (the research wrongly called BL43's format "undocumented /
custom"; `specixs_22Nov05` is plain SPEC). So the IXS side is **one shared SPEC reader** — `#F/#E/#D/#O/#H/#S/#G/#P/#V/#L` block parsing, scan-command classification, column/value extraction — with
per-facility differences (`HerixE` vs temperature abscissa; which `#V` lines hold analyzer Q; signal and
monitor column names) living entirely in the **card**, not in code. `spec_s30` becomes a thin card over
the base reader, and ID28/BL43 later are *more cards*, not more parsers. This is the §6 abstraction
proving itself on real data before we've written a line.

**Format drift is real and present in the test set, so the registry keys on dates from the files.** Every
format stamps its own date — SPEC `#D Fri Mar 07 … 2025`, numor `DATE_: 09-Sep-25` — and the test set
already spans 2019–2025 with two concrete drift cases: **ID28 has both 2019 and 2024 data** (the older
format may differ from the newer), and **IN8 ships each scan as both a classic numor and a `.nxs` NeXus
file** (two formats for the identical scan). `detect(path)` reads the embedded date and header markers to
resolve `(facility, instrument, format_version)`; a drifted format is a *new registry entry keyed to its
date range*, never an edit to a working parser.

**The pre-run format check (a human-in-the-loop workflow, not just code).** Because formats change
between beamtimes, the intended operating procedure is: **at the start of an experiment, point the build/
run session at the actual incoming files and have it confirm the parser and card match what the
instrument is emitting *now*** — verifying the SPEC column names, the `#V` analyzer lines, the abscissa
kind, the monitor column, and the date-detected `format_version` against a freshly captured file before
trusting the reduction. The registry makes this a check (does a plugin match this file's signature?) plus
a card review, not a code rewrite. This step is cheap insurance against silently parsing a changed format
with last year's assumptions, and it is the natural place the human re-confirms `detectors` enable flags
(0.3) for the current sample.

**Worked side-by-side (the abstraction test).** The card already tabulates S30-vs-IN8 in `DESIGN.md` §6;
Phase 2 adds the `detectors`, `energy_calibration`/`geometry`, and on-disk-format rows. If both parse and
both emit through the same core with only card differences, the abstraction holds; if either needs a code
special-case, the card schema is wrong and better to learn it here than at a beamline.

---

## 7. Deterministic QC & reduction (goal 2, the heart)

Per `DESIGN.md` §7.3 and correction 0.5: **deterministic checks flag candidates and must survive messy
input; the LLM adjudicates; the human is final.** Every check below is code, reproducible, wrapped so it
emits an `uncheckable` flag rather than throwing, and produces a machine-readable `{index|scan|spectrum,
type, reason}` that the report renders. None of them delete; none of them are the final decision (0.5).

### 7.1 Point-level (produces per-spectrum `mask` + `point_flags`)
- **Spike / cosmic ray.** A channel ≫ Poisson expectation given neighbors. Rule from `DESIGN.md` §7.3:
  *a point between its neighbors is never a spike; sub-~10-count points are background not spikes; σ
  thresholds respect the Poisson floor.* Concretely: flag `i` if `y[i] - median(neighbors)` exceeds
  `k·sqrt(expected)` **and** `y[i]` is a local max **and** the neighbor level is non-trivial. Tunable `k`
  in the card.
- **Stale-first-points (HERIX).** A leading run of near-equal, high, low-scatter points — a deterministic
  signature. Flag the leading run, type `stale_head`.
- **Monitor dropout.** `monitor == 0` (the MnTe parser silently `continue`s past these — Phase 2
  *records* them as masked points with a reason instead of dropping them invisibly).
- **Negative / non-physical rate** — flag, do **not** auto-remove (the old `clean.py` deleted all
  negatives; a negative after background subtraction can be legitimate noise).

### 7.2 Scan- and spectrum-level (produces `scan_flags` / `spectrum_flags`, never removes anything)
- **`truncated`** — fewer points than the card's expected grid, or scanned-axis range narrower than
  expected: "finished too early." Severity scales with how short.
- **`empty`** — integrated counts within Poisson noise of background everywhere: "no real data."
- **`dead_detector`** — one spectrum flat/noise while siblings carry signal (the per-detector quality
  call of 0.3; a *candidate* for the LLM/human to confirm, since a flat analyzer can be physics).
- **`clipped_peak` / `no_peak` (shape, in isolation)** — the feature falls off the window edge, or there
  is no excursion above background. This is the reduction-time half of "missed the peak" (0.5); the
  dispersion-relative half is Phase 3. Surfaced as a candidate for stage-2 shape judgment.
- **`saturated` / `monitor_unstable`** — monitor variance outside expected band over the scan.
- **`sensor_divergence`** — sensor vs setpoint disagree beyond card tolerance (IN8 `TT≈298`; the
  FeSeS_3 manual-override echo).

### 7.3 The LLM's job — adjudicate the flags (via LLMM / Prompter, off the critical path)
> **As-built (2026-06-28):** this layer is implemented, tested, and live-verified — see
> **`docs/ADJUDICATION.md`** for the as-built record (decision contract, components, the model-selection
> probe + evidence, how to run, known gaps). The text below is the original design intent.

Per 0.5 the LLM is **stage 2: it makes the QC decision** on the deterministic candidates, using context
and shape, and emits structured output (LLMM schema, not ```json-strip + `json.loads`):
1. **Decide** each flagged point/scan/spectrum: mask the cosmic ray; keep the genuinely short scan; drop
   the dead detector; hold the ambiguous one for the human. The decision and its reason are written into
   `corrections` as a record the human can override (§8) — it moves the pipeline forward during beamtime
   without ever being unreviewable or destructive.
2. **Shape-judge** "whole / half / no peak" on the `clipped_peak`/`no_peak` candidates — the contextual
   call §0.5 carved out as in-scope.
3. **Narrate** the decisions into one or two human sentences per scan for the report.

**Validate this is feasible early.** Before committing the LLM layer, run the adjudication + narration
prompt against LLMM (local 9B and a NanoGPT tier) on a handful of the §4 synthetic scans with injected
quirks, and check the local model decides sensibly and doesn't invent flags the detectors didn't raise.
The Prompter MCP is the right tool for that probe. *(Build-time check, flagged so the LLM layer isn't
assumed to work on the 9B without evidence.)*

### 7.4 Reports — the product
The report is what the human actually consumes, so it's a first-class output, not a log dump. Per
batch / run, in Markdown (your default), with figures:
- **Per-scan / per-spectrum:** the spectrum with masked points marked, the flag table, the LLM's
  decision + one-line narrative, the (scan, detector, Q, T) header. Masked points shown, **not hidden** —
  the reviewer must see what the pipeline decided to drop and why.
- **Run-level:** the scan catalogue (deterministic groupby by Q/T, §7.5), a coverage map (which Qs
  measured, **gap detection** per `DESIGN.md` §7.1), and the flag/decision rollup by type/severity.
- **The review surface:** a compact list of every LLM decision (and every proposed exclusion) with its
  reason and a stable id, formatted so the human's accept/reject feeds straight back into the card (§8).

### 7.5 Scan catalogue & collation (deterministic, replacing the old LLM node)
Groupby on `card.collation.key` with `card.collation.merge` policy (S30 keeps duplicates separate; IN8
merges + energy-sorts), per `DESIGN.md` §7.6. `Scan_Combining.py` is the working reference. No LLM.
**Merging obeys combine-before-divide** (from the research): when combining repeated measurements, add
raw counts and add monitors, *then* divide — never average already-normalized intensities — and
propagate Poisson errors through the division. The old `catalogue.py` LLM node — which also carried a
JSON-parse fallback for when the model returned junk — is deleted, not ported.

---

## 8. Closing the loop, minimally (goal 4)

The full event-stream session with a checkpointer (`DESIGN.md` §3) is Phase 3. Phase 2 closes the loop
with the **lightest mechanism that's still honest**: the card *is* the state.

```
   run reduction ──▶ report with LLM decisions + proposed exclusions (stable ids)
        ▲                         │
        │                         ▼
   re-run reduction        human acts on the review surface → tool writes state.json
   (idempotent; honors            (accept / reject each machine proposal; "unmask +6 meV")
    policy.toml + state.json)  +   human hand-edits policy.toml for durable rules
        └───────────────────────     ("scan 223 det3 always bad  # dead analyzer")
```

- **Two surfaces, matching D8 ownership.** Durable, human-known exclusions go in `policy.toml` —
  hand-edited and commented (the `EXCLUDED_SCANS`-style list). The per-run review — accepting or rejecting
  the machine's proposed QC decisions — happens through the **report's review surface**, and the *tool*
  persists the outcome to `state.json`; the human never hand-edits that JSON. `state.corrections` is an
  append-only log preserving the audit trail (which decisions were the LLM's, which the human's accept/
  reject). Both files are read at the start of every run.
- **Idempotence is the requirement that makes this safe.** Because reduction never deletes (0.4) and is
  keyed (§3), re-running over the same raw data with an edited card reproduces everything except where
  the human intervened. No partial state, no re-surfacing the same flag batch after batch (the failure
  `DESIGN.md` §10 calls out).
- **Why not the full session now:** a single box, batch (not live) reduction, and finished-only scans
  mean there's no concurrency or mid-scan truncation to defend against yet. The card-as-file loop gets
  goal 4 working and validated; the checkpointer/event-stream earns its complexity in Phase 3 when live
  mode and the fit stage need shared mutable state. Building it now would be machinery ahead of need.

This is the concrete, file-based form of P1 ("decisions freeze into config artifacts") and §10 ("human
review actions are the config being authored incrementally").

---

## 9. Module layout

```
isar/
  schema/
    scan.py            # ScanRecord (shared axes + N-spectrum signal block) + (de)serialize for §5.7
    axes.py            # scan_axes descriptors (§5.2); constant-Q-energy is the only populated case now
    card.py            # in-memory card = policy.toml (human, read-only) + state.json (machine); 2 loaders (D8)
  parsers/
    base.py            # plugin contract: parse_file / emit_file / card_template
    registry.py        # versioned catalogue; detect() reads date+headers → (facility, instrument, version)
    spec_base.py       # shared SPEC reader (#F/#E/#D/#O/#H/#S/#G/#P/#V/#L); used by all IXS facilities
    spec_s30.py        # thin card over spec_base: HERIX columns, 9 analyzers from #V4-12 → one record + emit
    ill_in8.py         # IN8 numor ASCII: parse (single detector) + emit
  reduce/
    qc_points.py       # §7.1 deterministic point checks → mask + point_flags (wrapped, survives mess)
    qc_scans.py        # §7.2 deterministic scan/spectrum checks → scan_flags / spectrum_flags
    adjudicate.py      # §7.3 LLM decision + shape-judgment + narration via LLMM (structured output)
    collate.py         # §7.5 deterministic groupby + combine-before-divide (ports Scan_Combining.py)
    report.py          # §7.4 Markdown + figures
  synth_format/
    run_builder.py     # truth-manifest → facility "run" (interleave alignment, fan detectors, monitor+time)
    quirks.py          # §4 injectable defects
    roundtrip.py       # parse(emit(truth)) ≈ truth assertions; the parser test harness
  driver/
    reduce_run.py      # §9 end-to-end: ingest → parse → QC → adjudicate → collate → write + report
  out/
    normalized/        # §5.7 ScanRecord JSON files
    reports/           # §7.4 Markdown reports + figures
tests/
  real_data/           # §4.1 real-file smoke tests against test_data/ (S30 + IN8); the fidelity guard
  roundtrip/           # §4 synthetic parse(emit(truth)) == truth; the coverage gate
```

Run (cmd):
```
python -m isar.synth_format.roundtrip      :: parser validation against synthetic truth (the gate)
python -m isar.driver.reduce_run <card>    :: reduce a real (or synthetic) run, emit normalized + report
```

---

## 10. Build order

1. **Schema + card** (`schema/`) — the §5.7 JSON `ScanRecord` and the §6 card, drafted against both
   instruments. D1 (JSON) and D3 (no floor) are baked in here. Nothing downstream is measurable without it.
2. **`spec_base` + `spec_s30` parser + emitter together** — the shared SPEC reader plus the HERIX card
   over it, capturing all nine analyzers (Q from `#V4`–`#V12`) into one record. Author the emitter
   alongside so step 3 can validate it.
3. **Both validation gates for S30** — (a) the synthetic **round-trip** `parse(emit(truth)) == truth`,
   no quirks yet (`tests/roundtrip/`), and (b) the **real-file smoke test** against
   `test_data/APS S30/FeSe_1-xS_x_1.spec_supplementary.dat` (`tests/real_data/`), the §4.1 fidelity
   guard. **Pause here for a human eyeball** of a few emitted SPEC files and the real reparsed record,
   the way Phase 1 paused after the generator. A parser is not "done" until both gates pass.
4. **Deterministic QC** (`qc_points`, `qc_scans`) + the quirk injectors. Score masking exactly against
   injected truth: zero false deletes, flag-not-delete enforced, every check wrapped to survive mess.
5. **`ill_in8` parser + emitter**, proving the card abstraction on a second instrument (single detector,
   absolute axis, COMND classification) — with its own round-trip *and* real-file smoke test against
   `test_data/ILL IN8/055177` (which carries a real disconnected sensor and an alignment COMND to catch).
   If the core needs special-casing, fix the card schema.
6. **Collate + report** (`collate`, `report`) — deterministic catalogue, combine-before-divide, coverage/
   gap map, per-scan/per-spectrum figures (matplotlib PNG, D6) with masks shown.
7. **LLM adjudication via LLMM** (`adjudicate`) — only after a Prompter probe (§7.3) shows the local tier
   is adequate. Structured output; off the critical path.
8. **Correction loop** (`driver` honoring `card.state`) — close goal 4; verify idempotent re-run.

Steps 1–4 already deliver goals 1 and (most of) 2 on synthetic data; 5 delivers goal 3's second
instrument; 6–8 deliver the report quality and goal 4.

---

## 11. Decisions (all resolved)

- **D1 — on-disk normalized format: JSON record per scan (§5.7).** Canonical store is one JSON file per
  physical scan, arrays inline, NeXus-aligned field names. Rationale: the store must be the *lossless
  source* you down-convert from, not a flat target (CSV/Markdown/NeXus are formatter outputs); and the
  LLM consumes a compact *derived* view, never the raw store, so JSON's verbosity costs no tokens.
- **D2 — per-analyzer Q: read from the file (§3, §5.3).** The 2025 S30 data logs all nine analyzers' Q in
  `#V4`–`#V12` (headers `Analyzer1_H/K/L` … `Analyzer9_H/K/L`), so read it directly (`q.source = reported`).
  The card's per-analyzer offset is a **fallback** for formats that don't log it. The build should confirm
  the `#V`↔`#H` mapping against the real file rather than assume it.
- **D3 — zero-count variance: no floor (§5.5).** Store variance honestly (0 for 0 counts), convention
  `none`; the downstream Poisson-likelihood fitter needs no floor. Guard: a stored `variance = 0` must
  never reach a `1/σ²` weight — handle it at use-time, never by flooring the stored data.
- **D4 — batch payload: per-card, and both modes are real (§3).** S30 commonly delivers a **growing SPEC
  file** (reparse + key-dedup); IN8 delivers **one file per scan** (trivially new-only). The card's
  batch-payload policy selects; the emitter models the growing-file case for S30 so the round-trip tests
  the harder path, and dedup-on-key makes new-only a no-op subset.
- **D5 — emitter fidelity: schema-faithful, with real files for byte fidelity (§4.1).** The synthetic
  emitter need only be schema-faithful (right fields/structure/values) for known-truth coverage; the
  **real-file smoke test** carries byte-level brittleness, which genuine facility output tests better than
  any synthetic emitter could.
- **D6 — report figures: matplotlib static PNG (§7.4).** No interactivity needed. *(Refined at
  implementation: the report renders in BOTH a self-contained HTML — same PNGs embedded inline as
  base64, one shareable file — and Markdown + sibling PNGs, plus a machine-readable `report.json`
  sidecar. Same figures, two renderers over one format-agnostic model; HTML is the human-facing
  "here is exactly what happened to your data" artifact, Markdown stays VCS-diffable.)*
- **D7 — deferred scan types/dimensions: deferred by design, MVP-focused.** The schema *represents*
  constant-E, angle/mesh/ramp scans, polarization channels, pump-probe, and multi-sample (§5.6); none are
  built in Phase 2. Activation trigger for each is real data that needs it (e.g. polarized HB-1 → the spin
  `channel` coord; routine maps → multi-axis QC). Listed so the boundary and the activation path are
  explicit. The current target is the MVP for the immediate need / showcase.
- **D8 — card on disk: TOML policy + JSON state (§6).** `<exp>.policy.toml` is human-authored, comment-
  bearing, and **read-only to the machine**; `<exp>.state.json` is machine-written (exclusions +
  corrections, appended each run). Driven by ownership: the correction loop writes state every run, so the
  hand-authored, annotated policy must live where the machine never writes, removing any clobber/round-
  trip-comment hazard. Repo rule: machine-written → JSON, human-authored → TOML. (Distinct from D1, which
  pinned the *ScanRecord* data output as JSON.)

**One standing operational item (not a blocker):** at the start of each beamtime, run the §6 pre-run
format check — point the session at a freshly captured file and confirm the parser/card/`format_version`
match what the instrument emits *now*, and re-confirm the `detectors` enable flags for the current sample.

---

## 12. Explicit non-goals (anti-creep)

- No fitting, no resolution measurement, no energy-zero calibration solved — schema carries the fields
  and flags; the solving is Phase 3.
- No scan types beyond constant-Q-energy **built** — the schema *represents* them (§5.6), Phase 2 writes
  no parser or QC for them.
- No persistent session / checkpointer / live driver — file-based idempotent re-run only (§8).
- No LLM in the path of a fitted number — the LLM adjudicates QC flags and narrates (§7.3, 0.5), it does
  not fit, and its QC decisions are always human-reviewable and non-destructive.
- No universal parser — per-experiment, per-format-version plugins reading from cards, exactly as
  `DESIGN.md` §6 insists.
- No physical deletion of any data point, ever — masks only (0.4, P5).

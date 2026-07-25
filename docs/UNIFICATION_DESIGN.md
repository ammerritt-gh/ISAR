# ISAR Unification — Assembly into a product (the tool grows a front door)

**Status:** Built (2026-07). The `isar` CLI + `isar/session/` layer described here are implemented and
tested (`tests/session/`); decisions U1–U8 held through the build. **One gate is deferred:** the full
83-scan S30 statistical comparison at M=200 (§10 step 5) is a multi-minute run left for a fast
machine — the calibration layer is proven to reproduce the gold check's per-scan inputs to machine
precision (step 3 gate, `tests/session/test_calibrations.py`), and the driver is proven end-to-end on
IN8 + a synthetic S30 held/idempotency/corrections smoke, but the end-to-end ω₀ comparison against
`results_gold.csv` has not yet been run. Decisions U1–U8 are in §9; open engineering items are marked
inline. User-facing companion: `docs/USING_ISAR.md`.

**Relationship to the master design:** This is the realization of `DESIGN.md` §3 (the architecture
diagram's spine), §4 (the event-stream model), §7.1 (ingestion & batching), the accumulated-state
half of §6 (the card fields that were designed but never added), and §10's minimal correction
channel. It is **build-order items 5–7 of `DESIGN.md` §14**, deferred while Phases 1–3 built the
analysis spine. It resolves master-design open questions §13 Q1 (live-first → re-runnable batch
driver, U1), Q2 (S30 batch payload → growing-file resend: re-parse + dedup, U4), Q4 (card format →
policy TOML + two JSON state files, §2, U3), and Q5 (checkpointer → plain JSON on disk, no
LangGraph, U2).

**Relationship to Phases 1–3:** Phases 1–3 built and validated every stage of the pipeline as
library code: parsers and the card (Phase 2), QC/combine/collate/adjudication (Phase 2),
calibration functions (Phase 3, §7), and the full analysis stack — structure enumeration, IC,
fitting, the error spine, model selection, verdict, confidence (Phase 3). **None of it is reachable
on new data.** The only end-to-end wiring lives in four `docs/*_check.py` scripts hardcoded to the
validation datasets. This phase adds *no new physics and no new statistics*: it assembles the
validated pieces behind a CLI so the pipeline runs on data ISAR has never seen.

---

## 0. Read this first — five framing corrections

**0.1 This is an assembly phase, not a capability phase.** Every fitted number must come out of an
already-validated code path. The two gold-check scripts (`docs/phase3_gold_check.py`,
`docs/phase3_gold_in8_check.py`) are the *recipes*: the driver generalizes their pipeline — same
calls, same order — replacing their hardcoded inputs (paths, scan lists, gold-CSV lookups) with
card policy, session state, and per-scan derivations. Where a check script and this design disagree
about how to call the analysis stack, the check script wins, because the check script is what was
scored. Anything genuinely new here (ingestion, ledger, prior building, report assembly) is
orchestration around the validated core, never inside it.

**0.2 The card finally completes.** `DESIGN.md` §6 specified the card as policy + *accumulated
state*, but `CardState` today holds only `exclusions` + `corrections`. The missing state half —
`resolution_versions`, `energy_zero_series`, `dispersion_prior`, `sample_map` — is exactly what a
driver needs to survive between batches. Completing the card *is* building the session (§2).

**0.3 The gate is a new user with new data, not a reproduced benchmark.** Reproducing the gold
results through the CLI is necessary (§10 step 5) but not sufficient. The definition of done
includes a fresh-eyes walkthrough: `pip install -e .` → `isar init <file>` → edit the card →
`isar run` → open the report, following only what `isar init` prints. If that walkthrough needs
tribal knowledge, the product is not done.

**0.4 User guidelines narrow, never widen.** The `[fitting]` guidelines (§6) realize P1's frozen
config (`SCAN_INITIAL_CONDITIONS` / `BAD_SCANS` as card artifacts, human-edited TOML). The user can
seed an expected ω, restrict the structure hypotheses, or exclude a scan — but the physics gates
(`isar/analyze/structures.py`) stay in charge of what is *admissible*. A guideline can remove
hypotheses from the enumerated set; it can never add one physics forbade. This keeps the
"LLM proposes / human proposes, deterministic code disposes" invariant intact for humans too.

**0.5 Idempotency comes from files-as-truth, not caching.** Every `isar run` re-parses every
manifest file (SPEC/numor parsing costs seconds) and re-*fits* only scans whose ledger entry is
new, previously held, whose content hash changed, **or whose applicable guidelines changed**
(§2.2's guideline hash — this is what makes a card edit an effective correction, §6). This
sidesteps stale record caches, handles
the S30 growing-file resend (re-parse + dedup by scan number is `DESIGN.md` §7.1's chosen
convention), and makes a crash before the single end-of-run save a clean no-op. Bootstrap fits are
the expensive thing (~tens of minutes for 83 scans at M=200); the ledger guarantees each scan pays
that cost once.

---

## 1. Product shape

One new console entry point (`pyproject.toml`: project renamed `isar-synth` → `isar`,
`[project.scripts] isar = "isar.cli.main:main"`) with the focused set of subcommands below. No more
than these; partial-stage debugging is served by the persisted intermediates and `--formats`, not by
an `isar fit`/`isar reduce` split.

| Command | What it does |
|---|---|
| `isar init <data-file-or-dir> [--out DIR]` | Detect the instrument (`registry.detect`), scaffold an experiment **workspace**: the matching card template copied **verbatim as text** (comments are the documentation — no tomllib round-trip), empty state, printed next steps. Default `--out` is `./isar_<instrument>_<yyyymmdd>/` (derived from the detected instrument and today's date), and `init` prints the exact `isar run ... --workspace <that dir>` command to use next. |
| `isar ingest <data>... --workspace DIR [--json]` | Parse data into the ledger **without fitting**: discover/classify files, parse+dedup, and upsert one `ingested` ledger entry per `(exp, scan, detector)` so parsed scans are visible through `workspace_api` before any `isar run`. Never fits, never deletes, never writes `card.policy.toml`. A later `isar run` re-queues every `ingested` entry. `--json` emits one machine-readable object (files by class, added/updated/unchanged scan ids, dedup notes, gaps). |
| `isar run <data>... --workspace DIR [--force] [--llm] [--M N] [--formats ...] [--detectors ...]` | The whole pipeline: ingest → parse+classify → route context events → QC/combine → (optional LLM adjudication) → calibration-gated fit with prior → verdict/confidence → collate → report. Idempotent (0.5); re-run per batch. |
| `isar status --workspace DIR` | Ledger summary without reparsing: counts by status/verdict, held scans with reasons, scan-number and coverage gaps, calibration versions, prior population. |
| `isar report --workspace DIR [--formats ...]` | Re-render the report from persisted state. Figure overlays use a cheap deterministic no-bootstrap refit; all numbers come from the ledger. |
| `isar score --workspace DIR --gold CSV` | Optional. Wraps the existing `isar.analyze.gold` harness against a run's results, for datasets where a reference CSV exists. Gold CSVs appear **nowhere** in the `run` path (U6). |

**The workspace** is the per-experiment directory the session lives in:

```
<workspace>/
  card.policy.toml    # human-owned; the machine NEVER writes it (Phase 2 D8 preserved)
  card.state.json     # machine-owned accumulated state — experiment-critical, not deletable
  session.json        # machine bookkeeping (manifest + ledger) — deletable, rebuilt by --force logic
  report/             # report.md/.html/.json, reduction.*, results.csv, figures/
```

**Both instruments, one driver.** Every instrument-specific decision routes through the card (two
new policy sections `[calibration]` and `[fitting]`, §3/§6, plus the existing fields). Zero
`if instrument ==` outside parsers and cards — the same generalization test Phase 2 imposed.

**During a beamtime** the operating loop is: batch of files lands → `isar run` (same command, same
workspace) → read the report → correct the card if needed (exclusions, expectations) → next batch.
A watch/daemon mode is explicitly deferred (§12); re-running the batch CLI *is* the live driver.

---

## 2. Session layer — new package `isar/session/`

The core of the phase; the CLI is a thin shell over it.

### 2.1 Schema additions (`isar/schema/card.py`) — additive only

```python
@dataclass
class CardPolicy:
    ...existing fields...
    calibration: dict = field(default_factory=dict)   # [calibration] — §3
    fitting: dict = field(default_factory=dict)       # [fitting] — §6
    sample: dict = field(default_factory=dict)        # [sample] — name override, require_alignment

@dataclass
class CardState:
    exclusions: list = field(default_factory=list)            # existing
    corrections: list = field(default_factory=list)           # existing
    resolution_versions: list = field(default_factory=list)   # [{version, method, source, created, ...}]
    energy_zero_series: list = field(default_factory=list)    # [{key, e0, source_scan}]
    dispersion_prior: list = field(default_factory=list)      # PriorPoint dicts (§5)
    sample_map: list = field(default_factory=list)            # [{from_scan, sample, source}]
```

*(Implemented additive fields, DECISIONS O7/O11: human-owned `CardPolicy.hypotheses`
(`[hypotheses.<name>]` tables — `docs/HYPOTHESES.md`), machine-owned
`CardState.hypothesis_snapshot` (resolved parameters + content hash), and machine-owned
`CardState.hypothesis_mismatch` (prior-vs-data disagreement evidence).)*

Two traps this must handle, both found in code:

- `_json.from_dataclass` **silently drops unknown TOML keys** (`isar/schema/_json.py`). Without the
  `CardPolicy` field additions, a `[fitting]` section vanishes without error — and so does a typo'd
  `[fittng]`. Mitigation (U7): `isar run` warns on any top-level TOML table that matches no
  `CardPolicy` field (compare `tomllib.load` keys against `dataclasses.fields`).
- Old `card.state.json` files must load unchanged — `from_dataclass` applies defaults for absent
  keys, so the additions are backward compatible by construction. A unit test pins this.

### 2.2 Workspace and ledger (`workspace.py`, `state.py`)

`Workspace.open(root)` / `.load() → (Card, SessionState)` / `.save(card, session)` — saves are
atomic (write-temp + `os.replace`) and happen **once, at the end of a successful run** (0.5).

`SessionState` (in `session.json`):

- `files: dict[path → {sha256, size, mtime, n_scans, format}]` — the ingest manifest.
- `scans: dict[scan_id → ScanEntry]` — the ledger. `scan_id = f"{experiment_id}:{scan_number}"`.
  `ScanEntry`: `content_hash`, `guidelines_hash`, `scan_number`, `scan_type`,
  `status ∈ {fitted, held_for_calibration, held_for_sample, excluded, context_applied, non_result}`,
  `detectors: dict[detector_id → FitOutcome dict]`, `reasons`, `provenance` (resolution version,
  zero source, prior size/source, M, seed, code versions — P6 made concrete).
  `guidelines_hash` is the hash of the *guidelines that applied to this scan* when it was
  processed — the matching `[fitting]` expectations/overrides/exclusions plus the relevant
  `[fitting]` defaults, canonically serialized. It is what makes the correction channel real: a
  card edit changes the hash for exactly the scans the edit touches, and only those re-enter the
  fit queue (§2.5 step 6).
- `data_paths` — remembered so `status`/`report` know what the experiment covers.
- `last_run` — summary counts, gap snapshot, timestamps.

The split (U3): `card.state.json` holds what the *experiment* owns (calibration, prior, exclusions,
corrections — small, human-reviewable, must survive); `session.json` holds what the *machine* owns
(manifest, ledger — deletable and rebuildable from files + a re-run).

### 2.3 Ingestion (`ingest.py`) — `DESIGN.md` §7.1 realized

- `discover(data_paths, card) → IngestPlan`: walk files; `registry.detect` each; unrecognized files
  (e.g. the `.nxs` NeXus twins beside every IN8 numor) are **listed, not fatal**. Partition into:
  measurement-format files matching the card's `(facility, instrument, format_version)`, companion
  calibration files (matched by `[calibration].companion_glob`, §3), and foreign/unknown.
- `parse_all(plan, card) → list[ScanRecord]`: parse via the card-pinned plugin; **dedup by
  `(experiment_id, scan_number)` keeping the last occurrence** (growing-file resend); **sort by scan
  number, not arrival**.
- One piece of enabling work hides here: `registry.REGISTRY` **starts empty today** — nothing in
  the package registers the three real plugins (only the tests do, with a dummy; the check scripts
  import parsers directly). Wiring registration (imports + `registry.register(...)` in
  `isar/parsers/__init__.py` or at CLI startup) is part of build step 2 and is an allowed additive
  change; until then only `registry.detect` works standalone.
- `file_gaps(records)`: scan-number sequence holes per experiment — the file-level gap detection
  that `collate.detect_gaps` (which covers Q-value space) does not do. Gaps land in `status` and the
  report header; a hole in the sequence nobody notices for hours is the worst failure for a speed
  tool.

### 2.4 Event routing (`router.py`) — `DESIGN.md` §4 realized

`route(records) → RoutedBatch{measurements, calibrations, alignments, environments}` — pure
dispatch on `record.scan_type`, which the parsers already set (classification never leaks
downstream; `DESIGN.md` §7.2). Then `apply_context_updates(...)`, **before any measurement is
fitted**:

- **calibration events + companion files** → `calibrations.refresh` (§3): update
  `card.state.energy_zero_series`, bump `card.state.resolution_versions` when the series or widths
  changed. Recalibration is first-class and versioned; every subsequent fit records which version
  it used.
- **alignment events** → append to `card.state.sample_map` (v1: recorded and reported; hard
  identity *gating* — hold measurements with no sample context — only when
  `[sample] require_alignment = true`, since single-sample experiments shouldn't pay the gate).
- **environment events** → ledger note (v1).

### 2.5 The driver (`driver.py`) — the per-scan pipeline, generalized from the gold checks

`run_batch(workspace, data_paths, *, force, llm, M, formats, detectors) → RunSummary`:

1. Load workspace; merge `data_paths` into the session.
2. `ingest.discover` + `parse_all` (everything, every run — 0.5).
3. Per record: `combine.reduce_record(record, card)` then `qc.qc_record(record, card)` (both are
   safe no-ops where not applicable; QC never raises).
4. `route` + `apply_context_updates` — context **before** measurements.
5. Optional LLM adjudication (§8): only if `--llm` or the card's `[adjudication] enabled` says so.
6. **Fit queue** = measurements that are new, content-drifted, guideline-drifted (stored
   `guidelines_hash` ≠ current, §2.2), or previously `held_*` (the bootstrap-gate re-examination
   falls out of this for free: held scans re-enter the queue every run until calibration exists),
   in scan-number order. Per scan × enabled detector:
   - exclusions first (`card.state.exclusions`, `policy.exclusions_seed`, guideline excludes) →
     ledger `excluded` with reason;
   - `calibrations.resolve(...)` (§3) → no zero available ⇒ ledger `held_for_calibration`
     (`verdict.held_for_calibration(reason)` supplies the verdict object) — **never E0 = 0**;
   - prior = user expectation (§6) if one applies, else `session_prior.prior_for(...)` (§5);
   - `build_context` → `fit_one` (both below) → `FitOutcome` → ledger; resolved-and-folded
     outcomes feed `session_prior.add_point`.
7. `collate.collate(records, card)` over **all** records (the catalogue is a whole-experiment
   view, not a per-batch one).
8. Write the run report (§7) + the existing reduction report (`report.build.generate_report`,
   kept as the reduction-detail companion at `report/reduction.*`).
9. Single `workspace.save(card, session)`.

`build_context` constructs `ScanContext.from_scan(record, detector_id, card, center=...,
gauss_fwhm=..., lorentz_fwhm=..., prior_omega=...)` and then folds in two guards the IN8 check
carried as hardcoded constants, now card-driven `[fitting]` defaults: **monitor-short masking**
(`monitor < monitor_frac × preset`, default 0.9, monitor-counting scans only) and the
**undersampled guard** (`usable points < min_fit_points`, default 6 ⇒ non-result, not a fit).

`fit_one` is the check scripts' recipe verbatim: `enumerate_structures(ctx)` → guideline structure
filter (§6) → `loop.fit_admissible_structures(ctx, minos=True)` → preliminary
`select.select_models` → `errors.attach(..., M=M, seed=seed, with_profile=True)` on survivors →
final `select.select_models` → `verdict.classify(results, ctx, sel, prior=prior)` + confidence →
`FitOutcome{verdict, confidence, omega0, interval, modality, gamma, gamma_provisional, best_label,
cost_per_dof, q, T, sample, prior_used, calib, reasons}`. Seeds are `seed_base + scan_number`
(deterministic across re-runs). `gamma_provisional = True` whenever the card's
`energy_status == "absolute"` (TAS 1-D resolution path — Γ honest-caveated until the 4-D engine,
§12).

---

## 3. Calibration wiring (`calibrations.py`) — the orphaned `isar/calibrate/` finally called

Card-driven by the new `[calibration]` policy section; three methods cover the three known cases:

| Method | Card | What it does |
|---|---|---|
| `bragg_companion` | S30 supplementary | Elastics live in the **companion raw `.spec`**, not the measurement file. For each companion (matched by `companion_glob`): `bragg.read_elastic_scans(path, analyzer=..., k_max=...)` → `build_energy_calibration(scans, key=zero_key)` → persist zeros + `resolution_by_slit` into `card.state`, bump the version. |
| `elastic_records` | S30 raw | Elastic scans arrive **in-stream** as parsed calibration records; construct `ElasticScan` objects from them and feed the same `build_energy_calibration` path. |
| `cooper_nathans` | IN8 / TAS | Stateless per-scan analytical resolution (`isar.synth.resolution.cn_energy_fwhm`); one provenance entry `{method: cooper_nathans, per_scan: true}` in `resolution_versions`. |

`calibration_view(card)` rebuilds the run-level calibration object from `card.state` — **state, not
memory, is authoritative**, which is what makes a restart a non-event.

`resolve(record, detector_id, card, runcal) → CalibResolution{center, gauss_fwhm, lorentz_fwhm,
zero_source, resolution_source, resolution_version, held_reason}`:

- **S30**: `center = runcal.zero(scan_number)`; `None` ⇒ held (a missing zero is a flagged
  non-result, never 0.0 — `DESIGN.md` §7.4). The per-scan slit comes from the measurement record's
  own header — `raw_header["P"][slit_p_group][slit_p_index]` (card knobs; defaults 17/4, the
  convention `bragg._slit` already uses). **This closes a real generalization gap: the gold check
  took the slit from the gold CSV row, which new data does not have.** Missing slit table entry ⇒
  `default_resolution`, provenance-flagged `resolution_source = "default"` (risk R1, §11).
- **IN8**: `center = 0.0` (`energy_status == "absolute"`), `gauss_fwhm = cn_energy_fwhm(record)`,
  `lorentz_fwhm = 0.0`.

Housekeeping: a one-line re-export `isar/calibrate/tas.py` makes `isar.calibrate` the public home of
the Cooper-Nathans functions without moving validated code out of `isar/synth/resolution.py`; the
`EnergyCalibration` name collision (schema per-record vs calibrate run-level) is handled by aliasing
the latter `RunEnergyCalibration` at session import sites — neither class is renamed.

---

## 4. What stays exactly as it is

For clarity of scope — these are **used, not modified**: all parsers and the registry; `qc`,
`combine`, `collate`; the entire `isar/analyze/` stack (structures, ic, loop, errors, select,
verdict, confidence, gold); `elastic`, `bragg`, `energy_zero` internals; the report renderers
(`findings`, `markdown`, `html`, `captions`, `figures`, `build`). The four `docs/*_check.py`
scripts run unmodified throughout — they are the validation record and the regression gate (§10
step 7). If the assembly appears to *need* a change inside `isar/analyze/`, that is a design
smell — stop and re-check against 0.1.

---

## 5. The prior builder (`session_prior.py`) — the running dispersion prior, finally running

Generalizes the gold check's inline leave-one-out `predict_omega` from gold-CSV rows to accumulated
fits. Lives in `session/` because `isar/analyze/prior.py`'s contract is "consumed, never built
here."

- **PriorPoint** (stored in `card.state.dispersion_prior`): `{scan_id, sample, t_bin, component,
  q_value, q, omega0, sigma, modality, verdict}`. The varying-Q component per (sample, T-bin) is
  identified with the same dominant-spread logic `collate` uses for coverage.
- **`add_point`** — append-only, **gated to `verdict == resolved` AND `modality == fold`**. A fork
  averaged into the prior poisons every neighbor (Phase 3 §4's whole point); flagged and
  model-incomplete outcomes stay out in v1 (conservative; revisit with data). A re-fit supersedes
  its own scan's entry.
- **`prior_for(sample, T, component, q_value, exclude_scan_id)`** — pool same-sample points within
  the T-bin (`t_round` reused from the card's `[collation]`); fewer than 2 ⇒ pool across T (the
  gold check's rule). ≥2 points at ≥2 distinct q ⇒ local linear fit, `sigma = max(0.3 meV, resid
  std)`; one point ⇒ that value with a floored sigma; none ⇒ `None`. Every consumer already handles
  `None` (IC grid unnarrowed, confidence `s_prior` neutral at 1.0).
- Returns `PhononPrior(omega0, amplitude=None, omega0_sigma)`. `amplitude=None` means the
  detection-power test abstains — the *underpowered* verdict stays conservative in v1; carrying a
  neighbor-median amplitude to enable it is a marked extension, because it is precisely the Phase-4
  hook ("needs ~N more counts").
- **Provenance**: every `FitOutcome.prior_used = {omega0, sigma, n_neighbors, source: auto|user}` —
  lands in `results.csv` and the report (P6).

The gold check's prior-independence demonstration (toggling the prior moved ω₀ by ≤ 0.0001 meV —
`out/phase3_gold/FINDINGS.md`) is the safety argument for swapping the LOO gold prior for the auto
prior; but the prior *does* enter confidence (`s_prior`) and fork-breaking, so validation compares
dispositions and ω₀, never confidence values (risk R4).

---

## 6. User fitting guidelines — `[fitting]` in the policy TOML

The human's control surface, scoped to three capabilities (settled with the creator): expected-ω
hints, per-scan overrides, exclusions/holds. Deliberately **not** included: global fit-policy knobs
(bootstrap M, error-method selection, verdict thresholds) — those stay code defaults and CLI flags
(`--M`), so a card can't quietly detune the validated machinery.

```toml
[fitting]
detectors = "enabled"           # or ["ana7"] — which spectra to fit (default: card-enabled)
omega_window = [0.0, 0.0]       # optional global sanity window (meV); [0,0] = off
monitor_frac = 0.9              # monitor-short point mask (was hardcoded in the IN8 check)
min_fit_points = 6              # undersampled guard (ditto)

[[fitting.expectations]]        # expected dispersion — by scan list OR Q-range, optional T
scans = [223, 224]              # or: q = {component = "k", min = 0.10, max = 0.35}
omega0 = 5.2
omega0_sigma = 0.5
structures = ["elastic+stokes+bg"]   # optional: NARROW the enumeration to these labels

[[fitting.overrides]]           # per-scan surgical control
scan = 231
exclude = true                  # never fitted; reported as excluded-with-reason
# or: omega0 = 4.0, omega0_sigma = 0.3   → per-scan IC seed
```

How guidelines flow into the existing machinery **without touching `isar/analyze/`**:

- An expectation becomes a `PhononPrior(source="user")` — it narrows the IC grid
  (`ic.omega_d_grid(prior_omega=...)`) and seeds verdict/confidence, taking precedence over the
  auto prior where it applies. Per-scan ICs are per-scan expectations; a raw parameter-vector IC
  override is out of scope v1 (the grid+NNLS IC is the validated mechanism — 0.1).
- `structures` filters the labels `enumerate_structures(ctx)` emitted. Narrow-only (0.4); a filter
  that would empty the set warns and keeps all.
- `omega_window` is a post-fit demotion applied at the **report/ledger level, not the verdict
  level** (the verdict buckets in `isar/analyze/verdict.py` do not change — §4): the computed
  verdict stays as classified, and the `FitOutcome`'s *disposition* is demoted from delivered to
  flagged with reason `"omega outside user window"`. Flag, don't delete. (Note: "flagged" is a
  report-level disposition — the grouping `gold.verdict_disposition` maps `model_incomplete` into —
  not a verdict string.)
- Exclusions get ledger status `excluded` with the reason echoed in the report, honored at the same
  point as `exclusions_seed` and `card.state.exclusions`.

**The v1 correction channel** (`DESIGN.md` §10, minimal form): the human reads the report, edits
`card.policy.toml` (an exclusion, an expectation), re-runs. The per-scan `guidelines_hash` in the
ledger (§2.2) makes the correction take effect on exactly the affected scans — the edit changes
their hash, they re-enter the fit queue, nothing else refits; `card.state.corrections` keeps the
audit trail. A richer review surface stays future work (§12).

---

## 7. The run report — the product

New `isar/report/run_report.py` + `isar/report/fit_figures.py`. The fit-overlay and
dispersion-summary figures exist today only inside the check scripts; `fit_figures.py` is their
first library home (factored, not rewritten). Everything else reuses the Phase-2/3 report stack
(`FindingsDoc`, `captions.save_captioned`, `figures.coverage_figure`). **No gold CSV anywhere in
this path** — a report must never require a reference answer (U6).

Contents, in order:

1. **Header** — counts: fitted / resolved / flagged / held(by class) / excluded / non-results;
   new-this-run vs experiment total; scan-number gaps.
2. **Triage table** — per scan × detector: verdict, disposition, ω₀ ± interval, confidence, best
   model, prior provenance, calibration provenance.
3. **Dispersion summary** — ω₀ vs the varying Q component per (sample, T-bin), colored by verdict,
   intervals as error bars, the running prior overlaid, held/declined as rug marks.
4. **Coverage & gaps** — the collation coverage grid + value-space gaps.
5. **Held/declined explanations** — every non-result with its machine-readable reason; adjudication
   narratives when the LLM ran, `"no LLM consult"` stamps when it didn't.
6. **QC/removal ledger** — nothing deleted, everything listed (masked points shown).
7. **Fit overlays** — all flagged scans always; delivered scans up to a card-capped count.
8. **`results.csv`** — the machine-readable sidecar: scan, detector, sample, T, h, k, l,
   q_component, q_value, verdict, disposition, confidence, omega0, omega0_err, interval_lo/hi,
   gamma, gamma_provisional, best_model, cost_per_dof, zero, zero_source, gauss/lorentz_fwhm,
   resolution_source, resolution_version, prior_omega, prior_sigma, prior_n, prior_source, reasons.

---

## 8. LLM placement — unchanged principles, one switch

The pipeline is fully deterministic by default. `--llm` (or the card's `[adjudication] enabled`)
turns on the existing Phase-2 adjudication layer (`adjudicate.adjudication_config(card)` →
`make_client(cfg)` → `adjudicate_records(...)`) for **QC-flag adjudication only**, off the critical
path: an LLMM failure degrades to a `"no LLM consult"` flag on the affected scans and the batch
proceeds (degraded mode per `DESIGN.md` §9). No LLM call sits in the path of any fitted number.
The Phase-3 residual-diagnosis LLM pass stays future work (§12).

---

## 9. Decisions (settled — record of why)

- **U1 — Re-runnable batch CLI, not a daemon.** The batch driver run per batch *is* the live driver
  (`DESIGN.md` §13 Q1); a `--watch` mode is sugar that can wrap `run_batch` later without design
  changes. Confirmed with the creator.
- **U2 — Plain JSON persistence, no LangGraph/DB.** Single box, one writer, restart-safe via
  files-as-truth (0.5) + atomic end-of-run save. A checkpointer adds a dependency without adding a
  property we need (`DESIGN.md` §13 Q5).
- **U3 — Two state files.** `card.state.json` = experiment-owned accumulated state (survives,
  human-reviewable); `session.json` = machine bookkeeping (deletable, rebuildable). Resolves §13 Q4.
- **U4 — Re-parse everything, re-fit selectively** (0.5). Content hash =
  `sha256(canonical-JSON(record))`.
- **U5 — LLM off by default** (`--llm` / card opt-in), adjudication only. Confirmed with the creator.
- **U6 — Gold CSVs are for `isar score` only.** The run path must work on data that has no
  reference answer — that is the entire point of the phase.
- **U7 — Unknown card sections warn.** Compensates `_json.from_dataclass`'s silent-drop so a typo'd
  section name can't silently disable guidelines.
- **U8 — Guidelines narrow, never widen** (0.4). Physics gates own admissibility; global fit-policy
  knobs stay out of the card. Confirmed with the creator (expectations, per-scan overrides,
  exclusions/holds are in; bootstrap-M-style knobs are out).

---

## 10. Build order with verification gates

Each step is mergeable and gated before the next. Windows note: test-data paths contain spaces
(`test_data/APS S30`, `test_data/ILL IN8`) — `pathlib` everywhere, quoted CLI examples.

0. **Baseline.** `pytest -m "not slow"` green; keep `out/phase3_gold/results_gold.csv` and
   `out/phase3_gold_in8/results_in8_gold.csv` as the comparison references.
   **Known data-state gap (found in doc verification, 2026-07):** `test_data/ILL IN8/
   IN8_fit_results.csv` is currently missing from disk (test_data is gitignored — it cannot be
   restored from git) and `out/phase3_gold_in8/` has never been generated on this machine.
   Until the human restores/reconfirms that CSV (only 055182 ≈ 4.2 meV and 055183-REJECT are
   vetted rows; the rest were Phase-1 placeholders — see the check script's docstring),
   `docs/phase3_gold_in8_check.py` and `tests/real_data/test_in8_gold_smoke.py` cannot run, and
   every IN8 gate below (steps 3, 5, 6, 7) is blocked. Resolve this at step 0, not step 5.
1. **Schema + workspace + ledger.** Card additions (§2.1), `state.py`, `workspace.py`. Gates: old
   state JSON loads; `[fitting]`/`[calibration]` survive the TOML→dataclass round-trip (the
   silent-drop trap); unknown-section warning fires; atomic save.
2. **Ingest + router + CLI skeleton** (`init`, `status`; entry point registered). Gates:
   `isar init` on an IN8 numor and on the S30 supplementary file scaffolds the right cards; IN8
   directory ingest skips `.nxs` gracefully; growing-file dedup proven with `spec_s30.emit_file`
   synthetics (emit N scans then N+k — only k new); ordering by scan number.
3. **Calibration layer** (§3). Gates: same fitted-elastic count, zero-series span, and
   `resolution_by_slit` as the gold check prints; **record-derived slit == gold-CSV slit for all 83
   scans** (proves the per-scan slit path); IN8 `cn_energy_fwhm` matches the recorded
   `res_gauss_fwhm` column.
4. **Prior builder + guidelines** (§5, §6). Gates (unit): linear-in-K recovery on a synthetic
   dispersion; fork exclusion; same-T-vs-pooled fallback reproduces `predict_omega` on the gold
   rows; structure filter never empties; user-beats-auto precedence; **guideline-hash
   invalidation** — editing an expectation/override/exclusion changes `guidelines_hash` for
   exactly the matching scans and no others.
5. **Driver + `isar run`** (§2.5) — the heavyweight gate, run locally:
   - **S30**: workspace with `[fitting] detectors = ["ana7"]`; run on **explicit files, not the
     directory** — `isar run "test_data/APS S30/FeSe_1-xS_x_1.spec_supplementary.dat" ...` with
     `[calibration] companion_glob = "FeSe_1-xS_x_1.spec"`. (Historically the directory also held a
     *second experiment* — `FeSeS_1.spec_supplementary.dat`, same detected format — plus two more
     raw `.spec` files; those have been relabeled `*.UNUSED` on this machine so directory ingest no
     longer sees them, but the general multi-experiment-per-directory case is real and allowed —
     ingest must not assume one experiment per directory, and this gate pins inputs to what the
     gold check read.) Against `results_gold.csv`: same
     held set; **0 gold-REJECT scans resolved (safety — the load-bearing property)**; ω₀ median
     |Δ| ≲ 0.05 meV on delivered scans; dispositions within a few scans. Confidence values may
     drift (auto prior ≠ LOO gold prior) — gate on dispositions and ω₀, not confidence (R4).
   - **Corrections**: on the finished S30 workspace, add an `[[fitting.overrides]]` exclusion for
     one fitted scan and re-run → exactly that scan changes status, nothing else refits; remove
     it → exactly that scan refits (the guideline-hash channel, end-to-end).
   - **IN8**: `isar run "test_data/ILL IN8" ...`; anchor 055182 |Δω₀| ≤ 0.5 meV; 055183 **not**
     resolved.
   - **Idempotency**: immediate re-run fits 0 scans; two-half sequential ingest ends identical to
     one-shot; deleted `session.json` rebuilds to the same `results.csv`.
   - **Bootstrap gate**: S30 run without the companion `.spec` → every measurement
     `held_for_calibration`; add the file, re-run → held scans re-examined and fitted.
6. **Run report + `report`/`status`/`score`** (§7). Gates: reports render for both instruments with
   zero instrument branches; `isar score --gold FeSe_1_fit_results.csv` reproduces the gold
   confusion counts from the step-5 run.
7. **Fresh-eyes walkthrough + regression** (0.3). All four `docs/*_check.py` run unmodified;
   `pytest` green; the new-user pass succeeds following only `isar init`'s printed instructions;
   `README.md` quickstart matches reality.

---

## 11. Risks (found in code, ranked)

- **R1 — Non-reference-analyzer resolution on S30.** The zero is global (mono drift — safe to
  share), but measured widths exist only for the analyzer column fitted (Ana7 validated); other
  enabled analyzers fall to `default_resolution`, provenance-flagged `resolution_source =
  "default"` in results and caveated in the report. Extension: per-analyzer elastic fits once
  per-analyzer slit P-indices are confirmed.
- **R2 — The FeSe raw `.spec` through `spec_s30_raw.parse_file` is unvalidated** (that parser was
  built against `Fe2Mo3O8_trimmed.spec`). Hence `bragg_companion` reuses `bragg.read_elastic_scans`'
  own reader — the validated gold path. Ingest must treat companion-glob matches as calibration
  sources, not measurement files, or the registry will happily double-parse them.
- **R3 — Silent TOML key drop** — mitigated by U7.
- **R4 — Confidence/fork-resolution drift vs gold under the auto prior** — `s_prior` and
  fork-breaking consume the prior; gate step 5 on dispositions + safety + ω₀ residuals only.
- **R5 — IN8 `experiment_id` (EXPNO) can be empty** → empty sample key in collation/prior.
  `[sample] name` card override; the driver substitutes it.
- **R6 — Runtime.** Full S30 at M=200 is tens of minutes; acceptable because the ledger makes it
  once-per-scan, and `--M 60` is the smoke path.
- **R7 — `EnergyCalibration` name collision** (schema vs calibrate) — alias, never rename (§3).
- **R8 — TAS Γ stays provisional** (1-D resolution; 4-D engine deferred) — `gamma_provisional`
  carried end-to-end so the report stays honest.

---

## 12. Explicit non-goals (anti-creep)

- **Watch/daemon mode** — wraps `run_batch` later (U1).
- **TAS 4-D resolution convolution** (Phase 3 D5) — Γ stays provisional on TAS; ω₀ is the product.
- **Phase-3 step-7 remnants** (evidence-weight wiring into selection) — orthogonal to assembly.
- **Migrating the check scripts onto the driver** — explicit follow-up once step-5 equivalence is
  proven; until then they are the validation record and must not change.
- **A review UI / rich correction surface** — v1's correction channel is card-edit + re-run (§6).
- **The Phase-4 planner** — this phase builds its inputs (prior, confidence, non-result buckets,
  provenance), not the planner.
- **New instruments (ID28, BL43LXU)** — the card/registry design already has their slots; authoring
  those cards is its own (small) later task, and nothing here may make it harder.

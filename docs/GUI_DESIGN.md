# ISAR GUI — Design

**Status:** Design draft, 2026-07-11; Phase 0 + Phase A implemented on branch
`ISAR-initial-GUI` as of 2026-07-11 (see §0bis for corrections found during that work). Every
capability in this document is **Planned** unless explicitly marked otherwise — Phase B and
Phase C remain entirely in the Planned voice. This document follows the discipline of
`DESIGN.md`: nothing is described in the implemented voice unless it is.

**Audience.** This document is a self-contained handoff. It assumes the reader knows `DESIGN.md`
(the master design) but has **no access to the design conversation** that produced this document.
All rulings from that conversation are restated here in full; nothing load-bearing is implied.

**Relationship to other documents.** Subordinate to `DESIGN.md` (its principles P1–P6 bind this
document). Sibling to `docs/UNIFICATION_DESIGN.md` (workspace/session internals the GUI reads)
and `docs/MEASUREMENT_STEERING.md` (the drive loop the GUI observes and nudges). Decision IDs
introduced here use the **G-series** and should be migrated into `docs/DECISIONS.md` when
accepted.

---

## 0. Summary of rulings (the fixed plan)

These were decided in design review and are the constitution of this document. Each is expanded
in the referenced section.

| ID | Ruling | § |
|---|---|---|
| G1 | GUI lives in the ISAR repo as a separate package, optional install extra, launched `isar view`. Boundary is enforced in **imports**, not repo topology. | §2 |
| G2 | The GUI is a **viewer and a proposer, never a third source of truth**. It reads workspace artifacts; it writes only append-only directives through the existing correction/mutation channel. | §3 |
| G3 | Three artifact classes: **immutable observations**, **versioned interpretations**, **append-only directives**. The GUI writes only class three. | §3 |
| G4 | Every GUI write compiles to exactly one of **four directive types**: objective edit, prior edit, fit-structure correction, direct proposal. Natural language is a fifth *input device*, not a fifth type. | §4 |
| G5 | Transport is a **directive inbox**: `workspace/inbox/` of timestamped JSON, drained by the driver at the top of each decide step, moved to the ledger. Atomic write-temp-then-rename. | §5 |
| G6 | **The driver remains the sole owner of the instrument.** Human directives are queue entries with priority `human` that still pass the driver's validate step (triangle closure, dark angles, limits). Append and nudge; never seize. | §5 |
| G7 | **Sandbox exception**: for interactive refitting the GUI may import the deterministic fit engine's frozen-config entry point, in-process, for live preview. Sandbox results are ephemeral and visually marked; **commit** writes a directive and the official number comes from the workspace re-run. | §6 |
| G8 | GUI snapshot rendering and report/diagnostic figure rendering are **the same code** at two zoom levels. One figure module, two consumers. | §7 |
| G9 | 4D honesty: every 2D view is an **explicit, on-screen-declared projection** (plane + integration window). Measured strips render as strips, gaps as gaps; any interpolated/modeled surface is labeled as a model. | §8 |
| G10 | Semantic grouping **is the card's collation policy**; changing it is a card edit. Display-side overlays (e.g. waterfall by T) are pure view state and never touch the card. | §9 |
| G11 | Coordinate math: a **metric/UB layer** (lattice + orientation vectors u, v → scattering plane, r.l.u. ↔ Å⁻¹, non-rectilinear axes) lives in **core, Qt-free**, sourced from the TAS_MCP / TAVI math. TAVI stays the sole resolution authority; the GUI grows **no third opinion** on scattering triangles — feasibility is consulted, not reimplemented. | §10 |
| G12 | Donor repositories: **ACIDD** (cut gesture, coordinate-layer-under-image-view, templates, session persistence), **PUFFIN** (live-parameter fitting grammar, MVVM + worker threads), **ISAR** (everything that thinks). All three are GPL-3; code may be lifted with copyright notices. The GUI is **assembly, not invention**. | §11 |
| G13 | Phasing: Phase 0 workspace read API → Phase A read-only viewer → Phase B planner → Phase C directives/sandbox/live dashboard. Read-only first so nothing destabilizes the pending TAVI SVP. | §13 |

---

## 0bis. Corrections from Phase 0/A implementation (2026-07-11)

Phase 0 + Phase A implementation surfaced five points where the design record above needed
correction. These layer on top of the original text (a design record is not rewritten); each
affected § carries a one-line pointer back here.

1. **The ledger is two artifacts, not one** (§4.4, §5.1, §7.1). `session.json` is the analysis
   **snapshot** (`SessionState`, rebuildable from re-runs). The append-only event stream is a
   separate file, `report/session_log.jsonl` (driver events); `ws.session_events()` reads the
   JSONL, not `session.json`. The future `directive.consumed` event (Phase C) is a new event
   kind in the JSONL stream, not a field added to `session.json`.
2. **Lattice/orientation is a versioned epoch series, not card policy** (§10). This supersedes
   "from the card's lattice": scans stay in absolute instrument coordinates, immutable.
   `CardState.lattice_epochs` (a class-two artifact, precedent: `resolution_versions`) maps
   absolute → HKL. Epochs change mid-experiment (transition, realignment, plane swap) and can be
   corrected retroactively — append a new epoch, re-derive HKL, never edit the scans — and
   analysis/grouping keys on HKL, not the acquisition-time orientation. Implemented: schema +
   stateless resolver in `isar/geometry/epochs.py` (identity-through-acquisition-epoch until
   absolute coordinates land in Phase B). See G15.
3. **`evaluate_model`/`reconstruct_curve` reality** (§7.2). `reconstruct_curve` re-solves
   amplitudes by NNLS against counts — it is not a pure evaluator of stored parameters.
   Resolution: `FitOutcome.amplitudes` is now persisted additively; `ws.evaluate_model` is pure
   (stored-params → curve, no refit) when amplitudes exist on the fit outcome, and falls back to
   the counts-based `reconstruct_curve` path on older ledgers that predate the field. Both share
   `assemble_curve` (`isar/report/audit.py`) so the two paths cannot diverge in math.
4. **matplotlib is a hard dependency, not Qt-free-by-extension** (§2, §14). The campaign path is
   matplotlib-free at import time only. `tests/test_qt_free_core.py` enforces Qt-freeness
   (`PySide6`/`pyqtgraph` never load on `import isar`) and a scipy-free `isar.workspace_api`
   import (scipy loads lazily, only on the `evaluate_model` audit fallback) — it does not assert
   matplotlib-freeness, since `isar/plotspec/backends/mpl.py` is explicitly allowed to import
   matplotlib.
5. **Dark angles / mechanical limits have no numeric data source yet** (§10). TAS_MCP's PUMA
   limits are `None`/soft placeholders; TAVI has no dark-angle configuration; TAVI's assembled
   `/validate` verdict is Qt-entangled (HTTP-only, not importable in-process). Consequence:
   planner forbidden-zone previews (Phase B) will be kinematics-only (triangle closure / Ewald
   sphere) until a per-instrument config is authored TAVI-side — the HTTP `/validate` call stays
   the sole authority for the real go/no-go, exactly as G11 already ruled.

---

## 1. Purpose & scope

### 1.1 Why a GUI

ISAR's console-and-report workflow hides the thing the user actually needs to *see*: data. What
are the fits versus the data; what are the error bars per interval-ladder tier; what did model
selection choose and why; how is the grouped data supporting a dispersion; where is the driver
planning to measure next and on what evidence. Buried in an after-the-fact report, none of that
gives the user control or visibility *during* the experiment.

### 1.2 The two halves

The GUI is two products with one window, and the split is load-bearing:

1. **Read-only** — see what ISAR is doing: current fit, results so far, growing coverage,
   the drive loop's reasoning. Side-effect-free by construction. Bonus: because GUI snapshots
   and report figures share rendering code (G8), building this half *improves the reports* —
   what the user wants to watch live is exactly what goes into the diagnostic, just as smaller
   snapshots.
2. **Directive** — nudge ISAR: edit objectives, adjust priors, correct fit structures, insert
   scans. Not merely a friendly way to compose run flags, but the channel by which a user runs
   ISAR *during* an experiment. Everything in this half is append-only (G2/G3) and passes
   through the driver's own validation (G6).

### 1.3 MVP user journey (the six items)

The MVP scenario is **"measure this phonon dispersion from X to Y."** These six items are the
acceptance narrative; §12 maps them to components.

1. Load a reciprocal-space map of the material (tied to its space group — non-rectilinear cells
   must not trip the display), click two points in the oriented scattering plane (e.g. HK0),
   see suggested scan spacing, amend, and **approve** the series. Data lives in the full
   (Q₃, E) 4D space; views are projections (G9).
2. Swap to e.g. the (H,1,0) plane with energy on the y-axis; watch the heatmap grow as scans
   land; see per-point phonon-energy estimates with error bars.
3. Click a struggling Q–E scan → see the 1D cut: raw data with error bars, ISAR's best-guess
   fit. Nudge Bayesian priors to separate two close branches. Or mark a key scan for
   over-counting ("this goes in the paper — bring down the Poisson scatter over this region").
4. Monitor progress; see ISAR's running guess at the dispersion; edit it in the GUI to steer
   subsequent scans.
5. At any time, issue natural-language commands: "scan at this point, fit the peak visible
   here, bring the error on the energy center within 0.1 meV."
6. Swap to a different temperature; compare dispersions; waterfall the 1D cuts at a key q
   by temperature.

### 1.4 Non-goals

- The GUI is **not** a beamline control program. It never talks to the instrument; only the
  driver does (G6).
- The GUI is **not** a second fitter of record. Sandbox fits (§6) never become delivered
  numbers directly.
- No paper-grade figure editor. Export what the report renders; final figures are made later
  from frozen config, per `DESIGN.md`.
- No web frontend in v1. Desktop Qt, same as PUFFIN/ACIDD, same box as the workspace.

---

## 2. Placement & packaging (G1)

> Corrected — see §0bis item 4: the Qt-free-core test does not assert matplotlib-freeness;
> matplotlib is a hard dependency and only the campaign path is matplotlib-free at import time.

**Decision: same repo, separate package, contract-coupled.**

- Package: `isar_gui/` at repo top level (peer of `isar/`, not inside it). Installed via an
  optional extra: `pip install -e .[gui]` (adds PySide6, pyqtgraph, watchdog).
- Launch: `isar view <workspace>` — the CLI subcommand does a guarded import of `isar_gui` and
  errors helpfully if the extra is missing.
- **Import boundary, enforced by test:** `import isar` must never pull in PySide6/pyqtgraph.
  The campaign path is deliberately matplotlib-free; it stays Qt-free with prejudice. Add a
  unit test: import `isar`, assert `"PySide6" not in sys.modules`.
- `isar_gui` may import from `isar` only: the workspace read API (§7), the metric layer (§10),
  the directive schemas (§4), and the sanctioned sandbox entry point (§6). It must **never**
  import `isar.analyze` internals, `isar.drive` internals, or parser plugins directly. Enforce
  with an import-linter contract or a grep test.

**Why not a separate repo:** the contract surface will churn while the GUI is young (per-scan
curves, candidate-model tables, EIG maps are new artifact surface); one repo gives version
lockstep for free. Because the coupling is through files and a small read API, extraction later
is cheap *if* the GUI ever earns independence (e.g. becomes a general TAS cockpit fronting
TAVI). Boundary in imports, not topology.

---

## 3. Artifact classes & the trust story (G2, G3)

Three classes of artifact exist in a workspace. The GUI's rights differ per class:

| Class | Examples | Mutability | GUI rights |
|---|---|---|---|
| **Immutable observations** | `ScanRecord`s, raw counts + monitor, QC flags as recorded | Never edited | Read only |
| **Versioned interpretations** | fits, intervals, verdicts, branch model / dispersion prior, collation products | Replaced by re-runs, never edited in place | Read only |
| **Append-only directives** | corrections, objective specs, priors nudges, human proposals, adjudication records | Append only | **The only thing the GUI writes** |

This is the GUI restatement of the existing pipeline truths: *scans are immutable; our guesses
about what they mean are not.* Rejection marks, never deletes; re-fits version, never overwrite;
directives append, never mutate. The GUI cannot corrupt a workspace because it holds no pen for
classes one and two.

Consequence worth stating: because human interventions are files in the provenance chain, **a
campaign is replayable with its human interventions included**. The morning diagnostic can (and
should) attribute beam spend to `human`-priority directives like any other proposer — "the human
spent 40 minutes here" appears in the ledger honestly.

---

## 4. The directive taxonomy (G4)

Every GUI action with side effects — every button, drag, approved plan, or typed sentence —
compiles to exactly one of **four directive types**. If a proposed feature does not reduce to
one of these, that is a design smell alarm: stop and re-derive.

### 4.1 The four types

1. **Objective edit** (`objective_edit`) — changes what the campaign is trying to achieve.
   "Measure this line at this spacing." "Tolerance 0.1 meV on ω₀ at these q." "Over-count this
   (Q, E) region for a paper figure." Legality already established: the objective spec is data,
   and mid-run re-specification is a session mutation (`DESIGN.md` §8, renegotiation door).
2. **Prior edit** (`prior_edit`) — re-seeds interpretation, never delivered numbers. Nudge
   `prior_omega`/`prior_sigma` at a q; assert branch separation where two branches are close;
   redraw the running dispersion guess (item 4). Affects the *next* fits via the branch model.
3. **Fit-structure correction** (`fit_correction`) — the existing §11 (`DESIGN.md`) correction
   channel: "there is a second phonon here, refit," "scan 223 is bad," "that ω was wrong."
   Card edits + re-run with ledger invalidation, exactly as human review corrections do today.
4. **Direct proposal** (`scan_proposal`) — "scan at this point now." Injected into the
   acquisition queue with provenance/priority `human`, which outranks EIG ranking but **still
   passes validate** (G6).

### 4.2 Natural language is an input device, not a type (item 5)

`goal_to_spec` already parses a natural-language goal into a formal objective spec at campaign
start. Extend it (as `directive_from_text` or similar) to parse *incremental* directives into
the same four schemas. LLM proposes structured output, schema validates, artifact freezes,
driver disposes — P1 intact. The GUI shows the compiled directive for confirmation **before**
writing it to the inbox (the user approves the artifact, not the sentence). Routing through
LLMM as usual; a failed/ambiguous parse is shown to the user, never guessed into the inbox.

### 4.3 Directive schema (sketch)

One envelope, four payloads. JSON, one file per directive.

```json
{
  "directive_id": "d_2026-07-11T21-04-13Z_a3f2",
  "created": "2026-07-11T21:04:13Z",
  "source": "gui",
  "source_detail": {"view": "planner", "user_action": "approve_line", "nl_text": null},
  "type": "objective_edit | prior_edit | fit_correction | scan_proposal",
  "payload": { "...type-specific..." },
  "requires_ack": true
}
```

Payload sketches (to be finalized against existing internal structures — the objective spec,
the branch-model prior format, and the correction format already exist; **reuse those shapes,
do not invent parallel ones**):

- `objective_edit`: `{ "op": "add_line | set_tolerance | add_overcount_region | amend",
  "line": {"q_start": [h,k,l], "q_end": [h,k,l], "n_points": N, "spacing_rlu": s},
  "tolerance": {"param": "omega0", "halfwidth_mev": 0.1, "applies_to": [...]},
  "region": {"q_range": ..., "e_range_mev": ..., "target": "counts | tolerance"} }`
- `prior_edit`: `{ "q": [h,k,l], "prior_omega_mev": x, "prior_sigma_mev": s,
  "branch_hint": "acoustic | optic | separated", "scope": "point | line | model" }`
- `fit_correction`: reuse the existing correction record format verbatim; the GUI is just
  another author of it.
- `scan_proposal`: `{ "q": [h,k,l], "e_range_mev": [lo, hi], "e_step_mev": d,
  "counting": {"mode": "monitor | time", "value": v}, "reason_text": "...",
  "then": {"fit": true, "tolerance": {...}} }` — the `then` clause covers item 5's compound
  command ("scan, fit, bring error within 0.1 meV") as a proposal plus an objective edit; the
  compiler may emit **two** directives from one sentence.

### 4.4 Acknowledgement

> Corrected — see §0bis item 1: the ledger is two artifacts (`session.json` snapshot +
> `report/session_log.jsonl` event stream); `directive.consumed` lands in the JSONL stream.

Directives are fire-and-forget at the file level, but the GUI needs feedback. The driver, on
draining a directive, appends a structured ledger event (`directive.consumed`, with
`directive_id`, outcome `accepted | rejected_validate | deferred`, and reason). The GUI watches
the ledger (§7) and surfaces the outcome. A rejected human proposal (triangle can't close, dark
angle) renders with the driver's reason — the same refusal logging discipline as
`recenter_refused_elastic`.

---

## 5. The inbox & ownership (G5, G6)

### 5.1 Transport

> Corrected — see §0bis item 1: `session.json` is the snapshot; the ledger events referenced
> below live in `report/session_log.jsonl`, a separate file.

```
workspace/
  inbox/                  # GUI (or any proposer) writes here
    d_<timestamp>_<id>.json
  inbox/consumed/         # driver moves drained directives here (or ledger-only, see below)
  session.json            # ledger gains directive.consumed events
```

- **Write protocol:** write to `inbox/.tmp_<id>`, then atomic rename into place. Windows note:
  use `os.replace`, and the driver must tolerate (skip, retry next cycle) files that are
  mid-rename or locked.
- **Drain protocol:** at the top of each decide step, the driver lists `inbox/`, processes
  directives in timestamp order, validates each, applies or rejects, logs
  `directive.consumed`, and moves the file to `inbox/consumed/`. The move is bookkeeping; the
  ledger event is the record.
- Restart-safe by the same idempotency argument as everything else: a directive is either in
  `inbox/` (pending) or in the ledger (decided). Re-running the drain is harmless.

### 5.2 Ownership ruling (G6) — restated because it is the whole safety story

**The GUI can only ever append to the scan queue and nudge ISAR; it never grabs control.**

- The driver is the sole owner of the instrument/backend connection. The GUI holds no socket
  to the loop, imports no driver internals, and cannot pause/step/abort the loop in v1.
  (A `stop_request` directive is conceivable later; it would still be a directive the driver
  honors at a safe point, not a seizure.)
- Human directives enter the same propose→validate→run pipeline as the driver's own proposals,
  with priority `human`. Validation is **not waived** for humans: triangle closure, dark
  angles, limit checks run on the user's clicks exactly as on EIG proposals. The human being
  wrong about feasibility at 3 a.m. is an expected input, and the planner should already have
  shown forbidden zones before approval (§10), making validate rejections rare but still real.
- Budget honesty: human-priority scans consume budget and are ledgered like any other. The
  campaign diagnostic attributes them.
- Concurrency model: **none needed** beyond inbox atomicity. No locks, no shared mutable
  state, no two-master problem. This is a direct dividend of files-as-truth.

---

## 6. The sandbox exception (G7) — interactive refitting

Item 3's interactive half ("nudge the priors, try a structure, see the fit *now*") cannot ride
`isar run` subprocess round-trips; it needs sub-second feedback. Resolution:

- **Sanctioned import:** the GUI may import the deterministic fit engine through **one**
  sanctioned entry point — the same frozen-config interface `isar run` uses (build a candidate
  config → run the fitter → get fit result + intervals + AIC table). Nothing deeper. This is
  the *only* place the GUI touches analysis code in-process.
- **Sandbox results are ephemeral and marked.** Rendered with an unmistakable visual treatment
  (e.g. dashed curves + "SANDBOX — not a result" banner). Never written into class-two
  artifacts, never exported as results, discarded on session close (the sandbox *config* may
  be saved as a draft directive).
- **Commit = directive.** "Keep this" compiles the sandbox config delta into a `prior_edit`
  and/or `fit_correction` directive; the official number appears only after the workspace
  re-run re-fits from frozen config. Play freely; bless deterministically. P1 survives: the
  GUI is a proposer whose proposals freeze before the fitter of record runs them.
- **Threading:** sandbox fits run in a worker thread (PUFFIN's worker pattern), UI never
  blocks; a slow bootstrap tier is not run in sandbox by default — sandbox uses the fast tiers
  (Hessian/profile) and *says so*, since certifying intervals are the re-run's job anyway.

---

## 7. The workspace read API (Phase 0) — the GUI's data layer

A deterministic, **Qt-free** module in core: `isar/workspace_api/` (name open; it belongs in
`isar/` because scripts, notebooks, and the report all want it too — it is the notebook
interface as much as the GUI's data layer).

### 7.1 Surface (sketch)

> Corrected — see §0bis item 1: `session_events` reads `report/session_log.jsonl`, not
> `session.json`.

```python
ws = Workspace.open(path)                    # read-only handle
ws.card                                      # policy (TOML, parsed) + state (JSON)
ws.scans                                     # ScanRecords w/ provenance tags, QC masks
ws.groups                                    # collation products per card key
ws.results                                   # fits: params, interval ladder per tier,
                                             #   interval_reliable, AIC candidate table,
                                             #   verdict, disposition, confidence, provenance,
                                             #   pooled members
ws.branch_model                              # dispersion prior + branch posterior +
                                             #   assignment_entropy per point
ws.session_events(kind=None, since=None)     # ledger: decide.action, escalate_fit,
                                             #   refusals, directive.consumed, budget ticks
ws.campaign                                  # objective spec, per-q states, budget,
                                             #   stop reason (if ended)
ws.evaluate_model(params, x)                 # deterministic curve eval for rendering —
                                             #   generalizes reconstruct_curve (no refit)
ws.watch(callback)                           # file-watcher: change notifications
                                             #   (results, ledger, state), debounced
```

### 7.2 Notes

> Corrected — see §0bis item 3: `reconstruct_curve` re-solves amplitudes by NNLS against counts;
> `evaluate_model` is pure only when `FitOutcome.amplitudes` exist, else it falls back to
> `reconstruct_curve`. Both share `assemble_curve` (`isar/report/audit.py`).

- `evaluate_model` and `reconstruct_curve` must be the same math; the existing
  reconstruct-at-render-time decision (`DESIGN.md` §12) already established stored-params →
  curve without refit. Expose it; do not duplicate it.
- `watch` is how the live views work (items 2 and 4, and the drive dashboard): watchdog on
  the workspace directory, debounce, re-read only what changed. Because Stage III writes
  artifacts as it goes, watching files **is** watching the campaign, with zero coupling to
  the loop. The GUI must tolerate partial/in-progress writes (retry-on-parse-error, read on
  next event) — same tolerance the driver extends to the inbox.
- This module is a prerequisite shared with the planned residual-driven-refit vision tier
  (which needs per-scan figures) — building it pays twice.
- **Version the read surface.** The Stage II ↔ III contract is frozen-and-additive; the read
  API inherits that discipline. GUI displays the workspace schema version and degrades
  gracefully (hide panes, don't crash) on missing newer fields.

### 7.3 Rendering unification (G8)

One figure module, two consumers: the report/diagnostic renderers and the GUI. Concretely —
a shared layer that turns read-API objects into *plot specifications* (data series, bands,
annotations, styling semantics like `(unreliable)`), with two backends: matplotlib (reports)
and pyqtgraph (GUI). The **semantics** live in the shared layer so the two views can never
disagree about what an unreliable interval or a pooled master looks like. This also fixes
figure hygiene: the render path becomes first-class instead of stranded in `out/`.

**Rendering rules (binding, inherited from `DESIGN.md` §12):**
- `interval_reliable=False` renders as `(unreliable)` — never a numeric range, never error
  bars. In plots: a distinct marker (e.g. hollow + warning glyph), no whiskers.
- Pooled masters are labeled as pooled and enumerate members on inspection; a rejected master
  is "pooled — uncertifiable," never junk.
- Interval-ladder comparison shows tiers side by side, *labeled by tier* (Hessian / profile /
  bootstrap), with the certifying tier visually primary.
- The AIC candidate table (model, ΔAIC, Akaike weight, n_modes) is the standing answer to
  "what did ISAR choose and why" — render it wherever a fit is shown.

---

## 8. Views & 4D honesty (G9)

Data lives in (Q₃, E); every 2D view is a projection and must say so on screen, always:
a persistent legend like `plane (H, 1, 0) × E — integrating |L| < 0.02, |K−1| < 0.02`.

- **Heatmap (item 2):** measured strips render as strips; gaps render as gaps. **No
  interpolation by default.** An optional smoothed/kriged surface is a *model overlay*,
  toggled explicitly and labeled as a model — the same discipline as everywhere else in ISAR.
  Conservative-by-construction applies to pixels.
- **Overlay channels:** per-point ω̂ estimates with error bars (from `ws.results`); branch
  model curve(s); points colored by `assignment_entropy` (the honesty meter — near ln 2
  renders visibly "undecided"); verdict glyphs (resolved/flagged/held).
- **Out-of-plane awareness:** points whose Q falls outside the declared integration window
  are excluded from the view, with a visible count of exclusions ("14 scans outside this
  projection") so the projection can't silently hide data.

---

## 9. Grouping (G10)

- **Semantic grouping is the card.** The GUI's group browser *is* the card's `collation.key`
  and merge policy made visible. If the user wants different grouping, the GUI edits the card
  (a class-three write via `fit_correction`/card-edit channel) and triggers re-collation on
  re-run. There is exactly one definition of "a dataset."
- **Display overlays are view state.** Waterfall by temperature at fixed q (item 6),
  side-by-side dispersion comparison across T, arbitrary overlay sets — pure GUI state, saved
  in the GUI's own session file (ACIDD-style `config/session.json` pattern), never in the
  workspace.

---

## 10. Coordinate & feasibility layer (G11)

> Corrected — see §0bis item 2: lattice/orientation is a versioned epoch series
> (`CardState.lattice_epochs`), not fixed card policy; scans stay in absolute instrument
> coordinates and epochs re-derive HKL retroactively. See §0bis item 5 and G14 on feasibility
> wiring: dark angles/limits have no numeric data source yet, so forbidden-zone previews will be
> kinematics-only until TAVI-side config exists.

A **metric/UB module in core** (Qt-free; suggested `isar/geometry/` or inside the schema
package next to the card, since lattice is card policy):

- Reciprocal metric tensor + B matrix from the card's lattice; orientation from u, v vectors;
  scattering-plane basis; r.l.u. ↔ Å⁻¹; distances/angles in non-rectilinear cells; in-plane ↔
  (H,K,L) mapping for the planner and all views.
- **Source the math from TAS_MCP / TAVI** — the triangle-closure and angle computations exist
  and are tested there. Port or depend; do not rewrite from scratch. (License compatible;
  everything relevant is GPL-3 — see §11.)
- **No third opinion on feasibility.** The planner *draws*; feasibility (triangle closure at
  (Q, E) for the card's kf, dark angles, limits) is consulted from the existing authority the
  driver's validate step uses. The planner pre-renders forbidden zones in the plane view
  **before** approval so users don't author doomed scans; validate remains the final word.
- Non-rectilinear display: a linear transform on the pyqtgraph view/ImageItem driven by the
  metric layer (hexagonal and monoclinic cells shear correctly; axes labeled in r.l.u.).
  Modest, contained work.

---

## 11. Donor map (G12) — assembly, not invention

All three donors and ISAR are **GPL-3** (verified 2026-07-11), so code may be lifted between
them freely with copyright/attribution notices. Strategy: copy patterns shamelessly, lift small
widgets where they fit, factor a shared widget library later **only if it hurts**.

### ACIDD (`C:\Users\AMM\Documents\Github\Science\ACIDD`)
Donates the **cut/plan gesture** and the image-view scaffolding:
- Make Cut interaction: click start → live crosshair + dashed preview → click end → draggable
  endpoint markers → live-updating profile. This **is** the planner's "click two points" (item
  1) and the viewer's cut-extraction gesture.
- `Step along cut (∥)` spinbox ≡ suggested scan spacing with amendment. `Cut width (⊥)` ≡
  integration width for view-cuts, and the over-count region gesture (item 3) for directives.
- Units toggle (pixels ↔ r.l.u.) ≡ the coordinate-layer-under-the-view pattern with live HKL
  readout under the cursor. **Note:** ACIDD's `pixel_to_hkl` math itself (CrysAlisPro
  `.par`/`.dcunwarp` + goniometer R) is detector-specific and does **not** transfer — keep the
  architecture (swappable coordinate layer, everything downstream speaks r.l.u.), slot in the
  TAS metric layer (§10).
- Cut **templates** (save set, re-apply to other files, auto-apply) ≡ "run this same line at
  the next temperature" (item 6) and re-usable measurement plans.
- Session persistence (`config/session.json`: files, cuts, dock layout restored on start) ≡
  the GUI session-state pattern (§9).

**The semantic inversion, stated plainly:** in ACIDD a cut is a *query* through dense existing
data; in the planner the same gesture through unmeasured space is a *directive*. One cut tool,
two artifact outputs, mode explicit on screen — the user must always know whether they are
browsing or authoring beamtime. Draw through measured region → view (side-effect-free); draw
through terra incognita + approve → `objective_edit` directive to the inbox.

### PUFFIN (`C:\Users\AMM\Documents\Github\Science\PUFFIN`)
Donates the **live fitting grammar** and app skeleton:
- MVVM layering (`view/` docks + `viewmodel/` + `worker/` threads) — adopt wholesale.
- Parameter docks with live preview; exclusion mode (click/box-drag to toggle points);
  F-to-fit; component selection (1–9, Q/E cycling); Docks menu; log dock.
- Composite model assembly + save-model-dialog patterns inform the sandbox's structure editor
  (add a phonon = add a component), though the sandbox builds **ISAR fit configs**, not
  PUFFIN model YAML.

### ISAR
Donates **everything that thinks**: parsing, QC, collation, the fitter and interval ladder,
model selection, branch model, verdicts, the driver, the ledger. The GUI adds no analysis.

---

## 12. Component inventory & MVP mapping

| # | Component | MVP items | Reads | Writes | Phase |
|---|---|---|---|---|---|
| C1 | Workspace browser (open, card summary, group tree per collation key) | — | card, groups | — | A |
| C2 | Q-group / fit inspector page: data + fit + components + residuals; interval ladder side-by-side; AIC table; verdict + confidence + adjudication narrative; provenance panel | 3 (view) | results, scans, evaluate_model | — | A |
| C3 | Plane heatmap view: declared projection, strips-and-gaps, ω̂ overlays w/ error bars, branch curves, entropy coloring, live growth via watch | 2 | results, branch_model, watch | — | A |
| C4 | Waterfall / comparison view: 1D cuts at fixed q by T; dispersion overlays across T | 6 | groups, results | — | A |
| C5 | Dispersion view: ω vs q with certifying-tier error bars, prior curve, off-curve flags | 3, 4 (view) | branch_model, results | — | A |
| C6 | Planner: RS map from card lattice/space group, oriented plane, forbidden zones, two-click line, spacing spinbox, scan-series preview, approve | 1 | card, geometry, feasibility | `objective_edit` | B |
| C7 | Directive composer + inbox monitor: pending/consumed/rejected with driver reasons | 1, 3, 4, 5 | session_events | all four types | C |
| C8 | Sandbox refit panel — **Partial:** live fast-tier preview, manual E₀, SANDBOX banner implemented; structure editor, prior sliders, Commit → directive planned | 3 (interactive) | `isar.sandbox_api` | `prior_edit`, `fit_correction` (planned) | C |
| C9 | NL command bar: text → compiled directive(s) via LLMM → user confirms artifact → inbox | 5 | — | any (via compiler) | C |
| C10 | Drive dashboard: EIG landscape over q, per-q state machine (met / declined / uncertifiable_interval / …), budget clock (labeled wall vs virtual), tier escalations, refusal log, proposal queue w/ human entries marked | 4 | campaign, session_events, watch | — | C |
| C11 | Dispersion-guess editor: drag the prior curve / control points → `prior_edit` scoped to line/model | 4 | branch_model | `prior_edit` | C |

---

## 13. Phasing (G13)

Ordered by risk; each phase is independently useful and independently shippable.

**Phase 0 — workspace read API + geometry layer.** Core-only, Qt-free, no GUI. Deliverables:
`isar/workspace_api/`, `isar/geometry/` (metric/UB sourced from TAS_MCP/TAVI math), the
Qt-free-core test, `evaluate_model` unification with `reconstruct_curve`. Worth building even
if the GUI stalls — it is also the notebook interface and the vision-tier prerequisite.

**Phase A — read-only viewer (C1–C5).** Against *finished* workspaces first (the frozen MVP-16
certification workspace is the natural characterization fixture), then live via `watch`. No new
contracts, no directives, cannot destabilize anything — deliberately safe while the TAVI SVP
(realistic clocks, McStas rung) is still pending, and immediately useful for SVP post-mortems.
Exit criterion: open the frozen certification workspace and walk items 2, 3(view), and 6.

**Phase B — planner (C6).** Only *authors campaign configs* — click, amend, approve, out comes
the objective spec / `objective_edit` the user would otherwise write by hand. Works before any
live plumbing exists (the artifact can seed `isar drive` at launch rather than mid-run). Exit
criterion: item 1 end-to-end, including forbidden-zone rendering on a non-rectilinear cell.

**Phase C — directives live (C7–C11).** The inbox + drain protocol in the driver, the sandbox,
NL compilation, the drive dashboard, the dispersion editor. The genuinely new machinery, last,
when A has proven the rendering and B has proven the geometry. Exit criterion: the full MVP
journey (§1.3) against TAVI-as-backend.

---

## 14. Testing

> Corrected — see §0bis item 4: the Qt-free core test also asserts a scipy-free
> `isar.workspace_api` import; it does not assert matplotlib-freeness (matplotlib is a hard
> dependency of the shared plotspec layer).

- **Qt-free core:** `import isar` never imports PySide6/pyqtgraph (unit test, Phase 0).
- **Import contract:** `isar_gui` imports only the sanctioned `isar` surfaces (lint/grep test).
- **Characterization:** Phase A views pinned against the frozen MVP-16 workspace — same
  discipline as the campaign diagnostic. Golden plot-spec snapshots (compare the shared-layer
  plot *specification*, not pixels, so both backends are covered by one fixture).
- **Directive round-trip:** schema validate → inbox write (atomic, Windows `os.replace`) →
  driver drain → ledger event → GUI ack surfaced. Include the rejected-by-validate path.
- **Sandbox discipline:** a sandbox session must produce zero writes outside `inbox/` and the
  GUI's own session file (assert on a tempdir workspace).
- **4D honesty:** projection legend always present; exclusion count correct on a synthetic
  workspace with deliberate out-of-plane scans.
- **Idempotent drain:** duplicate/re-listed directives are not applied twice (keyed by
  `directive_id` in the ledger).

---

## 15. Open questions

1. **Read-API module naming and exact home** (`isar/workspace_api/` vs `isar/session/read.py`
   growth) — decide against the current `session/` internals when Phase 0 starts.
2. **Feasibility authority wiring:** consult TAS_MCP as a library dependency, port the
   functions into `isar/geometry/`, or route through TAVI? Decide with the `tas4d` wiring
   (`DESIGN.md` §15 open item) since both touch the TAVI boundary.
3. **Directive payload finalization** must be done against the real objective-spec, prior, and
   correction structures in code — the sketches in §4.3 are shapes, not schemas. First task of
   Phase C (or late B for `objective_edit`).
4. **`stop_request` directive** (pause/finish-early as an append-only ask the driver honors at
   a safe point) — out of scope for v1, door open.
5. **Mid-run renegotiation UI** ("±0.15 meV at these four q — accept?") — lands naturally as a
   driver-authored question artifact + GUI dialog + `objective_edit` answer, once the
   renegotiation protocol itself (Planned, `DESIGN.md` §8) exists.
6. **Shared widget library** across ACIDD/PUFFIN/ISAR-GUI — explicitly deferred; copy first,
   factor when duplication hurts.
7. **GUI naming** — user-facing name for `isar view` (working title: none; the river does not
   need a tributary yet).

# DECISIONS — ISAR design ruling ledger

Append-only ledger of design rulings. IDs are stable citation anchors (D2). Started at the
2026-07-09 design review; each ruling is final unless explicitly reopened; "future" means
designed-for but not v1.
explicitly reopened; "future" means designed-for but not v1.

## Vocabulary & framing

- **V1.** Top-level narrative is the three-stage user story: **Parse → Fit & Verdict → Steer**,
  with the repo stages mapped beneath: Parse = {parse, reduce}, Fit & Verdict = {collate, fit,
  report}, Steer = {drive}. No R-stage-names user-facing (ISA[R]'s R is overloaded).
- **V2.** "Review" = ISAR's own per-scan sign-off, one of: *fit-and-use* / *collate-only* /
  *not-a-measurement* (alignment, calibration). Maps to the existing disposition set
  (resolved|flagged / branch_continuation / non_result). Never means human review in docs.
- **V3.** "Reduction" = everything raw→useful: parse AND collation (matches `isar/reduce/`).
- **V4.** The module currently named `review` is referred to as **collate** in all docs.
- **V5.** Docs discipline: every capability statement carries **Implemented / Partial / Planned**
  status. Reports speak only in implemented terms. "Vaguely Bayesian" and kin are banned;
  "Bayesian" is used only where earned (branch posterior, soft priors/MAP). Bootstrap =
  frequentist-by-simulation; Bayesian = credible intervals, status Planned.

## Data model & provenance (Topic 2)

- **P1.** Fit membership is binary and explicit: a scan is a member of a pooled fit or it is not.
  Pooled/master results must enumerate `members` (scan IDs) — see
  HANDOFF_pooled_member_provenance.md (verified missing 2026-07-09).
- **P2.** Branch influence is **decision provenance, not data provenance**: scans that shaped the
  branch model/priors without joining a pool are traced via `session_log.jsonl`, never via a
  members field.
- **P3.** Adjudication authority: LLM adjudicator decides flags (mask/keep/drop_detector/
  hold_for_human) but decisions are recorded only (record.adjudication + append-only
  card.state.corrections); application is deferred and human-gated (§8 loop). Verified in code.
- **P4.** The reduce-layer trust story (marks not deletions; reversible normalization; raw counts
  + monitor preserved; Poisson-aware leave-one-out rejection that refuses to eat weak signal;
  human-gated corrections) must be lifted from docstrings into user-facing docs verbatim — it is
  the answer to "how do I know it's not destroying data."

## Reporting (Topic 5, architect-facing portion)

- **R1.** Two report layers: the triage table (during-beamtime user) and the campaign diagnostic
  (architect). Spec: CAMPAIGN_DIAGNOSTIC_SPEC.md. User-facing campaign report = later artifact.
- **R2.** Figure policy: failures always figured; representative successes only; full per-scan
  set buried under figures/scans/; per-scan rendering at fit time is a prerequisite.
- **R3.** `interval_reliable=False` renders as `(unreliable)`, never a numeric range. Collate-only
  rows collapse to counts. Rejected masters are "pooled — uncertifiable", never junk.
- **R4.** Logging is part of the trust contract: `escalate_fit` events (promotion + outcome),
  stop-reason enum (no_signal / signal_unmodeled / uncertifiable_interval / interval_floored /
  fast_tier_only / infeasible), recenter guard (`recenter_refused_elastic`, floor at |ΔE| ≲
  resolution FWHM), structured `decide.action`. A question the logs cannot answer is a logging
  defect, not a rendering problem.

## Statistics layer (Topic 3)

- **S1.** The three-questions framing is canonical for docs/primer: (1) minimize Baker–Cousins
  Poisson cost; (2) select by Akaike weights; (3) certify via the interval ladder Hessian (never
  certifies) → profile → bootstrap (certifying) → Bayesian (Planned).
- **S2.** Raw-counts preservation (combine-before-divide) and Baker–Cousins validity are the same
  design decision; docs must present them as one.
- **S3.** γ-at-bound: Hessian lies near the bound; bootstrap degenerates on it; zero-width
  intervals are degeneracy, not precision. `interval_reliable` marks this and must be enforced
  uniformly in convergence (flagged interval never seats convergence AND never counts as a
  "no signal" decline — it is the third state, uncertifiable).
- **S4.** Persuasion strategy for the field: coverage-against-known-truth experiments, reproducible
  by the reader. See HANDOFF_stats_primer_gallery.md.

## Objective, budget, acquisition (Topic 4)

- **O1.** Objective v1 = **per-point tolerance** (each grid q to ±tol). Model-level/global
  objective (ARIANE-style: maximize model improvement, interpolation allowed) = named future tier.
- **O2.** Budget v1 = **wall-clock instrument session, all overhead included** ("8 hours
  overnight"). Instrument moves, alignment, and any blocking compute count against it. The
  deterministic tier's `counting_seconds` ledger is a simulation stand-in and must be renamed /
  clearly documented (MVP-16 confusion: amount=28800 vs spent=10.6). TAVI-tier runs optimize
  for speed as proof-of-concept.
- **O3.** Stop contract v1 = stop-and-report with best-effort deliveries. Mid-campaign
  renegotiation ("I can do ±0.15 at these four q, accept?") = future work, keep in design.
- **O4.** New stop state: **diminishing_returns** — best available marginal EIG per second below
  threshold. Distinct from met / unmeetable / budget_exhausted. The morning report states what the
  remaining budget would have bought.
- **O5.** Target loop shape = asynchronous, one-step-stale acquisition: while scan n counts, fit
  n−1 and queue proposal n+1 for the instant the instrument idles. On real instruments counting
  dominates fitting by 20–100×, so compute hides entirely; "fitting-bound" is a TAVI-tier
  artifact only.

## Workspace hygiene

- **W1.** `out/` is disposable EXCEPT: archive `out/mvp_zone_test16` (frozen characterization
  baseline for the diagnostic renderer + provenance fix) and `out/phase3_gold` (validation
  evidence cited by docs) to a blessed location before deletion.
- **W2.** Every entry point writes a `run_manifest.json` (purpose, command line, config hashes,
  date, products, outcome); promote-or-archive policy collapses iteration churn.
- **W3.** Pedagogical/validation figures live under `docs/` generated by script from the frozen
  reference workspace — never stranded in `out/`.

## Open (not yet ruled)

- TAVI integration contract (Topic 6): backend vs mid-campaign validator vs resolution provider.
- Docs architecture consolidation plan (Topic 7): which documents exist, who each is for,
  DESIGN.md consolidation pass.
- User-facing campaign report design (deferred by Adrian until post-testing).
- Renegotiation protocol design (O3, future).

## Simulation tiers & TAVI (Topic 6)

- **T1.** Certification ladder: analytic synth (fn:phonon_dft; Stage III certified here 2026-07)
  → **TAVI-as-backend campaign = the SVP, next milestone** → instrument. No McStas/TAVI campaign
  has run yet; docs must not claim otherwise.
- **T2.** Integration is **backend-first**: TAVI implements the same scan interface as the
  instrument (driver cannot tell the difference). Oracle use and dry runs are the same interface
  invoked off-path. SVP requirement: realistic clocks — counting time reported as the instrument
  would report it, exercising O2 budget semantics before a real night.
- **T3.** **TAVI is the single resolution authority** — it owns both the fast
  Cooper–Nathans/Popovici solution and the McStas gold standard. ISAR consumes versioned
  resolution artifacts (resolution_source / resolution_version columns are the contract hook)
  and grows no resolution code of its own. The tas4d convolution consumes TAVI-supplied
  ellipsoids.

## Docs architecture (Topic 7)

- **D1.** DESIGN.md is a consolidated **monolith**, general-purpose, with a document map
  referencing detail docs per category. Refinement patches merged into base text; resolved open
  questions moved here; a section concordance preserves pre-consolidation references.
- **D2.** Decision IDs (P/D/U + this ledger's V/P/R/S/O/T/W/D series) are stable citation
  anchors; section numbers are not.
- **D3.** Docs organize by audience and lifespan: timeless user-facing; timeless
  architect-facing; dated records under `docs/campaigns/` (historical, never load-bearing;
  READING_GUIDE must not route beginners through them).
- **D4.** This ledger (`docs/DECISIONS.md`) is append-only and continues beyond this session;
  future rulings get the next ID in their series.

## GUI (Topic 8, G-series)

Migrated from `docs/GUI_DESIGN.md` §0 on 2026-07-11 (design review preceding branch
`ISAR-initial-GUI`); status notes reflect Phase 0/A implementation as of that branch.

- **G1.** GUI lives in the ISAR repo as a separate package (`isar_gui/`), optional install extra
  (`.[gui]`), launched `isar view`. Boundary enforced in imports, not repo topology.
  Implemented (Phase 0/A).
- **G2.** The GUI is a viewer and a proposer, never a third source of truth: reads workspace
  artifacts, writes only append-only directives through the existing correction/mutation
  channel. Implemented (Phase 0/A) as far as read-only views go; the directive-write half is
  Phase C.
- **G3.** Three artifact classes — immutable observations, versioned interpretations,
  append-only directives — with the GUI writing only class three. Implemented (Phase 0/A) as
  far as read-only views go.
- **G4.** Every GUI write compiles to exactly one of four directive types (objective edit, prior
  edit, fit-structure correction, direct proposal); natural language is an input device, not a
  fifth type. Planned (Phase C).
- **G5.** Transport is a directive inbox (`workspace/inbox/`), drained by the driver at the top
  of each decide step, atomic write-temp-then-rename. Planned (Phase C).
- **G6.** The driver remains the sole owner of the instrument; human directives are queue
  entries with priority `human` that still pass the driver's validate step. Planned (Phase C).
- **G7.** Sandbox exception: the GUI may import the deterministic fit engine's frozen-config
  entry point in-process for live preview; sandbox results are ephemeral and marked; commit
  writes a directive. Planned (Phase C sandbox).
- **G8.** GUI snapshot rendering and report/diagnostic figure rendering are the same code at two
  zoom levels — one figure module (`isar/plotspec`), two consumers. Implemented (Phase 0/A) as
  far as read-only views go.
- **G9.** 4D honesty: every 2D view is an explicit, on-screen-declared projection; measured
  strips render as strips, gaps as gaps; interpolated/modeled surfaces are labeled as models.
  Implemented (Phase 0/A) as far as read-only views go.
- **G10.** Semantic grouping is the card's collation policy; display-side overlays (e.g.
  waterfall by T) are pure view state and never touch the card. Implemented (Phase 0/A) as far
  as read-only views go.
- **G11.** Coordinate math: a metric/UB layer lives in core, Qt-free, sourced from the TAS_MCP /
  TAVI math; TAVI stays the sole resolution authority; the GUI grows no third opinion on
  scattering triangles. Partially implemented — geometry vendored (see G14), preview-only
  feasibility; the wiring question in GUI_DESIGN.md §15 open item 2 is resolved: **vendored**
  (G14).
- **G12.** Donor repositories ACIDD (cut gesture, coordinate-layer-under-image-view, templates,
  session persistence), PUFFIN (live-parameter fitting grammar, MVVM + worker threads), ISAR
  (everything that thinks) — all GPL-3, code may be lifted with copyright notices. Applied
  (donor lifts with attribution).
- **G13.** Phasing: Phase 0 workspace read API → Phase A read-only viewer → Phase B planner →
  Phase C directives/sandbox/live dashboard. In progress (Phase 0 + Phase A done).
- **G14.** Geometry vendored (resolves GUI_DESIGN.md §15 open item 2). TAVI primitives and
  TAS_MCP assembly logic are copied into `isar/geometry/` with GPL-3 notices; the module is
  stateless; the instrument frame (z out-of-plane) is pinned by test. TAVI's HTTP `/validate`
  remains the sole feasibility authority; the vendored local feasibility check is preview-only
  (kinematics/triangle-closure), never a second opinion that overrides TAVI. Implemented
  (Phase 0/A).
- **G15.** Lattice epochs (see GUI_DESIGN.md §0bis item 2). Lattice/orientation is a versioned
  epoch series, not fixed card policy: scans stay in absolute instrument coordinates,
  immutable; `CardState.lattice_epochs` (class-two artifact, precedent `resolution_versions`)
  maps absolute → HKL; epochs change mid-experiment (transition, realignment, plane swap) and
  can be corrected retroactively by appending a new epoch and re-deriving HKL, never editing
  scans; analysis/grouping keys on HKL. Implemented: schema + stateless resolver in
  `isar/geometry/epochs.py` (identity-through-acquisition-epoch until absolute coordinates land
  in Phase B).
- **G16.** `isar_gui` sanctioned imports: `isar_gui` may import only `isar.workspace_api`,
  `isar.geometry`, `isar.plotspec`; `isar.workspace_api` may additionally use `isar.schema.card`,
  `isar.session.state`, `isar.session.workspace`, `isar.drive.session_log`,
  `isar.drive.isar_io.readers`, `isar.report.audit`, `isar.analyze.components`, `isar.geometry`,
  `isar.reduce.collate` (the canonical collation key — one definition of "a dataset"). Enforced
  by an AST import-contract test, `tests/test_import_contract.py` (task A-11; allow-lists kept in
  sync with this entry). Implemented (Phase 0/A).
- **G17.** Rendering semantics live in the spec layer: `isar/plotspec` spec objects carry the
  binding semantics (unreliable-never-whiskers, pooled labeling + members, tier labeling,
  certifying-primary, truth-gated legend notes, strip = measured/no interpolation, per-point
  color channel); backends map tokens only. Certifying-tier derivation is canonical in
  `isar_gui/viewmodels/certify.py`; pooled masters never overlay member data and appear in ω̂
  overlays as marked points (strips excluded). Implemented (Phase 0/A).

## Session status update

Topics 1–7 of the 2026-07-09 review are closed. Remaining open from the original list:
user-facing campaign report (deferred until post-testing); renegotiation protocol (O3, future);
tas4d engine choice (open question, decide when scheduled).

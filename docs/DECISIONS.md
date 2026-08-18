# DECISIONS — ISAR design ruling ledger

> **Lifecycle:** Current
> **Authority:** canonical for the append-only ruling ledger; IDs are stable citation anchors (D2, D4)
> **Provenance:** hand-written
> **Verified against code revision:** `5cf3408` (2026-08-15)

Append-only ledger of design rulings. IDs are stable citation anchors (D2). Started at the
2026-07-09 design review; each ruling is final unless explicitly reopened; "future" means
designed-for but not v1.

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
- **O6.** (2026-07-17, Implemented) Scan value is **two-term**: deficit-weighted EIG on the target's
  ω₀ **plus** λ × expected reduction of branch-assignment entropy. λ decays with label
  confidence; the per-q classification term zeroes once the rival band at that q reaches a
  looser **differentiation tolerance** (default `max(3 × tolerance_meV, resolution σ)`) — a
  rival is never driven to the target tolerance. **No hard rival-spend caps**: branch MI is
  bounded by current label entropy and therefore self-throttles; the budget stop supplies the
  hard bound. "Done" is unchanged — the user path certified on the target branch. The committed
  component-wise GH32 estimator and its version are part of the objective fingerprint.
- **O7.** (2026-07-17, Partial) **Hypothesis/policy split.** `ExcitationHypothesis` = physics
  character; declared in human-owned `card.policy.toml` (`[hypotheses.<name>]`); code never
  writes it. `SteeringPolicy` = tactics (opening moves, search bands, λ); the hypothesis
  suggests defaults, the planner owns decisions, the objective's `steering` block overrides.
  `acoustic` / `optic` / `null` ship as named presets-as-data. New goal compiles freeze shipped
  presets; policy tables parse and validate, while card-vs-goal resolution precedence remains.
  Reference: `docs/HYPOTHESES.md`, MEASUREMENT_STEERING §4.5.
- **O8.** (2026-07-17, Implemented) Hypothesis priors enter the branch-belief regression as **priors
  on parameters** (mean + precision on intercept and slope-at-ZC in the WLS normal equations),
  never as pseudo-anchors — pseudo-data double-counts evidence and feeds the classifier features
  the prior painted in (circularity). The acoustic zone anchor migrates to this form.
  Classification likelihoods and occupancy counts use real anchors only. Zone-centre energy
  priors are lower-truncated normals; the zero-mean acoustic case is half-normal.
- **O9.** (2026-07-17, Implemented) **Path is first-guess.** The driver may insert/extend q without
  restriction when classification value warrants, each logged as driver-inserted with a
  machine-readable purpose. The compiled user path remains the certification set;
  driver-inserted q never seat convergence.
- **O10.** (2026-07-17, Partial) **Three-tier extensibility.** New excitation characters are
  Tier-1 *data* (parameter presets in TOML), never code branches. `dispersion_type`
  (`branch` / `level` / `continuum`) is a Tier-2 registry plugin interface — declared with the
  first slice, `branch` only implemented. The assignment posterior's `new` class + the `null`
  hypothesis are the Tier-3 agnostic runtime track: notice the unknown, don't force it, hand it
  to the human (who writes a Tier-1 preset for the next campaign).
- **O11.** (2026-07-17, Implemented) **Mismatch detection + reproducibility.** Mandatory sanity-check
  opening move probing the declared hypothesis's most discriminating feature; deterministic
  search-band widening when the expected signal is absent (pure function of the persisted
  decline counter); posterior-predictive prior-vs-data disagreement flagged in
  `card.state.json` and stated in the report. The resolved hypothesis + preset version is
  content-hashed into `card.state.json` at campaign start — presets are part of the determinism
  chain.
- **O12.** (2026-07-17, Implemented) **Pre-release clean break.** With no external ISAR users or
  saved-campaign compatibility obligation, branch-aware steering starts a v2 contract:
  `isar.objective/2` inside `isar.objective-envelope/2`; v1 envelopes and frozen objectives are
  rejected rather than migrated. All in-repository consumers and characterization goldens move in
  the same change. The Stage II↔III additive-only rule resumes at v2. W1's v1 evidence remains
  historical evidence, not a load-bearing v2 fixture.
- **O13.** (2026-07-18, Partial) **Counting time is tolerance-driven and dynamic.** In newly compiled
  objectives, configured quicklook and production `ncount` values are floors rather than fixed
  prescriptions. Pooled raw counts are fit with their summed per-point monitor exposure in the
  Poisson model mean; observations and their ΣN variance are never rescaled. Refinement requests are
  derived from the authoritative pooled interval, request only the remaining exposure, and are
  bounded by the production ladder, per-scan growth/cap, neutron budget, and an empirical
  interval-floor brake. Legacy/direct policies remain static unless they opt in. **Implemented
  subset:** exposure-aware pooled Poisson fitting, deterministic tolerance scaling/brakes, and exact
  replay. **Planned remainder:** Fisher/sample-based value-of-information dwell optimisation.
- **O14.** (2026-07-18, Implemented) **Live instrument limits bound batches, not intent.** The frozen
  objective retains the operator's policy and fingerprint. At campaign start ISAR snapshots TAVI's
  point, per-point neutron, queue-neutron, and queue-depth limits with field-level provenance; runtime
  proposals use the smaller policy/live ceiling and split larger tolerance-driven requests into legal
  pooled batches. `/validate` remains final feasibility authority. A live ceiling below the policy
  floor is explicitly unmeetable. Logical proposal IDs advance independently of physical submissions,
  so refusals count separately while recenter retries remain one acquisition.
- **O15.** (2026-07-18, Implemented) **A branch is an identity sequence, not a nearest-energy
  sequence.** Natural-language optic energies anchor only the zone-centre hypothesis; only explicit
  “nearest at every q” wording selects legacy `nearest_energy`. `tracked_branch` preserves all fitted
  modes, assigns optic/acoustic/new/missing jointly over the frozen path after pooled updates, and
  rebuilds the prior from connected, high-confidence folds. Acquisition confirms the ZC first, moves
  outward by adjacency, permits one provisional bridge, and blocks certification and plotting across
  unresolved gaps. Acoustic is retained as labeled non-target context rather than polished to the
  optic tolerance.

## Workspace hygiene

- **W1.** `out/` is disposable EXCEPT: archive `out/mvp_zone_test16` (frozen v1 characterization
  baseline for the diagnostic renderer + provenance fix) and `out/phase3_gold` (v1 validation
  evidence cited by docs) to a blessed location before deletion. Under O12 these preserve history;
  they do not block regenerated v2 fixtures or serve as v2 compatibility requirements.
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
  writes a directive. **Partial:** the Qt-free `isar.sandbox_api` preview entry point, serialized
  GUI worker, manual E₀ placement, and marked fast-tier overlay are implemented; structure/prior
  editing and Commit-to-directive remain Planned (Phase C).
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
  `isar.geometry`, `isar.plotspec`; G7 additionally permits only the sandbox viewmodel to import
  the Qt-free `isar.sandbox_api` frozen-config entry point. `isar.workspace_api` may use `isar.schema.card`,
  `isar.schema.statistics` (the canonical statistical-semantics read gate, so the facade duplicates
  no validation), `isar.session.state`, `isar.session.workspace`, `isar.session.unit_data` (the
  per-unit ingest data reader behind `ws.unit_data`; Qt-free, scipy-free), `isar.drive.session_log`,
  `isar.drive.isar_io.readers`, `isar.report.audit`, `isar.analyze.components`,
  `isar.analyze.model_manifest` (the shared manifest→curve reconstruction the predictive producer
  and the evaluator also use), `isar.predictive` (the one strict predictive-artifact validator,
  ADR-020), `isar.geometry`,
  `isar.reduce.collate` (the canonical collation key — one definition of "a dataset"),
  `isar.reduce.llm_client` + `isar.reduce.adjudicate` (Qt-free/scipy-free config validators the GUI
  settings/credential editors reach through the facade — LLM endpoint safety + cloud-fallback
  default; re-exported by `isar/workspace_api/config_gates.py`), `isar.scene.model` (the stdlib-only
  scene DTO vocabulary the input adapter constructs), `isar.scene.store` (its READ surface only —
  `scene_view.py` calls `read_current`, and `tests/scene/test_import_ban.py` bans the input adapter
  from the module entirely, so a stored revision can never leak back into a build's inputs), and
  `isar.schema.steering` (`CANONICAL_MASTER`, the one definition of ordinary canonical evidence).
  Enforced
  by an AST import-contract test, `tests/test_import_contract.py` (task A-11; allow-lists kept in
  sync with this entry). Implemented (Phase 0/A).
- **G17.** Rendering semantics live in the spec layer: `isar/plotspec` spec objects carry the
  binding semantics (unreliable-never-whiskers, pooled labeling + members, tier labeling,
  certifying-primary, truth-gated legend notes, strip = measured/no interpolation, per-point
  color channel); backends map tokens only. Certifying-tier derivation is canonical in
  `isar_gui/viewmodels/certify.py`; pooled masters overlay their persisted combined spectrum,
  never a substituted member scan, and appear in ω̂ overlays as marked points (strips excluded).
  Legacy masters without `spectrum_ref` remain explicitly curve-only. Implemented (Phase 0/A).
- **G18.** Center view host (GUI Pass 3): the five display surfaces (fit inspector, waterfall,
  plane, dispersion, sandbox) are switchable pages of one central host; docks are tool panels
  only. Auto-follow (selection → scan page, campaign start → plane page) is user-overridable;
  display switching is pure view state (G10 unchanged). Dock layouts are version-gated via
  `saveState(_LAYOUT_VERSION)` — an old/unreadable blob fails restore atomically and the window
  falls back to the programmatic default, never a binary migration. Implemented (Pass 3).
- **G19.** Campaign observation and control (GUI Pass 3): the GUI observes a campaign only
  through workspace artifacts (`ws.session_events()` over `report/session_log.jsonl`; the
  terminal `campaign_summary.json` only after the loop ends) and controls it only as the
  sole-writer `isar drive` subprocess behind the single-writer coordinator. Stop is a
  cooperative, token-scoped, append-only **control request** (`--control-token`; immutable
  `report/control/stop-<token>.json`; honored at the iteration boundary; first-class
  `operator_stopped` state, CLI exit 4; certification skipped; nothing deleted) — distinct from
  the Phase-C directive system, whose inbox/schema it deliberately does not use. Tree-kill
  (Job Object, best-effort, synchronous-taskkill fallback) is operator-fired escalation only.
  Resume never re-compiles a goal through the LLM: `--resume-latest` reuses the frozen
  objective from the last `campaign_start`; start fingerprints (`source: {kind, sha256}`) make
  the Resume-vs-Start decision comparable. Live UI state is ledger-honest: the existing durable
  `model_update.per_q` snapshot supplies persistent point state, joined in the viewer with the
  additive activity-event overlay described by G22.
  Implemented (Pass 3).
- **G20.** Compact campaign monitor (GUI Pass 3): the small window is an explicit mode — same
  window, dock layout stashed in memory, central stack swapped to a glanceable read-only card
  (status, current scan, budget, mini plane, log ticker, cooperative Stop). The session file
  always carries the full layout (snapshots guarded while compact; close expands first).
  Implemented (Pass 3).
- **G21.** (2026-07-19, Implemented) **Pooled masters are first-class derived objects, not scan
  chips.** Home presents a scrollable master/member tree. Masters select their persisted combined
  spectrum and fit but cannot be dragged into manual groups; only physical children carry the
  existing scan MIME payload. Reduce/collate publishes content-addressed pooled blobs before the
  atomic results row containing `spectrum_ref`; old and crash-orphaned blobs are retained. Legacy
  workspaces explicitly fall back to curve-only master display.
- **G22.** (2026-07-19, Implemented) **Live work is an additive event overlay.** Durable
  `activity_start`/`activity_end` and advisory `activity_progress` events describe scan, fit,
  pooling, and certification work; chronological JSONL remains canonical while the GUI feed is
  newest-first. Resume closes unmatched starts as `interrupted`, and viewers independently treat
  pre-resume starts as stale. Persistent per-q state remains the existing `StopDecision.per_q`.
- **O16.** (2026-07-19, Implemented) **Final certification is a master-only canonical generation.**
  Drive submits a `isar.certification.request/1`; the session runner resolves it without crossing the
  decision-only import boundary. `isar.certification/2` then contains one immutable unit per requested
  pooled master in objective-path order. Its fingerprint covers pooled data/provenance, policy/model space, resolved calibration and
  resolution, frozen prior/branch/target state, tier, settings, seeds, and the algorithm contract.
  Exact ordinary master results and valid checkpoints are reused (`bayes` may satisfy `full`, never
  the reverse); missing units run without fitting or mutating physical rows and checkpoint in
  completion order. Canonical session/state/results files publish only after every unit exists, with
  a hash-bearing marker written last. Revisions under `report/certification/<fingerprint>/` are
  derived artifacts retained indefinitely; ISAR never garbage-collects them. An operator may
  manually prune an unreferenced old revision. Version-1 scan checkpoints remain retained but are
  not reusable by version 2.
- **O17.** (2026-07-19) **No shutdown redesign from the OP2 interruption.** The retained GUI log has
  no `via taskkill` fallback line, but external `taskkill`, an operator escalation, and silent Job
  Object teardown cannot be distinguished from the retained evidence. Kill-on-close and the current
  crash/resume contract remain unchanged.
- **O18.** (2026-07-19, Implemented) **The pooled master is the sole scientific decision unit.**
  Physical scans contribute counts, exposure, response, masks, cost, and provenance. Their fitted
  parameters, intervals, branch labels, and verdicts cannot seed the prior, assign the authoritative
  track, certify, or stop a q-point. Scan-quality counters, low information, candidate rejection, and
  production caps are nonterminal diagnostics. Campaign outcomes are `met`, `incomplete`, and
  independently proven `infeasible`; poor or ambiguous fits continue until a global limit.
- **O19.** (2026-07-19, Implemented) **Measurement and analysis are separate cumulative clocks.**
  Deterministic mode defaults to no measurement budget and a 3600-second analysis ceiling;
  measurement-dominated tiers default to eight measurement hours and no analysis ceiling. Serialized
  top-level analysis spans emit durable ten-second checkpoints keyed by stable operation ID. Replay
  takes the maximum cumulative elapsed value per operation, closes unmatched spans as interrupted,
  and never multiplies child-worker time. Limits gate new work, not an active fit wave or recovery of
  already acquired data. Ordinary fit batches use immutable revisions under
  `report/analysis/<fingerprint>/`: at most one worker-sized wave is dispatched at once, completed
  units are atomically checkpointed, and scan/master results publish only after the canonical
  worklist is complete. These derived revisions follow O16's retain-by-default/manual-prune policy.
- **O20.** (2026-07-19, Implemented) **Acquisition is master-predictive and model-robust.** Every usable
  master retains a content-addressed, bounded ensemble of full parameter draws from nested sampling,
  bootstrap, or trusted Laplace covariance. Candidate scans estimate information about target ω and
  plausible model/branch identity with deterministic common seeds. Missing draws cause a one-time
  full-tier master escalation. A seeded exposure-scaled Poisson-deviance bootstrap (default α=0.01)
  tests member homogeneity; inconsistency blocks certification and requests review but never removes
  data or terminates q. Γ at the resolution boundary is diagnostic only when model-marginalized ω
  remains identifiable.
- **O21.** (2026-07-20, Partial) **Live uncertainty work is HESSE-first, revision-safe, and
  certification remains independent.** Quicklooks remain `cheap`; production scans and changed
  pooled-master revisions may run `auto`, publishing a fresh result from trusted HESSE covariance
  and promoting that exact frozen revision to full errors when covariance/trust, parameter-bound,
  or model-weight gates demand it. A trusted HESSE **fold** may refresh the dispersion prior: prior
  admission is a scientific quality gate, not a requirement to spend bootstrap time. Ambiguous
  forks still never enter the prior, and final certification still refits frozen pools at an
  explicitly full tier. Promotions are content-addressed and may publish only while their source
  revision remains current; superseded computations are retained as provenance. Bounded master
  staleness and the fit fast path remain conditional on measured post-phase gates and default off
  until their shadow comparisons show no canonical divergence.
- **O22.** (2026-07-20, Implemented) **Certification regression is an acquisition boundary, not an
  operator handoff.** When authoritative full certification leaves any requested q outside the
  objective, the same drive invocation returns immediately to proposal and measurement while its
  physical and analysis budgets permit. Certification itself consumes no acquisition iteration.
  Passing q revisions are recorded in an atomic drive-owned acceptance ledger; a later certification
  request contains only failed, changed, or unassessed revisions at the requested tier. The session
  runner still publishes that dirty master-only generation atomically and preserves all untouched
  masters. A tier, policy, calibration, or master-revision change invalidates acceptance. Repeating
  the same failed scientific state without intervening progress stops honestly instead of looping.
- **O23.** (2026-07-20, Implemented) **Full live uncertainty attaches only to authoritative pooled
  masters.** OP7 showed that the compatibility escalation router spent 254.9 seconds attaching full
  errors to provisional physical scans during 20 master escalations. Physical rows cannot seat
  convergence or refresh the prior, so the drive now splits fit tiers by role: the provisional scan
  remains `fast` while the changed master receives the required `full`/`bayes` work. Generic
  `run_batch` callers retain their historical single-tier behavior unless they opt into additive
  role overrides. Fresh-master `auto` remains shadow-gated under O21; certification remains full and
  independent. Certification profiling reports pass wall and the slowest parallel unit rather than
  misrepresenting summed worker time as elapsed wall. The same-machine role-split rerun reduced OP7
  wall from 1412.7 to 1210.6 seconds and removed provisional scan error attachment entirely. Fresh
  TAVI identities make that run performance evidence rather than a strict frozen shadow replay;
  all 21 endpoints certified and the operator accepted the small numeric/model-selection variation
  as scientifically equivalent. The strict comparator remains the gate for true frozen replays.
- **O24.** (2026-07-20, Implemented) **Pooled evidence, branch identity, and target delivery are
  separate states.** AP1 produced stable two-mode pooled spectra near H=2.30, but a near-even
  acoustic/optic assignment correctly withheld target `omega0`. The old reducer consequently erased
  both “measured” state and viewer markers, while the production cap remained diagnostic and repeated
  dwell requests saturated at the live instrument ceiling. Every current pooled master now marks q
  measured and preserves its candidates; only a confirmed target fold can certify, seed the target
  prior, or join the target curve. New named acoustic and optic maps use connected `tracked_branch`;
  acoustic anchors at the nearest locally resolvable point. O18's anti-abandonment rule is amended:
  a count threshold alone remains nonterminal, but it is a safety ceiling rather than a quota. The
  second AP1 replay exposed an anchor exception that repeated H=2.15 despite two precise candidates.
  A statistically adequate ambiguous anchor now requires adjacent identity evidence immediately;
  same-q production remains available only while candidate precision is inadequate. Adjacent sides are
  ranked by classification information with path order as a deterministic tie-breaker, and any anchor
  revisit requires fitted neighboring candidate/track evidence (not merely a submitted quicklook) and
  consumes the bounded bridge allowance. Demonstrated
  exposure saturation plus exhausted bridge/revisit opportunities terminates honestly as `incomplete`
  (`branch_ambiguity` / `frontier_blocked`) rather than pausing or blindly acquiring the same q.
- **O25.** (2026-07-20, Partial; supersedes O24's implementation claim) **AP1/AP2 prove the
  connected branch controller still conflates acquisition seed, physical reduced-q geometry,
  unordered evidence, and target delivery.** AP2's 0.5 meV objective proposed H=2.15, H=2.10,
  and H=2.05 quicklooks, then repeated H=2.10 for thirteen production submissions before moving
  to H=2.00. O24's described repeat guard and ambiguous-frontier broadening were therefore not
  Implemented. Phase 0 repairs the controller before the approved A → B → C Smarter Steering
  sequence: persist the off-centre acquisition seed separately from the branch-period origin;
  rerun joint identity only on authoritative pooled-master evidence; and allow only confirmed
  target assignments to publish `omega0`, certify, seed the target prior, or enter the target
  curve. Local lowest-energy/only-candidate ordering never establishes acoustic identity, and
  target/rival roles apply symmetrically to acoustic and optic objectives. An equivalent same-q
  identity revisit requires a newer fitted neighboring-evidence revision and consumes the existing
  `maximum_bridge_steps` allowance; unchanged evidence broadens through O6/O9 information ranking
  or ends honestly as `incomplete` when no feasible route remains. Additive state records the
  authoritative evidence revision, proposal fingerprint, acquisition action role, and role-aware
  identity. Phase 0 remains in progress until AP1/AP2 replay gates pass. Curvature/zones,
  confidence-scaled physics nudges, and per-second OCBA/VOI economy remain Planned sub-phases
  A, B, and C behind their recorded prerequisites.
- **O26.** (2026-07-21, Partial; supersedes the remainder of O24 and extends O25) **AP3 proves
  branch character and acquisition continuity must be global evidence questions.** The controller
  followed a connected acoustic dispersion from Γ toward H=3 but locally relabelled its high-q
  continuation optic, while global classification value jumped across an unmeasured interval.
  Character is now recomputed over the authoritative connected trajectory; confidence-scaled Γ,
  slope, and multi-candidate ordering evidence may overturn earlier labels. Broadening retains O6/O9
  information but discounts it by propagated association uncertainty, with no hard distance cap.
  A smooth low-q acoustic trajectory may mark exact Γ `inferred_unresolved`; this is neither a fit nor
  delivery and is excluded from prior admission and precision completion. AP3's period already makes
  H=3 a zone boundary; automatic lattice/space-group zone derivation remains Planned. Phase 0 remains
  Partial pending campaign replay and unified optic regression gates.
- **O27.** (2026-07-21, Implemented for the tracked optic path; supersedes the same-q revisit
  allowance of O24/O25) **Branch identity is an assignment question, and a blocked identity may be a
  gap in the rival trajectory rather than a shortage of counts at the frontier.** OP9 and OP10 ran
  OP8's objective, controller, target and tolerance and stopped `branch_ambiguity` at H=2.75 holding
  eleven of twelve authorised productions, with `exposure_saturated` false and the per-scan ceiling
  three orders of magnitude above the largest scan submitted; they terminated on running out of
  unvisited q to broaden to. Two causes. Identity adequacy was read off ω₀ *precision* against the ω₀
  tolerance, so a 0.129 meV interval against a 0.5 meV tolerance made an undecided assignment look
  settled and permanently suppressed the same-q rung gated on `not adequate`. And O24/O25 charged the
  same-q identity revisit to `maximum_bridge_steps`, a path-geometry knob defaulting to one, which
  made the production ladder, the ncount escalation and `exposure_saturated` unreachable at an
  ambiguous frontier. Precision adequacy and identity adequacy are now distinct; identity is the
  tracker's own confirmation predicate (`target_probability`, `identity_entropy`,
  `local_identity_clear`) carried additively through `results.csv` to `GridQ`, never re-derived from
  `branch_posterior`, which the driver clears for exactly the ambiguous case. Every same-q production
  charges to `maximum_production_batches_per_q`; `maximum_bridge_steps` again means path geometry
  only, and the newer-neighbour basis of O25 is retained as the *preferential* revisit trigger rather
  than a permanent precondition for further counting. Counting alone was insufficient: a replay spent
  ~1.7e10 counts at H=2.75 across twelve productions without moving an assignment whose two
  candidates (5.53 and 7.72 meV, ~22σ apart) were already resolved as peaks and ambiguous only in
  their labels, because the acoustic branch had gone unfitted at 2.60/2.65/2.70. Acquisition
  therefore reconnects the rival branch at an under-resolved q on the confirmed run behind a blocked
  frontier, below broadening and above the frontier's own same-q fallback, capped per gap q so a
  reconnect that never resolves yields the beam instead of escalating to that point's ceiling. A
  proposal-less frontier still holding authorised same-q work raises `ControllerInvariantError`
  instead of being reported as a scientific result; O24's honest-`incomplete` claim held only when
  the ceiling or saturation was genuinely reached. The frozen OP10 envelope replays to `met`, 21/21
  converged, at 63 scans and 1.26e9 counts against OP8's 64 and 1.41e9, needing one 25 M reconnect
  scan at H=2.70. The `identity_saturated` brake (the identity-space analogue of `interval_floored`,
  for points whose branch never matches the target and so never reach `master_updates`) is
  unit-tested but did not fire in that replay and remains campaign-unproven; AP1/AP2/AP3 campaign
  replays were not rerun, so O25/O26 stay Partial. Evidence:
  `docs/campaigns/2026-07-21_OP9_OP10_TRACKED_FRONTIER.md`.
- **O28.** (2026-07-23, Implemented for the action-inventory control plane and the predicted-rival
  identity probe; gated on a full driver-level AP6 replay; extends O27) **A blocked branch identity
  can be a missing *kind* of measurement, not a shortage of counts — so the controller enumerates a
  registry of action generators and its terminal is exhaustion relative to that registry.** AP6 mapped
  the acoustic phonon (2,0,0)→(3,0,0) and stopped `incomplete — branch_ambiguity` at path indices
  18–20 with beam time left. The frontier point H=2.9 was identity-ambiguous (P(target)=0.707, below
  the 0.85 gate) and `identity_saturated` — more counting had provably stopped sharpening the label —
  yet the rival mode that would settle it sits at ~7.115 meV, above the objective's declared 7.0 meV
  `energy_search_meV` ceiling. Every scan the driver emitted at the frontier was clipped at 7.0, so the
  discriminating evidence was outside every window it was willing to emit. Counting could not fix this
  and neither could broadening: at a path endpoint there is no unvisited neighbour, and H=3 is a zone
  boundary where the acoustic branch flattens at its maximum and sits closest to the optic branch, so
  the point may be genuinely underdetermined. The fix is architectural. Acquisition is a two-phase
  registry pipeline — declared generators emit `MeasurementIntent`s, evaluators score them, selection
  ranks the survivors — carried through a three-stage lifecycle
  (`MeasurementIntent → FinalizedProposal → ValidatedAttempt`) with per-generator failure isolation, so
  a generator that raises becomes a `controller_fault` (resumable) rather than an escaped exception or a
  false exhaustion proof. `action_inventory_exhausted` is exhaustion *relative to* a declarative
  `GENERATOR_REGISTRY` with a derived version, so growing the registry cannot leave an old proof looking
  current. The target search domain and the identity search domain are separated
  (`energy_search_meV` retained as the target alias; `identity_search_meV` additive): an identity-class
  action may look beyond the target ceiling, but only when the band's provenance shows it was
  compiler-chosen — a band an operator declared, or one carrying no provenance at all, stays a hard
  constraint and the rival action is reported `policy_forbidden` rather than silently scanned past.
  Focused evidence has its own sink (`evidence_sink`): an `auxiliary_track_node` probe is fit
  independently, never pooled into the (Q,T) master, never certification-eligible, and merged into the
  tracker's per-path-point evidence as a provenance-tagged candidate that can change the identity but
  never supply the point's ω₀; a clean null (`candidate_not_detected`) is first-class evidence, distinct
  from a failed analysis. The `narrow_rival_probe` generator is registered: at a blocked
  `identity_saturated` frontier with a predicted rival inside the identity domain it emits a narrow,
  resolution-relative window centred on the rival, routed to `auxiliary_track_node`. AP6 replays in two
  modes: *strict historical* — the real frozen objective carried no band provenance, so the rival stays
  withheld and the terminal is unchanged, but the inventory now *names* the forbidden action an operator
  could authorise; *migrated* — with the band recorded as compiler-generated, the probe enumerates as an
  eligible action reaching 7.115 meV and the frontier is no longer a geometric dead end. **Acceptance is
  not "AP6 reaches `met`"**; H=2.9–3.0 may be underdetermined and the posterior was not tuned through the
  gate. The replay is proven at the `enumerate_actions`/reconstruction level; a full campaign replay
  through the live driver against the frozen workspace is outstanding, and the zone-aware boundary probe
  (which needs a cross-zone structure-factor consumer that does not yet exist) is deferred to a later PR.
  Auxiliary withholding is reversible: the canonical master's model interval remains intact behind
  `target_delivery_eligible=False`, and a later canonical selection clears the withholding state and
  restores delivery without requiring a refit. The persisted probe search window authorizes its
  candidates for identity scoring after resume; candidate energies themselves never widen that domain.
  **O25/O26 stay Partial** — AP1/AP2/AP3 campaign replays are unchanged by this. Evidence:
  `docs/campaigns/2026-07-23_AP6_ACTION_INVENTORY.md`.
- **O29.** (2026-07-24, Implemented for the AP7 and AP8 terminals; a standing invariant on the terminal
  taxonomy) **A campaign must not enter a terminal stop while measurement budget remains unless the
  objective is met, the tolerance is infeasible by construction, the operator asked it to stop, or a
  controller fault needs repair.** The legitimate terminals are `met`; `infeasible` (tolerance below the
  resolution-asymmetry systematic floor, unreachable by construction); `incomplete` on genuine
  measurement-budget or analysis-clock exhaustion; `operator_stopped`; and `controller_fault` — the last
  a software defect surfaced for repair-and-resume, never a scientific result. Any *other* stop with beam
  time still available — in particular an `action_inventory_exhausted` or a
  `branch_ambiguity`/`frontier_blocked` while budget remains — is a smell: either the generator registry
  lacks an action for a situation that is in fact resolvable, or a resolvable state was misclassified as
  terminal. Two 2026-07-24 campaigns were exactly that. **AP7** converged all 20 measurable q, then
  hard-failed *final certification* on an internal worklist-index invariant — the inferred reduced-q
  origin is legitimately excluded from the master worklist, so the surviving canonical indices begin at 1
  and the consumer's demand that they equal `range(len)` raised — and reported a bare `incomplete`. The
  invariant now checks the union of worklist and `excluded_points` against `range(total)`, and a
  non-completing certification pass is classified `controller_fault` (resumable) rather than reading as
  data-limited. **AP8** confirmed the *entire* acoustic path, then a single interior point whose two
  pooled members disagreed on ω₀ (`pool_consistent=False`, though the pooled value was within tolerance)
  had its identity evidence dropped from the branch tracker; that emptied the q's observation, broke the
  tracked trajectory, and retroactively wiped every downstream confirmed identity — collapsing the path
  and stopping `branch_ambiguity`/`frontier_blocked` at indices 14–20 with unlimited budget unused. The
  fix keeps a `pool_consistent=False` master's candidate as identity/connectivity evidence (the mode is
  fitted; only its ω₀ *agreement* is in doubt), while convergence (`stopping._master_hw`) and the
  dispersion prior (`_rebuild_*_prior`) gate on consistency independently, so the point stays connected
  but unconverged and is *re-acquired* to resolve the disagreement instead of terminating. The standing
  rule for future generators and terminals: before emitting a budget-remaining terminal, prove the action
  space is genuinely empty relative to the registry (O28), and prefer acquiring more or better data over
  discarding confirmed work. Evidence: `tests/drive/test_ap7_certification_replay.py`,
  `tests/drive/test_ap8_pool_inconsistency_replay.py`, and the two 2026-07-24 fix commits.

- **O30.** (2026-07-24, Implemented in `isar/session/branch_tracking.py`) **A declared hypothesis'
  `confidence` scales how much the hypothesis is allowed to *discriminate*, never how much a
  mismatched candidate is forgiven. It is applied as a likelihood ratio against a baseline common to
  every branch, so `confidence = 0` contributes exactly zero evidence rather than zero penalty.**
  The scorer multiplied confidence into `_normal_compatibility`, which returns `-z²/2` and is therefore
  always ≤ 0. Lowering confidence scaled the *penalty* toward zero, so a less-trusted hypothesis scored
  a mismatched candidate **better**: for one 4 meV candidate against an acoustic prior at 0 ± 0.2 meV,
  the emission ran -162.5 at `confidence = 1` up to -2.5 at `confidence = 0`. Since `MISSING_LOG_PENALTY`
  is confidence-independent, a zero-confidence branch strictly preferred grabbing an arbitrary candidate
  to staying unassigned, and two branches with different confidences were scored on incomparable
  scales.

  The repair is a mixture of a **normalized**, domain-truncated branch density with a common uniform
  density over the authorized energy domain, scored as a ratio against that same uniform:
  `log[(c·N(x; μ_b, σ_b) + (1−c)·U(x)) / U(x)]`. At `c = 1` this is a log Bayes factor against "the
  mode could be anywhere"; at `c = 0` it is exactly 0 for every candidate and every branch. A
  *rejected* alternative, recorded because it looks like the obvious fix and is not: mixing two
  **peak-normalized** Gaussians (`log[c·e^{−z²/2} + (1−c)·e^{−z_v²/2}]`) fails in the same direction
  as the bug and harder, because weight moves to the broader, less-negative component as confidence
  falls — the same 4 meV mismatch runs -200 at `c = 1` to -0.08 at `c = 0`.

  Two consequences are deliberate. **A confident-but-contradicted hypothesis can lose to a vague one.**
  Confidence is not monotone in the score and must not be tested as if it were; what is guaranteed is
  that both branches are measured against one baseline, so low confidence buys no branch an advantage
  the other cannot have. **Normalization returns**, which `_normal_compatibility` had removed because a
  3 meV-wide ZC prior then permanently lost to `new` even at its own mean. The ratio form fixes that
  case rather than reviving it: against a uniform over the same domain, a broad prior at its mean gives
  `log(N_peak/U) > 0` — positive support.

  The authorized domain is a normalized union of the target band, any declared identity band, and
  persisted auxiliary probe/null windows. Uniform width and truncated-normal mass are summed across
  disjoint intervals; values outside every interval are impossible (`-inf`) even at zero confidence.
  The normalized union and an explicit scorer revision are part of the durable authority signature and
  `branch_tracks` audit record, so a changed band or scorer cannot reuse stale assignments.

  **What this does not fix, stated so it is not mistaken for fixed.** The *absolute* trade between
  assigning a candidate and leaving a branch unassigned is set by `MISSING_LOG_PENALTY +
  UNASSIGNED_LOG_PENALTY` (≈ −3.58 nats), and those constants are unchanged. Because the compatibility
  term is now bounded below by `log(1−c)` rather than running arbitrarily negative, lowering a branch's
  confidence from 1.0 to ~0.5 can still flip the tracker from correctly abstaining to attaching a
  grossly mismatched candidate: the evidence against the assignment shrinks past the fixed structural
  cost of refusing it. That is a defensible reading of low confidence — with little prior information,
  explaining an observed mode is reasonable — but it is a *consequence of those constants*, not
  something this decision establishes. Re-anchoring them against the same uniform baseline is open
  work. The scope of O30 is the comparability and boundedness of the confidence term, not the
  assign-versus-abstain threshold. Evidence: `tests/session/test_branch_tracking.py`.

- **O31.** (2026-07-24, Implemented; amends O29's terminal taxonomy, resolves it against O28) **An
  action the operator's own declared policy forbids is a sixth legitimate terminal — an operator-policy
  limit, neither a controller fault nor a data limit — and it must say so where the operator will read
  it.** O28 and O29 were in direct contradiction. O28 shipped strict-historical AP6 as
  `branch_ambiguity`/`frontier_blocked` with beam time left, on the grounds that the rival which would
  settle the identity sat above a search band carrying no compiler provenance and so could not be
  widened. O29 then declared exactly that stop — a budget-remaining `action_inventory_exhausted` or
  `branch_ambiguity` — a smell that is always one of two defects. Both cannot be right, and the code
  sides with O28: `RejectionClass.OPERATOR` is a **durable** rejection
  (`isar/drive/acquisition/inventory.py`), so an inventory holding only a policy-forbidden action is
  complete, holds no eligible action, and legally proves `action_inventory_exhausted`.

  The ruling keeps that behaviour and names it. A band the operator declared, or one with no
  provenance at all (an old frozen objective, where equality with the shipped default is *not*
  evidence the compiler chose it), is a hard constraint: spending their beam outside the range they
  asked for is not the controller's call, and guessing wrong silently rewrites a scientist's intent
  (O28 rules 5 and 6). So O29's five legitimate terminals become six, and its standing rule is
  amended: a budget-remaining terminal is a defect to hunt **unless** the inventory's only remaining
  action is `OPERATOR`-rejected. The distinction from `controller_fault` is exact — a fault means the
  inventory is *incomplete* and exhaustion could not be proven; this means exhaustion **was** proven
  and the one remaining action is withheld by policy.

  The obligation that makes this honest rather than a loophole: such a terminal must name the
  forbidden action and its escape in the stop reason itself, not only in the persisted inventory. The
  escape is to declare `identity_search_meV` (or re-compile the objective so the band carries compiler
  provenance) and resume — not more beam, and not a repair.
  `_operator_authorization_hint` appends the generator's own operator-facing text to both exhaustion
  terminals (`_blocked_frontier_decision` and `_exhausted_action_space_decision`), reading the two
  shapes the signal takes: an action generated and then `OPERATOR`-rejected, and a generator that
  emitted nothing and declared `policy_forbidden`. Structural, saturation, budget and transient
  rejections are deliberately excluded — they are not the operator's to lift, and offering them as
  authorizable would send a scientist to edit a policy that cannot help. `terminal_cause`, the
  `StopDecision` state and the per-q reasons are unchanged: this is reason text, so no consumer keyed
  on the cause changes behaviour. References: O28 (the AP6 strict-historical replay), O29 (the
  taxonomy this amends), `docs/MEASUREMENT_STEERING.md`.

- **O32.** (2026-07-29, Implemented in `isar/schema/hypotheses.py` and
  `isar/drive/acquisition/scan_design.py`) **The scan-window EIG regime is a typed steering knob that
  is always serialized, and the resulting one-time fingerprint break is taken plainly rather than
  hidden behind a dual representation.** `scan_window_eig_mode` (`off` | `shadow` |
  `authoritative`) joins `SteeringPolicy` as an ordinary field, so it appears in every resolved
  steering dict and therefore in `content_fingerprint`. Every campaign's `hypothesis_fingerprint`
  changes once at this release, and with it every derived scientific-action fingerprint and
  content-addressed audit artifact. The rejected alternative — omit the key when it holds its default
  — would have kept old fingerprints alive at the cost of two representations of one policy, which is
  exactly the ambiguity that makes a fingerprint untrustworthy later. Pre-release, a clean break is
  cheaper than a permanent special case (ADR-013 of the EIG plan). Physical behaviour is unchanged:
  the acceptance test asserts the same action class, Q sequence, window, step, count request and
  emitted request in both `off` and `shadow`, and deliberately does **not** assert equal action
  fingerprints, which is impossible by construction.

  Two rules keep the knob honest. `authoritative` is refused at policy-freeze time with the stable
  code `scan_window_eig_authoritative_unavailable` — never quietly read as `shadow`, because a
  scientist would otherwise believe a scored window was executed when no scorer exists. And the
  inventory schema moves to `isar.scan_design_inventory/2` +
  `isar.scan_design_candidate/2` in **all** modes with no `/1` compatibility path, so one reader
  shape covers every persisted record. References: O12 (the frozen Stage II↔III contract this stays
  inside — the scan-design inventory is a workspace-internal audit artifact),
  `docs/EIG_STEERING_DESIGN.md`, `docs/MEASUREMENT_STEERING.md`.

- **O33.** (2026-07-29, Implemented in `isar/session/driver.py`, `isar/predictive.py`,
  `isar/session/certification.py` and `isar/workspace_api/workspace.py`) **The predictive artifact
  moves to `isar.master_predictive/2` as a clean break, and the contract version joins the
  master-fit fingerprint so cached fits regenerate exactly once.** `/1` emission is deleted rather
  than kept alongside `/2`: every reader — the drive loop, the workspace facade, the arbitrary-grid
  evaluator — requires `/2`, and no reader guesses a `/1` artifact into `/2`, which is impossible
  anyway since `/1` carries neither the fitted grid, nor the evaluation context, nor an exposure
  basis. The rejected alternative was a dual-read layer accepting both; it would have made every
  consumer carry a permanent branch and, worse, would have let a `/1` artifact silently supply a
  prediction with an *assumed* exposure basis, which is the exact error the `/2` work exists to
  remove.

  Because the predictive artifact is part of a master fit's output contract, `MASTER_FIT_CONTRACT`'s
  fingerprint payload now includes `PREDICTIVE_CONTRACT`. A scientifically unchanged fit therefore
  fails its cache check once at this release and regenerates under the new contract, instead of
  carrying a stale `/1` side artifact forever behind a valid fingerprint. A leftover `/1` artifact
  is a non-result, never a fault: the loop logs `predictive_artifact_unsupported` with
  `artifact_schema_unsupported` and takes its ordinary no-predictive path, and the facade refuses
  the artifact without blocking the workspace.

  This is in-bounds of O12's frozen Stage II↔III contract: `master_predictive` is a
  workspace-internal artifact, not one of the three frozen boundary files (`report/results.csv`,
  `report/report.json`, `card.state.json`). Pre-release, a clean break is cheaper than a permanent
  compatibility branch (ADR-006/013 of the EIG plan; same reasoning as O32). References: O12,
  O32, `docs/EIG_STEERING_DESIGN.md`.

- **O34.** (2026-07-29, Implemented in `isar/drive/acquisition/grid_adapters.py`,
  `scan_grid.py` and `physical_windows.py`) **The grid a candidate is scored on is an
  instrument-adapter contract, not ISAR arithmetic: the scored grid is the adapter-canonical
  *executed* energies.** ISAR designs a uniform window as `(e_lo, e_hi, e_step)`, but the instrument
  executes the point list its own parser derives from the submitted command *string*, at its own
  numeric precision. Scoring against energies the instrument will never visit is a silent
  correctness bug — the scored grid and the executed grid look equally plausible in isolation, and
  the disagreement surfaces only as an unexplained score that never reproduces on the data.

  An adapter is therefore the single authority on three things for one instrument: the wire
  rendering, the point count, and the point positions. Every grid identity — candidate fingerprints,
  dedup keys, target-support counts — is taken over the executed energies, and the adapter's
  `id`/`version` are hashed into the `isar.uniform_scan_grid/2` fingerprint so the same
  `(lo, hi, step)` executed under different instrument semantics is a different scoring object and a
  stored candidate names the semantics it was scored under.

  The shipped `tavi_delta_e` adapter reproduces `tavi.utilities.parse_scan_steps` **without
  importing TAVI**: ISAR must not grow a runtime dependency on the instrument server to know what it
  asked for. Two details of that reproduction are load-bearing and were both wrong in the arithmetic
  it replaces — the `%g` wire round-trip (TAVI parses the command back with `float()`, so a designed
  bound of `3.3999999999999995` is executed as `3.4`) and numpy's round-half-to-even at 3 decimals
  (Python's `round` disagrees on exact `x.xxx5` ties). Cross-repo agreement is held by golden parity
  fixtures plus one `live` round-trip test asserting the adapter-predicted grid equals TAVI's
  returned `scan_values_1` exactly; that test is the whole point of the ruling, since a reproduction
  nobody checks against the instrument is just a second guess.

  The rejected alternative was keeping ISAR's own rounding and declaring a `grid_energy_decimals`
  construction constant. That is what the generator config carried; it is removed in
  `isar.physical_window_generator_config/2`, because a caller-supplied precision is a statement about
  the instrument that the caller is not entitled to make. References: O33,
  `docs/EIG_STEERING_DESIGN.md`.

  *Repaired 2026-07-29.* "Single authority on the point count" was stated but not delivered: the
  legacy and generator-declared window paths kept a private `floor(span/e_step + 0.5) + 1` on the
  raw designed span plus a clamp at the point cap, and the adapter counts over the **wire** span.
  On near-tie windows ISAR therefore recorded and priced a point TAVI never visits — `deltaE
  -5.44493 7 0.162679` recorded at 78 points, executed at 77. Both paths now take their count from
  `resolve_uniform_grid`, so the ruling holds for every family. The window *geometry* stays frozen:
  the legacy step is rendered verbatim into the submitted command, so its cap-coarsened form is
  still derived from the designed span (`COARSEN_FROM_DESIGNED_SPAN`), which is why the emitted
  command bytes are unchanged and only `n_points`/`scoring_grid`/`nominal_cost` move. The count is
  never clamped back — where the two coarsening rules disagree the record states what the instrument
  will really execute.

- **O35.** (2026-07-29, Implemented in `isar/session/predictive_artifact.py`, `isar/predictive.py`
  and the three artifact readers) **The target projection is separated out of the physical
  predictive artifact and bound to the tracker authority basis that produced it.**
  `isar.master_predictive/2` carried a target projection resolved from the fit's own local best
  mode. The fit's local best can disagree with the target the campaign stands behind once branch
  triage and the connected tracker have run, so a scored design could aim at an energy the campaign
  no longer believes in — and nothing in the artifact recorded which authority state the projection
  came from, so a stale one was indistinguishable from a current one.

  `/2` becomes physical only: manifests, named draws, expected counts, `omega0_draws`, and per-draw
  `mode_energies` — the full menu of fitted mode energies a projection selects from without a refit.
  The projection moves to `isar.target_projection/2`, resolved *after* triage and the tracker,
  carrying an `authority` block (action role, master revision, track revision and state, candidate
  id and state, selected candidate index, calibration and policy fingerprints). A consumer whose
  state differs on any of those holds a stale projection and must re-resolve rather than score.
  `action_role` was first published *beside* that block, which made the staleness test blind to the
  one field it names — a master re-resolved as a rival probe compared equal and reused the master's
  projection; it moved inside on 2026-07-29 as a clean break.

  An unresolvable projection **publishes** with `complete: false`, a structured `reason`, and no
  target draws at all. Raising was rejected because a missing artifact is indistinguishable from a
  producer that never ran; a *partially* populated one was rejected because it invites a consumer to
  score the half it likes. The reasons are stable codes (`descriptor_only`, `withheld_delivery`,
  `ambiguous_track`, `no_reference_target_energy`, `reference_beyond_mode_support`,
  `no_resolution_basis_for_mode_support`, `no_unique_mode`,
  `no_retained_model_carries_the_target_mode`), and equidistant modes refuse rather than let a
  nearest-energy rule publish a coin flip as a QoI.

  *Extended 2026-07-29.* Nearest-energy selection was unconditional, so an authoritative ω₀ of 99.0
  meV against modes at {2.0, 5.0} published `complete: true` naming the 5.0 mode. Both selection
  constants are now resolution-relative to the artifact's own `gaussian_fwhm_meV` and recorded in a
  `mode_selection` block on the projection: a support gate at 3 × FWHM
  (`reference_beyond_mode_support` outside it, and `no_resolution_basis_for_mode_support` when no
  usable width is declared — fail closed, since a valid `/2` always carries one), and an equidistance
  tolerance of 1e-6 × FWHM floored at the previous absolute 1e-9. An absolute 1e-9 meV decided a tie
  at a precision no measurement has.

  The reshape reaches existing workspaces the same way O33's did, and by the same mechanism:
  `PREDICTIVE_CONTRACT` — defined once in `isar/predictive.py` and re-exported, never re-declared —
  advances to `isar.master_predictive/2+physical_only.1` and joins the master-fit fingerprint, so
  every cached master regenerates its predictive artifact under the new shape without refitting the
  science. A migration of stored `/2` artifacts was rejected: the projection cannot be reconstructed
  from a stored artifact at all, since the authority basis it must bind to is not in there.

  Paired with this, `validate_master_predictive_artifact` becomes the one strict read gate all three
  readers run (ADR-020). The drive loop previously checked only the schema label, so the same
  artifact was usable or not depending on who opened it; the gate also recomputes
  `predictive_basis_fingerprint` from the artifact's own content, which is what makes a stale or
  hand-edited basis a refusal instead of a plausible score. References: O33, O34,
  `docs/EIG_STEERING_DESIGN.md`.

- **S5.** (2026-07-25, Implemented; amends S1 and V5, both of which record the superseded status)
  **The Bayesian rung of the interval ladder is built and running; what is still Planned is its
  licence to certify, not its machinery.** S1 wrote the ladder as "Hessian (never certifies) →
  profile → bootstrap (certifying) → Bayesian (Planned)", and V5 wrote "Bayesian = credible
  intervals, status Planned". Both were true when ruled and are now false in the same way: they
  describe an absent capability, when the accurate statement is a present capability that is
  deliberately denied authority.

  The tier is wired end to end. `--errors bayes` (`isar/cli/main.py`, with `--bayes-nlive`,
  `--bayes-dlogz`, `--bayes-maxiter`) sets `run_bayes` in `isar/session/driver.py`, which runs the
  dynesty nested sampler on the preliminary-best label and persists `bayes_interval`,
  `bayes_gamma_interval`, posterior parameter samples, the convergence gates (`bayes_ok`,
  `bayes_ess`, `bayes_fail_reason`) and the three-method agreement ratio `bayes_freq_ratio`.

  What is unchanged, and is the point of this ruling: **the bootstrap `freq_interval` remains the
  sole certifying interval.** `isar/session/driver.py` states it at the field definition —
  certification and confidence never read the Bayesian keys — and certification reads
  `within_model["freq_interval"]`. The reason is not doubt about the sampler but the absence of a
  coverage measurement of the kind §3 of `docs/STATS_PRIMER.md` performed for the bootstrap. Until
  the credible interval's coverage is measured the same way, promoting it would certify on an
  uncalibrated interval, which is precisely what the ladder exists to prevent.

  Consequently the correct status phrase everywhere is **"Implemented, diagnostics-only"**, and
  the Planned item is narrowed to: Bayesian coverage validation, and promotion to certifying
  authority conditional on it. Per D4 this entry amends S1 and V5 rather than editing them; the
  superseded wording stays in place as history. Applied in `DESIGN.md` §7 and §15,
  `docs/STATS_PRIMER.md` §0/§3/§5, and the Fig. 1 pipeline map.

- **G23.** (2026-07-27, Implemented; amends G17) **Fit displays separate evidence from pooled
  authority, and authority is revision-safe.** WISH-1, WISH-2, WISH-3, and WISH-9 are one
  read-only display grammar. Individual fits show evidence: a small filled green circle is usable,
  a small hollow grey circle lacks a certifying interval, and a small hollow red diamond is
  explicitly unreliable. Pooled masters show the current answer: a large hollow green circle is
  target-matched but uncertified, a large hollow amber diamond has unresolved or ambiguous target
  identity, a large hollow red diamond is an explicit target mismatch, a large heavy blue square
  is certified, and a large hollow grey square is a legacy master without a matching assessment.
  Unselected candidates remain small neutral points. Each point is one composed semantic glyph;
  prior and branch curves are neutral; legends use a fixed semantic order; aggregate results are
  called **pooled masters**, never master scans.

  Certification and authoritative master uncertainty require the latest
  `MasterAssessmentView` whose populated `master_revision` exactly matches the fit. Only that
  assessment's `certified` state or `converged` flag produces the certified glyph, and its
  `omega0 ± calibrated_halfwidth` supplies the displayed interval. HKL matching is a legacy
  fallback only when a revision is absent; two differing populated revisions never match. Without
  such an assessment, the display uses the fit's certifying interval and identifies it as
  fit-level uncertainty. The inspector draws that interval horizontally at ω₀, with no
  count-axis uncertainty; plane and dispersion views preserve asymmetric lower and upper energy
  spans. `interval_reliable=False` suppresses the bootstrap rung but does not suppress a valid
  profile or trusted-HESSE fallback; an explicitly unreliable fit with no fallback never acquires
  whiskers.

  State colour is the default. One shared, session-persisted **Colour by entropy** action controls
  both plane and dispersion; the modes are mutually exclusive, and entropy replaces categorical
  point colour while shape, size, and fill retain authority/state. The dispersion status ribbon
  uses the same assessment-derived state as its plot.

  Strip colour belongs to the plot specification (`viridis` by default). Both backends consume
  that colour map; the Qt colourbar owns only a private proxy image and propagates its levels and
  lookup table to every current strip, clearing all strip references on re-render and stripless
  transitions. A colourbar is never attached to a measured strip.

  The latest-acquisition overlay is one unfilled, high-contrast double outline above measured
  strips and below fit points. “Latest” means the globally newest physical submission with a
  matching completed `analyze(job_id)` event: pending and failed jobs, pooled products, and
  certification-only analyses do not qualify. Event `acquisition_id` joins the submission to all
  of its physical strips; physical-result order is only the documented fallback when detailed
  event identity is absent. The box is the union of the acquisition's displayed strip widths and
  measured energy extrema, so multiple detector rows still produce one box. If that global
  acquisition is outside the selected temperature/projection slice, the view draws nothing.

- **T4.** (2026-07-27; background catalog 2 revised 2026-07-28; Implemented for the TAVI interface
  and ISAR's plumbing; the fitting-side
  family of `docs/BACKGROUND_DESIGN.md` §2–4 remains Planned) **Background is generated truth with a
  declared identity, and ISAR is allowed to know the identity but never the magnitude.** Backgrounds
  were the largest real-data risk named by the statistics-contract audit, and the workstream was
  blocked on a simulator that plants none: nothing to fit, and no way to score a fit. This ruling
  settles how the simulator plants it and how ISAR carries it.

  **Interface: hybrid, override replaces.** TAVI holds one background profile as instrument
  configuration (GUI, `GET`/`PUT /background`) for manual work, and a scan body may carry a per-scan
  `background` override which replaces that profile wholesale — never a merge, because a
  partially-merged profile is a profile nobody declared. `/validate` accepts and checks the same key,
  so validate-then-submit cannot disagree.

  **Categories explain origin; each source owns its scale.** Catalog version 2 of
  `tavi.background/2` has six stable source IDs grouped as Environment (flat floor,
  energy-dependent slope, cosmic-ray spikes), Instrument (composite aluminum powder lines), and
  Sample (elastic incoherent line, broad elastic tail). Diffuse scattering is removed because it is
  measurable sample physics, not background. Category tells the scientist what the source is
  assumed to represent; it does not invoke hidden calibration. Smooth sources scale independently
  as `monitor counts × catalog shape/rate × source scale`, independent of the selected sample.
  Aluminum is one synthetic six-reflection source whose catalog-owned relative line weights and
  visibility treatment scale together; it uses marginalized Q/E resolution widths with documented
  fallbacks and ignores Q–E covariance. Cosmic rays are a seeded post-Poisson event overlay: scale
  changes event incidence, never event amplitude, and events remain present in noiseless
  deterministic scans.

  **Metadata always resolves, and identity is a fingerprint.** The v2 request is exactly
  `{catalog_version, enabled, sources: {id: {enabled, scale}}}`; omitted source IDs normalize
  disabled at ×1, while unknown fields/IDs, invalid scales, and catalog-version mismatches refuse.
  Every scan reports the complete normalized source states and catalog numerics, the delivery
  source (`config_default` | `per_scan_override`), a **profile fingerprint** over all remembered
  settings, and an **effective fingerprint** over globally and individually active sources.
  Delivery source, RNG seed, and realized events are deliberately excluded. The effective
  fingerprint is the pooling identity. A disabled profile still stamps a block: the absence of
  background is provenance too. Catalog version 2 remains current because the aluminum
  relative-weight and visibility recalibration landed before release; the keyed envelope, source
  IDs, and `tavi.background/2` schema identifier are unchanged.

  **The truth firewall is physical, not a naming convention.** The planted numbers are the answer to
  the question the fitter is being asked, so they are simply not on the record. The parser keeps
  `enabled`, v2 `delivery_source`, `background_schema`, `catalog_version` and the two fingerprints
  in `raw_header["background_provenance"]` (historical v1 `source` remains readable), and redacts the
  numeric block — source definitions and scales, event seed, cosmic point indices/counts/amplitudes
  — out of the stored payload (`{"redacted": "sim_truth",
  "effective_fingerprint": …}`). Source IDs are excluded with the numbers because names can reveal
  planted truth. The raw JSON on disk is untouched
  (P4's mask-not-delete concerns data points, not the record's embedded copy) and validation scoring
  reads the truth there **after** fitting. Both leak paths are closed independently: a payload
  carrying only the requested `launch.background` (an interrupted job with no result block) is
  redacted too, and gets a fingerprint-less provenance rather than no provenance at all.

  **Pooling classes fail closed, and "disabled" is not "unknown".** Collation refuses to pool members
  whose effective fingerprints differ: kept, fit and reported individually, group flagged
  `pool_refusal`. Provenance without a fingerprint is class `unknown`, and because that class is an
  admission rather than an identity, **two** unknown members do not pool with each other either —
  sharing the label is not evidence of shared truth. Conversely a provenance whose `enabled` is
  literally `false` normalizes to the background-free class, so an explicitly-clean scan pools with
  legacy pre-background data: TAVI guarantees a disabled profile is bit-identical to pre-background
  output, so the evidence is numerically equivalent and refusing would only discard exposure. The
  normalization is in the pooling key only; the full provenance stays on the record.

  **Campaign transport is a compile-time freeze, and steering never sets backgrounds.**
  `CampaignConfig.sample_background` is normalized exactly once at setup against TAVI's advertised
  catalog version and source IDs, logged as `sample_background_frozen`, and stamped verbatim onto
  every validate and scan body as a per-scan override; resume refuses on
  `background_fingerprint_drift` whenever the frozen request no longer matches. A
  campaign therefore never depends on TAVI's mutable config default, so a GUI edit, another client,
  or a crash leftover cannot change its planted truth mid-run. **Omission is a declaration
  of clean, not an invitation to inherit:** an undeclared `sample_background` freezes a complete
  disabled v2 profile at the current catalog version, stamps it like any other, and is subject to
  the same drift check — the frozen event carries `"defaulted": true` so logs still separate declared-clean
  from defaulted-clean. Sending no key instead let a persisted enabled TAVI profile silently consume
  a live campaign's whole iteration budget, and let a resume that dropped its declared background
  bypass the drift guard and be caught only downstream, after the spend. The sentinel string
  `sample_background = "instrument_config"` is the one deliberate opt-out — no freeze, no stamping,
  no drift check, inherit the instrument's live configuration — and it is a footgun by construction.
  `TaviClient.set_background`
  exists for manual, test and campaign-setup use only — the steering loop calls it nowhere, because
  stamping is transport and choosing a background would be a scientific decision the loop has no
  business making. References: T2 (same-interface backend rule), T3, P4,
  `docs/BACKGROUND_DESIGN.md` "TAVI prerequisite (landed)".

- **S6.** (2026-07-29, Implemented; amends S1 and S5 without changing their legacy authority)
  **Every decision-bearing statistical object now has an optional, producer-owned semantics record;
  authority is named by use, never asserted once for the object.** STAT-1 is metadata-only. It changes
  no fit, constraint value, likelihood, AIC/AICc/BIC value, Akaike weight, survivor, interval,
  calibration, verdict, certification result, stop, action, acquisition, or EIG calculation.

  The pure `isar/schema/statistics.py` seam defines four v1 records:
  `isar.fit_selection_semantics/1`, `isar.interval_semantics/1`,
  `isar.probability_semantics/1`, and `isar.predictive_semantics/1`. It owns controlled vocabulary,
  strict writer and tolerant legacy-reader validation, finite canonical-JSON SHA-256 fingerprints,
  compact CSV projections, and scoped authority. Full fit records persist in `session.json`; compact
  projections are appended to the frozen `results.csv` columns; predictive and projection sidecars
  keep full records. `unknown_legacy` is reader-only. Missing or invalid producer provenance emits
  `semantics_construction_failed` and leaves semantics unavailable without suppressing or changing
  the scientific result. New authority consumers must abstain on absent or unsupported semantics.

  Effective constraints now carry per-parameter origin. Candidate semantics distinguish
  data-likelihood, soft-penalized, and hard-conditioned estimates; record the candidate-generation
  route; and fingerprint the offered, scored, Akaike-denominator, error-attachment, and legacy-pool
  model sets separately. Effective soft constraints, data-dependent/post-selection hard constraints,
  and prespecified/prior-predictive hard constraints are labeled selection-ineligible under their
  respective reasons. Ordinary AIC nevertheless continues to score them until STAT-2 separates
  comparable selection fits from constrained delivery fits and resolves AIC versus AICc.

  Interval metadata is producer-owned: HESSE, unconstrained/penalized/conditional profile,
  unconstrained/penalized/conditional parametric bootstrap, legacy Akaike-weighted bootstrap pool,
  and optimizer-bound nested intervals are distinct measures. `confidence` is a heuristic triage
  score; branch assignment is a normalized assignment-score distribution; tracker weights are
  normalized factor-graph scores; every entropy names its state space and source measure. Existing
  field names and thresholds remain unchanged.

  Current nested output is classified as a Bayesian posterior under an
  `optimizer_bound_data_dependent` prior kernel, with
  `posterior_measure_kind=posterior_resample`, `evidence_comparable=false`, and
  `bayesian_model_average=false`. A current `isar.master_predictive/2` artifact records its actual
  per-model nested/bootstrap/Laplace particle basis, has `formal_bayesian_model=false`, and is always
  design-ineligible because `/2` permits heterogeneous fallback. `predictive_complete_for_eig` and
  `construction_complete` mean construction completeness only. The mixed `model_interval` remains
  active for `legacy_master_certification`, `stopping`, and diagnostic display, but is explicitly
  ineligible for EIG and acquisition. It remains conditional on target-present retained models;
  target-presence aggregation is deferred to STAT-4a.

  STAT-1 does not bump `PREDICTIVE_CONTRACT`, alter numerical `predictive_basis_fingerprint`, backfill
  cached masters, or join semantics-bearing references into scientific/cache/tracking/action
  identities. STAT-2 must introduce or bump the selection contract used by both the master-fit cache
  fingerprint and the unchanged-master carry gate. STAT-3 refuses pre-STAT-2 or semantics-incomplete
  inputs when constructing the homogeneous
  `akaike_weighted_parametric_bootstrap_predictive` shadow artifact. A workspace cannot enter EIG-3
  by carrying a pre-STAT-1 cached artifact. EIG-3 remains blocked on STAT-2, STAT-3, and STAT-4a and is
  shadow-only after those gates; certification repair, target-presence support, scan-window scoring,
  and acquisition authority are outside this ruling.

- **S7.** (2026-07-29, Implemented; amends S6)
  **STAT-1 semantics identities prove their stated comparison context, and interval authority is
  attached to the realized delivery rather than producer capability.** This STAT-1.5 repair is
  metadata-only: it changes no fit, likelihood, information criterion, weight, interval value,
  verdict, certification result, stop, action, cache contract, acquisition, or EIG calculation.

  Candidate fit records now publish distinct `data_likelihood_fingerprint`,
  `model_parameterization_fingerprint`, `constraint_fingerprint`, and
  `comparison_context_fingerprint` values. The data identity includes observed counts, fitted
  energy grid, mask, exposure normalization, fixed likelihood context, and the Baker–Cousins
  revision. The parameterization identity includes component structure, parameter names, bounds,
  fixed values, and effective parameter count. `conditioning_fingerprint` remains an additive
  compatibility alias for the complete comparison-context identity rather than the former
  constraint-only hash.

  Every offered, scored, Akaike-denominator, error-attachment, and legacy-pool identity is now a
  sorted set of structured model/context records. The complete selection identity also includes the
  canonical candidate-generation route, each candidate's full fit semantics, criterion inputs, and
  aggregate `selection_eligibility` with all structured contributing reasons. Merely permuting
  candidate order cannot change these fingerprints. Data-dependent or post-selection conditioning
  is the strongest candidate-level constraint defect; an incomparable data-likelihood context is
  the strongest selection-level defect.

  Interval records separately retain `producer_authority_scopes` and realized
  `authority_scopes`. The latter is recomputed after the delivery ladder, branch triage, and
  connected-tracker selection or withholding. A selected reliable bootstrap, certifiable profile,
  or trusted HESSE interval can carry active delivery/stopping authority; a flagged bootstrap,
  unselected profile, untrusted HESSE result, or withdrawn scalar delivery cannot.

  Recognized predictive/projection semantics are self-authenticated on read by recomputing
  `statistical_semantics_fingerprint`; the strict `isar.master_predictive/2` reader additionally
  checks the artifact-level reference and every per-model semantics record against the authenticated
  ensemble. Absence remains accepted for legacy numerical use and unavailable to later semantics
  authority. The master interval records both the particle measure actually present and that the
  `/2` contract permits heterogeneous fallback; an accidentally homogeneous artifact remains
  design-ineligible until STAT-3 supplies the homogeneous contract.

  The metadata identity firewall is pinned through result-content hashing, master revision and fit
  cache inputs, tied model ordering, branch-track revision and assignments, calibration lookup,
  predictive numerical identity, stopping, action fingerprints, and the deterministic campaign
  harness. `PREDICTIVE_CONTRACT` and the unchanged-master carry gate remain unchanged. With these
  repairs landed, the descriptive STAT-1 contract is complete enough for STAT-2 to begin enforcing
  selection eligibility.

- **S8.** (2026-08-02, In force; no production behavior change)
  **STAT-2b.1 held-out disposition: retain AIC and withhold an AICc switch.** The completed sealed
  STAT-2b.1 campaign is accepted as valid evidence under its frozen decision gate. It found no
  reproducible practical basis for replacing production AIC with AICc. Production model selection
  therefore remains on AIC.

  This ruling does not declare AIC, the current candidate-generation route, or the current
  constrained-delivery selection path ideal or fully calibrated. The held-out evidence leaves
  difficult-regime, comparison-completeness, RMSE, missed-mode, and false-certification problems
  open for continuing statistical work.

  No AICc switch is authorized. S8 does not authorize STAT-2c or any production-selection authority
  change; STAT-2c remains pending a separate owner decision. The continuing selection and
  error-method program may address the open problems without blocking the separately bounded EIG-4
  workstream established by O36.

- **O36.** (2026-08-02, Planned; amends the milestone naming in T1 and T2 without changing their
  validation requirements) **Restricted same-action EIG may proceed before the full statistical and
  phenomenon-generalization programs are complete.** EIG-4 is the current SVP. Its first authority
  is frozen to a clean, already-confirmed target at an already-selected scientific action and Q,
  using canonical target production or confirmation only. It compares `legacy_uniform/1` as the
  control with `target_core/1` as the possible narrower winner at equal per-point exposure.

  The objective is conditional target-QoI information per executed point. A narrower scan may
  contain less total target-QoI information; absolute-information noninferiority is not required.
  Authority requires paired uncertainty to resolve superiority in information per executed point.
  An unresolved comparison does not authorize `target_core/1`.

  `legacy_uniform/1` is the control for the first eligible same-action comparison, not the universal
  safety or recovery scan. An ineligible EIG state returns control to the existing non-EIG design and
  recovery path without EIG altering its choice. That path may choose the legacy window, a
  broad/recovery window, or another already-established action-specific behavior.

  EIG must not choose or change Q, scientific action, target identity, fit model, hypothesis,
  physical constraint, interval method, certification rule, or model-selection authority.
  Target-presence, model-identity, and branch-identity information may be calculated but remain
  diagnostics-only for this first authority. Current production AIC weights remain the input source;
  the held-out campaign did not validate those weights.

  A later change that alters the EIG design measure, model weights, particle semantics, QoI
  projection, or eligibility basis must change the affected fingerprints. Earlier shadow or
  authority evidence is then non-reusable until the changed path is re-evaluated.

  The scorer core consumes a generic QoI projection. Its first adapter exposes
  `qoi_id = "excitation.energy"` and `qoi_unit = "meV"`; the scorer core must not import or branch
  on `PhononDHO`, phonon classes, acoustic/optic labels, `omega0`, or DHO parameter names. This is a
  bounded local fit-model comparison at one already-selected Q. Phenomenon-level information transfer
  across Q and energy, a phenomenon registry, continuum framework, magnon implementation, and a full
  model refactor are post-SVP. This documents a future seam; it introduces no runtime interface here.

  O36 amends the milestone naming in both T1 and T2. T1's backend-validation ladder and T2's
  realistic-clock requirement remain required, but are backend-runtime and fidelity-validation rungs,
  not the current SVP definition. Historical evidence and filenames remain unchanged.

- **O37.** (2026-08-03, Implemented in the Slice 5 offline comparison contract; does not amend O36)
  **Slice 5 distinguishes exactly scoped scientific policy from diagnostic discovery and grants no
  acquisition authority.** A distinction policy is hand-authored scientific intent, matched only to
  the completed decision context and policy provenance it declares. Discovery has no global
  distinction scale: completed variants remain separate diagnostics, and their identities and refusal
  reasons must not be collapsed into an inferred scientific class.

  The first supported conditional target-QoI compiler is
  `isar.scalar_absolute_partitions/o36.5`. That is a bounded scoring mechanism, not a universal
  model of phenomena, branches, continua, or discovery.
  Slice 5 may transform coherent completed `off` decisions into `shadow` counterfactual diagnostics,
  compare only `legacy_uniform/1` with `target_core/1` at equal per-point exposure, and report
  `target_core_minus_legacy`. Candidate-pair or exposure mismatches are refusals, never substitutions
  or normalized estimates.

  Strict loaders must reject missing, malformed, stale, mismatched, or incoherent completed
  artifacts. The established retained evidence basis has 12 reviewed roots, 233 master rows, and
  230 pooled outcomes, but zero authenticated predictive `/2` plus projection evidence; zero
  Slice 5-ready comparisons are therefore expected until a runner establishes otherwise. A retained
  corpus with zero ready comparisons is a truthful result of the narrow contract, not permission to
  broaden it. Slice 5 has `eig_shadow = diagnostic` only:
  `eig_authoritative` and `acquisition_selection` remain ineligible. It creates no scored inventory,
  recommendation, request, or execution authority; Slice 6 remains the planned separately gated
  scored-shadow-inventory step.

- **O38.** (2026-08-03, Implemented in the Slice 6 scored-shadow inventory contract; does not grant
  Slice 7 or authoritative selection) **A live shadow comparison authenticates one exact completed
  source master, scores only `legacy_uniform/1` versus `target_core/1`, and always executes the
  legacy control.** The proposal carries an in-memory source reference naming the atomic master
  session key, detector, master revision, predictive/projection artifacts, selection identities,
  and source acquisition provenance. The adapter resolves only those references; a same-Q or newer
  master is never a substitute.

  New inventories use `isar.scan_design_inventory/3` with a mandatory shadow envelope. `off` records
  `not_requested`; `shadow` records complete, partial, or unavailable diagnostic evidence and chooses
  the scored/unscored legacy-control basis accordingly. Every basis still selects the unique legacy
  candidate. `isar.scan_design_candidate/2` and historical inventory `/2` remain unchanged; retained
  Slice 5.1 artifacts are not migrated or reminted.

  Policy authoring is human-owned card content, compiled against the non-recursive current campaign
  configuration scope before campaign start. Frozen objectives are self-contained and carry the
  authenticated policy set plus the separately fingerprinted `isar.scan_window_shadow_scoring_config/o36.6`.
  Card/goal defaults are 128 simulations, eight blocks, and at most eight runtime variants. Explicit
  frozen specs may reduce the simulation count within the validated bounds, but runtime policy and
  variant limits are always enforced before acquisition begins.

  The pure scorer evaluates the exact legacy and target-core predictive grids once each, reuses those
  results across supported policy variants, and preserves the Slice 4 paired orientation alongside a
  normalized target-core-minus-legacy diagnostic. Its seed is the signed-63-bit prefix of SHA-256 over
  the scoring-configuration, design-measure, and candidate-pair identities in the
  `isar.scan_window_shadow_seed/o36.6` domain. Complete or partial evidence grants only
  `eig_shadow = diagnostic`; every selection scope remains ineligible.

  Live scoring occurs between action selection and request finalization under a fixed five-second
  soft allowance charged to the cumulative analysis clock. Deadline, local analysis-limit, and
  unexpected failures persist valid unavailable evidence and fail open to the same legacy request;
  they never catch process interrupts. Realization clamps run afterward and do not relabel the
  pre-finalization comparison. Slice 7 realized-request rescoring, thresholds, winners,
  recommendations, and authoritative selection remain planned and forbidden in Slice 6 evidence.

- **O39.** (2026-08-03, Implemented compatibility and provenance correction to O38) **An omitted
  `scan_window_eig` block in a frozen `isar.objective/2` means the authenticated no-policy default
  scoring configuration; it does not authorize card supplementation, and exact-master shadow
  provenance does not rewrite the proposal's established evidence identity.** Slice 6 added the
  top-level block without minting an objective `/3`, so the `/2` reader accepts historical omission
  and materializes `ScanWindowEIGConfig()` (128 simulations, eight blocks, eight runtime variants,
  no policy). New serialization always emits the complete frozen block. Explicit null, malformed
  records, unknown fields, and card authoring or binding markers still refuse. Full spec, envelope,
  and resume inputs remain self-contained; a historical `shadow` objective with no policy therefore
  produces an unscored legacy control, never a card-derived score.

  The proposal-wide `authoritative_evidence_revision` retains the pre-Slice transition-first fallback
  chain because it participates in finalized request identity. The in-memory
  `ShadowScoringSource.action_provenance`, by contrast, records the exact selected completed master's
  `master_revision` (or null) used by Slice 2 eligibility. These are deliberately separate meanings:
  a transition revision preserves off/shadow request parity, while the exact master revision prevents
  same-Q, later-master, or cross-revision substitution in diagnostic scoring.

  The scan-design layer owns only lightweight `isar.schema.eig_shadow` records; strict scored `/3`
  reads invoke numerical EIG validation lazily. Pair refusal precedence is degenerate alias, exact
  target-generator refusal, missing family, ambiguous family, exposure mismatch, then remaining pair
  invalidity, all before prediction. Incomplete Slice 4 variants may retain fingerprinted nonnumeric
  `score_refusal_evidence`, omitted otherwise for payload compatibility. The five-second allowance is
  cooperative: a deadline check immediately after a synchronous scorer return discards that raw score
  set and marks the current and remaining variants unavailable without changing the legacy request.

- **O40.** (2026-08-04, Implemented in the EIG-4 Slice 7 realized-comparison contract; does not grant
  Slice 8 evidence eligibility or authoritative selection) **Every shadow submission binds the exact
  adapter-realized legacy request and an equal-per-point-exposure target-core counterfactual to one
  diagnostic comparison sidecar.** The immutable Slice 6 `/3` inventory remains the planned-pair
  record. After initial finalization, execution-time finalization, and any minimum-target-support step
  reduction, Slice 7 constructs the pair through isolated copies of the existing finalizer. The target
  starts at the finalized legacy `ncount`; any final exposure change is a structured refusal. Adapter
  point counts govern budget and request identity, and historical designed-span coarsening that still
  exceeds the live cap refuses rather than clamping, switching algorithms, or submitting.

  `isar.scan_design_realized_comparison/o36.7` is the sole independently persisted Slice 7 artifact.
  Its nested realized request/pair/variant/seed records bind source inventory and shadow identities,
  rendered commands, ordered executed grids, request/grid fingerprints, score disposition, and
  diagnostic-only authority. Exact identity/grid/exposure parity reuses authenticated Slice 6 variant
  evidence; any change evaluates and scores the complete pair together under the distinct
  `isar.scan_window_realized_seed/o36.7` domain. Rescored variants nest the existing
  `isar.scan_window_score_set/o36.5` internals. A cumulative five-second in-memory allowance covers all
  Slice 7 refreshes for one proposal, separately from Slice 6's five seconds; worst-case shadow work can
  therefore approach ten seconds. A forced pre-submit refresh consumes only the remaining Slice 7
  allowance.

  Physical submissions and acquisition provenance carry seven realized fields: five identities
  (comparison reference and fingerprint, ordered variant fingerprints, selected request fingerprint,
  and selected grid fingerprint), applicability, and invalidation reason. Returned TAVI energies must
  have ordered adapter-canonical equality with the recorded tuple after representation normalization,
  not raw floating-point bit equality;
  `executed_grid_mismatch` invalidates later authority evidence without blocking ordinary analysis.
  Missed-window recovery retains the parent sidecar reference but clears realized variant/request/grid
  claims and records `eig_realization_invalidated_by_recovery`. Missing or unauthenticated recovery
  sidecars log `realized_comparison_unavailable` and never block recovery, rescore, or search for
  replacement evidence. Refresh/persistence failure or failed pre-submit authentication after one
  forced refresh removes all five current EIG identities and leaves ordinary legacy execution
  available. `isar.scan_design_realization/1` is additive-open, so readers ignore unknown additive
  fields. Shadow mode still always executes `legacy_uniform/1`; thresholds, winners, recommendations,
  target-core execution, and authoritative selection remain forbidden. Slice 8 remains explicitly
  planned.

- **S9.** (2026-08-03, In force; governance and evidence-maintenance rule, no production behavior
  change) **Completed STAT-2b.1 execution evidence is immutable, while current production source
  closure is maintained through an explicitly approved candidate cycle.** The historical held-out
  gate, evaluator identity, and committed aggregate are authenticated and replayed at their frozen
  identities; they are never refreshed to make current source pass. Current production closure is a
  separate manifest rooted at an approved production anchor.

  Protected-semantic drift requires selection or evidence-contract review and proportionate proof.
  Protected-adjacent drift requires bounded source review and focused parity. A repository-local
  dependency newly reached from the protected boundary is unclassified until reviewed; only a
  dependency incapable of affecting that path may be recorded as unrelated and exempt. The whole
  `isar/session/driver.py` remains protected as the high-churn selection-critical owner until any
  authority-seam extraction is separately reviewed.

  A mismatch may remain visible while an in-flight branch updates its closure record, but it must be
  closed before that statistical slice or containing PR merges. Candidate construction and passing
  verification never grant approval. Promotion requires explicit owner review of the complete
  boundary, exemptions, classifications, and candidate fingerprint, followed by a separate minimal
  approval commit that changes no protected production source or immutable review evidence.

  S9 does not amend S8: production remains AIC, no AICc switch is authorized, difficult-regime
  findings remain open, and neither STAT-2c nor another statistical campaign is authorized. O40
  separately authorizes EIG-4 Slice 7 without changing those statistical dispositions.

- **O41.** (2026-08-04, In force; owner acceptance of the EIG-4 Slice 8 V3 evidence and
  authorization to implement Slice 9 only; no production behavior change) **The coherent bounded
  shadow gate is sufficient to implement the restricted authoritative selector, but it does not
  itself make authoritative runtime available.** Owner acceptance binds the passed
  `isar.eig4_slice8_gate_result/o36.8.2` V3 result with fingerprint
  `f6fd65ddf1aca48443d31727c5a81aeddc6b6bfea0eccb5020d8f55ef3b38f8f`. Its positive authority
  envelope contains exactly one clean case, `s8-c01-strong-confirmed`; this is deliberately narrow
  evidence, not a general phenomenon claim.

  Within the frozen O36 envelope, an eligible comparison may select `target_core/1` only when all
  authenticated structural and execution joins hold and target-core-minus-legacy information per
  executed point satisfies `delta - 2 * paired_SE > 0`. The multiplier is a pragmatic engineering
  margin, not calibrated 95% coverage. An eligible unresolved or inferior comparison retains
  `legacy_uniform/1`. An ineligible state takes the existing non-EIG design/recovery path rather than
  treating the legacy comparison control as a universal recovery rule.

  Stale, malformed, unauthenticated, or adapter-grid-mismatched evidence has no EIG authority.
  Slice 9 must preserve the established unavailable-evidence and fallback behavior, including the
  rule that a grid mismatch cannot authorize either candidate. This ruling authorizes implementation
  and review of Slice 9 only: `authoritative` setup must continue to refuse with
  `scan_window_eig_authoritative_unavailable` until that selector lands and receives separate owner
  review. It authorizes no Slice 10 demonstration, additional campaign, STAT rerun, broader
  statistical claim, or production change in this documentation-only acceptance commit.

- **O43.** (2026-08-05, In force; owner ruling during EIG-4 completion; reverses the acting half of
  the O41/`2ea22c8` margin gate) **The scan-window choice is a symmetric comparison on information
  per neutron, and ties are broken by cost, not by retaining the survey.** Neither candidate
  justifies itself against the other. `per_point_ncount_equal` is already a structural
  precondition, so the persisted per-executed-point delta *is* a per-neutron delta up to a fixed
  constant: its sign is the comparison. A positive sign selects `target_core/1`; a negative sign
  selects `legacy_uniform/1` as a real answer about that q rather than as a fallback.

  `O41_MARGIN_SE_MULTIPLE` (3.0, unchanged, and still persisted as
  `margin_standard_error_multiple`) becomes the two-sided half-width of the band in which the two
  candidates are indistinguishable. Inside that band, and equally when the two partition offsets
  select opposite roles, the comparison expresses no preference — so the smaller purchase wins.
  The structural preconditions guarantee the target core executes strictly fewer points at equal
  per-point exposure, so at an equal information *rate* the legacy scan buys proportionally more
  of the same thing for proportionally more beam, and the smaller buy reaches the next decision
  sooner while the peak estimate is still moving.

  **What this deliberately gives up.** `2ea22c8` installed the three-sigma floor and the per-offset
  agreement rule precisely to refuse the R1-E margins of ~1.7e-5 nats/point that changed role
  between the two 0.5 meV offsets. Those cases are now acted on. The evidence for accepting that:
  across the twelve scored decisions of the repaired R3 cold start
  (`test_workspaces/O41_R3_Bragg200`) the 17-point core was **never worse per neutron** than the
  79-152 point legacy scan — the delta was non-negative in all twelve, three were exact ties at
  delta = 0.0 with standard error 0.0, and two refusals rested on offset disagreement at 4.2 and
  202.6 standard errors. Nine of twelve were refused. Under this ruling all twelve select the core.

  **What now carries the risk.** A narrow scan centred on a wrong peak cannot discover it was
  wrong, and an information margin never protected against that. Identity confirmation does, and it
  is already upstream and unchanged: O36 eligibility refuses an unconfirmed target, a fork
  modality, or a pool-inconsistent master before this comparison is reached. Codes
  `o41_offset_disagreement`, `o41_margin_below_floor` and `o41_margin_not_decisive` remain computed
  and persisted as descriptions of a decision; they no longer veto one. New codes
  `o41_tie_resolved_by_cost` and `o41_legacy_decisive` name the two outcomes this ruling adds.

- **O44.** (2026-08-05, In force; owner ruling after the A11 campaign) **An acoustic branch has no
  measurable target at the reduced-q origin, and the campaign must know that from its geometry
  rather than discover it from failed fits.** For an acoustic branch ω(q→0) → 0, so at the zone
  centre the excitation is degenerate with the elastic line and nothing separable exists to measure,
  however many neutrons are spent. That q is therefore not precision-coverage eligible from the
  first iteration, on the branch type and the tracker's published `reduced_q_origin_index` alone.

  This is **branch-conditional, not origin-conditional.** An optic branch has a finite ω at the zone
  centre and is perfectly measurable there — often preferentially — so the exemption keys on the
  target branch. What changes is precisely one thing: that q stops being a *branch target* the
  controller schedules and must certify. Calibration paths are untouched by this ruling and are not
  claimed to schedule it either; if an acoustic campaign is ever required to measure its own elastic
  reference at the zone centre, that is a separate acquisition concern to be designed, not something
  this exemption silently provides.

  **Why the existing exemption was not enough.** `branch_tracking` already retires the origin once a
  linear fit through resolved neighbours can infer the intercept, marking it `inferred_unresolved`.
  That is earned late and only if the fit passes. Until then, fitting a DHO at the zone centre
  returns a centre with an error bar thousands of times larger than itself — measured at
  ω₀ = 0.485 meV ± 2065 meV on A11 — which classifies as a *confirmed* identity rather than an
  unresolved one, so the campaign never earns the exemption and keeps returning. Measured waste:
  **19 of 60 scans in A11 (32%), 122 of 163 in the R3 cold-start baseline (75%), and 152 of 193 in
  its repeat (79%)**, against a constant 41 scans of useful work on the other twenty q in all three.

  Consequence for the stop contract: an acoustic campaign's "N measurable grid q converged" now
  excludes the origin, so N is one lower than the path length. That is the honest count.

- **S10.** (2026-08-06, In force; governance rule superseding the S9 candidate/active closure
  cycle; no production behavior change) **Continuous production source closure is retired;
  evidence freezes are reserved for owner-named milestones.**
  S10 supersedes the candidate/active source-closure cycle of S9: protected-semantic and
  protected-adjacent drift reconciliation, candidate closure generation, owner review artifacts,
  separate promotion commits, and closure-before-merge are no longer required, and the closure
  tooling retains no merge or production authority (it may remain temporarily as an optional
  diagnostic). S9's first half remains in force: completed STAT-2b.1 execution evidence stays
  immutable and authenticated at its frozen identities. S10 does not amend S8 — production model
  selection remains AIC. New formal gates are created only when the owner names a milestone
  (production selection-criterion change, new acquisition authority, first acceptance of a major
  steering capability, real-instrument deployment, or a publication-grade claim); they are designed
  before the final run, executed once, and preserved at their commit. Demotion of the closure tests
  and tooling is executed by the next governance slice under this ruling.

- **S11.** (2026-08-07, In force; verification rule under S10; no production behavior change)
  **Evidence identity is content-aware going forward and EOL-tolerant looking back.**
  Documentation evidence promoted from now on records content-aware identity
  (`docs/assets/MANIFEST.json` `schema_version` 3): JSON and TOML are hashed in canonical parsed
  form, CSV/MD/TXT as LF-normalized text, everything else as raw bytes, so a checkout's line-ending
  policy cannot invalidate an artifact whose content is unchanged. Frozen historical evidence
  manifests keep the raw-byte hashes they recorded — including the Slice-8 shadow-gate bundles,
  whose run manifests mix CRLF- and LF-hashed rows and therefore reproduce on no uniform checkout.
  Those are verified by matching any EOL variant of the retained bytes (raw, LF-normalized,
  CRLF-forced) against the recorded digest; content changes still fail. Verification moves, the
  frozen record does not: this retires the pending "add the `-text` attribute and re-freeze the
  manifests" operation of the PR #9 review (R9), which S10 would otherwise have required an
  owner-named milestone to perform.

- **S12.** (2026-08-08, In force; validation governance under S10/H1; no production behavior
  change) **Real-data golden gates are sealed held-out instruments: run only when the owner
  names it, never consulted during development.** A golden gate is a frozen real-scan scenario
  whose candidate specifications, seeds, declared priors, and expected claims live entirely in
  the gitignored local `test_data/golden/<scenario>/scenario.json`; the repository carries only
  the generic runner (`tests/real_data/test_golden_scenarios.py`, marker `golden`, enabled by
  `ISAR_GOLDEN=1`). The failure this prevents is Goodhart drift: a benchmark whose expected
  answers are visible in the tree quietly becomes a tuning target, and a system fitted to its
  own exam proves nothing to a skeptical colleague. Expectations pin qualitative contracts and
  toleranced quantities (winner identity, evidence orderings, mode positions), not exact
  floats, so legitimate machinery evolution does not spuriously fail the gate; a genuine
  divergence is adjudicated by the owner at the gate run, and per S10 the frozen scenario is
  never refreshed to match current source without an owner-named re-freeze. First instance:
  the H204 IN8 scan (T=1.5 K, B=−3.5 T; frozen 2026-08-08 at `26e4b75`), whose exam record —
  including the blind/assisted evidence contrast that motivates the seal — is retained in the
  bundle's `provenance/` directory.

- **S13.** (2026-08-09, In force; STAT-2 program closeout; no production behavior change)
  **STAT-2c and STAT-2d are closed without further implementation: their substance was delivered
  by H4 by another route, and what H4 did not deliver it made moot.** STAT-2c was written as the
  first possible authority switch off plain AIC; H4 (SCENE-1 PR-b, 2026-08-08) performed that
  switch — production selection is the evidence posterior over structures, stamped
  `isar.model_selection/2`, with `isar.fit_selection_semantics/2` replacing `/1` outright and
  EIG eligibility failing closed on anything but posterior weights. An audit of STAT-2c's
  remaining requirement list against code found each item either delivered (revision bump,
  weight-kind prohibition, downstream consumer rewiring, no-eligible-comparison refusal) or
  dissolved (the constrained-fit lane move: under H4(c)/H5 a prespecified-origin soft prior *is*
  the parameter's prior, entering lnZ exactly once, so those fits are evidence-comparable and no
  lane move exists to perform; `score_models` keeps accepting all converged fits because it no
  longer holds authority — it is the persisted AIC diagnostics lane). STAT-2d's "remove the
  legacy constrained-AIC path" finds no dead path to remove: AIC survives in exactly three
  deliberate places — the per-candidate diagnostics lane, the named recorded-fallback lanes
  (selection-ineligible for acquisition), and the D6 shadow lane with the STAT-2b harness — and
  all three are retained by design. The execution plan
  `docs/plans/STAT2_SELECTION_CONSISTENCY_PLAN.md` is deleted and tombstoned per documentation
  policy (plans leave the tree when their work lands); the accepted behavior lives in H4/H5/H7,
  `STATS_PRIMER.md`, and `GLOSSARY.md`. The STAT-2 lane discipline itself (estimator eligibility
  must never silently influence weights) and STAT-3 through STAT-6 of the statistical-semantics
  audit remain live.

## SCENE hypothesis layer (H-series)

- **H1.** (2026-08-08, In force; opening ruling of the SCENE program — target-free
  multi-phenomenon discovery with competing scene hypotheses; constitutional for the duration of
  the SCENE transition) **ISAR remains pre-release and single-developer: during SCENE, schema/API
  compatibility is not a goal.** Existing objective, tracking, fitting, and acquisition contracts
  may be replaced or deleted where doing so yields a cleaner target architecture. The additive-only
  contract-evolution rule that resumed after the v1→v2 reset (O12, DESIGN §4) is superseded for
  contracts SCENE rebuilds: `isar.campaign-program/1` replaces the objective envelope outright,
  with no compatibility reader, and the retired format becomes unreadable by design. Regressions
  are judged against scientific behavior and the SCENE world suite, not historical serialization
  compatibility; behavioral flips are re-baselined deliberately — one commit per flip set, with
  enumerated rationale. Formal evidence freezes remain governed by S10 (owner-named milestones
  only); for the duration of SCENE the owner names none of the program's internal authority
  changes as freeze milestones — freezes are held for external ones (real-instrument deployment,
  publication-grade claims). New *internal* acquisition authority during SCENE — including
  scene-level EIG and the scan-window authoritative mode — is validated through ordinary
  deterministic test suites and calibration batteries, never through shadow phases, release-review
  ceremonies, or frozen evidence bundles. Under this ruling the pending O42 release review
  (reserved in `docs/MEASUREMENT_STEERING.md`; never issued as a ledger entry) is retired: the
  scan-window authoritative selector becomes default-on once the SCENE world suite exercises it
  (the flip itself lands with that suite, not with this entry). Compatibility shims, adapters for retired formats, and defensive dual-path
  readers are defects under this ruling, not prudence. S8 (production model selection remains AIC)
  is not amended here; its supersession is a SCENE-1 ruling, recorded when the evidence-based
  selector lands.
- **H2.** (2026-08-08, In force; SCENE-2 — the campaign contract) **`isar.campaign-program/1`
  replaces `isar.objective-envelope/2` outright, exercising the H1 ruling.** A campaign program is
  an ordered list of steps, each carrying its own schema — `isar.campaign-step.targeted/1` for the
  per-point-tolerance objective that was the envelope's payload, and
  `isar.campaign-step.discover_and_map/1` for target-free discovery. `isar.objective/1`,
  `isar.objective/2`, `isar.objective-envelope/1`, and `isar.objective-envelope/2` are refused on
  read with an error naming the replacement; they are unreadable by design and no compatibility
  reader will be written, because under H1 a shim for a retired pre-release contract is a defect.
  The CLI source flag is `isar drive --program <file.json>`, mutually exclusive with `--goal` and
  `--resume-latest`; `--compile-only <OUTPUT>` writes the frozen program and exits 0. A well-formed
  program this build cannot execute — a discovery step, or more than one step, both of which land
  in SCENE-4 — exits **7**, deliberately distinct from the malformed-input exit 2 so an operator
  script can tell "not built yet" from "written wrong". `--compile-only` on a discovery program
  therefore exits 7 rather than freezing it; that refusal is accepted for now and lifts with
  SCENE-4. The campaign fingerprint's comparison scope changes with the contract — start records
  name `program_schema` and `program_step_id` where they named an envelope — and that change is
  intended, judged against the SCENE world suite rather than against comparability with pre-SCENE
  campaigns.
- **H3.** (2026-08-08, In force; SCENE-1 PR-a — catalogue + grammar) **The phenomenon catalogue
  (`isar/analyze/catalogue.py`, `isar.phenomenon-catalogue/1`) is the single authority for what
  can be present in a scan, and the phonon-less nulls are first-class candidates.** Six entries —
  acoustic, optic, elastic, quasielastic (its own phenomenon, measured in its own right and
  distinct from the not-yet-catalogued diffuse entry — owner ruling 2026-08-08), unknown, bg_only.
  The catalogue owns no physics: DHO gates and recipes stay in `analyze/phonon.py`; entries hold
  references. `enumerate_phonon_structures` keeps its name as the catalogue-backed implementation
  and now closes with `elastic+bg` (elastic gate) and `bg`, so selection can honestly prefer
  "nothing inelastic here". Verdicts read that winner: a significant `elastic+bg` winner is
  **`elastic_only`** — a positive but *categorically humble, scan-local* claim (owner ruling
  2026-08-08: elastic intensity is ubiquitous — incoherent + can/sample background, strongest near
  Bragg — and can mask arbitrary signals; mapping/excluding elastic is future scene-level work
  tied to the TAVI-background workstream; it is never a region-exclusion verdict). Its confidence
  is `S_SCOPE_ELASTIC (0.5) × geomean(s_structure, s_provenance, s_detectability)` — a hard
  product cap, audited by name — and the claim requires the elastic line to be significant over
  bare background (nested LRT vs `bg`, α=0.05), else the scan falls to the existing
  `no_signal`/`underpowered` null convention; `provisional=True` whenever a prior expects a mode
  the scan lacked confirmed power to exclude (§0.4 guard). Dormant declarations recorded now,
  consumed at PR-b: per-entry parameter priors; **constraint transport** (transportable
  parameters with per-parameter smoothness class + scale in r.l.u.; one-directional
  clean→complicated through the existing `ParamConstraints` soft path, applied-exactly-once like
  catalogue priors — never the banned parameter-compatibility re-scoring; a field break such as
  an avoided crossing is a typed observation); and the near-degenerate acoustic **constrained
  variant** (FWHM = resolution, Bose-tied amplitude I ∝ (n+1)/ω ≈ k_BT/ω²; exact reduced-q origin
  stays excluded per O44). Consequential re-baselines under H1: the STAT-2b campaign-only null
  family shrinks to the QE-bearing nulls, `elastic_only` abstains on the gold axis, and counts as
  a missed window for the drive supervisor.
- **H4.** (2026-08-08, In force; SCENE-1 PR-b — Laplace evidence) **Production model selection is
  the posterior over structures, computed from per-candidate Bayesian evidence; AIC becomes a
  persisted diagnostic. S8 is superseded.** Each converged candidate's lnZ is produced by a
  validity ladder — Laplace at θ̂, warm-started MAP refit under the full prior bundle, or dynesty
  nested sampling — with the rung and every escalation reason recorded per candidate; a silent
  Gaussian volume on a boundary-pinned fit is structurally impossible. Priors obey the
  exactly-once rule (a transported soft constraint IS its parameter's prior; hard-fixed
  parameters contribute nothing), evidence is comparable only within one data-likelihood arena
  (cross-arena comparison raises), and selection is P(s) = π_s·Z_s / Σ π·Z with π uniform over
  admissible structures — the weight the pooling, EIG, and master consumers read. Dominance
  pruning skips the sampler for a deferred escalation whose evidence ceiling (lnZ ≤ −cost/2)
  sits ≥ `PRUNE_MARGIN` (100) nats below a trusted leader, recorded as `dominated`; surviving
  escalations sample at n_live=128 (`ESCALATION_N_LIVE`, scoped to this lane; the bayes-tier
  error diagnostics keep D4's n_live=400, and the n_live used is recorded in the candidate's
  validity provenance). `isar.fit_selection_semantics/2` replaces `/1` outright per H1 — no
  compatibility reader; the recorded AIC fallback lanes (`aic_no_evidence_inputs`,
  `aic_all_evidence_invalid`) are named degraded modes and are selection-ineligible for
  acquisition steering (fail closed). S8's retention of AIC as production authority is
  superseded; its diagnostic content survives — AIC/AICc/BIC are computed and persisted per
  candidate, and the STAT-2b harness and shadow-selection lane stay AIC-labeled by design (they
  measure criteria, D6). Recorded with this entry, per the PR-b rulings of 2026-08-08: **(a)**
  the Bose factor is universal — thermal occupation (n(ω₀,T)+1) is an explicit factor of every
  phonon amplitude at the component layer, so the fitted amplitude is the intrinsic strength and
  amplitude transport is physical; fitted-A values re-baseline deliberately, ω₀/Γ untouched;
  **(b)** the acoustic/optic/unknown identity framing is deferred to its own design discussion —
  PR-b evidence is per-structure, the prior machinery keeps the identity axis additive, and when
  identity lands it records, it does not drive (branch assignment keeps driving until the
  SCENE-6 swap); **(c)** STAT-1.5 is recast under `/2`: prespecified-origin soft-prior
  (transport) MAP fits are evidence-comparable — the prior enters lnZ exactly once as itself —
  while `data_dependent_rescue` and `post_selection` origins remain selection-ineligible.
- **H5.** (2026-08-08, In force; PR-b review response) **H4(c) is implemented in the selection
  lane only: a prespecified-origin soft prior costs no selection eligibility, and grants no
  acquisition authority.** The producer gated on the *presence* of a soft constraint, so a
  transported branch-model prior read as `ineligible_soft_penalized` — contradicting H4(c). It now
  gates on origin: `prespecified_scientific` / `prior_predictive` (`PRESPECIFIED_SOFT_ORIGINS`,
  owned by `isar/schema/statistics.py` so the vocabulary and the rule that reads it stay in one
  place) no longer cost eligibility; `data_dependent_rescue` / `post_selection` still do, and an
  unrecognised origin fails closed. The `/2` schema invariant is **narrowed, not removed**: an
  eligible record may carry effective constraints only when every one of them is a prespecified
  soft prior — any hard conditioning still raises `eligible_with_effective_constraints`.
  **Acquisition is deliberately untouched.** `estimator_kind` stays `soft_penalized_likelihood`
  and the constraint stays recorded, so `isar/eig/eligibility.py` still refuses the fit on the
  non-MLE estimator and on `selection_constrained`; a test pins that boundary explicitly. Letting
  soft-prior fits steer beam time is a new acquisition authority and needs its own owner-named
  ruling — this entry is not it.
- **H6.** (2026-08-08, In force; PR-b review response) **The intensity basis is renamed by the H4
  amplitude change, and the old name is a refusal, not a migration.** An `IntensityMeasure` value
  is now `intrinsic_amplitude_per_exposure`; a pre-H4 point persisted the MEASURED amplitude
  `A·(n+1)` under `amplitude_per_exposure`. Nothing in the stored number distinguishes the two and
  the occupation is not recoverable after the fact, so a resumed workspace would fit `log A(ω₀)`
  across incomparable anchors and emit `phonon.A` constraints wrong by `(n+1)`. Following the
  `RETIRED_SCHEMAS` precedent (H1), `RETIRED_AMPLITUDE_BASES` is read as a refusal table:
  `build_branch` **excludes** such anchors and logs one line naming the count and the remedy
  (re-fit those scans). Exclusion rather than raising, because this is consumed mid-campaign — a
  resumed campaign should lose incomparable anchors and say so, not abort; below `MIN_ANCHORS` the
  existing "too sparse" path already returns no branch. Correspondingly the producer refuses to
  interpret a mode record carrying only `amplitude`: `None`, never a measured value relabelled
  intrinsic.
- **H7.** (2026-08-09, In force; SCENE closeout — escalation cost) **The escalation phase of one
  candidate set is bounded in wall clock, and an exhausted budget degrades selection instead of
  hiding.** `candidate_set_evidence` gains `budget_s` (`ESCALATION_BUDGET_S`, `None` disables);
  a candidate left unsampled is recorded `unresolved` carrying its evidence ceiling — no lnZ, no
  posterior entry, and explicitly *not* ruled out, which is what separates it from `dominated`.
  `EvidenceSet.unresolved_contenders()` reports the skipped candidates whose ceiling still
  reaches the best trusted evidence; when any exists the comparison is **incomplete rather than
  close**, and `select_models` degrades to the recorded AIC lane with the new
  `evidence_budget_exhausted` fallback rather than ranking on a posterior over whichever
  candidates happened to be sampled. That lane is already selection-ineligible for acquisition
  (H4), so the degradation is owner-visible where it matters. Precedence is deliberate: dominance
  is decided from an already-computed ceiling at no sampler cost, so it is evaluated first and a
  certain ruling is never lost to the budget; a skipped candidate whose ceiling sits below the
  leader is ruled out by its own bound and costs the set nothing. The budget is a **backstop
  against a pathological set, not a throughput lever** — the expensive class is a superset of the
  winner, whose ceiling is ≥ the leader's by construction (exactly why dominance pruning cannot
  touch it), so a tight budget would push ordinary campaign scans into the degraded lane. It is
  checked before each escalation begins rather than interrupting one, so a set may overrun by a
  single dynesty run, itself bounded by `max_iter`. The cost *reduction* this backstop does not
  provide is the N-mode growth policy, which remains an open physics decision. This entry is the
  first concrete instance of the "budget-exhausted honesty" acceptance world of `SCENE_DESIGN.md`
  §4; the world itself is not yet built.

- **H8.** (2026-08-09, In force; SCENE closeout — N-mode growth design) **A declared peak count is
  a prior that permits a candidate, and "boundary-pinned" is three different physical situations,
  not one.** Recorded now because each clause governs work that lands later (SCENE-1 PR-c,
  SCENE-3/4); none of it is implemented by this entry.
  **(a) Declaration permits, it does not force.** `loop.py`'s `_offer_multimode` gate
  (`cost_per_dof > GATE_COST`, or a Wald–Wolfowitz structured residual) is a measurement;
  `expected_two` — a guideline declaring ≥2 peaks — currently bypasses it outright. The gate
  stands and a declaration *relaxes* it, on the same footing as every other declared prior. A
  declaration must still be able to raise a candidate the diagnostic would not have offered,
  because a candidate that is never enumerated can never be evaluated; what it may not do is
  compel one. Hard enforcement ("exactly two, no more") is a separate future feature, explicitly
  not authorized here. Note for the record that enumeration is not a probabilistic thumb on the
  scale: with π uniform over admissible structures, adding a candidate dilutes every prior to
  1/N, so forcing a candidate never helped it win — the objection to the bypass was always cost.
  **(b) The three rails get three treatments.** *Amplitude → 0* is not a mode: no intensity, no
  observable, no physics. It may be dropped without paying for evidence, recorded with a reason
  (as `dominated` is) rather than deleted. *Ω_d at the window edge* is legitimate and sometimes
  mandatory — real data carries excitations outside the fitted window (the IN8 H204 window starts
  near 2 meV), and refusing them means being unable to fit the data at all; the dynesty
  escalation it triggers is therefore spent on physics, not junk, and a DHO pinned against a wall
  is a poor description of a mode outside the window. *Γ → 0* is ordinary — instrument resolution
  dominates — so the hard floor is a modelling artifact putting a wall where the physics has
  none. In each case the *statistics* of the escalation are correct as parameterized (an MLE at a
  bound truncates the posterior, so the Gaussian volume genuinely is wrong); the remedy is to fix
  the parameterization, e.g. a Γ prior with support to zero under the H4 bundle, which makes the
  boundary integrable rather than pathological.
  **(c) Growth is residual-located and earns its keep on evidence.** Offer mode N+1 where the
  residual will actually accept one — `greedy_multimode_ic` already grid-scans exactly that — in
  place of a global goodness-of-fit trigger. No admission test beyond the marginal likelihood is
  needed or wanted: Z averages the likelihood over the prior, so an added mode that buys nothing
  spreads the same fit quality over more volume and loses. That is the Occam factor, automatic
  and untuned.
  **(d) Near-degeneracy is reported, never silently skipped.** `greedy_multimode_ic` today drops
  mode-2 candidates within one resolution FWHM of mode 1. A hard "cannot identify, skip" rule is
  refused: attempt the fit under the declared-dormant constrained variant (FWHM pinned to
  resolution) and state honestly that these tools cannot separate the pair, rather than returning
  a candidate that never existed.
  **(e) The stopping rule is neighbouring scans, never a hard-coded physical bound** — a
  geometric cap on branches per window is precisely the kind of rule that invites a trap. A
  phonon branch cannot appear at one q and vanish at the next, so mode count is testable across
  scans in a way it never is inside one noisy scan. Two consequences bind SCENE-3/4: mode-count
  uncertainty must be *representable* rather than collapsed to a winner, and an unresolved count
  is an acquisition signal — "three modes, poorly constrained" is a reason to measure the
  neighbour. The count question therefore belongs to the scene and question layers, not to
  `loop.py`.

- **H9.** (2026-08-09, In force; SCENE-1 PR-c) **Constrained variants, the local identity axis,
  and declared-constraint vocabulary are live; identity records and never drives.** Five parts.
  **(a)** `user_declared` joins the constraint-origin vocabulary and `PRESPECIFIED_SOFT_ORIGINS`:
  a scientist's pre-fit soft declaration is prespecified by construction and enters lnZ exactly
  once (H4(c)); eyeballed *seeding* deliberately has no origin class — initial conditions never
  touch `ParamConstraints` and carry no evidence weight. **(b)** The Γ floor is an integrable
  boundary, not an invalidity: a converged fit whose only pins are `.Gamma` takes the
  `laplace_truncated` rung — rung-1 Laplace shedding the Gaussian mass outside the support —
  instead of dynesty (per H8(b) the escalation there was a parameterization artifact). Ω_d
  window-edge pins and unknown pin status still escalate. The mass factor carries a measured
  ≈1–2 nat conservative bias at genuinely railed bounds (one-sided-exponential vs half-Gaussian);
  accepted deliberately — the direction never inflates a railed candidate and selection-material
  gaps in this class are 10–40 nats; a gradient-aware factor is the named upgrade path.
  **(c)** A catalogue `ConstrainedVariant` is executable (predicate + pins) and competes as an
  ADDITIONAL candidate under a derived per-candidate context — H8(d)'s near-degenerate acoustic
  twin pins intrinsic Γ to the floor so the observed width is pure resolution; its origin
  `catalogue_variant` is deliberately NOT prespecified-soft, because a hard-conditioned fit is
  never an unconditioned data-likelihood estimate. Selection eligibility hardened to match the
  schema invariant: eligible ⇔ every effective constraint is soft with a prespecified origin.
  **(d)** Identity is open-vocabulary, catalogue-driven, and lives in (Q,E) space as cross-scan
  objects: a scan's fit CONTRIBUTES per-candidate evidence (structure × identity twins split
  only where rival entries declare distinguishing priors — the no-dilution rule) and the
  persisted per-label lnZ is the scene store's aggregation input; branch assignment keeps
  driving delivery until SCENE-6, with `identity_ba_disagreement` recorded, never acted on.
  **(e)** Deferred by name: the unresolvable near-degenerate PAIR (amplitude tie
  A₂ = A₁·ω₁/ω₂, classical Bose) awaits cross-component tie machinery and an owner design
  pass; `greedy_multimode_ic`'s close-seed skip stands until then; out-of-window mode treatment
  is SCENE-3's; no production catalogue entry declares identity-conditioned priors yet — the
  numbers are the owner's to declare, the mechanism is ready.

- **H10.** (2026-08-09, In force; SCENE-1 PR-c review response) **An offered variant only
  disqualifies a scan from steering by winning, and the boundary-mass factor is exact for its
  local model — with H9(b)'s bias story corrected on the record.** Two rulings. **(a)** The
  near-zone-centre acoustic and crossing regimes deliberately offer close-together rival
  hypotheses; "offered, priced, rejected" conditions nothing about the winner, so a NON-winning
  candidate whose effective constraints all carry origin `catalogue_variant` contributes neither
  eligibility nor reasons to the selection aggregate and adds no science codes in the EIG
  consumer. Fail-closed edges kept: a variant that wins stays ineligible, any other constraint
  origin still disqualifies, and the arena check still counts every candidate. **(b)** The
  `laplace_truncated` mass factor is now gradient- and offset-aware — exact for the local
  linear-plus-quadratic exponent: `ln mass = (gσ)²/2 + lnΦ(a/σ − gσ)` with g the inward
  one-sided data-cost gradient and a = θ̂ − lo, quadrature-verified, reducing to the
  half-Gaussian at g = 0, a = 0. **Premise correction:** H9(b) attributed the ≈1–2 nat
  dynesty gap to a "conservative over-correction" of the half-Gaussian; measurement refuted the
  direction — the exact factor sheds MORE mass than ln ½ for every g > 0 (the gradient means
  less than half the Gaussian sits inside the support), so the boundary factor cannot be that
  gap's source. The residual gap on the synthetic probe sits within the sampler's own
  seed-to-seed scatter (~1 nat at n_live=400) and is attributable, if real, to trusting HESSE
  curvature at a railed axis. H9(b)'s acceptance of the rung stands; its causal story does not.

- **H11.** (2026-08-10, In force; SCENE-3 PR 3a) **Scene layer input contract, probability model,
  and the scene-factor ban.** Three parts, and one retirement.
  **(a) The interpretation artifact is retired as an input channel.** Decision 8 of the
  2026-08-07/08 SCENE program grilling planned a per-scan interpretation artifact for the scene to
  read; it is narrowed out of existence. The scene consumes the persisted candidate rows through
  the typed `workspace_api` adapter, `FitOutcome.interpretation_ref` stays dormant and is never
  written, and a scene revision EMBEDS the rows it consumed. The reason is auditability, not
  economy: a revision that carries its own inputs and a digest over them can be replayed and
  disputed years later, whereas a revision pointing at a mutable artifact can only be believed.
  **(b) The scoring contract is SCENE_DESIGN §2b, and its invariants are test obligations.**
  `ln W(S) = Σ_j lnZ(j, h_j(S)) + Σ_f ln φ_f(S)`, normalized within a cluster. One arena
  contributes its local evidence exactly once and a pooled master supersedes its member scans (the
  adapter's ownership rule); normalized per-scan posteriors are never multiplied, because that
  arithmetic re-applies the local priors once per scan; a multimode structure's lnZ enters once
  even when an alternative assigns its components to different phenomena, since the modes were
  fitted jointly to the same counts and splitting the evidence per component would manufacture
  evidence that was never measured; a gap in coverage contributes no update in either direction;
  an arena with no usable finite lnZ is experiment-map-only — no evidence, no term, no cluster
  membership, footprint still recorded; identity may be `None` locally (H9(d)) and arise purely at
  scene level. Enumeration is capped and pruned rather than approximated: the kept set is exactly
  the top of the base ordering, and both the cap and the per-arena margin record a stated upper
  bound on the mass they dropped.
  **(c) The scene-factor ban, refined to the two cases that would be easy to write by accident.**
  No factor term may depend on a single observation's parameters alone — that is a local
  likelihood wearing a scene factor's clothes — and none may re-apply an identity or parameter
  prior already priced into local lnZ. Factors read only the typed `ComponentObservation` surface,
  which carries no counts, no θ, no covariance, no lnZ and no local posterior, so the ban is
  structural rather than a reviewer's memory. Fitted parameters may inform the BUILDER's discrete
  structure derivation (what couples to what, what the alternatives are); they may never enter a
  scored smoothness or compatibility term. Cross-scan parameter coupling has exactly one sanctioned
  channel, and it is constraint transport in local evidence, per the PR-a transport design. Every
  factor declares `scene_prior` or `structural_likelihood`, a version, and the scene variables it
  consumes; the declaration set is fingerprinted into each revision, so a scored scene names the
  factor set that scored it.
  **Supersedes/relates:** narrows the SCENE program plan's decision 8; rests on H1 (validation by
  the world suite, no shadow ceremony), H4 (the evidence posterior these lnZ values come from) and
  H9(d) (the local identity axis).

- **H12.** (2026-08-10, In force; SCENE-3 PR 3a review response; amends H11) **The scene-factor ban admits
  genuine joint likelihoods, evidence sinks are channels rather than noise, and nothing may be
  discarded on evidence a factor has not yet contributed.** Six parts; **(a) amends H11(c)**.
  **(a) H11(c) amended — the ban is about double-counting, not about arity** (external-review
  adoption, owner ruling). H11(c)'s "no factor term may depend on a single observation's
  parameters alone" was the right prohibition stated as the wrong rule. The prohibition is: a
  scene factor may not score a function of a single observation alone when that information could
  have been priced in its local evidence, and may not reapply any prior or constraint already
  contained in local lnZ. Genuine joint cross-observation structural likelihoods ARE permitted —
  when they carry declared probabilistic semantics and propagate the relevant uncertainty rather
  than asserting a preferred configuration. Constraint transport is therefore one sanctioned
  mechanism for cross-scan parameter coupling, not the only one, correcting H11(c)'s "exactly one
  sanctioned channel". Enforcement is mechanical, not rhetorical: conditioning provenance
  (`constraint_origin`, `effective_constraints`, the evidence arena fingerprint) travels with
  scene evidence, so the builder can see which arena already priced a transported constraint and
  hold the exactly-once rule itself. *Corollary, and the failure this prevents:* an unpriced
  builder heuristic doing a likelihood's job — a hard threshold, a smoothness bonus, a
  compatibility reward — is exactly what the ban exists to stop, and it stops it whether the
  heuristic reads one observation or ten.
  **(b) Ownership rule refined: an evidence sink is a scientific evidence channel.** A
  non-canonical sink is not lesser evidence, it answers a different question:
  `auxiliary_track_node` is identity evidence, `track_edge_evidence` is connectivity evidence,
  `branch_belief_only` is transition evidence. The adapter therefore PRESERVES those candidate
  rows with the sink attached instead of emptying the table, the builder makes the routing
  decision (retained and deferred, or quarantined when the string is uninterpretable), and
  scene-level routing of the three channels is named deferred work rather than silent loss.
  Supersession considers canonical masters only, so a barred master no longer erases its members'
  arenas. **Supersedes** the footprint-only treatment of auxiliary sinks in the SCENE-3 plan's
  decision 9.
  **(c) The pruning and cap contract: base-lnZ discarding is legal only under declared factor
  bounds.** Pruning candidates and capping alternatives on Σ lnZ before factor terms exist is
  exact only for an empty factor registry; with a scoring factor active a discarded candidate may
  be rescued by its term, and the recorded mass bound can point the wrong way by orders of
  magnitude. So: every factor declares `score_bound`, a bound on its total ln-term per
  alternative; margin pruning is permitted only within the summed span of the active scoring
  factors, with the effective margin and the widened bound both recorded; the truncation bound
  carries the same span; and ONE unbounded scoring factor disables margin pruning entirely and
  makes a truncated cluster record `truncation_unbounded` with no bound at all. An unbounded
  truncation is an unresolved cluster, never a silently narrowed one — a false bound is worse
  than no bound. Exact enumeration is the default posture (`SCENE_ALT_CAP` 64 → 4096), and the
  declared bound is enforced during scoring rather than trusted. Refines H11(b)'s "the kept set
  is exactly the top of the base ordering": it still is, and under active factors the base
  ordering is no longer the total ordering, which is why the recorded bound must widen.
  **(d) The legacy fallback is deleted, and the standing posture is recorded** (owner ruling).
  Bare member labels and a candidates-column-less workspace now refuse scene construction with a
  message naming the workspace to re-deliver, instead of guessing at ownership or reporting a
  stale file as an empty scene. General posture, so this is not re-argued per slice: pre-release,
  with a sole developer and user, there is no backwards-facing code by default — no compatibility
  shims, no dual-path readers, no aliases for renamed keys. The owner re-decides this before any
  major campaign, at which point data in flight starts to have a claim.
  **(e) H11(a) restated: one self-contained immutable layer, not two.** The revision embeds the
  exact evidence inputs it consumed — the candidate rows, the comparison-completeness status, the
  conditioning provenance — and digests them, so it can be replayed and disputed rather than
  believed. The vestigial `interpretation_ref` pointer is removed outright rather than left
  dormant: a reserved field that nothing writes is an invitation to build the second layer this
  decision declined.
  **(f) LOFO is named score-term ablation.** The operation holds the hypothesis space fixed and
  removes one factor's SCORE terms; a factor's `couples` and `propose` contributions to what the
  alternatives are remain. That is the useful question ("given the same hypotheses, how much did
  this term move belief?") but it is not leave-one-factor-out, and the old name promised a
  rebuild the code deliberately does not do.
  **Supersedes/relates:** amends H11(c) and refines H11(b); supersedes the SCENE-3 plan's
  decision 9; rests on H7 (comparison completeness) and H9 (conditioning provenance).

- **H13.** (2026-08-10, In force; SCENE-3 PR 3b) **Sample knowledge is declared, never derived;
  Bragg is a scene-level classification rather than a likelihood; a declared structure may
  terminate a track without penalizing it; and absence inside coverage is priced even when the
  neighbouring arena is empty.** Five parts.
  **(a) Sample knowledge enters the scene as a DECLARATION, and zone geometry is independent of
  the reflection list.** `build_scene(..., sample=SampleKnowledge(...))` carries the scientist's
  declared reduced-zone period, Bragg reflections along the scan path, and the lattice epoch in
  force, lifted off the experiment card by `isar.workspace_api.scene.sample_knowledge` — read
  only, since the policy half of the card is hand-authored. Zone centres are the multiples of the
  declared period anchored at the crystal origin `q = 0`, never at a measured or tracker-selected
  q (the O44 rule, ported). The failure this prevents is the FCC (300) case: (300) has integer
  indices, sits exactly on a fold of the reduced-zone period, and is EXTINCT. So the period never
  confers a reflection, and no space group, structure-factor table, or integer-index shortcut
  derives one declaration from the other — a scene that inferred reflections from periodicity
  would confidently call an extinct position Bragg. Nothing is a required card key: an undeclared
  field leaves the capability that consumes it inert and names itself in the revision's
  `sample_knowledge.degradations`, because a capability that was never asked for and one that ran
  and found nothing are different states and a silent scene cannot tell them apart. A malformed
  declaration fails loudly instead (H1).
  **(b) "Bragg" is classified at the scene, and in PR 3b it is classified and not scored.** The
  local catalogue's elastic entry is lattice-blind by design — it fits a resolution-limited
  feature at ω = 0 and knows nothing about where in reciprocal space it sits — so the local
  evidence cannot have priced lattice position, and classifying it at the scene re-prices nothing
  (H12(a)). `reciprocal_lattice_localization` labels every non-mode component `bragg_compatible` /
  `off_lattice` / `unclassified` / `unplaceable_q` against the declared list, recorded per cluster
  under `classification`. The last two are distinct on purpose: `unclassified` is "no reflections
  were declared", whose reason is recorded build-level in `sample_knowledge.degradations`, while
  `unplaceable_q` is a per-arena fact that a declaration-level ledger cannot see — merging them
  would leave an unplaceable arena inert with no reason recorded anywhere. It attaches no likelihood, and the reason is categorical humility about the
  elastic channel: elastic intensity is ubiquitous (sample environment, mounts, incoherent
  scattering, powder on the plate), so an off-lattice elastic feature is not improbable — it is
  merely not-Bragg. Scoring it would penalize a completely ordinary observation. Modes are not
  classified at all: a phonon at a reflection's q is a phonon.
  **(c) `structural_endpoint` is assigned by the zone-centre factor, and it carries no penalty by
  EMERGENCE rather than by exception.** `zone_centre_approach` reinterprets one neighbour report
  when a zone period is declared and the alternative READS the mode as that branch terminating: it
  is the terminal node of a linked track, the track descends toward the centre over its final edge,
  the declared geometry applies on the derived path, and a contiguous run of covered,
  elastic-bearing consecutive arenas reaches the fold (a declared dispersion slope, where there is
  one, must be able to carry the remaining energy to zero). The original PR 3b predicate — a low
  |ω| next to a centre — was granted from energy alone and so exempted isolated features and flat
  low-lying tracks that the declared gapless physics says nothing about; the reading competes with
  the same assignment's other linkages and is priced against them instead.
  `blocked_unresolved` says *unresolved pending better data* — count
  longer and the answer may change; `structural_endpoint` says *the declared physics terminates
  the track here* — nothing is missing, and no amount of counting will produce a peak. The
  distinction is what an operator acts on. Presence and connectivity key on `searched_no_signal`
  alone, so a reinterpreted report simply stops being priced; special-casing the exemption into
  two score functions would have put the same physics in three places. A `refine_visibility` hook
  may only replace an absence state — never `observed`, never `censored_outside_footprint` — and
  the builder enforces that rather than trusting each factor, because a hook able to rewrite a
  detection could argue a real measurement out of existence.
  **(d) Coverage coupling closes the presence hole (carried item 15), and coupling stays
  footprint- and discrete-deterministic.** A cluster edge required a component on both sides, so
  an arena whose surviving candidates were all background-only emitted nothing and could never
  join a cluster. The consequence was inverted physics exactly where absence evidence is
  strongest: a track disappearing into a searched, covered, entirely empty neighbour went
  unpriced, while the same track disappearing into a neighbour that happened to hold something
  else was priced. The `couples_arena` hook couples a mode to a whole neighbouring ARENA through
  its footprint, gated on the same coverage predicate `visibility_of` uses — no coverage is no
  claim, so it grows no cluster. It is a graph-layer change, not a scoring one, and it reads
  footprints and discrete component positions only, so the partition stays deterministic. Only
  scoring arenas participate, so an H7-incomplete arena is never resurrected through its
  footprint.
  **(e) The spurion vocabulary is three distinct outcomes, and none of them is "not a mode"**
  (program decision 11, recorded here before any code exists for it). A feature that does not
  cohere with the scene is one of: *unknown-physical* — real scattering the declared model does
  not describe; *spurious* — an artifact of the instrument, which coheres in ITS OWN coordinates
  (analyzer/monochromator angles, incident energy) rather than in (Q, ω), which is exactly how a
  spurion is told from physics; or *no_signal* — nothing there. The vocabulary is
  registry-extensible because the list of known spurion mechanisms grows with the instrument, not
  with this repo's release cycle. PR 3b adds no spurion catalogue code; recording the distinction
  now prevents the cheaper collapse into a single "rejected" bucket, which would lose the one
  discriminant that matters.
  **Supersedes/relates:** rests on H11 (the scene input contract), H12(a) (what a factor may
  price) and H12(c) (score-bound honesty — the two factors here declare no score, so pruning is
  unchanged by their presence); ports O44's origin rule for zone geometry; H1 governs the
  fail-loudly treatment of a malformed declaration.

- **H14.** (2026-08-10, In force; SCENE-3 PR 3c, and the PR #19 response waves it carries;
  amends H13)
  **A scene revision is a content-addressed, replayable artifact whose lineage lives outside its
  digest; it is read back through the facade, built observe-only at every delivery, and it honours
  the branch tracker's four scientific contracts by its own route.** Five parts. Parts (a)–(c) also
  record the response-wave semantics of PR 3a/3b, ratified by the owner on 2026-08-10; (a) amends
  H13(c)'s account of which factors read a neighbour report.
  **(a) What the response waves settled about the scene's own vocabulary** (owner rulings,
  2026-08-10), each clause naming the failure it prevents.
  *Component descriptors, not label grammar.* A candidate row persists ordered
  `ComponentDescriptor`s (`kind` ∈ `mode`/`elastic`/`quasielastic`, plus a mode's `slot` into that
  row's `omegas`), derived at the writer from the ENUMERATED STRUCTURE the candidate was fitted as.
  A display label is a stable identifier and never a second schema — a label grammar that drifted
  by one suffix would silently reclassify physics instead of failing.
  *Footprints are merged bin CELLS of the original grid under the kept mask.* Coverage is built
  from the kept bins' cells (midpoint to midpoint, adjacency read structurally off the grid index),
  not from the window: an interior QC hole or excluded region censors, so absence inside it is
  `censored_outside_footprint` rather than evidence. `energy_window` is the kept-POINT extent and
  is NOT the coverage envelope. There is deliberately no gap-threshold knob (`BAND_GAP_FACTOR` is
  deleted): any factor times a global spacing statistic misreads a stitched coarse-to-fine axis one
  way and a comb rejection the other.
  *Support follows the alternative's linkage.* A mode is `observed` at a neighbour when THIS
  alternative links it there; the any-partner-within-tolerance proximity match is deleted rather
  than kept alongside, because a second assignment channel could disagree with the linkage the
  alternative asserts, and one observed peak would otherwise support every nearby mode.
  *One null, one charge.* Detection is `presence`'s, and connectivity is score-free structure:
  `connectivity` keeps `couples` and `propose` and declares `score=None`, its `CONNECT_BREAK_NATS`
  deleted rather than re-homed, because a neighbour that looked and found nothing is the same null
  `presence` already charges as a miss. **Ratified consequence:** the effective penalty on a
  vanishing track is now −1 nats per (mode, neighbour), not −3. Re-pricing the weights belongs to
  the SCENE-7 calibration battery, not to a review response. This amends H13(c)'s "presence and
  connectivity key on `searched_no_signal`": only `presence` reads a neighbour report at all.
  *Topology, not radius.* Adjacency is CONSECUTIVE ordinals along the path derived from the q set
  alone (`PATH_COLLINEAR_TOL`); `Q_ADJACENCY`/`q_adjacent` are deleted. A radius made every arena
  inside it a neighbour of every other, so inserting an intermediate scan left the skip-neighbour
  term standing beside the two new ones and priced one absence twice. A non-collinear set fails
  closed — no ordinals, no coupling, `path_topology_unavailable` — and the card's SCALAR zone
  period and reflection list apply only where the derived direction IS a reciprocal axis
  (`PATH_AXIS_TOL`), else `zone_geometry_inapplicable_on_path` /
  `lattice_geometry_inapplicable_on_path`. A program-carried topology is the named upgrade, not a
  default to drift into.
  *Detectability gates every would-be absence.* Before a `searched_no_signal` may be charged: on a
  band covered only from the anti-Stokes side the detailed-balance weight `exp(−ω/k_BT)` must clear
  `AS_DETECT_WEIGHT` (1/3) — the scene's amplitude-free mirror of production's `AS_DETECT_Z`, with
  the dimensional difference between a weight floor and a z-score on predicted counts noted in the
  source rather than papered over; the neighbour must have counted comparably deep
  (`DETECT_EXPOSURE_RATIO` 0.3); and where the neighbour holds mode components of its own, the
  continuation must be reachable under a declared dispersion slope — with none declared, such a
  neighbour is `reachability_unknown` rather than a non-detection, while a covered neighbour
  holding NO modes at all is unambiguous and still charges. Unknowns fail closed with their own
  reasons. **Ratified consequence:** a workspace that
  persists no `effective_exposure` charges no absence anywhere. That is the correct direction — a
  glance that saw nothing is not a long count that saw nothing — and its blast radius is recorded
  here before real data meets it.
  *`structural_endpoint` is a proposed, priced reading of the linkage, never an energy-only state.*
  It requires a ≥2-node linked track whose final edge DESCENDS centre-ward into a contiguous,
  covered, elastic-bearing run of consecutive arenas reaching a declared fold
  (`ENDPOINT_OMEGA_TOL_FACTOR` is deleted with the predicate it served). The old |ω| tolerance
  handed the amnesty to an isolated feature or a flat low-lying optic branch, about which the
  declared gapless physics says nothing. Elastic strictly, not `blocking_present`: a quasielastic
  feature is no response for a branch to terminate into.
  *Linkage truncation is unresolved, not narrowed.* A capped assignment records the dropped PRIOR
  mass over the PRE-cap enumerated count and marks the cluster `truncation_unbounded` — linkage
  variants share a base `Σ lnZ`, so no base ordering bounds the dropped readings' factor terms.
  **(b) The scene store: content-addressed blobs, and lineage in an append-only log.**
  `<workspace>/report/scene/` holds one `<digest>.json` per revision, a `current.json` pointer
  (`isar.scene-pointer/1`) carrying `digest`, `parent` and `written_at`, an append-only
  `history.jsonl` (`digest`, `parent`, `written_at`, `source`), and a `last_attempt.json`
  diagnostic. **Lineage and time live outside the blob**: a revision must record what it succeeded
  AND an unchanged workspace must rebuild to the SAME digest, and those are compatible only if
  `parent`/`written_at` sit outside the digested payload — inside, every rerun mints a blob and
  "did anything actually change?" stops being answerable by comparing hashes. The DURABLE lineage
  is the log, not the pointer: `current.json` carries the latest link only, so A→B→C leaves nothing
  on disk that remembers B→A, and one appended line per flip keeps every link (nothing is ever
  deleted). An unchanged rebuild appends nothing, because no flip happened.
  Blob first, pointer second, and the pointer
  is convenience rather than authority: a crash between them leaves a valid old pointer beside an
  orphan blob. **Existence is not integrity**: an existing blob is compared BYTE FOR BYTE with the
  revision and rewritten when it differs, before the unchanged test and before any pointer flip —
  otherwise a workspace whose blob had been lost or silently corrupted would short-circuit on the
  pointer and report a cheerful all-clear while every read went on raising, and a corrupt orphan
  would have the pointer flipped onto it. `unchanged=True` therefore means the content really is on
  disk. Corruption raises on read, and on
  write when the existing pointer is unreadable — on read that is a pointer naming a missing blob,
  unparseable JSON, a payload that does not re-digest to its own name, or a pointer that breaks its
  own contract: the `schema` must be exactly `isar.scene-pointer/1` (a foreign or future one
  refuses by name rather than being read as a `/1`), `digest` and any `parent` must be sha256
  hexdigests (which is also what stops a pointer naming a path instead of a blob), and `written_at`
  must be a string — all validated before any field is used, on the read and the write path alike.
  Absence is quiet, because a workspace with no scene is ordinary. Nothing here ever
  deletes a blob.
  **Two writers, no lock.** `isar report --scene` can run against a live campaign, so single-writer
  is false and concurrency is OPTIMISTIC: each writer captures the pointer digest when it snapshots
  its inputs and passes it back as `expected_parent`; the flip refuses with `ConcurrentSceneWrite`
  (a `ValueError` subclass, so the driver hook's skip and the CLI's exit 3 apply unchanged) when
  the pointer moved meanwhile, and the losing writer's blob is still written and addressable. A
  lock file is rejected: a stale lock left by a crash would block every subsequent campaign, which
  is worse than the race it prevents. Strict JSON throughout: `canonical_bytes` refuses non-finite values
  (`allow_nan=False`), so a revision that digests cleanly can always be re-read — a non-finite lnZ
  therefore persists as `null`, matching the builder's own treatment of it as unsummable.
  **The truncation bound is recorded in LOG space** (`log_discarded_mass_bound`), replacing the
  linear `discarded_mass_bound` outright with no dual field (H1). The linear form overflowed to
  `inf` for a wide factor span over a large product, and `inf` is neither JSON nor informative: a
  vacuous bound must never read as a bounded approximation, and downgrading it to "absent" hid a
  truncation that did have a bound. The log-space value is finite for every finite input, so
  `discarded_mass_unbounded` is deleted with the overflow arm it existed for, and `null` now means
  exactly one of two things, told apart by `truncated`: FALSE, nothing was dropped (the bound would
  be −inf, which strict JSON cannot carry); TRUE, nothing bounds what was dropped — and then
  `truncation_unbounded`, the single remaining unbounded lane, is True as well.
  **(c) The read surface, and what a hypothesis map may claim.** `isar.workspace_api.scene_view` is
  the scene's OUTPUT door, deliberately a separate module from the input adapter — which stays
  banned from importing the store at all, so an already-stored revision can never contaminate a
  builder's inputs. `ws.scene` (the property a reader uses) delegates to `scene_revision(ws)`,
  which returns the pointer metadata plus the payload verbatim, with no typed re-modelling of it
  (H12(e)); absence reads as `None` and corruption propagates rather than degrading into "no
  scene", because the two mean opposite things to an
  operator. The reader GATES what it interprets: the payload's `schema` must be exactly
  `isar.scene-revision/1` and the keys it projects (`observations`, `clusters`, `footprints`,
  `non_scoring`) must be present and be lists, so a `{}` or a future revision refuses by name
  instead of reading as an empty scene — "the scene claims nothing here" and "this reader cannot
  read the scene" are opposite statements. The view holds the CANONICAL BYTES and decodes a fresh
  payload per access, like `SceneRevision` itself: a shared dict is mutable shared state, where one
  reader's edit becomes another's belief and the content stops matching `digest`.
  **A revision is about the rows it was built from, and the facade checks that it still is.** Every
  read recomputes the live rows' observation fingerprints through the builder's own adapter and
  compares them with the ones the revision embedded; any difference — a re-fitted row, an arena the
  revision never saw, one it saw that is gone, or a workspace the adapter now refuses outright —
  sets `stale` on the view and the map. Unconfirmable is not confirmed. Without it a scene frozen
  three deliveries ago renders as the current belief over current data, which is exactly what a
  hook that can never fail a delivery makes possible; `last_attempt` on the view then says why it
  froze. `hypothesis_map(ws)` is a projection for a renderer and re-derives no belief: posterior
  mass comes from the recorded cluster marginals, visibility from the recorded reports, coverage
  from the recorded footprints. It carries THREE axes and deliberately does not unify them —
  markers and gap bounds are intrinsic `|ω|`, which is what the revision records, while a curve's
  `x` is the SIGNED energy window the delivered model was fitted on; folding is the renderer's ±ω
  overlay, and doing it here would either discard the curve's anti-Stokes side or invent a sign for
  a mode that has none. A curve is joined to an entry only when every source claim holds, and is
  WITHHELD with a recorded reason otherwise: `cluster_unresolved` (see below), no delivered row,
  `source_mismatch` (the live row no longer fingerprints as the observation the revision scored),
  the leading alternative naming a candidate the row did not deliver (no stored amplitudes for it,
  and drawing the delivered model under the winner's name would attribute one hypothesis's
  intensity to another), an unplaceable window, an evaluator refusal, or `no_pure_model` — the
  evaluator's reconstruct-from-counts fallback re-solves amplitudes, which is fitting, and a read
  surface never fits (hard rule 7). Only the pure path renders.
  **Cluster authority travels with every entry.** An entry carries `cluster_authority` ∈
  `exact` / `bounded` / `unresolved`, derived from the cluster record (unresolved on
  `truncation_unbounded` or `linkage_truncated`; bounded when a finite `log_discarded_mass_bound`
  records dropped mass; exact otherwise), plus that bound when it is finite. An UNRESOLVED
  cluster's marginals are conditional masses over the RETAINED set, not posteriors over the
  hypothesis space: its curve is withheld and its markers are flagged
  `conditional_on_retained_set`. The numbers are kept — they are what the scene computed — and
  labelled for what they are, rather than projecting as an ordinary winner beside a fully
  enumerated one. The per-cluster classification payload key is the generic
  `classification: {factor: {component: state}}` (ratified over a factor-specific
  `lattice_classification`), so a second classifying factor needs no schema change.
  **(d) Invocation: observe-only at every delivery, explicit offline.** One function
  (`isar.session.scene_hook.build_and_store`) serves both callers, so a scene built by the campaign
  and one built offline from the same ledger cannot be different revisions. The driver runs it
  LAST, after `results.csv` and `card.state` are on disk, inside its own
  `timer.stage("scene_observe")` — a cost `isar profile` cannot see is a cost nobody can budget. It
  NEVER raises: a scene that cannot be built is a missing explanation, never a failed delivery, and
  the traces are a summary context line, the log, and `report/scene/last_attempt.json` — the hook
  records every attempt, `{"status": "ok", "digest"}` or `{"status": "failed", "reason"}`, because
  a hook that cannot fail a delivery otherwise leaves a scene that STOPPED building looking exactly
  like one with nothing to rebuild (the pointer simply stays put). The sidecar is a diagnostic and
  never authority: it decides nothing, its own write failure is logged and dropped, and an
  unreadable one reads as absent rather than refusing to show a perfectly good revision. Adapter refusals are logged as one WARNING
  line — a workspace that refuses once refuses on EVERY delivery, and a traceback per scan would
  bury the log for a condition whose message already says to re-deliver — while an unexpected
  failure logs the full traceback. Measured cost per delivery: **7.4 s at 50 arenas, 22.7 s at
  100**; acceptable while the scene only explains, and the per-delivery cadence is to be revisited
  at SCENE-4, when the scene starts steering. `isar report --scene` is a FLAG on the existing
  command, not a subcommand: the report is written first and its exit semantics are unchanged, and
  only the explicitly requested scene step can exit **3** — distinct from 2 ("no workspace here")
  so a script can tell a workspace that cannot be scored from one that is not there. Asked for by
  name, a refusal must be visible rather than an empty scene reading as "this campaign measured
  nothing". Named limitation: `catalogue_version` is stamped from the catalogue's `DEFAULT_PROFILE`
  at the hook, because no per-fit profile is persisted; a campaign fitted under another profile
  would stamp the wrong one, and the fix is to persist the profile with the fit.
  **(e) The four branch-tracker contracts are ported as scene SPECIFICATIONS, never as oracle
  equality.** The tracker earned four rules the hard way on real campaigns — an inferred endpoint
  claims no measured precision, absence is evidence only after a relevant search, a resolution
  blockage is not repaired by counting (while a detection-power blockage is exactly what counting
  repairs), and the zone centre is the declared period anchored at `q = 0` (O44). They are rules
  about physics, so the scene layer honours them by a completely different route: no target, no
  anchor, no selected candidate, no per-point track state. `tests/scene/test_scene_contracts.py`
  cites the tracker source lines each test ports, states the rule, and then checks the scene's OWN
  vocabulary — visibility states, presence terms, marginals. A field-for-field oracle would make
  the scene a second implementation of the thing SCENE exists to replace. The tracker is untouched
  and keeps driving delivery until SCENE-6.
  **Supersedes/relates:** amends H13(c) (which factors read a neighbour report); implements the
  store and read surface H11(a)/H12(e) require; rests on H12(c) (the bound honesty the two
  unbounded flags spell out) and H1 (loud refusal and no compatibility reader for a workspace whose
  rows predate the delivery contract); ports O44's origin rule as a scene specification.

- **H15.** (2026-08-13, In force; SCENE-4 PR 4.0) **A declared peak count RELAXES the
  ``_offer_multimode`` diagnostic, it never bypasses it — H8(a) implemented.** `loop.py`'s
  `_offer_multimode` (`isar/analyze/loop.py`) gains a `declared_count` input; with a declaration
  ≥ 2 it evaluates the SAME two triggers (cost/dof, Wald–Wolfowitz runs-test residual) at named
  relaxed thresholds — `GATE_COST_DECLARED` (1.3, < the global `GATE_COST` 2.0) and
  `RUNS_ALPHA_DECLARED` (1.5, < the global `RUNS_Z` 2.0 — a smaller runs-test z-critical is a
  more permissive alpha), both provisional v1, SCENE-7-calibrated. Natural abstention holds: a
  declared scan clean under BOTH relaxed diagnostics is still not offered. The `or expected_two`
  disjunct that unconditionally forced the 2-mode offer is removed at every call site —
  `fit_shadow_candidates`, the production two-stage `_enumerate_and_fit`, and the structure-hint
  shortcut gate `_try_structure_hint` (reached from `fit_structures_two_stage_hinted`) — because
  the diagnostic is one function and a declared scan's offer must not depend on whether a
  structure hint happened to be in scope; a declared hinted scan with between-threshold structure
  rejects the shortcut and receives the full two-mode offer. **Route vocabulary:**
  `multimode_route="guideline_forced"` is retired for `"declaration_relaxed"`, recorded ONLY when
  the relaxed thresholds (not the global ones) made the offer — re-derived from the gate outcome
  via a new `route_out` provenance channel threaded through `_enumerate_and_fit` /
  `fit_structures_two_stage` / `fit_structures_two_stage_hinted`, never guessed from `peaks`
  presence. The production writer (`isar/session/scan_delivery.py::fit_one`) and the aliased
  shadow-selection bundle (`loop.shadow_bundle_from_complete_results`) both read this same
  provenance rather than recomputing it from `peaks`. The eligibility science flag
  `selection_guideline_forced` is renamed `selection_declaration_relaxed`
  (`isar/eig/eligibility.py`) with no vocabulary-index shift (straight rename, not a split — no
  `RETIRED, never emitted` placeholder was needed, since nothing depended on the old string's
  continued presence at its rank). `isar/synth/stat2b/campaign.py`'s legacy/standard route
  builders were checked and left unchanged: neither passes a declared count into the fit, so
  their `residual_triggered`/`not_offered` derivation is already correct as the identity case of
  the same rule.

- **H16.** (2026-08-13, In force; SCENE-4 PR 4.0) **Scene authority readiness — the posterior is
  made steering-trustworthy before SCENE-4 lets it influence acquisition.** Five rulings, one per
  external-review finding held to gate the authority flip (issues #21/#22/#25/#26/#28):
  **(a) Counts are authoritative; fits are disposable (#26).** A new pre-fit artifact,
  `isar.measurement-arena/1` (one JSON line per measured arena, `report/measurements.jsonl`,
  written by `isar/session/scan_delivery.py` at QC-complete and appended by the driver's
  `checkpoint_entry` seam), records identity, sample/conditions provenance, q, lattice epoch,
  kept energy cells, exposure, and `count_trust` BEFORE any fit attempt — so a fit crash can no
  longer erase the fact that a measurement happened. Three honest lanes: trusted+fitted scores;
  trusted+fitless is experiment-map-only (`no_interpretation`, footprint kept); count-untrusted
  is recorded-as-attempted with NO coverage credit. A DELIVERED per-scan fit whose candidate
  cell is absent or malformed is a corrupt interpretation artifact and REFUSES the build with a
  re-deliver instruction — never demoted to the fitless lane (a candidateless pooled master
  keeps the tolerated SCENE-3 supersedes-nothing contract; master records are a named future
  work package). A workspace with delivered candidates and no measurements log is refused (H1,
  the energy_bands precedent).
  **(b) Transported conditioning cannot double-count (#21, interim Variant A).** Scene scoring
  eligibility keys on the arena AGGREGATE constraint origin over every usable candidate:
  `none` / `user_declared` / `prespecified_scientific` / `catalogue_variant` score;
  `prior_predictive` / `data_dependent_rescue` / `post_selection` / `mixed` are non-scoring
  `transported_conditioning` (experiment-map-only); the tri-state `None` (pre-H9 workspace, no
  semantics record) and any unrecognized origin string refuse the build. Distinct
  individually-safe origins across candidates do NOT reduce to `mixed` — that reduction is a
  single-fit concept. The full transport-DAG contract stays deferred; this lane is the honest
  fail-closed interim.
  **(c) The scene partitions by physical scope (#25/#28).** Topology and clusters partition by
  `(sample identity, ConditionSet, path identity)`: `ConditionSet` is typed (named dimensions,
  canonical units; temperature binned for partition comparison by the card's declared collation
  `t_round`, binning provenance recorded, raw values in fingerprints; a declared temperature
  with no binning policy fails closed per-arena); the lattice-epoch token joins the identity and
  incompatible epochs never couple; `topology_role` (`canonical_path`/`context_probe`/None)
  enters the vocabulary now with None reproducing SCENE-3 collinearity exactly — a
  `context_probe` arena is withheld from path derivation entirely, so one off-path diagnostic
  scan can no longer collapse the whole topology (the role PRODUCER lands with the PR 4.3 driver
  wiring, when steered campaigns first take context scans). Cross-partition: no edges, no
  linkage, no presence charges, no endpoint readings.
  **(d) A refiner that moves belief declares how much (#22).** `refine_visibility`-bearing
  factors declare `rescue_bound_per_report`; `_score_span` widens by that bound × a conservative
  pre-pruning cardinality (`_max_refinable_reports` — richest usable candidate × path-adjacent
  same-partition neighbours, whole build), so margin pruning stays "bounded, never optimistic"
  (H12(c)) under multi-report endpoint rescues; a refiner with no declared bound disables margin
  pruning outright (the existing unbounded lane). A new `transformation_ablation` ledger —
  distinct from H12(f) score-term ablation — rescores the fixed alternatives with each refiner
  off (visibility recomputed, consuming score factors rerun, renormalized), so the endpoint
  factor's influence is no longer absorbed into presence's ablation entry.
  **(e)** The expected_two relaxation landed in the same slice is **H15**.

- **H17.** (2026-08-14, In force; SCENE-4 PR 4.1) **The question + agenda core — three
  objects, semantic identity, authority-aware predicates, a fully frozen step contract, and a
  verdict with no beam directive.**
  **(a) Three objects, not one.** `QuestionSpec` (immutable identity + declarative versioned
  predicate + dependencies + generator-ID references), `QuestionAssessment` (one scene
  revision's answer state), `AgendaRevision` (content-addressed snapshot, scene-store
  pattern). `question_id = fingerprint({family, claim kind, subject})` and NOTHING else —
  thresholds, policy provenance, program fingerprint and scene content stay out, so re-freezes
  and new scans never re-key a question; subjects are built from frozen declarations and
  partition identity, never cluster indices, component IDs, or scan refs; external references
  are `(agenda_digest, question_id)`. Program-scoped subjects fan out per compatible
  partition; nothing pools probability across physical partitions.
  **(b) Snapshot and history are different objects.** The digested payload carries only
  `open`/`answered`/`unresolvable` — each a pure function of the derivation inputs (scene
  digest, frozen step, policy, manifest, exhaustion evidence). `overtaken`/`dissolved`/
  `reopened` are relations BETWEEN agendas: typed transition records on
  `report/agenda/transitions.jsonl`, outside the digest, written at flip time. A lattice-epoch
  change dissolves with `equivalence_unproven` — no automatic re-anchoring without the
  deferred transform contract (DESIGN_GOALS: carry the token, never build
  transform-and-reuse). `derive_agenda` is pure and parent-independent; there is no metadata
  short-circuit — the store's byte comparison reports `unchanged`.
  **(c) Predicates answer only from variables the scene represents.** Authority lanes
  `exact`/`bounded`/`unresolved` from persisted truncation flags (lost bounds and capped
  linkage fail closed); log-space adversarial omitted-mass intervals (a huge finite bound is
  honestly vacuous, and a vacuous bound answers nothing); cross-cluster claims multiply as
  independent factors. Structural presence needs no identity semantics; ClassifyIdentity
  answers only from explicit identity alternatives (`identity=None` is `unclassified_mass`,
  never a physical unknown); TestModelMismatch needs an explicit unknown-model competitor;
  **EstimateParameter never closes in v1** (owner ruling 2026-08-14, superseding the same-day
  mixture-spread-plus-floor ruling after the between-vs-within variance counterexample: a
  floor is a lower bound and cannot certify above itself; the spread is a diagnostic,
  `within_candidate_uncertainty_unavailable` the blocker, per-candidate interval persistence a
  scheduled 4.2/4.3 re-baseline — with the stated consequence that `complete` is unreachable
  for phenomenon-bearing campaigns until it lands); the ZC endpoint is a bounded compatibility
  assessment under declared slope support with `maximum_justifiable_zc_confidence = 0.0` in
  v1; VerifyCompleteness is frame-relative and never reads the leader-only visibility rows.
  Blockers are typed (durable / transient / model-authority); only durable can feed
  unresolvable closure, which additionally requires a `QuestionExhaustion` proof fresh on
  every comparand (`basis_agenda_digest` = the same-pass exhaustion-free derivation digest,
  scene digest, registry fingerprint) — stale proofs are rejected naming the failing
  comparand, and exhaustion without a durable blocker is a `generator_defect`, never a
  closure.
  **(d) Policy is frozen once, with three authority postures.** `isar.question-policy/1`
  (`isar/schema/question_policy.py`): target thresholds (user ask > card guidelines > named
  defaults; the card `[questions]` lane ships wired but empty by owner ruling),
  identity-bearing derivation constants (`emergent_omega_tile_meV`, frozen for the program's
  life — changing one is a program revision, so a threshold re-freeze can never silently
  re-ID emergent questions), and capability floors/ceilings a user ask may strengthen but
  never weaken (refused, not clamped). Every value is a named provisional v1 declaration,
  SCENE-7-calibrated. The discover step is `isar.campaign-step.discover_and_map/2`: the
  reader requires EVERY effective field explicitly (no read-time defaulting), `/1` is retired
  by name through a retired→replacement mapping, and unsupported staleness modes or emergent
  policies refuse rather than being accepted and ignored.
  **(e) The verdict carries no beam directive.** `evaluate_agenda` returns `complete` /
  `in_progress` / `incomplete_over_budget` only (four-reasons law; completion ≠ beam-idle):
  completion requires every spec closed AND VerifyCompleteness answering yes; a missing
  completeness question is a derivation defect, never implicit completion; unresolvable
  closures never block completion and surface as consultation-request records (the channel is
  4.3); `complete` beats budget on the same delivery. The tracker's targeted `evaluate_stop`
  is untouched beside it until SCENE-6. Offline by design: no driver wiring (4.3), no
  generator bindings or exhaustion producers (4.2) — no live derivation can close unresolvable
  in this slice, asserted by test.
  **(f) The PR #31 review response (2026-08-14, request-changes; sixteen findings, all fixed,
  none refuted).** The defect class was unintended completion-unreachability and misidentified
  questions, distinct from the deliberate (c) openness. (1) Presence went CLASS-AGNOSTIC
  (owner's door 2): `structural_presence` ignores identity, so per-class copies at one anchor
  were one question wearing N labels — one presence per anchor; ClassifyIdentity's ANSWER
  carries the class; ConnectTrack chains per classify-answered class; the unused
  `catalogue_classes` parameter deleted (4.2's opening book reintroduces class vocabulary
  with genuinely distinct declared findability). (2) Emergent subjects derive per (q tile,
  ω tile) on BOTH frozen quanta — `emergent_omega_tile_meV` is finally identity-bearing —
  with per-tile evidence combined ACROSS clusters before thresholding (two 0.4 uppers jointly
  open at 0.64). (3) TestModelMismatch is a CONDITIONAL opener (explicit unknown competitor
  present AND frozen `include_unknown` true, else inert with a note): its absence-blocker is
  model-authority and unclosable, so the unconditional question made ordinary campaigns
  impossible to complete — a fully surveyed blank campaign now reaches `complete`, asserted
  by test. (4) Exhaustion proofs must declare `required_generator_ids` EXACTLY equal to the
  question's frozen generator set — a proof may not choose its own requirements. (5)
  `evaluate_agenda` validates its payload: duplicate/orphan rows refuse, unassessed specs
  fail closed as a derivation defect; duplicate question IDs refuse derivation itself. (6)
  Non-axis discovery paths REFUSE loudly (scalar projection cannot represent them; 4.2+
  representation named) and the full endpoints join the subject frame, so parallel paths are
  distinct subjects. (7) Zone anchors are epoch-scoped per partition (mirroring H16's
  epoch-scoped knowledge factors; H13 symmetric for undeclared). (8) The agenda snapshot
  embeds the complete `resolved_policy` block, preserving per-field provenance in storage.
  (9) Tile membership is half-open where a tile is a grid cell, so a boundary scan belongs to
  exactly one endpoint tile; `partition_key`/`partition_id` moved to `isar.scene.model` as
  the one implementation, and `completeness_relative` scopes non-scoring arenas to their
  EXACT partition; the discover compiler defaults `include_unknown` only when absent/null so
  invalid explicit values reach the schema's refusal.

- **H18.** (2026-08-14, In force; SCENE-4 PR 4.2a scheduling + PR #32 review wave) **The
  EstimateParameter re-baseline is scheduled and lands; the question-policy schema grows a
  version; the identity gate stays representability-only; the milestone splits; and the PR #32
  review wave repairs interval authority, connectivity, and parameter-precision closure.**
  Seven parts, the owner rulings of 2026-08-14.
  **(a) The per-candidate interval re-baseline is scheduled into 4.2a as its FIRST commit.**
  H17(c)'s same-day "EstimateParameter never closes in v1" ruling named per-candidate interval
  persistence as a scheduled 4.2/4.3 re-baseline with no date; the owner pulls it forward to
  open 4.2a rather than trail it, so the family's completion gap closes before any other
  4.2a work lands on top of it.
  **(b) `isar.question-policy/2` retires `/1` by name.** Two new TARGET fields —
  `survey_order` (`"placement_informed"` default, `"uniform"` the ruled alternative) and
  `parameter_precision_min_mass` (a posterior-support threshold in `(0, 1]`) — join the
  resolved-policy block; a persisted `/1` block is refused with a message naming `/2` and
  instructing recompile, never silently read. The same commit repairs
  `DEFAULT_CATALOGUE_PROFILE` to the real profile ID
  (`isar.analyze.catalogue.DEFAULT_PROFILE`) — the string it carried before,
  `isar.catalogue.inelastic-default/1`, named no profile that ever existed.
  **(c) The identity gate stays gated on representability only.** SCENE-4.2a A6 stands: a
  challenge run with no identity-conditioned priors completes classification-free, mapping
  every phenomenon as real-but-unclassified rather than opening a question the scene can never
  answer. Whether and how a future profile's identity-conditioned PRIOR payloads should shape
  the gate is an explicit open owner item, not decided here.
  **(d) The milestone splits: 4.2a and 4.2b.** 4.2a is the interval re-baseline plus the
  question-policy and catalogue repairs above and their immediate consequences; scope that
  depends on a completed re-baseline (further predicate refinement, additional families) waits
  for 4.2b. The split is scheduling, not a capability-status claim.
  **(e) [PR #32 review wave] Interval authority is fit-conditional; delivery selection is not
  interval authority.** Every candidate row — winner or not — projects only its own
  candidate-tier `freq_interval` record as a producer-soundness verdict:
  `authority_basis == "producer_sound"` iff the record is available, single-model bootstrap
  (`parametric_bootstrap_percentile` under `frequentist_sampling_distribution`), not bimodal,
  and converged at or above the named provisional v1 floor. The Akaike-pooled model-mixture kind
  can never satisfy the single-model requirement, so mixture intervals are structurally excluded
  from candidate slots. A non-winner survivor with a sound record is trusted exactly like a
  winner with one; a winner with an unsound record is not trusted for having won.
  **(f) [PR #32 review wave] Identity abstention must not suppress connectivity.** Beside the
  per-class ConnectTrack, the agenda now derives one CLASS-AGNOSTIC track per partition with
  ≥2 confirmed presence tiles wherever no per-class track has actually derived there —
  suppression keys on derivation, not on the partition merely containing an answered
  classification elsewhere. The class-agnostic track dissolves with `equivalence_unproven` once
  a per-class track derives; the label is ClassifyIdentity's answer, never a precondition for
  the connectivity claim to exist.
  **(g) [PR #32 review wave] Parameter-precision closure clears on retained mass, and the
  published center is honest.** `parameter_within_tolerance/2` closes when the qualifying
  posterior support reaches ≥ `parameter_precision_min_mass`, consistent with `decide()` and
  the rest of the predicate vocabulary. The answer's `center_meV` is the midpoint of
  `envelope_meV` — the supported-center interval, the intersection of every contributing
  acceptance window at the chosen center — never a re-derived tolerance witness that moves when
  only the requested tolerance changes; every contributing interval must additionally agree on
  the exact `(interval_kind, inference_paradigm, nominal_level)` triple, and conditioning
  fingerprints ride the answer as provenance only, never as a compatibility gate.
  **Supersedes/relates:** (e) and (g) together supersede H17(c)'s "EstimateParameter
  never closes in v1" ruling — the per-candidate interval persistence H17(c) named as a future
  re-baseline has landed (scheduled by (a)), delivery-realized authority is replaced by
  fit-conditional producer-soundness, and closure now clears on retained mass exactly as the
  rest of the family does; refines H17(f)(1)'s class-agnostic presence framing by extending
  CLASS-AGNOSTIC treatment from ConfirmPresence to ConnectTrack.

- **H19.** (2026-08-15, In force; SCENE-4 PR 4.2b, branch SCENE-4c2) **The discovery generator
  layer runs end to end, offline: seven generators, a five-outcome planner, and the stores a
  decision needs to prove itself later.** Twelve parts — (i)-(l) added 2026-08-15 by the same
  day's PR #33 response wave (two blocking external reviews on this PR, same-PR-wave amendment
  per the H17(f) precedent: a review response on the SAME PR a decision already covers folds
  into that decision's own lettered parts, never a new H-number).
  **(a) Seven generators, one newly bound to VerifyCompleteness.** `exposure_fill` joins the six
  question-bound generators (`opening_book_survey`, `presence_probe`, `identity_discriminator`,
  `track_bridge_scan`, `parameter_refine`, `mismatch_probe`) because the opening book's
  `underexposed_cell` proposals serve VerifyCompleteness's exposure floor and only that closure:
  exhaustion is proved against `spec.generator_ids` exactly, so a family with no generator can
  never honestly exhaust, and VerifyCompleteness's generator-less state (H17) ends here.
  Unresolvable closure stays gated on a durable blocker regardless — the new generator opens no
  shortcut around that requirement.
  **(b) ConnectTrack's class-agnostic trigger changes, superseding H18(f).** Existence, not
  span: the class-agnostic track exists wherever ANY confirmed tile in a partition lacks an
  answered classification — not only where no per-class track has derived, H18(f)'s trigger —
  and spans every confirmed tile in the partition, suppressed only when the partition is fully
  classified. Both directions are ruled: new opens in previously-suppressed partitions, and
  dissolution in a fully-classified MULTI-class partition (two confirmed tiles in two distinct
  classes has no per-class track possible, and under the new rule carries no class-agnostic
  track either) — cross-class connectivity stays deferred physics, never folded into the
  unclassified lane.
  **(c) Cold start reaches a fifth planner outcome, never a fault.** The planner's outcome enum
  is `{selected, agenda_complete, controller_fault, no_action_with_honest_exhaustion,
  no_eligible_action}` — `no_eligible_action` is added at the enum's birth because the
  four-member enum approved earlier had no honest bucket for an unmeasured workspace: its
  program-scoped DiscoverRegion question is INAPPLICABLE (`program_scoped_subject_unfanned`) at
  the generator wrapper, before the book is ever called, and nothing is claimed exhausted.
  First-scan reality — creating the first partitioned evidence — stays 4.3's partition-producer
  obligation, not this milestone's.
  **(d) Discovery inventories live under `report/inventory/discovery/`, never the legacy
  top-level directory.** `tools/eig_replay/replay_o41_test4.py` globs `report/inventory/*.json`
  non-recursively with no kind check, so a same-directory discovery artifact would be silently
  ingested as legacy vocabulary; the subdirectory keeps it structurally invisible, and its
  `inventory_ref` carries the new kind `discovery_action_inventory`, refused by name where the
  legacy kind is expected (the eligibility census's kind check). `isar.action-inventory/1`
  becomes `/2`: the generator registry an inventory was built under is now INSTANCE data
  (`generator_registry_fingerprint`/`generator_registry_manifest` on the inventory itself),
  never the module-global, so a legacy inventory keeps naming the legacy registry and a
  discovery one the discovery registry.
  **(e) `report/decisions.jsonl`, one line per planning call, clock-free.** Schema
  `isar.discovery-decision/1`: `decision_basis` embeds the scene/agenda/basis digests, the
  program fingerprint and step id, the resolved-policy fingerprint, the generator registry
  {fingerprint, manifest}, the scheduler-policy declaration, and the placement {fingerprint,
  manifest} — every manifest EMBEDDED, not referenced, the same precedent H17(f)(8) set for the
  agenda snapshot's `resolved_policy` block. `scan_design_provenance` carries EXACTLY the five
  keys `isar/eig/eligibility.py`'s `_scan_design_identity` reads (`inventory_fingerprint`,
  `context_fingerprint`, `selected_candidate_id`, `status`, `recovery_state`);
  `scan_design_events` rides BESIDE it as a sibling field, the consumer contract's own name
  (`EligibilityInput.scan_design_events`), so the provenance object stays exactly those five
  fingerprinted keys. No wall-clock anywhere in the record — the whole point is that replaying
  the same scene and program twice appends a byte-identical second line.
  **(f) `report/program/` is the workspace's first persisted program document.** A
  content-addressed frozen-program store (pointer + blobs, planner-written, idempotent on the
  same program) — the same store discipline the scene and agenda stores already keep. It exists
  because DecisionBasis and `ws.agenda`'s freshness verdict both need a recoverable program
  source, and there was no persisted one until now; the 4.3 driver and any consultation-channel
  re-freeze write through the same store, never a second one. `program_fingerprint()` is the ONE
  implementation of a program's content address (`isar/schema/campaign_program.py`), and its
  equality with the store's own digest rule is pinned by test, never assumed.
  **(g) `DEFAULTS_PROFILE_VERSION` stays 1.** It names the defaults VALUE SET; 4.2b adds no
  question-policy field and changes no default — the scheduler's declaration lives in
  DecisionBasis, not the policy block, and the ConnectTrack trigger change is versioned by
  `OPENER_VERSION`, not this one. The flag PR #32 carried into this milestone dissolves here;
  whether a retroactive profile `/2` is worth minting for its own sake stays the owner's open
  call, not decided by this ruling.
  **(h) The partition-None guard's first-commit interpretation.** `propose_survey` raises on a
  program-scoped subject at its own front door (C0) — a contract violation trap, not a
  lattice-agnostic survey. The discovery generator WRAPPERS (`opening_book_survey`,
  `exposure_fill`) check `subject.partition_id` themselves before ever calling the book,
  inheriting that guard by construction: the book's raise stays reachable only as a genuine
  upstream contract violation, and the wrapper's own INAPPLICABLE reading is what (c)'s
  cold-start outcome is built from.
  **(i) [PR #33 response wave] Physical action identity is ONE class, not the generator's ID.**
  `action_class = "discovery_scan"` for every discovery generator, never the generator id — a
  generator id in `action_class` forked one physical scan into as many scientific actions as
  generators that happened to want it, and blocked dedup across generators; which generator
  asked stays fully recorded on `ActionEvaluation.generator` and every
  `QuestionActionAssociation`, never on the intent. `campaign_slice_fingerprint` carries the
  subject's PARTITION token on every discovery intent (`None` only for the program-scoped
  subjects no shipped generator actually reaches), so identical (q, ω) in two DIFFERENT
  partitions never dedup into one action. The `window_center_meV` union rule: `None` yields to
  a declared value from either side; two DIFFERENT declared values still refuse as a controller
  fault. Neither the generator-id `action_class` framing nor a `None`
  `campaign_slice_fingerprint` on discovery intents appears anywhere in (a)-(h) above — this
  closes a code defect the PR #33 review caught (A-P0-1), not a wording correction to an
  earlier letter.
  **(j) [PR #33 response wave] Completion is the agenda verdict evaluator's call; honest
  exhaustion requires an APPLIED closure, never a merely emitted proof.** The planner's outcome
  is decided from `isar.scene.agenda.evaluate_agenda`'s own verdict on the second
  (exhaustion-informed) derivation pass — never re-inferred from raw open/closed statuses, since
  a question can leave `open` by closing `unresolvable` rather than answering yes (the
  four-reasons law). `no_action_with_honest_exhaustion` requires every remaining question to
  have closed via an APPLIED exhaustion proof; a proof `apply_exhaustion` REJECTS for want of a
  durable blocker (`generator_defect`, the question stays `open`) can never reach it — the
  honest reading of that state is `no_eligible_action`. `apply_exhaustion` requires a durable
  blocker on the assessment for ANY closure, including an all-clean-zero proof, and no
  4.1/4.2 evaluator emits one yet, so `no_action_with_honest_exhaustion` is reachable in 4.2b
  only through the authenticated `blocker_evidence` transition `derive_agenda` now accepts
  (validated against fresh comparands and the target assessment's `generator_ids`, attached
  AFTER the basis digest is computed and BEFORE `apply_exhaustion` runs, with both derivation
  passes receiving IDENTICAL evidence so no proof self-rejects on `basis_agenda_digest`) —
  reachability through a live durable-blocker producer stays 4.3+ territory. Neither (c) (the
  cold-start fifth-outcome ruling) nor (e) (the decisions.jsonl schema) states or implies that
  an EMITTED proof alone is sufficient for honest exhaustion — this closes a code and test
  defect the PR #33 review caught (A-P0-2/A-P0-3), not a wording correction to either letter.
  **(k) [PR #33 response wave] Publication guards: one race closed, one narrowed, one converted from
  silent to loud. The interprocess store lock CLOSES the dual-writer pointer race; the
  publication-time scene re-check NARROWS the stale-scene window; the program-store CAS
  converts last-writer-wins into a loud refusal.** A per-store interprocess lock (`os.open(..., O_CREAT|O_EXCL)`, bounded
  retry, DEAD-OWNER-ONLY stale-lock takeover — age alone is never sufficient; a live writer
  paused past the threshold must never be evicted) wraps every store's
  pointer-read/compare/flip/history-append as one critical section, so two racing writers can
  no longer both pass their own `expected_parent` check off one stale read. The planner
  separately re-reads the SCENE pointer immediately before the agenda write and refuses
  (`StaleSceneAtPublication`, nothing published) if it moved since the decision's own snapshot —
  this NARROWS the window rather than closing it (the re-read sits outside the agenda store's
  own lock, against a different store than the one being written); the agenda store's
  `expected_parent` CAS and lock are what actually refuse a lineage fork. The program store is
  now written with `expected_parent` from a snapshot taken at the same planning-start moment as
  the agenda snapshot, so a concurrent program writer surfaces `ConcurrentRevisionWrite` instead
  of last-writer-wins.
  **(l) [PR #33 response wave] DecisionBasis is complete: locality is visible AND fingerprinted,
  and the rank table persists.** `locality_coordinate` can change which action `select()` picks
  (the scheduler's rank vector), so `decision_basis` now carries a `locality` block (`{fn,
  coordinate}`, present even on the scene-missing fault path) AND `context_fingerprint`'s own
  digest input includes it — two decisions differing ONLY in locality now produce different
  records AND different fingerprints, never identical ones on either count. `selection` gains
  the FULL ordered rank table (one row per eligible surviving evaluation, winner first) that the
  scheduler's `select()` was already building and the planner was discarding. `ws.agenda`'s
  freshness verdict grows a fifth comparand, `derivation`: the persisted derivation manifest's
  opener/matcher/predicate-evaluator versions against `workspace_api`'s own pinned literals (the
  same duplication posture the `AGENDA_SCHEMA` pin already sets) — `registry` alone said
  nothing about the OPENER/matcher/evaluator code that produced a stored agenda's specs and
  assessments, so a stale-derivation agenda with a current scene/program/policy/registry
  previously read as `fresh`.
  **Supersedes/relates:** (b) supersedes H18(f)'s "no per-class track has actually derived"
  ConnectTrack trigger with the any-confirmed-tile-unclassified trigger above; the
  track-EXISTENCE rule moves, the class-agnostic-track-exists-when-needed PRINCIPLE H18(f)
  established does not. (i) and (j) supersede no earlier H19 letter's wording — (a)-(h) never
  state a generator-id `action_class`, a `None` discovery `campaign_slice_fingerprint`, or
  emitted-proof sufficiency; those were code and test defects the PR #33 review caught, not
  ledger claims being corrected. (k) and (l) are purely additive: no earlier H19 letter
  addressed the interprocess store lock, the publication-time scene re-check, the program-store
  CAS, or DecisionBasis's locality/rank-table completeness.

- **H20.** (2026-08-18, In force; SCENE-4 PR 4.3, branch `SCENE-4d`) **The discovery controller
  goes live: the campaign parent owns the scene build, partitions become physical, cold start is
  declared knowledge all the way down, and the only two durable blockers are "the machine cannot"
  and "that is outside the region you gave me".** Eight parts, each an owner ruling taken during
  the build (commits `8f89a88` C1, `95c97d8` C2, `334f938` C3, `da8a892` C4, `8bac6c5` C5,
  `81287b7` C6, `a03cc4d` C7, `cd4d1ff` C8).
  **(a) The per-delivery scene build is PARENT-OWNED and runs in a SUPERVISED SUBPROCESS.** The
  analysis child's post-run scene hook is DELETED, not flagged off (H1): the child parses, QCs,
  fits, persists, and stops. The campaign parent invokes exactly one build per delivery through
  `isar scene-build --workspace DIR`, which prints one structured line
  (`isar.scene-build-report/1`: `{digest, pointer_flipped, unchanged}`), and pins the digest it
  returns into `plan_next_action` — the planner loads the revision BY CONTENT ADDRESS
  (`RevisionStore.read_revision`) and never consults `current.json` on the campaign path, so live
  and replay planner inputs are one canonical persisted representation by construction. A
  subprocess rather than the in-process call the plan first proposed, because the drive layer's
  constitutional ban on importing `isar.analyze`/`isar.session` (the Stage II ↔ III file contract)
  vetoes an import; the seam is process-neutral, so a persistent build worker can slot in behind it
  when fixed-lag staleness is built. `isar run` becomes the explicit COMPOSITION of analysis plus
  that one canonical build, with `--no-scene` as the campaign runners' opt-out, so the GUI (which
  shells `isar run` and has no scene call site of its own) keeps byte-identical behaviour through
  one build semantics and three frontends. A failed build is a controller fault and never a
  fallback onto an older scene; the `StaleSceneAtPublication` guard (H19(k)) is SCOPED — the pin
  discharges it on the pinned path, and every pointer-derived caller keeps it verbatim.
  **(b) Path identity in `partition_key` is PHYSICAL, superseding the 4.0 plan letter.** The 4.0
  text said the program fingerprint and step id join the key; that predates (f) making program
  recompilation ROUTINE, and an administrative component would land every post-answer arena in a
  fresh partition, severing lnZ normalization, coverage and completeness continuity mid-campaign
  for a policy change that alters no physics. The fourth key component is instead
  `path_geometry_fingerprint(q_start, q_end)`: both endpoints on the program's own 9-decimal
  rounding with signed zero normalized, sorted as an UNORDERED pair — direction is not identity,
  a→b and b→a are one physical path — carried on the record as `path_fingerprint` for
  `canonical_path` arenas and `None` otherwise, where the geometric `path_topology` fallback
  governs unchanged. `program_fingerprint`/`program_step` remain PROVENANCE on the record, never
  key components. Partitions therefore mean (sample, conditions, lattice frame, physical
  trajectory), and `95c97d8` is the slice's sole digest re-baseline.
  **(c) The durable-blocker taxonomy is exactly two members.** `instrument_geometry_infeasible`
  (the machine literally cannot perform the measurement) and `program_boundary` (the measurement
  lies outside the assigned search region, by user or machine limits). `resolution_floor` and
  `bose_dead` are DELETED from both pinned vocabularies together (H1, no compat: constructing one
  now refuses as unclassified). There is no Bose dead — anti-Stokes just gets weaker, a matter of
  degree and never absolute — and both deleted names belong to EIG worthwhileness pricing
  (SCENE-5), where a threshold says "not worth measuring here", never "cannot"; a campaign that
  genuinely needs a weak anti-Stokes side simply counts until it has it. This extends the
  2026-08-15 no-hard-rules-for-phenomena ruling (`561d156`) into the blocker vocabulary. The
  `bose_dead` DETECTABILITY reason in the scene scorer is a DIFFERENT vocabulary — declared
  knowledge physics demoting an absence's informativeness — and SURVIVES, guarded by its own test.
  4.3 emits `instrument_geometry_infeasible` only, classified through the canonical
  `classify_rejection` scope rather than substring matching (a bad grid at a REACHABLE q is not a
  geometry decline); `program_boundary` stays valid with no producer in this slice.
  **(d) Cold start is Option A, and the declaration retires on the DECLARING STEP's first
  observation.** The driver mints the initial partition from the card's declarations through the
  SAME `partition_key()` code path — sample from the card, `ConditionSet` through the same
  `t_round` binning measured provenance uses, lattice epoch from declared knowledge, path from the
  step's geometry fingerprint — and passes it into `plan_next_action` as an explicit input, which
  UNIONS it with observed partitions until the declaring step has produced any observation
  (keyed on `program_step` provenance), then retires it permanently: "cold start is just
  initialization; as soon as you take one measurement, that determines the next." This supersedes
  the earlier "no observations anywhere" wording, which broke warm multi-step campaigns. An
  unresolvable declared condition REFUSES at construction with `cold_start_condition_unresolved`
  rather than minting an orphan partition salted by a `scan_ref` a declaration has not got; the
  declared lattice epoch seeds both frame-compatibility maps by `setdefault`, so declared anchors
  fire at cold start and an observation always wins; and the declaration's full key components ride
  in `DecisionBasis`, so replay cites the persisted record and never the mutable card. This
  discharges the H-entry obligation `823502e` recorded.
  **(e) Discovery realization v1 is a nice-number zero-phase grid, and the exposure is declared in
  the wire's own currency.** The energy step snaps to the grid `{1, 0.5, 0.25, 0.2} × 10ⁿ` meV, the
  largest value not exceeding the instrument FWHM at the predicted window centre ("step about the
  FWHM"), and points sit on the ZERO-PHASE grid `k × step` clipped to the window — scans may start
  and stop on non-integer energies, but points always line up with integers, and the scan is never
  anchored at the window edge; fewer than two aligned points refuses rather than widening the
  window. The counting currency is RULED (owner, 2026-08-18): discovery exposure is **10⁸ neutrons
  on the monitor** per survey point, declared DIRECTLY in `number_neutrons` — the one currency the
  wire carries — as the named ISAR default of the `discovery_monitor_ncount` threshold, resolved
  through the ordinary precedence chain (user ask > card policy > ISAR default) and frozen into
  the compiled program, refused when absent like every other threshold. It is a named provisional
  v1, expected to vary by instrument and SCENE-7-calibrated. The earlier 30 s/point counting
  intent and the proposed validate-calibration time-to-counts conversion are SUPERSEDED by this
  direct declaration: with exposure stated in the instrument's own currency there is nothing left
  to convert. `DISCOVERY_COUNTING_SECONDS_PER_POINT`, the provisional exposure constant
  `DISCOVERY_NCOUNT_PROVISIONAL`, and the `discovery_counting_seconds_per_point` proposal-meta
  stamp are all deleted rather than kept as vestiges (H1).
  **(f) Consultation v1: delivery-stable epoch-bearing prompt identity, and an answer that changes
  policy compiles a new frozen revision.** A prompt id is
  `fingerprint(kind, step, subject, options, epoch)` where `epoch` counts the persisted settlements
  for that base identity: the agenda revision and scene digest ride as PROVENANCE and are never
  identity components, or the id would re-mint every delivery and orphan the answer file a human
  had already written; a settled subject that closes AGAIN is a genuinely new occurrence and is
  re-asked under epoch+1, so a historical answer can never re-apply to a later incarnation.
  Timeouts are DELIVERY-COUNTED, never wall-clock, because a clock would fork same-seed replay.
  Nothing blocks: the beam never idles on a human, silence takes the declared default, and a late
  answer is recorded stale rather than applied. A policy-changing answer publishes a NEW frozen
  program revision through the `report/program/` store under CAS with the HONEST parent (the
  fingerprint the step planned under, never a pointer read at write time); the ADOPTION RULE is
  that a later run started from the authored program reads the store back and adopts a stored
  revision that strips to exactly that authored program, and starts a fresh lineage when the
  authored program genuinely changed. `keep_spending` reopens the question but NEVER un-rejects
  the declined action — the instrument's "I cannot reach that" is still true, and re-submitting it
  forever is a loop, not a measurement. `isar answer` is the operator surface over the one writer.
  **(g) The broken-scan lane keys on the PHYSICAL action, and retirement is visible in the basis.**
  Local corruption quarantines through the EXISTING untrusted `isar.measurement-arena/1` record —
  no new trust state, nothing masked or deleted — and the retry counter and retirement key on
  `physical_action_key` (the scientific fingerprint MINUS the scene-digest-bearing evidence basis),
  because the scene moves every delivery and a per-scene identity made the retry bound unreachable
  in production. The retry budget defaults to 2, resolved through the same precedence chain as
  every other threshold and frozen into the program, never a bare code constant; an exhausted
  budget retires the action from SELECTION only, leaves its evaluations eligible in the persisted
  inventory (a broken detector is not a statement about the physics), and surfaces a
  `broken_scan_offramp` consultation request while the step keeps planning — bad data is not a stop
  reason. The retirement set and the C7 declined set are separately embedded in `DecisionBasis` and
  folded into `context_fingerprint` on the omitted-when-empty rule H19(l) set for locality.
  **(h) The parent build's CAS window is a NON-FAULT.** `ConcurrentSceneWrite` — a racing
  read-only `isar report --scene` publish winning the pointer CAS inside the build's window — is
  reported as exit 0 with `pointer_flipped=false`, and the parent steers from its own computed
  digest. The blob is content-addressed and durable before the pointer lock is taken, and the
  pointer is not campaign authority; classifying a lost flip race as a machine failure would let a
  read-only CLI stop a live campaign, which the four-reasons stop law forbids. A genuine fault is
  narrower: the just-committed digest being unloadable, which is store corruption.
  **Supersedes/relates:** (a) supersedes H14(d)'s observe-only driver hook — the operation is
  unchanged and canonical, its OWNER moves from the analysis child to the campaign parent, and the
  hook itself no longer exists. (b) supersedes the 4.0 plan letter's "program fingerprint / step id
  join the key there", for the reason (f) creates. (c) narrows H17's `DURABLE_BLOCKER_NAMES` to two
  members and extends `561d156`. (d) discharges the H-entry obligation `823502e` named and
  supersedes this slice's own earlier retirement wording. (e)-(g) supersede no earlier ruling: the
  realization grid, the consultation channel and the broken-scan lane had no prior H-series text.
  (h) is additive to H19(k), which addressed the store lock and the program-store CAS but not the
  campaign parent's classification of a lost pointer flip.

Topics 1–7 of the 2026-07-09 review are closed. Remaining open from the original list:
user-facing campaign report (deferred until post-testing); renegotiation protocol (O3, future);
tas4d engine choice (open question, decide when scheduled).

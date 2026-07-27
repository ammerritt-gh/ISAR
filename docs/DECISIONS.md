# DECISIONS — ISAR design ruling ledger

> **Lifecycle:** Current
> **Authority:** canonical for the append-only ruling ledger; IDs are stable citation anchors (D2, D4)
> **Provenance:** hand-written
> **Verified against code revision:** `d0ea1df` (2026-07-25)

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
  `isar.session.state`, `isar.session.workspace`, `isar.session.unit_data` (the per-unit ingest
  data reader behind `ws.unit_data`; Qt-free, scipy-free), `isar.drive.session_log`,
  `isar.drive.isar_io.readers`, `isar.report.audit`, `isar.analyze.components`, `isar.geometry`,
  `isar.reduce.collate` (the canonical collation key — one definition of "a dataset"),
  `isar.reduce.llm_client` + `isar.reduce.adjudicate` (Qt-free/scipy-free config validators the GUI
  settings/credential editors reach through the facade — LLM endpoint safety + cloud-fallback
  default; re-exported by `isar/workspace_api/config_gates.py`). Enforced
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

- **T4.** (2026-07-27, Implemented for the TAVI interface and ISAR's plumbing; the fitting-side
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

  **Origin fixes scaling.** Every term declares `instrument`, `sample_environment`, or `sample`, and
  the origin alone determines the base: the first two scale as `N × rate`, `sample` as
  `N × diffuse_background × rate` through an explicit new calibration channel — never the phonon
  factor, which would make the background a function of the very signal it is supposed to be
  independent of. A missing sample scale refuses; only a term the preset marked `optional` is
  skipped, and the skip is recorded.

  **Metadata always resolves, and identity is a fingerprint.** Every scan reports the resolved
  numerics, the preset, the delivery source (`config_default` | `per_scan_override`), a **profile
  fingerprint** over the pre-scaling physics (delivery source deliberately excluded — the same
  physics through either door is the same physics), and an **effective fingerprint** adding the
  applied sample scale and skipped terms. The effective fingerprint is the pooling identity. A
  disabled profile still stamps a block: the absence of background is provenance too. There is no
  lockdown flag; identity is carried by fingerprints, not by enforcement.

  **The truth firewall is physical, not a naming convention.** The planted numbers are the answer to
  the question the fitter is being asked, so they are simply not on the record. The parser keeps
  `enabled`, `source`, `background_schema` and the two fingerprints in
  `raw_header["background_provenance"]` and redacts the numeric block out of the stored payload
  (`{"redacted": "sim_truth", "effective_fingerprint": …}`). The preset **name** is excluded with the
  numbers — `flat_high` states a magnitude as plainly as `5e-9`. The raw JSON on disk is untouched
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
  `CampaignConfig.sample_background` is expanded exactly once at setup into the frozen numeric form
  plus fingerprint, logged as `sample_background_frozen`, and stamped verbatim onto every validate and
  scan body as a per-scan override; resume refuses on `background_fingerprint_drift`. A campaign
  therefore never depends on TAVI's mutable config default, so a GUI edit, another client, a crash
  leftover, or a preset retune cannot change its planted truth mid-run. **Omission is a declaration
  of clean, not an invitation to inherit:** an undeclared `sample_background` freezes the disabled
  profile `{"enabled": false, "terms": []}`, stamps it like any other, and is subject to the same
  drift check — the frozen event carries `"defaulted": true` so logs still separate declared-clean
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

## Session status update

Topics 1–7 of the 2026-07-09 review are closed. Remaining open from the original list:
user-facing campaign report (deferred until post-testing); renegotiation protocol (O3, future);
tas4d engine choice (open question, decide when scheduled).

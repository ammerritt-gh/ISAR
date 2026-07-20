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

## Session status update

Topics 1–7 of the 2026-07-09 review are closed. Remaining open from the original list:
user-facing campaign report (deferred until post-testing); renegotiation protocol (O3, future);
tas4d engine choice (open question, decide when scheduled).

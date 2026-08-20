# SCENE-4 — rulings ledger + execution plan of record

> **Status:** live
> **Authority:** SCENE-4 execution plan of record (the scene-layer contract itself stays
> `docs/design/scene.md`)
> **Owner:** Adrian Merritt
> **Exit criteria:** PRs 4.0–4.4 merged with their H-entries and ARCHITECTURE §15 rows flipped; this
> file then leaves the tree (doc rule 4 — git history keeps the task list).

The grilled rulings of 2026-08-13 (topics 1–8, all RULED) and the five-PR execution plan built
from them — the durable contract each SCENE-4 PR should be reviewed against. Owner rulings here
are decisions of record until their slice's H-ruling is recorded as a dated amendment in
`../design/scene.md` (H15/H16 cover
PR 4.0); the program-level plan of record remains the 22 grilled decisions cited in
`docs/design/scene.md`. Review provenance: the PR 4.0 slice text below survived three plan-verifier
epochs (ten blockers fixed) before implementation; external (ChatGPT) review dispositions are
recorded at the bottom.

## Standing rule (recorded to memory)

`docs/DESIGN_GOALS.md` is USER-owned master objectives: always read when planning, NEVER edit,
build toward it / never against it, surface contradictions to Adrian. Its Pause/Failure section is
law for stopping semantics.

## Topic 1 — Scope frame: RULED

Plan-of-record scope accepted as the SCENE-4 boundary: question layer + deterministic opening book
+ generalized stop + first challenge run + scan-window authoritative default-on + shadow-machinery
deletion. Deterministic scheduler only (permanent fallback thereafter); NO mutual-information
scoring (SCENE-5), NO tracker deletion / targeted-campaign changes (SCENE-6). ChatGPT's "readiness
step" treated as an explicit scope question, not silently adopted.

## Topic 2 — Scene authority readiness (issues #21–#28): RULED

Which SCENE-3 review findings gate "the scene may steer":

- **#21 transported-prior double-counting: RULED (Variant A, 2026-08-13)** — cheap fail-closed
  interim lane lands in PR 4.0: data-derived transported conditioning with unreconstructable
  sources ⇒ non-scoring with reason; DECLARED user priors stay eligible; full transport-DAG
  contract deferred (SCENE-6-adjacent).
- **#22 refiner accounting: FIX in readiness.** Refiners declare bounded per-report rescue;
  `_score_span` includes it; refiner ablation in revision. Riders: (a) sample is FCC — ZC at
  (2,0,0) AND (3,0,0); never bake lattice assumptions into worlds/tests; zone geometry stays
  declared-knowledge (H13). (b) DESIGN INPUT for question layer: endpoint machinery must serve an
  explicit question ("does the branch reach ZC?") answered by extrapolation under declared prior
  (linear near ZC), confidence grows with fit quality / proximity, honest ceiling (cannot test AT
  the ZC — elastic line owns it). Predicates must support answered-by-extrapolation-with-
  confidence-X, not only posterior-mass thresholds.
- **#26 silent arena omission: FIX, upgraded.** Hierarchy ruling: hypothesis map (scientific
  model) → scientific question (what to challenge) → measured S(Q,ω)/QE space (ALL counts in the
  map) → fits on top. **Counts are authoritative; fits are disposable.** Coverage/experiment map
  derives from MEASUREMENT, not fit success — fitless/malformed-fit arenas still stamp footprints.
  Separate lane: BROKEN scan (counts untrustworthy) → quarantine, empty map there, record failure,
  retry when possible, offramp against infinite rescan loops. Split: counts-authoritative footprint
  = readiness; quarantine/retry/offramp = driver behaviour, lands with question layer.
- **#25 topology collapse: option (a) RULED.** Readiness lands partition as fail-closed floor:
  off-path arenas tolerated + recorded + uncoupled; topology never collapses from one context scan.
  Symmetry-equivalent coupling (±k, other BZ via reduced-q folding) + off-path confirmation-scan
  generation are ONE feature, named and deferred (SCENE-5-adjacent), designed on its own terms —
  do not build against it (DESIGN_GOALS sample-knowledge map is the fuller picture). Campaign
  freedom directive: no pre-computed safe zone required by default; hard limits optional on the
  program contract; off-path never punished.
- **#28 partitioning: FIX, generalized.** Partition key `(sample, conditions, path)` where
  conditions is an OPEN parameter set (T, field, pressure — never hardcode T), from persisted
  provenance, in fingerprints. Cross-condition coupling only via future declared+scored factors.
- **#23, #24: DEFERRED** (store/read-surface robustness, not posterior meaning).
- **#27: dissolves into predicate design** — an `unresolved` cluster never satisfies a completion
  predicate.

## DESIGN_GOALS harvest for SCENE-4 (2026-08-13 reading)

- **Stop law (Pause/Failure States):** only four stop reasons — user choice, campaign goal met,
  over budget, machine failure. Bad fits / low intensity / unknown phenomena / unexpected results
  NEVER stop a campaign; they inform new scans. Explicit SCENE-4 work item ordered by the doc:
  review-and-cleanup pass removing all legacy extra stopping points from the drive path.
- **Sample knowledge (defer full map, never build against):** lattice params / UB / space group
  are stamped, MUTABLE, per-scan provenance with transform-and-reuse semantics; alignment is a
  hypothesis ISAR updates (misalignment detection → UB correction → reorient old scans);
  mosaic/twinning/coaligned-crystal and sublattice modelling are future phenomena. SCENE-4
  consequences: partition/fingerprint keys must anticipate lattice-epoch/UB-stamp axes (H13
  lattice epoch already exists); nothing may assume lattice/orientation immutable.

## Topic 3 — Question lifecycle & authority: RULED

- **Regime spectrum** (user-declared intent, on the user to state clearly):
  1. Open discovery — find/pin/identify phenomena, fit to ISAR standard defaults; emergent
     in-domain questions fully join stop accounting.
  2. Strict targeted — map THIS (possibly ranked multi-target); everything else only in service
     of the target; emergent questions record-only. = SCENE-6 pre-pinned question sets.
  3. Guided — region + declared expectations + some freedom; dialed between 1 and 2.
  SCENE-4 wires regime 1 only (the challenge campaign); program contract carries the policy knob;
  regimes 2/3 wait for SCENE-6. **Stronger campaign writer (real goal compiler) deferred by
  ruling** — current test campaign: map phonons on the FCC test sample in a limited region.
- Out-of-domain discoveries: recorded + reported, never chased by default, never block stop; may
  PROMPT the user to widen the domain.
- Regime-1 wording enumerates families: pin down = ConfirmPresence, identify = ClassifyIdentity,
  fit-to-defaults = EstimateParameter (auto-opened per confirmed phenomenon, default tolerances).
  → **All seven families in SCENE-4 scope** (schema + predicate + generator bindings, wired for
  regime 1) — ratified below.
- **Human-in-the-loop ruling:** users can be prompted mid-campaign for permissions/updates; never
  set-and-completely-forget; consequences of user choices/absence are on the user. Design
  consequence: a consultation channel in the drive loop (prompt = question + options + default +
  on-timeout behaviour), riding the same control-file surface as the stop token. Unanswered-prompt
  default semantics: OPEN QUESTION (next to rule).
- **Unanswered prompts: RULED — non-blocking with declared defaults.** Silence chooses the
  stated default; opt-in hard gates are the exception; beam never idles on a human.
- **"ISAR is always measuring": RULED.** Campaign COMPLETION ≠ stop measuring. Only user
  intervention idles the beam; goal-met completes the campaign and the instrument moves on
  (queued campaigns / open-ended fallback campaign = the ideal). SCENE-4: completion verdict
  kept distinct from beam-stop so a campaign queue slots in later without redesign (the due
  `run_program` seam is where multi-campaign orchestration would live); queue/fallback itself
  deferred (DESIGN_GOALS objective — never build against it).
- All-seven families: ratified (no objection raised).

## Topic 4 — Completion predicates: RULED

- **Thresholds' home: RULED.** One named versioned defaults profile (no magic numbers in
  predicate code); goal compiler resolves + freezes effective values into the campaign program at
  compile time; predicates read only the program. Precedence: user ask > card policy guidelines >
  ISAR named defaults. Mid-campaign changes only via consultation-channel update → re-freeze →
  program version bump (provenance, not drift). Default VALUES set at implementation as named
  provisional v1 declarations, SCENE-7-calibrated.
- **Hypothesis-driven core: RULED (design principle).** The fundamental question is always
  "what is our confidence in this MODEL versus our data." Fitted quantities (phonon energies) are
  instrumental to higher models (e.g. "the dispersion changes this way with temperature").
  Consequence: completion predicates are statements about model confidence; a parameter tolerance
  (±0.1 meV) is the degenerate special case, not the template. SCENE-4 instantiates the modest
  cases but the predicate schema must not hard-wire parameter-tolerance-only completion. The
  model hierarchy above phenomena (dispersion-vs-T etc.) is future work — presumably the empty
  "Phenomena Hypotheses" section of DESIGN_GOALS (Adrian's to write; never edit).

- **Unresolvable terminal: RULED ("reasonable for now").** A question closes three ways:
  answered / overtaken (subject dissolved) / unresolvable-with-available-experiment. Unresolvable
  requires BOTH (1) a recorded physical blocker on the discriminating measurement (below
  resolution floor — after attempting per the near-degeneracy ruling; bose_dead; quarantined
  region; instrument infeasibility) AND (2) generator exhaustion (no new proposable action).
  Exhaustion without a blocker = generator defect report, question stays open. Closure emits a
  non-blocking consultation prompt (overrule to keep spending). `unresolved` cluster_authority can
  never ANSWER a question but its blockers may count toward unresolvable closure. Deterministic
  stand-in until SCENE-5's EIG≈0 criterion.

## Topic 5 — Opening book: RULED

- **Generator family, not a phase.** The book is DiscoverRegion's deterministic generator
  behaviour in the sparse-coverage regime; question layer authoritative from scan one; no
  bootstrap phase, no handoff condition. `opening_book.py` = where that generator lives.
- **Hypothesis-informed placement, not geometry-uniform.** Even blind survey is
  hypothesis-driven: catalogue phenomenon classes carry findability knowledge (acoustic → near
  declared ZCs at low ω — strongest, best-predicted, extrapolatable — but offset from the
  Bragg/elastic line, which is itself a modelled phenomenon; not at ZB — weaker, poorly
  predicted). The deterministic book encodes the conclusions of the hypothesis → model → EIG
  chain as declared per-class placement rules; SCENE-5 replaces the encoded conclusions with the
  real EIG computation. A true uniform prior-less sparse map is a USER-selectable option, not the
  default.
- **Iterative from scan one:** generators consult the current scene each round — first detection
  feeds extrapolation for the next placement ("fixing one point lets us extrapolate to the
  next"). Not a fixed upfront batch.
- Frozen discovery profile: placement strategy per catalogue class, window policy (broad over
  intrinsic domain + resolution margin), default counting time, AS via detectability gate at
  declared T. Frozen into program at compile; numbers = named provisional v1,
  SCENE-7-calibrated.

## Topic 6 — Scheduler policy: RULED

Strict lexicographic, re-evaluated after every delivery: (1) campaign priority class from the
program (regime defaults: breadth before polish — no EstimateParameter refinement while
DiscoverRegion has unsurveyed territory); (2) declared dependency order (ConfirmPresence →
ClassifyIdentity → EstimateParameter chains); (3) instrument locality tiebreak — RIDER: a
stand-in for real motor-speed/cost knowledge, future work, design the seam so a genuine cost
model slots in; (4) stable ID order (replay determinism). **Starvation accepted** for SCENE-4:
mitigation = user drafts leeway into targets, or tuned priorities when ISAR runs loose;
principled fix is SCENE-5's value computation. Backstops: unresolvable terminal, offramp, budget.

## Topic 7 — Flip + carried items: RULED

- **Default-on flip:** mechanical per decision 19 — authoritative selection default (O43),
  O42-retirement H-entry, AGENTS routing row update, world-suite coverage as acceptance evidence;
  `eig_shadow.py` + `realized_comparison.py` deleted this slice unless wiring overlaps SCENE-5.
- **expected_two:** implement the 2026-08-09 permit-not-force ruling (declaration relaxes the
  diagnostic, `or expected_two` bypass at loop.py:945 goes); lands in SCENE-4.0 readiness.
- **run_program seam:** minimal — drive runs a program end-to-end through one seam; completion
  verdict separated from beam-stop; queue/fallback not foreclosed, not built.
- **Cadence → PIPELINED-STALENESS CONTRACT (Adrian's reframe, load-bearing):** ISAR accepts it
  usually does NOT have the perfect scan ready. Model A picks scan 2; scan 1 lands; scan 2 starts
  while the update to model B (which picks scan 3) computes; selection runs on a slightly stale
  posterior and that is OK. Accuracy-vs-throughput is an OPERATOR tradeoff, never fixed in ISAR:
  expose configuration (synchronous / fixed-lag pipelined / fixed-batch-N for very fast
  machines; more staleness for weak compute). Design consequences: (1) staleness policies are
  DECLARED and STRUCTURAL (fixed lag/batch), so replay stays exact — wall-clock-adaptive "use
  whatever revision is done" would be replay-breaking, later opt-in if ever; (2) every action
  decision STAMPS the scene revision digest it was selected against (provenance: which model
  picked this scan); (3) SCENE-4 default = synchronous (lag 0) on synth for exact test replay;
  the policy knob + stamping land now so nothing bakes in "always fresh"; async execution
  machinery itself is future work. Keep recording per-delivery build timings for the data-driven
  incremental-build decision later.

## Topic 8 — Acceptance: RULED

Matrix accepted. Beyond the plan-of-record worlds (canonical, blank/book-terminates, budget
honesty, AS warm+cold, mismatch) and the challenge-run exit, SCENE-4 proves: stop law (bad
fits/unknown phenomena never stop; four reasons only; legacy stop-point audit with enumerated
removals); counts-authoritative footprints + broken-scan quarantine/retry/offramp; partition
floor (off-path scan leaves topology intact; two-condition workspace separates); refiner-bound
pruning honesty (#22 acceptance case); unresolvable terminal (attempt → blocker+exhaustion →
close, campaign proceeds, prompt recorded); consultation channel (non-blocking prompt, silence
takes declared default); replay (same seed ⇒ identical action sequence; every decision stamps
its revision digest); default-on selection-path coverage.

**Worlds philosophy (Adrian):** worlds are simply tests for ISAR — "given this situation, how
does it perform." Tuning an existing world is an OWNER-ONLY decision (allowed if one stops
working for our tests, on his call); adding worlds is otherwise the standard move. Split
ratified: ONE new world for the off-path/context-scan campaign claim (needs the full steering
loop); fixture-level tests for two-condition partition, refiner bounds, unresolvable closure;
zero retunes of the existing twelve without his say-so.

## Consolidated: what SCENE-4 is (post-grill)

The deterministic decision layer between the scene posterior and acquisition. The campaign
program opens an agenda of QUESTIONS (seven families, all in scope, regime-1 wiring); the scene
posterior — made steering-trustworthy by the SCENE-4.0 readiness fixes — feeds emergent
questions; completion predicates (model-confidence statements, frozen thresholds) close them;
`evaluate_stop` becomes open-questions accounting under the four-reasons stop law, with
completion ≠ beam-idle; the deterministic scheduler (priority → dependency → locality-seam →
stable ID) picks actions under a declared pipelined-staleness contract; the opening book is
DiscoverRegion's hypothesis-informed generator, not a phase; scan-window selection flips
authoritative default-on and the shadow machinery dies; users are promptable mid-campaign,
non-blocking, silence takes declared defaults. Exit: the challenge run end-to-end on synth.

Left out (named, not foreclosed): scene-EIG/MI (SCENE-5); targeted-campaign unification +
tracker deletion + regimes 2/3 (SCENE-6); calibration of all provisional numbers (SCENE-7);
symmetry-equivalent coupling + off-path confirmation generation (one deferred feature,
SCENE-5-adjacent); full transport-DAG contract (#21); campaign queue / open-ended fallback;
stronger goal compiler; motor-cost models; incremental scene builds; full sample-knowledge map
(DESIGN_GOALS: mutable lattice/UB/space-group provenance, alignment-as-hypothesis,
mosaic/twinning, sublattices); model hierarchy above phenomena (dispersion-vs-T).

## Cold-start ruling (2026-08-15, owner — PR #33 review discussion): RULED

How a fresh (or freshly re-conditioned) workspace gets its first scan. Decision of record
until 4.3's H-entry lands:

1. **Option A — the driver constructs the initial partition from declarations.** Every
   partition component is derivable before any measurement: sample identity from the card,
   ConditionSet from the declared setpoint through the SAME t_round binning rule measured
   provenance uses, lattice epoch from declared knowledge, path from the program step. 4.3's
   driver mints this declared partition with the same `partition_key()` code path, the agenda
   fans out to it, and the ordinary machinery plans scan one — no bootstrap special case
   anywhere. A declaration/reality mismatch on first measurement is an ordinary one-time
   dissolve/reopen, not a failure mode. (The rejected alternative — a canonical context-less
   bootstrap scan — cannot express the physics below and would orphan its own attribution.)
2. **Initialization is catalogue-declared, never controller-coded.** The default phonon
   profile's placement knowledge yields the owner's walkthrough: first scan just off the
   nearest in-region zone centre at low ω hunting the acoustic branch (highest prior,
   strongest, best-estimated there); persistent absence updates the posterior and the hunt
   pivots to optic phonons at the ZC from finite E upward; initialized points extrapolate
   outward (phonons disperse continuously). A different catalogue initializes differently by
   its own declarations; flat or absent knowledge degrades HONESTLY to the uniform sparse
   survey (anchor scoring fails closed; `survey_order: uniform` remains user-selectable), and
   emergent signal drives everything thereafter.
3. **The owner's walkthrough is the 4.4 cold-start acceptance script** for the default
   profile (first proposal just-off-ZC low-ω; failed acoustic hunt pivots to the ZC optic
   hunt; outward mapping from initialized points), plus a cheap degradation test: blank
   knowledge ⇒ the first proposal is uniform sparse, never an error.
4. **4.4 script check, not assumed:** whether on-ZC survey nodes need a nonzero-E window
   floor against the Bragg/elastic tail (proposal-level exclusion), or fitter-side elastic
   handling suffices.

Related inbox candidates (same discussion): the soft energy ceiling with spare beam time;
the failed-in-zone hunt recommending another Brillouin zone. Both in
`docs/DESIGN_GOALS.md` `## Inbox`, 2026-08-15 entries (recorded in the former
`DESIGN_GOAL_INBOX.md`, since merged into that section).

## Zone-centre endpoint ruling (2026-08-15, owner — PR #33 review discussion): RULED

The `zc_linear_compatibility` question's beam posture, superseding the flagged C4
interpretation's status (the code survives; its STATUS changes):

1. **Prior posture.** Acoustic phonons reach the zone centre, linearly nearby — at
   overwhelming prior. Compatible-by-prior is the standing answer; the burden is on
   FALSIFICATION. A scientific case for anything else arrives as a DECLARED hypothesis
   ("prove it does not disperse to the ZC"), never as default-profile hedging.
2. **No hard rules for phenomena — the protocol is EIG-emergent.** The near-ZC triplet scan
   (Stokes + anti-Stokes + elastic fitted together) happens when EIG demands it: peaks close
   together (cheap window), high intensity, three mutually-constraining peaks (easiest fit).
   Far from the ZC: Stokes first, anti-Stokes only if the fit needs it; at low T the Bose
   factor collapses anti-Stokes EIG (the discovery profile's declared
   AS-detectability-at-declared-T gate is the pattern: knowledge as declared data, never
   code). Priors exist so they can WEAKEN: linearity decays away from the fold, so
   hypothesis strength (predicted energies) is high near the ZC and low far away, and
   placement, window content, and stopping all fall out of that gradient priced against
   cost. Recorded here as named SCENE-5 EIG design inputs.
3. **The C4 zc stand-in is a declared deterministic PLACEHOLDER for an EIG conclusion**
   (the Topic 5 pattern), accepted as such — not physics law, not worth refining, and it
   dissolves when SCENE-5's real EIG lands. The reviewer's "could it scan closer?" is
   answered there: EIG prices the shrinking lever arm against the elastic wing; the
   deterministic v1 does not pretend to.
4. **Misalignment signature, toward the deferred sample-knowledge work:** Bragg peaks ARE
   the zone centres — the map itself. Acoustic branches converging on a non-ZC coordinate
   will usually find the Bragg peak sitting there too; the coincidence is ALIGNMENT evidence
   (lattice constants / UB update), never dispersion physics. Detection belongs to the
   alignment-as-hypothesis objective (DESIGN_GOALS); recorded so the future work is built
   toward this signature.

## Cross-class connectivity ruling (2026-08-15, owner — PR #33 review discussion): RULED

The fully-classified multi-class partition's connectivity silence (one acoustic tile + one
optic tile => no track question; the H19(b) seen consequence) is ACCEPTED as correct scope,
with a named tripwire:

1. "Does this acoustic connect to that optic" is not a linkage-bookkeeping question. Near an
   (avoided) crossing the branches hybridize and character swaps; the real question is about
   the CROSSING itself (gap or no gap) — model-hierarchy territory: a hypothesis about two
   branches with its own priors and its own EIG, arriving with the deferred
   symmetry/crossing feature. A cross-class track RULE today would legislate per-phenomenon
   behaviour before the machinery exists to price it (forbidden by the no-hard-rules
   principle, same day's ruling above).
2. **Tripwire, named where the owner would incur it:** declaring identity-conditioned priors
   BEFORE the crossing work lands silences connectivity in mixed-class regions — fine for
   well-separated branches, blind exactly at crossings. The day priors are declared, either
   accept that blindness knowingly or pull the crossing/hypothesis work forward.
3. Unreachable in production until priors are declared (the representability gate); no code
   change.

## Defaults-profile label ruling (2026-08-15, owner — PR #33 review discussion): RULED

Option (c): `DEFAULTS_PROFILE_VERSION` keeps versioning the default VALUE SET only (H19(g)
stands; no retroactive v2), and the one reader trap is signposted — the reading rule
("a profile version is meaningful only alongside its policy schema version; never cite it
bare") is recorded in `SCHEMA_LANDSCAPE.md` beside the `/2` block. Replay and audit are
unaffected either way: effective values freeze into every compiled program.

With this, all four owner questions the PR #33 reviews left open are RULED (cold start;
zc prior posture / no hard rules; cross-class connectivity; this label rule).

# SCENE-4 execution plan (rev 2 after external-review triage — 2026-08-13)

Five PRs, sequenced; one branch per PR, owner reviews and merges (SCENE-3 cadence). Code
surfaces verified by scout against `a1c6e41`+`acec76c`. Every slice: H-entry, doc updates
in-change, one deliberate re-baseline commit where digests move, three-lane test discipline
(targeted while iterating; not-slow parallel + full parallel at slice boundary; serial before
merge). External (ChatGPT) review triaged 2026-08-13; dispositions at the bottom.

## PR 4.0 — Scene authority readiness (M) — branch `SCENE-4a`

The posterior becomes steering-trustworthy (with respect to the ruled issues) before anything
reads it to steer.

- **#21 interim lane — RULED: Variant A.** A fail-closed check at scene intake: an arena whose
  winning comparison carries data-derived transported conditioning (constraint origin =
  data-derived, e.g. `prior_predictive` from accumulated anchors) whose source observations
  cannot be reconstructed is **non-scoring with reason `transported_conditioning`** —
  experiment-map-only, like H7-incomplete arenas. Explicit lane per origin state — ALL EIGHT
  members of `CONSTRAINT_ORIGINS` (`isar/schema/statistics.py:55–66`) plus the tri-state `None`
  on `CandidateRow.constraint_origin` (`model.py:238–246`), aggregate written by
  `aggregate_constraint_origin` (`loop.py:373–375`, persisted `scan_delivery.py:500`):
  - SCORING: `"none"` (recorded, unconstrained); `"user_declared"` / `"prespecified_scientific"`
    (declared information counted exactly once in lnZ is legitimate — H204 finding);
    `"catalogue_variant"` (catalogue-declared model structure, e.g. resolution-pinned variants —
    prespecified, not neighbour data).
  - NON-SCORING `transported_conditioning`: `"prior_predictive"`, `"data_dependent_rescue"`,
    `"post_selection"` (data-derived by construction), and `"mixed"` (the aggregate cannot
    prove all components benign — fail closed).
  - REFUSE the build: `None` (no semantics record — workspace predates persisted fit semantics;
    re-deliver instruction, H1 precedent: the footprint fields) and any origin string not in
    `CONSTRAINT_ORIGINS` (schema drift surfaces loudly, never degrades silently).
  Acceptance: one fixture per enum member plus the `None` case and an unrecognized-origin case,
  each asserting its lane and recorded reason; a transported arena does not score merely for
  having finite lnZ. Full transport-DAG contract stays deferred.
- **#22 refiner accounting** — `isar/scene/factors.py` (protocol at :88–146): refiners declare a
  per-report rescue bound; the pruning contribution is **per-report bound × a conservative
  maximum refinable-report cardinality computed from every usable candidate BEFORE any pruning**
  (one rescued report per alternative is not the bound — an alternative with N endpoint readings
  can move N×R nats). Fallback posture if the pre-pruning cardinality is unobtainable: a
  score-affecting refiner disables margin pruning outright (joins the `truncation_unbounded`
  lane semantics). Ablation is a **transformation ablation**, distinct ledger from score-term
  ablation: recompute visibility without the refiner, rerun the score factors that consume
  visibility, renormalize over the fixed alternatives. Payload assembly lives in `build.py`
  (store stays payload-agnostic). Fix the test pinning "knowledge factors cost no pruning span".
- **#26 counts-authoritative intake** — three honest states: trusted measurement + fitted
  candidates / trusted measurement + no usable interpretation (experiment-map-only) / untrusted
  broken measurement (recorded as attempted, NO coverage credit). Requires a minimal persisted
  **measurement-arena record**: producer `isar/session/scan_delivery.py`, written at
  QC-complete BEFORE any fit attempt (so it survives a fit crash); persisted artifact
  `<workspace>/report/measurements.jsonl`, schema `isar.measurement-arena/1` (one Stage II↔III
  contract file added — H1, judged by the world suite); adapter
  `isar/workspace_api/scene.py::scene_observations` (:33, sole `SceneObservation` constructor
  at :71) joins candidate rows onto measurement records and carries ALL new 4.0 fields (sample
  identity, ConditionSet, lattice-epoch token, trust status) — the facade stays read-only and
  Qt-free. Record fields: arena identity, sample/conditions/program-step provenance, q +
  lattice-epoch token, kept energy cells, effective exposure, count-trust status. The
  trusted-vs-quarantined status field lands HERE (a broken scan must never acquire coverage
  credit that 4.3's retry machinery would have to retract). A malformed candidate table still
  fails the scene build loudly — a legit fitless arena and a corrupt table never share a lane.
  Scene intake (`build.py:305–471` `_validated`/`_non_scoring_reason`/`_split_by_evidence`)
  consumes the record; silent omission becomes impossible by construction.
- **#25 + #28 partition** — partition key `(sample identity, ConditionSet, lattice-epoch frame
  token, path identity)` — path identity via `topology_role` campaign provenance with geometric
  collinearity as the fallback; the PR 4.3 provenance producer completes the path component
  (program fingerprint / step id join the key there):
  - *Path identity from campaign provenance* (program fingerprint, step id, topology role:
    canonical-path / context-probe / unassigned); geometric collinearity (`path_topology()`,
    `build.py:243`) remains the FALLBACK inside a provenance group and for provenance-less
    workspaces. Off-path arenas are unassigned or own-component — they neither collapse the path
    (was #25) nor accidentally form new "paths" with other unrelated probes by collinearity.
  - *ConditionSet* typed, not an untyped dict: named dimensions, canonical units, exact-vs-binned
    semantics, fingerprint; missing/unknown fails closed to no-coupling. Generalize the existing
    temperature-specific `CampaignSliceKey` in the action-inventory layer rather than growing a
    parallel condition identity (verify shape at implementation).
  - *Frame token:* the H13 lattice epoch joins the partition identity now; coupling across
    unproven frame compatibility fails closed. Same numeric HKL ≠ same scientific q across
    incompatible lattice interpretations (DESIGN_GOALS mutable-map objective — carry the token,
    do not build transform-and-reuse).
  - `SceneObservation` (`model.py:318–364`) gains sample identity + conditions; all key parts
    enter `observation_fingerprint` (digest move intended → the one re-baseline commit).
- **expected_two permit-not-force** — the declaration RELAXES the offering diagnostic, never
  replaces it (2026-08-09 ruling). Mechanism: `_offer_multimode` (loop.py:936–949) gains the
  declared count as an input; with a declaration ≥ 2 it evaluates the SAME two diagnostics at
  named relaxed thresholds — `GATE_COST_DECLARED` (< `GATE_COST`) for the cost trigger and
  `RUNS_ALPHA_DECLARED` (> the default alpha) for the Wald–Wolfowitz runs test — both
  provisional v1 constants, SCENE-7-calibrated. Fully decidable, natural abstention: a declared
  scan whose one-mode fit is clean (cost/dof under the relaxed threshold AND unstructured
  residuals at the relaxed alpha) is still NOT offered. Undeclared scans keep today's constants
  exactly. Evaluated where the gate runs today — no enumeration or composite needed before the
  decision. (The residual-located seed DETECTOR generalization stays with the future N-mode
  growth work, per ruling item 3 — deliberately not built here.) The relaxation applies at ALL
  `_offer_multimode` call sites uniformly — including the structure-hint shortcut gate
  (`_try_structure_hint`, loop.py:1129, reached from `fit_structures_two_stage_hinted`
  loop.py:1165–1170 on the master-refit path): the diagnostic is one function, and a declared
  scan's offer must not depend on whether a structure hint happened to be in scope. A declared
  hinted scan with between-threshold structure therefore rejects the shortcut and receives the
  full two-mode offer. The bare `or expected_two` disjunct is removed at ALL sites:
  `fit_shadow_candidates` (loop.py:850–855) and the production two-stage `_enumerate_and_fit`
  (loop.py:1057–1058, reached via `scan_delivery.py:1603`).
  **Route vocabulary:** `multimode_route="guideline_forced"` is retired for
  `"declaration_relaxed"`, recorded ONLY when the relaxed thresholds (not the global ones) made
  the offer — re-derived from the gate outcome, never from `peaks` presence. All producers and
  consumers move together: production writer `scan_delivery.py:1628–1634`, shadow producers
  loop.py:799–808 and loop.py:899–905, eligibility consumer + vocabulary list
  `isar/eig/eligibility.py:42, 438–444` (the `selection_guideline_forced` flag becomes the
  relaxation-keyed equivalent), `isar/synth/stat2b/campaign.py:492`. Tests: declared scan with
  structure between relaxed and global thresholds is offered while the identical undeclared
  scan is not; the offered candidate still loses when evidence rejects it; a declared clean
  one-mode scan is not offered; a declared scan offered by the GLOBAL triggers persists an
  ordinary route (not `declaration_relaxed`) and raises no relaxation flag, while a
  relaxation-driven offer persists `declaration_relaxed` and does; no convergence/usability
  gate is bypassed.
- **Exit:** #21/#22/#25/#26/#28 + expected_two acceptance green (fixture-level, `tests/scene/`
  + `tests/session/` + `tests/analyze/`): the #21 origin-lane cases above (one per enum member
  + `None` + unrecognized); multi-report refiner rescue not pruned under a one-report bound;
  a refiner whose pre-pruning cardinality is unobtainable disables margin pruning outright
  (asserted on the built revision's truncation lane); the transformation ablation appears as
  its own ledger entry, distinct from score-term ablation, on the revision payload;
  measured-but-fitless arena fills the experiment map; count-untrusted arena stamps NO
  footprint / earns no coverage credit while remaining recorded as attempted; malformed
  candidate table fails loudly; two-condition fixture separates; two observations on
  incompatible lattice epochs do not couple; off-path fixture keeps the path topology and joins
  nothing else; the expected_two cases above incl. route/flag provenance AND the hinted case
  (declared scan + structure hint at between-threshold structure ⇒ shortcut rejected, two-mode
  offered, `declaration_relaxed` route); not-slow lane green post-re-baseline.

## PR 4.1 — Question + agenda core (M/L, offline: no driver wiring) — branch `SCENE-4b`

- **Three objects, not one** (`isar/scene/questions.py`), because identity, assessment, and
  agenda change at different rates and Bayesian answers can reopen:
  - `QuestionSpec` — immutable: stable ID, family, subject, predicate declaration,
    priority/dependencies, generator IDs, policy provenance.
  - `QuestionAssessment` — per scene revision: status for THIS revision. As delivered (H17):
    the digested snapshot carries `open/answered/unresolvable` only — each a pure function of
    the derivation inputs — while `overtaken`/`dissolved`/`reopened` are typed HISTORY
    transitions (relations between two agendas, recorded outside the digest at flip time,
    with successor/dissolution records and `equivalence_unproven` markers). A question
    answered at scene N may legitimately reopen at N+1.
  - `AgendaRevision` — content-addressed (SCENE-3 store pattern): program revision, scene
    digest, derivation manifest, specs, current assessments, lineage.
- **`QuestionSubject` contract before families:** canonical semantic identity + how a subject
  matches into a NEW scene revision + what overtakes it. Subjects are never raw cluster indices
  or leading-alternative component pointers — question IDs must survive scene updates or
  dependency/stop semantics become label-matching.
- **Predicates declarative + versioned** (kind, parameters, required scene variables, authority
  requirement, evaluator version, defaults-profile provenance), returning structured
  assessments. Three authority lanes: `exact`; `bounded` — real bound arithmetic, the predicate
  holds only under ADVERSARIAL allocation of the omitted mass (a finite-but-vacuous bound
  answers nothing — the sharpened #27); `unresolved` — never answers, blockers may feed
  unresolvable closure. Model-confidence statements first-class; parameter tolerance the
  degenerate case.
- **Family contract matrix** (lands in SCENE_DESIGN §5): per family — subject, opener, answer
  emitted, predicate, generator IDs, blockers, dependencies. `VerifyCompleteness` v1 is
  explicitly relative to the frozen domain, catalogue, discovery profile, detection limits, and
  generator registry — never metaphysical absence. `EstimateParameter` v1 scoped to parameter
  intervals the scene revision persists (no sideways reads into live workspace winner rows —
  the exact coupling the SCENE-3 read-surface work removed). The ZC endpoint answer is a NAMED
  assessment model (declared prior, data provenance, propagated uncertainty, explicit
  confidence ceiling), not a side effect of refiner bookkeeping.
- **Defaults profile + compiler freezing:** `goal_to_spec.py` resolves user ask > card
  guidelines > named defaults; the discover step contract is version-bumped with the resolved
  policy block REQUIRED (thresholds, emergent-question policy, staleness policy, prompt
  defaults). A program without it is refused with a recompile message — frozen may never mean
  "acquires today's defaults at read time" (H1 clean break; no read-time defaulting).
- **Pure `AgendaVerdict` evaluator** (new, beside — not replacing — targeted `evaluate_stop`,
  which the tracker keeps until SCENE-6): open-questions accounting → completion verdict
  distinct from beam-stop; four-reasons law; unresolvable is never a stop reason;
  footprint-quantified nulls feed VerifyCompleteness.
- **Exit:** fixture-level — predicate semantics per family incl. bound arithmetic; three closure
  paths; stable question identity across an added neighbouring scan (assessment changes, ID
  does not); reopening; agenda derivation replay-identical and content-addressed from identical
  revisions.

## PR 4.2 — Action generation + deterministic scheduling (M, offline) — branch `SCENE-4c`

Selection is fully testable from stored revisions before any driver goes live.

- **Through the existing `ActionInventory`, never beside it:** questions bind generators via
  `MeasurementIntent` carrying question identity; proposals flow into the canonical inventory
  (`inventory.py` GeneratorSpec pattern, :92–100) so exhaustion is proved against the same
  action set acquisition generated — no second scheduler-owned action list (that would recreate
  the pre-inventory controller/acquisition split). Intent/evaluation provenance gains question
  ID + agenda revision.
- **Question-scoped exhaustion:** per-question completeness summaries over generator reports.
  A failed or missing required generator is `controller_fault` (machine/software stop lane),
  NEVER an unresolvable-question result. Every family has a real generator binding in this PR —
  a question with no implemented generator cannot honestly exhaust.
- **Opening book** (`opening_book.py`): consumes DECLARED per-catalogue-class placement rules
  (registered with the catalogue profile, not an `if acoustic/elif optic` tree);
  hypothesis-informed anchors decide ORDER, never coverage — invariant: DiscoverRegion may not
  leave an in-domain interval wider than the frozen `maximum_initial_spacing_rlu` unsurveyed
  (field exists, `campaign_program.py:78–109`). Iterative from scan one; uniform prior-less
  survey = user option.
- **Lexicographic scheduler** over eligible `ActionEvaluation`s: priority → dependency →
  locality-seam (stand-in for a future motor-cost model) → stable ID; persists the complete
  rank vector and selection reason; starvation accepted (backstops: unresolvable terminal,
  offramp, budget).
- **`DecisionBasis` envelope** — the full basis, not the scene digest alone: scene revision
  digest, agenda revision, frozen program fingerprint, generator-registry version, scheduler
  policy/version, inventory content hash, staleness policy (+ locality state). Persisted on the
  decision and the resulting measurement provenance.
- **Exit:** offline determinism — same stored revision + program ⇒ identical inventory, rank
  vector, selection, and DecisionBasis; question-scoped exhaustion closes one question while
  another retains actions; generator defect yields controller fault; coverage invariant holds
  on a sparse-anchor fixture.

## PR 4.3 — Live driver integration (M/L) — branch `SCENE-4d`

- **Loop wiring** (`drive/loop.py`): delivery → scene build (`scene_hook.build_and_store`,
  `driver.py:630` — no longer observe-only in discovery) → agenda → inventory → selection.
  The driver STAMPS `topology_role` provenance onto measurement records (`canonical_path` for
  program-step scans, `context_probe` for diagnostics) — the 4.0 partition landed the consumer
  vocabulary with `None` defaults, and steered campaigns take their first context scans here, so
  the producer must land here too (carried from 4.0 executor report).
  Steering consumes the JUST-BUILT revision object, never a re-read of `current.json` (the
  report CLI is a concurrent writer); a failed synchronous scene build is a controller fault —
  never a silent fallback to an older stored scene passed off as lag-0.
- **Staleness:** schema + DecisionBasis stamp land for all three modes; only synchronous is
  implemented; nonzero fixed-lag / batch-N settings are REFUSED as unsupported, never accepted
  and ignored. Per-delivery build timings recorded.
- **Consultation channel v1 — narrow, append-only:** stable prompt ID, originating question +
  agenda revision, options, declared default, timeout behaviour, expected program revision;
  events `prompt_issued` / `prompt_answered` / `prompt_default_applied` via report/feed +
  `report/control/` answer files (stop-token surface). An answer that changes policy COMPILES A
  NEW FROZEN PROGRAM REVISION (no in-place mutation); a late answer to an already-defaulted
  prompt is stale unless explicitly re-applied. Hard gates v1 map to operator-requested pause
  (no queue exists to give a gate alternate work yet).
- **Broken-scan lane** distinguishes: local corruption → quarantine + bounded retry + offramp
  prompt; transient request failure → existing refusal/suppression vocabulary; global
  machine/beam failure → the stop-law pause. Transient failures never become durable question
  blockers.
- **`run_program` seam:** returns step result + completion verdict + beam directive; a completed
  step returns to its caller — no implied queue; verdict ≠ idle stays structural.
- **Discovery stop routing exclusively through `AgendaVerdict`** — structurally isolated from
  the legacy targeted stop states (`met/incomplete/infeasible/continue`), so the four-reasons
  law holds on the discovery path from its FIRST live test, without touching tracker semantics
  (owner-adjudicated, SCENE-6).
- **Exit:** campaign fast worlds — canonical discovery question-driven; blank world terminates
  honestly; same-seed full-campaign replay (identical action sequence + DecisionBasis);
  synchronous-failure fixture cannot steer from a stale scene; prompts recorded with defaults
  taken; broken-scan world quarantines and offramps without stopping.

## PR 4.4 — Authority flip, stop-law cleanup, acceptance closure (M) — branch `SCENE-4e`

- **Default-on flip at COMPILE time, not runtime:** newly compiled programs explicitly freeze
  `scan_window_eig_mode="authoritative"`; the step schema requires an explicit mode; a
  historical absent-mode program is refused under H1 with a recompile message — the runtime
  `.get(..., "off")` fallback (`eig_runtime.py`, default at `objective.py:232`) is never changed
  so that an old frozen program silently starts choosing different windows. O42-retirement
  H-entry; AGENTS.md routing row updated; world-suite coverage of the selection path as
  acceptance evidence.
- **Targeted regression for the flip:** default-on realization is shared behaviour — exercised
  under still-live tracker campaigns (AP7/AP8 replays or current equivalents) with evidence that
  only the intended scan-window decisions changed; no target/tracker authority change.
- **Deletions per import/call-site map,** not filename boundary: shadow-only orchestration and
  artifacts go (`eig_shadow.py`, `realized_comparison.py` + ceremony tests); shared
  score/evaluation kernels still used by authoritative selection move or stay; invariant-bearing
  tests are kept and re-pointed. Escape hatch: slip to SCENE-5 only if wiring genuinely
  overlaps. `legacy_selector_adapter` + the acquire.py selector stay until SCENE-6 (decision 19
  split, unchanged).
- **Stop-point audit (DESIGN_GOALS order):** enumerate every stop/terminal site in `drive/`
  (`evaluate_stop` states, `TERMINAL_ACTION_INVENTORY_EXHAUSTED` / `TERMINAL_CONTROLLER_FAULT`,
  loop routing) against the four-reasons law; each removal enumerated in the PR body;
  targeted-path violations flagged to owner, not silently changed (tracker lives until SCENE-6).
  The discovery path needs no audit here — it was born isolated on `AgendaVerdict` in 4.3.
- **New world (one):** off-path/context-scan campaign world — steered campaign takes a
  diagnostic scan; topology survives; campaign continues. Existing twelve untouched (tuning =
  owner-only).
- **Challenge run exit:** NL discover command → program → book → scene → question-driven
  completion verdict on synth, deterministic scheduler selecting, budget-exhausted honesty and
  AS warm+cold worlds green.
- **Docs in-change:** SCENE_DESIGN §3/§5 (slice row → Implemented; question-layer contract +
  family matrix), design/steering.md, design/eig.md (scan-window = realization layer),
  ARCHITECTURE §15 rows, H-entries; `tests/docs/` claims where machine-checkable.

## Acceptance additions from review triage (fold into the topic-8 matrix)

Transported-arena safety (per the ruled #21 Variant A origin lanes) · multi-report refiner rescue survives a
correct bound · measured-but-fitless fills the map · malformed candidate table fails loudly ·
stable question identity across an added scan · question reopening on lost confidence ·
bounded-truncation predicates do bound arithmetic and refuse vacuous bounds · question-scoped
exhaustion (one closes, another still has actions) · generator defect ⇒ controller fault, never
unresolvable · synchronous build failure cannot steer from the prior scene · compile-explicit
default-on + absent-mode refusal · targeted-path flip regression · full DecisionBasis on every
submitted request.

## Sharpened exit criterion

Given a frozen `discover_and_map` program and a steering-eligible scene revision, ISAR
deterministically derives a replay-identical, content-addressed agenda of stable questions;
assesses them with authority-aware, versioned predicates; generates a complete question-scoped
inventory through the canonical ActionInventory; selects one action via the declared scheduler;
stamps the full DecisionBasis on it; and closes the campaign only through user choice, goal
completion, budget exhaustion, or machine/controller failure. Fitless trusted measurements stay
in the experiment map; untrusted counts get no coverage credit; off-path and cross-condition
observations cannot corrupt an unrelated path; unresolved or insufficiently bounded mass never
satisfies a predicate; the challenge campaign reaches an honest deterministic verdict end to end.

## External-review dispositions (2026-08-13, ChatGPT; Adrian = authority)

- **ADOPTED (genuine catches):** #22 cardinality bound (N×R, pre-pruning, fallback = disable
  margin pruning) + transformation-ablation ledger + build.py-not-store.py; #26 pre-fit
  measurement record with three states and trust status in 4.0; bounded-truncation bound
  arithmetic (#27 does not fully dissolve); compile-time default-on flip (frozen programs must
  never acquire new defaults at read time) + targeted-path flip regression; discovery stop
  routing isolated on AgendaVerdict from first live test; ActionInventory integration instead
  of a parallel scheduler authority; question-scoped exhaustion + generator-defect =
  controller_fault; QuestionSpec/Assessment/AgendaRevision split + stable QuestionSubject
  contract + reopening; declarative versioned predicates; DecisionBasis envelope; steer from
  just-built revision, no silent stale fallback; refuse unimplemented staleness modes;
  consultation answers compile a new program revision, hard gate v1 = pause; opening-book
  coverage invariant on `maximum_initial_spacing_rlu` + declared placement rules;
  path-identity-from-provenance with geometry as fallback; typed ConditionSet generalizing
  `CampaignSliceKey`; lattice-epoch token in partition identity; expected_two contract spelled;
  EstimateParameter v1 scoped to persisted intervals; endpoint extrapolation as a named
  assessment model; PR split (offline scheduling separated from live integration; five PRs);
  deletion by call-site map; family contract matrix; VerifyCompleteness modesty.
- **REJECTED (over-engineering / re-litigation):** the maximal measurement-record field list
  (minimal fields only: identity, provenance, cells, exposure, trust); the demand that #21's
  interim lane MUST land (owner's ruling, presented as the open question, not adopted by
  fiat); prose-scale prescriptions (one-page-per-family documents etc. — proportionate
  versions land instead).
- **RULED 2026-08-13:** #21 Variant A — fail-closed interim lane lands in PR 4.0.

## Deferred register (named, not foreclosed)

Scene-EIG (S5) · unification/tracker deletion/regimes 2–3/campaign-program/2 (S6) · calibration
of all provisional numbers (S7) · symmetry-equivalent coupling + confirmation-scan generation
(one feature, S5-adjacent) · #21 transport contract · #23/#24 · campaign queue / open-ended
fallback · stronger goal compiler · motor-cost model · incremental scene builds · wall-clock-
adaptive staleness · full sample-knowledge map · model hierarchy above phenomena.

## Workflow (Adrian, 2026-08-13)

Staged delivery, review as we go: one branch per PR (`SCENE-4a`…`SCENE-4e`), PRs built
up for owner review, owner merges — same cadence as SCENE-3's 3a/3b/3c. Each PR gets the usual
independent-review gates (plan-verifier before implementation of a slice, fresh verifier on the
claim at the slice boundary) plus `review_files` pre-commit. No slice starts until its
predecessor's review findings are dispositioned.

## Risks

1. Fingerprint/digest churn from new SceneObservation fields → one deliberate re-baseline
   commit in 4.0, enumerated flips.
2. Shared `stopping.py` between discovery and tracker-driven targeted paths → audit flags, owner
   rules; AP7/AP8 replays must stay green or be deliberately re-baselined.
3. Scene build on the critical path → timings recorded per delivery; incremental builds only if
   data demands (deferred).
4. Program-contract additivity under H1 → judged by world suite, no compat shims.

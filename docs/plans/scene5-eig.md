# SCENE-5 — scene-EIG authoritative from birth

> **Status:** draft
> **Owner:** @ammerritt-gh · **Exit criteria:** in the block below
> **Design basis:** owner grill of 2026-08-25 (decisions D1–D9 below; they become dated H23
> amendments in `../design/scene.md` as slices land). Program constitution: H1 — no shadow
> ceremony, validated as ordinary pytest, deterministic fallback never a parallel authority.

**Exit criteria.** (1) The FCC test-sample challenge battery (§4) passes as ordinary pytest:
acoustic-priority efficiency, full-range find-and-classify-both, boundary-convergence resolution
with the temperature A/B. (2) The new fast worlds (§4) and the standing twelve pass in the
not-slow lane. (3) A live discovery decision ledger shows every selection EIG-scored, with the
deterministic scheduler reached only under an injected software fault — and that fault raising
the operator alert. (4) `ARCHITECTURE.md` §15 rows flipped to match; `design/scene.md` and
`design/eig.md` amended in place; this plan deleted in the landing change.

## 1. What changes

Discovery placement stops being heuristic. Today the seven question-bound generators mint one
deterministic proposal each (tile midpoints, weakest-cut midpoints, full-domain windows) and
`scheduler.select()` ranks survivors lexicographically with no information term. After SCENE-5,
each generator emits its **admissible set** — every unmeasured node on the frozen tile lattice
that could serve its question, canonical heuristic pick flagged — and **scene-EIG scores the
union**: for each candidate, the expected information gain about the open questions' own
variables, computed by comparing the hypothesis map (what the scene's phenomena predict at the
candidate, with honest width) against the intensity map (what has been measured). Highest
priority-weighted score wins. The lexicographic scheduler survives unchanged as the
software-fault fallback, ranking canonical picks only.

**The governing razor (owner, 2026-08-25):** every scene-EIG behavior must derive from the
hypothesis-map / intensity-map comparison. Anything that cannot is either the software-fault
fallback or a defect to fix — never a third category.

## 2. Grilled decisions (D1–D9 — settled, not to be re-litigated in-slice)

- **D1 Phenomenon-owned predictive.** Prediction at unmeasured (q, ω) is owned by phenomenon
  objects with a declared **model-family registry**: `curve` (branch and level in one family — a
  level is a curve under a tight zero-slope prior), `elastic`, `quasielastic`, `unknown`
  (maximally diffuse over the intrinsic domain — the universal floor), `continuum` declared and
  refusing per O10. Every physical assumption lives inside one family's versioned entry; nothing
  outside the registry knows any shape. An alternative's linkage chain is same-phenomenon
  *evidence consumed by curve families only*, never the carrier of a belief.
- **D2 Decide-side only.** Beliefs and scores feed EIG exclusively. Nothing enters `ln W(S)`,
  local evidence, or any predicate — the fit-vs-decide invariant (`design/steering.md` §4.4) at
  the scene layer.
- **D3 Zone geometry is a structural input to the curve family.** Fold symmetry at declared
  folds; descent to zero only at declared gapless anchors; fold ≠ reflection (FCC (300):
  integer-indexed, on a fold, extinct — the belief must never predict return-to-zero there).
  Undeclared geometry ⇒ unconstrained belief, named in the `degradations` vocabulary. Never
  inferred from integer indices.
- **D4 Placement authority.** Generators keep sole authority over *what candidates exist* and
  their question attribution; EIG owns *which is taken*. Admissible sets live on the frozen
  q/ω tile quanta (identity-bearing constants from 4.1). Dedup, associations, exhaustion
  accounting, and the five planner outcomes keep their shape. Fallback behavior is
  byte-identical to today over canonical picks.
- **D5 Priority-weighted sum.** Score(a) = Σ_Q w(Q) · I(Y_a ; Θ_Q), each question's MI on its
  own variable set (nuisance variables never leak between questions). Weights are a declared,
  fingerprinted policy map keyed on `QuestionSpec.priority` — family-level defaults in v1, the
  surface accepting per-subject values ("X high, Y low, Z barely") with no schema change.
- **D6 Paranoia budget = operator prior input.** A declared weight on unknown/unexpected mass in
  the hypothesis map, shipped with a decent default beside the priority weights. It makes
  verification scans win the ranking honestly; it is not a ritual and not a threshold.
- **D7 Fallback = software faults only.** No epistemic state is unscoreable (D1's unknown floor);
  unresolved-cluster retained-set masses steer without ceremony; physical infeasibility is the
  geometry-decline machinery's, not scoring's. Closed reason vocabulary
  (`scene_revision_missing`/unreadable, registry-manifest fingerprint mismatch, scoring
  exception), persisted in `DecisionBasis`, **plus a loud non-blocking GUI alert in operator
  words**. The beam never idles; the campaign continues on canonical placements.
- **D8 Off-path v1 = −E only.** The organic `context_probe` producer mints detailed-balance
  confirmation probes (the one equivalence already functional in the engine). ±q and cross-zone
  equivalences are pinned behind lattice-as-hypothesis (DESIGN_GOALS, Sample Knowledge) — chiral
  phonons are the named ±q counterexample; nothing here may foreclose that work. Exposure per
  point stays `discovery_monitor_ncount`: cost pricing is SCENE-5.5 (H21(a)), so "worth it" is
  purely relative rank in this program.
- **D9 Acceptance is behavioral.** Worlds assert decision *sequences* the map comparison
  produces and heuristics cannot; no efficiency numbers (SCENE-7's benchmark). The fallback
  drill is an ordinary test, not a headline world.

## 3. Slices

Each slice gates through plan-verifier before implementation and a fresh outcome verifier after;
serialized-contract probing per the W2 lesson (verify what is *persisted*, not dataclass
surfaces).

- **5.0 Phenomenon belief layer** (offline; no controller change). `isar/scene/phenomena.py`:
  the family registry with fingerprinted manifests; belief construction per scene alternative
  from persisted candidate rows + declared `[hypotheses.*]` + `SampleKnowledge` zone geometry
  (D1/D3); seeded predictive draws producing expected Poisson counts for a candidate
  `(node, window, ncount)` under each alternative. The scene layer is deliberately
  amplitude-blind (`CandidateRow` persists no amplitude and no Γ), so the **intensity scale and
  linewidth are declared, fingerprinted priors inside each family's registry entry** (D1's
  every-assumption-lives-in-one-entry rule) — never a fitted amplitude read from the scene — and
  5.0 extends **no persisted contract**: neither the candidate-row delivery contract nor
  `isar.scene-revision/1`; the diff adds `isar/scene/phenomena.py` plus tests only. Resolution
  precedence: alternative's claimed identity class → matching declared hypothesis → agnostic
  floor. Acceptance: predictive unit tests including the FCC fold (no return-to-zero at the
  extinct index), Bose weighting on both energy sides, degradations on undeclared knowledge,
  seeded determinism; the count scale traces to the family entry's declared prior (changing that
  entry changes the prediction; changing anything else does not); and **predictive width is
  pinned** — the `unknown` family is maximally diffuse over the declared intrinsic domain, and a
  curve-family belief is strictly wider at an unmeasured node away from evidence than at a node
  a persisted candidate row places, asserted by a test that fails when the width term is stubbed
  constant.
- **5.1 MI scoring engine** (offline). Expanded slice spec, gated 2026-08-25:

  *Policy (`isar/schema/question_policy.py` + `isar/schema/campaign_program.py`):*
  `isar.question-policy/3` retires `/2` by name on the `/1` precedent (refusal with a recompile
  message; H1, no read-time defaulting). Born in `/3`: `priority_weights` — a map from each
  priority integer in `priorities`' range to a positive finite weight (D5's exchange rates;
  shipped default decreasing in priority number, SCENE-7 calibrates), `paranoia_unknown_prior`
  — the operator's declared prior that an unsurveyed tile holds something (D6; unit interval,
  shipped default modest), and `mi_n_draws` — the seeded predictive draw count per alternative
  (positive integer; it changes decisions, so it is policy, not a hidden constant). The retired
  set becomes `("isar.question-policy/1", "isar.question-policy/2")` — both stay refused by
  name, each with a recompile message naming `/3` as the successor. Blast radius:
  `tests/drive/fixtures/program_discover_challenge.json` embeds `/2` literally and is
  regenerated through the compiler; `tests/schema/test_question_policy.py` pins the refusal
  message by regex and is updated to cover BOTH a `/1` and a `/2` block against the new
  messages; tests constructing policies via `resolve_policy()` follow automatically.

  *Relevance registry + scorer (`isar/scene/eig.py`, new):* a declared, fingerprinted
  RELEVANCE registry beside the family registry — one extractor `(id, version)` per question
  family mapping a cluster's alternatives to the question's own variable classes:
  ConfirmPresence → in-band placeable-mode presence (binary); ClassifyIdentity → claimed
  identity label; ConnectTrack → endpoint linkage reachability (binary, mirroring
  `_track_connected`'s walk); EstimateParameter → the matched mode's ω tile on the frozen
  `emergent_omega_tile_meV` quantum (the eig.md QoI-bin precedent); TestModelMismatch →
  unknown-identity assignment (binary). DiscoverRegion maps to a synthetic two-state variable
  at an unsurveyed node — present (unknown-family predictive + background) vs absent
  (background only), prior = `paranoia_unknown_prior` — which is D6 entering the arithmetic:
  coverage value IS presence uncertainty where nothing has looked. VerifyCompleteness declares
  extractor `none/1` (MI 0): exposure fill is deterministic progress, and its canonical
  proposals win only when everything informative is exhausted — by fingerprint tiebreak,
  recorded as the declared v1 posture. An alternative's expected-count vector is the SUM of
  its beliefs' predicted rates plus background — Poisson superposition of independent
  scattering channels, so a two-mode alternative and a one-mode alternative separate exactly by
  the second mode's predicted counts; `build_beliefs` cannot emit a `continuum` belief in v1
  (no component kind maps to that family — asserted by test), so `ContinuumRefusesPrediction`
  stands untouched for direct callers and never interrupts scoring. The background rate is a
  declared, fingerprinted constant in the relevance registry's own manifest
  (`background_counts_per_point` at the family registry's reference exposure, scaled linearly
  with the design's ncount) — always present, no None case, used identically in both states of
  the DiscoverRegion Bernoulli. Estimator: exact outer sum over the enumerated alternatives
  with their payload posteriors; `mi_n_draws` seeded Poisson draws per alternative from its
  COMPOSED rate vector (seeds content-derived from design × cluster × question × base seed —
  replay-identical); MI = H(g) − E_Y[H(g | Y)] per cluster, additive across clusters, zero for
  a single-class partition. Score(design) = Σ over OPEN questions of
  `priority_weights[spec.priority] × MI_Q`; each question's MI is computed on its own extractor
  classes only (nuisance variables never leak). Deliverable API — the scorer's full input set,
  stated: the stored scene payload (its embedded `sample_knowledge` block lifted into
  `SampleKnowledge`), the stored agenda payload (whose embedded `resolved_policy` supplies
  weights, paranoia, and draw count with no store read), the CALLER-SUPPLIED declared
  `[hypotheses.*]` mapping (neither payload carries it; offline callers and tests pass it
  directly, and slice 5.2 owns wiring it from the frozen step's hypothesis block / card-lifted
  declarations), the family registry, the design list, and a base seed → a ranked `ScoreTable`
  with itemized per-question terms, the relevance-registry and family-registry fingerprints,
  and the policy fingerprint.

  *Phenomena amendments (dispositions of the 5.0 advisories, curve family version → 2):*
  `reference_counts` becomes integrated spectral weight — the predictive normalizes the peak by
  its width so a retuned linewidth redistributes counts instead of minting them (delta advisory
  B); within-span position uncertainty gains the Lipschitz slope-cone term
  `min(d_left, d_right) × slope` in quadrature (advisory A2 — a mid-gap node now reads
  honestly uncertain, so the MI values densifying a sparse track); the mirror test pins one
  more anchor value (delta advisory A).

  *Acceptance:* the temperature A/B behavioral unit (an anti-Stokes-side design's MI collapses
  at low T through the Bose factor in the predictive — no temperature branch in scoring code);
  the nuisance guard (identity ambiguity with no open ClassifyIdentity question moves no
  ranking; opening the question makes the discriminating design score); the priority-weight
  trade (a large low-weight gain outranks a marginal high-weight one, and retuned weights flip
  it); paranoia monotonicity over the declared background source (zero prior → MI exactly 0; a
  higher prior → strictly higher gap-node score); a design scored WITH a declared hypothesis
  differs from the same design scored with none (the hypotheses input is live);
  a multi-belief alternative separates from a single-belief one under the sum-composition rule;
  the no-continuum-from-build_beliefs assertion; cross-cluster additivity; byte-identical
  replay of the full table on the same seed; spectral-weight conservation under linewidth
  retune; `/3` refusing BOTH `/2` and `/1` by name with recompile messages, and field
  validation of the three new declarations.
- **5.2 Controller integration.** Generators emit admissible sets + canonical flag (D4);
  `plan_next_action` scores when a scene revision exists, falls back per D7 with reason +
  GUI alert wiring through the campaign monitor; `DecisionBasis` carries the score table,
  belief-registry fingerprint, weight-map fingerprint; decisions replay byte-identical on the
  same seed. Acceptance: seeded decide-loop replay world; the fallback drill; fallback ranking
  proven byte-identical to the 4.2b scheduler over canonical picks.
- **5.3 Organic −E producer** (D8). The `context_probe` producer minting detailed-balance
  confirmation probes priced by the same comparison; world-level off-path end-to-end (closes
  H22's named deferral).
- **5.4 Acceptance closure.** New fast worlds: optic-pivot (acoustic absence reshapes the
  posterior; the optic-band probe wins organically — H22(f)'s sequence), FCC-boundary,
  −E-producer behavior. The FCC test-sample challenge battery (§4). Docs: H23 amendments in
  `design/scene.md`, `design/eig.md` boundary note, `ARCHITECTURE.md` §15, HYPOTHESES.md status
  line; this plan deleted in the same change.

## 4. Acceptance battery (owner-specified, 2026-08-25)

On the FCC test sample (TAVI deterministic simulation), as slow-lane pytest:

1. **Acoustic-priority run** — "measure the acoustic phonon here, priority high": placement
   maps the branch efficiently over the declared range; the optic is measured only to
   exclusion/differentiation, then dropped from spending.
2. **Full-range discovery** — "map everything in the domain": both phonons found and
   classified; neither starved.
3. **Boundary convergence** — near the extinct (3,0,0)-analog fold where the branches approach:
   the ranking buys discrimination (window placement / −E probe) rather than re-measuring one
   point; run at two temperatures, the chosen probes differ because the Bose factor rewrote the
   predictive — asserted with no temperature branch anywhere in steering code.

Plus fast worlds per D9 and the standing twelve unchanged.

## 5. Risks and levers

- **Compute per decision** (scoring dozens–hundreds of lattice candidates × alternatives ×
  seeded draws): measured before 5.2 ships; the honest lever is coarsening the scored lattice,
  never reverting to midpoints. Cadence is per-delivery, minutes-scale.
- **Candidate-set expressiveness**: if the frozen lattice proves too coarse for a placement the
  integral wants, the fix is a denser declared quantum or an eighth generator minting
  EIG-suggested nodes *inside* the existing contract — named watch item, not v1 scope.
- **Belief misspecification**: the curve family under-claims curvature between sparse points; a
  soft mode reads as surprisingly informative data, which is the honest failure direction
  (attracts measurement rather than repelling it). Calibration of family priors is SCENE-7's
  battery.
- **Within-span width is flat** (5.0 verifier advisory A2): between two sparse anchors the
  curve family's position uncertainty interpolates the endpoints and adds no
  interpolation-uncertainty term, so a node midway between distant anchors reads as
  well-predicted. Owner disposition due at 5.1, where the MI engine starts consuming the width.
- **`reference_counts` is peak height, not integrated intensity** — RESOLVED in 5.1: spectral
  weight now conserved under linewidth retune (family versions bumped to 2); the mirror test
  pins the extra anchor value. Verified by the 5.1 outcome verifier.
- **5.1 verifier notes for SCENE-7 / 5.2** (all P4, correct-but-worth-knowing): the paranoia
  MI is concave in the prior (a Bernoulli's MI peaks at p≈0.5 — "higher prior, higher score"
  holds on (0, ~0.5], which covers every sane declaration); at shipped defaults a gap node
  scores ~0.02 nats vs a presence question's ~0.69, so D6's verification scans need the
  SCENE-7 calibration of `paranoia_unknown_prior` and the unknown family's weight before they
  genuinely compete; the predictive's per-meV density adds to a per-point background, so a
  design's signal-to-background varies with grid density — 5.2's compute/behavior measurement
  pass should pin the per-point integration convention (density × resolution element) before
  live rankings depend on it.

## 6. Non-goals

Cost/per-neutron pricing and absolute worthwhileness thresholds (SCENE-5.5, H21(a)); the
targeted-lane single-engine swap (SCENE-6); the `continuum` plugin (O10 door stays declared);
±q and cross-zone equivalence, lattice-as-hypothesis, alignment correction (pinned, DESIGN_GOALS
Sample Knowledge); any change to scene scoring, `ln W(S)`, predicates, or the question
vocabulary.

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
- **5.1 MI scoring engine** (offline). Θ_Q relevance mapping per question family (declared,
  fingerprinted, beside the generator registry); per-candidate per-question MI over the
  enumerated alternatives (exact outer sum, seeded draws only inside the predictive);
  priority-weight map + paranoia weight in a `question-policy/3` revision (D5/D6). Deliverable:
  score a stored scene + agenda offline into a ranked table with itemized per-question terms.
  Acceptance: the temperature A/B behavioral unit (anti-Stokes probe value collapses when Bose
  kills it — no code branch saying so); the nuisance guard (an uncertain unasked-about variable
  moves no ranking).
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

## 6. Non-goals

Cost/per-neutron pricing and absolute worthwhileness thresholds (SCENE-5.5, H21(a)); the
targeted-lane single-engine swap (SCENE-6); the `continuum` plugin (O10 door stays declared);
±q and cross-zone equivalence, lattice-as-hypothesis, alignment correction (pinned, DESIGN_GOALS
Sample Knowledge); any change to scene scoring, `ln W(S)`, predicates, or the question
vocabulary.

# SCENE — target-free multi-phenomenon discovery

> **Lifecycle:** Active plan
> **Authority:** canonical for the scene layer (catalogue, scene posterior, question layer, scene-EIG)
> **Provenance:** hand-written
> **Owner:** Adrian Merritt
> **Exit criteria:** SCENE-7 acceptance — 12 fast adversarial worlds pass as ordinary pytest;
> slow-lane calibration batteries (label calibration, log/Brier, FDR, interval coverage) pass at
> agreed thresholds; efficiency benchmark vs dense survey + three targeted campaigns recorded;
> DESIGN §15 rows flipped to match reality.

Constitution: DECISIONS **H1**. Program plan of record: the 22 grilled decisions of 2026-08-07/08
(H-series entries land per slice). Challenge command the program must satisfy end-to-end on
synthetic worlds: *"Scan the region from (2,0,0) to (3,0,0) up to 10meV, map out all the
phenomena of interest."*

## 1. What changes

ISAR's unit of reasoning moves from "which fitted peak is the target branch at this Q?" to "what
physical scene most plausibly explains everything measured in this region, and which measurement
best distinguishes the remaining scenes?" Identity (Bragg / acoustic / optic / unknown) is
established by Bayesian evidence, never by structural defaults like "lowest is acoustic".
Nothing new may be phonon-shaped: the catalogue drives model enumeration, observation models are
a declared layer, and unknown-physical / no-signal / spurious are distinct outcomes.

## 2. Architecture (the loop)

```
natural-language goal ──goal compiler──> isar.campaign-program (frozen; only accepted contract)
      └─ discover_and_map intent: path region, intrinsic ω-domain, catalogue profile, profiles
scan data ──catalogue-driven grammar──> structure × identity candidates
      └─ identity-conditional Laplace evidences (catalogue priors applied exactly once, locally)
      └─ content-addressed interpretation artifact + results.csv projection columns
interpretation artifacts ──> cluster-factorized scene posterior (report/scene/ revisions)
      └─ structural/cross-scan factors ONLY + per-factor evidence ledger
scene posterior ──> open scientific questions (completion predicates → generalized stop)
      └─ scene-EIG (MI on question-relevant variables, additive across clusters) selects action
      └─ scan-window EIG realizes it (existing fence intact) ──> instrument / TAVI
```

Load-bearing rules, each naming the failure it prevents:
- **Identity priors apply exactly once, at local evidence.** Scene factors are structural and
  cross-scan (presence, topology, connectivity, zone-centre approach, detailed-balance partner
  consistency, reciprocal-lattice localization); a scene factor that re-scores parameter
  compatibility ("near the acoustic prediction") double-counts the same fitted energy and
  inflates every identity posterior.
- **"Bragg" is a scene-level identity.** The local catalogue entry is generic lattice-blind
  `elastic`; reciprocal-lattice localization is cross-scan knowledge. A local "Bragg" entry would
  smuggle a scene factor into local priors.
- **"Unknown" is a real competitor** — the maximally diffuse catalogue entry, with an honest
  evidence value — never a fallback label.
- **The posterior is cluster-factorized and deterministic.** Product over ambiguity clusters;
  per-cluster explicit alternative sets, exact scoring, recorded pruning; replay-identical
  revisions. The cluster is the future representation seam (ToF-scale data may someday swap one
  cluster's local representation); a flat global scene list is forbidden. Sampling is confined to
  seeded predictive draws inside EIG integrals.
- **Laplace evidence validity is checked, never assumed.** Boundary-pinned MLEs mark
  `laplace_invalid` and escalate (dynesty) rather than reporting a silent Gaussian volume —
  with one carved-out exception (H9(b)/H10(b)): a fit pinned ONLY at the Γ floor takes
  `laplace_truncated`, rung-1 Laplace with the gradient- and offset-aware boundary-mass
  factor, because the floor is a modelling artifact and the boundary integrable. Amplitude→0,
  Ω_d window-edge, Γ-ceiling, and unknown-pin fits all still escalate. Evidence comparisons
  require an identical data+mask arena (arena hash asserted) — the Baker–Cousins
  saturated-model constant only cancels on same-masked data.
- **No shadow ceremony (H1).** Scene-EIG is authoritative from birth, validated by the world
  suite as ordinary pytest; the deterministic question scheduler is the permanent fallback when
  scoring is unavailable.

### 2b. Scene scoring contract (SCENE-3)

For observation arena `j` with local candidates `h`, SCENE-1 persists `lnZ(j, h)`. A
scene alternative `S` — one coherent per-cluster assignment — chooses exactly one active
candidate `h_j(S)` per participating arena, plus discrete scene variables
(component→phenomenon assignment, connectivity/topology states, visibility states). Its
unnormalized score is

    ln W(S) = Σ_j lnZ(j, h_j(S)) + Σ_f ln φ_f(S)

and the cluster posterior is `P(S|D) = exp(ln W(S)) / Σ_{S'} exp(ln W(S'))`, normalized
within the cluster. Every scene factor `φ_f` declares probabilistic semantics —
`scene_prior` or `structural_likelihood` — never an undeclared heuristic bonus.

Invariants, each preventing a named failure: one observation arena contributes its local
evidence exactly once (a pooled master supersedes its member scans; audit rows never
score); normalized per-scan posteriors are never multiplied together (that arithmetic
double-counts the local priors); a multimode structure's lnZ enters once even when a
scene alternative assigns its components to different phenomena (the modes were fitted
jointly to the same counts — splitting or duplicating the evidence per component would
manufacture evidence); a gap in coverage contributes no update in either direction. The
scene operates when local candidates carry `identity=None` — identity may arise purely
from scene-level evidence.

**Completeness (H7).** Only an arena whose local candidate comparison FINISHED may score.
A fit whose evidence escalation budget expired with a contender unresolved publishes no
Bayesian winner, and the lnZ values that survive are not the arena — scoring them turns an
unfinished comparison into a confident posterior. Such an arena is experiment-map-only:
footprint recorded, rows embedded, no term and no cluster membership. The same applies to
an arena routed to a non-canonical evidence sink (H12(b)), whose channel the scene routes in
a later slice.

**Pruning and truncation are bounded, never optimistic (H12(c)).** Margin pruning and the
enumeration cap both decide on base `Σ lnZ` before any factor term exists, so both are
permitted only within the declared span `Σ_f (hi_f − lo_f)` of the active scoring factors'
`score_bound`s: the effective margin is `margin_nats + span`, the recorded mass bounds are
widened by it, and one unbounded scoring factor disables margin pruning outright and makes a
truncated cluster record `truncation_unbounded` with no bound at all. An unbounded truncation
is an unresolved cluster, not a narrowed one. Exact enumeration is the default posture.

**Score-term ablation (H12(f)).** The per-cluster `score_ablation` lists give each factor's
influence by removing its SCORE terms over the FIXED alternative set — index-aligned with
`alternatives`, as the payload's `score_ablation_alignment` declares. A factor's `couples`
edges (and so the cluster partition and the hypothesis space itself) are deliberately not
removed; that is why the operation is not called leave-one-factor-out.

**What a scene alternative is after PR 3b.** An alternative is a per-cluster CANDIDATE
ASSIGNMENT — one surviving candidate per participating arena — *together with* a LINKAGE: which
component at one arena is the same track as which component at an adjacent one, as
`(component_id, component_id)` pairs. Two alternatives may share an assignment and differ only in
their linkage; that is the branch-crossing ambiguity, and since they share the same local fits
their base `Σ lnZ` is identical, so only factor terms can separate them. The default linkage is
nearest-ω chaining within the resolution width; alternative linkages are proposed by a factor's
`propose` hook and are capped per assignment (`MAX_LINKAGE_VARIANTS`), with the truncation
recorded (`linkage_truncated`) — linkage variants share a base, so no dropped-mass bound is
available for them and none is claimed.
Visibility states (`observed`, `searched_no_signal`, `censored_outside_footprint`,
`blocked_unresolved`) are derived per component against the neighbouring footprint and annotate
the leading alternative in the cluster payload. A component is `observed` at a neighbour when the
alternative's LINKAGE puts it there, never by an independent proximity match — the linkage is
one-to-one per adjacent arena pair, so one observed peak supports at most one claimed mode.
Component→phenomenon assignment remains a later extension.

**What a component is, and what a footprint covers.** Both are read from what the delivery
PERSISTED, never reconstructed from a display string or an envelope. Each candidate row carries
ordered component descriptors (`kind` ∈ `mode` / `elastic` / `quasielastic`, plus the mode's `slot`
into that row's `omegas`), derived at the writer from the enumerated structure the candidate was
fitted as; a label is a stable identifier and nothing decides physics by parsing it. Each row also
carries `energy_bands`: the measured CELLS of the kept bins, merged into runs, where
`energy_window` is only the kept POINT extent. A QC mask or an excluded region in the middle of the
axis is a HOLE, so absence inside it is `censored_outside_footprint` rather than evidence. Cell
adjacency is read off the ORIGINAL grid — each kept bin covers midpoint to midpoint, two kept cells
with no dropped bin between them share a boundary and merge — so there is no tunable gap threshold
and there must not be one: any factor times a global spacing statistic mis-reads a stitched
coarse-to-fine axis in one direction and a comb rejection (uniform kept spacing, holes everywhere)
in the other. Both fields are inputs to `observation_fingerprint`, so changing the derivation moves
every revision digest — which is the intended behaviour, not a hazard to work around. A workspace
whose rows predate either field is refused with a re-deliver instruction (H1: no tolerant fallback
between the stored evidence and `ln W(S)`).

The two non-mode kinds are kept apart because they answer different questions. Both obscure a band
at the elastic line, so either makes a low-ω absence `blocked_unresolved` — that is
`blocking_present`. Only `elastic` is a Bragg-adjacent claim, so only it satisfies the zone-centre
endpoint prerequisite and only it is classified against the reciprocal lattice; a quasielastic
feature is diffusive scattering, and asking where it sits relative to a reflection is the wrong
question.

**Declared sample knowledge, and the factors that read it (H13).** `build_scene(..., sample=)`
takes a `SampleKnowledge` — the scientist's DECLARED zone period, Bragg reflections along the scan
path, and the lattice epoch in force — lifted off the experiment card by
`isar.workspace_api.scene.sample_knowledge` (read-only; the card's policy half stays
hand-authored). It is recorded in the revision's `sample_knowledge` block and digested with it, so
a scene scored blind to the lattice can never look replayable against one that was not. Nothing is
required: an undeclared field leaves the capability that consumes it inert and names itself in
`sample_knowledge.degradations`, never a guessed default. Zone geometry and the reflection list are
INDEPENDENT declarations — a fold position is not thereby a reflection (FCC (300) is
integer-indexed, sits on a fold, and is extinct), so no space group, structure-factor rule, or
integer-index shortcut derives one from the other.

Two factors read it, and neither scores — so neither costs the builder its pruning:

- `zone_centre_approach` assigns `structural_endpoint` through the `refine_visibility` hook, where
  a declared gapless branch legitimately has nothing to show: the neighbouring arena sits at a
  zone centre of the declared period, an ELASTIC component is chosen there (a quasielastic one is
  no response for a branch to be swallowed by), and the mode's energy is within a plausible descent
  to zero. `blocked_unresolved` says *unresolved pending better data*;
  `structural_endpoint` says *the declared physics terminates the track here*. `presence` keys on
  `searched_no_signal` alone, so the absence of a penalty at an endpoint is emergent from the
  visibility grammar rather than special-cased into a score function.
- `reciprocal_lattice_localization` classifies each ELASTIC component `bragg_compatible` /
  `off_lattice` / `unclassified` (no reflections declared) / `unplaceable_q` (declared, but this
  arena has no placeable q) through the `classify` hook, recorded per cluster under
  `classification`. Modes and quasielastic components are absent from the map rather than
  unclassified — they are not candidate reflections at all. It classifies and does not score: elastic intensity is ubiquitous, so an
  off-lattice elastic feature is not improbable, merely not-Bragg.

A refiner may only replace an absence state — never `observed`, never
`censored_outside_footprint` — and the builder enforces that rather than trusting each factor.

**Coverage coupling (carried item 15).** A cluster edge needed a component on BOTH sides, so an
arena whose surviving candidates were all background-only emitted nothing and could never join a
cluster: a track disappearing into a searched, covered, entirely empty neighbour went unpriced,
while the same track disappearing into a neighbour that happened to hold something else was
priced — inverted exactly where absence evidence is strongest. The `couples_arena` hook couples a
mode to a whole neighbouring ARENA through its footprint, gated on coverage (no coverage is no
claim, so it grows no cluster). Only scoring arenas participate, so an H7-incomplete arena is
never resurrected by it.

**A linkage refines an assignment; it never adds prior mass to it.** An assignment that happens
to admit two linkage readings must not outweigh a rival assignment that admits one — that would
be ln 2 nats no evidence paid for, set by an enumeration knob (the crossing proposer's
reachability tolerance) rather than by the data. Each `(assignment, linkage)` entry therefore
carries the builder's declared `linkage_prior` term, `ln(1/n)` over that assignment's `n`
enumerated linkages, itemized in the ledger and declared in the factor manifest like any other
term. The assignment marginal is then exactly what linkage-free scoring would give, while the
linkage ambiguity stays represented inside it. Every emitted link set is also structurally
validated: a component continues as at most one track per adjacent arena, and a proposer that
emits otherwise fails the build rather than publishing an impossible reading.

## 3. Slices

| Slice | Goal | Status |
|---|---|---|
| SCENE-0 | Constitution (H1) + synthetic world-suite foundation (12 worlds, ensemble runner, world manifests) | Implemented (2026-08-08; PR #11 merged at `edd6bf7`) |
| SCENE-1 | Catalogue, identity-conditional Laplace evidence, global grammar (bg-only + elastic+bg first-class, `elastic_only` verdict), observation footprints, interpretation artifacts — three sequential PRs | PR-a Implemented (H3, 2026-08-08: six-entry catalogue drives enumeration; nulls first-class; `elastic_only` with the 0.5 cap + LRT gate; transport/prior/variant declarations dormant); PR-b Implemented (H4, 2026-08-08: per-structure Laplace evidence + validity ladder, posterior drives selection, AIC persisted diagnostic, `fit_selection_semantics/2` replaces `/1`, universal Bose factor, dominance pruning + scoped escalation n_live; identity axis deferred by ruling); PR-c Implemented (H9, 2026-08-09: `user_declared` origin; `laplace_truncated` Γ-floor rung, its boundary-mass factor gradient- and offset-aware rather than half-Gaussian; executable constrained variants — near-degenerate acoustic twin as an additional candidate; local identity axis with the no-dilution rule, records-only BA disagreement; candidate rows persist at_limit/variant/identity/effective_constraints and per-label lnZ as the scene store's aggregation input. Deferred by H9(e): the near-degenerate pair's amplitude tie, out-of-window treatment → SCENE-3, identity prior payloads → owner) |
| SCENE-2 | `isar.campaign-program/1` sole contract; old envelope unreadable by design | Implemented (H2, 2026-08-08; PR #12 merged at `058fb40`) |
| SCENE-3 | Scene layer core: clusters, structural factors, revisioned scene store, workspace_api read surface; tracker contracts ported as specifications | Partial — PR 3a Implemented (H11/H12, 2026-08-10: typed observation adapter over persisted candidate rows, ambiguity clusters, exact per-cluster scoring with recorded pruning/truncation bounds, score-term ablation ledger); PR 3b Implemented (H13, 2026-08-10: four live scene factors, declared sample knowledge from the card, structural-endpoint visibility, footprint-coverage coupling, endpoint/off-lattice/unknown-extra acceptance worlds); scene store, read surface, invocation sites, and the tracker-contract port = PR 3c Planned |
| SCENE-4 | Question layer, deterministic opening book, generalized stop; first challenge run; scan-window authoritative default-on; shadow machinery deleted | Planned |
| SCENE-5 | Scene-EIG authoritative from birth | Planned |
| SCENE-6 | Single-engine swap: targeted campaigns as pinned question sets over scenes; tracker deleted; `campaign-program/2` question-native targeted payload | Planned |
| SCENE-7 | Calibration batteries + efficiency benchmark; program closeout | Planned |

## 4. Acceptance worlds

Fast lane (ordinary pytest), each killing a named failure mode: canonical Bragg+acoustic+optic;
no-acoustic (gapped lowest — kills "lowest = acoustic"); pure elastic (no DHO invented); blank
(bg-only wins); unknown extra mode (unknown survives as competitor); acoustic endpoint under
Bragg (structural endpoint, no fitted zero-energy phantom); branch crossing (identity follows
global evidence, not local rank); anti-Stokes warm + cold (merged when visible, never chased when
Bose-dead); off-lattice elastic (`elastic_only` locally, scene says not-Bragg); model mismatch
(unknown + TestModelMismatch, never a forced label); budget-exhausted honesty (unresolved
posteriors reported unresolved); same-seed replay (identical revisions, inventories, selections).
Slow lane: calibration ensembles and the efficiency benchmark.

## 5. Question layer

DiscoverRegion, ConfirmPresence, ClassifyIdentity, ConnectTrack, EstimateParameter,
TestModelMismatch, VerifyCompleteness. Each question = completion predicate + concerned scene
variables + campaign priority + generator families (registered in the existing
GENERATOR_REGISTRY pattern). `evaluate_stop` generalizes to open-questions accounting; a targeted
campaign is a pre-pinned question set. Scene-EIG computes mutual information on the
question-relevant scene variables only — the guard against an uncertain nuisance variable
dominating acquisition.

## 6. Persistence

Per scan: the persisted candidate rows on `FitOutcome`/results.csv are the scene's input
contract (H11(a), H12(e) — no separate interpretation artifact; scene revisions embed the
exact rows they consumed, with completeness status and conditioning provenance). Per scene
update: content-addressed revision under `<workspace>/report/scene/` with a current-pointer
and the per-factor evidence ledger (score-term ablation deltas, H12(f)). The GUI reads all
of it through `workspace_api` only (Qt-free; hard rules 6/7 unchanged).

## 7. Status discipline

This document is the canonical scene-layer reference. Capability status lives in DESIGN §15 and
must match reality per slice; per-slice rulings land as H-series entries in `docs/DECISIONS.md`.

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
      └─ persisted candidate rows on FitOutcome/results.csv (no separate artifact — H11(a))
persisted candidate rows ──> cluster-factorized scene posterior (report/scene/ revisions)
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

A `refine_visibility` factor (a REFINER — `zone_centre_approach` in the live registry) is not a
coupling-only factor even though it declares no `score`: it rewrites what a scoring factor
reads (`searched_no_signal` → `structural_endpoint` moves `presence` by `-PRESENCE_MISS_NATS`
per rewritten report), so it owes the span the same honesty (#22). A refiner declares
`rescue_bound_per_report` — the most nats ONE refined report can move — and the builder widens
the span by `rescue_bound_per_report × N`, where `N` is a CONSERVATIVE upper bound on refinable
`(mode, adjacent neighbour)` reports counted over every USABLE candidate of the WHOLE build,
BEFORE any pruning (`build._max_refinable_reports`): a global, pre-partition count is a superset
of any one cluster's true count, so it only widens the margin, never narrows it. One rescued
report per alternative is not the bound — an alternative with several endpoint-refinable reports
can move `N × rescue_bound_per_report` nats, and the widened span accounts for all of them, not
one. A refiner that will not declare `rescue_bound_per_report` is treated exactly like an
unbounded scoring factor: the whole span collapses to `None`, joining the same
`truncation_unbounded` semantics — the fail-safe posture for a refiner whose worst case cannot be
bounded.

**Score-term ablation (H12(f)).** The per-cluster `score_ablation` lists give each factor's
influence by removing its SCORE terms over the FIXED alternative set — index-aligned with
`alternatives`, as the payload's `score_ablation_alignment` declares. A factor's `couples`
edges (and so the cluster partition and the hypothesis space itself) are deliberately not
removed; that is why the operation is not called leave-one-factor-out.

**Transformation ablation (#22), a distinct ledger.** A refiner declares no score term to zero
out, so `score_ablation` cannot answer what its visibility rewrite did — the per-cluster
`transformation_ablation` ledger does: for each `refine_visibility`-bearing factor, visibility is
recomputed WITHOUT that refiner's hook, every scoring factor that consumes visibility
(`presence`) is rerun against the unrewritten reports, and the result is renormalized over the
SAME fixed alternative set (no re-enumeration) — index-aligned with `alternatives`, per the
payload's `transformation_ablation_alignment`. This is how the revision exposes how much an
endpoint reading moved posterior odds independently of `presence`'s own ledger entry (issue #22's
acceptance).

**What a scene alternative is after PR 3b.** An alternative is a per-cluster CANDIDATE
ASSIGNMENT — one surviving candidate per participating arena — *together with* a LINKAGE: which
component at one arena is the same track as which component at an adjacent one, as
`(component_id, component_id)` pairs. Two alternatives may share an assignment and differ only in
their linkage; that is the branch-crossing ambiguity, and since they share the same local fits
their base `Σ lnZ` is identical, so only factor terms can separate them. The default linkage is
nearest-ω chaining within the REACH of a consecutive path edge — the resolution floor plus the
declared dispersion slope over Δq, never the resolution alone; alternative linkages are proposed by
a factor's `propose` hook and are capped per assignment (`MAX_LINKAGE_VARIANTS`). A truncated
cluster records `linkage_truncated` and the exact share of prior mass the cap dropped
(`linkage_dropped_prior_mass`, `(n_enumerated − n_kept)/n_enumerated` for the worst assignment),
and it joins the `truncation_unbounded` lane: linkage variants share a base `Σ lnZ`, so there is
no base ordering to bound the dropped readings' factor terms by, and a dropped reading can score
differently from every kept one. Such a cluster's marginals are unresolved, never narrowed.
Visibility states (`observed`, `searched_no_signal`, `censored_outside_footprint`,
`blocked_unresolved`, `structural_endpoint`) are derived per component against the neighbouring
footprint and annotate the leading alternative in the cluster payload; a report demoted by the
detectability gate or terminated by a structural reading carries the `reason` that did it. A
component is `observed` at a neighbour when the alternative's LINKAGE puts it there, never by an
independent proximity match — the linkage is one-to-one per adjacent arena pair, so one observed
peak supports at most one claimed mode.
Component→phenomenon assignment remains a later extension.

**One null, one charge (H14(a)).** Every ln-term is charged against one `(chosen mode at arena i,
adjacent arena j)` report; there is exactly one such row per pair, it carries exactly one
visibility state, and exactly one factor reads it — `presence` is the only factor in the live
registry with a `score`. `connectivity` supplies `couples` and `propose` and declares `score=None`:
its `CONNECT_BREAK_NATS` charged the same null `presence` charges as a miss, so it was deleted
rather than re-homed, and the effective penalty on a vanishing track is `PRESENCE_MISS_NATS` = −1
nats per (mode, neighbour) with `PRESENCE_MATCH_NATS` = +0.5 for a linked continuation. Those two
weights are provisional v1 declarations tuned by the SCENE-7 calibration battery; they live in the
factor's `config`, so a retuning moves the registry fingerprint.

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
path, steepest dispersion slope (`dispersion.max_slope_meV_per_rlu`, meV per rlu) and the lattice
epoch in force — lifted off the experiment card by
`isar.workspace_api.scene.sample_knowledge` (read-only; the card's policy half stays
hand-authored). It is recorded in the revision's `sample_knowledge` block and digested with it, so
a scene scored blind to the lattice can never look replayable against one that was not. Nothing is
required: an undeclared field leaves the capability that consumes it inert and names itself in
`sample_knowledge.degradations`, never a guessed default. Zone geometry and the reflection list are
INDEPENDENT declarations — a fold position is not thereby a reflection (FCC (300) is
integer-indexed, sits on a fold, and is extinct), so no space group, structure-factor rule, or
integer-index shortcut derives one from the other.

Two factors read it, and neither scores — so neither costs the builder its pruning:

- `zone_centre_approach` assigns `structural_endpoint` through the `refine_visibility` hook, and
  it is a READING OF THE ALTERNATIVE'S LINKAGE rather than an exemption granted from energy: the
  mode must be the terminal node of a track this alternative LINKS (≥ 2 nodes), the track must
  DESCEND toward the centre over its final edge, the declared geometry must apply on this path, and
  a contiguous run of consecutive arenas from the neighbour to the fold must be covered and carry a
  chosen ELASTIC component (a quasielastic one is no response for a branch to be swallowed by).
  Where a slope is declared it must be able to carry the remaining ω to zero over the Δq to the
  centre; where none is, the card's assertion that this fold has a gapless branch is what licenses
  the reading. Competing readings of the same assignment — the track ending mid-zone unexplained,
  or continuing — stay enumerated and priced, so the posterior decides and not the builder. An
  isolated low-energy feature and a flat low-lying track therefore get no reading at all and stay
  charged. `blocked_unresolved` says *unresolved pending better data*;
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

**The path is a topology, not a radius in q.** Arenas are ordered by the PATH they were measured
along, derived from the q set alone: the direction is fixed by the two most-separated distinct q,
every arena is projected onto it, and the set is a path only if every perpendicular residual is
within `PATH_COLLINEAR_TOL` (0.01 rlu — the q-positioning scale, an order of magnitude under the
0.05 rlu HKL grid step). Adjacency is CONSECUTIVE ordinals and nothing else, so inserting an
intermediate scan between two arenas replaces their judgement with two new ones instead of leaving
a skip-neighbour term standing beside them. A q set that is not a path fails closed: no ordinals,
no cross-arena coupling, every arena its own cluster, and `path_topology_unavailable` recorded in
`sample_knowledge.degradations`. `h` is never a path coordinate — a campaign walking (2,2,0) →
(2,0,0) moves along k at constant h, so ranking on h would collapse the whole path onto one point.
The card's SCALAR zone period and reflection positions are rlu ALONG a reciprocal axis, so they
apply only where the derived direction IS an axis (`PATH_AXIS_TOL`); an off-axis collinear path
keeps its topology and records `zone_geometry_inapplicable_on_path` /
`lattice_geometry_inapplicable_on_path` instead of comparing them against a coordinate they do not
mean.

**Reach is dispersion, and absence is detectability.** Two gates stand between geometric inclusion
and a priced absence, because neither is what "we looked and it was not there" needs. *Reach*: the
ω a track may move over an edge is the resolution floor plus `dispersion_slope_max × Δq` from the
declared sample knowledge — energy resolution says how well two peaks are told apart and nothing
whatever about dω/dq. With no slope declared, reachability is UNKNOWN, and a neighbour that holds
mode components at other energies is not a neighbour that found nothing: the absence is demoted to
`blocked_unresolved` with reason `reachability_unknown`, and only a neighbour with NO mode
components at all is an unambiguous non-detection. *Detectability*: an absence on a band covered
only from the anti-Stokes side is informative only if the anti-Stokes intensity was there to see,
so the detailed-balance weight `exp(−ω/k_BT)` must clear `AS_DETECT_WEIGHT` (the scene's
amplitude-free mirror of production's `AS_DETECT_Z`) — below it the reason is `bose_dead`; and the
neighbour must have counted comparably deep, `effective_exposure_j ≥ DETECT_EXPOSURE_RATIO ×
effective_exposure_i`, or the reason is `insufficient_detection_power`. Facts the footprint does
not carry (an unknown temperature, an unrecorded exposure) establish nothing and fail closed with
their own reasons. Every gate demotes a state and records why; none of them adds a term, so the
exactly-once factorization is untouched.

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
| SCENE-1 | Catalogue, identity-conditional Laplace evidence, global grammar (bg-only + elastic+bg first-class, `elastic_only` verdict), observation footprints, persisted candidate rows — three sequential PRs | Implemented — PR-a (H3, 2026-08-08; merged `fd1aa76`), PR-b (H4, 2026-08-08; merged `e02350a`), PR-c (H9/H10, 2026-08-09; merged `c5adf15`); deferrals recorded in H9(e) |
| SCENE-2 | `isar.campaign-program/1` sole contract; old envelope unreadable by design | Implemented (H2, 2026-08-08; PR #12 merged at `058fb40`) |
| SCENE-3 | Scene layer core: clusters, structural factors, revisioned scene store, workspace_api read surface; tracker contracts ported as specifications | Implemented — PR 3a (H11/H12), 3b (H13), 3c (H14), all 2026-08-10, incl. the PR #19 response waves; final merge `a1c6e41` |
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
exact rows they consumed, with completeness status and conditioning provenance).

**Counts are authoritative, fits are disposable** (PR 4.0, #26). Alongside results.csv,
`isar.session.scan_delivery` appends one line of `isar.measurement-arena/1`
(`isar.schema.measurement.MeasurementArena`) per measured arena to
`<workspace>/report/measurements.jsonl`, at the point a `ScanContext` is built — BEFORE `fit_one`
is ever called, so the record survives a fit crash untouched. Same fsync-append idiom as the
scene store's `history.jsonl`; last record per `scan_ref` wins on read. `count_trust` names three
honest states: `"trusted"` (a usable QC-masked measurement; a fitted candidate table, if any, is a
separate, disposable interpretation of it — a trusted arena with none yet is `no_interpretation`,
not a failure), `"excluded"` / `"held_for_calibration"` / `"undersampled"` (a terminal gate
outcome — attempted, never coverage). `isar.workspace_api.scene.scene_observations` joins
candidate rows onto these records; a delivered candidate table with no matching record, or none
at all, is refused (H1, re-deliver) rather than guessed at. `isar.scene.build.build_scene` prices
this at intake: a `"trusted"` arena (fitless or not) is footprinted like any other measured arena;
a count-untrusted one takes no part in the footprint set or the path topology at all — it was
attempted, never measured — and is recorded `non_scoring` with reason `count_untrusted`.

Per scene update (H14(b)), under `<workspace>/report/scene/`:

```
<digest>.json      the canonical bytes of one revision — exactly what the digest covers
current.json       isar.scene-pointer/1: {schema, digest, parent, written_at}
history.jsonl      one append-only line per pointer flip: {digest, parent, written_at, source}
last_attempt.json  what the last build-and-store attempt did, including that it failed
```

The revision payload (`isar.scene-revision/1`) carries the factor manifest and its
`registry_fingerprint`, the margin/cap/score-span actually used, the `sample_knowledge` block with
its `degradations`, the embedded observations and their candidate rows, the footprints, the
`non_scoring` arenas with reasons, and per cluster: the alternatives with their itemized ln-terms,
the marginals, the visibility reports of the leading alternative, the `classification` map
(`{factor: {component: state}}`), the score-term ablation ledger (`score_ablation`, positional
per `score_ablation_alignment`, H12(f)), and the transformation ablation ledger
(`transformation_ablation`, positional per `transformation_ablation_alignment`, #22 — see below).

**Lineage and time live outside the blob.** A revision records what it succeeded, and an unchanged
workspace must rebuild to the same digest; both are only possible if `parent` and `written_at` sit
outside the digested payload. The DURABLE lineage is `history.jsonl`, one appended line per flip:
`current.json` carries the latest link only, so A→B→C would otherwise leave nothing on disk that
remembers B→A. A rebuild that changed nothing appends nothing, because no flip happened.

The blob is written first and the pointer second, so a crash between them leaves a valid old
pointer and an orphan blob; the pointer is convenience, never authority, and no blob is ever
deleted. **Existence is not integrity**: a blob already at the target name is compared byte for
byte with the revision and rewritten when it differs, before the unchanged test and before any
pointer flip — so a lost OR corrupted blob self-heals on the next build of the same content,
`unchanged` means the content really is on disk, and a corrupt orphan is repaired before the
pointer can flip onto it. Absence is quiet (a workspace with no scene is ordinary); corruption
raises on read, and on write when the existing pointer is unreadable — on read that is a pointer
naming a missing blob, unparseable JSON, a payload that does not re-digest to its own name, or a
pointer that breaks its own contract (a foreign `schema`, a `digest`/`parent` that is not a sha256
hexdigest, a missing `written_at`), each checked before any field of it is used.

**Two writers, no lock.** `isar report --scene` can run against a live campaign, so concurrency is
optimistic rather than locked: each writer captures the pointer digest when it snapshots its inputs
and passes it back as the expected parent, and the flip refuses with `ConcurrentSceneWrite` (a
`ValueError`, so both callers' existing handling applies) if the pointer moved meanwhile. The
losing writer's blob is still written and addressable. A lock file is deliberately rejected: a
stale lock after a crash would block every later campaign, which is worse than the race.

Encoding is strict JSON (sorted keys, `allow_nan=False`): a non-finite lnZ persists as `null`, as
the builder already treats it. The truncation bound is recorded in LOG space
(`log_discarded_mass_bound`): the linear form overflowed to `inf`, which is neither JSON nor
informative, and a vacuous bound must not read as a bounded approximation. It is finite for every
finite input, so `null` means either "nothing was dropped" (`truncated` false) or "nothing bounds
what was dropped" (`truncated` and `truncation_unbounded` both true) — no third case.

The GUI and notebooks read all of it through `workspace_api` only (Qt-free; hard rules 6/7
unchanged): `ws.scene` / `scene_revision(ws)` for the stored revision — `None` when there is none, raising when the store
is damaged — and `hypothesis_map(ws)` for the renderable projection (H14(c)): one entry per scoring
arena plus gap rows for everything the scene does not claim, stating only what the revision already
decided. It keeps THREE axes apart — markers and gap bounds in intrinsic `|ω|`, a delivered curve
on the SIGNED energy window it was fitted on, folding left to the renderer.

The read surface gates what it interprets and binds what it renders (H14(c)): a payload whose
`schema` is not `isar.scene-revision/1`, or which lacks the keys the projection reads, refuses
rather than reading as an empty scene; the view holds canonical bytes and decodes a fresh payload
per access; every read recompares the live rows' observation fingerprints with the revision's and
reports `stale`; and a curve is withheld with a recorded reason unless the cluster is resolved, the
row still fingerprints as the observation that was scored, the leading alternative IS the delivered
model, and the evaluator's PURE path (stored amplitudes) rendered it — the fallback re-solves
amplitudes, which is fitting. Every entry carries `cluster_authority` (`exact` / `bounded` /
`unresolved`) with the finite `log_discarded_mass_bound` when there is one; an unresolved cluster's
marker weights are conditional masses over the retained set, flagged
`conditional_on_retained_set`, never projected as ordinary posteriors.

A scene is built by one function, `isar.session.scene_hook.build_and_store` (H14(d)): the driver
calls it observe-only after `workspace.save`, timed as the `scene_observe` stage and never able to
fail a delivery, and `isar report --scene` calls it offline, where a refusal is visible and exits
3. `catalogue_version` is stamped from the catalogue's `DEFAULT_PROFILE` until a per-fit profile is
persisted.

## 7. Status discipline

This document is the canonical scene-layer reference. Capability status lives in DESIGN §15 and
must match reality per slice; per-slice rulings land as H-series entries in `docs/DECISIONS.md`.

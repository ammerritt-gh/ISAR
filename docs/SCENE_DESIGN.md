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
- **Laplace evidence validity is checked, never assumed.** Boundary-pinned MLEs (amplitude→0,
  Γ→bound) mark `laplace_invalid` and escalate (dynesty) rather than reporting a silent Gaussian
  volume. Evidence comparisons require an identical data+mask arena (arena hash asserted) — the
  Baker–Cousins saturated-model constant only cancels on same-masked data.
- **No shadow ceremony (H1).** Scene-EIG is authoritative from birth, validated by the world
  suite as ordinary pytest; the deterministic question scheduler is the permanent fallback when
  scoring is unavailable.

## 3. Slices

| Slice | Goal | Status |
|---|---|---|
| SCENE-0 | Constitution (H1) + synthetic world-suite foundation (12 worlds, ensemble runner, world manifests) | In progress |
| SCENE-1 | Catalogue, identity-conditional Laplace evidence, global grammar (bg-only + elastic+bg first-class, `elastic_only` verdict), observation footprints, interpretation artifacts — three sequential PRs | PR-a Implemented (H3, 2026-08-08: six-entry catalogue drives enumeration; nulls first-class; `elastic_only` with the 0.5 cap + LRT gate; transport/prior/variant declarations dormant); PR-b Implemented (H4, 2026-08-08: per-structure Laplace evidence + validity ladder, posterior drives selection, AIC persisted diagnostic, `fit_selection_semantics/2` replaces `/1`, universal Bose factor, dominance pruning + scoped escalation n_live; identity axis deferred by ruling); PR-c (persistence + variants + identity discussion) Planned |
| SCENE-2 | `isar.campaign-program/1` sole contract; old envelope unreadable by design | In progress |
| SCENE-3 | Scene layer core: clusters, structural factors, revisioned scene store, workspace_api read surface; tracker contracts ported as specifications | Planned |
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

Per scan: content-addressed interpretation artifact (structure × identity candidates, evidences,
methods, validity flags, arena hash) + `FitOutcome.interpretation_ref` + flat projection columns
in results.csv. Per scene update: content-addressed revision under `<workspace>/report/scene/`
with a current-pointer and the per-factor evidence ledger (leave-one-factor-out deltas). The GUI
reads all of it through `workspace_api` only (Qt-free; hard rules 6/7 unchanged).

## 7. Status discipline

This document is the canonical scene-layer reference. Capability status lives in DESIGN §15 and
must match reality per slice; per-slice rulings land as H-series entries in `docs/DECISIONS.md`.

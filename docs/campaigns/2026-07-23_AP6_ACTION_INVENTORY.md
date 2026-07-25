# AP6 branch-ambiguity stop: the incomplete action space

> **Lifecycle:** Historical evidence
> **Provenance:** hand-written
> **Verified against code revision:** `d0ea1df` (2026-07-25)
> **Superseded by:** the current contract named in [`campaigns/README.md`](README.md). A dated record of what happened, never a statement of current behaviour (DECISIONS D3).

**Status:** historical evidence; repair Implemented for the action-inventory control plane and the
predicted-rival identity probe (O28). AP6 replay passes at the `enumerate_actions` reconstruction
level in strict-historical and migrated modes; a full campaign replay through the live driver against
the frozen `test_workspaces/AP6_Failed/` workspace is outstanding. Commits `653e22e…7ecb924` on
`repair/ap3-global-branch-evidence`.

AP6 mapped the acoustic phonon from (2,0,0) to (3,0,0) in 21 steps and stopped
`incomplete — branch_ambiguity; frontier_blocked at path index/indices 18, 19, 20` with beam time
remaining and only two productions at the blocked frontier. The frozen artifacts are pinned in
`tests/drive/fixtures/ap6_frontier.json` and re-derived through the real predicates in
`tests/drive/test_ap6_forensic_replay.py`; that module is the baseline this repair is measured
against and does not change.

## What the artifacts showed

Path index 18 (H=2.90) was identity-ambiguous: `track_state = "ambiguous"`,
`target_probability = 0.7069` against the 0.85 gate, `local_identity_clear = False`. It was also
`identity_saturated` — three comparable authoritative revisions had grown the exposure fourfold while
neither the target probability nor the identity entropy moved, the identity-space analogue of
`interval_floored` for a point whose branch never matches the target and so never reaches
`master_updates`. Indices 19 and 20 were `blocked` behind it. The saturation brake is what made the
terminal honest rather than a `controller_fault`: with more counting provably not paying,
`same_q_identity_work_remains` was False at every blocked point, so a proposal-less frontier was an
honest `incomplete` and not a controller invariant violation.

But it was honest about the wrong thing. Every scan the driver emitted at the frontier ran
`e_lo = -2.0` to `e_hi = 7.0` — the acoustic default band — and the rival mode that would settle the
acoustic-vs-optic identity at H=2.9 sits at ~7.115 meV. The discriminator was outside every window the
driver was willing to emit, because `energy_search_meV` did double duty: it bounded where the *target*
was expected and it was also the hard ceiling on every emitted scan. The window builder widens to cover
a supplied rival band and then clamps it straight back into the search band, so the evidence that would
break the tie was never in any scan ISAR proposed.

## Why counting and broadening could not fix it

The two obvious rungs were both structurally unavailable, not merely unused. Counting more at the same
window is exactly what `identity_saturated` records as spent. Broadening to a neighbour needs an
unvisited neighbour, and index 18 is one step from a path endpoint. And H=3 is a **zone boundary**
(this sample has zone centres at every even H), where the acoustic branch is flat at its maximum and
closest to the optic branch — so the two are genuinely hardest to separate there, and the point may be
underdetermined regardless of beam time. The missing thing was not more of an existing measurement; it
was a *different kind* of measurement, and the controller had no way to express one.

## The repair

The controller no longer drives a single fused selector. Acquisition is a two-phase registry pipeline
— declared generators emit `MeasurementIntent`s, evaluators score them, selection ranks the survivors
— carried through a three-stage lifecycle (`MeasurementIntent → FinalizedProposal → ValidatedAttempt`).
Each generator runs behind a failure-isolation wrapper, so one that raises becomes a resumable
`controller_fault` rather than an escaped exception or a false exhaustion proof, and
`action_inventory_exhausted` is exhaustion relative to a declarative `GENERATOR_REGISTRY` whose version
is derived from its contents — a grown registry cannot leave an old proof looking current.

On that base, four contracts make the rival reachable without licensing a scan anywhere:

- **Search-domain split.** `energy_search_meV` stays the target alias; `identity_search_meV` is
  additive. An identity-class action may look past the target ceiling, but only when the band's
  provenance shows it was compiler-chosen. A band an operator declared — or one carrying no provenance,
  which is AP6's real frozen objective — stays a hard constraint, and the rival action is reported
  `policy_forbidden` rather than silently scanned past. Equality with the shipped default is not
  evidence of authorship.
- **Evidence sink.** An `auxiliary_track_node` probe is fit independently, never pooled into the (Q,T)
  master, never certification-eligible, and merged into the tracker's per-path-point evidence as a
  provenance-tagged candidate that can change the identity but never supply the point's ω₀. A clean
  null (`candidate_not_detected`) is first-class evidence, distinct from a failed analysis, so a probe
  that looked and found nothing is not mistaken for a broken scan.
- **The generator.** `narrow_rival_probe` fires only where a point is identity-ambiguous and
  `identity_saturated`, and emits a narrow, resolution-relative window centred on the predicted rival,
  clamped into the identity domain. The declared window is honoured onto the *emitted* `ScanProposal`,
  not just the intent — the merge-gate test (`tests/drive/test_rival_probe_merge_gate.py`) asserts the
  proposal's `e_lo`/`e_hi` reach the rival. An earlier defect, caught by that test, had `_build_proposal`
  re-derive every window from the target band and silently reproduce the exact (-2, 7) scan AP6 already
  took.

## Two replay modes

`tests/drive/test_ap6_migrated_replay.py` reconstructs the blocked frontier at the `enumerate_actions`
level and runs the grown registry over it:

- **Strict historical** — AP6's real objective carried no band provenance, so the rival stays withheld
  and the terminal is unchanged. The one thing that changed is honesty: the inventory now records that
  an identity probe would have settled the frontier and the declared band forbade it, naming the 7.115
  meV rival an operator could choose to authorise.
- **Migrated** — with the band recorded as compiler-generated, the identity domain widens, the probe
  enumerates as an eligible action reaching 7.115 meV, and the frontier that used to be a geometric
  dead end has a justified next measurement.

## What is not claimed

Acceptance was never "AP6 reaches `met`". H=2.9–3.0 may be underdetermined and the posterior was not
tuned through the 0.85 gate to make a replay green. The repair is that the driver stops failing for the
wrong reason — a missing action class — not that it manufactures certainty. The replay is proven at the
reconstruction level; a full campaign replay through the live driver against the frozen workspace is
outstanding, and the zone-aware boundary probe (which needs a cross-zone dynamic-structure-factor
consumer that does not yet exist, since the symmetry-equivalent points give no new ω) is deferred to a
later PR. AP1/AP2/AP3 campaign replays are unchanged by this work, so O25/O26 remain Partial.

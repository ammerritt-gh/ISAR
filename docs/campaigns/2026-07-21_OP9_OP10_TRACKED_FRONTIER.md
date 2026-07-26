# OP9/OP10 tracked-frontier premature stop

> **Lifecycle:** Historical evidence
> **Provenance:** hand-written
> **Verified against code revision:** `d0ea1df` (2026-07-25)
> **Superseded by:** the current contract named in [`campaigns/README.md`](README.md). A dated record of what happened, never a statement of current behaviour (DECISIONS D3).

**Status:** historical evidence; repair Implemented for the tracked optic path and gated on an OP10
replay that reaches `met` (commits `ee038d1`, `4dfdf81`). AP campaign replays not rerun here.

OP9 and OP10 mapped the 6 meV optic phonon from (2,0,0) to (3,0,0) and both stopped
`incomplete — branch_ambiguity; frontier_blocked at path index/indices 15…20`. OP8 had run the same
objective, the same controller, the same 0.5 meV tolerance and the same target the previous evening
and reached `met`. The regression window is the AP-repair series `09cfe95…7348cc6`, which landed
between them.

## What the artifacts showed

Every field that authorises a stop read false. Path index 15 (H=2.75) had taken **one** production
against an authorised `maximum_production_batches_per_q` of twelve; indices 16–20 had taken none.
`exposure_saturated`, `strategy_ceiling_reached` and `stuck` were all false, and the per-scan
ceiling for the run was 10<sup>10</sup> counts against a largest submitted scan of 2.5×10<sup>7</sup>.
The last four iterations broadened to the remaining unvisited q and left the frontier's diagnostic
numbers byte-identical. The campaign ended because it ran out of unvisited q to broaden to, not
because it ran out of information.

Two defects compounded. Identity adequacy was decided by ω₀ *precision* against the ω₀ tolerance —
a 0.129 meV interval beat a 0.5 meV tolerance on the first quicklook, so an undecided assignment
read as settled and permanently suppressed the same-q statistics rung, which is gated on
`not adequate`. Separately, the same-q identity revisit was capped by `maximum_bridge_steps`, a
path-geometry knob defaulting to one, so the frontier got a single revisit and the eight-batch
ladder, the ncount escalation and `exposure_saturated` became structurally unreachable. Before the
AP series the revisit had been unconditional but for `stuck`, which is why OP8 spent 43 productions
and converged.

Neither of the two commits made earlier that day touched this path. `c06668d` raised the per-scan
ceiling 4×10<sup>9</sup> → 10<sup>10</sup> for a run that peaked at 2.5×10<sup>7</sup>; nothing on
the blocked path consults the ceiling.

## What counting alone did not fix

Repairing the two defects made the controller count, and it still failed. A replay escalated
2.5×10<sup>7</sup> → 3.2×10<sup>9</sup> and spent roughly 1.7×10<sup>10</sup> counts at H=2.75
across twelve productions without moving the assignment.

The frontier was never count-limited. Its two candidates were 5.53 ± 0.065 and 7.72 ± 0.097 meV —
2.18 meV apart, of order 22σ, already cleanly resolved *as peaks*. The ambiguity was in their
acoustic/optic **labels**: P(optic) = 0.752 against the tracker's 0.85 confirm gate, because the
acoustic branch had gone unfitted at 2.60/2.65/2.70. Those q had only ever received a 10<sup>7</sup>
quicklook, and with a three-point hole in the rival trajectory nothing connected 5.53 meV to the
acoustic branch. A tracked target is confirmed by continuity, so its identity can be blocked by a
gap in the *rival* branch rather than by anything about the frontier itself.

## What the repair does

Precision adequacy and identity adequacy are separate questions, and identity now comes from the
tracker's own confirmation predicate — `target_probability`, `identity_entropy`,
`local_identity_clear` — carried through `results.csv` to `GridQ` rather than re-derived from
`branch_posterior`, which the driver deliberately clears for exactly the ambiguous case. Every
same-q production charges to `maximum_production_batches_per_q`; `maximum_bridge_steps` returns to
meaning path geometry only.

Acquisition keeps its neighbour-first ordering and gains two lower rungs. Where a q on the confirmed
run behind a blocked frontier resolved fewer modes than the frontier does, a production is spent
*there* to restore the rival evidence, capped per gap q so a reconnect that never resolves yields
the beam instead of escalating against the point's own ceiling. Only below that does the frontier
receive more of its own exposure. A proposal-less frontier still holding authorised same-q work now
raises `ControllerInvariantError` rather than being reported as a scientific `branch_ambiguity`
result.

Replaying the frozen OP10 envelope reaches `met` with all 21 q converged, at 63 scans and
1.26×10<sup>9</sup> counts against OP8's 64 and 1.41×10<sup>9</sup>, with a largest single scan of
2.5×10<sup>7</sup> — no escalation anywhere in the run. One 25 M reconnect scan at H=2.70 took
P(optic) at H=2.75 from 0.752 to 0.9999999.

## Not established here

The `identity_saturated` brake — the identity-space analogue of `interval_floored`, for points that
never accumulate `master_updates` because their branch never matches the target — is unit-tested but
never fired in the successful replay, so it remains campaign-unproven. The AP1/AP2/AP3 campaign
replays were not rerun; only their unit regressions were exercised. Why the acoustic mode was not
admitted at 2.60/2.65/2.70 at quicklook exposure was not investigated: the scan windows covered it
and the branches were separated by roughly 3 meV against a 2.1 meV FWHM, so a weak-mode admission
question remains open behind the acquisition repair.

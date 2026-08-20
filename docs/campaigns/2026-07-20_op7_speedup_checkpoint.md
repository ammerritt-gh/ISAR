# OP7 speedup checkpoint

> **Status:** terminal
> **Verified against code revision:** `d0ea1df` (2026-07-25)
> **Superseded by:** the current contract named in [`campaigns/README.md`](README.md). A dated record of what happened, never a statement of current behaviour (DECISIONS D3).

**Date:** 2026-07-20
**Branch:** `Speedup-Phase`
**Workspace:** local `test_workspaces/OP7` (historical evidence; not load-bearing)

## Outcome

OP7 completed the 21-point, 0.1 meV deterministic objective in **1412.7 seconds** over 114
physical iterations. This is 20.3% below the original 1772.1-second reference, but still 2.39×
the 590.7-second sprint target. OP5 is not an end-to-end control because it stopped `continue`;
OP6 used a looser 0.5 meV tolerance.

Phase 1 behaved as designed. All 20 full provisional fits requested and executed M=50. Their
masters requested M=200 and adaptively executed a mean 170.8 draws (14.6% fewer than the cap).

Selective certification and automatic recovery also behaved correctly. The first pass fit all 21
points in 157.0 seconds, the second carried 18 and refit 3 in 40.4 seconds, and the final pass
carried 19 and refit 2 in 46.1 seconds. No operator resume or 21-point recertification was needed.

## Profile

| Live-loop bucket | Seconds | % wall |
|---|---:|---:|
| provisional scan error attachment | 254.916 | 18.0% |
| pooled-master error attachment | 247.766 | 17.5% |
| two-stage fitting (scan + master) | 98.547 | 7.0% |
| enumeration / IC / optimization remainder | 5.595 | 0.4% |
| collate + ingest | 135.283 | 9.6% |
| proposal evaluation | 89.214 | 6.3% |
| final certification wall | 243.432 | 17.2% |

Certification units run in parallel, so summed unit fit time is not campaign wall. The first
pass contained 1514.6 seconds of unit work but completed in 157.0 seconds. Its critical unit was
q=(2,0,0): 151.1 seconds total, including a 120.5-second bootstrap.

Because OP7 uses analytic TAVI scans, acquisition itself is effectively instantaneous. A high
fitting/certification share is therefore expected and is not a defect by itself; phase acceptance
is based on total campaign wall time and unchanged canonical decisions, not on reducing that share.

## Phase decision

OP7 exercised 21 cheap, 73 fast, and 20 lagged full live iterations; it exercised **zero** `auto`
fits. Each full iteration attached authoritative errors to both the provisional physical scan and
the pooled master. Physical rows cannot seat convergence or refresh the prior, so their 254.9
seconds of full error attachment are redundant under the master-authoritative contract.

The next lossless slice therefore splits tiers by role: a lagged full production iteration keeps
the provisional scan fast and makes only its changed master full. The generic `run_batch` default
remains compatible; drive opts into the role split. Fresh-master `auto` remains shadow-gated.

The conditional later phases remain off:

- Phase 3 gate fails: fresh-master point estimation was 46.128 seconds, 3.3% of wall, not >20%.
- Phase 4 gate fails: fit remainder was 5.595 seconds, 0.4% of wall, not >20%.

The role split projects an OP7 saving of approximately 255 seconds before any asynchronous overlap.
The approved design originally required both the canonical shadow comparator and a same-machine
campaign rerun. On 2026-07-20 the operator explicitly accepted basically equivalent final results
for this lossless role-routing slice; the strict comparator remains mandatory for frozen-input
replays and for enabling fresh-master `auto`.

## Role-split rerun

The clean `test_workspaces/OP7_role_split` rerun completed the same 21-point objective in
**1210.6 seconds**, down **202.1 seconds (14.3%)** from OP7 and **31.7%** from the original
1772.1-second reference. It used 110 live iterations and two certification passes. The first
certification fit all 21 q points in 171.1 seconds; after automatic corrective acquisition, the
second carried 18 accepted revisions and fit only three in 51.5 seconds.

| Live-loop bucket | OP7 (s) | Role split (s) | Delta (s) |
|---|---:|---:|---:|
| provisional scan error attachment | 254.916 | 0.000 | -254.916 |
| pooled-master error attachment | 247.766 | 360.272 | +112.506 |
| two-stage fitting | 98.547 | 99.900 | +1.353 |
| enumeration / IC / optimization remainder | 5.595 | 4.929 | -0.666 |
| final certification wall | 243.432 | 222.644 | -20.788 |
| **campaign wall** | **1412.7** | **1210.6** | **-202.1** |

The strict workspace comparator reports differences because a fresh TAVI campaign receives new
scan identities; those identities enter master revisions and deterministic bootstrap seeds, so
this is performance evidence rather than a byte-identical frozen replay. The scientific endpoint
is equivalent at the campaign level: all 21 q points certified, with the same verdict,
disposition, modality, target branch, track state, and pool-consistency state at every q. The
remaining categorical difference is the q=2.6 linewidth/reliability diagnostic. Across final
pooled ω₀ values, the mean absolute difference was 0.031 meV and the maximum 0.127 meV. Three q
points exchanged the near-nested `stokes2+bg` and `elastic+stokes2+bg` AIC winner. Per operator
acceptance, this is treated as basically the same end result, while the strict comparator remains
unchanged for true frozen shadow replays.

The 590.7-second sprint target is not yet met. The rerun confirms that the largest remaining live
critical path is pooled-master error attachment (360.3 seconds), followed by certification wall
(222.6 seconds). Phase 3 remains gated off: fresh-master point estimation is approximately
`435.229 - 360.272 = 74.957` seconds, only 6.2% of campaign wall. Phase 4 also remains gated off:
the fit remainder is 4.929 seconds, 0.4% of wall. The next guarded experiment is fresh-master
`auto`; it is not silently enabled by this evidence because its crash-safe asynchronous candidate
work unit is not yet available.

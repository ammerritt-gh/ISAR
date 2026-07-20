# Speedup sprint evidence — Phases 1 and 2

**Date:** 2026-07-20  
**Branch:** `Speedup-Phase`  
**Reference:** local `out/svp_recert1` (historical evidence; not load-bearing)

## Phase 1 instrumentation baseline

`isar profile --workspace out/svp_recert1` now reports the shared disjoint decomposition:

| Bucket | Seconds | % of 1772.1 s campaign wall | % of fit time |
|---|---:|---:|---:|
| provisional scan error attachment | 452.373 | 25.5% | 26.2% |
| pooled-master error attachment | 407.690 | 23.0% | 23.6% |
| two-stage point fitting | 367.984 | 20.8% | 21.3% |
| enumeration / IC / optimization remainder | 501.479 | 28.3% | 29.0% |

The profiler and campaign diagnostic use the same helper. Malformed timing payloads warn instead
of disappearing. Provisional scans now default to M=50 while masters/certification retain M=200;
requested and effective counts are persisted. The adaptive rule is fixed at blocks of 32, a
64-draw minimum when the cap permits, two consecutive half-width changes below 5%, deterministic
seed order, and the configured cap. Consequently M=50 executes exactly 50 draws.

Focused Phase-1/2 verification completed with 48 passing tests covering timings, malformed input,
adaptive bootstrap, CLI routing, shadow canonicalization, auto-tier triggers, promotion storage,
and prior admission.

## Controlled Phase 2 batch

The 81 frozen physical scans from `svp_recert1` were copied to
`out/speedup_phase2_auto` and force-refit in one 30-worker batch:

```text
isar run out\speedup_phase2_auto\data --workspace out\speedup_phase2_auto \
  --force --M 200 --scan-M 50 --workers 30 --errors auto --no-render
```

The batch completed 81 scan fits and 21 pooled-master fits in **550.366 seconds**. Auto promoted
80/81 scan fits and 20/21 master fits on at least one approved distrust trigger. The result proves
the new path is executable, role-aware, promotion-recorded, and below 590.7 seconds as a parallel
throughput experiment. It is **not** a replacement for the required sequential drive replay: its
worker overlap makes the wall number non-comparable with the historical 1772.1-second campaign.

The canonical comparator found identical copied steering-event streams but differences in
`dispersion_prior` and `certified_results` after the force-refit. Those differences have not been
adjudicated. Therefore the Phase-2 validation gate is **not passed**, `auto` is not made the drive
default, and asynchronous promotion scheduling is not enabled.

The current synchronous record is not yet a recoverable promotion queue: it is created after the
full attachment and immediately resolved against the same source revision. Runtime consumption of
pending records and a non-tautological current-revision publication check remain Phase-2 work.

## Conditional-phase decision

Phase 3 requires a comparable Phase-2 drive wall above 590.7 seconds *and* fresh-master point
estimation above 20% of that wall. This batch supplies neither condition: it is not a drive replay,
and master point estimation is a small fraction of its measured fit workload. Phase 3 therefore
remains off with compatibility policy `always`.

Phase 4 requires a post-Phase-3 miss plus fit remainder above 20%. That gate is likewise unavailable,
so `fit_fastpath` remains off. No freshness or fit-search semantics were weakened to manufacture a
speed result.

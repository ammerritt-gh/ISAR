# STAT-2b.1 corrected development and scaling preflight

> **Lifecycle:** Historical evidence
> **Provenance:** hand-written

## Disposition

The owner-approved STAT-2b held-out run from commit `58e9648` was stopped after 1,254 of
2,163 checkpoints had opened. Its scientific records were not inspected. The run is aborted and
scientifically ineligible; its original gate remains byte-identical and is marked superseded by
`tools/stats/stat2b-heldout-gate.superseded-status.json`.

STAT-2b.1 opened no additional held-out seeds. It repaired the evidence harness, ran an open
development-only scaling preflight, reran all development evidence from fresh corrected
checkpoints, and stopped with a new gate proposal whose `owner_approved` field is `false`.

## Defect and repair

Full delivery consumed the selected label, delivery data and constraint context, predeclared
target rank, candidate-derived target hint, candidate-derived multimode peak hints, error tier,
bootstrap count, and delivery seed. The old cache key covered only delivery context plus selected
label. Arms selecting the same label could therefore share a delivery even when their
candidate-derived target or peak hints differed.

Commit `5767dea` introduced `isar.stat2b_delivery_inputs/1`. Every arm now persists the canonical
input record, its fingerprint, reuse group, reuse status, and source arm. Delivery is deduplicated
only when those complete records match. Execution order is sorted by fingerprint, so arm ordering
and worker scheduling cannot alter reuse identity.

## Scaling result

The preflight used the same two open development realizations from each of 21 cells at every tier.
BLAS and OpenMP thread variables were fixed to one; the inner bootstrap worker count remained one.
All 210 records had identical non-runtime scientific fingerprints across tiers.

| Dataset workers | Wall seconds | Datasets/minute | Failures | Scientific differences |
|---:|---:|---:|---:|---:|
| 4 | 268.16 | 9.40 | 0 | 0 |
| 8 | 171.39 | 14.70 | 0 | 0 |
| 16 | 122.52 | 20.57 | 0 | 0 |
| 24 | 106.80 | 23.60 | 0 | 0 |
| 28 | 111.78 | 22.55 | 0 | 0 |

The frozen rule selected **24 workers**: it is the smallest clean tier at or above 90% of the best
throughput. The 28-worker tier was slower, so the user-requested 28-worker ceiling was measured but
not selected. Scaling-summary fingerprint:
`0c803fd7e2516b60dc7812819515067679f24ac9d1e0ffd24fa7b7c37d74342d`.

## Corrected development result

The corrected campaign completed 21 full-path smoke realizations and 420 development
realizations in 1,101.73 seconds at 24 workers.

- Campaign-spec fingerprint:
  `29f6b0623c4bf6c17666116741383d615f354eef939b12e72c901f3600b08c96`
- Campaign-source fingerprint:
  `b09504044e61899fdda82b03be4c90fa7edfdb6b303b07ec61777fd76ebec62d`
- Numerical-runtime fingerprint:
  `9d0e5cbf15a54999545e4133d2029cb77c0132cfa58e2d59a955b2167554f18f`
- Development-summary fingerprint:
  `cae8a48fd687a2cb725b2a36bc9f82ae2a4cb4874ef490b8b7bb61b8dfcb0da7`

The open dataset keys were identical to the pre-repair development campaign. Correct separation
increased unique full deliveries from 565 to 651. The 86 newly separated datasets produced 404
per-arm target-hint corrections across the four constrained-prior cells and the near-gamma
hard-prior cell. Selection rows, all scored arm outputs, delivery outputs apart from the declared
hint and timing, all six numerical margins, and the proposed held-out replication count were
unchanged.

The new proposal retains 103 held-out realizations per cell and selects 24 workers. Its projected
runtime is 3,374.71 seconds (56.25 minutes), with a proposed ceiling of 5,062.07 seconds
(84.37 minutes). This is a proposal only; it has no held-out authority until the owner explicitly
reviews and approves it.

## Evidence and reconstruction

The reviewed aggregate evidence is committed under
[`../assets/stat2b/2026-07-30-stat2b1/`](../assets/stat2b/2026-07-30-stat2b1/).
It includes the campaign specification, run manifest, decision summary, cell summaries, generated
findings, figures and captions, proposed gate, scaling summary, and an authenticated
pre/post-development comparison. Per-dataset JSONL and checkpoints remain under `out/`; the run
manifest preserves their fingerprints and source/runtime identity.

Regenerate the promotable aggregate from the retained local campaign output with:

```text
python tools/docs/export_stat2b_evidence.py
```

STAT-2c did not begin.

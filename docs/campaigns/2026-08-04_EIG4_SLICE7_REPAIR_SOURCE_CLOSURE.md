# EIG-4 Slice 7 repair source closure

> **Status:** terminal

## Outcome

The Slice 7 repair landed at production anchor
`70958cd13437b227efef3bef6b19aa473387457a`. The renewable source-closure workflow then produced a
replacement schema-v2 **bootstrap candidate** with fingerprint
`c49e92670df0b9c8a5ed6316cdd7fd36941554555daf6b17f253a0d8e21dad6e`. No active closure exists;
this candidate supersedes the earlier unapproved proposal and approves neither artifact.

The historical source drift remains four additions, three modifications, zero deletions, and 57
unchanged files, with no dependency-edge drift. The repair also changes the protected-adjacent
`isar/drive/session_log.py` by registering the diagnostic realized-comparison event kind. Its bounded
review classifies that change as nonsemantic for STAT-2b and binds focused Slice 7 and source-closure
parity tests. The whole `isar/session/driver.py` remains protected and was not changed.

The candidate and its generated review record authenticate:

- production-source fingerprint
  `429c72e0cd5c731ac0b154cd063b133f17e90ebe609ee5ae69b0c58e77d7f7f3`;
- dependency-graph fingerprint
  `db1258426e112ca2fe97fce541695c48e910166c9bcb5d7e421c7c95ae103e9c`;
- review fingerprint
  `3a3d77cd90735f93c0ba91abbc7030f808eaf607c96f102c11acb65b3eccef31`.

Portable candidate verification and Git-backed reconstruction both passed against the exact repair
anchor. Neither check constitutes owner approval.

## Verification evidence

The retained [verification record](2026-08-04_EIG4_SLICE7_REPAIR_SOURCE_CLOSURE/verification.json)
records:

- eight changed production/test modules compiled;
- 279 focused Slice 7, loop, scorer, and documentation tests passed and one skipped;
- the non-slow suite passed with 3,490 tests, eight skips, and 39 deselections in 77.74 seconds;
- the full suite passed with 3,529 tests and eight skips in 509.68 seconds.

The immutable held-out campaign and aggregate were authenticated, not rerun or rewritten.

## Statistical disposition

S8 and S9 remain unchanged: production criterion is AIC; AICc switching and STAT-2c authority are
not authorized; difficult regimes remain open. O40 remains diagnostic-only, and Slice 8 remains
planned. Promotion of this candidate still requires explicit owner review and a separate minimal
approval commit with no protected production-source changes.

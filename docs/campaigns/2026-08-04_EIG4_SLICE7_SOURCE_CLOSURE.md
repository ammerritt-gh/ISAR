# EIG-4 Slice 7 source-closure upkeep

> **Lifecycle:** Historical evidence
> **Provenance:** hand-written

## Outcome

EIG-4 Slice 7 landed at production anchor
`d3f2ee24baabf03519b21705df730408992e4243`. The source-closure workflow produced a replacement
schema-v2 **bootstrap candidate** with fingerprint
`7b5539af494327ae3ed34806a10075387afa7de286d6efcecd5c598555b0176b`. No active closure exists;
this candidate supersedes the earlier unapproved proposal and approves neither one.

The reviewed historical drift is four additions, three modifications, zero deletions, and 57
unchanged files. The only Slice 7 addition inside the historical source roots is
`isar/schema/eig_realized.py`. It is classified `unrelated_exempt`: the module serializes
diagnostic-only EIG evidence and is not imported by the STAT-2b selection or predictive-evidence
path. Dependency-edge drift is empty. The whole `isar/session/driver.py` remains protected and was
not changed.

The candidate and its generated review record authenticate:

- production-source fingerprint
  `34ae255263a289deeb4bc91a48d0feae318414340ab60edd4f6338ad9ea74a87`;
- dependency-graph fingerprint
  `db1258426e112ca2fe97fce541695c48e910166c9bcb5d7e421c7c95ae103e9c`;
- review fingerprint
  `afd0989c65860e7bc76bf7810d7b3c1af6feba33e3bc116b7538d8d4cfa1b23d`.

`python tools\stats\stat2b_source_closure.py verify --candidate
tools\stats\stat2b1-production-source-closure.candidate.json` returned `candidate_valid` before the
candidate evidence commit. Git-backed provenance becomes checkable only after that commit and does
not constitute owner approval.

## Verification evidence

The retained [verification record](2026-08-04_EIG4_SLICE7_SOURCE_CLOSURE/verification.json) records:

- six changed closure/Slice 7 modules compiled;
- 300 focused source-closure, selection, predictive, schema, held-out, and Slice 7 tests passed;
- 92 documentation tests passed and one skipped;
- the non-slow suite passed with 3,477 tests and eight skips.

The immutable held-out campaign and aggregate were authenticated, not rerun or rewritten.

## Statistical disposition

S8 and S9 remain unchanged: production criterion is AIC; AICc switching and STAT-2c authority are
not authorized; difficult regimes remain open. O40 adds diagnostic realized-request evidence only.
Promotion of this candidate still requires explicit owner review and a separate minimal approval
commit with no protected production-source changes.

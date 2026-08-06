# STAT-2b.1 production source-closure upkeep

> **Lifecycle:** Historical evidence
> **Provenance:** hand-written

> **Superseded candidate:** Owner review withheld approval on 2026-08-04. Candidate schema v1 at
> commit `77529b6` is retained as historical evidence; it was never active. The lifecycle and
> aggregate-authentication repair is recorded in
> [2026-08-04_STAT2B1_SOURCE_CLOSURE_REPAIR.md](2026-08-04_STAT2B1_SOURCE_CLOSURE_REPAIR.md).

## Scope and disposition

This record captures the 2026-08-03 upkeep cycle for the production source boundary behind the
completed STAT-2b.1 held-out campaign. The reviewed production anchor is
`ce490fec5badbc76e8799b7fb3f84ef5c201b855`. The cycle creates an **unapproved** closure candidate;
it does not approve that candidate, refresh the historical gate, change production selection, or
authorize another statistical campaign.

S8 remains exact: production uses AIC, no AICc switch is authorized, difficult-regime findings
remain open, and neither STAT-2c nor Slice 7 is authorized.

## Historical authentication and replay

The upkeep tool authenticated the committed historical inputs before evaluating the aggregate:

- held-out gate byte SHA-256:
  `c4d7ef5cba4ee42c71c8aa1e112a4e77b300c2bac5fc29954ce797d71024e6e0`;
- historical campaign-source fingerprint:
  `e655e3bbf92f2f57b1fbab4074120f8194d15930f46f55fb56590909f0662525`;
- historical gate semantic fingerprint:
  `ceaecf278329297729ae77d29eccfd940a5eaeefde703b86815225be5fc2a645`;
- historical evaluator `isar/synth/stat2b/campaign.py` SHA-256:
  `6800a240ed4b42280d762f4d260e9a6630c4bf8caf3a31483e6d59ef28b65a64`.

Only `2026-07-31_STAT2B1_HELDOUT/decision_summary.json` was replayed. The replay authenticated the
sealed specification, held-out summary, run, frozen gate, 21-cell matrix, nine declared arms, and
`n=103` for every applicable arm/cell before applying the historical margins. It reproduced
`authority_withheld` and the canonical `isar.stat2b1_aggregate_replay/1` fingerprint
`48b17e372543eca0e7d848d4eda595f7fa3b9a5381a5acbd15ed8a348e3b8f56`. Every failed rule and scope
is preserved structurally in the sibling [`review.json`](2026-08-03_STAT2B1_SOURCE_CLOSURE_UPKEEP/review.json).
No raw held-out or quarantine data was accessed.

## Drift and protected boundary

The exact historical source-map drift is three additions, three modifications, and no deletions.
The six changed paths retain separate enduring roles and current-hunk dispositions:

| Path | Enduring role | Current change disposition |
|---|---|---|
| `isar/drive/objective.py` | campaign runtime dependency | adjacent nonsemantic |
| `isar/schema/card.py` | campaign runtime dependency | adjacent nonsemantic |
| `isar/session/predictive_artifact.py` | evidence critical | evidence-contract semantic |
| `isar/schema/eig_policy.py` | protected adjacent | adjacent nonsemantic |
| `isar/schema/action_identity.py` | unrelated exempt | unrelated exempt |
| `isar/schema/eig_shadow.py` | unrelated exempt | unrelated exempt |

The AST-only traversal found 85 repository-local modules and 154 dependency edges. The reviewed
boundary contains 22 `protected_semantic`, 60 `protected_adjacent`, and three `unrelated_exempt`
modules. `isar/predictive.py`, absent from the historical hand-maintained map, is included by the
complete traversal. `isar/session/driver.py` is protected in full as the high-churn owner of the
selection-critical orchestration seam. Even harmless driver changes require bounded source review
and focused parity; extracting that seam is deferred to a separately reviewed refactor.

## Candidate and approval boundary

The private engine owns AST parsing, classification, historical replay, canonical fingerprints,
and verification. `tools/stats/stat2b_source_closure.py` is a thin command adapter with
`candidate`, `audit`, and `verify`; it intentionally has no approval command. Candidate generation
writes the immutable review first and never places the candidate fingerprint in that review.

The schema-v1 candidate at commit `77529b6` had
`owner_approved=false`. Its candidate closure fingerprint is
`889c86bc889c51b9c261c0e8195394b91157e96c0ae2ba02468eaecf025b12be`; the immutable review-record
fingerprint is `53d80a289aa60687fb46ccfd727bf09a18f9b4aa9b799141f872c833f21b35a3`. Passing checks cannot
promote it. Explicit owner review of the final boundary, exemptions, classifications, and candidate
fingerprint is still required.

## Verification record

Completed checks are recorded structurally in the sibling
[`verification.json`](2026-08-03_STAT2B1_SOURCE_CLOSURE_UPKEEP/verification.json):

- syntax: five files compiled, zero failures;
- consolidated closure/selection/shadow/campaign/statistical-semantics/predictive/projection/schema/
  held-out-evidence set: 282 passed, zero skipped/deselected/failed;
- documentation tests: 92 passed, one skipped, zero deselected/failed;
- non-slow parallel suite: 3,456 passed, eight skipped, 39 marker-deselected, zero failed;
- full parallel suite: 3,495 passed, eight skipped, zero deselected/failed.

Portable candidate verification and Git-backed protected-source parity with
`ce490fec5badbc76e8799b7fb3f84ef5c201b855` both pass. Git ancestry is reported separately and the
approved anchor is an ancestor of this candidate branch.

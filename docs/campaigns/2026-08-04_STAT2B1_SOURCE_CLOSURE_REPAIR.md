# STAT-2b.1 source-closure lifecycle repair

> **Status:** terminal

## Disposition

Review withheld approval from the source-closure candidate committed at `77529b6` because its
implementation could bootstrap only once and did not reproduce the complete held-out-summary
fingerprint. This focused repair supersedes that candidate with a schema-v2 **unapproved**
candidate. It changes closure governance and evidence authentication only: protected production
source remains anchored at `ce490fec5badbc76e8799b7fb3f84ef5c201b855`.

S8 remains exact. Production uses AIC; no AICc switch, STAT-2c authority, Slice 7 authority, or new
statistical campaign is authorized.

## Repaired lifecycle

Candidate construction now has two internal modes behind the same private engine interface:

- `bootstrap` authenticates the immutable held-out gate and uses its historical source map as the
  initial comparison basis when no active closure exists;
- `renewal` authenticates the active manifest and its committed candidate/review provenance, keeps
  that active file byte-identical and in force, and writes a replacement candidate beside it.

Every construction requires an explicit proposed production anchor and explicit versioned review
path. Renewal calculates source drift against the active module hashes and dependency drift against
the active graph. The historical gate and aggregate replay remain authenticated in every cycle.
The reusable engine no longer requires the bootstrap review's three-addition/three-modification/
zero-deletion result; that exact result remains a historical test and review fact.

Approval provenance is now defined precisely. A future active manifest must name owner
`ammerritt-gh`, a valid ISO date, matching proposed/approved production anchors, a 40-character
candidate artifact commit, the candidate artifact path and byte SHA-256, the approved candidate
fingerprint, and the unchanged review path/byte/semantic fingerprints. Git-backed verification
proves that candidate and review exist with those exact bytes at the preceding artifact commit.
The CLI still exposes no approval command.

## Evidence authentication and dependency coverage

The held-out summary is pinned both by raw byte SHA-256
`e4d626d813d1f827cc5c077963674c123073134589281cab8b7e2c361b64fb8b` and by recomputing
`isar.stat2b_heldout_summary/1` over the complete summary with its stored fingerprint field removed.
The recomputed value must equal the stored and frozen value
`e85c05edb6114f0f014e66a30074147a5f086622af9db5d6df296b7666ef438c`. The historical gate replay
still reproduces `authority_withheld` and
`48b17e372543eca0e7d848d4eda595f7fa3b9a5381a5acbd15ed8a348e3b8f56`.

The conservative traversal now parses every non-exempt protected module, then follows newly found
repository-local imports. The replacement boundary contains 87 modules and 192 edges: 22
`protected_semantic`, 62 `protected_adjacent`, and three `unrelated_exempt`. No boundary shrink was
attempted. Drift records now state `current_public_symbols`; they no longer claim those names are
the symbols changed from a historical revision.

## Candidate and verification

The replacement candidate remains `owner_approved=false`, with all approval and artifact-commit
fields null. Its candidate fingerprint is
`ea8d5718eb7dd84e1aaf7ef15fb1d996410a784467506347b431da7f9c02e1e0`; the immutable review
fingerprint is `ab6c3f105b0c6dfc52a6bf17960b557e0fba517e982404ee87206e369bb8650d`.

The structured records are the sibling
[`review.json`](2026-08-04_STAT2B1_SOURCE_CLOSURE_REPAIR/review.json) and
[`verification.json`](2026-08-04_STAT2B1_SOURCE_CLOSURE_REPAIR/verification.json). Verification
completed with:

- three changed Python files compiled;
- 287 consolidated closure/selection/shadow/campaign/statistical-semantics/predictive/projection/
  schema/held-out-evidence tests passed;
- 92 documentation tests passed and one skipped;
- 3,461 non-slow parallel tests passed, eight skipped, 39 marker-deselected;
- zero failures.

Portable candidate verification and exact Git-backed parity with production anchor
`ce490fec5badbc76e8799b7fb3f84ef5c201b855` pass. Explicit owner approval is still required; the
future approval commit must identify the preceding candidate artifact commit and may change no
protected production source or immutable review bytes.

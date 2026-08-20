# EIG-4 Slice 1 eligibility census evidence — 2026-08-02

> **Status:** terminal
> **Verified against code revision:** `a61a22709572945e2aa23da5ac86225242338c11` (2026-08-02)

## Disposition

This is safe-to-retain, read-only historical evidence from the EIG-4 Slice 1 census. It is not a
runtime authority, an eligibility reason-code freeze, or authorization to begin Slice 2. Owner
review of the retained evidence is still required before either action.

The reviewed run completed at `2026-08-02T18:16:12Z` under ruleset
`isar.eig4_slice1_census_rules/1` and summary schema `isar.eig4_slice1_census_summary/1`. It used
the reviewed manifest [`tools/stats/eig4_slice1_campaigns.toml`](../../tools/stats/eig4_slice1_campaigns.toml)
with SHA-256 `0fb4bfb1e0d032a494eb8e5ceb69e1f397ccf1ebb24965a1d7d463879ee0b08d`.

## Population and denominator

The run inspected 233 total rows. Of these, 230 had pooled outcomes; three `svp-recert1` master
entries without pooled outcomes were excluded from the eligibility denominator. The primary
population contains 189 pooled outcomes: 146 ineligible and 43 unassessable, with zero
observable/eligible outcomes. The supplementary population contains 41 pooled outcomes: all 41
ineligible and zero observable/eligible outcomes.

The eligibility rate is **not measurable**, rather than 0%: no pooled outcome is fully observable
under the current contract. The zero eligible count therefore cannot support a zero-percent
scientific eligibility claim.

## Contract observability

The retained population contains zero authenticated current `/2` predictive artifacts and zero
authenticated `/2` particle-source masses. Primary records comprise 170 legacy `/1` predictive
artifacts and 19 missing predictive references; their unauthenticated descriptive weights are
Laplace/HESSE `103.002136290111` and bootstrap `66.997863709889`. The 41 supplementary records are
also legacy `/1`, with Laplace/HESSE `16.000006096344` and bootstrap `24.999993903656`.

Producer-grounded code tests at `a61a22709572945e2aa23da5ac86225242338c11` validate that future
current `/2` `nested_sampling` and `laplace_hesse` bases are classified as material non-bootstrap
mass. This run, however, supplies no observed `/2` evidence for either basis.

Selection records are missing and their distributions are empty. Scan-design references are
missing, so no selected candidate families, realization events, or recovery events are recorded.
Projections are missing with currentness unknown, and shadows are `missing_legacy` throughout.

`speedup-phase2-auto` shares the `svp-force-refit` lineage and is not an independent lineage.
`OP11` remains supplementary by `artifact_match`; `AP8` remains supplementary as an `edge_case`.

## Retained bundle and review limits

The reviewed output and input/sidecar hashes were checked for path sanitation, and this committed
bundle is a read-only copy of the compact retained artifacts:
[masters.csv](2026-08-02_EIG4_SLICE1_CENSUS/masters.csv),
[summary.json](2026-08-02_EIG4_SLICE1_CENSUS/summary.json), and
[run_manifest.json](2026-08-02_EIG4_SLICE1_CENSUS/run_manifest.json).

`masters.jsonl` and generated `findings.md` remain local under ignored `out/`; they are not
committed. The review found no retained local paths. It cannot prove absolutely that ignored files
were absent before the run, because no pre-run filesystem snapshot exists.

## Next state

This retained evidence requires owner review. It does not authorize Slice 2 and does not freeze
eligibility reason codes.

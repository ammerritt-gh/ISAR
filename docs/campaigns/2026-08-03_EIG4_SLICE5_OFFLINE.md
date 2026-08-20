# EIG-4 Slice 5.1 offline comparison evidence — 2026-08-03

> **Status:** terminal
> **Verified against code revision:** `e13a6b9d9dc4a624a39f7ecf5bb52f9023b35ea3` (2026-08-03)

## Disposition

This is read-only retained evidence from the repaired Slice 5.1 discovery and zero-policy scoring
workflow. It supports O37's existing policy-scoped architecture; it is not a new ledger fact, a
scientific comparison, or authority to select or acquire a scan window.

The run used the reviewed
[`eig4_slice1_campaigns.toml`](../../tools/stats/eig4_slice1_campaigns.toml) manifest, whose SHA-256
is `0fb4bfb1e0d032a494eb8e5ceb69e1f397ccf1ebb24965a1d7d463879ee0b08d`. Discovery emitted an
`isar.eig_offline_corpus_index/o36.5.1` corpus with fingerprint
`b234c33ddfdc1389d367c348f247bcf974808e5192271bcc39ddfa40a013de95`.

## Observed availability

The 230 comparison snapshots comprise 64 `nonterminal_or_unclassified`, 74
`scientifically_ineligible`, 92 `unassessable`, zero `comparison_unavailable`, and zero `ready`
cases. No distinction policy was needed or supplied. Scoring used seed `9006000`, 128 simulations,
and 8 blocks, and produced a complete zero-result report with fingerprint
`ebfd2056608bf3ae90a5a0339516489813c0ba5f82e4e0e6edfef1a698133888`.

This is a zero-availability observation, not a zero-effect result. In particular, it says nothing
about whether `target_core/1` is more informative than `legacy_uniform/1`; no ready case reached
numerical evaluation. Discovery also reported that `svp_recert1` and `speedup_phase2_auto` use an
older objective schema, so no current campaign-configuration scope could be produced for them.

## Retained bundle

The retained artifacts are [corpus-index.json](2026-08-03_EIG4_SLICE5_OFFLINE/corpus-index.json),
[zero-policy-report.json](2026-08-03_EIG4_SLICE5_OFFLINE/zero-policy-report.json), and
[run_manifest.json](2026-08-03_EIG4_SLICE5_OFFLINE/run_manifest.json). The manifest records the
commands, clean repair revision, campaign-manifest hash, artifact hashes and semantic fingerprints,
observed counts, and exact implementation-source hashes.

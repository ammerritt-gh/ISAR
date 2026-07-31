# STAT-2b.1 held-out selection-criterion evidence — 2026-07-31

> **Lifecycle:** Historical evidence
> **Provenance:** hand-written
> **Verified against code revision:** `abd2dd1` (2026-07-31)

## Disposition

The sealed STAT-2b.1 held-out campaign completed all 2,163 datasets across 21 scenario cells at
24 workers in 3,146.31 seconds, below the frozen 5,062.07-second ceiling. Its frozen owner gate
returned **`authority_withheld`**. This record preserves evidence, not a change to production
model selection: AIC remains current, STAT-2c did not begin, and S8 remains for owner review.

## Evidence

- Held-out summary fingerprint:
  `e85c05edb6114f0f014e66a30074147a5f086622af9db5d6df296b7666ef438c`
- Run fingerprint:
  `262e6c93932650f4576deb8edb906abb60a180906e80db14226cfb7fd328912d`
- Frozen gate fingerprint:
  `ceaecf278329297729ae77d29eccfd940a5eaeefde703b86815225be5fc2a645`
- Campaign specification fingerprint:
  `43b9fcbc7f6c2cbb57510c4801ac8084e96caecc7b511e6707240e257e974a54`

The evidence-completeness check passed. Predictive-loss superiority and false-extra-mode
noninferiority passed overall, but the independent vetoes did not: comparative false
certification failed overall and in four critical strata; complete comparison failed overall and
for target-absent truth; partially resolved two-mode missed-mode and predictive-loss checks
failed; and RMSE failed in near-elastic, partially resolved, and weak-signal strata.

The compact aggregate bundle is committed beside this record:
[decision summary](2026-07-31_STAT2B1_HELDOUT/decision_summary.json),
[cell summaries](2026-07-31_STAT2B1_HELDOUT/cell_summaries.csv),
[generated findings](2026-07-31_STAT2B1_HELDOUT/generated_findings.txt),
[run manifest](2026-07-31_STAT2B1_HELDOUT/run_manifest.json),
[held-out specification](2026-07-31_STAT2B1_HELDOUT/campaign-spec.toml), and
[owner gate](2026-07-31_STAT2B1_HELDOUT/heldout_owner_gate.json).
Per-dataset JSONL, checkpoints, logs, and the two unsuccessful attempts remain local under `out/`.

## Next state

The owner must review and record S8 separately. Until then, no criterion switch or STAT-2c work
is authorized.

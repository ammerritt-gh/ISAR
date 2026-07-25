# Campaign records — index

> **Lifecycle:** Current (this index)
> **Authority:** canonical for *which* campaign record covers what
> **Provenance:** hand-written

Every file in this directory is **historical evidence**. Each records what a particular campaign
did on a particular date, including things that were later found to be wrong and repaired.

**No record here states current behaviour.** `DECISIONS.md` D3 makes this a rule, not a
convention: campaign notes are dated records, never load-bearing, and the reading guide must not
route beginners through them. If you want to know what the controller does *today*, read
[DESIGN.md §8](../../DESIGN.md), [MEASUREMENT_STEERING.md](../MEASUREMENT_STEERING.md), and the
decision named in the last column below.

The value of these records is diagnostic history: what broke, what the evidence looked like, and
which ruling settled it. That is worth keeping and worth not mistaking for a contract.

## Autonomous driver campaigns

| Date | Record | Purpose | Outcome | Governing decision |
|---|---|---|---|---|
| 2026-07-07 | [2026-06_MVP_FIRST_RUN.md](2026-06_MVP_FIRST_RUN.md) | First end-to-end autonomous run — one NL command, no human numbers | Completed; exposed the first live defects | Phase 4 / `isar drive` |
| 2026-07-08 | [2026-07_MVP_ZONE_RETEST.md](2026-07_MVP_ZONE_RETEST.md) | Re-run on the two trouble zones (near-Γ, zone boundary) | Passed first certification scorecard; two zones stayed open | — |
| 2026-07-09 | [2026-07-09_MVP16_DIAGNOSTIC.md](2026-07-09_MVP16_DIAGNOSTIC.md) | Architect diagnostic over `out/mvp_zone_test16` | The hand-assembled prototype that `isar diagnose` was built from | [CAMPAIGN_DIAGNOSTIC_SPEC.md](../CAMPAIGN_DIAGNOSTIC_SPEC.md) |
| 2026-07-10 | [2026-07-10_SVP_RECERT.md](2026-07-10_SVP_RECERT.md) | TAVI-as-backend deterministic re-certification | Deterministic rung certified; McStas/clocks out of scope | S3, R4 |
| 2026-07-20 | [2026-07_AP1_BRANCH_MAPPING_DEADLOCK.md](2026-07_AP1_BRANCH_MAPPING_DEADLOCK.md) | Acoustic branch-mapping deadlock | Diagnosed; repaired | O25 |
| 2026-07-20 | [2026-07_AP3_GLOBAL_BRANCH_EVIDENCE.md](2026-07_AP3_GLOBAL_BRANCH_EVIDENCE.md) | Global branch-evidence failure after a zone jump | Phase 0 repair in focused tests; live replay pending | O25/O26 |
| 2026-07-21 | [2026-07-21_OP9_OP10_TRACKED_FRONTIER.md](2026-07-21_OP9_OP10_TRACKED_FRONTIER.md) | Premature tracked-frontier stop on the 6 meV optic branch | Repaired for the tracked optic path; OP10 replay reaches `met` | O27 |
| 2026-07-23 | [2026-07-23_AP6_ACTION_INVENTORY.md](2026-07-23_AP6_ACTION_INVENTORY.md) | `branch_ambiguity` stop from an incomplete action space | Action-inventory control plane shipped; full driver replay outstanding | O28, O29 (amended by O31) |

## Speedup sprint evidence

| Date | Record | Purpose | Outcome | Governing plan |
|---|---|---|---|---|
| 2026-07-20 | [2026-07-20_speedup_phases_1_2.md](2026-07-20_speedup_phases_1_2.md) | Phase 1–2 timing evidence | Recorded | [SPEEDUP_DESIGN.md](../SPEEDUP_DESIGN.md) |
| 2026-07-20 | [2026-07-20_op7_speedup_checkpoint.md](2026-07-20_op7_speedup_checkpoint.md) | OP7 checkpoint | Recorded | [SPEEDUP_DESIGN.md](../SPEEDUP_DESIGN.md) |

## Analysis-core checks and forward notes

| Record | What it is |
|---|---|
| [IN8_PHASE1_CHECK.md](IN8_PHASE1_CHECK.md) | First stress-test of the Phase-1 fitter on real triple-axis (ILL IN8) data |
| [PHASE_4_NOTES.md](PHASE_4_NOTES.md) | Original forward notes for closing the measurement loop. **Built and shipped** — the realized driver is `isar/drive/`. Kept because they framed what Phase 3 was built toward. |
| [PHASE_5_NOTES.md](PHASE_5_NOTES.md) | Forward notes only, not designed. The horizon beyond Phase 4. |

Both `PHASE_4_NOTES.md` and `PHASE_5_NOTES.md` are aspiration documents rather than campaign
evidence, and neither describes shipped behaviour: Phase 4's realized form is in `isar/drive/`,
and Phase 5 does not exist. They live here because they are dated and historical.

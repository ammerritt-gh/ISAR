# Campaign evidence — index

> **Status:** live (this index)
> **Authority:** canonical for *which* frozen evidence bundle covers what

This directory holds the **retained frozen evidence bundles**: machine-written artifacts
(manifests, gate results, case data, fingerprints) from formal campaigns and gates, kept because
measured data is auditable forever (DESIGN_GOALS, Auditing). They are records of a date, never
statements of current behaviour — for what the controller does *today*, read
[DESIGN.md §8](../../DESIGN.md) and [MEASUREMENT_STEERING.md](../MEASUREMENT_STEERING.md).

The dated campaign *narratives* that used to live beside them were absorbed into the living
design docs and deleted on 2026-08-20 (the doc rebuild,
[HANDOFF-doc-rebuild.md](../HANDOFF-doc-rebuild.md)); git history retains every one
(`git log --diff-filter=D --oneline -- docs/campaigns/`).

## Retained bundles

| Bundle | What it froze | Ruling / evidence for |
|---|---|---|
| [2026-07-31_STAT2B1_HELDOUT/](2026-07-31_STAT2B1_HELDOUT/) | Sealed STAT-2b.1 held-out criterion comparison (decision_summary, cell summaries, owner gate) | S8 (authority withheld for the AICc switch) |
| [2026-08-02_EIG4_SLICE1_CENSUS/](2026-08-02_EIG4_SLICE1_CENSUS/) | EIG-4 Slice 1 eligibility census (eligibility not measurable) | O36 context, diagnostic |
| [2026-08-03_EIG4_SLICE5_OFFLINE/](2026-08-03_EIG4_SLICE5_OFFLINE/) | Slice 5.1 offline `off`→`shadow` comparison corpus + zero-policy report | O37 |
| [2026-08-03_STAT2B1_SOURCE_CLOSURE_UPKEEP/](2026-08-03_STAT2B1_SOURCE_CLOSURE_UPKEEP/) | Schema-v1 source-closure candidate (rejected, never active) | S9 (cycle retired by S10) |
| [2026-08-04_STAT2B1_SOURCE_CLOSURE_REPAIR/](2026-08-04_STAT2B1_SOURCE_CLOSURE_REPAIR/) | Schema-v2 source-closure repair candidate | S9 (cycle retired by S10) |
| [2026-08-04_EIG4_SLICE7_SOURCE_CLOSURE/](2026-08-04_EIG4_SLICE7_SOURCE_CLOSURE/) | Slice 7 bootstrap source-closure candidate | O40 / S9 |
| [2026-08-04_EIG4_SLICE7_REPAIR_SOURCE_CLOSURE/](2026-08-04_EIG4_SLICE7_REPAIR_SOURCE_CLOSURE/) | Slice 7 repair renewal candidate | O40 / S9 |
| [2026-08-04_EIG4_SLICE8_SHADOW_GATE/](2026-08-04_EIG4_SLICE8_SHADOW_GATE/) | Slice 8 shadow-gate first attempt (invalidated: `eligible_same_action_join_mismatch`; disposition in `attempt-disposition.json`) | O41 context |
| [2026-08-04_EIG4_SLICE8_SHADOW_GATE_REPAIR/](2026-08-04_EIG4_SLICE8_SHADOW_GATE_REPAIR/), […_ABORTED/](2026-08-04_EIG4_SLICE8_SHADOW_GATE_REPAIR_ABORTED/), […_V2/](2026-08-04_EIG4_SLICE8_SHADOW_GATE_REPAIR_V2/) | Repair attempts (V2 aborted pre-freeze: `fixture_regime_mismatch`; dispositions inside) | O41 context |
| [2026-08-04_EIG4_SLICE8_SHADOW_GATE_REPAIR_V3/](2026-08-04_EIG4_SLICE8_SHADOW_GATE_REPAIR_V3/) | The **passed** coherent Slice 8 gate; owner-accepted result `f6fd65dd…` binding the O41 authority envelope (one clean case, `s8-c01-strong-confirmed`) | **O41** |
| [2026-08-04_EIG4_SLICE9_SOURCE_CLOSURE/](2026-08-04_EIG4_SLICE9_SOURCE_CLOSURE/) | Slice 9 source-closure candidate | O41 / S9 |
| [2026-08-05_EIG4_R3_SOURCE_CLOSURE/](2026-08-05_EIG4_R3_SOURCE_CLOSURE/) | R3 cold-start evidence behind the symmetric per-neutron comparison | **O43** |
| [2026-08-05_O44_ZONE_CENTRE_SOURCE_CLOSURE/](2026-08-05_O44_ZONE_CENTRE_SOURCE_CLOSURE/) | A11 zone-centre waste evidence behind the acoustic-origin exemption | **O44** |

Each bundle is self-describing — machine-written JSON (run manifests, gate results, candidate
records, dispositions; exact filenames vary by campaign type) carrying commit anchors and
fingerprints. The ruling cited in the last column states what the evidence decided.

## Narratives pending absorption (terminal)

| Record | Absorbs into |
|---|---|
| [2026-07_MVP_ZONE_RETEST.md](2026-07_MVP_ZONE_RETEST.md) | branch-disambiguation mechanics → the steering design doc (rebuild slice C) |
| [2026-07-29_EIG_EXPOSURE_BASIS.md](2026-07-29_EIG_EXPOSURE_BASIS.md) | exposure-heterogeneity findings → the steering design doc (rebuild slice C) |
| [PHASE_4_NOTES.md](PHASE_4_NOTES.md) | held while `archive/PHASE_3_DESIGN.md` cites it; dies with the archive slice |

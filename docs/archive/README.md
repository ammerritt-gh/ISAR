# Archived design rationale

> **Lifecycle:** Archived (this index is Current)
> **Provenance:** hand-written
> **Verified against code revision:** `d0ea1df` (2026-07-25)

**Nothing in this directory is normative.** These documents are not maintained against the code,
and no current contract may take one as its source. They are kept for one reason: they record the
*reasoning* behind decisions that `DESIGN.md` states but does not justify.

Citing them for rationale or provenance is fine and expected — `README.md` and
`docs/READING_GUIDE.md` both do. Citing them for what the code does today is not.

| File | What it uniquely records | Current replacement |
|---|---|---|
| [PHASE_1_DESIGN.md](PHASE_1_DESIGN.md) | The synthetic core: why the inverse-crime method is the right validation shape, what "happy path" parameters mean and why those, the three-estimator comparison, why grid+NNLS over other initial-condition methods | `DESIGN.md`; `docs/STATS_PRIMER.md` for the statistics |
| [PHASE_2_DESIGN.md](PHASE_2_DESIGN.md) | Parsing and reduction: the "five corrections to the framing" in §0 that produced mask-not-delete, three-stage QC, and all-detectors-captured — the reasoning behind Hard Rules 3 and 5 | `DESIGN.md` §6 |
| [PHASE_3_DESIGN.md](PHASE_3_DESIGN.md) | Fitting: the error-spine-as-architecture principle, physics-gated enumeration, the modality fork, and what "messy" means in real scans | `DESIGN.md` §7 |

## Why these three and not the rest

The four session handoffs that used to live here were deleted on 2026-07-25. A handoff is
temporary by construction — it briefs one person for one piece of work, and once that work lands
it describes a past state of the world, not a design. The phase designs are different: they argue
for a position, and the argument stays useful after the code exists.

The test applied was irreplaceability, not age. Everything deleted either had its content
absorbed into a current document or described work that shipped. See the tombstone table in
[`../DOC_INVENTORY.md`](../DOC_INVENTORY.md) for each deleted file, the reason, and the commit
that still contains it.

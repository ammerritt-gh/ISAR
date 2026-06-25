"""ISAR reduction steps that operate on a parsed `ScanRecord` (Phase 2, post-parse).

`combine` performs tracked, reversible cosmic-ray rejection on the raw S30 miniscan ensemble and the
combine-before-divide normalization (§7.3, §7.5). QC/adjudication/collation are later bolt-ons that
consume its outputs (the reject mask + `cosmic_ray_rejected` flags).
"""

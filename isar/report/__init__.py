"""Human-facing reporting (Phase 2 step 6): make the reduction legible.

The whole reason ISAR does its own reduction is so a human can see what happened to their data —
what each scan looked like, what got removed, and why — with the assurance it is reversible (§0.4).
`model` builds a format-agnostic report model (the catalogue, coverage, flag rollup, and the
per-scan removal ledger); `figures` renders matplotlib PNGs with masked/rejected points shown, not
hidden (§7.4); `html`/`markdown` render the model; `build.generate_report` ties it together.
"""

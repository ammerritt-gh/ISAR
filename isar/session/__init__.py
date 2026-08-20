"""ISAR session layer (the retired unification plan §2, git history; current contract: docs/WORKSPACE_AND_CLI.md) — the persistent experiment workspace.

The analysis pipeline (parse → reduce → calibrate → fit → report) exists as validated library
code; this package assembles it into a re-runnable, idempotent driver over a per-experiment
*workspace*. It adds no physics and no statistics — only orchestration and persistence:

- `workspace.Workspace` — the on-disk experiment directory (policy TOML + two JSON state files).
- `state.SessionState` / `state.ScanEntry` — the ingest manifest + per-scan processing ledger.

Later steps add ingest, event routing, calibration wiring, the prior builder, guidelines, and the
driver in sibling modules.
"""

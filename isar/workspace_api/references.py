"""What an experiment still needs from outside its own folder (read-only, Qt-free).

An experiment folder holds everything ISAR derives, but not necessarily the facility files it was
read from (the ingest manifest keeps their absolute paths) nor the resolution curves the stored fits
used (the stored calibration records the curve folder). A copy of the folder, or a rename of it, can
therefore leave those references dangling, or change what the calibration fingerprint hashes.
:func:`outside_references` reports them. It stats folders, never files: a beamtime folder on a
network share would freeze the window if every file were stat-ed on bind. It never writes.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class OutsideReferences:
    source_folders: tuple[str, ...]                       # distinct parent folders of manifest files, sorted
    missing_source_folders: tuple[tuple[str, int], ...]   # (folder, files in it) for folders absent now
    recorded_resolution_folders: tuple[str, ...]          # folders the stored calibration recorded, sorted
    resolution_inside: bool                               # one of them is the root or inside it
    sources_inside: bool                                  # a manifest file is inside the root


def _inside(path: str, roots: tuple[Path, ...]) -> bool:
    """True when ``path`` is one of ``roots`` or beneath it (case-folded strings, no stat)."""
    p = Path(os.path.normcase(path))
    return any(p == r or p.is_relative_to(r) for r in roots)


def outside_references(ws) -> OutsideReferences:
    """Read ``ws.files`` (manifest paths) and the curve folders the stored calibration recorded.

    The recorded folders are what the stored fits used and what the calibration fingerprint hashes,
    so ``resolution_inside`` is true exactly when a rename or a copy changes that fingerprint.
    ``sources_inside`` compares the manifest paths (ingest stores them resolved) with the root by
    string, so a renamed folder would leave those rows pointing at the old name.
    """
    root = Path(ws.root)
    # The root as given and as resolved: ingest stores resolved paths, a caller may hold either.
    roots = {Path(os.path.normcase(str(root)))}
    try:
        roots.add(Path(os.path.normcase(str(root.resolve()))))
    except OSError as exc:
        # an unresolvable root keeps only its literal form; the literal compare still runs
        log.warning("could not resolve experiment root %s: %s", root, exc)
    roots_t = tuple(roots)

    per_folder: dict[str, int] = {}
    sources_inside = False
    for f in ws.files:
        folder = str(Path(f.path).parent)
        per_folder[folder] = per_folder.get(folder, 0) + 1
        if not sources_inside and _inside(f.path, roots_t):
            sources_inside = True
    source_folders = tuple(sorted(per_folder))
    missing = tuple((d, per_folder[d]) for d in source_folders if not os.path.isdir(d))

    state = ws.card.state
    recorded: set[str] = set()
    entries = list(getattr(state, "resolution_versions", None) or [])
    entries += list((getattr(state, "tabulated_calibration", None) or {}).values())
    for entry in entries:
        folder = entry.get("folder") if isinstance(entry, dict) else None
        if isinstance(folder, str) and folder:
            recorded.add(folder)
    recorded_t = tuple(sorted(recorded))
    return OutsideReferences(
        source_folders=source_folders,
        missing_source_folders=missing,
        recorded_resolution_folders=recorded_t,
        resolution_inside=any(_inside(d, roots_t) for d in recorded_t),
        sources_inside=sources_inside,
    )

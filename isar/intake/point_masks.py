"""Manual exclusions identify measured points, independent of display zero, scale and offsets.

An entry is an explicit mask/restore operation, shared by every target in one gesture.
The append-only intake log is the authority; no data or old result is rewritten.
"""
from __future__ import annotations

from collections import Counter
import functools
import hashlib
import logging
from pathlib import Path

import numpy as np

from isar.intake import store

log = logging.getLogger(__name__)


class UnreadableUnit(Exception):
    """The stored unit data of one analyzer are missing or unreadable."""


def _stamp(path: Path) -> tuple:
    """A unit file's identity: its path, modification time and size (no time or size when it is not there)."""
    try:
        st = path.stat()
    except OSError:
        return (str(path), None, None)
    return (str(path), st.st_mtime_ns, st.st_size)


_reported: set = set()     # shortcut: one entry per unit file state that failed to read; a session's worth, never trimmed


# shortcut: 256 cached units (each a set of point hashes), oldest re-read first; upgrade past a few hundred masked units
@functools.lru_cache(maxsize=256)
def _ids(data_dir, key: str, files: tuple) -> frozenset[str]:
    """``files`` (the stamps of the unit files, taken before the read) is the cache key only."""
    from isar.session.unit_data import read_unit_data_for_unit   # here: the read side never loads isar.session
    exp, scan, det = key.rsplit(":", 2)
    data = read_unit_data_for_unit(data_dir, exp, int(scan), det)
    if data is None:
        if (key, files) not in _reported:       # a failure is never cached, so it is reported once per file state
            _reported.add((key, files))
            log.warning("point masks of %s: its stored unit data is missing or unreadable under %s; its masks are held",
                        key, data_dir)
        raise UnreadableUnit(key)
    return frozenset(point_ids(data["x"], data["counts"], data["variance"]))


def current_ids(data_dir, key: str) -> frozenset[str]:
    """The identities of a unit's points as its stored unit data give them. Cached per the identity of the unit
    files (path, mtime, size), stamped before the read: an answer never outlives the file it was read from, and a
    rewritten file is read again. ``key`` is ``exp:scan:det``. Raises :class:`UnreadableUnit` (never cached)."""
    from isar.session.unit_data import unit_data_paths_for_read
    exp, scan, det = key.rsplit(":", 2)
    files = tuple(_stamp(p) for p in unit_data_paths_for_read(data_dir, exp, int(scan), det))
    return _ids(data_dir, key, files)


def point_ids(x, counts, variance) -> tuple[str, ...]:
    """Raw coordinates and measured values plus occurrence distinguish duplicate measurements.

    Reordering distinct points or appending data preserves identity. Replacing a measurement
    does not transfer its exclusion to a different measurement at the same energy.
    """
    arrays = [np.asarray(a, dtype=float) for a in (x, counts, variance)]
    if any(a.ndim != 1 or a.shape != arrays[0].shape for a in arrays):
        raise ValueError("point identity requires aligned one-dimensional arrays")
    seen = Counter()
    result = []
    for values in zip(*arrays):
        token = ":".join(float(v).hex() for v in values)
        occurrence = seen[token]
        seen[token] += 1
        result.append(hashlib.sha256(f"{token}:{occurrence}".encode("ascii")).hexdigest())
    return tuple(result)


def selected_mask(ids, excluded) -> np.ndarray:
    excluded = set(excluded)
    return np.array([p in excluded for p in ids], dtype=bool)


def entry_problem(entry):
    points = entry.get("points")
    if not isinstance(entry.get("masked"), bool):
        return "masked must be true or false"
    if not isinstance(points, dict) or set(points) != set(entry.get("targets") or ()):
        return "points must name every target"
    if any(not isinstance(v, list) or not all(isinstance(p, str) and len(p) == 64 for p in v)
           for v in points.values()):
        return "points must contain measured-point identities"
    return None


def save_selection(root, selection: dict[str, list[str]], *, masked: bool | None = None):
    """Mask a mixed selection, restore an all-masked one, or apply an explicit restore.

    Only registered BL43 analyzer data are editable. Selection is checked against the stored
    measurements before writing; restoring also permits an old identity after a source re-read.
    """
    from isar.intake import inputs, values
    from isar.session.unit_data import read_unit_data_for_unit

    keys = tuple(selection)

    def build():
        values._check_units(root, list(keys))
        read = store.read_all(root, store.ENTRIES)
        index = inputs.index_entries(read.entries, lines=read.lines)
        chosen = {k: sorted(set(v)) for k, v in selection.items() if v}
        if not chosen:
            return None, None
        for key, ids in chosen.items():
            exp, scan, det = key.rsplit(":", 2)
            data = read_unit_data_for_unit(Path(root) / "data", exp, int(scan), det)
            if data is None:
                raise values._Bad(f"No measured data for {key}")
            known = set(point_ids(data["x"], data["counts"], data["variance"]))
            if masked is False:
                known.update(index.point_masks.get(key, ()))
            if not set(ids) <= known:
                raise values._Bad("The displayed data changed. Refresh the plot and select the points again.")
        turn_on = (not all(set(ids) <= set(index.point_masks.get(key, ()))
                           for key, ids in chosen.items())) if masked is None else bool(masked)
        return {"kind": "point_mask", "targets": list(chosen), "points": chosen,
                "masked": turn_on, "reason": None}, turn_on

    return values._save(root, keys, build)

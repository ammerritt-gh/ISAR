"""Qt-free parsing for atomic scan × detector identifiers.

The persisted unit form is ``experiment_id:scan_number:detector_id``.  Experiment ids may
themselves contain colons and digit-only components, so parsing is deliberately right-anchored:
the final component is the detector and the preceding component is the scan number.  The legacy
``experiment_id:scan_number`` and bare ``scan_number`` forms remain readable.
"""
from __future__ import annotations


def unit_key(experiment_id, scan_number, detector_id) -> str:
    """Build the canonical persisted ``experiment:scan:detector`` unit identifier."""
    return f"{experiment_id}:{scan_number}:{detector_id}"


def split_key(key):
    """Return ``(experiment_id, scan_number, detector_id)`` for a supported key.

    ``scan_number`` is an int when its right-anchored token is decimal; malformed identifiers
    degrade to ``(None, <key>, None)`` rather than guessing a different scientific identity.
    """
    if isinstance(key, int):
        return None, key, None
    value = str(key)
    parts = value.split(":")
    if len(parts) == 1:
        return (None, int(parts[0]), None) if parts[0].isdigit() else (None, value, None)

    # Atomic identity: final detector, immediately preceded by the scan number.
    if len(parts) >= 3 and parts[-2].isdigit():
        return ":".join(parts[:-2]) or None, int(parts[-2]), parts[-1]
    # Legacy scan-level identity: final component is its scan number.
    if parts[-1].isdigit():
        return ":".join(parts[:-1]) or None, int(parts[-1]), None
    return None, value, None


def match_member(label, by_unit, by_scan):
    """The unit a pooled master's member label names: a unit key (``by_unit``), else a numeric scan label
    that matches exactly one unit (``by_scan``: scan number -> list of units). ``None`` when unresolved."""
    match = by_unit.get(label)
    if match is not None:
        return match
    try:
        candidates = by_scan.get(int(label), [])
    except ValueError:
        return None
    return candidates[0] if len(candidates) == 1 else None


def scan_prefix(key) -> str:
    """Return the legacy scan-level prefix of ``key`` without guessing malformed inputs."""
    experiment_id, scan_number, _detector_id = split_key(key)
    if experiment_id is None:
        return str(scan_number)
    return f"{experiment_id}:{scan_number}"

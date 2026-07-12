"""isar_gui.scan_keys — the one source of truth for the GUI's scan-unit key (Phase 3, Qt-free).

The atomic visible unit is a *scan × analyzer* — one ledger entry — so the canonical GUI key is the
full ledger id ``"exp:scan:det"`` (``== ScanView.scan_id``). Every site that builds, splits, or
matches a key routes through here so the string form has exactly one definition.

Key forms this module tolerates:
  * ``"exp:scan:det"`` — the new-form unit key (an analyzer of a source scan);
  * ``"exp:scan"``     — a **legacy v1** scan-level key (pre-atomic GuiStore memberships used this);
  * ``"scan"``         — a bare scan number with no experiment id.

The scan number is the first all-digit ``:``-token (matching
:meth:`isar.workspace_api.workspace.Workspace._split_unit_id`), so an experiment id that itself
contains ``:`` still splits correctly and a numeric detector id is not mistaken for the scan.

:func:`member_matches` is the legacy bridge: a stored member matches a unit when it is that exact unit
key **or** it is the unit's ``scan_prefix`` — so a v1 scan-level membership (``"exp:scan"``) pins
*every* analyzer unit of that scan for free, with no key rewriting on load (nothing is ever deleted).
"""
from __future__ import annotations


def unit_key(exp, scan_number, det) -> str:
    """Build the canonical unit key ``"exp:scan:det"`` from its parts."""
    return f"{exp}:{scan_number}:{det}"


def split_key(key):
    """``(experiment_id, scan_number, detector_id)`` from a key in any accepted form.

    ``scan_number`` is an ``int`` when the token is all-digit, else the raw token string; the parts
    before it join as the experiment id (``None`` when there is nothing before), the parts after it
    join as the detector id (``None`` for a legacy scan-level key). A key with no digit token yields
    ``(None, <key>, None)`` so a caller still gets a usable, distinct handle. Never raises.
    """
    s = str(key)
    parts = s.split(":")
    idx = next((i for i, p in enumerate(parts) if p.isdigit()), None)
    if idx is None:
        return None, s, None
    scan_number: int | str = int(parts[idx])
    exp = ":".join(parts[:idx]) if idx > 0 else None
    det = ":".join(parts[idx + 1:]) if idx + 1 < len(parts) else None
    return exp, scan_number, det


def scan_prefix(key) -> str:
    """The scan-level prefix ``"exp:scan"`` of a key (drops the detector). ``"scan"`` when no exp.

    For a key with no digit token this returns the key unchanged, so it is always a stable,
    round-trip-safe handle for grouping units of the same source scan.
    """
    exp, scan_number, _det = split_key(key)
    if exp is None:
        return str(scan_number)
    return f"{exp}:{scan_number}"


def member_matches(member, unit) -> bool:
    """Does a stored group ``member`` cover the ``unit`` key?

    True when ``member`` is the exact unit key, or when ``member`` equals the unit's
    :func:`scan_prefix` — the latter is the legacy v1 case, where a scan-level membership covers all
    analyzer units of that scan. Comparison is on strings; never raises.
    """
    m = str(member)
    u = str(unit)
    if m == u:
        return True
    return m == scan_prefix(u)

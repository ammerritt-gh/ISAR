"""isar_gui.scan_keys — compatibility re-exports for the Qt-free scan-key codec.

The atomic visible unit is a *scan × analyzer* — one ledger entry — so the canonical GUI key is the
full ledger id ``"exp:scan:det"`` (``== ScanView.scan_id``). Every site that builds, splits, or
matches a key routes through here so the string form has exactly one definition.

Key forms this module tolerates:
  * ``"exp:scan:det"`` — the new-form unit key (an analyzer of a source scan);
  * ``"exp:scan"``     — a **legacy v1** scan-level key (pre-atomic GuiStore memberships used this);
  * ``"scan"``         — a bare scan number with no experiment id.

The implementation lives in :mod:`isar.workspace_api.scan_keys`, so the GUI and read facade cannot
drift.  It is right-anchored: detector is final and scan number immediately precedes it.

:func:`member_matches` is the legacy bridge: a stored member matches a unit when it is that exact unit
key **or** it is the unit's ``scan_prefix`` — so a v1 scan-level membership (``"exp:scan"``) pins
*every* analyzer unit of that scan for free, with no key rewriting on load (nothing is ever deleted).
"""
from __future__ import annotations

from isar.workspace_api.scan_keys import scan_prefix, split_key, unit_key


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

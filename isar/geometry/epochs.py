"""Lattice-epoch resolver (NEW — not vendored).

Design ruling (binding): a lattice/orientation is a *versioned interpretation*, not static
policy. Scans stay in absolute instrument coordinates; a versioned epoch series maps
absolute Q -> HKL. Epochs change mid-experiment (phase transition, realignment, HK0->H0L
swap) and may be corrected retroactively — you append a new epoch and re-derive HKL, never
edit scans. Grouping keys on HKL.

Epoch record shape (a plain JSON-friendly dict; kept in ``CardState.lattice_epochs``)::

    {
      "version":    int,
      "created":    iso8601-str,
      "valid_from": {"scan_ref": str | None, "ts": str | None},
      "supersedes": int | None,          # the version this epoch retroactively replaces
      "lattice":    {"a","b","c","alpha","beta","gamma"},
      "orient":     {"u": [h,k,l], "v": [h,k,l]},   # optional; None -> unoriented
      "method":     str,                 # how the epoch was derived
      "source":     str,                 # who/what recorded it
    }

Both functions are stateless: they take the epoch list (or one record) per call and hold no
module-level state.
"""
from __future__ import annotations

from .sample_state import SampleState


def _valid_from(epoch: dict, key: str):
    return (epoch.get("valid_from") or {}).get(key)


def resolve_epoch(epochs: list[dict], *, scan_ref=None, ts=None) -> dict | None:
    """Return the epoch that governs a scan, or ``None``.

    Selection: drop any epoch that another epoch supersedes, then, among the survivors,
    keep those whose ``valid_from`` boundary is at or before the query (a ``None`` boundary
    means "valid from the beginning") and return the one with the latest boundary
    (ties broken by the highest ``version``).

    The query is located by ``scan_ref`` (compared against ``valid_from.scan_ref``) if given,
    else by ``ts`` (compared against ``valid_from.ts``). Both string comparisons: scan refs
    are fixed-width numeric strings and ISO-8601 timestamps both order lexicographically.
    With neither locator, the highest-version surviving epoch is returned. ``None`` when the
    list is empty or nothing is yet in effect.
    """
    if not epochs:
        return None
    superseded = {e.get("supersedes") for e in epochs if e.get("supersedes") is not None}
    candidates = [e for e in epochs if e.get("version") not in superseded]
    if not candidates:
        return None

    if scan_ref is not None:
        key, query = "scan_ref", scan_ref
    elif ts is not None:
        key, query = "ts", ts
    else:
        return max(candidates, key=lambda e: e.get("version", 0))

    applicable = []
    for e in candidates:
        bound = _valid_from(e, key)
        if bound is None or bound <= query:
            applicable.append(e)
    if not applicable:
        return None
    # None boundary sorts earliest (empty string < any real ref/timestamp).
    return max(applicable, key=lambda e: (_valid_from(e, key) or "", e.get("version", 0)))


def sample_state_from_epoch(epoch: dict) -> SampleState:
    """Build the geometry value object for an epoch.

    Sets the lattice from ``epoch["lattice"]`` and, when ``epoch["orient"]`` supplies both
    ``u`` and ``v`` hkl vectors, orients the crystal so they span the horizontal scattering
    plane (via ``set_orientation_from_plane``). Missing/absent orient -> unoriented sample.
    """
    lat = epoch["lattice"]
    sample = SampleState(
        float(lat["a"]), float(lat["b"]), float(lat["c"]),
        float(lat["alpha"]), float(lat["beta"]), float(lat["gamma"]),
    )
    orient = epoch.get("orient")
    if orient:
        u, v = orient.get("u"), orient.get("v")
        if u is not None and v is not None:
            # Imported lazily so the common path (resolve_epoch + unoriented build) does not
            # pull the plane/UB orientation machinery unless an oriented epoch is requested.
            from .plane import set_orientation_from_plane
            set_orientation_from_plane(sample, u, v)
    return sample

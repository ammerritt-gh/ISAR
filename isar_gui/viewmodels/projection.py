"""Shared, Qt-free projection derivation for the (q, E) family of views (Task 12, A-7).

Factored verbatim out of the C3 heatmap VM (Task 11, A-5) so the heatmap plane and the C5 dispersion
view (A-7) declare the *same* plane the *same* way — the two sibling views must never disagree about
which q component varies, which window integrates, or how many scans that window excludes (G9
4D-honesty). Pure functions over :class:`~isar.workspace_api.FitView`-like objects + card params; no
Qt, no workspace writes. Imports only numpy + the plotspec value layer (``ProjectionLegend``).

Projection declaration (the plane): among the fitted, non-pooled scans at the viewed temperature we
pick the VARYING q component as the one least concentrated at a single modal value (so a lone
out-of-window scan can never masquerade as the scan axis), fix the other two at their modal values,
and integrate within the card's ``hkl_round_step`` of those modal values. Scans outside that window
are EXCLUDED from the plane and counted — never silently dropped.
"""
from __future__ import annotations

from collections import Counter

import numpy as np

from isar.plotspec import spec as ps
from isar.workspace_api.scan_keys import split_key

#: Index -> reciprocal-lattice axis label.
AXIS = {0: "H", 1: "K", 2: "L"}


def _g(v) -> str:
    return f"{v:g}"


def candidates(results) -> list:
    """Fitted results carrying a full 3-component Q and a temperature — the plane's population.

    Pooled masters ARE on the plane (they are the DELIVERED per-q answer and belong on the ω̂ overlay,
    marked). The one place a master must be dropped — the heatmap's measured STRIPS, since a master
    has no persisted spectrum (Task 10's honesty rule) — filters pooled inline at that call site, so
    there is a single definition of "on the plane" here. Partial / absent Q and missing T are always
    off the plane."""
    out = []
    for fv in results:
        q = fv.q
        if q is None or len(q) < 3 or any(v is None for v in q):
            continue
        if fv.T is None:
            continue
        out.append(fv)
    return out


def restrict(cands, keys):
    """Keep only candidates a manual group covers (plan Part 2A). ``keys`` is a set of unit ids.

    A candidate is kept when its ``unit_id`` is in ``keys`` OR it is a pooled master whose members
    intersect ``keys`` — the pooled clause preserves the DELIVERED per-q answer when the group covers
    its members (``candidates`` deliberately includes masters). ``keys is None`` -> unchanged."""
    if keys is None:
        return cands
    keyset = {str(k) for k in keys}
    selected_scans = {split_key(k)[1] for k in keyset}
    out = []
    for fv in cands:
        uid = getattr(fv, "unit_id", None)
        if uid is not None and str(uid) in keyset:
            out.append(fv)
        elif getattr(fv, "pooled", False):
            members = getattr(fv, "members", None) or []
            if any(_persisted_member_covered(member, keyset, selected_scans)
                   for member in members):
                out.append(fv)
    return out


def _persisted_member_covered(member, keyset: set[str], selected_scans: set) -> bool:
    """Whether one persisted pooled-member label is covered by selected unit keys.

    Production ``FitView.members`` contains strings. A qualified label is an atomic identity and
    therefore matches exactly; a bare decimal scan label intentionally covers any selected analyzer
    unit with that scan number, matching the compact chain-of-custody form written by the engine.
    """
    label = str(member)
    if label in keyset:
        return True
    experiment_id, scan_number, detector_id = split_key(label)
    return experiment_id is None and detector_id is None and isinstance(scan_number, int) \
        and scan_number in selected_scans


def restrict_or_clear(cands_all, keys):
    """``(cands, stale)`` for a view rebuild (plan R-lifecycle self-healing).

    Applies :func:`restrict`; but if a restriction is active and every id went stale (``cands_all`` is
    non-empty yet nothing survives the filter), returns the FULL population with ``stale=True`` so the
    caller auto-clears its restriction and notes it — rather than leaving a stale banner over an empty
    plot. An empty ``cands_all`` (no fits yet) is NOT stale: the ids may simply not be fitted yet."""
    if keys is None:
        return cands_all, False
    cands = restrict(cands_all, keys)
    if cands_all and not cands:
        return cands_all, True
    return cands, False


def modal_T(cands):
    """The most common (6-dp-rounded) temperature among ``cands`` — the default viewed T — or None."""
    if not cands:
        return None
    return Counter(round(fv.T, 6) for fv in cands).most_common(1)[0][0]


def modal_sample(fits):
    """The most common non-None ``sample`` among ``fits`` (for matching prior/branch anchors), or None."""
    samples = [fv.sample for fv in fits if fv.sample is not None]
    if not samples:
        return None
    return Counter(samples).most_common(1)[0][0]


def choose_axes(fits, step):
    """(varying_index, [modal0, modal1, modal2]). Varying = the component least concentrated at a
    single modal (step-rounded) value; ties broken by the largest raw spread. The other two are
    fixed at their modal values — a robust plane that out-of-window scans cannot hijack."""
    qs = np.array([[float(v) for v in fv.q[:3]] for fv in fits], dtype=float)
    modal = [0.0, 0.0, 0.0]
    conc = [1.0, 1.0, 1.0]
    spread = [0.0, 0.0, 0.0]
    n = len(fits)
    for i in range(3):
        rounded = np.round(qs[:, i] / step) * step if step else qs[:, i]
        vals, counts = np.unique(rounded, return_counts=True)
        m = int(np.argmax(counts))
        modal[i] = float(vals[m])
        conc[i] = float(counts[m]) / n
        spread[i] = float(qs[:, i].max() - qs[:, i].min())
    varying = sorted(range(3), key=lambda i: (conc[i], -spread[i]))[0]
    return varying, modal


def split_window(fits, varying, modal, step):
    """Partition ``fits`` into (in_window, excluded) by the integration window on the two fixed
    components: a scan is in-window when both fixed components lie within ``step`` of their modal."""
    fixed = [i for i in range(3) if i != varying]
    in_window, excluded = [], []
    for fv in fits:
        if all(abs(fv.q[i] - modal[i]) <= step + 1e-9 for i in fixed):
            in_window.append(fv)
        else:
            excluded.append(fv)
    return in_window, excluded


def projection_legend(varying, modal, step) -> ps.ProjectionLegend:
    """The always-on 4D-honesty declaration (G9): the shown plane + the integration window."""
    fixed = [i for i in range(3) if i != varying]
    plane_parts = [(AXIS[i] if i == varying else _g(modal[i])) for i in range(3)]
    plane = f"({', '.join(plane_parts)}) × E"
    integ = []
    for i in fixed:
        s = AXIS[i]
        m = modal[i]
        integ.append(f"|{s}|≤{_g(step)}" if abs(m) < 1e-9
                     else f"|{s}−{_g(m)}|≤{_g(step)}")
    return ps.ProjectionLegend(plane=plane, integration=", ".join(integ))


def prior_curve(points, varying, *, sample=None, T=None, t_round, want_sigma=False):
    """Reduce a list of PriorPoint-shaped dicts (``{sample, t_bin/T, q, omega0, sigma, ...}``) to the
    sorted ω(q) curve for one sample + temperature bin, projected onto the ``varying`` q component.

    Returns ``(xs, ys)`` — or ``(xs, ys, sigmas)`` when ``want_sigma`` — as numpy arrays, or ``None``
    when fewer than two DISTINCT q positions survive (a curve needs at least two points; this is the
    heatmap's exact degrade rule, shared so prior + branch overlays degrade identically). Anchors are
    matched to ``sample`` (when given) and to within ``t_round`` of ``T`` (when both given)."""
    rows = []
    for p in points or []:
        if sample is not None and p.get("sample") != sample:
            continue
        pt = p.get("t_bin", p.get("T"))
        if T is not None and pt is not None and abs(float(pt) - float(T)) > t_round:
            continue
        q = p.get("q")
        w = p.get("omega0")
        if q is None or len(q) <= varying or w is None:
            continue
        rows.append((float(q[varying]), float(w), _f_or_nan(p.get("sigma"))))
    if len({round(x, 6) for x, _, _ in rows}) < 2:
        return None
    rows.sort()
    xs = np.array([x for x, _, _ in rows])
    ys = np.array([w for _, w, _ in rows])
    if want_sigma:
        return xs, ys, np.array([s for _, _, s in rows])
    return xs, ys


def _f_or_nan(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("nan")


def nearest_point(points, x, y, tol):
    """The payload of the ``points`` entry (``(px, py, payload)``) nearest ``(x, y)`` in
    data-span-normalized units, or ``None`` when none is within ``tol``. Span-normalizing each axis
    to its own data range makes the pick zoom-safe and axis-scale invariant (a click is 'near' by
    fraction of the plotted extent, not by raw meV vs rlu). Shared by the plane + dispersion VMs."""
    if not points:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    xr = (max(xs) - min(xs)) or 1.0
    yr = (max(ys) - min(ys)) or 1.0
    best, best_d = None, None
    for px, py, payload in points:
        d = ((px - x) / xr) ** 2 + ((py - y) / yr) ** 2
        if best_d is None or d < best_d:
            best_d, best = d, payload
    return best if (best_d is not None and best_d <= tol * tol) else None

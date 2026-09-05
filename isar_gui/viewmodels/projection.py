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


def filter_prior_rows(points, *, sample=None, T=None, t_round) -> list[dict]:
    """Apply the shared sample/temperature slice used by curves and their metadata."""
    rows = []
    for point in points or []:
        if sample is not None and point.get("sample") != sample:
            continue
        point_T = point.get("t_bin", point.get("T"))
        if T is not None and point_T is not None and abs(float(point_T) - float(T)) > t_round:
            continue
        rows.append(point)
    return rows


def prior_curve(points, varying, *, sample=None, T=None, t_round, want_sigma=False):
    """Reduce a list of PriorPoint-shaped dicts (``{sample, t_bin/T, q, omega0, sigma, ...}``) to the
    sorted ω(q) curve for one sample + temperature bin, projected onto the ``varying`` q component.

    Returns ``(xs, ys)`` — or ``(xs, ys, sigmas)`` when ``want_sigma`` — as numpy arrays, or ``None``
    when fewer than two DISTINCT q positions survive (a curve needs at least two points; this is the
    heatmap's exact degrade rule, shared so prior + branch overlays degrade identically). Anchors are
    matched to ``sample`` (when given) and to within ``t_round`` of ``T`` (when both given)."""
    rows = []
    for p in filter_prior_rows(points, sample=sample, T=T, t_round=t_round):
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


def branch_track_overlays(payload, varying, *, sample=None, T=None, t_round=5.0,
                          modal=None, step=None) -> list[dict]:
    """Project additive ``card.state.branch_tracks`` into segmented plotspec branch dictionaries.

    Accepts both the current dictionary root (``{"tracks": [...]}``) and early list-shaped
    prototypes.  Each persisted segment remains a separate segment; ambiguous/missing states are
    retained for the builder to suppress, so the view can never join confirmed islands across a
    frontier gap.
    """
    if isinstance(payload, dict):
        tracks = payload.get("tracks")
        target = payload.get("target_branch")
    else:
        tracks, target = payload, None
    if not isinstance(tracks, list):
        return []
    out = []
    branch_numbers = {str(track.get("branch_id") or "branch"): index + 1
                      for index, track in enumerate(sorted(
                          (track for track in tracks if isinstance(track, dict)),
                          key=lambda item: str(item.get("branch_id") or "branch")))}
    for track in tracks:
        if not isinstance(track, dict):
            continue
        if sample is not None and track.get("sample") not in (None, sample):
            continue
        track_t = track.get("t_bin", track.get("T"))
        if T is not None and track_t is not None and abs(float(track_t) - float(T)) > t_round:
            continue
        branch_id = str(track.get("branch_id") or "branch")
        role = str(track.get("role") or "").lower()
        if not role:
            role = ("target" if target is not None and branch_id == str(target) else
                    "rival" if target is not None else "branch")
        raw_segments = track.get("segments")
        if not isinstance(raw_segments, list):
            raw_segments = [track]
        confirmed = bool(raw_segments) and all(
            str(segment.get("state", "confirmed")).lower() == "confirmed"
            for segment in raw_segments if isinstance(segment, dict))
        label = track.get("label")
        if not confirmed:
            label = f"Branch {branch_numbers.get(branch_id, 1)}"
        elif not label:
            physical = branch_id.replace("_", " ").title()
            if role == "target":
                label = f"{physical} target"
            elif role in {"rival", "context"}:
                label = f"{physical} (non-target context)"
            else:
                label = physical
        segments = []
        for segment in raw_segments:
            if not isinstance(segment, dict):
                continue
            rows = []
            for point in segment.get("points") or []:
                if not isinstance(point, dict):
                    continue
                point_t = point.get("t_bin", point.get("T"))
                if T is not None and point_t is not None \
                        and abs(float(point_t) - float(T)) > t_round:
                    continue
                # Persisted tracks carry both path-distance ``q`` and crystallographic ``hkl``.
                # This view's x axis is an H/K/L component, so the explicit HKL wins whenever it is
                # available; scalar q remains the fallback for early projected prototypes.
                hkl = point.get("hkl")
                q = hkl if isinstance(hkl, (list, tuple, np.ndarray)) else point.get("q")
                if isinstance(q, (list, tuple, np.ndarray)):
                    if len(q) <= varying:
                        continue
                    if modal is not None and step is not None:
                        fixed = [i for i in range(min(3, len(q))) if i != varying]
                        if any(abs(float(q[i]) - float(modal[i])) > float(step) + 1e-9
                               for i in fixed):
                            continue
                    x = q[varying]
                else:
                    x = point.get("x", q)
                energy = point.get("energy_meV", point.get("omega0", point.get("omega")))
                try:
                    if x is not None and energy is not None:
                        rows.append((float(x), float(energy)))
                except (TypeError, ValueError):
                    continue
            segments.append({
                "state": segment.get("state", "confirmed"),
                "x": [r[0] for r in rows], "y": [r[1] for r in rows],
            })
        endpoints = [(segment["x"][-1], segment["y"][-1]) for segment in segments
                     if segment.get("x") and segment.get("y")
                     and str(segment.get("state", "confirmed")).lower() == "confirmed"]
        out.append({"branch_id": branch_id, "label": str(label), "role": role,
                    "segments": segments, "endpoint": (endpoints[-1] if endpoints else None)})
    return out


def unselected_candidate_points(fv, x: float) -> list[dict]:
    """View-only points for fitted modes not selected as the row's authoritative answer."""
    candidates = getattr(fv, "branch_candidates", None) or []
    if not candidates:
        return []
    selected = getattr(fv, "selected_candidate_index", None)
    energies = [_candidate_energy(candidate) for candidate in candidates]
    if selected is None and getattr(fv, "omega0", None) is not None:
        viable = [(abs(e - float(fv.omega0)), i) for i, e in enumerate(energies) if e is not None]
        selected = min(viable)[1] if viable else None
    out = []
    for index, (candidate, energy) in enumerate(zip(candidates, energies)):
        if index == selected or energy is None or not isinstance(candidate, dict):
            continue
        # Candidate uncertainty is classification evidence, not a certifying target interval.  Keep
        # it off the whisker channel even when the candidate fit carries a local standard error.
        out.append({"q": float(x), "omega": energy,
                    "yerr": None, "selected": False,
                    "candidate_index": index})
    return out


def _candidate_energy(candidate) -> float | None:
    if not isinstance(candidate, dict):
        return None
    value = candidate.get("energy_meV", candidate.get("omega0", candidate.get("omega")))
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


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


# ---- pooled masters as the plane's measured columns --------------------------------------------
def pooled_masters(ws, fits) -> list:
    """Every collated master with a persisted spectrum whose members sit among ``fits``.

    A master is the collated group, not its fit: it is located through the ledger entry's ``pooled``
    provenance and ``members`` (:attr:`Workspace.scans`), never through a fit row, so a master whose
    fit was withheld, underpowered or failed still shows its ΣN. Each item is a namespace with
    ``members`` (the member FitViews present in ``fits`` — they carry the plane position and T),
    ``master`` (the master's own FitView when it has one, else ``None``), ``spectrum``
    (:meth:`Workspace.pooled_spectrum`), ``scan_id``, ``scan_number`` and ``member_ids``. An
    unreadable spectrum is logged and skipped — never silently drawn from a member instead.
    """
    import logging
    from types import SimpleNamespace

    log = logging.getLogger(__name__)
    by_unit = {fv.unit_id: fv for fv in fits if fv.unit_id and not fv.pooled}
    by_master = {fv.unit_id: fv for fv in fits if fv.unit_id and fv.pooled}
    out = []
    for sv in getattr(ws, "scans", ()) or ():
        prov = getattr(sv, "provenance", None) or {}
        ref = prov.get("spectrum_ref")
        if not prov.get("pooled") or not ref:
            continue
        member_ids = [":".join(str(part) for part in member) for member in prov.get("members") or ()]
        members = [by_unit[mid] for mid in member_ids if mid in by_unit]
        if not members:
            continue  # none of its members is on this plane (other T, or outside the projection)
        try:
            spectrum = ws.pooled_spectrum(ref)
        except (ValueError, OSError) as exc:
            log.warning("pooled spectrum for %s unreadable, no column drawn: %s", sv.scan_id, exc)
            continue
        if spectrum is None:
            continue
        out.append(SimpleNamespace(
            members=members, master=by_master.get(sv.scan_id), spectrum=spectrum,
            scan_id=sv.scan_id, scan_number=sv.scan_number, member_ids=member_ids))
    return out


def common_exposure_reference(spectra) -> float | None:
    """One monitor value every displayed master is normalised to: the median kept ΣM over all of
    their points. Masters pool different numbers of members with different exposures, so raw ΣN
    columns are not comparable across q; counts per common reference monitor are."""
    kept = [np.asarray(s.monitor, float)[np.asarray(s.mask, bool) & (np.asarray(s.monitor, float) > 0)]
            for s in spectra]
    values = np.concatenate(kept) if kept else np.zeros(0)
    return float(np.median(values)) if values.size else None


def counts_per_reference(spectrum, reference):
    """``(values, yerr)`` of a pooled spectrum expressed as counts per ``reference`` monitor;
    NaN where no member measured (ΣM = 0 or masked), so a renderer leaves those cells empty."""
    monitor = np.asarray(spectrum.monitor, float)
    counts = np.asarray(spectrum.counts, float)
    yerr = np.asarray(spectrum.yerr, float)
    keep = np.asarray(spectrum.mask, bool) & (monitor > 0)
    with np.errstate(divide="ignore", invalid="ignore"):
        scale = monitor / float(reference)
        values = np.where(keep, counts / scale, np.nan)
        errors = np.where(keep, yerr / scale, np.nan)
    return values, errors

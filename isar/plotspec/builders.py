"""Spec builders: turn read-side records into :class:`~isar.plotspec.spec.PlotSpec` objects. This is
the seam the report figures and the GUI viewmodels share (G8) — read-only accessors in, a pure spec
out. Only the spectrum/fit overlay is migrated for now; the rest of `isar/report/figures.py`
migrates lazily.

Imports only numpy + the record accessors it reads (`isar.schema.axes`); never matplotlib, Qt,
`isar.report`, `isar.drive`, or `isar.analyze` (importing report here would cycle — report figures
delegate to this module)."""
from __future__ import annotations

import numpy as np

from isar.plotspec import spec as ps
from isar.schema import axes


def _rejected_points(sp) -> np.ndarray:
    """Point indices with at least one cosmic-ray-rejected miniscan pass (kept for the ring glyph)."""
    if sp.miniscan_reject is None or sp.miniscan_counts is None:
        return np.zeros(0, dtype=int)
    rejected = sp.miniscan_reject & ~np.isnan(sp.miniscan_counts)
    return np.nonzero(rejected.any(axis=1))[0]


def spectrum_spec(record, det_id: str) -> ps.PlotSpec:
    """The spectrum/fit overlay for one analyzer as a spec: kept points as a DATA series with
    Poisson whiskers, masked points as MASKED markers (shown, not deleted), cosmic-ray-cleaned
    points as COSMIC_RAY rings. Fixed-Q, so the projection legend declares the integration."""
    sp = record.spectra[det_id]
    energy = axes.scanned_axes(record)[0].values
    mask = sp.mask.astype(bool)
    yerr = np.sqrt(np.clip(sp.variance, 0.0, None))

    series = [ps.Series(x=energy[mask], y=sp.intensity[mask], yerr=yerr[mask],
                        label="kept (+/-sqrt N)", role=ps.Role.DATA)]
    markers = [ps.Marker(x=float(e), y=float(v), kind=ps.MarkerKind.MASKED,
                         label="masked (shown, not deleted)")
               for e, v in zip(energy[~mask], sp.intensity[~mask])]
    markers += [ps.Marker(x=float(energy[i]), y=float(sp.intensity[i]),
                          kind=ps.MarkerKind.COSMIC_RAY, label="cosmic-ray cleaned")
                for i in _rejected_points(sp)]

    q = [round(float(v), 3) for v in sp.q]
    projection = ps.ProjectionLegend(plane="E", integration=f"Q = {q} (fixed)")
    return ps.PlotSpec(
        title=f"scan {record.scan_number}  {det_id}   Q={q}",
        xlabel="energy transfer (meV)",
        ylabel="counts (raw; Poisson sqrt N errors)",
        series=series, markers=markers, projection=projection,
        meta={"scan_number": record.scan_number, "det_id": det_id, "q": q},
    )


# ---------------------------------------------------------------------------
# Fit inspector (C2) builders — plain-data in, PlotSpec out (no workspace_api here).
# ---------------------------------------------------------------------------
def _interval_at(x, y, x0):
    """Curve value at ``x0`` by linear interpolation (for placing the ω₀ marker), or the peak of
    ``y`` when ``x0`` is missing/out of range or the grid is empty."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    if y.size == 0:
        return 0.0
    if x0 is None or x.size == 0 or x0 < float(np.min(x)) or x0 > float(np.max(x)):
        return float(np.max(y))
    return float(np.interp(float(x0), x, y))


def fit_overlay_spec(*, x, counts, yerr, x_fit, total, components, mask=None,
                     omega0=None, interval=None, interval_reliable=True,
                     scan_number=None, det_id=None, q=None, pooled=False,
                     members=None, n_members=None, pooled_data=False, title=None,
                     xlabel="energy transfer (meV)",
                     ylabel="counts (raw; Poisson sqrt N errors)"):
    """The C2 data+fit overlay as a :class:`~isar.plotspec.spec.PlotSpec`.

    Plain data only (no workspace_api import): measured ``(x, counts, yerr)``; the delivered curve
    ``(x_fit, total)`` and its ``components`` (a list of ``(name, values)`` over ``x_fit``); ω₀ with
    its interval + reliability flag; pooled/members provenance for the honesty channel.

    Rendering discipline (binding): a **pooled master** draws a data overlay only when its validated,
    persisted combined spectrum was supplied. It never substitutes one member's counts under a
    "pooled" title. A legacy pooled spec remains curve-only and carries a "member data not shown"
    annotation; its members are still enumerated in ``meta``. The ω₀ marker shows an interval whisker
    ONLY when the
    interval is reliable — an unreliable ω₀ is the sanctioned warning-marker + ``"(unreliable)"``
    annotation (via :func:`~isar.plotspec.spec.mark_unreliable`), never a numeric whisker.
    """
    series: list[ps.Series] = []
    annotations: list[ps.Annotation] = []
    # Honesty law: a pooled master shows no member counts (curve-only + a note). Per-scan fits draw
    # their measured data with Poisson whiskers.
    if pooled and not pooled_data:
        n = n_members if n_members else "?"
        annotations.append(ps.Annotation(
            text=f"pooled master — member data not shown ({n} members)", xy=None,
            role=ps.State.POOLED))
    elif counts is not None:
        x = np.asarray(x, dtype=float)
        counts = np.asarray(counts, dtype=float)
        yerr = None if yerr is None else np.asarray(yerr, dtype=float)
        if x.shape != counts.shape:
            raise ValueError("fit overlay x and counts must have the same shape")
        if yerr is not None and yerr.shape != counts.shape:
            raise ValueError("fit overlay yerr and counts must have the same shape")
        shown = _shown_mask(mask, counts, "fit overlay")
        # QC is mask-not-delete: accepted values retain their Poisson whiskers; rejected values
        # remain in the spec as distinct hollow markers below.
        series.append(ps.Series(x=x[shown], y=counts[shown],
                                yerr=None if yerr is None else yerr[shown],
                                label="data (+/-sqrt N)",
                                role=ps.Role.DATA))

    if x_fit is not None and total is not None:
        xf = np.asarray(x_fit, dtype=float)
        if pooled:
            fit_label = (f"pooled master ({n_members} members)"
                         if n_members else "pooled master")
        else:
            fit_label = "fit"
        series.append(ps.Series(x=xf, y=np.asarray(total, dtype=float), label=fit_label,
                                role=ps.Role.FIT))
        for name, values in (components or []):
            series.append(ps.Series(x=xf, y=np.asarray(values, dtype=float), label=str(name),
                                    role=ps.Role.COMPONENT))

    markers: list[ps.Marker] = []
    if counts is not None and (not pooled or pooled_data):
        markers.extend(ps.Marker(x=float(px), y=float(py), kind=ps.MarkerKind.MASKED,
                                 label="masked (shown, not used in fit)")
                       for px, py in zip(x[~shown], counts[~shown]))
    # ω₀ marker — reliable interval => a whisker; unreliable => warning-marker + annotation only.
    grid_x = x_fit if (x_fit is not None and total is not None) else x
    grid_y = total if (x_fit is not None and total is not None) else counts
    if omega0 is not None and grid_y is not None:
        y0 = _interval_at(grid_x, grid_y, omega0)
        if interval_reliable and interval is not None and len(interval) >= 2:
            lo, hi = float(interval[0]), float(interval[-1])
            series.append(ps.Series(x=np.array([float(omega0)]), y=np.array([y0]),
                                    errors=ps.ErrorBars(
                                        left=np.array([float(omega0) - lo]),
                                        right=np.array([hi - float(omega0)])),
                                    label="ω₀ (interval)", role=ps.Role.FIT))
        else:
            markers.append(ps.Marker(x=float(omega0), y=y0, kind=ps.MarkerKind.WARNING,
                                     label="ω₀ (unreliable)"))
            annotations.append(ps.Annotation(text="(unreliable)", xy=(float(omega0), y0),
                                              role=ps.State.UNRELIABLE))

    projection = None
    if q is not None:
        qr = [round(float(v), 3) for v in q]
        projection = ps.ProjectionLegend(plane="E", integration=f"Q = {qr} (fixed)")

    if title is None:
        base = f"scan {scan_number}" if scan_number is not None else "fit"
        if det_id:
            base += f"  {det_id}"
        title = f"pooled — {base}" if pooled else base

    meta = {"scan_number": scan_number, "det_id": det_id,
            "q": None if q is None else list(q),
            "pooled": bool(pooled), "n_members": n_members,
            "members": list(members) if members else None,
            "omega0": None if omega0 is None else float(omega0),
            "interval": None if interval is None else [float(v) for v in interval],
            "interval_reliable": bool(interval_reliable)}
    # Keep the legacy PlotSpec byte-for-byte stable unless pooled data is genuinely available.
    if pooled_data:
        meta["pooled_data"] = True
    return ps.PlotSpec(
        title=title, xlabel=xlabel, ylabel=ylabel,
        series=series, markers=markers, annotations=annotations, projection=projection,
        meta=meta,
    )


_VERDICT_MARKER = {
    "resolved": ps.MarkerKind.VERDICT_RESOLVED,
    "flagged": ps.MarkerKind.VERDICT_FLAGGED,
    "held": ps.MarkerKind.VERDICT_HELD,
}


def dispersion_spec(*, points, projection, prior=None, branches=None,
                    off_curve=None, color_norm=None, color_by_entropy=False,
                    title="dispersion ω̂(q)", xlabel="q (rlu)",
                    ylabel="energy transfer (meV)", value_label=None, meta=None):
    """The C5 dispersion view (ω̂ vs q) as a :class:`~isar.plotspec.spec.PlotSpec` — the heatmap's
    1-D sibling, same honesty law, plain data only (no workspace_api).

    ``points`` is a list of per-ω̂ dicts: ``q`` and ``omega`` (required); ``yerr`` — the certifying
    half-width (symmetric, from the interval ladder) or ``None`` when NO tier certifies; ``entropy``
    (assignment entropy or None); ``verdict`` (resolved/flagged/held or None); ``pooled`` (bool) and
    ``n_members`` for a combine-before-divide master.

    Binding rendering discipline:

    * a point with a ``yerr`` is a certifying ω̂ — it joins the DATA scatter with that whisker; a
      point with ``yerr is None`` is uncertified — a WARNING :class:`~isar.plotspec.spec.Marker`
      and a SINGLE ``"(unreliable)"`` note, never a numeric whisker;
    * the assignment-entropy colour channel rides ONLY on the whiskered scatter (values aligned to
      that subset) and its legend note appears ONLY when at least one whiskered point carries an
      entropy value — the note never asserts an encoding that isn't on screen;
    * a POOLED master is the delivered per-q answer: it is drawn on its own DATA scatter tagged
      :data:`~isar.plotspec.spec.State.POOLED` and labeled ``"pooled (N)"``, carrying a whisker per
      the same certify law (a master owns its own interval; ``yerr is None`` → a warning marker), so
      it is visible AND distinguishable from the per-scan member points, which stay shown;
    * ``prior`` (``(xs, ys)``) draws as a :data:`~isar.plotspec.spec.Role.PRIOR` curve; ``branches``
      (list of ``(label, xs, ys)``) as :data:`~isar.plotspec.spec.Role.BRANCH` curves — both optional;
    * ``off_curve`` (list of ``{q, omega, n_sigma}``) annotates points that sit >kσ off the prior;
    * the :class:`~isar.plotspec.spec.ProjectionLegend` is MANDATORY (G9 4D-honesty) — ``None`` raises.
    """
    if projection is None:
        raise ValueError("dispersion_spec requires a ProjectionLegend (declared projection "
                         "is always on screen; G9 4D-honesty)")

    if points and all("display_state" in point for point in points):
        return _state_first_dispersion_spec(
            points=points, projection=projection, prior=prior, branches=branches,
            off_curve=off_curve, color_norm=color_norm,
            color_by_entropy=color_by_entropy, title=title, xlabel=xlabel,
            ylabel=ylabel, value_label=value_label, meta=meta)

    series: list[ps.Series] = []
    markers: list[ps.Marker] = []
    annotations: list[ps.Annotation] = []

    rel_x, rel_y, rel_err, rel_ent = [], [], [], []
    provisional_x, provisional_y, provisional_err = [], [], []
    uncertified_x, uncertified_y = [], []
    candidate_label_added = False
    pool_groups: dict[tuple[str, str], dict[str, list]] = {}
    any_unreliable = False
    for p in points or []:
        x = float(p["q"])
        y = float(p["omega"])
        ent = p.get("entropy")
        ent = None if ent is None else float(ent)
        yerr = p.get("yerr")
        if p.get("selected") is False:
            # Candidate modes remain visible as context, but never masquerade as the delivered
            # branch answer, receive a certifying whisker, or join a dispersion line.
            markers.append(ps.Marker(
                x=x, y=y, kind=ps.MarkerKind.AMBIGUOUS_CANDIDATE,
                label=("ambiguous mode candidates" if not candidate_label_added else None)))
            candidate_label_added = True
            continue
        if p.get("pooled"):
            # Pooled master: its OWN interval (not a member's) whiskers it, per certify law; no
            # certifying interval → a pooled hollow-square warning.  Verdict is folded into this
            # single glyph rather than painted as a second marker on top of it.
            verdict = (p.get("verdict") or "").lower()
            track_state = (p.get("track_state") or "").lower()
            if yerr is not None:
                group = pool_groups.setdefault((verdict, track_state), {
                    "x": [], "y": [], "err": [], "ent": [], "n": []})
                group["x"].append(x)
                group["y"].append(y)
                group["err"].append(float(yerr))
                group["ent"].append(ent)
                group["n"].append(p.get("n_members"))
            else:
                explicitly_unreliable = p.get("interval_reliable") is False
                suffix = f" · verdict: {verdict}" if verdict else ""
                if explicitly_unreliable:
                    any_unreliable = True
                    markers.append(ps.Marker(x=x, y=y, kind=ps.MarkerKind.POOLED_WARNING,
                                             label=f"pooled ω̂ (unreliable){suffix}"))
                else:
                    markers.append(ps.Marker(
                        x=x, y=y, kind=ps.MarkerKind.POOLED_UNCERTIFIED,
                        label=f"pooled master (no certifying interval){suffix}"))
        elif yerr is not None:
            if str(p.get("track_state") or "").lower() == "provisional":
                provisional_x.append(x)
                provisional_y.append(y)
                provisional_err.append(float(yerr))
            else:
                rel_x.append(x)
                rel_y.append(y)
                rel_err.append(float(yerr))
                rel_ent.append(ent)
        else:
            if p.get("interval_reliable") is False:
                any_unreliable = True
                markers.append(ps.Marker(x=x, y=y, kind=ps.MarkerKind.WARNING,
                                         label="ω̂ (unreliable)"))
            else:
                uncertified_x.append(x)
                uncertified_y.append(y)
        kind = _VERDICT_MARKER.get((p.get("verdict") or "").lower())
        if (kind is not None and not p.get("pooled")
                and str(p.get("track_state") or "").lower() != "provisional"):
            markers.append(ps.Marker(x=x, y=y, kind=kind, label=f"verdict: {p['verdict']}"))

    # Prior + branch overlays first (drawn under the scatter).
    if prior is not None:
        px, py = prior
        series.append(ps.Series(x=np.asarray(px, float), y=np.asarray(py, float),
                                label="dispersion prior", role=ps.Role.PRIOR))
    series.extend(_branch_series(branches))
    for branch in branches or ():
        if isinstance(branch, dict) and branch.get("endpoint") is not None:
            endpoint = branch["endpoint"]
            annotations.append(ps.Annotation(
                text=str(branch.get("label") or branch.get("branch_id") or "branch"),
                xy=(float(endpoint[0]), float(endpoint[1]))))

    if uncertified_x:
        series.append(ps.Series(
            x=np.asarray(uncertified_x, float), y=np.asarray(uncertified_y, float),
            label="individual estimates (no certifying interval)",
            role=ps.Role.DATA, style="muted"))

    # The entropy colour channel rides ONLY on the certifying (whiskered) scatter; attach it — and
    # its legend note — ONLY when at least one such point carries an entropy value.
    colored = bool(rel_x) and any(e is not None for e in rel_ent)
    if rel_x:
        series.append(ps.Series(
            x=np.asarray(rel_x, float), y=np.asarray(rel_y, float), yerr=np.asarray(rel_err, float),
            label="ω̂ (certifying interval)", role=ps.Role.DATA,
            state=(ps.State.ENTROPY if colored else None),
            color_values=(list(rel_ent) if colored else None),
            color_norm=((color_norm if color_norm is not None else ps.LN2) if colored else None)))
    if provisional_x:
        series.append(ps.Series(
            x=np.asarray(provisional_x, float), y=np.asarray(provisional_y, float),
            yerr=np.asarray(provisional_err, float), label="provisional bridge",
            role=ps.Role.DATA, style="provisional"))
    for (verdict, track_state), group in pool_groups.items():
        # A style group keeps verdict/provisional semantics on the square itself; unlike the old
        # separate verdict marker, it cannot obscure the authoritative pooled master.
        p_colored = any(e is not None for e in group["ent"])
        ns = [n for n in group["n"] if n is not None]
        if len(group["x"]) == 1 and ns:
            plabel = f"pooled master ({int(ns[0])})"
        elif ns:
            plabel = f"pooled masters ({len(group['x'])})"
        else:
            plabel = "pooled master"
        styles = []
        if verdict in _VERDICT_MARKER:
            styles.append(f"verdict_{verdict}")
        if track_state == "provisional":
            styles.append("provisional")
        series.append(ps.Series(
            x=np.asarray(group["x"], float), y=np.asarray(group["y"], float),
            yerr=np.asarray(group["err"], float), label=plabel, role=ps.Role.DATA,
            state=ps.State.POOLED, style=" ".join(styles) or None,
            color_values=(list(group["ent"]) if p_colored else None),
            color_norm=((color_norm if color_norm is not None else ps.LN2) if p_colored else None)))
        colored = colored or p_colored

    if any_unreliable:
        annotations.append(ps.Annotation(text="(unreliable)", xy=None, role=ps.State.UNRELIABLE))
    if colored:
        annotations.append(ps.Annotation(
            text="color = assignment entropy (ln 2 = undecided)", xy=None))

    for oc in (off_curve or []):
        nσ = oc.get("n_sigma")
        note = "off prior" if nσ is None else f"off prior ({float(nσ):.1f}σ)"
        annotations.append(ps.Annotation(text=note, xy=(float(oc["q"]), float(oc["omega"]))))

    return ps.PlotSpec(
        title=title, xlabel=xlabel, ylabel=ylabel,
        series=series, markers=markers, annotations=annotations,
        projection=projection, value_label=value_label, meta=dict(meta or {}))


_DISPLAY_GROUPS = (
    ("individual_valid", ps.Glyph.INDIVIDUAL_VALID, "usable individual evidence"),
    ("individual_uncertified", ps.Glyph.INDIVIDUAL_UNCERTIFIED,
     "individual evidence (no certifying interval)"),
    ("individual_unreliable", ps.Glyph.INDIVIDUAL_UNRELIABLE,
     "individual evidence (unreliable)"),
    ("master_target", ps.Glyph.MASTER_TARGET, "pooled master (target, uncertified)"),
    ("master_ambiguous", ps.Glyph.MASTER_AMBIGUOUS, "pooled master (ambiguous)"),
    ("master_mismatch", ps.Glyph.MASTER_MISMATCH, "pooled master (target mismatch)"),
    ("master_certified", ps.Glyph.MASTER_CERTIFIED, "pooled master (certified)"),
    ("master_unassessed", ps.Glyph.MASTER_UNASSESSED, "pooled master (legacy unassessed)"),
    ("unselected_candidate", ps.Glyph.UNSELECTED_CANDIDATE, "unselected candidates"),
)


def _state_first_dispersion_spec(*, points, projection, prior, branches, off_curve,
                                 color_norm, color_by_entropy, title, xlabel, ylabel,
                                 value_label, meta):
    """One series per semantic state, emitted in fixed legend order."""
    series: list[ps.Series] = []
    context_series: list[ps.Series] = []
    annotations: list[ps.Annotation] = []
    if prior is not None:
        px, py = prior
        context_series.append(ps.Series(
            x=np.asarray(px, float), y=np.asarray(py, float),
            label="dispersion prior", role=ps.Role.PRIOR))
    context_series.extend(_branch_series(branches))
    for branch in branches or ():
        if isinstance(branch, dict) and branch.get("endpoint") is not None:
            endpoint = branch["endpoint"]
            annotations.append(ps.Annotation(
                text=str(branch.get("label") or branch.get("branch_id") or "branch"),
                xy=(float(endpoint[0]), float(endpoint[1]))))

    for state, glyph, label in _DISPLAY_GROUPS:
        rows = [point for point in points if str(point.get("display_state")) == state]
        if not rows:
            continue
        xs = np.asarray([float(row["q"]) for row in rows], dtype=float)
        ys = np.asarray([float(row["omega"]) for row in rows], dtype=float)
        bottom, top, any_error = [], [], False
        for row, y in zip(rows, ys):
            interval = row.get("interval")
            if interval is not None and len(interval) >= 2:
                bottom.append(y - float(interval[0]))
                top.append(float(interval[-1]) - y)
                any_error = True
            elif row.get("yerr") is not None:
                bottom.append(float(row["yerr"]))
                top.append(float(row["yerr"]))
                any_error = True
            else:
                bottom.append(0.0)
                top.append(0.0)
        errors = (ps.ErrorBars(bottom=np.asarray(bottom), top=np.asarray(top))
                  if any_error and glyph not in {
                      ps.Glyph.INDIVIDUAL_UNRELIABLE,
                      ps.Glyph.UNSELECTED_CANDIDATE,
                  } else None)
        entropy = [row.get("entropy") for row in rows]
        colored = color_by_entropy
        legacy_state = (
            ps.State.UNRELIABLE if glyph is ps.Glyph.INDIVIDUAL_UNRELIABLE
            else ps.State.POOLED if glyph in {
                ps.Glyph.MASTER_TARGET, ps.Glyph.MASTER_AMBIGUOUS,
                ps.Glyph.MASTER_MISMATCH, ps.Glyph.MASTER_CERTIFIED,
                ps.Glyph.MASTER_UNASSESSED,
            } else None)
        series.append(ps.Series(
            x=xs, y=ys, errors=errors, label=label, role=ps.Role.DATA, glyph=glyph,
            state=legacy_state,
            color_values=(entropy if colored else None),
            color_norm=((color_norm if color_norm is not None else ps.LN2) if colored else None)))

    series.extend(context_series)
    if color_by_entropy:
        annotations.append(ps.Annotation(
            text="color = assignment entropy (ln 2 = undecided)", xy=None))
    for oc in off_curve or ():
        n_sigma = oc.get("n_sigma")
        note = "off prior" if n_sigma is None else f"off prior ({float(n_sigma):.1f}σ)"
        annotations.append(ps.Annotation(
            text=note, xy=(float(oc["q"]), float(oc["omega"]))))
    return ps.PlotSpec(
        title=title, xlabel=xlabel, ylabel=ylabel, series=series,
        annotations=annotations, projection=projection, value_label=value_label,
        meta=dict(meta or {}))


def _branch_series(branches) -> list[ps.Series]:
    """Normalize old ``(label, x, y)`` branches and additive segmented track dictionaries.

    A segment is a deliberate connectivity assertion.  Missing/ambiguous/blocked segments are not
    rendered as curves, so two confirmed islands can never be joined by an invented line.  Rival
    (normally acoustic) tracks are dashed; provisional segments are dashed independently and carry
    their own series, never extending a confirmed segment.
    """
    out: list[ps.Series] = []
    for branch in branches or []:
        if not isinstance(branch, dict):
            label, bx, by = branch
            out.append(ps.Series(x=np.asarray(bx, float), y=np.asarray(by, float),
                                 label=str(label), role=ps.Role.BRANCH))
            continue
        label = str(branch.get("label") or branch.get("branch_id") or "branch")
        role = str(branch.get("role") or "").lower()
        base_style = "rival" if role in {"rival", "context", "acoustic"} \
            or str(branch.get("branch_id") or "").lower() == "acoustic" else ""
        segments = branch.get("segments")
        if not isinstance(segments, list):
            segments = [branch]
        labeled = False
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            state = str(segment.get("state") or branch.get("state") or "confirmed").lower()
            if state in {"missing", "ambiguous", "blocked", "frontier_blocked"}:
                continue
            xy = _segment_xy(segment)
            if xy is None:
                continue
            sx, sy = xy
            if len(sx) < 2:
                continue
            styles = [base_style] if base_style else []
            if state == "provisional":
                styles.append("provisional")
            out.append(ps.Series(
                x=sx, y=sy, label=(label if not labeled else None), role=ps.Role.BRANCH,
                style=" ".join(styles) or None))
            labeled = True
    return out


def _segment_xy(segment) -> tuple[np.ndarray, np.ndarray] | None:
    """Extract one branch segment from either explicit arrays or a point dictionary list."""
    xs = segment.get("x", segment.get("q"))
    ys = segment.get("y", segment.get("omega", segment.get("energy_meV")))
    if xs is not None and ys is not None and not isinstance(xs, dict):
        try:
            xa, ya = np.asarray(xs, float), np.asarray(ys, float)
            if xa.ndim == ya.ndim == 1 and xa.shape == ya.shape:
                return xa, ya
        except (TypeError, ValueError):
            pass
    points = segment.get("points")
    if not isinstance(points, list):
        return None
    pairs = []
    for point in points:
        if not isinstance(point, dict):
            continue
        x = point.get("x", point.get("q"))
        if isinstance(x, (list, tuple, np.ndarray)):
            # Projected callers should normally provide scalar x; accepting the first component
            # keeps a standalone builder useful for the common H path without guessing a plane.
            x = x[0] if len(x) else None
        y = point.get("y", point.get("omega", point.get("energy_meV")))
        try:
            if x is not None and y is not None:
                pairs.append((float(x), float(y)))
        except (TypeError, ValueError):
            continue
    if not pairs:
        return None
    return np.asarray([p[0] for p in pairs]), np.asarray([p[1] for p in pairs])


def waterfall_spec(*, traces, offset_step, q=None, axis_label=None,
                   title="waterfall", xlabel="energy transfer (meV)",
                   ylabel="counts (offset per trace)", meta=None) -> ps.PlotSpec:
    """The C4 waterfall view: one DATA+FIT trace pair per ``traces`` entry, stacked with a vertical
    offset (``i * offset_step`` for the i-th trace, 0-indexed) — plain data only, no workspace_api.

    ``traces`` is a list of per-trace dicts: ``label`` (e.g. ``"T=12 K"``), ``x``/``counts``/``yerr``
    (the measured spectrum; ``counts`` is ``None`` for a pooled trace), ``curve_x``/``curve_total``
    (the delivered fit curve), ``pooled`` (bool), ``n_members``.

    The offset is a pure VIEW-STATE display device (G10: never written to the workspace/card) — so
    it is ALWAYS declared on screen via an Annotation (``"offset +<step> per trace"``), the honesty
    rule for stacked plots. ``yerr`` is preserved at its true magnitude (NOT shifted) since it is a
    property of the data, not of the display offset.

    A **pooled** waterfall trace remains curve-only until this trace contract carries a validated
    combined spectrum. Its fit-curve label carries ``"pooled (N) — member data not shown"`` so the
    missing counts are never silently implied to be a real measured trace.

    ``q``/``axis_label`` (optional) add the fixed-q honesty declaration as a :class:`ProjectionLegend`
    (this view fixes q and varies T — the ``dispersion_spec`` mandatory-projection rule doesn't apply
    here since q, not a plane, is what's fixed, but the fixed value is still worth declaring)."""
    series: list[ps.Series] = []
    annotations: list[ps.Annotation] = []

    for i, tr in enumerate(traces or []):
        offset = i * float(offset_step)
        label = tr.get("label") or f"trace {i}"
        pooled = bool(tr.get("pooled"))
        n_members = tr.get("n_members")

        if pooled:
            annotations.append(ps.Annotation(
                text=f"{label}: pooled master ({n_members or '?'}) — member data not shown",
                xy=None, role=ps.State.POOLED))
        elif tr.get("counts") is not None:
            x = np.asarray(tr["x"], dtype=float)
            y = np.asarray(tr["counts"], dtype=float) + offset
            yerr = None if tr.get("yerr") is None else np.asarray(tr["yerr"], dtype=float)
            series.append(ps.Series(x=x, y=y, yerr=yerr, label=label, role=ps.Role.DATA))

        if tr.get("curve_x") is not None and tr.get("curve_total") is not None:
            xf = np.asarray(tr["curve_x"], dtype=float)
            yf = np.asarray(tr["curve_total"], dtype=float) + offset
            fit_label = (f"{label}: pooled master ({n_members or '?'}) — member data not shown"
                        if pooled else label)
            series.append(ps.Series(x=xf, y=yf, label=fit_label, role=ps.Role.FIT))

    annotations.append(ps.Annotation(text=f"offset +{float(offset_step):g} per trace", xy=None))

    projection = None
    if q is not None:
        axis = axis_label or "q"
        projection = ps.ProjectionLegend(plane="E (per T)", integration=f"{axis}={float(q):g} (fixed)")

    return ps.PlotSpec(
        title=title, xlabel=xlabel, ylabel=ylabel,
        series=series, annotations=annotations, projection=projection,
        meta=dict(meta or {}),
    )


def residuals_spec(*, x, residuals, yerr=None, mask=None, xlabel="energy transfer (meV)",
                   ylabel="data - fit (counts)", title="residuals"):
    """Residuals (measured - delivered curve, on the measured grid) as a RESIDUAL-role PlotSpec.

    A single :data:`Role.RESIDUAL` series over the data grid; ``yerr`` (Poisson whiskers) optional.
    The zero line is implicit — no separate series, so the spec stays minimal and golden-stable.
    """
    x = np.asarray(x, dtype=float)
    r = np.asarray(residuals, dtype=float)
    yerr = None if yerr is None else np.asarray(yerr, dtype=float)
    if x.shape != r.shape:
        raise ValueError("residual x and values must have the same shape")
    if yerr is not None and yerr.shape != r.shape:
        raise ValueError("residual yerr and values must have the same shape")
    shown = _shown_mask(mask, r, "residual")
    return ps.PlotSpec(
        title=title, xlabel=xlabel, ylabel=ylabel,
        series=[ps.Series(x=x[shown], y=r[shown],
                          yerr=None if yerr is None else yerr[shown],
                          label="residual", role=ps.Role.RESIDUAL)],
        markers=[ps.Marker(x=float(px), y=float(py), kind=ps.MarkerKind.MASKED,
                           label="masked residual (shown, not used in fit)")
                 for px, py in zip(x[~shown], r[~shown])],
    )


def _shown_mask(mask, values, context: str) -> np.ndarray:
    """Validate and return a boolean fit/QC mask without changing data values.

    ``True`` means the point participated in fitting.  A missing mask preserves legacy behavior;
    a malformed mask is a visible caller error rather than silently misaligning scientific points.
    """
    if mask is None:
        return np.ones(values.shape, dtype=bool)
    out = np.asarray(mask, dtype=bool)
    if out.shape != values.shape:
        raise ValueError(f"{context} mask and values must have the same shape")
    return out

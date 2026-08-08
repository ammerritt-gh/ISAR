"""Fit-level figures for the run report (UNIFICATION_DESIGN §7).

Factored here so the report layer — not a one-off check script — owns the dispersion-summary plot.
The figure is built from ledger outcomes only (no refitting), so `isar report` is cheap.
"""
from __future__ import annotations

import io

import numpy as np

_DISPOSITION_COLOR = {
    "resolved": "#2266cc",
    "flagged": "#e08a00",
    "reject": "#cc3333",
    "abstain": "#999999",
}


def _dominant_component(qs: np.ndarray) -> int:
    if qs.shape[0] < 2:
        return 1
    return int(np.argmax(qs.max(axis=0) - qs.min(axis=0)))


def dispersion_figure(outcomes, *, sample: str | None = None) -> bytes:
    """omega0 vs the dominant-varying Q component, colored by disposition, intervals as error bars.

    `outcomes` is a list of FitOutcome-like dicts (from the ledger). Held/declined scans with no
    omega0 are shown as a rug along the bottom so coverage holes are visible.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    have_omega = [o for o in outcomes if o.get("omega0") is not None and o.get("q")]
    fig, ax = plt.subplots(figsize=(6.2, 4.4))

    if have_omega:
        qs = np.array([o["q"] for o in have_omega], dtype=float)
        ci = _dominant_component(qs)
        comp = "hkl"[ci]
        for disp, color in _DISPOSITION_COLOR.items():
            sel = [o for o in have_omega if o.get("disposition") == disp]
            if not sel:
                continue
            x = [float(o["q"][ci]) for o in sel]
            y = [float(o["omega0"]) for o in sel]
            yerr = _errbars(sel, y)
            ax.errorbar(x, y, yerr=yerr, fmt="o", color=color, ecolor=color, elinewidth=1,
                        capsize=2, label=disp, markersize=5)
        # rug: scans with no omega (held/declined) at their Q, along the axis bottom
        rug = [o for o in outcomes if o.get("omega0") is None and o.get("q")]
        if rug:
            ymin = ax.get_ylim()[0]
            ax.plot([float(o["q"][ci]) for o in rug], [ymin] * len(rug), "|",
                    color="#bbbbbb", markersize=10, label="no fit (held/declined)")
        ax.set_xlabel(f"Q component {comp} (rlu)")
        ax.set_ylabel("omega0 (meV)")
        ax.legend(fontsize=8, loc="best")
    else:
        ax.text(0.5, 0.5, "no fitted phonons yet", ha="center", va="center", transform=ax.transAxes)
        ax.set_xlabel("Q (rlu)"); ax.set_ylabel("omega0 (meV)")

    title = "Dispersion summary" + (f" — {sample}" if sample else "")
    ax.set_title(title)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110)
    plt.close(fig)
    return buf.getvalue()


_REASON_MARKER = {
    # reason enum → (matplotlib marker, filled?, on-rug?)
    "met": ("o", True, False),
    "uncertifiable_interval": ("D", False, False),   # open diamond, no error bar
    "interval_floored": ("^", True, False),          # filled triangle
    "fast_tier_only": ("D", False, False),
    "no_signal": ("x", False, True),                 # × on the bottom rug
    "elastic_only": ("x", False, True),              # phonon-less winner: same rug as no_signal
    "infeasible": ("x", False, True),
    "signal_unmodeled": ("x", False, True),
    "info_saturated": ("s", False, False),
}


def campaign_dispersion_figure(per_q_rows, score_rows=None):
    """Delivered ω₀ vs the dominant q, calibrated halfwidth bars, truth curve, reason-coded markers.

    A NEW campaign-level figure (leaves :func:`dispersion_figure` untouched). ``per_q_rows`` are the
    diagnostic renderer's per-q rows (``hkl``, ``q``, ``omega0``, ``calibrated_halfwidth``, ``reason``,
    ``interval_reliable``); ``score_rows`` (optional) are score.json rows (``hkl``, ``omega_true``)
    that draw the truth line. Converged points are filled with error bars; uncertifiable/fast-tier
    points are open markers WITHOUT error bars; no-signal/infeasible q are × on the bottom rug.

    Returns a matplotlib Figure (the caller captions + saves it)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    hkls = [r.get("hkl") for r in per_q_rows if r.get("hkl")]
    fig, ax = plt.subplots(figsize=(6.4, 4.4))
    if not hkls:
        ax.text(0.5, 0.5, "no grid points", ha="center", va="center", transform=ax.transAxes)
        ax.set_xlabel("q (rlu)"); ax.set_ylabel("omega0 (meV)")
        return fig

    ci = _dominant_component(np.array([[float(v) for v in h] for h in hkls], dtype=float))
    comp = "hkl"[ci]

    # truth curve
    if score_rows:
        pts = sorted(((float(s["hkl"][ci]), float(s["omega_true"]))
                      for s in score_rows if s.get("hkl") and s.get("omega_true") is not None),
                     key=lambda t: t[0])
        if pts:
            ax.plot([p[0] for p in pts], [p[1] for p in pts], "-", color="0.6", lw=1.2,
                    alpha=0.9, label="truth", zorder=1)

    seen_labels: set = set()
    ymin = None
    for r in per_q_rows:
        hkl = r.get("hkl")
        if not hkl:
            continue
        x = float(hkl[ci])
        reason = r.get("reason") or ("met" if r.get("converged") else None)
        marker, filled, on_rug = _REASON_MARKER.get(reason, ("o", True, False))
        omega = r.get("omega0")
        if on_rug or omega is None:
            # coverage-hole rug along the bottom
            if omega is None:
                continue
            ax.plot([x], [omega], marker, color="#bbbbbb", markersize=8, zorder=2)
            continue
        y = float(omega)
        ymin = y if ymin is None else min(ymin, y)
        color = "#2266cc" if filled else "#e08a00"
        hw = r.get("calibrated_halfwidth")
        label = reason if reason not in seen_labels else None
        seen_labels.add(reason)
        if filled and hw is not None and r.get("interval_reliable"):
            ax.errorbar([x], [y], yerr=[[hw], [hw]], fmt=marker, color=color, ecolor=color,
                        elinewidth=1, capsize=2, markersize=6, label=label, zorder=3)
        else:
            ax.plot([x], [y], marker, markerfacecolor="none" if not filled else color,
                    markeredgecolor=color, markersize=7, label=label, zorder=3)

    ax.set_xlabel(f"q component {comp} (rlu)")
    ax.set_ylabel("omega0 (meV)")
    ax.set_title("Delivered dispersion vs truth")
    ax.legend(fontsize=8, loc="best")
    fig.tight_layout()
    return fig


def _errbars(sel, y):
    lo, hi = [], []
    for o, yi in zip(sel, y):
        iv = o.get("interval")
        if iv and len(iv) >= 2:
            lo.append(max(0.0, yi - float(iv[0])))
            hi.append(max(0.0, float(iv[-1]) - yi))
        else:
            lo.append(0.0)
            hi.append(0.0)
    return [lo, hi]

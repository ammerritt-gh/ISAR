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

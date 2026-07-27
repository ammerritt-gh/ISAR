"""Shared, Qt-free certifying-interval + interval-ladder derivation (Task 11, A-5).

Factored verbatim out of the C2 inspector VM (Task 10) so the heatmap (A-5) and the dispersion view
(A-6) derive the certifying tier with byte-identical semantics — the inspector, the heatmap ω̂
whisker, and the dispersion band must never disagree about which tier certifies.

Pure functions over a :class:`~isar.workspace_api.FitView`-like object (only its interval-ladder
attributes are read); no Qt, no workspace writes. The certifying rule follows ``FitOutcome``
(isar/session/driver.py) exactly:

* ``interval_reliable=False`` suppresses the bootstrap interval, not the lower ladder rungs;
* bootstrap ``interval`` is authoritative and certifies when present on a reliable fit;
* else the profile-likelihood ``omega0_profile`` certifies when present;
* else the HESSE ``hesse_interval`` certifies **only** when ``hesse_trusted``;
* the bayes posterior is **diagnostics-only** — it appears on the ladder but NEVER certifies.

``interval_reliable=False`` renders the bootstrap row as ``"(unreliable)"`` — never numbers —
while a valid profile or trusted-HESSE fallback may still certify the fit.
"""
from __future__ import annotations

from dataclasses import dataclass

from isar.workspace_api.display import fit_certifying_interval


@dataclass
class LadderRow:
    """One interval-ladder tier as display-ready state. ``certifying`` is set on at most one row
    (bayes never). ``interval_text`` is the honest text — ``"(unreliable)"`` or ``"—"``, never
    numbers, when the tier does not carry a usable numeric range."""

    tier: str
    interval_text: str
    certifying: bool = False
    annotation: str = ""
    diagnostics_only: bool = False


def _fmt_interval(iv) -> str:
    lo, hi = float(iv[0]), float(iv[-1])
    return f"[{lo:.3f}, {hi:.3f}]"


def certifying_tier(fit) -> str | None:
    """The name of the tier that certifies ``fit`` (``"bootstrap"``/``"profile"``/``"hesse"``), or
    ``None`` when nothing does. Matches :func:`ladder_rows`'s certifying flags exactly."""
    interval = fit_certifying_interval(fit)
    return None if interval is None else interval.method


def certifying_interval(fit):
    """The numeric interval (lo..hi sequence) of the certifying tier — bayes never certifies — or
    ``None``. This is exactly the interval the ω̂ whisker may show; an uncertified ω̂ gets ``None``
    (so it is drawn as a warning marker, never a numeric whisker)."""
    interval = fit_certifying_interval(fit)
    return None if interval is None else (interval.lo, interval.hi)


def ladder_rows(fit) -> list:
    """The four interval-ladder rows (bootstrap / profile / hesse / bayes), labeled and with at most
    one ``certifying``. Moved verbatim from the inspector VM's ``_ladder_rows``."""
    boot = fit.interval
    reliable = fit.interval_reliable is not False
    prof = fit.omega0_profile
    hesse = fit.hesse_interval
    hesse_trusted = bool(fit.hesse_trusted)
    bayes = fit.bayes_interval

    certifying = certifying_tier(fit)
    boot_certifies = certifying == "bootstrap"
    prof_certifies = certifying == "profile"
    hesse_certifies = certifying == "hesse"

    # bootstrap: never numbers when unreliable
    if boot is None:
        boot_text = "—"
    elif not reliable:
        boot_text = "(unreliable)"
    else:
        boot_text = _fmt_interval(boot)
    rows = [LadderRow("bootstrap", boot_text, certifying=boot_certifies,
                      annotation=("authoritative" if boot_certifies else ""))]

    rows.append(LadderRow("profile", _fmt_interval(prof) if prof is not None else "—",
                          certifying=prof_certifies))

    hesse_note = "trusted" if hesse_trusted else ("not trusted" if hesse is not None else "")
    rows.append(LadderRow("hesse", _fmt_interval(hesse) if hesse is not None else "—",
                          certifying=hesse_certifies, annotation=hesse_note))

    ess = fit.bayes_ess
    bayes_note = "diagnostics-only"
    if ess is not None:
        bayes_note += f"; ESS={ess:.0f}"
    rows.append(LadderRow("bayes", _fmt_interval(bayes) if bayes is not None else "—",
                          certifying=False, annotation=bayes_note, diagnostics_only=True))
    return rows

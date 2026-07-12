"""Shared, Qt-free certifying-interval + interval-ladder derivation (Task 11, A-5).

Factored verbatim out of the C2 inspector VM (Task 10) so the heatmap (A-5) and the dispersion view
(A-6) derive the certifying tier with byte-identical semantics — the inspector, the heatmap ω̂
whisker, and the dispersion band must never disagree about which tier certifies.

Pure functions over a :class:`~isar.workspace_api.FitView`-like object (only its interval-ladder
attributes are read); no Qt, no workspace writes. The certifying rule follows ``FitOutcome``
(isar/session/driver.py) exactly:

* bootstrap ``interval`` is authoritative and certifies **when** ``interval_reliable`` and present;
* else the profile-likelihood ``omega0_profile`` certifies when present;
* else the HESSE ``hesse_interval`` certifies **only** when ``hesse_trusted``;
* the bayes posterior is **diagnostics-only** — it appears on the ladder but NEVER certifies.

``interval_reliable=False`` renders the bootstrap row as ``"(unreliable)"`` — never numbers.
"""
from __future__ import annotations

from dataclasses import dataclass


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
    boot = fit.interval
    reliable = fit.interval_reliable is not False
    prof = fit.omega0_profile
    hesse = fit.hesse_interval
    hesse_trusted = bool(fit.hesse_trusted)
    if reliable and boot is not None:
        return "bootstrap"
    if prof is not None:
        return "profile"
    if hesse_trusted and hesse is not None:
        return "hesse"
    return None


def certifying_interval(fit):
    """The numeric interval (lo..hi sequence) of the certifying tier — bayes never certifies — or
    ``None``. This is exactly the interval the ω̂ whisker may show; an uncertified ω̂ gets ``None``
    (so it is drawn as a warning marker, never a numeric whisker)."""
    tier = certifying_tier(fit)
    if tier == "bootstrap":
        return fit.interval
    if tier == "profile":
        return fit.omega0_profile
    if tier == "hesse":
        return fit.hesse_interval
    return None


def ladder_rows(fit) -> list:
    """The four interval-ladder rows (bootstrap / profile / hesse / bayes), labeled and with at most
    one ``certifying``. Moved verbatim from the inspector VM's ``_ladder_rows``."""
    boot = fit.interval
    reliable = fit.interval_reliable is not False
    prof = fit.omega0_profile
    hesse = fit.hesse_interval
    hesse_trusted = bool(fit.hesse_trusted)
    bayes = fit.bayes_interval

    boot_certifies = reliable and boot is not None
    prof_certifies = (not boot_certifies) and prof is not None
    hesse_certifies = (not boot_certifies and not prof_certifies
                       and hesse_trusted and hesse is not None)

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

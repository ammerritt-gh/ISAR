"""Physics-gated enumeration of admissible peak-structure hypotheses (§2).

Peak count is never a free knob: ``enumerate_structures`` returns only the structures the
physics permits for this scan's context. Step 1 ships two — ``stokes+bg`` and
``elastic+stokes+bg`` — with the elastic gate encoding the IN8 off-window-elastic
degeneracy fix. The gate vocabulary also names the anti-Stokes and background-kind gates
so the §2 physics lives in one place, even where a gate is trivial at step 1.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from isar.analyze.components import (
    Component, ConstantBackground, Elastic, PhononDHO, QuasiElastic,
)
from isar.synth._ff import KB

# Anti-Stokes is dropped only when its *predicted counts* — Stokes excursion × the
# detailed-balance weight exp(−ħω/kT) — cannot be told from the background Poisson noise.
# Detectability, not temperature alone: a weight-suppressed anti-Stokes on a huge Stokes peak
# is still resolvable (keep it); a moderately-suppressed one under a high background at short
# counting time is not (drop it). Governs the Stokes-only high-q rescue (steering Phase 3a).
AS_DETECT_Z = 3.0        # predicted anti-Stokes must clear this × √background to be resolvable
AS_MIN_COUNTS = 2.0      # …and this absolute floor (a fraction of a count is never resolvable)


@dataclass(frozen=True)
class Hypothesis:
    """A named structure recipe: the ordered components that build its composite. Each entry is
    either a component *class* (instantiated with its default prefix) or a pre-built component
    *instance* (an explicit prefix override, e.g. ``PhononDHO(prefix="phonon2")`` for N-mode).
    """

    label: str
    components: tuple

    @property
    def k(self) -> int:
        """Number of free parameters (the model's degrees of freedom consumed)."""
        return self.build().n_params

    def build(self):
        """Instantiate the CompositeModel for this hypothesis. Classes are instantiated;
        pre-built instances (components are stateless) are reused as-is."""
        from isar.analyze.components import CompositeModel
        return CompositeModel([c() if isinstance(c, type) else c for c in self.components])


def elastic_admissible(ctx) -> bool:
    """Elastic line admissible iff its position is within the fitted window (± one
    resolution FWHM) and the energy axis is calibratable.

    The window test is the §2/§8 off-window-elastic guard: on IN8 FX=2 (EN≥1) the elastic
    at 0 sits below the window with no in-window leverage, so an elastic NNLS column would
    take an arbitrary degenerate amplitude — exclude it instead.
    """
    if ctx.energy_status == "needs_conversion_and_zero":
        return False
    lo, hi = ctx.window
    tol = ctx.gauss_fwhm
    return (lo - tol) <= ctx.center <= (hi + tol)


def quasielastic_admissible(ctx) -> bool:
    """A broad quasi-elastic component is admissible iff the experiment card declares its
    width (``ctx.quasielastic_fwhm`` is not None — a TAS card default, §8).

    Unlike the sharp :class:`~isar.analyze.components.Elastic`, a *broad* quasi-elastic has
    leverage across the window even when the elastic centre is off-window (the IN8 FX=2,
    EN≥1 case), so it carries no in-window gate. Whether it earns its extra parameter on a
    given scan is left to model selection (§4) — it is enumerated, not assumed.
    """
    return getattr(ctx, "quasielastic_fwhm", None) is not None


def _background_and_stokes_amp(ctx):
    """Robust (background, Stokes-peak excursion) in counts from the fitted points. Background =
    median (robust to the peak); excursion = max − background (conservative: an over-estimated
    Stokes amp over-estimates the predicted anti-Stokes, biasing toward *keeping* the pair — the
    safe direction). Returns (nan, 0.0) when there are no usable counts."""
    c = np.asarray(getattr(ctx, "counts_used", []), dtype=float)
    c = c[np.isfinite(c)]
    if c.size == 0:
        return float("nan"), 0.0
    bg = float(np.median(c))
    return bg, max(float(c.max()) - bg, 0.0)


def antistokes_mode(ctx) -> str:
    """How anti-Stokes is handled for this context.

    Default ``"tied"`` — the Bose tie inside :class:`PhononDHO` (negligible as T→0, growing
    with T), so a low-T gain-side feature is fit as the elastic line, not a phantom
    anti-Stokes peak. ``"tied"`` is also the *safe default* whenever the drop cannot be
    justified (no prior, no measurable Stokes peak): dropping a side removes model freedom, so
    do it only on positive evidence.

    ``"stokes_only"`` (steering Phase 3a, high-q rescue) when the anti-Stokes is **undetectable
    against the background**, not merely temperature-suppressed. The predicted anti-Stokes
    excursion is the measured Stokes excursion × the detailed-balance weight
    ``exp(−ħω/kT)`` (ħω from the dispersion prior ``ctx.prior_omega``); it is resolvable only if
    it clears ``AS_DETECT_Z × √background`` and the absolute ``AS_MIN_COUNTS`` floor. So a
    weight-suppressed anti-Stokes riding a strong Stokes peak stays ``tied`` (still resolvable),
    while a suppressed one under a high background / short count is dropped (buried in noise —
    the ``no_signal`` failure it otherwise causes). Combines temperature, background and
    amplitude, per the owner's note that usefulness depends on the noise floor, not just kT.
    A free/separate anti-Stokes component (high-T disambiguation, §4) remains a later extension.
    """
    e_pred = getattr(ctx, "prior_omega", None)
    T = getattr(ctx, "T", None)
    if e_pred is None or T is None:
        return "tied"
    e_pred, T = float(e_pred), float(T)
    if not (np.isfinite(e_pred) and e_pred > 0 and T > 0):
        return "tied"
    bg, stokes_amp = _background_and_stokes_amp(ctx)
    if not np.isfinite(bg) or stokes_amp <= 0:
        return "tied"                                      # no measurable Stokes peak → can't judge
    as_counts = stokes_amp * np.exp(-e_pred / (KB * T))    # predicted anti-Stokes excursion
    noise = np.sqrt(max(bg, 1.0))                          # Poisson background noise
    resolvable = as_counts >= AS_DETECT_Z * noise and as_counts >= AS_MIN_COUNTS
    return "tied" if resolvable else "stokes_only"


def enumerate_structures(ctx) -> list[Hypothesis]:
    """The admissible structure hypotheses for this scan context (§2).

    Always emits the single-phonon ``stokes+bg``; adds ``elastic+stokes+bg`` when the
    elastic line is admissible, ``stokes+qe+bg`` when the card declares a broad
    quasi-elastic width (§8 TAS), and ``elastic+stokes+qe+bg`` when both gates fire. The
    quasi-elastic is opt-in per card, not enumerated by default; model selection (§4)
    decides whether it earns its parameter. Second-phonon / sloped-BG remain later-step.
    """
    elastic = elastic_admissible(ctx)
    qe = quasielastic_admissible(ctx)
    pho = _phonon_for(ctx)                          # class (tied) or Stokes-only instance (Bose-dead)
    hyps = [Hypothesis("stokes+bg", (pho, ConstantBackground))]
    if elastic:
        hyps.append(Hypothesis("elastic+stokes+bg", (pho, Elastic, ConstantBackground)))
    if qe:
        hyps.append(Hypothesis("stokes+qe+bg", (pho, QuasiElastic, ConstantBackground)))
    if elastic and qe:
        hyps.append(Hypothesis("elastic+stokes+qe+bg",
                               (pho, Elastic, QuasiElastic, ConstantBackground)))
    return hyps


def _phonon_for(ctx, prefix: str | None = None):
    """The phonon *recipe* for this context: the bare :class:`PhononDHO` class (Bose-tied,
    default) or a Stokes-only pre-built instance when :func:`antistokes_mode` reports the
    anti-Stokes is Bose-dead (Phase 3a). Passing the instance rides the existing
    ``Hypothesis`` class-or-instance path (same mechanism as the ``phonon2`` override)."""
    if antistokes_mode(ctx) == "stokes_only":
        return PhononDHO(prefix=prefix, peak="stokes") if prefix else PhononDHO(peak="stokes")
    return PhononDHO(prefix=prefix) if prefix else PhononDHO


def enumerate_multimode(ctx, n_max: int = 2) -> list[Hypothesis]:
    """The two-mode (2-phonon) structure hypotheses for this scan context — the escalation set
    the two-stage loop (:mod:`isar.analyze.loop`) offers when the 1-mode winner's fit is poor
    or its residual is structured (§ multiplicity is data-gated, never a free knob).

    Mirrors :func:`enumerate_structures` under the identical elastic/quasi-elastic gates, but
    with a second phonon. Mode 1 keeps the default ``phonon`` prefix so each 1-mode hypothesis
    is *strictly nested* in its 2-mode counterpart (the LRT/AIC comparison in §4 is then exact);
    mode 2 gets the ``phonon2`` namespace. Capped at N=2 this round (``n_max`` is accepted for
    the later N=3 extension; values >2 are treated as 2)."""
    if n_max < 2:
        return []
    elastic = elastic_admissible(ctx)
    qe = quasielastic_admissible(ctx)

    def twophonon():
        # mode 1 (default "phonon" prefix), mode 2 ("phonon2"). Both honour the Bose-dead
        # Stokes-only choice so a two-mode high-q fit stays one-sided as well.
        return (_phonon_for(ctx), _phonon_for(ctx, prefix="phonon2"))

    hyps = [Hypothesis("stokes2+bg", (*twophonon(), ConstantBackground))]
    if elastic:
        hyps.append(Hypothesis("elastic+stokes2+bg",
                               (*twophonon(), Elastic, ConstantBackground)))
    if qe:
        hyps.append(Hypothesis("stokes2+qe+bg",
                               (*twophonon(), QuasiElastic, ConstantBackground)))
    if elastic and qe:
        hyps.append(Hypothesis("elastic+stokes2+qe+bg",
                               (*twophonon(), Elastic, QuasiElastic, ConstantBackground)))
    return hyps

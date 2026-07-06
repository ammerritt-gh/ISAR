"""Physics-gated enumeration of admissible peak-structure hypotheses (§2).

Peak count is never a free knob: ``enumerate_structures`` returns only the structures the
physics permits for this scan's context. Step 1 ships two — ``stokes+bg`` and
``elastic+stokes+bg`` — with the elastic gate encoding the IN8 off-window-elastic
degeneracy fix. The gate vocabulary also names the anti-Stokes and background-kind gates
so the §2 physics lives in one place, even where a gate is trivial at step 1.
"""
from __future__ import annotations

from dataclasses import dataclass

from isar.analyze.components import (
    Component, ConstantBackground, Elastic, PhononDHO, QuasiElastic,
)


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


def antistokes_mode(ctx) -> str:
    """How anti-Stokes is handled for this context. Step 1: always ``"tied"`` — the Bose
    tie inside :class:`PhononDHO` (negligible as T→0, growing with T), so a low-T gain-side
    feature is fit as the elastic line, not a phantom anti-Stokes peak. A free/separate
    anti-Stokes component (high-T disambiguation, §4) is a later extension.
    """
    return "tied"


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
    hyps = [Hypothesis("stokes+bg", (PhononDHO, ConstantBackground))]
    if elastic:
        hyps.append(Hypothesis("elastic+stokes+bg", (PhononDHO, Elastic, ConstantBackground)))
    if qe:
        hyps.append(Hypothesis("stokes+qe+bg", (PhononDHO, QuasiElastic, ConstantBackground)))
    if elastic and qe:
        hyps.append(Hypothesis("elastic+stokes+qe+bg",
                               (PhononDHO, Elastic, QuasiElastic, ConstantBackground)))
    return hyps


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
        # class = mode 1 (default "phonon" prefix); instance = mode 2 ("phonon2").
        return (PhononDHO, PhononDHO(prefix="phonon2"))

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

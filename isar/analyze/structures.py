"""Physics-gated enumeration of admissible peak-structure hypotheses (§2).

Peak count is never a free knob: ``enumerate_structures`` returns only the structures the
physics permits for this scan's context. Step 1 ships two — ``stokes+bg`` and
``elastic+stokes+bg`` — with the elastic gate encoding the IN8 off-window-elastic
degeneracy fix. The gate vocabulary also names the anti-Stokes and background-kind gates
so the §2 physics lives in one place, even where a gate is trivial at step 1.
"""
from __future__ import annotations

from dataclasses import dataclass

from isar.analyze.components import Component, ConstantBackground, Elastic, PhononDHO


@dataclass(frozen=True)
class Hypothesis:
    """A named structure recipe: the ordered component classes that build its composite."""

    label: str
    components: tuple[type[Component], ...]

    @property
    def k(self) -> int:
        """Number of free parameters (the model's degrees of freedom consumed)."""
        return sum(len(c.params) for c in self.components)

    def build(self):
        """Instantiate the CompositeModel for this hypothesis."""
        from isar.analyze.components import CompositeModel
        return CompositeModel([c() for c in self.components])


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
    elastic line is admissible. Second-phonon / quasi-elastic / sloped-BG are not
    enumerated by default — added only on evidence at later steps.
    """
    hyps = [Hypothesis("stokes+bg", (PhononDHO, ConstantBackground))]
    if elastic_admissible(ctx):
        hyps.append(Hypothesis("elastic+stokes+bg", (PhononDHO, Elastic, ConstantBackground)))
    return hyps

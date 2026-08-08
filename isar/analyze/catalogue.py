"""The phenomenon catalogue (SCENE-1).

A *phenomenon* is what the scene layer reasons about: a named, identity-bearing thing that can be
present in a scan (an acoustic branch, an optic branch, elastic scattering, an unassigned
excursion, or nothing but background). Each entry declares the local models that can represent it,
the gates that decide whether it is admissible in a given scan context, and — declared now,
consumed later — its parameter priors, its transportable parameters, and its constrained variants.

**What the catalogue is not.** It owns no physics. The DHO admissibility gates and the
context-dependent phonon recipe stay in :mod:`isar.analyze.phonon` (module-map ownership rule);
the catalogue holds *references* to them. It is also lattice-blind: the ``elastic`` entry is
generic incoherent/coherent elastic scattering, not a Bragg model — Bragg is scene-level knowledge
(DECISIONS decision 20), and an elastic-only outcome is a scan-local claim, never region exclusion.

**Status in this commit.** Only ``local_models`` and their gates drive anything: they reproduce the
legacy :func:`isar.analyze.phonon.enumerate_phonon_structures` output exactly (pinned by
``tests/analyze/test_catalogue.py``). ``parameter_priors``, ``transport``, ``constrained_variants``
and ``delivery_profile`` are **dormant declarations** — nothing reads them yet; they are consumed
when Laplace evidence lands (SCENE-1 PR-b).

Import direction: :mod:`isar.analyze.components` imports :mod:`isar.analyze.phonon` at module
level, so ``phonon`` never imports this module at module level — the delegation there is a
function-local import.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from isar.analyze import phonon
from isar.analyze.components import ConstantBackground, Elastic, PhononDHO, QuasiElastic
from isar.analyze.structures import Hypothesis

CATALOGUE_SCHEMA = "isar.phenomenon-catalogue/1"
DEFAULT_PROFILE = "isar.catalogue.default/1"


@dataclass(frozen=True)
class LocalModel:
    """One way of representing a phenomenon inside a single scan.

    ``recipe`` is a component class or pre-built instance, exactly as
    :class:`~isar.analyze.structures.Hypothesis` consumes it. ``gate`` is a reference to the
    admissibility predicate that decides whether this model may enter the structure set for a
    context (``None`` ⇒ always admissible). A recipe may still be *specialised* by context — the
    phonon's Bose-tied vs Stokes-only choice is made by :func:`isar.analyze.phonon._phonon_for`,
    which is where that physics lives.
    """

    name: str
    recipe: object
    gate: Callable[[object], bool] | None = None

    def admissible(self, ctx) -> bool:
        return True if self.gate is None else bool(self.gate(ctx))


@dataclass(frozen=True)
class TransportedParameter:
    """A parameter that may be carried between neighbouring q-points as a soft constraint.

    Declared now, consumed at PR-b. ``smoothness`` is the expected variation class
    (``"smooth"`` / ``"slow"`` / ``"rough"``) and ``scale_rlu`` the reduced-lattice-unit distance
    over which that variation is expected to matter. Transport is one-directional and enters local
    evidence through the existing soft-constraint path
    (:class:`isar.schema.steering.ParamConstraints`) — never a parameter-compatibility re-scoring
    loop. A field break (energies hold while intensities swap: an avoided crossing) is a typed
    observation, not something to absorb silently.
    """

    param: str
    smoothness: str
    scale_rlu: float
    note: str = ""


@dataclass(frozen=True)
class ConstrainedVariant:
    """A physically constrained special case of a phenomenon. Declared now, wired at PR-b."""

    name: str
    when: str
    constraints: tuple[str, ...]
    note: str = ""


@dataclass(frozen=True)
class PhenomenonDefinition:
    """One catalogue entry. See the module docstring for which fields are live vs dormant."""

    name: str
    identity: str
    local_models: tuple[LocalModel, ...]
    gates: tuple[Callable[[object], object], ...] = ()
    parameter_priors: tuple[str, ...] = ()          # dormant — consumed at PR-b
    transport: tuple[TransportedParameter, ...] = ()  # dormant — consumed at PR-b
    constrained_variants: tuple[ConstrainedVariant, ...] = ()  # dormant — wired at PR-b
    delivery_profile: str = "triage"

    def model(self, name: str) -> LocalModel:
        for m in self.local_models:
            if m.name == name:
                return m
        raise KeyError(f"{self.name!r} has no local model {name!r}")


# --------------------------------------------------------------------------- the default profile
# The phonon recipe is declared as the bare PhononDHO class; the Bose-tied / Stokes-only choice for
# a given context is made by phonon._phonon_for (anti-Stokes is a functional tie, never a fit
# parameter).
_ACOUSTIC = PhenomenonDefinition(
    name="acoustic",
    identity="acoustic branch — ω → 0 at the zone centre",
    local_models=(LocalModel("dho", PhononDHO),),
    gates=(phonon.antistokes_mode,),
    transport=(
        TransportedParameter("omega0", "smooth", 0.5,
                             "dispersion is smooth on the zone scale away from crossings"),
        TransportedParameter("gamma", "slow", 0.5,
                             "damping varies slowly; a jump is an observation, not noise"),
        TransportedParameter("amplitude", "smooth", 0.25,
                             "smooth but eigenvector-sensitive — structure-factor changes are real"),
    ),
    constrained_variants=(
        ConstrainedVariant(
            "near_degenerate",
            "close to the zone centre, where ω is below or near the resolution width",
            ("fwhm = instrument resolution",
             "amplitude Bose-tied: I ∝ (n+1)/ω ≈ k_BT/ω² in the classical limit"),
            "the exact reduced-q origin stays excluded (DECISIONS O44)",
        ),
    ),
)

_OPTIC = PhenomenonDefinition(
    name="optic",
    identity="optic branch — finite ω at the zone centre",
    local_models=(LocalModel("dho", PhononDHO),),
    gates=(phonon.antistokes_mode,),
    transport=(
        TransportedParameter("omega0", "smooth", 1.0,
                             "flatter than acoustic; transportable over a wider q range"),
        TransportedParameter("gamma", "slow", 1.0),
        TransportedParameter("amplitude", "smooth", 0.25,
                             "eigenvector-sensitive; structure factor may vanish by symmetry"),
    ),
)

_ELASTIC = PhenomenonDefinition(
    name="elastic",
    identity="elastic scattering at the energy zero — generic and lattice-blind",
    local_models=(
        LocalModel("sharp", Elastic, phonon.elastic_admissible),
        LocalModel("broad", QuasiElastic, phonon.quasielastic_admissible),
    ),
    gates=(phonon.elastic_admissible, phonon.quasielastic_admissible),
    transport=(
        TransportedParameter("amplitude", "rough", 0.1,
                             "incoherent + can/sample elastic; strongest near Bragg, not smooth"),
    ),
)

_UNKNOWN = PhenomenonDefinition(
    name="unknown",
    identity="an unassigned resolved excursion — real signal with no branch identity yet",
    local_models=(LocalModel("dho", PhononDHO),),
    gates=(),
    delivery_profile="triage",
)

_BG_ONLY = PhenomenonDefinition(
    name="bg_only",
    identity="no phenomenon — background alone",
    local_models=(LocalModel("flat", ConstantBackground),),
    gates=(),
    delivery_profile="triage",
)

DEFAULT_CATALOGUE: tuple[PhenomenonDefinition, ...] = (
    _ACOUSTIC, _OPTIC, _ELASTIC, _UNKNOWN, _BG_ONLY,
)

PROFILES: dict[str, tuple[PhenomenonDefinition, ...]] = {DEFAULT_PROFILE: DEFAULT_CATALOGUE}


def entry(name: str, profile: str = DEFAULT_PROFILE) -> PhenomenonDefinition:
    """The named catalogue entry from ``profile``."""
    for definition in PROFILES[profile]:
        if definition.name == name:
            return definition
    raise KeyError(f"no phenomenon {name!r} in catalogue profile {profile!r}")


def enumerate_structures(ctx, profile: str = DEFAULT_PROFILE) -> list[Hypothesis]:
    """The admissible structure hypotheses for a scan context, driven by the catalogue.

    Always the single-phonon ``stokes+bg``; plus the sharp elastic when its gate fires, plus the
    broad quasi-elastic when the card declares its width, plus both when both fire. Whether an
    enumerated component earns its parameter is model selection's decision (§4), not the
    catalogue's.
    """
    elastic_entry = entry("elastic", profile)
    sharp = elastic_entry.model("sharp")
    broad = elastic_entry.model("broad")
    bg = entry("bg_only", profile).model("flat").recipe

    elastic = sharp.admissible(ctx)
    qe = broad.admissible(ctx)
    pho = phonon._phonon_for(ctx)   # Bose-tied class or Stokes-only instance (DHO physics)

    hyps = [Hypothesis("stokes+bg", (pho, bg))]
    if elastic:
        hyps.append(Hypothesis("elastic+stokes+bg", (pho, sharp.recipe, bg)))
    if qe:
        hyps.append(Hypothesis("stokes+qe+bg", (pho, broad.recipe, bg)))
    if elastic and qe:
        hyps.append(Hypothesis("elastic+stokes+qe+bg",
                               (pho, sharp.recipe, broad.recipe, bg)))
    return hyps

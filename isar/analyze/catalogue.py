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

**Status.** Only ``local_models`` and their gates drive anything: the enumeration is the legacy
:func:`isar.analyze.phonon.enumerate_phonon_structures` list (pinned by
``tests/analyze/test_catalogue.py``) extended by the phonon-less nulls ``elastic+bg`` and ``bg``
(SCENE-1 PR-a — what the ``elastic_only`` / ``no_signal`` verdicts select on). ``parameter_priors``,
``transport``, ``constrained_variants`` and ``delivery_profile`` are **dormant declarations** —
nothing reads them yet; they are consumed when Laplace evidence lands (SCENE-1 PR-b).

Import direction: :mod:`isar.analyze.components` imports :mod:`isar.analyze.phonon` at module
level, so ``phonon`` never imports this module at module level — the delegation there is a
function-local import.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

import numpy as np

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
class ParamPrior:
    """A proper per-parameter prior an entry declares for its local models (SCENE-1 PR-b).

    ``param`` is the *suffix* name (``"Omega_d"``, ``"A"``, …), applied per component instance
    through its prefix. ``kind`` ∈ ``"gaussian"`` / ``"lognormal"`` / ``"flat"``. ``mu`` and
    ``sigma`` are floats **or ctx-attribute names** (e.g. ``"acoustic_omega"``,
    ``"gauss_fwhm"``) resolved at evaluation time — context-aware priors without a callable zoo.
    For ``lognormal``, ``mu`` is the linear-space median and ``sigma`` the log-space width.
    ``flat`` needs neither. All densities are truncated/normalized on the optimizer bounds, so
    every prior is proper and lnZ carries an honest Occam factor.
    """

    param: str
    kind: str
    mu: float | str | None = None
    sigma: float | str | None = None
    note: str = ""


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
    parameter_priors: tuple[ParamPrior, ...] = ()   # consumed by the evidence layer (PR-b);
    #                                                 entries declare none until the identity
    #                                                 framing lands (owner deferral 2026-08-08)
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

# Three look-alikes sit near E=0 and are routinely conflated; they are distinct phenomena:
#   elastic       — a resolution-limited *static* line at E=0 (incoherent scattering plus
#                   can/sample background). No intrinsic width: its width is the resolution.
#                   Bragg is not this entry — Bragg identity is scene-level (DECISIONS decision 20).
#   quasielastic  — *relaxational/diffusive dynamics*: broad in ENERGY, centred at E=0. A
#                   measurement target in its own right (linewidth ⇒ relaxation rate), not a
#                   fallback shape for a poorly-fit elastic line.
#   diffuse       — broad in Q, elastic in E (disorder/short-range correlations). A distinct
#                   phenomenon, handled scene-level today; not yet a catalogue entry.
_ELASTIC = PhenomenonDefinition(
    name="elastic",
    identity="static elastic scattering at the energy zero — resolution-limited, lattice-blind",
    local_models=(
        LocalModel("sharp", Elastic, phonon.elastic_admissible),
    ),
    gates=(phonon.elastic_admissible,),
    transport=(
        TransportedParameter("amplitude", "rough", 0.1,
                             "incoherent + can/sample elastic; strongest near Bragg, not smooth"),
    ),
)

_QUASIELASTIC = PhenomenonDefinition(
    name="quasielastic",
    identity="quasi-elastic scattering — relaxational dynamics, broad in energy about E=0",
    local_models=(
        LocalModel("broad", QuasiElastic, phonon.quasielastic_admissible),
    ),
    gates=(phonon.quasielastic_admissible,),
    transport=(
        # No width transport: the QE width is card-declared (``quasielastic_fwhm``), not carried
        # from a neighbouring q-point.
        TransportedParameter("amplitude", "rough", 0.1,
                             "shares the elastic channel's roughness — incoherent and sample-shape "
                             "sensitive, not smooth in q"),
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
    _ACOUSTIC, _OPTIC, _ELASTIC, _QUASIELASTIC, _UNKNOWN, _BG_ONLY,
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
    broad quasi-elastic when the card declares its width, plus both when both fire. The
    phonon-less nulls close the list: ``elastic+bg`` (elastic gate) and ``bg`` (always) are
    first-class candidates, so selection can honestly prefer "nothing inelastic here" — the
    winner the ``elastic_only`` / ``no_signal`` verdicts read (SCENE-1 PR-a). Whether an
    enumerated component earns its parameter is model selection's decision (§4), not the
    catalogue's.
    """
    sharp = entry("elastic", profile).model("sharp")
    broad = entry("quasielastic", profile).model("broad")
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
    if elastic:
        hyps.append(Hypothesis("elastic+bg", (sharp.recipe, bg)))
    hyps.append(Hypothesis("bg", (bg,)))
    return hyps


def enumerate_multimode_structures(ctx, n_max: int = 2,
                                   profile: str = DEFAULT_PROFILE) -> list[Hypothesis]:
    """The two-mode (2-phonon) escalation set, catalogue-driven (SCENE-1 PR-b).

    Multiplicity as the catalogue owns it: a second instance of the same phenomenon occupies the
    ``phonon2`` prefix namespace, and mode 1 keeps the default ``phonon`` prefix so each 1-mode
    hypothesis stays *strictly nested* in its 2-mode counterpart (the §4 LRT/AIC comparison is
    then exact). Both modes honour the Bose-dead Stokes-only choice
    (:func:`isar.analyze.phonon._phonon_for` — DHO physics stays in phonon.py). The phonon-less
    nulls do NOT appear here: they live in the 1-mode set and a "two nulls" structure is not a
    thing. Capped at N=2 (``n_max`` accepted for the later N=3 extension; >2 treated as 2).
    Labels, order and k are pinned bit-identical to the legacy hand enumeration by
    ``tests/analyze/test_catalogue.py``.
    """
    if n_max < 2:
        return []
    sharp = entry("elastic", profile).model("sharp")
    broad = entry("quasielastic", profile).model("broad")
    bg = entry("bg_only", profile).model("flat").recipe

    elastic = sharp.admissible(ctx)
    qe = broad.admissible(ctx)

    def twophonon():
        return (phonon._phonon_for(ctx), phonon._phonon_for(ctx, prefix="phonon2"))

    hyps = [Hypothesis("stokes2+bg", (*twophonon(), bg))]
    if elastic:
        hyps.append(Hypothesis("elastic+stokes2+bg", (*twophonon(), sharp.recipe, bg)))
    if qe:
        hyps.append(Hypothesis("stokes2+qe+bg", (*twophonon(), broad.recipe, bg)))
    if elastic and qe:
        hyps.append(Hypothesis("elastic+stokes2+qe+bg",
                               (*twophonon(), sharp.recipe, broad.recipe, bg)))
    return hyps


# --------------------------------------------------------------------------- prior resolution
@dataclass(frozen=True)
class ResolvedPrior:
    """One parameter's resolved, proper prior: global name, distribution, bounds, provenance.

    ``origin`` ∈ ``"transport"`` (a soft constraint from ``ctx.constraints`` — it IS the prior
    for that parameter, the applied-exactly-once rule), ``"identity"`` (declared by a catalogue
    entry), or ``"default_flat"`` (normalized flat over the optimizer bounds).
    ``origin_detail`` carries the constraint-origin string when transport supplied it (what the
    STAT eligibility rules read)."""

    name: str
    kind: str                       # "gaussian" | "lognormal" | "flat"
    mu: float | None
    sigma: float | None
    lo: float
    hi: float
    origin: str
    origin_detail: str | None = None

    def _dist(self):
        from scipy import stats

        if self.kind == "gaussian":
            return stats.norm(loc=self.mu, scale=self.sigma)
        if self.kind == "lognormal":
            return stats.lognorm(s=self.sigma, scale=self.mu)
        raise ValueError(f"no distribution for kind {self.kind!r}")

    def _trunc_mass(self, dist) -> float:
        return float(dist.cdf(self.hi) - dist.cdf(self.lo))

    def logpdf(self, value: float) -> float:
        v = float(value)
        if not (self.lo <= v <= self.hi):
            return float("-inf")
        if self.kind == "flat":
            span = self.hi - self.lo
            return -math.log(span) if span > 0 else 0.0
        dist = self._dist()
        z = self._trunc_mass(dist)
        if not (z > 0):
            return float("-inf")            # prior mass entirely outside the bounds
        return float(dist.logpdf(v)) - math.log(z)

    def ppf(self, u: float) -> float:
        """Inverse CDF of the truncated prior on [lo, hi] — the dynesty prior transform."""
        u = min(max(float(u), 0.0), 1.0)
        if self.kind == "flat":
            return self.lo + u * (self.hi - self.lo)
        dist = self._dist()
        c_lo = float(dist.cdf(self.lo))
        z = self._trunc_mass(dist)
        if not (z > 0):
            return self.lo
        return float(np.clip(dist.ppf(c_lo + u * z), self.lo, self.hi))

    def spec(self) -> dict:
        """Fingerprintable declaration of this prior (no evaluation state)."""
        return {"name": self.name, "kind": self.kind, "mu": self.mu, "sigma": self.sigma,
                "lo": self.lo, "hi": self.hi, "origin": self.origin,
                "origin_detail": self.origin_detail}


@dataclass(frozen=True)
class PriorBundle:
    """The resolved priors for one candidate fit, in ``composite.param_names()`` order
    (excluded — hard-fixed — parameters absent: they contribute no prior factor, their k is
    already reduced)."""

    params: tuple[ResolvedPrior, ...]

    def logpdf(self, named_values: dict) -> float:
        return float(sum(p.logpdf(named_values[p.name]) for p in self.params))

    def by_name(self) -> dict:
        return {p.name: p for p in self.params}

    def spec(self) -> list[dict]:
        return [p.spec() for p in self.params]

    @property
    def nontrivial(self) -> tuple[ResolvedPrior, ...]:
        """The informative (non-flat) priors — what the curvature check inspects."""
        return tuple(p for p in self.params if p.kind != "flat")


def _resolve_value(value, ctx, *, what: str) -> float:
    """A prior hyper-parameter: a float, or the name of a finite ctx attribute (context-aware
    priors, e.g. ``mu="acoustic_omega"``). A named attribute that is missing or non-finite is a
    declaration error — fail loudly, never silently flatten."""
    if isinstance(value, str):
        got = getattr(ctx, value, None)
        if got is None or not np.isfinite(float(got)):
            raise ValueError(f"prior {what} names ctx attribute {value!r}, which is missing "
                             f"or non-finite on this context")
        return float(got)
    return float(value)


def _validated(name: str, kind: str, mu: float, sigma: float, lo: float, hi: float,
               origin: str, origin_detail=None) -> ResolvedPrior:
    """Construct a non-flat :class:`ResolvedPrior`, refusing invalid hyper-parameters at
    resolution time — a broken prior must never fail later inside logpdf/ppf/dynesty."""
    if not (np.isfinite(mu) and np.isfinite(sigma)):
        raise ValueError(f"prior for {name} ({origin}) has non-finite mu/sigma "
                         f"({mu!r}, {sigma!r})")
    if sigma <= 0:
        raise ValueError(f"prior for {name} ({origin}) requires sigma > 0, got {sigma!r}")
    if kind == "lognormal" and mu <= 0:
        raise ValueError(f"lognormal prior for {name} ({origin}) requires mu > 0 "
                         f"(the linear-space median), got {mu!r}")
    if not (lo < hi):
        raise ValueError(f"prior for {name} ({origin}) has degenerate bounds ({lo!r}, {hi!r})")
    return ResolvedPrior(name, kind, mu, sigma, lo, hi, origin, origin_detail)


def resolve_priors(composite, ctx, bounds, *, entry=None, exclude=()) -> PriorBundle:
    """Resolve every free parameter's proper prior for one candidate (SCENE-1 PR-b).

    Precedence per parameter — **exactly one prior factor each** (the H3/H4 no-double-counting
    rule): a transported soft constraint from ``ctx.constraints`` IS the prior (truncated
    Gaussian on the optimizer bounds); else an ``entry``-declared :class:`ParamPrior` matched by
    suffix; else normalized flat over the bounds. ``exclude`` names the hard-fixed parameters
    (no prior factor; k already reduced). ``bounds`` is the per-parameter (lo, hi) list in
    ``composite.param_names()`` order, exactly as :func:`isar.analyze.loop.build_problem` built
    them.
    """
    constraints = getattr(ctx, "constraints", None)
    soft = dict(getattr(constraints, "soft", None) or {})
    origins = dict(getattr(constraints, "origins", None) or {})
    declared = {p.param: p for p in (entry.parameter_priors if entry is not None else ())}
    excluded = set(exclude)

    resolved = []
    matched_suffixes = set()
    for name, (lo, hi) in zip(composite.param_names(), bounds):
        if name in excluded:
            continue
        lo, hi = float(lo), float(hi)
        if not (np.isfinite(lo) and np.isfinite(hi)):
            # A normalized flat prior over an infinite interval does not exist; letting it
            # through silently zeroes the candidate's evidence (logpdf −inf) and a strictly
            # better-fitting model loses with no flag. Fail at construction, loudly.
            raise ValueError(f"free parameter {name!r} has non-finite optimizer bounds "
                             f"({lo!r}, {hi!r}); evidence needs a proper prior — bound the "
                             f"parameter in loop._bound or declare a ParamPrior for it")
        suffix = name.split(".", 1)[-1]
        if name in soft:
            mu, sigma = float(soft[name][0]), float(soft[name][1])
            resolved.append(_validated(name, "gaussian", mu, sigma, lo, hi,
                                       "transport", origins.get(name)))
        elif suffix in declared:
            matched_suffixes.add(suffix)
            d = declared[suffix]
            if d.kind == "flat":
                resolved.append(ResolvedPrior(name, "flat", None, None, lo, hi, "identity"))
            else:
                mu = _resolve_value(d.mu, ctx, what=f"{name} mu")
                sigma = _resolve_value(d.sigma, ctx, what=f"{name} sigma")
                resolved.append(_validated(name, d.kind, mu, sigma, lo, hi, "identity"))
        else:
            resolved.append(ResolvedPrior(name, "flat", None, None, lo, hi, "default_flat"))
    # A declared suffix no free parameter carries is a stale/misspelled declaration — silent
    # non-application would quietly change every evidence it was meant to shape.
    unmatched = set(declared) - matched_suffixes - {
        n.split(".", 1)[-1] for n in composite.param_names() if n in soft or n in excluded}
    if unmatched:
        raise ValueError(f"catalogue entry {getattr(entry, 'name', '?')!r} declares priors for "
                         f"parameter suffix(es) {sorted(unmatched)} that match no free "
                         f"parameter of this composite")
    return PriorBundle(tuple(resolved))

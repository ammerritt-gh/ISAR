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
(scene-program decision 20), and an elastic-only outcome is a scan-local claim, never region exclusion.

**Status.** ``local_models`` and their gates drive the per-count enumeration
(:func:`enumerate_count_structures`; one and two modes pinned bit-identical to the legacy hand
lists by ``tests/analyze/test_catalogue.py``), with the phonon-less nulls ``elastic+bg`` and ``bg``
(SCENE-1 PR-a — what the ``elastic_only`` / ``no_signal`` verdicts select on).
``parameter_priors`` is live: :func:`resolve_priors` turns declarations into the
:class:`PriorBundle` the evidence layer counts exactly once (SCENE-1 PR-b), and since PR-c
commit E a declaration also *splits* the enumeration: entries competing for one local model slot
become one identity-bearing candidate each (:func:`_identity_candidates`). No entry in the
production catalogue declares a prior yet, so that enumeration is still bit-identical to the
identity-agnostic list.
``constrained_variants`` is live (SCENE-1 PR-c): :func:`isar.analyze.loop._offer_variants` fires
each declared ``when`` predicate against the converged one-mode fits and offers the pinned twin as
an ADDITIONAL candidate. ``transport`` and ``delivery_profile`` remain **dormant declarations** —
nothing reads them yet.

Import direction: :mod:`isar.analyze.components` imports :mod:`isar.analyze.phonon` at module
level, so ``phonon`` never imports this module at module level — the delegation there is a
function-local import.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from functools import cached_property
from typing import Callable

import numpy as np

from isar.analyze.prior import PriorSettings

from isar.analyze import phonon
from isar.analyze.components import ConstantBackground, Elastic, PhononDHO, QuasiElastic
from isar.analyze.structures import Hypothesis
from isar.schema import placement as _placement
from isar.schema.placement import (  # noqa: F401 — re-exported for existing callers/tests
    FALLBACK_RULE_KEY, PLACEMENT_RULES, PLACEMENT_RULES_SCHEMA, PlacementRule, ResolvedPlacement,
    _DEFAULT_PLACEMENT_RULES)

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

    ``param`` is the *suffix* name (``"omega0"``, ``"h"``, …), applied per component instance
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
    """A physically constrained special case of a phenomenon, offered as its OWN candidate.

    Live since SCENE-1 PR-c (H1: the prose ``when``/``constraints`` strings were replaced
    outright, no compatibility form). ``when(ctx, omega0)`` decides, from a converged
    unconstrained fit's ω̂ and the scan context, whether the special case is worth pricing here;
    ``pins(ctx, mode_prefix)`` returns ``(hard, origins)`` dicts keyed by *global* parameter name
    for that mode's prefix, exactly as :class:`isar.schema.steering.ParamConstraints` carries them.
    The offering seam (:func:`isar.analyze.loop._offer_variants`) merges those pins into a derived
    context and fits a twin candidate labelled ``<base><label_suffix>``; the variant never replaces
    the unconstrained candidate, and evidence decides between them (2a ruling 2).
    """

    name: str
    when: Callable[[object, float], bool]
    pins: Callable[[object, str], tuple[dict, dict]]
    label_suffix: str
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
    transport: tuple[TransportedParameter, ...] = ()  # dormant — nothing reads it yet
    constrained_variants: tuple[ConstrainedVariant, ...] = ()  # live: offered as additional
    #                                                            candidates (SCENE-1 PR-c)
    delivery_profile: str = "triage"

    def model(self, name: str) -> LocalModel:
        for m in self.local_models:
            if m.name == name:
                return m
        raise KeyError(f"{self.name!r} has no local model {name!r}")


# --------------------------------------------------------------------------- variant physics
def _near_degenerate(ctx, omega0) -> bool:
    """A mode whose fitted ω₀ sits at or below the resolution width: the peak and the elastic
    line are not separated by the instrument, so "this is a resolution-limited excitation" is a
    real rival to "this is a broad low-energy mode"."""
    return float(omega0) <= float(ctx.gauss_fwhm)


def _near_degenerate_pins(ctx, mode_prefix: str) -> tuple[dict, dict]:
    """Pin the DHO's INTRINSIC width to the floor, so the observed FWHM is the instrument
    resolution alone (the DHO is convolved with the resolution downstream).

    Not ``Γ = ctx.gauss_fwhm``: that would add a Lorentzian of resolution width *on top of* the
    resolution kernel and model a peak twice as broad as the one the variant claims.
    """
    return ({f"{mode_prefix}.Gamma": phonon.GAMMA_FLOOR},
            {f"{mode_prefix}.Gamma": "catalogue_variant"})


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
            when=_near_degenerate,
            pins=_near_degenerate_pins,
            label_suffix="_neardeg",
            note="close to the zone centre, where ω sits below the resolution width; the exact "
                 "reduced-q origin stays excluded (ruling O44). The amplitude tie "
                 "I ∝ (n+1)/ω is NOT applied here — it needs cross-component parameter ties "
                 "(deferred, C2).",
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
#                   Bragg is not this entry — Bragg identity is scene-level (scene-program decision 20).
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


def identity_entry(identity: str | None, profile: str = DEFAULT_PROFILE):
    """The catalogue entry a candidate's ``identity`` names, or ``None`` when it is
    identity-agnostic. An identity naming no entry is a wiring error and raises (``entry``)."""
    return None if identity is None else entry(identity, profile)


def _identity_candidates(recipe, profile: str) -> tuple[PhenomenonDefinition, ...]:
    """The catalogue entries competing for one local-model slot, or ``()`` for no identity split.

    A split is only earned when the rivals are *distinguishable*: more than one entry declares
    ``recipe`` as a local model AND at least one of them declares ``parameter_priors``. Splitting
    without that gives twins with identical likelihoods and identical priors — identical evidence,
    whose only effect is to divide the uniform structure prior between copies of one model. The
    production catalogue declares no priors, so this returns ``()`` and enumeration stays
    bit-identical to the identity-agnostic list (pinned by ``tests/analyze/test_catalogue.py``).
    """
    sharing = tuple(d for d in PROFILES[profile]
                    if any(m.recipe is recipe for m in d.local_models))
    return sharing if len(sharing) > 1 and any(d.parameter_priors for d in sharing) else ()


def _phonon_bearing_adder(hyps: list, profile: str):
    """An ``add(label, components)`` that appends one hypothesis per competing phonon identity —
    or exactly one identity-agnostic hypothesis when the catalogue declares no split.

    N-mode note: the identity applies to the CANDIDATE, so a two-mode candidate prices both of its
    modes under one entry's priors. Per-mode identity assignment (an acoustic ⊗ optic pair) needs
    the cross-mode structure SCENE-3 owns and is deliberately not invented here.
    """
    ids = _identity_candidates(PhononDHO, profile)

    def add(label: str, components: tuple) -> None:
        if not ids:
            hyps.append(Hypothesis(label, components))
            return
        hyps.extend(Hypothesis(f"{label}@{d.name}", components, identity=d.name) for d in ids)

    return add


def enumerate_structures(ctx, profile: str = DEFAULT_PROFILE) -> list[Hypothesis]:
    """The admissible structure hypotheses for a scan context, driven by the catalogue.

    Always the single-phonon ``stokes+bg``; plus the sharp elastic when its gate fires, plus the
    broad quasi-elastic when the card declares its width, plus both when both fire. The
    phonon-less nulls close the list: ``elastic+bg`` (elastic gate) and ``bg`` (always) are
    first-class candidates, so selection can honestly prefer "nothing inelastic here" — the
    winner the ``elastic_only`` / ``no_signal`` verdicts read (SCENE-1 PR-a). Whether an
    enumerated component earns its parameter is model selection's decision (§4), not the
    catalogue's.

    Each phonon-bearing structure becomes one candidate per competing *identity*
    (``<label>@<entry>``, e.g. ``stokes+bg@acoustic``) when — and only when — the catalogue
    distinguishes those identities by a declared prior (:func:`_identity_candidates`). The
    phonon-less nulls carry no identity: there is no phenomenon in them to identify.
    """
    return enumerate_count_structures(ctx, 1, profile) + enumerate_count_structures(ctx, 0, profile)


def enumerate_count_structures(ctx, n: int, profile: str = DEFAULT_PROFILE) -> list[Hypothesis]:
    """The admissible structures holding exactly ``n`` modes: one rung of the count ladder
    (:func:`isar.analyze.loop.fit_structures_two_stage`).

    ``n = 0`` is the phonon-less nulls (``elastic+bg`` under the elastic gate, then ``bg``). For
    ``n >= 1`` each structure is the ``n`` modes plus the sharp elastic and/or the broad
    quasi-elastic as their gates allow, in the order plain, elastic, quasi-elastic, both. Mode ``i``
    takes the prefix ``phonon`` (``i = 1``) or ``phonon<i>``, so each ``n``-mode structure is
    *strictly nested* in its ``n + 1``-mode counterpart; the label counts the modes (``stokes+bg``,
    ``stokes2+bg``, ``stokes3+bg``, ...). Every mode honours the Bose-dead Stokes-only choice
    (:func:`isar.analyze.phonon._phonon_for`; DHO physics stays in phonon.py). Labels, order and k
    for ``n <= 2`` are pinned to the legacy hand enumeration by ``tests/analyze/test_catalogue.py``.
    """
    sharp = entry("elastic", profile).model("sharp")
    broad = entry("quasielastic", profile).model("broad")
    bg = entry("bg_only", profile).model("flat").recipe

    elastic = sharp.admissible(ctx)
    qe = broad.admissible(ctx)

    hyps: list[Hypothesis] = []
    if n == 0:
        if elastic:
            hyps.append(Hypothesis("elastic+bg", (sharp.recipe, bg)))
        hyps.append(Hypothesis("bg", (bg,)))
        return hyps

    def modes():
        # a Bose-tied class or Stokes-only instance per mode (DHO physics: phonon._phonon_for)
        return tuple(phonon._phonon_for(ctx) if i == 1 else phonon._phonon_for(ctx, f"phonon{i}")
                     for i in range(1, n + 1))

    stokes = "stokes" if n == 1 else f"stokes{n}"
    add = _phonon_bearing_adder(hyps, profile)
    add(f"{stokes}+bg", (*modes(), bg))
    if elastic:
        add(f"elastic+{stokes}+bg", (*modes(), sharp.recipe, bg))
    if qe:
        add(f"{stokes}+qe+bg", (*modes(), broad.recipe, bg))
    if elastic and qe:
        add(f"elastic+{stokes}+qe+bg", (*modes(), sharp.recipe, broad.recipe, bg))
    return hyps


def enumerate_multimode_structures(ctx, n_max: int,
                                   profile: str = DEFAULT_PROFILE) -> list[Hypothesis]:
    """Every structure holding 2..``n_max`` modes, count by count
    (:func:`enumerate_count_structures`). The one-mode set and the phonon-less nulls are
    :func:`enumerate_structures`; together the two lists are the whole count ladder."""
    return [h for n in range(2, n_max + 1) for h in enumerate_count_structures(ctx, n, profile)]


# --------------------------------------------------------------------------- prior resolution
@dataclass(frozen=True)
class ResolvedPrior:
    """One parameter's resolved, proper prior: global name, distribution, bounds, provenance.

    ``origin`` ∈ ``"transport"`` (a soft constraint from ``ctx.constraints`` — it IS the prior
    for that parameter, the applied-exactly-once rule), ``"identity"`` (declared by a catalogue
    entry), ``"default_flat"`` (normalized flat over the optimizer bounds) or ``"default_softlog"``
    (the recipe's width prior, below).

    ``kind == "softlog"`` is the recipe's softened log-uniform width density
    ``1/(x + mu) / ln((hi + mu)/mu)`` with ``mu`` the knee (meV) and ``sigma`` ``None``. It is
    normalised on ``[0, hi]`` like :meth:`isar.analyze.prior.SpectrumPrior.ln_width`, so the
    campaign path and the fast pass share one width density (its mass on ``[lo, hi]`` is 1 less the
    ~0.002-nat mass below the Γ floor, which is not renormalised).
    ``origin_detail`` carries the constraint-origin string when transport supplied it (what the
    STAT eligibility rules read)."""

    name: str
    kind: str                       # "gaussian" | "lognormal" | "flat" | "softlog"
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
        if self.kind == "softlog":
            return -math.log(v + self.mu) - math.log(math.log((self.hi + self.mu) / self.mu))
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
        if self.kind == "softlog":
            # shortcut: this inverts the CDF truncated to [lo, hi], so the sampled density exceeds
            # logpdf by -ln(1 - mass below lo), ~0.0016 nats per free width; upgrade if that
            # offset ever matters between the Laplace and nested lanes.
            return (self.lo + self.mu) * ((self.hi + self.mu) / (self.lo + self.mu)) ** u - self.mu
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

    def _energy_groups(self) -> list[list[int]]:
        """Positions of the mode energies (``*.omega0``) the ordered-energy prior can order, one
        list per group of exchangeable modes: each carries one plain flat energy prior, and every
        one of its other parameters carries the same prior as the others of its group (a prior on
        one mode's h or Gamma alone singles it out). The recipe's default width prior is not part
        of that comparison: a delta mode (Γ hard-fixed, no width prior) and a finite-width mode
        are one ordered group, as :meth:`isar.analyze.prior.SpectrumPrior.ln_energies` has it (one
        k!, one ordered transform over every mode's ω₀). Groups of one are not ordered."""
        return [g.tolist() for g in self._energy_group_indices]

    @cached_property
    def _energy_group_indices(self):
        # The bundle is frozen; grouping depends on its priors, never on sampled values.
        groups = {}
        for i, p in enumerate(self.params):
            if p.name.endswith(".omega0") and p.kind == "flat":
                groups.setdefault(self._mode_priors(i), []).append(i)
        indices = tuple(np.asarray(g, dtype=int) for g in groups.values() if len(g) >= 2)
        for group in indices:
            group.flags.writeable = False
        return indices

    @cached_property
    def _ungrouped_priors(self):
        grouped = {i for group in self._energy_group_indices for i in group}
        return tuple(p for i, p in enumerate(self.params) if i not in grouped)

    def _mode_priors(self, energy_index: int) -> tuple:
        """The density of every parameter prior of the mode whose energy is at ``energy_index``
        except its default width prior, with the mode prefix stripped, so two modes compare equal
        when their priors are the same."""
        prefix = self.params[energy_index].name.split(".", 1)[0]
        return tuple((p.name.split(".", 1)[1], p.kind, p.mu, p.sigma, p.lo, p.hi)
                     for p in self.params if p.name.split(".", 1)[0] == prefix
                     and p.origin not in ("default_softlog", "default_flat_width"))

    def logpdf_ordered(self, named_values: dict) -> float:
        """:meth:`logpdf` with each group of exchangeable mode energies under the normalised
        ordered-energy prior (``N!/W^N``, :func:`isar.analyze.prior.ordered_energy_log_prior`)
        instead of one flat factor per label. The Laplace lane of the ordered prior;
        :meth:`transform_ordered` is its sampler."""
        from isar.analyze.prior import ordered_energy_log_prior

        groups = self._energy_group_indices
        total = float(sum(p.logpdf(named_values[p.name]) for p in self._ungrouped_priors))
        for g in groups:
            first = self.params[g[0]]
            total += ordered_energy_log_prior(
                [named_values[self.params[i].name] for i in g], first.lo, first.hi)
        return total

    def transform_ordered(self, u) -> np.ndarray:
        """Nested-sampling prior transform, ``u`` in ``params`` order: each parameter's own ``ppf``,
        except each group of exchangeable mode energies, which are the ascending order statistics of
        uniforms (:func:`isar.analyze.prior.ordered_energy_transform`), assigned to the group's
        modes in component order. Samples :meth:`logpdf_ordered`."""
        from isar.analyze.prior import ordered_energy_transform

        u = np.asarray(u, dtype=float)
        out = np.array([p.ppf(v) for p, v in zip(self.params, u)], dtype=float)
        for g in self._energy_group_indices:
            first = self.params[g[0]]
            out[g] = ordered_energy_transform(u[g], first.lo, first.hi)
        return out

    @property
    def nontrivial(self) -> tuple[ResolvedPrior, ...]:
        """The informative (non-flat) priors — what the curvature check inspects."""
        # shortcut: the softlog knee's curvature 1/(Γ+0.12)² is ignored by the unseen-informative
        # test; it can rival the likelihood's only for a width below about 0.1 meV, where the
        # campaign path already takes the Γ-floor rung; upgrade if the campaign path prices such
        # widths.
        return tuple(p for p in self.params if p.kind not in ("flat", "softlog"))


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


def resolve_priors(composite, ctx, bounds, *, entry=None, exclude=(),
                   prior_settings=None) -> PriorBundle:
    """Resolve every free parameter's proper prior for one candidate (SCENE-1 PR-b).

    Precedence per parameter — **exactly one prior factor each** (the H3/H4 no-double-counting
    rule): a transported soft constraint from ``ctx.constraints`` IS the prior (truncated
    Gaussian on the optimizer bounds); else an ``entry``-declared :class:`ParamPrior` matched by
    suffix; else the recipe default: the softened log-uniform density for a ``.Gamma``
    (``default_softlog``), normalized flat over the bounds for anything else. ``exclude`` names the hard-fixed parameters
    (no prior factor; k already reduced). ``bounds`` is the per-parameter (lo, hi) list in
    ``composite.param_names()`` order, exactly as :func:`isar.analyze.loop.build_problem` built
    them. ``prior_settings`` (a :class:`~isar.analyze.prior.PriorSettings`, ``None``: recipe v5)
    sets the default width density as :meth:`isar.analyze.prior.SpectrumPrior.ln_width` has it: the
    softened log-uniform with the settings' knee, or flat when ``width_kind`` is ``"flat"`` or under
    ``legacy_v4``. The bounds are the caller's (``prior.mode_bounds(names, ctx, prior_settings)``).
    """
    settings = prior_settings or PriorSettings()
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
                             f"parameter in prior.mode_bounds or declare a ParamPrior for it")
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
        elif suffix == "Gamma":
            if settings.width_kind == "flat" or settings.legacy_v4:
                # shortcut: normalised over [lo, hi], SpectrumPrior over [0, cap]; the unrenormalised
                # mass below the floor is lo/cap, ~5e-5 nats, like the softlog width's.
                resolved.append(ResolvedPrior(name, "flat", None, None, lo, hi, "default_flat_width"))
            else:
                resolved.append(ResolvedPrior(name, "softlog", settings.width_knee, None,
                                              lo, hi, "default_softlog"))
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


# --------------------------------------------------------------------------- placement rules (SCENE-4 PR 4.2a, commit A4)
# Findability knowledge re-enters as DECLARED DATA: where a catalogue class is worth looking for
# it, for the opening book to order its survey (A5). These are ORDERING declarations only — they
# never label a measured feature, gate coverage, or touch a posterior. SCENE-5 replaces the encoded
# conclusions with the real EIG computation; until then a rule is a named provisional v1 guess,
# SCENE-7-calibrated.
#
# The registry itself (``PlacementRule``, ``PLACEMENT_RULES``, the manifest/fingerprint
# functions) moved to :mod:`isar.schema.placement` in the A5 boundary repair, so ``isar.drive``
# can read placement data without crossing the Stage II<->III wall
# (``tests/drive/test_boundary.py``). What stays here is the class-list-aware wrapper: this
# module is the one that knows which classes a profile actually declares (``PROFILES``), so the
# completeness/orphan validation belongs here, not in the class-list-blind schema module.
def _placement_class_names(profile: str) -> list[str]:
    if profile not in PROFILES:
        raise KeyError(f"no catalogue profile {profile!r}")
    return [d.name for d in PROFILES[profile]]


def placement_rules(profile: str = DEFAULT_PROFILE) -> dict[str, ResolvedPlacement]:
    """The resolved placement rule for every catalogue class in ``profile``.

    A class with an explicit row uses it; a class with none falls through to the profile's
    ``FALLBACK_RULE_KEY`` row (``used_fallback=True`` on the result) when the profile declares
    one. An unknown catalogue profile, an unregistered placement-rule profile, or a class with
    neither an explicit row nor a declared fallback all raise loudly — never silent synthesis.
    """
    return _placement.resolve_placement_rules(profile, _placement_class_names(profile))


def placement_rules_manifest(profile: str = DEFAULT_PROFILE) -> dict:
    """Canonical, JSON-able, deterministic manifest of every class's resolved placement rule —
    the replayable artifact (:func:`placement_rules_fingerprint` is change-detection over it)."""
    return _placement.placement_rules_manifest(profile, _placement_class_names(profile))


def placement_rules_fingerprint(profile: str = DEFAULT_PROFILE) -> str:
    """Content address over :func:`placement_rules_manifest` — changes iff a rule field changes."""
    return _placement.placement_rules_fingerprint(profile, _placement_class_names(profile))

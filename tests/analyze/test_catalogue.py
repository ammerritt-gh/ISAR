"""The phenomenon catalogue (SCENE-1) and its enumeration pin.

:func:`isar.analyze.phonon.enumerate_phonon_structures` is catalogue-backed. Its output is pinned
here — labels, order, and per-component identity — for every reachable gate combination
(elastic × quasi-elastic × anti-Stokes mode). The phonon-bearing prefix was snapshotted from the
legacy implementation before it was deleted and must never drift; the phonon-less nulls
(``elastic+bg``, ``bg``) were appended deliberately at SCENE-1 PR-a (the ``elastic_only`` /
``no_signal`` selection candidates) and are pinned from that point on. A drift here is a real
regression, not a test to update.
"""
from dataclasses import replace

import numpy as np
import pytest

from isar.analyze import catalogue, evidence, loop, phonon
from isar.analyze.components import ConstantBackground, Elastic, PhononDHO, QuasiElastic
from isar.analyze.context import ScanContext
from isar.analyze.lineshapes import convolute_voigt_dho


def _ctx(*, elastic: bool, qe: bool, stokes_only: bool) -> ScanContext:
    """A ScanContext that drives the three gates independently.

    ``elastic`` — ``needs_conversion_and_zero`` closes the elastic gate outright (:func:`phonon.
    elastic_admissible`); ``needs_zero`` with an in-window centre opens it.
    ``qe`` — the card-declared quasi-elastic width (``quasielastic_fwhm``).
    ``stokes_only`` — a planted Stokes peak whose predicted anti-Stokes is buried in background
    (E=30 meV, T=100 K) versus resolvable (E=5 meV); ``prior_omega=None`` would force ``tied``.
    """
    prior = 30.0 if stokes_only else 5.0
    x = np.linspace(-10.0, 40.0, 101)
    counts = 10.0 + 30.0 * np.exp(-0.5 * ((x - prior) / 1.2) ** 2)
    return ScanContext(
        x=x, counts=counts, mask=np.ones_like(x, dtype=bool),
        T=100.0, center=0.0, gauss_fwhm=1.2, lorentz_fwhm=0.0, instrument="S30",
        energy_status="needs_zero" if elastic else "needs_conversion_and_zero",
        prior_omega=prior,
        quasielastic_fwhm=1.5 if qe else None,
    )


def _identity(component) -> tuple:
    """Component identity that survives class-vs-instance recipes: (kind, class, prefix, peak).

    A bare class carries no prefix/peak override; an instance carries the ones it was built with.
    Comparing these catches a swap of ``PhononDHO`` for ``PhononDHO(peak="stokes")`` — exactly the
    distinction the legacy ``_phonon_for`` encodes.
    """
    if isinstance(component, type):
        return ("class", component.__name__, None, None)
    return ("instance", type(component).__name__,
            getattr(component, "prefix", None), getattr(component, "peak", None))


def _shape(hyps) -> list[tuple]:
    return [(h.label, tuple(_identity(c) for c in h.components)) for h in hyps]


# Snapshot of the legacy enumeration, verified against the pre-catalogue implementation.
_BG = ("class", "ConstantBackground", None, None)
_ELASTIC = ("class", "Elastic", None, None)
_QE = ("class", "QuasiElastic", None, None)
_PHO_TIED = ("class", "PhononDHO", None, None)
_PHO_STOKES = ("instance", "PhononDHO", "phonon", "stokes")


def _expected(elastic: bool, qe: bool, stokes_only: bool) -> list[tuple]:
    pho = _PHO_STOKES if stokes_only else _PHO_TIED
    out = [("stokes+bg", (pho, _BG))]
    if elastic:
        out.append(("elastic+stokes+bg", (pho, _ELASTIC, _BG)))
    if qe:
        out.append(("stokes+qe+bg", (pho, _QE, _BG)))
    if elastic and qe:
        out.append(("elastic+stokes+qe+bg", (pho, _ELASTIC, _QE, _BG)))
    # Phonon-less nulls close the list (SCENE-1 PR-a).
    if elastic:
        out.append(("elastic+bg", (_ELASTIC, _BG)))
    out.append(("bg", (_BG,)))
    return out


_COMBOS = [(e, q, s) for e in (False, True) for q in (False, True) for s in (False, True)]


@pytest.mark.parametrize("elastic,qe,stokes_only", _COMBOS)
def test_catalogue_enumeration_matches_legacy(elastic, qe, stokes_only):
    """The catalogue-backed enumeration reproduces the legacy structure list exactly."""
    ctx = _ctx(elastic=elastic, qe=qe, stokes_only=stokes_only)
    # the ctx really does drive the gates the parametrisation claims
    assert phonon.elastic_admissible(ctx) is elastic
    assert phonon.quasielastic_admissible(ctx) is qe
    assert phonon.antistokes_mode(ctx) == ("stokes_only" if stokes_only else "tied")

    assert _shape(phonon.enumerate_phonon_structures(ctx)) == _expected(elastic, qe, stokes_only)


def test_catalogue_profile_names_are_frozen():
    """SCENE-2 campaign programs freeze these strings as their ``catalogue_profile`` reference."""
    assert catalogue.CATALOGUE_SCHEMA == "isar.phenomenon-catalogue/1"
    assert catalogue.DEFAULT_PROFILE == "isar.catalogue.default/1"
    assert catalogue.PROFILES[catalogue.DEFAULT_PROFILE] is catalogue.DEFAULT_CATALOGUE


def test_catalogue_entries_present():
    names = {d.name for d in catalogue.DEFAULT_CATALOGUE}
    assert names == {"acoustic", "optic", "elastic", "quasielastic", "unknown", "bg_only"}
    assert all(d.identity for d in catalogue.DEFAULT_CATALOGUE)


def test_quasielastic_is_its_own_phenomenon():
    """QE is measured in its own right, so it is an entry — not a second local model of elastic."""
    assert [m.name for m in catalogue.entry("elastic").local_models] == ["sharp"]
    assert [m.name for m in catalogue.entry("quasielastic").local_models] == ["broad"]


# --- multimode enumeration is catalogue-driven (PR-b), pinned to the legacy list -------------

_PHO2_TIED = ("instance", "PhononDHO", "phonon2", "stokes+astokes")
_PHO2_STOKES = ("instance", "PhononDHO", "phonon2", "stokes")


def _expected_multimode(elastic: bool, qe: bool, stokes_only: bool) -> list[tuple]:
    pho = _PHO_STOKES if stokes_only else _PHO_TIED
    pho2 = _PHO2_STOKES if stokes_only else _PHO2_TIED
    out = [("stokes2+bg", (pho, pho2, _BG))]
    if elastic:
        out.append(("elastic+stokes2+bg", (pho, pho2, _ELASTIC, _BG)))
    if qe:
        out.append(("stokes2+qe+bg", (pho, pho2, _QE, _BG)))
    if elastic and qe:
        out.append(("elastic+stokes2+qe+bg", (pho, pho2, _ELASTIC, _QE, _BG)))
    return out


@pytest.mark.parametrize("elastic,qe,stokes_only", _COMBOS)
def test_catalogue_multimode_matches_legacy(elastic, qe, stokes_only):
    ctx = _ctx(elastic=elastic, qe=qe, stokes_only=stokes_only)
    got = _shape(phonon.enumerate_phonon_multimode(ctx, n_max=2))
    expected = _expected_multimode(elastic, qe, stokes_only)
    # mode 1: class when tied (no prefix override), instance when stokes-only — same as 1-mode.
    if not stokes_only:
        expected = [(label, ((_PHO_TIED[0], _PHO_TIED[1], None, None), *rest[1:]))
                    for label, rest in expected]
    assert got == expected


def test_multimode_below_two_is_empty():
    assert phonon.enumerate_phonon_multimode(_ctx(elastic=True, qe=True,
                                                  stokes_only=False), n_max=1) == []


# --- constrained variants are executable declarations (PR-c) ---------------------------------
def test_near_degenerate_variant_declaration_is_executable():
    """The acoustic entry declares one live variant: a predicate on ω̂ and a pin builder."""
    variant, = catalogue.entry("acoustic").constrained_variants
    assert variant.name == "near_degenerate"
    assert variant.label_suffix == "_neardeg"

    ctx = _ctx(elastic=True, qe=False, stokes_only=False)      # gauss_fwhm = 1.2
    assert variant.when(ctx, 0.5) is True
    assert variant.when(ctx, ctx.gauss_fwhm) is True           # at the width, inclusive
    assert variant.when(ctx, 5.0) is False


def test_near_degenerate_pins_intrinsic_width_to_the_floor():
    """Γ → the floor, so the OBSERVED width is the resolution: the DHO is convolved with the
    resolution kernel downstream, and pinning Γ to ``gauss_fwhm`` would model a peak twice as
    broad as the variant claims."""
    variant, = catalogue.entry("acoustic").constrained_variants
    ctx = _ctx(elastic=True, qe=False, stokes_only=False)

    hard, origins = variant.pins(ctx, "phonon2")
    assert hard == {"phonon2.Gamma": phonon.GAMMA_FLOOR}
    assert origins == {"phonon2.Gamma": "catalogue_variant"}
    assert hard["phonon2.Gamma"] != pytest.approx(ctx.gauss_fwhm)


# --- the identity axis, local half (PR-c commit E) --------------------------------------------
#
# Identity is open-vocabulary and catalogue-driven: nothing below names a phenomenon the code
# knows about. The split is earned only by a DISTINGUISHING declaration — two entries sharing a
# local model with no declared prior would be twins with identical evidence, and enumerating both
# would only divide the uniform structure prior between copies of one model.
_SYNTH = "isar.catalogue.test-identity/1"

_TWO_IDENTITIES = (
    catalogue.PhenomenonDefinition(
        name="a", identity="synthetic identity A — declares a distinguishing prior",
        local_models=(catalogue.LocalModel("dho", PhononDHO),),
        parameter_priors=(catalogue.ParamPrior("Omega_d", "gaussian", 4.0, 0.5),),
    ),
    catalogue.PhenomenonDefinition(
        name="b", identity="synthetic identity B — a different prior on the same model",
        local_models=(catalogue.LocalModel("dho", PhononDHO),),
        parameter_priors=(catalogue.ParamPrior("Omega_d", "gaussian", 8.0, 0.5),),
    ),
    catalogue.PhenomenonDefinition(
        name="elastic", identity="elastic", gates=(phonon.elastic_admissible,),
        local_models=(catalogue.LocalModel("sharp", Elastic, phonon.elastic_admissible),),
    ),
    catalogue.PhenomenonDefinition(
        name="quasielastic", identity="qe", gates=(phonon.quasielastic_admissible,),
        local_models=(catalogue.LocalModel("broad", QuasiElastic,
                                           phonon.quasielastic_admissible),),
    ),
    catalogue.PhenomenonDefinition(
        name="bg_only", identity="background alone",
        local_models=(catalogue.LocalModel("flat", ConstantBackground),),
    ),
)


@pytest.fixture()
def synth_profile(monkeypatch):
    """A two-identity catalogue profile registered for the duration of one test."""
    monkeypatch.setitem(catalogue.PROFILES, _SYNTH, _TWO_IDENTITIES)
    return _SYNTH


def test_production_catalogue_declares_no_identity_split():
    """The no-dilution rule, at the source: no production entry declares a prior, so the phonon
    slot has no distinguishable rivals and enumeration must not split."""
    assert catalogue._identity_candidates(PhononDHO, catalogue.DEFAULT_PROFILE) == ()
    for ctx in (_ctx(elastic=e, qe=q, stokes_only=s) for e, q, s in _COMBOS):
        for hyp in (*catalogue.enumerate_structures(ctx),
                    *catalogue.enumerate_multimode_structures(ctx, 3)):
            assert hyp.identity is None
            assert "@" not in hyp.label


@pytest.mark.parametrize("elastic,qe,stokes_only", _COMBOS)
def test_undistinguished_rivals_do_not_split(monkeypatch, elastic, qe, stokes_only):
    """Two entries sharing the phonon model but declaring NO prior stay one candidate — the
    enumeration is bit-identical (labels, order, components, k) to the identity-agnostic list."""
    bare = tuple(catalogue.PhenomenonDefinition(
        name=d.name, identity=d.identity, local_models=d.local_models, gates=d.gates)
        for d in _TWO_IDENTITIES)
    monkeypatch.setitem(catalogue.PROFILES, _SYNTH, bare)
    ctx = _ctx(elastic=elastic, qe=qe, stokes_only=stokes_only)

    got = catalogue.enumerate_structures(ctx, profile=_SYNTH)
    assert _shape(got) == _expected(elastic, qe, stokes_only)
    assert [h.k for h in got] == [h.k for h in phonon.enumerate_phonon_structures(ctx)]
    assert all(h.identity is None for h in got)


def test_distinguished_rivals_split_every_phonon_bearing_structure(synth_profile):
    ctx = _ctx(elastic=True, qe=True, stokes_only=False)
    hyps = catalogue.enumerate_structures(ctx, profile=synth_profile)

    assert [h.label for h in hyps] == [
        "stokes+bg@a", "stokes+bg@b",
        "elastic+stokes+bg@a", "elastic+stokes+bg@b",
        "stokes+qe+bg@a", "stokes+qe+bg@b",
        "elastic+stokes+qe+bg@a", "elastic+stokes+qe+bg@b",
        "elastic+bg", "bg",
    ]
    by_label = {h.label: h for h in hyps}
    assert by_label["stokes+bg@a"].identity == "a"
    assert by_label["stokes+bg@b"].identity == "b"
    # the phonon-less nulls hold no phenomenon to identify, and the split costs no parameters
    assert by_label["elastic+bg"].identity is None and by_label["bg"].identity is None
    assert by_label["stokes+bg@a"].k == by_label["stokes+bg@b"].k
    assert by_label["stokes+bg@a"].components == by_label["stokes+bg@b"].components


def test_multimode_enumeration_carries_identity(synth_profile):
    ctx = _ctx(elastic=True, qe=False, stokes_only=True)
    hyps = catalogue.enumerate_multimode_structures(ctx, 2, profile=synth_profile)

    assert [h.label for h in hyps] == ["stokes2+bg@a", "stokes2+bg@b",
                                       "elastic+stokes2+bg@a", "elastic+stokes2+bg@b"]
    assert [h.identity for h in hyps] == ["a", "b", "a", "b"]
    # one identity per CANDIDATE: both of a two-mode candidate's modes are priced under it
    assert all(len(h.build().phonon_indices()) == 2 for h in hyps)


def test_identity_conditions_the_prior_bundle(synth_profile):
    """The whole point of the split: twins of one structure resolve DIFFERENT priors."""
    ctx = _ctx(elastic=False, qe=False, stokes_only=False)
    by_label = {h.label: h for h in catalogue.enumerate_structures(ctx, profile=synth_profile)}

    bundles = {}
    for label in ("stokes+bg@a", "stokes+bg@b"):
        hyp = by_label[label]
        prob = loop.build_problem(hyp, ctx)
        bundles[label] = catalogue.resolve_priors(
            prob.composite, ctx, prob.bounds,
            entry=catalogue.identity_entry(hyp.identity, profile=synth_profile))

    a = bundles["stokes+bg@a"].by_name()["phonon.Omega_d"]
    b = bundles["stokes+bg@b"].by_name()["phonon.Omega_d"]
    assert (a.kind, a.mu, a.origin) == ("gaussian", 4.0, "identity")
    assert (b.kind, b.mu, b.origin) == ("gaussian", 8.0, "identity")
    assert bundles["stokes+bg@a"].spec() != bundles["stokes+bg@b"].spec()
    # everything else is untouched by identity
    assert [p.name for p in bundles["stokes+bg@a"].params] == \
           [p.name for p in bundles["stokes+bg@b"].params]


def test_identity_entry_is_none_for_agnostic_and_loud_for_unknown(synth_profile):
    assert catalogue.identity_entry(None) is None
    assert catalogue.identity_entry("a", profile=synth_profile).name == "a"
    with pytest.raises(KeyError):
        catalogue.identity_entry("no_such_phenomenon", profile=synth_profile)


def _low_omega_ctx() -> ScanContext:
    """A phonon whose ω₀ sits below the resolution width, so ``near_degenerate`` fires. The
    elastic gate is closed (``needs_conversion_and_zero``) to keep the candidate list short."""
    x = np.linspace(-6.0, 6.0, 81)
    counts = convolute_voigt_dho(x, 0.6, 0.0, 1.2, 0.0, 0.3, 300.0, 0.0, 12.0, 150.0, peak="all")
    return ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=150.0,
                       center=0.0, gauss_fwhm=1.2, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="needs_conversion_and_zero", prior_omega=0.6)


def test_variants_come_from_the_entry_that_owns_the_identity(monkeypatch):
    """A constrained variant is a provision of ONE phenomenon. Once identity splitting is live,
    the ``@a`` twin must be offered ``a``'s variant and the ``@b`` twin nothing — offering every
    identity the same entry's variant would price physics the catalogue never declared there."""
    with_variant = replace(_TWO_IDENTITIES[0],
                           constrained_variants=catalogue.entry("acoustic").constrained_variants)
    monkeypatch.setitem(catalogue.PROFILES, _SYNTH, (with_variant, *_TWO_IDENTITIES[1:]))
    ctx = _low_omega_ctx()

    hyps = catalogue.enumerate_structures(ctx, profile=_SYNTH)
    assert [h.label for h in hyps] == ["stokes+bg@a", "stokes+bg@b", "bg"]   # incl. the null
    pairs = [(loop.fit_hypothesis(h, ctx, minos=False), h) for h in hyps]
    fits = {h.label: r for r, h in pairs}
    assert all(fits[l].optimizer_converged and fits[l].omega0 <= ctx.gauss_fwhm
               for l in ("stokes+bg@a", "stokes+bg@b"))      # both twins are near-degenerate

    offered = loop._offer_variants(pairs, ctx, minos=False, profile=_SYNTH)

    assert [twin.label for _, twin, _, _ in offered] == ["stokes+bg@a_neardeg"]
    _, twin, derived, x0 = offered[0]
    assert x0 == fits["stokes+bg@a"].params                  # the seed kept is the parent's fit
    assert twin.identity == "a"                              # the twin inherits its parent's
    assert derived.constraints.hard == {"phonon.Gamma": phonon.GAMMA_FLOOR}
    assert derived.constraints.origins == {"phonon.Gamma": "catalogue_variant"}


def test_identity_twins_share_one_arena_with_independent_evidence(synth_profile):
    """Two identities of one structure are independent evidence contributions in the SAME arena:
    same data likelihood, distinct records, distinct lnZ (the priors differ, the fit does not)."""
    ctx = _ctx(elastic=False, qe=False, stokes_only=False)      # a real peak at 5 meV
    by_label = {h.label: h for h in catalogue.enumerate_structures(ctx, profile=synth_profile)}

    triples = []
    for label in ("stokes+bg@a", "stokes+bg@b"):
        hyp = by_label[label]
        result = loop.fit_hypothesis(hyp, ctx)
        assert result.identity == hyp.identity        # carried from hypothesis at fit time
        prob = loop.build_problem(hyp, ctx)
        bundle = catalogue.resolve_priors(
            prob.composite, ctx, prob.bounds,
            entry=catalogue.identity_entry(hyp.identity, profile=synth_profile))
        triples.append((hyp, result, bundle))

    es = evidence.candidate_set_evidence(triples, ctx)
    recs = es.by_label()
    assert set(recs) == {"stokes+bg@a", "stokes+bg@b"}
    assert all(r.arena_fingerprint == es.arena_fingerprint for r in es.records)
    assert recs["stokes+bg@a"].prior_spec != recs["stokes+bg@b"].prior_spec
    # the data-only fit is identical, so any lnZ difference is the identity prior, not the data
    assert recs["stokes+bg@a"].log_evidence != recs["stokes+bg@b"].log_evidence

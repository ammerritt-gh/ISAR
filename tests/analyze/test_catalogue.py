"""The phenomenon catalogue (SCENE-1) and its enumeration pin.

:func:`isar.analyze.phonon.enumerate_phonon_structures` is catalogue-backed. Its output is pinned
here — labels, order, and per-component identity — for every reachable gate combination
(elastic × quasi-elastic × anti-Stokes mode). The phonon-bearing prefix was snapshotted from the
legacy implementation before it was deleted and must never drift; the phonon-less nulls
(``elastic+bg``, ``bg``) were appended deliberately at SCENE-1 PR-a (the ``elastic_only`` /
``no_signal`` selection candidates) and are pinned from that point on. A drift here is a real
regression, not a test to update.
"""
import numpy as np
import pytest

from isar.analyze import catalogue, phonon
from isar.analyze.context import ScanContext


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
    got = _shape(phonon.enumerate_phonon_multimode(ctx))
    expected = _expected_multimode(elastic, qe, stokes_only)
    # mode 1: class when tied (no prefix override), instance when stokes-only — same as 1-mode.
    if not stokes_only:
        expected = [(label, ((_PHO_TIED[0], _PHO_TIED[1], None, None), *rest[1:]))
                    for label, rest in expected]
    assert got == expected


def test_multimode_below_two_is_empty():
    assert phonon.enumerate_phonon_multimode(_ctx(elastic=True, qe=True,
                                                  stokes_only=False), n_max=1) == []

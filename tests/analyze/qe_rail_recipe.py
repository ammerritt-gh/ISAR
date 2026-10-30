"""Synthetic recipe for a central QE railed at its lower width bound beside the elastic line (change 8).

The truth is a BL43LXU spectrum on an elastic line a little wider than the fit's resolution, which puts
the fit's QE on the elastic ridge. Every number is made up and generated at test time (hard rule 10);
the committed resolution curve a06 is the only measured thing used.
"""
import numpy as np

from isar.analyze import fast_pass, prior
from isar.analyze.components import CentralQE, CompositeModel, ConstantBackground, Elastic, PhononDHO
from isar.analyze.context import ScanContext
from isar.synth import bl43_curves

CURVE = bl43_curves.curve("a06")
X = np.arange(-5.0, 20.0, 0.25)
SETTINGS = fast_pass.FastPassSettings(mode_ceiling=2)


def context(counts=None, x=X, curve=CURVE, gauss_fwhm=0.0):
    y = np.ones(x.size) if counts is None else np.asarray(counts, dtype=float)
    return ScanContext(x=x, counts=y, mask=np.ones(x.size, bool), T=97.0, center=0.0,
                       gauss_fwhm=gauss_fwhm, lorentz_fwhm=0.0, instrument="BL43LXU",
                       energy_status="absolute", resolution_curve=curve)


def qe_floor():
    """The QE width's lower bound and one resolution FWHM, meV, on the recipe's context."""
    eng = fast_pass._Engine(context(), SETTINGS)
    return eng.sp.bounds("qe_width")[0], eng.fwhm


def widened(seed, *, wide=1.6, gamma1=0.4, qe_h=0.0, qe_width=None, bg=2.0):
    """Poisson counts of two phonons, an elastic line, a central QE and a background, with the elastic
    line ``wide`` times the fit's resolution FWHM. ``qe_width`` defaults to 1.05 x the lower bound."""
    lo, fwhm = qe_floor()
    qe_width = 1.05 * lo if qe_width is None else qe_width
    truth = context(gauss_fwhm=wide * fwhm, curve=None)
    comp = CompositeModel([PhononDHO(), PhononDHO(), Elastic(), CentralQE(), ConstantBackground()])
    theta = [5.0, gamma1, 60.0, 11.0, 0.8, 40.0, 200.0, qe_width, qe_h, bg]
    mean = comp.evaluate(X, np.asarray(theta, dtype=float), truth)
    return context(np.random.default_rng(seed).poisson(mean).astype(float))

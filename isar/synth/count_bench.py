"""Peak benchmark for the fast pass: does it find the peaks a human would call clear, and say so?

Method (STATS_PRIMER section 2.3 is written from this docstring).

Generator. Truths are made with the production model: full DHO pairs (Stokes and anti-Stokes by
detailed balance), an elastic line and a flat background, convolved with one of the 28 committed
BL43LXU resolution curves (:mod:`isar.synth.bl43_curves`), on the real files' grid (-16..+20 meV in
0.2 meV steps), then drawn as Poisson counts. The strongest phonon's height h (C-dho's coordinate,
the maximum of the convolved profile over E >= 0, counts per bin) is 600 at level ``high`` (302 K-like)
or 300 at level ``low`` (97 K-like); the elastic line is 0.2 x and the background 0.04 x that
(background 0 in a share of the random suite). Exposure is 1: heights are counts per point, the
fitter's own coordinate. Fixed *named* truths cover the cases a human has judged (broad low peak, weak
shoulder, close doublet, near-elastic peak, zero background, and a 302 K / 97 K soft-mode pair);
a *random suite* draws the same kinds with seeded parameters. The truth never reaches the fitter: it
sees an :class:`Observation` (counts, T, the resolution curve, exposure), nothing else.

Visibility grade. For a true peak, ``grade = D(lam_full, lam_full - lam_peak)`` with the deviance
``D(mu, nu) = 2 sum[mu ln(mu/nu) - (mu - nu)]`` (twice the Poisson log-likelihood difference between
the noiseless truth with and without that peak, everything else held fixed). The refit diagnostic is
the same difference after refitting the remaining peaks (the truth's structure minus that peak, on
noiseless counts, started at the truth), which shows how far neighbours absorb the peak.

Should-find. A true peak is should-find when grade >= 100 (level high) or 25 (low), refit >= 0.3 x
grade (isolated), omega0 > 2 resolution FWHM and Gamma < omega0 / 2. Other peaks are scored for
matching and for false peaks only; a weak shoulder or broad mode is never counted as a miss.

Matching. Per spectrum, fitted peaks (confident and tentative) against true peaks, one-to-one,
within one resolution FWHM: the assignment that maximises the number of in-gate pairs and then
minimises the total |delta omega0| (a greedy nearest-first match can lose a pair). Each should-find
peak gets exactly one outcome: ``confident`` (matched to a confident fitted peak),
``tentative_or_abstained`` (matched to a tentative one, or the fit errored or gave no answer) or
``missed``. A confident fitted peak with no match is a false peak.

Figures (targets, never gates), each with numerator, denominator and Wilson 95% interval, for all
spectra and split by level: missed / should-find (aim <= 0.10); false / confident fitted (aim <=
0.05); shares of the four count states; flag hit rate on planted shoulder, broad_low and soft cases (a
flag whose [e_lo - FWHM, e_hi + FWHM] holds the planted omega0); false-flag rate on ``clean`` spectra
(any flag); per-spectrum time (median, p90) from ``fast_pass_timing.total_seconds``.

Streams. Development uses the named truths with draws from ``STREAM_DRAW_FIT`` and random truths
from ``STREAM_CASE_TUNING``; evaluation uses the same named truths with draws from ``STREAM_DRAW_EVAL``
and random truths from ``STREAM_CASE_EVAL``. Thresholds are tuned on development; the settings are
then frozen and the reported figures come from evaluation. ``figures.json`` records the settings in
force (``dataclasses.asdict`` of ``guidelines.fast_pass_settings(card)``) and their sha256.
``--settings`` reaches the fits only through the bench card's ``[fitting.fast_pass]`` and
``[fitting.prior]`` tables, production's one settings path.

Outputs (``out/count_bench/<timestamp>/``): ``figures.json``, ``cases.csv`` (one row per spectrum),
``true_peaks.csv``, ``fitted_peaks.csv``, ``records.jsonl`` (per spectrum: the truth, the fast-pass
record verbatim, the timing) and the gallery.

Run it deliberately: ``python -m isar.synth.count_bench --help`` or double-click
``run-isar-count-bench.bat``. The routine suite runs only canned records and fake fits
(``isar/synth/tests/test_count_bench.py``).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import math
import os
import sys
import time
import tomllib
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import numpy as np
from scipy.optimize import linear_sum_assignment

from isar.analyze.phonon import DEFAULT_MODE_CEILING, MODE_CEILING_ENV, default_mode_ceiling

from . import bl43_curves, config, noise

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- named constants
# The mode ceiling is production's one rule (isar.analyze.phonon.default_mode_ceiling: the
# environment, else 6); the generator takes an explicit argument over it, which may be 0.

# Independent random streams: every draw is seeded from (base_seed, stream, keys...), so the
# development cases, the evaluation cases and the two kinds of Poisson draw never share numbers.
STREAM_NAMED = 0             # label of the fixed named truths (they draw no case parameters)
STREAM_CASE_TUNING = 1       # random truths of the development set
STREAM_CASE_EVAL = 2         # random truths of the evaluation set
STREAM_DRAW_FIT = 3          # Poisson draws the development set shows the fitter
STREAM_DRAW_EVAL = 8         # Poisson draws the evaluation set shows the fitter
STREAMS = (STREAM_CASE_TUNING, STREAM_CASE_EVAL, STREAM_DRAW_FIT, STREAM_DRAW_EVAL)

# Draw counts [setting]: estimates at 30 s per fit and 12 workers; S1U7 rescales by measured time.
MODE_PLANS = {
    "development": dict(case_stream=STREAM_CASE_TUNING, draw_stream=STREAM_DRAW_FIT,
                        named_draws=5, random=60, clean=150),
    "evaluation": dict(case_stream=STREAM_CASE_EVAL, draw_stream=STREAM_DRAW_EVAL,
                       named_draws=10, random=200, clean=200),
}
CLEAN_INDEX0 = 100_000       # the extra clean spectra count up from here, apart from the random suite

TARGET_MISSED = 0.10         # aim: missed / should-find (reported, never a gate)
TARGET_FALSE = 0.05          # aim: false / confident fitted
TUNING_WORKERS = 16
HARD_KILL_GRACE_S = 300.0    # no case starts in the last this many seconds before the wall-clock cap

# Generator [setting]: round numbers, not file values (hard rule 10). Energy axis = the real files' grid.
X_MIN, X_MAX, X_STEP = -16.0, 20.0, 0.2
LEVEL_HEIGHT = {"high": 600.0, "low": 300.0}   # strongest phonon h, counts per bin
LEVEL_T = {"high": 300.0, "low": 100.0}        # 302 K-like and 97 K-like
ELASTIC_REL, BACKGROUND_REL = 0.2, 0.04        # of the strongest phonon height
ZERO_BACKGROUND_SHARE = 0.10                   # of the random suite
ANALYZERS = tuple(d["detector_id"] for d in
                  tomllib.loads(bl43_curves.CARD.read_text(encoding="utf-8"))["detectors"])
KIND_WEIGHTS = {
    "clean": 0.25, "single": 0.10, "separated": 0.15, "close_doublet": 0.10, "shoulder": 0.12,
    "broad_low": 0.10, "near_elastic": 0.08, "empty": 0.05, "generic": 0.05,
}
NEEDS_TWO_MODES = ("separated", "close_doublet", "shoulder", "broad_low", "generic")
CLEAN_GRADE_MIN = 100.0      # every peak of a clean spectrum is at least this visible
CLEAN_RESAMPLES = 200

# Should-find [setting from the operator's judgements of the 2026-10-25 galleries].
G_CLEAR = {"high": 100.0, "low": 25.0}
REFIT_SHARE_MIN = 0.3        # refit >= this x grade: not absorbed by neighbours
ISOLATED_FWHM = 2.0          # omega0 above this many FWHM
MAX_GAMMA_OVER_OMEGA0 = 0.5


# --------------------------------------------------------------------------- seeds and ceiling
def mode_ceiling(explicit: int | None = None) -> int:
    """The generator's maximum mode count: the explicit argument (0 allowed: an empty-only
    benchmark), else production's :func:`~isar.analyze.phonon.default_mode_ceiling`."""
    if explicit is None:
        return default_mode_ceiling()
    value = int(explicit)
    if value < 0:
        raise ValueError(f"mode ceiling must be >= 0, got {value}")
    return value


def rng_for(base_seed: int, stream: int, *keys: int) -> np.random.Generator:
    """A generator that depends only on ``(base_seed, stream, keys)`` (scheduling-independent)."""
    return np.random.default_rng(np.random.SeedSequence([int(base_seed), int(stream),
                                                         *(int(k) for k in keys)]))


# --------------------------------------------------------------------------- truth (harness-only)
@dataclass(frozen=True)
class ModeTruth:
    omega0: float        # E0, the undamped energy (meV)
    gamma: float         # DHO damping (meV); above omega0 is overdamped
    height: float        # h: maximum of the convolved mode over E >= 0, counts per bin (C-dho)


@dataclass(frozen=True)
class CaseTruth:
    """Everything the generator knows. Held by the harness; the fitter never receives it."""

    stream: int          # STREAM_NAMED, STREAM_CASE_TUNING or STREAM_CASE_EVAL
    index: int
    kind: str
    modes: tuple         # ascending omega0
    elastic_height: float
    background: float
    T: float
    exposure: float      # 1.0: heights are counts per point, the fitter's coordinate
    gauss_fwhm: float    # the curve's FWHM (lorentz_fwhm is 0, as ScanContext makes it)
    lorentz_fwhm: float
    ceiling: int
    resolution: str = "a06"      # key into bl43_curves
    level: str = "high"
    name: str = ""               # a named truth's name
    planted: int | None = None   # index into modes of the planted shoulder / broad / soft mode

    @property
    def case_id(self) -> str:
        if self.name:
            return self.name
        return f"{'T' if self.stream == STREAM_CASE_TUNING else 'E'}{self.index:04d}"

    @property
    def n_modes(self) -> int:
        return len(self.modes)


def curve_of(key: str):
    return bl43_curves.curve(key)


def _loguniform(rng, lo: float, hi: float) -> float:
    return float(math.exp(rng.uniform(math.log(lo), math.log(hi))))


def _build(*, stream, index, kind, drawn, planted, level, resolution, background_on, ceiling,
           name="", T=None) -> CaseTruth:
    """A truth from ``drawn = [(omega0, gamma, relative_height)]`` (the strongest has 1); ``planted``
    indexes ``drawn``. Modes are stored ascending in omega0."""
    scale, fwhm = LEVEL_HEIGHT[level], curve_of(resolution).fwhm
    order = sorted(range(len(drawn)), key=lambda i: drawn[i][0])
    modes = tuple(ModeTruth(float(drawn[i][0]), float(drawn[i][1]), scale * float(drawn[i][2]))
                  for i in order)
    return CaseTruth(
        stream=int(stream), index=int(index), kind=kind, modes=modes,
        elastic_height=ELASTIC_REL * scale, background=BACKGROUND_REL * scale if background_on else 0.0,
        T=LEVEL_T[level] if T is None else float(T), exposure=1.0, gauss_fwhm=float(fwhm),
        lorentz_fwhm=0.0, ceiling=int(ceiling), resolution=resolution, level=level, name=name,
        planted=None if planted is None or planted >= len(drawn) else order.index(planted))


def _mode(omega0: float, gamma: float, rel: float, *, underdamped: bool = True) -> tuple:
    """A mode inside the window; underdamped (gamma < omega0 / 1.2) unless a broad kind asks otherwise."""
    omega0 = float(min(max(omega0, 0.4), X_MAX - 1.0))
    return omega0, float(min(gamma, omega0 / 1.2) if underdamped else gamma), float(rel)


def _spaced(rng, n: int, fwhm: float, lo: float, gap: float) -> np.ndarray:
    """``n`` ascending energies in [lo, X_MAX - 1] at least ``gap`` apart (best effort after 200 tries)."""
    for _ in range(200):
        energies = np.sort(rng.uniform(lo, X_MAX - 1.0, size=n))
        if n == 1 or np.min(np.diff(energies)) >= gap:
            break
    return energies


def _draw_modes(kind: str, rng, fwhm: float, ceiling: int) -> tuple:
    """``([(omega0, gamma, relative_height)], planted_index_or_None)`` for one kind."""
    gam = lambda: _loguniform(rng, 0.15, 1.5)   # noqa: E731
    if kind == "empty":
        return [], None
    if kind == "single":
        return [_mode(rng.uniform(2.5, 18.0), gam(), 1.0)], None
    if kind in ("clean", "separated"):
        n = int(rng.integers(1, min(3, ceiling) + 1)) if kind == "clean" else int(rng.integers(2, min(3, ceiling) + 1))
        lo = 2.0 * fwhm + 0.2 if kind == "clean" else 1.0
        energies = _spaced(rng, n, fwhm, lo, 3.0 * fwhm)
        g_hi = 0.5 if kind == "clean" else 1.5
        heights = [rng.uniform(0.3, 1.0) for _ in range(n)]
        heights[int(rng.integers(n))] = 1.0
        return [_mode(w, _loguniform(rng, 0.15, g_hi), h) for w, h in zip(energies, heights)], None
    if kind == "close_doublet":
        w1, sep, g = rng.uniform(3.0, 15.0), rng.uniform(0.5, 2.0) * fwhm, gam()
        return [_mode(w1, g, 1.0), _mode(w1 + sep, g * rng.uniform(0.7, 1.4), rng.uniform(0.3, 1.0))], None
    if kind == "shoulder":
        w1 = rng.uniform(4.0, 14.0)
        sep = rng.choice([-1.0, 1.0]) * rng.uniform(0.8, 1.5) * fwhm
        return [_mode(w1, gam(), 1.0), _mode(w1 + sep, gam(), rng.uniform(0.05, 0.15))], 1
    if kind == "broad_low":
        main = [_mode(w, gam(), h) for w, h in zip(_spaced(rng, 2, fwhm, 2.5, 4.0 * fwhm), (1.0, rng.uniform(0.3, 1.0)))]
        broad = _mode(rng.uniform(4.0, 14.0), rng.uniform(2.0, 4.0), rng.uniform(0.04, 0.10), underdamped=False)
        return [*main, broad], 2
    if kind == "near_elastic":
        modes = [_mode(rng.uniform(0.7, 2.5) * fwhm, _loguniform(rng, 0.15, 0.6), 1.0)]
        if ceiling >= 2 and rng.random() < 0.5:
            modes.append(_mode(rng.uniform(5.0, 17.0), gam(), rng.uniform(0.2, 1.0)))
        return modes, None
    n = int(rng.integers(2, ceiling + 1))      # generic: 2..ceiling modes, spaced >= 0.3 FWHM
    energies = _spaced(rng, n, fwhm, 1.0, 0.3 * fwhm)
    heights = [_loguniform(rng, 0.15, 1.0) for _ in range(n)]
    heights[int(rng.integers(n))] = 1.0
    return [_mode(w, gam(), h) for w, h in zip(energies, heights)], None


def generate_case(base_seed: int, stream: int, index: int, ceiling: int | None = None,
                  kind: str | None = None) -> CaseTruth:
    """One seeded random truth. ``ceiling`` is the largest mode count it may hold; ``kind`` forces a
    kind (the extra clean spectra)."""
    ceiling = mode_ceiling(ceiling)
    rng = rng_for(base_seed, stream, index)
    resolution = str(rng.choice(ANALYZERS))
    level = "high" if rng.random() < 0.5 else "low"
    background_on = rng.random() >= ZERO_BACKGROUND_SHARE
    if kind is None:
        kinds = list(KIND_WEIGHTS)
        kind = str(rng.choice(kinds, p=np.array(list(KIND_WEIGHTS.values())) / sum(KIND_WEIGHTS.values())))
    if ceiling < 2 and kind in NEEDS_TWO_MODES:
        kind = "single"
    if ceiling < 1:
        kind = "empty"
    fwhm = curve_of(resolution).fwhm
    common = dict(stream=stream, index=index, level=level, resolution=resolution,
                  background_on=background_on, ceiling=ceiling)
    if kind == "clean":                 # every peak clearly visible, else resample, else a single peak
        for _ in range(CLEAN_RESAMPLES):
            drawn, planted = _draw_modes(kind, rng, fwhm, ceiling)
            truth = _build(kind=kind, drawn=drawn, planted=planted, **common)
            if min(g for g, _ in _grades_only(truth)) >= CLEAN_GRADE_MIN:
                return truth
        kind = "single"
        for _ in range(CLEAN_RESAMPLES):
            drawn, planted = _draw_modes(kind, rng, fwhm, ceiling)
            truth = _build(kind=kind, drawn=drawn, planted=planted, **common)
            if _grades_only(truth)[0][0] >= CLEAN_GRADE_MIN:
                break
        return truth
    drawn, planted = _draw_modes(kind, rng, fwhm, ceiling)
    if len(drawn) > ceiling:            # a truncated draw loses its planted mode
        drawn, planted = drawn[:ceiling], (planted if planted is not None and planted < ceiling else None)
    return _build(kind=kind, drawn=drawn, planted=planted, **common)


def named_cases(base_seed: int) -> list:
    """The 14 fixed truths (energies invented, never the paper's): the base shape plus one planted
    change, each at both levels, and the soft-mode pair. They do not depend on a stream: the same
    truths serve development and evaluation, which differ in their Poisson draws."""
    base = [(4.0, 0.3, 1.0), (9.5, 0.4, 0.3), (14.0, 0.3, 0.15)]
    plain = {
        "clean_three": (base, None, True),
        "broad_low": ([*base, (6.5, 3.0, 0.06)], 3, True),
        "shoulder": ([*base, (10.7, 0.4, 0.08)], 3, True),
        "close_doublet": ([(4.0, 0.3, 1.0), (9.3, 0.4, 0.3), (10.0, 0.4, 0.2), (14.0, 0.3, 0.15)], None, True),
        "near_elastic": ([*base, (1.5, 0.4, 0.2)], None, True),
        "zero_background": (base, None, False),
    }
    soft = {"soft_high": ("high", 302.0, [*base, (10.7, 0.4, 0.08)]),
            "soft_low": ("low", 97.0, [*base, (6.0, 4.0, 0.05)])}
    out = []
    for k, (name, (drawn, planted, bg)) in enumerate(plain.items()):
        resolution = str(rng_for(base_seed, STREAM_NAMED, k).choice(ANALYZERS))
        for level in ("high", "low"):
            out.append(_build(stream=STREAM_NAMED, index=len(out), kind=name, drawn=drawn, planted=planted,
                              level=level, resolution=resolution, background_on=bg, ceiling=6,
                              name=f"{name}_{level}"))
    for j, (name, (level, T, drawn)) in enumerate(soft.items()):
        resolution = str(rng_for(base_seed, STREAM_NAMED, len(plain) + j).choice(ANALYZERS))
        out.append(_build(stream=STREAM_NAMED, index=len(out), kind="soft", drawn=drawn, planted=3,
                          level=level, resolution=resolution, background_on=True, ceiling=6, name=name, T=T))
    return out


def energy_axis() -> np.ndarray:
    return noise.energy_grid(X_MIN, X_MAX, X_STEP)


# --------------------------------------------------------------------------- what the fitter sees
@dataclass(frozen=True, eq=False)
class Observation:
    """One measured spectrum plus the instrument facts a real fit has. No truth lives here."""

    x: np.ndarray
    counts: np.ndarray
    T: float
    gauss_fwhm: float
    lorentz_fwhm: float
    exposure: float      # the declared exposure; truths use 1, so heights are counts per point
    resolution: object = None   # the TabulatedResolution (None: the Voigt widths above)


def context_of(obs: Observation):
    from isar.analyze.context import ScanContext
    # Relative scale 1 everywhere, reference the declared exposure, source "time" (the prior recipe's
    # amplitude scale for counts that are not per monitor count). A resolution curve replaces the
    # Voigt and puts its FWHM in gauss_fwhm, lorentz_fwhm 0 (ScanContext.__post_init__).
    return ScanContext(x=obs.x, counts=obs.counts, mask=np.ones_like(obs.x, dtype=bool), T=obs.T,
                       center=0.0, gauss_fwhm=obs.gauss_fwhm, lorentz_fwhm=obs.lorentz_fwhm,
                       instrument="COUNT_BENCH", energy_status="absolute",
                       exposure_scale=np.ones_like(obs.x, dtype=float), exposure_reference=obs.exposure,
                       exposure_source="time", resolution_curve=obs.resolution)


def _prefix(i: int) -> str:
    return "phonon" if i == 0 else f"phonon{i + 1}"


def _observation_of(truth: CaseTruth, counts: np.ndarray) -> Observation:
    return Observation(energy_axis(), counts, truth.T, truth.gauss_fwhm, truth.lorentz_fwhm,
                       truth.exposure, curve_of(truth.resolution))


def _truth_model(truth: CaseTruth):
    """The production composite for the truth (anti-Stokes partner derived inside PhononDHO by
    detailed balance, never a parameter), its parameter vector, and a context to evaluate on."""
    from isar.analyze.components import CompositeModel, ConstantBackground, Elastic, PhononDHO

    ctx = context_of(_observation_of(truth, np.zeros_like(energy_axis())))
    phonons = [PhononDHO(prefix=_prefix(i)) for i in range(truth.n_modes)]
    theta = []
    for m in truth.modes:            # the height coordinate: the truth's height is the parameter
        theta += [m.omega0, m.gamma, m.height]
    theta += [truth.elastic_height, truth.background]
    return CompositeModel([*phonons, Elastic(), ConstantBackground()]), theta, ctx


def truth_lambda(truth: CaseTruth) -> np.ndarray:
    """Noise-free expected counts of the truth on the scan grid (exposure included)."""
    comp, theta, ctx = _truth_model(truth)
    return np.clip(truth.exposure * comp.evaluate(ctx.x, theta, ctx), 0.0, None)


def draw_observation(truth: CaseTruth, rng: np.random.Generator) -> Observation:
    """One Poisson realisation (sigma = sqrt(N), 0 at zero counts: no floor is applied anywhere)."""
    return _observation_of(truth, rng.poisson(truth_lambda(truth)).astype(float))


def observe(truth: CaseTruth, draw: int, base_seed: int, draw_stream: int) -> Observation:
    """The draw ``draw`` of ``truth`` in a draw stream; the gallery regenerates the data with it."""
    return draw_observation(truth, rng_for(base_seed, draw_stream, truth.stream, truth.index, draw))


# --------------------------------------------------------------------------- visibility and should-find
def deviance(mu, nu) -> float:
    """D(mu, nu) = 2 sum[mu ln(mu/nu) - (mu - nu)] over mu > 0 (nu floored at 1e-12)."""
    mu, nu = np.asarray(mu, dtype=float), np.clip(np.asarray(nu, dtype=float), 1e-12, None)
    pos = mu > 0
    return float(2.0 * np.sum(mu[pos] * np.log(mu[pos] / nu[pos]) - (mu[pos] - nu[pos])))


def _mode_lambdas(truth: CaseTruth) -> tuple:
    """(full noiseless lambda, [each mode's lambda]), exposure included."""
    comp, theta, ctx = _truth_model(truth)
    parts = comp.evaluate_components(ctx.x, theta, ctx)
    full = np.clip(truth.exposure * comp.evaluate(ctx.x, theta, ctx), 0.0, None)
    return full, [truth.exposure * parts[i][1] for i in range(truth.n_modes)]


@lru_cache(maxsize=1024)
def _grades_only(truth: CaseTruth) -> tuple:
    """``(grade, nan)`` per mode: the cheap half, used while a clean truth is being drawn."""
    full, per_mode = _mode_lambdas(truth)
    return tuple((deviance(full, full - lam), float("nan")) for lam in per_mode)


def _refit_deviance(truth: CaseTruth, skip: int, full: np.ndarray) -> float:
    """D(lam_full, the best refit of the truth's structure without mode ``skip``), on noiseless
    counts, started at the truth. nan (logged) when the refit fails."""
    from isar.analyze import loop
    from isar.analyze.components import ConstantBackground, Elastic, PhononDHO
    from isar.analyze.structures import Hypothesis

    kept = [m for j, m in enumerate(truth.modes) if j != skip]
    hyp = Hypothesis(f"refit{len(kept)}", (*[PhononDHO(prefix=_prefix(j)) for j in range(len(kept))],
                                           Elastic, ConstantBackground))
    comp = hyp.build()
    start = {"elastic.h": truth.elastic_height, "bg.BG": truth.background}
    for j, m in enumerate(kept):
        start.update({f"{_prefix(j)}.omega0": m.omega0, f"{_prefix(j)}.Gamma": m.gamma,
                      f"{_prefix(j)}.h": m.height})
    ctx = context_of(_observation_of(truth, full))
    try:
        fit = loop.fit_hypothesis(hyp, ctx, x0=np.array([start[n] for n in comp.param_names()]))
        theta = [fit.params[n] for n in comp.param_names()]
    except Exception as exc:  # noqa: BLE001 - one failed refit must not end the run
        log.warning("refit without mode %d of %s failed: %s: %s", skip, truth.case_id, type(exc).__name__, exc)
        return float("nan")
    return deviance(full, truth.exposure * comp.evaluate(ctx.x, theta, ctx))


@lru_cache(maxsize=1024)
def visibility_grades(truth: CaseTruth) -> tuple:
    """``(grade, refit)`` for every true mode, in ``truth.modes`` order (see the module docstring)."""
    full, per_mode = _mode_lambdas(truth)
    return tuple((deviance(full, full - lam), _refit_deviance(truth, i, full))
                 for i, lam in enumerate(per_mode))


def should_find(mode: ModeTruth, grade: float, refit: float, level: str, fwhm: float) -> bool:
    """A peak a human would call clear: visible, isolated, clear of the elastic line and underdamped.
    A nan refit is not isolated."""
    return bool(grade >= G_CLEAR[level] and refit >= REFIT_SHARE_MIN * grade
                and mode.omega0 > ISOLATED_FWHM * fwhm and mode.gamma < MAX_GAMMA_OVER_OMEGA0 * mode.omega0)


# --------------------------------------------------------------------------- matching and scoring
def match_peaks(true_omega0, fit_omega0, fwhm: float) -> list:
    """One-to-one ``[(true_index, fit_index)]`` within one FWHM: the most in-gate pairs first, then
    the least total |delta omega0| (out-of-gate pairs cost 1e6 and are dropped)."""
    t, f = np.asarray(true_omega0, dtype=float), np.asarray(fit_omega0, dtype=float)
    if not t.size or not f.size:
        return []
    gap = np.abs(t[:, None] - f[None, :])
    cost = np.where(gap <= fwhm, gap, 1e6)
    rows, cols = linear_sum_assignment(cost)
    return [(int(i), int(j)) for i, j in zip(rows, cols) if cost[i, j] < 1e6]


def score_case(truth: CaseTruth, fast_pass: dict | None, status: str, grades) -> tuple:
    """``(true_peak_rows, fitted_peak_rows)`` for one spectrum.

    ``fast_pass`` is the C-fastpass-record (None when the fit errored or gave no answer, ``status``
    saying which); ``grades`` is :func:`visibility_grades`. Every should-find peak gets exactly one
    outcome; a confident fitted peak with no match is false."""
    peaks = list((fast_pass or {}).get("peaks") or [])
    pairs = dict(match_peaks([m.omega0 for m in truth.modes], [p["omega0"] for p in peaks], truth.gauss_fwhm))
    matched_fit = {j: i for i, j in pairs.items()}
    true_rows = []
    for i, (m, (grade, refit)) in enumerate(zip(truth.modes, grades)):
        sf = should_find(m, grade, refit, truth.level, truth.gauss_fwhm)
        j = pairs.get(i)
        if not sf:
            outcome, why = "not_scored", ""
        elif fast_pass is None:
            outcome, why = "tentative_or_abstained", status
        elif j is None:
            outcome, why = "missed", ""
        elif peaks[j]["confident"]:
            outcome, why = "confident", ""
        else:
            outcome, why = "tentative_or_abstained", "tentative"
        true_rows.append({"index": i, "omega0": m.omega0, "gamma": m.gamma, "height": m.height,
                          "grade": grade, "refit": refit, "should_find": sf, "outcome": outcome,
                          "status": why, "matched_fit": j,
                          "abs_delta_omega0": None if j is None else abs(m.omega0 - peaks[j]["omega0"])})
    fit_rows = [{"index": int(p.get("index", j)), "omega0": p["omega0"], "confident": bool(p["confident"]),
                 "delta": bool(p.get("delta", False)), "matched_true": matched_fit.get(j),
                 "false": bool(p["confident"]) and j not in matched_fit}
                for j, p in enumerate(peaks)]
    return true_rows, fit_rows


def flags_text(flags) -> str:
    return "; ".join(f"{f['kind']} {f['e_lo']:.2f}-{f['e_hi']:.2f} meV" for f in flags)


def flag_hit(truth: CaseTruth, flags) -> bool | None:
    """Does a flag cover the planted mode (its interval widened by one FWHM)? None without a planted mode."""
    if truth.planted is None:
        return None
    w = truth.modes[truth.planted].omega0
    return any(f["e_lo"] - truth.gauss_fwhm <= w <= f["e_hi"] + truth.gauss_fwhm for f in flags)


# --------------------------------------------------------------------------- the fitter under test
def _card(ceiling: int | None = None, settings: dict | None = None):
    """The campaign card; with ``ceiling``, its ``[fitting] max_modes`` is that ceiling (the card's
    route, as a beamtime sets it; the card takes no ceiling below 1). ``settings`` is
    ``{"fast_pass": {...}, "prior": {...}}``: written into ``[fitting.fast_pass]`` and
    ``[fitting.prior]`` exactly as a hand-edited card carries them, so the fit reads them through
    ``guidelines.fast_pass_settings``, production's one settings path."""
    # The one synthetic card the STAT-2b campaign already builds; reused, not re-declared.
    from isar.synth.stat2b.campaign import _campaign_card
    card = _campaign_card()
    fitting = dict(card.policy.fitting or {})
    if ceiling is not None:
        fitting["max_modes"] = max(int(ceiling), 1)
    for table in ("fast_pass", "prior"):
        if (settings or {}).get(table):
            fitting[table] = dict(settings[table])
    return replace(card, policy=replace(card.policy, fitting=fitting))


def load_settings(text: str | None) -> dict | None:
    """``--settings``: inline JSON or the path of a JSON file, ``{"fast_pass": {...}, "prior": {...}}``."""
    if not text:
        return None
    raw = text if text.lstrip().startswith("{") else Path(text).read_text(encoding="utf-8")
    settings = json.loads(raw)
    unknown = set(settings) - {"fast_pass", "prior"}
    if unknown:
        raise ValueError(f"--settings takes only 'fast_pass' and 'prior' tables, got {sorted(unknown)}")
    return settings


def settings_meta(ceiling: int | None, settings: dict | None) -> dict:
    """What the fits ran on: the resolved FastPassSettings and its sha256 (evaluation on frozen settings)."""
    from isar.session import guidelines
    resolved = asdict(guidelines.fast_pass_settings(_card(ceiling, settings)))
    digest = hashlib.sha256(json.dumps(resolved, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    return {"fast_pass_settings": resolved, "settings_sha256": digest, "settings_override": settings}


def fit_observation(obs: Observation, *, seed: int, errors_tier: str = "cheap",
                    ceiling: int | None = None, settings: dict | None = None) -> dict:
    """The production fit of one observation: ``scan_delivery.fit_one`` on a synthetic context, at
    the bench's ``ceiling`` (``None``: the card's default route, the environment then 6), so the
    benchmark measures the ceiling it states. ("cheap": error bars are most of the cost and the
    benchmark reads only the fast-pass record.)"""
    from isar.session import scan_delivery
    return scan_delivery.fit_one(context_of(obs), _card(ceiling, settings), prior=None, M=1, seed=int(seed),
                                 errors_tier=errors_tier)


# --------------------------------------------------------------------------- one spectrum
@dataclass
class CaseResult:
    case_id: str = ""
    stream: int = 0
    index: int = 0
    draw: int = 0
    draw_stream: int = 0
    kind: str = ""
    level: str = ""
    resolution: str = ""
    n_true: int = 0
    status: str = "ok"                 # ok | fit_error | no_answer | error
    state: str = ""                    # the engine's state, verbatim
    preferred_count: int | None = None
    p_preferred: float | None = None
    n_confident: int | None = None
    n_tentative: int | None = None
    n_flags: int | None = None
    flags: str = ""
    flag_hit: bool | None = None       # planted shoulder / broad / soft mode covered by a flag
    false_flag: bool | None = None     # any flag on a clean spectrum
    seconds: float | None = None       # fast_pass_timing.total_seconds
    wall_seconds: float = 0.0          # around the whole fit call
    note: str = ""
    # not in cases.csv:
    truth: CaseTruth | None = None
    record: dict | None = None
    timing: dict | None = None
    true_peaks: list = field(default_factory=list)
    fitted_peaks: list = field(default_factory=list)


CASE_CSV_SKIP = ("truth", "record", "timing", "true_peaks", "fitted_peaks")


def run_case(args) -> CaseResult:
    """Everything for one spectrum; top-level and picklable so a pool worker can run it. Never raises:
    a failure becomes a row that stays in every denominator."""
    truth, draw, base_seed, draw_stream, errors_tier, settings = args
    res = CaseResult(case_id=truth.case_id, stream=truth.stream, index=truth.index, draw=draw,
                     draw_stream=draw_stream, kind=truth.kind, level=truth.level,
                     resolution=truth.resolution, n_true=truth.n_modes, truth=truth)
    t0 = time.monotonic()
    record = None
    try:
        grades = visibility_grades(truth)
        obs = observe(truth, draw, base_seed, draw_stream)
        seed = int(rng_for(base_seed, draw_stream, truth.stream, truth.index, draw, 1).integers(2 ** 31))
        try:
            fit = fit_observation(obs, seed=seed, errors_tier=errors_tier, ceiling=truth.ceiling,
                                  settings=settings)
        except Exception as exc:  # noqa: BLE001
            log.exception("fit failed for %s draw %d", truth.case_id, draw)
            res.status, res.note = "fit_error", f"{type(exc).__name__}: {exc}"
        else:
            record, res.timing = fit.get("fast_pass"), fit.get("fast_pass_timing")
            if not record:
                record, res.status = None, "no_answer"
    except Exception as exc:  # noqa: BLE001 - the truth's own grading failed
        log.exception("scoring setup failed for %s", truth.case_id)
        res.status, res.note = "error", f"{type(exc).__name__}: {exc}"
        res.wall_seconds = time.monotonic() - t0
        return res
    res.wall_seconds, res.record = time.monotonic() - t0, record
    res.true_peaks, res.fitted_peaks = score_case(truth, record, res.status, grades)
    flags = (record or {}).get("flags") or []
    if record:
        peaks, probs = record.get("peaks") or [], record.get("count_probabilities") or {}
        res.state, res.preferred_count = record["state"], record.get("preferred_count")
        res.p_preferred = probs.get(str(res.preferred_count))
        res.n_confident = sum(bool(p["confident"]) for p in peaks)
        res.n_tentative = len(peaks) - res.n_confident
    res.n_flags, res.flags = len(flags), flags_text(flags)
    res.flag_hit = flag_hit(truth, flags)
    res.false_flag = bool(flags) if truth.kind == "clean" else None
    res.seconds = (res.timing or {}).get("total_seconds")
    return res


# --------------------------------------------------------------------------- aggregation
def wilson(k: int, n: int, z: float = 1.96) -> tuple:
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (max(0.0, centre - half), min(1.0, centre + half))


def _figure(name: str, level: str, num: int, den: int, target: float | None, note: str) -> dict:
    value = num / den if den else None
    lo, hi = wilson(num, den)
    return {"name": name, "level": level, "numerator": num, "denominator": den, "value": value,
            "wilson95": [lo, hi] if den else None, "target": target,
            "target_reached": None if value is None or target is None else value <= target,
            "note": note}


STATES = ("resolved", "close_call", "unresolved", "search_limited")


def summarize(results) -> dict:
    """The figures, for all spectra and split by level. Targets are reported, never enforced."""
    res = list(results)
    figures, times = [], {}
    for level in ("all", "high", "low"):
        sel = [r for r in res if level == "all" or r.level == level]
        find = [p for r in sel for p in r.true_peaks if p["should_find"]]
        confident = [p for r in sel for p in r.fitted_peaks if p["confident"]]
        planted = [r for r in sel if r.flag_hit is not None]
        clean = [r for r in sel if r.false_flag is not None]
        figures += [
            _figure("missed_share", level, sum(p["outcome"] == "missed" for p in find), len(find),
                    TARGET_MISSED, "missed / should-find; a fit error or no answer is tentative_or_abstained"),
            _figure("false_share", level, sum(p["false"] for p in confident), len(confident), TARGET_FALSE,
                    "confident fitted peaks with no true peak within one FWHM / confident fitted peaks"),
            _figure("flag_hit_rate", level, sum(bool(r.flag_hit) for r in planted), len(planted), None,
                    "planted shoulder, broad_low and soft cases: a flag covers the planted omega0"),
            _figure("false_flag_rate_clean", level, sum(bool(r.false_flag) for r in clean), len(clean), None,
                    "clean spectra with any flag"),
        ]
        figures += [_figure(f"state_{s}", level, sum(r.state == s for r in sel), len(sel), None,
                            "share of spectra") for s in STATES]
        figures.append(_figure("state_no_record", level, sum(not r.state for r in sel), len(sel), None,
                               "fit error, no answer or scoring error"))
        t = np.array([r.seconds for r in sel if r.seconds is not None], dtype=float)
        times[level] = {"n": int(t.size), "median_s": float(np.median(t)) if t.size else None,
                        "p90_s": float(np.percentile(t, 90)) if t.size else None}
    return {"figures": figures, "time": times, "n_spectra": len(res),
            "n_failed_fits": sum(r.status != "ok" for r in res)}


# --------------------------------------------------------------------------- running
@dataclass
class RunInfo:
    wall_clock_s: float = 0.0
    n_requested: int = 0
    n_completed: int = 0
    stopped_by_deadline: bool = False
    killed_in_flight: int = 0


def plan_tasks(mode: str, base_seed: int, ceiling: int | None = None, *, named_draws: int | None = None,
               random: int | None = None, clean: int | None = None) -> tuple:
    """``([(truth, draw)], draw_stream)`` for a mode: the named truths with several draws, random
    truths with one, and extra clean spectra for the false-flag rate. The mode alone picks the streams."""
    plan = MODE_PLANS[mode]
    ceiling = mode_ceiling(ceiling)
    n_named = plan["named_draws"] if named_draws is None else named_draws
    n_random = plan["random"] if random is None else random
    n_clean = plan["clean"] if clean is None else clean
    tasks = [(t, d) for t in named_cases(base_seed) for d in range(n_named)]
    tasks += [(generate_case(base_seed, plan["case_stream"], i, ceiling), 0) for i in range(n_random)]
    tasks += [(generate_case(base_seed, plan["case_stream"], CLEAN_INDEX0 + i, ceiling, kind="clean"), 0)
              for i in range(n_clean)]
    return tasks, plan["draw_stream"]


def run_benchmark(tasks, *, base_seed: int, n_workers: int, draw_stream: int = STREAM_DRAW_EVAL,
                  errors_tier: str = "cheap", settings: dict | None = None,
                  deadline_s: float | None = None) -> tuple:
    """Run ``tasks`` (``[(truth, draw)]``) on ``n_workers`` processes (1 = in this process).
    ``deadline_s`` is the wall-clock cap: no case starts in its last grace seconds
    (:data:`HARD_KILL_GRACE_S`, at most half the cap), and workers still running are killed at the cap;
    the cases that finished are reported. The in-process path can only check between cases.
    Returns (results sorted by case, RunInfo)."""
    if n_workers < 1:
        raise ValueError("n_workers must be >= 1")
    t0 = time.monotonic()
    info = RunInfo(n_requested=len(tasks))
    jobs = [(t, d, base_seed, draw_stream, errors_tier, settings) for t, d in tasks]
    # shortcut: a cap under two grace periods gets half the cap as grace, so it still starts cases
    grace = 0.0 if deadline_s is None else min(HARD_KILL_GRACE_S, deadline_s / 2)
    elapsed = lambda: time.monotonic() - t0   # noqa: E731
    closed = lambda: deadline_s is not None and elapsed() >= deadline_s - grace   # noqa: E731
    results: list = []
    if n_workers == 1:
        for job in jobs:
            if closed():
                break
            results.append(run_case(job))
    else:
        ex = ProcessPoolExecutor(max_workers=n_workers)
        pending, queue = set(), iter(jobs)
        try:
            while True:
                while len(pending) < n_workers and not closed():
                    job = next(queue, None)
                    if job is None:
                        break
                    pending.add(ex.submit(run_case, job))
                if not pending:
                    break
                done, pending = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
                results += [f.result() for f in done]
                if deadline_s is not None and elapsed() >= deadline_s:
                    info.killed_in_flight = len(pending)
                    for proc in list(getattr(ex, "_processes", {}).values()):
                        proc.terminate()
                    break
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
    info.stopped_by_deadline = len(results) < len(jobs)
    info.n_completed, info.wall_clock_s = len(results), elapsed()
    results.sort(key=lambda r: (r.stream, r.index, r.draw))
    return results, info


def _write_csv(path: Path, rows: list, names: list) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=names)
        w.writeheader()
        for row in rows:
            w.writerow({k: ("" if row.get(k) is None else row[k]) for k in names})


def write_outputs(folder: Path, results, summary: dict, meta: dict) -> dict:
    """``figures.json``, ``cases.csv``, ``true_peaks.csv``, ``fitted_peaks.csv`` and ``records.jsonl``
    in ``folder`` (the gallery is drawn separately); returns their paths by name."""
    folder.mkdir(parents=True, exist_ok=True)
    paths = {n: folder / n for n in ("figures.json", "cases.csv", "true_peaks.csv", "fitted_peaks.csv",
                                     "records.jsonl")}
    paths["figures.json"].write_text(json.dumps({"meta": meta, "summary": summary}, indent=2, default=str),
                                     encoding="utf-8", newline="\n")
    names = [f.name for f in fields(CaseResult) if f.name not in CASE_CSV_SKIP]
    _write_csv(paths["cases.csv"], [asdict_shallow(r) for r in results], names)
    key = {"case": None, "draw": None}
    for fname, attr, cols in (
            ("true_peaks.csv", "true_peaks", ["index", "omega0", "gamma", "height", "grade", "refit",
                                             "should_find", "outcome", "status", "matched_fit",
                                             "abs_delta_omega0"]),
            ("fitted_peaks.csv", "fitted_peaks", ["index", "omega0", "confident", "delta", "matched_true",
                                                  "false"])):
        rows = [{**row, "case": r.case_id, "draw": r.draw} for r in results for row in getattr(r, attr)]
        _write_csv(paths[fname], rows, [*key, *cols])
    with open(paths["records.jsonl"], "w", encoding="utf-8", newline="\n") as fh:
        for r in results:
            line = {"case": r.case_id, "draw": r.draw,
                    "truth": asdict(r.truth) if r.truth is not None else None,
                    "fast_pass": r.record, "timing": r.timing}
            if r.record is None:
                line["status"] = r.status
            fh.write(json.dumps(line, default=str) + "\n")
    return paths


def asdict_shallow(r: CaseResult) -> dict:
    """The CaseResult's own fields without ``asdict``'s recursive copy of the record."""
    return {f.name: getattr(r, f.name) for f in fields(r)}


# --------------------------------------------------------------------------- command line
def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m isar.synth.count_bench",
        description="Fast-pass peak benchmark: figures, per-spectrum and per-peak tables, the "
                    "fast-pass records and galleries in an output folder. Targets are reported, never gates.")
    p.add_argument("--out", type=Path, default=None,
                   help="output folder (default: out/count_bench/<timestamp> under the repo)")
    p.add_argument("--mode", choices=tuple(MODE_PLANS), default="evaluation",
                   help="development tunes thresholds; evaluation reports figures on streams that "
                        "development never saw")
    p.add_argument("--n-workers", type=int, default=None,
                   help=f"worker processes (default: min({TUNING_WORKERS}, cpu-2))")
    p.add_argument("--named-draws", type=int, default=None, help="draws per named truth (default by mode)")
    p.add_argument("--random", type=int, default=None, help="random-suite spectra (default by mode)")
    p.add_argument("--clean", type=int, default=None, help="extra clean spectra (default by mode)")
    p.add_argument("--seed", type=int, default=20261023, help="base seed")
    p.add_argument("--ceiling", type=int, default=None,
                   help=f"largest mode count generated and fitted (default: ${MODE_CEILING_ENV}, else "
                        f"{DEFAULT_MODE_CEILING})")
    p.add_argument("--settings", default=None,
                   help='fast-pass and prior settings: inline JSON or a JSON file, '
                        '{"fast_pass": {...}, "prior": {...}}, written into the bench card')
    p.add_argument("--errors-tier", default="cheap", choices=("cheap", "fast", "auto", "full"))
    p.add_argument("--max-seconds", type=float, default=None,
                   help="wall-clock cap: no case starts in its last "
                        f"{int(HARD_KILL_GRACE_S)} s, and workers still running are killed at the cap")
    return p


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ.setdefault(var, "1")     # one thread per fit worker, before the pool starts
    t0 = time.monotonic()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = Path(args.out) if args.out else config.OUT_DIR / "count_bench" / stamp
    folder.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.FileHandler(folder / "count_bench.log", encoding="utf-8"),
                                  logging.StreamHandler(sys.stderr)])
    settings = load_settings(args.settings)
    ceiling = mode_ceiling(args.ceiling)
    n_workers = args.n_workers or max(1, min(TUNING_WORKERS, (os.cpu_count() or 2) - 2))
    tasks, draw_stream = plan_tasks(args.mode, args.seed, ceiling, named_draws=args.named_draws,
                                    random=args.random, clean=args.clean)
    print(f"count_bench: {args.mode}, {len(tasks)} spectra, ceiling {ceiling}, {n_workers} workers")
    results, info = run_benchmark(tasks, base_seed=args.seed, n_workers=n_workers, draw_stream=draw_stream,
                                  errors_tier=args.errors_tier, settings=settings,
                                  deadline_s=args.max_seconds)
    summary = summarize(results)
    meta = {"mode": args.mode, "base_seed": args.seed, "ceiling": ceiling, "n_workers": n_workers,
            "case_stream": MODE_PLANS[args.mode]["case_stream"], "draw_stream": draw_stream,
            "errors_tier": args.errors_tier, "run": asdict(info),
            **settings_meta(ceiling, settings),
            "targets_are_not_gates": "ruling R15: reported, never enforced"}
    write_outputs(folder, results, summary, meta)
    for fig in summary["figures"]:
        if fig["level"] == "all" and not fig["name"].startswith("state_"):
            value = "n/a" if fig["value"] is None else f"{fig['value']:.3f}"
            aim = "" if fig["target"] is None else f" (aim <= {fig['target']})"
            print(f"  {fig['name']}: {fig['numerator']}/{fig['denominator']} = {value}{aim}")
    print(f"  time per spectrum: median {summary['time']['all']['median_s']} s, "
          f"p90 {summary['time']['all']['p90_s']} s; failed fits {summary['n_failed_fits']}")
    print(f"Output folder: {folder}")
    print(f"Wall clock: {time.monotonic() - t0:.1f} s ({info.n_completed}/{info.n_requested} spectra"
          f"{', stopped at the deadline' if info.stopped_by_deadline else ''}"
          f"{f', {info.killed_in_flight} killed in flight' if info.killed_in_flight else ''})")
    return 0 if info.n_completed else 2


if __name__ == "__main__":
    sys.exit(main())

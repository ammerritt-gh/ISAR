"""Peak-count benchmark: how many modes does a spectrum hold, and does the fitter say so?

A seeded generator draws spectra with 0..ceiling modes (hard cases over-represented), the
production fit path (:func:`isar.session.scan_delivery.fit_one`, on a synthetic
:class:`~isar.analyze.context.ScanContext`) fits one fresh Poisson draw of each, and three figures
are scored against the generator's truth, which the harness holds and never passes to the fitter
(the fitter sees only an :class:`Observation`):

1. correct counts on *resolvable* cases (an ambiguous answer is not correct; it is reported as an
   abstention and stays in the denominator);
2. confident extra modes on single-mode and empty spectra;
3. the A/B figure: per resolvable case, fresh draws from the truth curve and from the fitted
   curve, the likelihood ratio picks the source; the share of resolvable cases at <=60%.

*Resolvable* ("a human could fit it"): over repeated Poisson draws of one truth, a high-budget
reference tells N modes from N-1 in >=90% of draws, while wrongly splitting the truth with its
weakest mode removed in <=5% of draws. The reference is its own fixed configuration (many starts,
some placed at the true mode positions, which the fitter under test never sees; nested sampling on
the N and N-1 models through :func:`isar.analyze.errors.bayes.bayesian_posterior`, the lane the
evidence ladder escalates to), independent of any escalation threshold of the fitter.

Every figure is a *target*, never a gate (rulings R14/R15): the figures file reports what was
reached, with its denominator and Monte Carlo uncertainty.

Run the full characterisation deliberately: ``python -m isar.synth.count_bench --help`` or
double-click ``run-isar-count-bench.bat``. The routine suite only runs a tiny subset
(``isar/synth/tests/test_count_bench.py``).
"""
from __future__ import annotations

import argparse
import csv
import itertools
import json
import logging
import math
import os
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy.optimize import nnls

from . import config, noise

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- named constants
# The mode ceiling: a sibling unit owns the production resolution (card [fitting] max_modes ->
# this environment variable -> 6). The generator takes the ceiling as an explicit argument and
# reads only the environment/default here, so it imports nothing that does not exist yet.
MODE_CEILING_ENV = "ISAR_MODE_CEILING"
DEFAULT_MODE_CEILING = 6

# Independent random streams: every draw is seeded from (base_seed, stream, keys...), so the
# tuning cases, the evaluation cases and the four kinds of Poisson draw never share numbers.
STREAM_CASE_TUNING = 1       # case parameters of the cases S1U4 tunes thresholds on
STREAM_CASE_EVAL = 2         # case parameters of the evaluation cases
STREAM_DRAW_FIT = 3          # the one fresh draw the fitter under test sees
STREAM_DRAW_REFERENCE = 4    # the reference's draws of the truth
STREAM_DRAW_NULL = 5         # the reference's draws of the truth minus its weakest mode
STREAM_DRAW_AB = 6           # the A/B test's fresh draws
STREAM_REF_SEARCH = 7        # random starts and sampler seeds of the reference
STREAMS = (STREAM_CASE_TUNING, STREAM_CASE_EVAL, STREAM_DRAW_FIT, STREAM_DRAW_REFERENCE,
           STREAM_DRAW_NULL, STREAM_DRAW_AB, STREAM_REF_SEARCH)

# Resolvability (ruling R5) and the three targets (R5, R14, R15: reported, never gates).
RESOLVE_DRAWS = 200          # Poisson draws per truth for the resolvability classifier
RESOLVE_DETECT_MIN = 0.90    # the extra mode must be found in at least this share of draws
FALSE_SPLIT_MAX = 0.05       # the single mode may be wrongly split in at most this share
AB_DRAWS = 1000              # fresh draws per source (truth, fit) in the A/B test
TARGET_CORRECT = 0.90
TARGET_EXTRA = 0.05
TARGET_AB = 0.60             # the best test may pick the source right at most this often

# The tuning run S1U4 uses is capped (R15): reached figures are recorded, not tuned further.
TUNING_MAX_CASES = 60
TUNING_MAX_SECONDS = 30 * 60
TUNING_WORKERS = 16
HARD_KILL_GRACE_S = 300.0    # no case starts in the last this many seconds before the wall-clock cap

# A fit answer is "ambiguous" when its runner-up count carries at least this posterior share. A
# stand-in for the flag the fitter will publish: a ``count_ambiguous`` key in the fit wins.
AMBIGUOUS_RUNNER_UP_P = 0.20

# The reference: N modes beat N-1 when ln Z_N - ln Z_{N-1} exceeds this (equal prior weight on the
# two counts, as the fitter's ladder uses). Its own flat prior box, fixed and declared here, so the
# reference does not move when the fitter's prior recipe does; amplitudes are boxed relative to the
# observed peak count (the reference may look at the data; the fitter's recipe may not).
REF_LN_BAYES_MIN = 0.0
REF_AMPLITUDE_FACTOR = 100.0     # intrinsic phonon amplitude A in [0, factor * max counts]
REF_ELASTIC_FACTOR = 20.0        # elastic area E in [0, factor * max counts]
REF_MAX_SUBSET_STARTS = 8        # cap on truth-position starts for an N-1 sub-model
REF_EVIDENCE_KINDS = ("laplace", "nested")   # laplace: production Gaussian rungs, no sampler

# Generator: the instrument window is the BL43LXU-like one (about -16..+20 meV); the resolution is
# a generic Voigt of a few tenths-to-two meV, not any measured curve.
X_MIN, X_MAX, X_STEP = -16.0, 20.0, 0.5
KIND_WEIGHTS = {            # hard cases deliberately over-represented
    "empty": 0.08, "single": 0.12, "broad_single": 0.10, "close_doublet": 0.20,
    "weak_on_tail": 0.14, "near_elastic": 0.12, "generic": 0.24,
}
NEEDS_TWO_MODES = ("close_doublet", "weak_on_tail", "generic")


# --------------------------------------------------------------------------- seeds and ceiling
def mode_ceiling(explicit: int | None = None) -> int:
    """The generator's maximum mode count: explicit argument, else ``ISAR_MODE_CEILING``, else 6."""
    if explicit is not None:
        value = int(explicit)
    else:
        raw = os.environ.get(MODE_CEILING_ENV)
        if raw is None or raw == "":
            return DEFAULT_MODE_CEILING
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError(f"{MODE_CEILING_ENV}={raw!r} is not an integer") from exc
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
    omega0: float        # intrinsic mode energy (meV)
    gamma: float         # DHO damping (meV), < omega0
    height: float        # Stokes+anti-Stokes peak height above background at exposure 1 (counts)


@dataclass(frozen=True)
class CaseTruth:
    """Everything the generator knows. Held by the harness; the fitter never receives it."""

    stream: int          # which case stream made it (STREAM_CASE_TUNING / STREAM_CASE_EVAL)
    index: int
    kind: str
    modes: tuple
    elastic_height: float
    background: float
    T: float
    exposure: float
    gauss_fwhm: float
    lorentz_fwhm: float
    ceiling: int

    @property
    def case_id(self) -> str:
        return f"{'T' if self.stream == STREAM_CASE_TUNING else 'E'}{self.index:04d}"

    @property
    def n_modes(self) -> int:
        return len(self.modes)


def _res_fwhm(gauss: float, lorentz: float) -> float:
    """Voigt FWHM (Olivero-Longbothum approximation); only used to place hard cases."""
    return 0.5346 * lorentz + math.sqrt(0.2166 * lorentz ** 2 + gauss ** 2)


def _loguniform(rng, lo: float, hi: float) -> float:
    return float(math.exp(rng.uniform(math.log(lo), math.log(hi))))


def _mode(omega0: float, gamma: float, rel: float) -> tuple:
    """A mode that is underdamped (omega0 > gamma) and inside the window."""
    omega0 = float(min(max(omega0, 0.4), X_MAX - 1.0))
    return omega0, float(min(gamma, omega0 / 1.2)), float(rel)


def _draw_modes(kind: str, rng, res: float, ceiling: int) -> list:
    """``[(omega0, gamma, relative_height)]`` for one case kind; the strongest has height 1."""
    gam = lambda: _loguniform(rng, 0.15, 1.5)   # noqa: E731 - three uses below
    if kind == "empty":
        return []
    if kind == "single":
        return [_mode(rng.uniform(1.5, 18.0), gam(), 1.0)]
    if kind == "broad_single":
        return [_mode(rng.uniform(4.0, 16.0), rng.uniform(1.5, 4.0), 1.0)]
    if kind == "close_doublet":
        w1, sep = rng.uniform(3.0, 15.0), rng.uniform(0.5, 2.0) * res
        g = gam()
        return [_mode(w1, g, 1.0), _mode(w1 + sep, g * rng.uniform(0.7, 1.4), rng.uniform(0.3, 1.0))]
    if kind == "weak_on_tail":
        w1 = rng.uniform(4.0, 14.0)
        off = rng.choice([-1.0, 1.0]) * rng.uniform(1.5, 4.0) * res
        return [_mode(w1, gam(), 1.0), _mode(w1 + off, gam(), _loguniform(rng, 0.03, 0.15))]
    if kind == "near_elastic":
        modes = [_mode(rng.uniform(0.7, 2.5) * res, _loguniform(rng, 0.15, 0.6), 1.0)]
        if ceiling >= 2 and rng.random() < 0.5:
            modes.append(_mode(rng.uniform(5.0, 17.0), gam(), rng.uniform(0.2, 1.0)))
        return modes
    n = int(rng.integers(2, ceiling + 1))      # generic: 2..ceiling modes, spaced >= 0.3 res
    for _ in range(200):
        energies = np.sort(rng.uniform(1.0, X_MAX - 1.0, size=n))
        if n == 1 or np.min(np.diff(energies)) >= 0.3 * res:
            break
    heights = [_loguniform(rng, 0.15, 1.0) for _ in range(n)]
    heights[int(rng.integers(n))] = 1.0
    return [_mode(w, gam(), h) for w, h in zip(energies, heights)]


def generate_case(base_seed: int, stream: int, index: int, ceiling: int | None = None) -> CaseTruth:
    """One seeded case. ``ceiling`` is the largest mode count the generator may draw."""
    ceiling = mode_ceiling(ceiling)
    rng = rng_for(base_seed, stream, index)
    gauss, lorentz = float(rng.uniform(1.0, 2.0)), float(rng.uniform(0.2, 0.8))
    res = _res_fwhm(gauss, lorentz)
    kinds = list(KIND_WEIGHTS)
    kind = str(rng.choice(kinds, p=np.array(list(KIND_WEIGHTS.values())) / sum(KIND_WEIGHTS.values())))
    if ceiling < 2 and kind in NEEDS_TWO_MODES:
        kind = "single"
    if ceiling < 1:
        kind = "empty"
    drawn = _draw_modes(kind, rng, res, ceiling)[:ceiling]
    scale = _loguniform(rng, 30.0, 1500.0)           # strongest mode height at exposure 1
    modes = tuple(ModeTruth(w, g, scale * rel) for w, g, rel in drawn)
    elastic_ratio = _loguniform(rng, 0.3, 30.0) * (3.0 if kind == "near_elastic" else 1.0)
    return CaseTruth(
        stream=int(stream), index=int(index), kind=kind, modes=modes,
        elastic_height=scale * elastic_ratio,
        background=max(scale * _loguniform(rng, 0.003, 0.08), 0.05),
        T=_loguniform(rng, 80.0, 320.0), exposure=_loguniform(rng, 0.5, 4.0),
        gauss_fwhm=gauss, lorentz_fwhm=lorentz, ceiling=int(ceiling))


def null_truth(truth: CaseTruth) -> CaseTruth:
    """The truth with its weakest mode removed: the "single mode" a false split would come from."""
    weakest = min(range(truth.n_modes), key=lambda i: truth.modes[i].height)
    return replace(truth, kind=f"{truth.kind}:null",
                   modes=tuple(m for i, m in enumerate(truth.modes) if i != weakest))


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
    exposure: float      # the declared exposure (a monitor, as a real scan records it)


def context_of(obs: Observation):
    from isar.analyze.context import ScanContext
    # A monitor-normalised spectrum: relative scale 1 everywhere, reference the declared exposure.
    # exposure_source is a sibling unit's field; set only where this base has it.
    extra = {"exposure_source": "monitor"} if "exposure_source" in {f.name for f in fields(ScanContext)} else {}
    return ScanContext(x=obs.x, counts=obs.counts, mask=np.ones_like(obs.x, dtype=bool), T=obs.T,
                       center=0.0, gauss_fwhm=obs.gauss_fwhm, lorentz_fwhm=obs.lorentz_fwhm,
                       instrument="COUNT_BENCH", energy_status="absolute",
                       exposure_scale=np.ones_like(obs.x, dtype=float), exposure_reference=obs.exposure,
                       **extra)


def _prefix(i: int) -> str:
    return "phonon" if i == 0 else f"phonon{i + 1}"


def _truth_model(truth: CaseTruth):
    """The production composite for the truth (anti-Stokes partner derived inside PhononDHO by
    detailed balance, never a parameter), its parameter vector, and a context to evaluate on."""
    from isar.analyze.components import CompositeModel, ConstantBackground, Elastic, PhononDHO
    from isar.analyze.lineshapes import voigt_area_for_height
    from isar.analyze.phonon import omega_d_from

    x = energy_axis()
    ctx = context_of(Observation(x, np.zeros_like(x), truth.T, truth.gauss_fwhm, truth.lorentz_fwhm,
                                truth.exposure))
    phonons = [PhononDHO(prefix=_prefix(i)) for i in range(truth.n_modes)]
    xf = np.arange(X_MIN, X_MAX + 1e-9, 0.05)
    theta = []
    for comp, m in zip(phonons, truth.modes):
        od = omega_d_from(m.omega0, m.gamma)
        unit_height = float(np.max(comp.evaluate(xf, {"Omega_d": od, "Gamma": m.gamma, "A": 1.0}, ctx)))
        theta += [od, m.gamma, m.height / unit_height]
    theta += [voigt_area_for_height(truth.elastic_height, truth.gauss_fwhm, truth.lorentz_fwhm),
              truth.background]
    return CompositeModel([*phonons, Elastic(), ConstantBackground()]), theta, ctx


def truth_lambda(truth: CaseTruth) -> np.ndarray:
    """Noise-free expected counts of the truth on the scan grid (exposure included)."""
    comp, theta, ctx = _truth_model(truth)
    return np.clip(truth.exposure * comp.evaluate(ctx.x, theta, ctx), 0.0, None)


def draw_observation(truth: CaseTruth, rng: np.random.Generator) -> Observation:
    """One Poisson realisation (sigma = sqrt(N), 0 at zero counts: no floor is applied anywhere)."""
    return Observation(x=energy_axis(), counts=rng.poisson(truth_lambda(truth)).astype(float),
                       T=truth.T, gauss_fwhm=truth.gauss_fwhm, lorentz_fwhm=truth.lorentz_fwhm,
                       exposure=truth.exposure)


# --------------------------------------------------------------------------- the fitter under test
@dataclass(frozen=True)
class CountAnswer:
    n_modes: int | None          # None when the fit produced no answer
    ambiguous: bool
    runner_up_p: float | None


def _card():
    # The one synthetic card the STAT-2b campaign already builds; reused, not re-declared.
    from isar.synth.stat2b.campaign import _campaign_card
    return _campaign_card()


def fit_observation(obs: Observation, *, seed: int, errors_tier: str = "cheap") -> dict:
    """The production fit of one observation: ``scan_delivery.fit_one`` on a synthetic context.
    ("cheap": the count decision is made before any error bars, and those are most of the cost.)"""
    from isar.session import scan_delivery
    return scan_delivery.fit_one(context_of(obs), _card(), prior=None, M=1, seed=int(seed),
                                 errors_tier=errors_tier)


def count_answer(fit: dict) -> CountAnswer:
    """The count a fit result states, and whether it says the answer is ambiguous."""
    if not fit.get("best_label"):
        return CountAnswer(None, False, None)
    share: dict = {}
    for row in fit.get("candidates") or ():
        p = row.get("posterior_probability")
        p = row.get("akaike_weight") if p is None else p
        if p is not None and np.isfinite(p):
            share[int(row["n_modes"])] = share.get(int(row["n_modes"]), 0.0) + float(p)
    total = sum(share.values())
    others = sorted((p / total for n, p in share.items() if n != int(fit["n_modes"])),
                    reverse=True) if total > 0 else []
    runner = others[0] if others else 0.0
    ambiguous = bool(fit.get("count_ambiguous", runner >= AMBIGUOUS_RUNNER_UP_P))
    return CountAnswer(int(fit["n_modes"]), ambiguous, float(runner))


def fitted_lambda(fit: dict, obs: Observation) -> np.ndarray | None:
    """The selected fit's expected counts on the observation grid, rebuilt from its manifest."""
    manifest = fit.get("model_manifest")
    if not manifest:
        return None
    from isar.analyze.model_manifest import compose_manifest
    comp, theta = compose_manifest(manifest)
    return np.clip(comp.evaluate(obs.x, theta, context_of(obs)), 0.0, None)


# --------------------------------------------------------------------------- the reference
@dataclass(frozen=True)
class ReferenceBudget:
    """The reference's cost knobs; the full run uses the design's 200 draws, the fast subset shrinks them."""

    draws: int = RESOLVE_DRAWS          # draws of the truth
    null_draws: int = RESOLVE_DRAWS     # draws of the truth minus its weakest mode
    evidence: str = "laplace"           # "laplace" (production Gaussian rungs) | "nested" (sampler)
    random_starts: int = 8              # random-start fits per model, beside the truth-placed ones
    n_live: int = 100                   # nested-sampling live points (evidence="nested" only)
    dlogz: float = 0.5
    max_iter: int | None = 40_000

    def __post_init__(self):
        if self.evidence not in REF_EVIDENCE_KINDS:
            raise ValueError(f"reference evidence must be one of {REF_EVIDENCE_KINDS}, got {self.evidence!r}")


def _reference_hypothesis(m: int):
    from isar.analyze.components import ConstantBackground, Elastic, PhononDHO
    from isar.analyze.structures import Hypothesis
    return Hypothesis(f"ref{m}", (*[PhononDHO(prefix=_prefix(i)) for i in range(m)],
                                  Elastic, ConstantBackground))


def _start_vector(comp, ctx, pairs) -> np.ndarray:
    """Initial parameters for mode (omega0, gamma) ``pairs``; the linear amplitudes by NNLS."""
    from isar.analyze.phonon import EPS_OMEGA_D, GAMMA_FLOOR, GAMMA_MAX, omega_d_from
    nonlinear = {}
    for i, (w0, g) in enumerate(pairs):
        g = float(min(max(g, GAMMA_FLOOR), GAMMA_MAX))
        nonlinear[f"{_prefix(i)}.Omega_d"] = float(min(max(omega_d_from(w0, g), EPS_OMEGA_D), X_MAX))
        nonlinear[f"{_prefix(i)}.Gamma"] = g
    phi, lin_names = comp.nnls_basis(ctx.x_used, nonlinear, ctx)
    coef, _ = nnls(phi, ctx.counts_used)
    linear = dict(zip(lin_names, coef))
    return np.array([nonlinear[n] if n in nonlinear else max(float(linear[n]), 0.0)
                     for n in comp.param_names()], dtype=float)


def _reference_bundle(names, cmax: float):
    from isar.analyze.catalogue import PriorBundle, ResolvedPrior
    from isar.analyze.phonon import EPS_OMEGA_D, GAMMA_FLOOR, GAMMA_MAX
    box = {"Omega_d": (EPS_OMEGA_D, X_MAX), "Gamma": (GAMMA_FLOOR, GAMMA_MAX),
           "A": (0.0, REF_AMPLITUDE_FACTOR * cmax), "E": (0.0, REF_ELASTIC_FACTOR * cmax),
           "BG": (0.0, cmax + 10.0)}
    return PriorBundle(tuple(ResolvedPrior(n, "flat", None, None, *box[n.split(".")[-1]], "identity")
                             for n in names))


def reference_ln_evidence(obs: Observation, m: int, positions, budget: ReferenceBudget,
                          rng: np.random.Generator) -> float | None:
    """ln Z of the m-mode model by nested sampling, or None when it could not be computed.

    ``positions`` are the true mode (omega0, gamma) pairs: starts are placed at them (every
    m-subset, capped), which only the reference may do. The best converged start defines the
    problem for the evidence; with ``evidence="nested"`` the sampler explores the whole prior box.
    A failed calculation is None, never a low evidence."""
    from isar.analyze import loop

    ctx, hyp = context_of(obs), _reference_hypothesis(m)
    comp = hyp.build()
    subsets = list(itertools.combinations(positions, m))
    if len(subsets) > REF_MAX_SUBSET_STARTS:
        subsets = [subsets[i] for i in rng.choice(len(subsets), REF_MAX_SUBSET_STARTS, replace=False)]
    starts = [list(s) for s in subsets]
    for _ in range(budget.random_starts if m else 0):
        starts.append([(rng.uniform(0.5, X_MAX - 1.0), _loguniform(rng, 0.15, 2.0)) for _ in range(m)])
    best = None
    for pairs in starts or [[]]:
        try:
            fit = loop.fit_hypothesis(hyp, ctx, x0=_start_vector(comp, ctx, pairs))
        except Exception as exc:  # noqa: BLE001 - one bad start must not end the reference
            log.warning("reference start failed (m=%d): %s: %s", m, type(exc).__name__, exc)
            continue
        if fit.optimizer_converged and (best is None or fit.cost < best.cost):
            best = fit
    if best is None:
        return None
    bundle = _reference_bundle(comp.param_names(), max(float(np.max(obs.counts)), 1.0))
    if budget.evidence == "laplace":
        # escalation="defer": the Gaussian rungs only. An invalid rung is NaN, i.e. None here.
        from isar.analyze.evidence import structure_evidence
        ln_z = structure_evidence(hyp, ctx, best, bundle, escalation="defer").log_evidence
        return float(ln_z) if np.isfinite(ln_z) else None
    from isar.analyze.errors.bayes import bayesian_posterior
    bz = bayesian_posterior(hyp, ctx, best, prior_bundle=bundle, seed=int(rng.integers(2 ** 31)),
                            n_live=budget.n_live, dlogz=budget.dlogz, max_iter=budget.max_iter)
    return float(bz.log_evidence) if bz.converged and np.isfinite(bz.log_evidence) else None


@dataclass
class Resolvability:
    label: str                      # resolvable | borderline | not_applicable
    n_modes: int
    detect_found: int = 0
    detect_draws: int = 0
    detect_indeterminate: int = 0
    split_found: int = 0
    split_draws: int = 0
    split_indeterminate: int = 0
    seconds: float = 0.0


def _prefers_n(data_truth: CaseTruth, positions, n: int, key: int, stream: int, base_seed: int,
               budget: ReferenceBudget) -> bool | None:
    """One reference draw: does ln Z_N beat ln Z_{N-1}? None when either evidence is missing."""
    rng = rng_for(base_seed, stream, data_truth.stream, data_truth.index, key)
    obs = draw_observation(data_truth, rng)
    search = rng_for(base_seed, STREAM_REF_SEARCH, data_truth.stream, data_truth.index, key, stream)
    lnz_n = reference_ln_evidence(obs, n, positions, budget, search)
    lnz_lo = reference_ln_evidence(obs, n - 1, positions, budget, search)
    if lnz_n is None or lnz_lo is None:
        return None
    return lnz_n - lnz_lo > REF_LN_BAYES_MIN


def classify_resolvability(truth: CaseTruth, base_seed: int, budget: ReferenceBudget) -> Resolvability:
    """Resolvable / borderline for one truth by the reference (an exact early exit: the label is
    decided as soon as the allowed misses or false splits are exceeded, and the denominators say
    how many draws were actually used)."""
    n, t0 = truth.n_modes, time.monotonic()
    if n == 0:
        return Resolvability("not_applicable", 0)
    positions = [(m.omega0, m.gamma) for m in truth.modes]
    out = Resolvability("borderline", n)
    miss_ok = budget.draws - math.ceil(RESOLVE_DETECT_MIN * budget.draws - 1e-9)
    split_ok = math.floor(FALSE_SPLIT_MAX * budget.null_draws + 1e-9)
    misses = 0
    for k in range(budget.draws):
        out.detect_draws += 1
        verdict = _prefers_n(truth, positions, n, k, STREAM_DRAW_REFERENCE, base_seed, budget)
        out.detect_indeterminate += verdict is None
        out.detect_found += bool(verdict)
        misses += not verdict                       # an indeterminate draw is not "found"
        if misses > miss_ok:
            break
    else:
        splits, null = 0, null_truth(truth)
        for k in range(budget.null_draws):
            out.split_draws += 1
            verdict = _prefers_n(null, positions, n, k, STREAM_DRAW_NULL, base_seed, budget)
            out.split_indeterminate += verdict is None
            out.split_found += verdict is None or bool(verdict)   # unknown counts against it
            splits += verdict is None or bool(verdict)
            if splits > split_ok:
                break
        else:
            out.label = "resolvable"
    out.seconds = time.monotonic() - t0
    return out


# --------------------------------------------------------------------------- the A/B test
def ab_accuracy(lam_truth: np.ndarray, lam_fit: np.ndarray, n_draws: int,
                rng: np.random.Generator) -> tuple:
    """Accuracy of the likelihood-ratio test at telling truth draws from fit draws.

    ``n_draws`` fresh spectra from each curve; the statistic ln L(truth) - ln L(fit) of the draw
    picks "truth" when positive (an exact tie scores half). Returns (accuracy, binomial s.e.)."""
    lt, lf = np.clip(lam_truth, 1e-12, None), np.clip(lam_fit, 1e-12, None)
    log_ratio = np.log(lt / lf)
    def statistic(draws):                   # sum_i d_i ln(lt_i/lf_i) - (lt_i - lf_i)
        return draws @ log_ratio - np.sum(lt - lf)
    from_truth = statistic(rng.poisson(lt, size=(n_draws, lt.size)))
    from_fit = statistic(rng.poisson(lf, size=(n_draws, lf.size)))
    right = (np.sum(from_truth > 0) + 0.5 * np.sum(from_truth == 0)
             + np.sum(from_fit < 0) + 0.5 * np.sum(from_fit == 0))
    acc = float(right / (2 * n_draws))
    return acc, math.sqrt(acc * (1.0 - acc) / (2 * n_draws))


# --------------------------------------------------------------------------- one case
@dataclass
class CaseResult:
    case_id: str = ""
    stream: int = 0
    index: int = 0
    kind: str = ""
    n_true: int = 0
    resolvability: str = ""
    detect_found: int = 0
    detect_draws: int = 0
    detect_indeterminate: int = 0
    split_found: int = 0
    split_draws: int = 0
    split_indeterminate: int = 0
    status: str = "ok"                 # ok | fit_error | no_answer | error
    n_fit: int | None = None
    ambiguous: bool | None = None
    runner_up_p: float | None = None
    outcome: str = ""                  # correct | abstain | wrong | n/a (not resolvable)
    confident_extra: bool | None = None
    ab_accuracy: float | None = None
    ab_se: float | None = None
    ab_pass: bool | None = None
    seconds_reference: float = 0.0
    seconds_fit: float = 0.0
    note: str = ""


def run_case(args) -> CaseResult:
    """Everything for one case; top-level and picklable so a pool worker can run it. Never raises:
    a failure becomes a row that stays in every denominator."""
    truth, base_seed, budget, n_ab, errors_tier = args
    res = CaseResult(case_id=truth.case_id, stream=truth.stream, index=truth.index,
                     kind=truth.kind, n_true=truth.n_modes)
    try:
        cls = classify_resolvability(truth, base_seed, budget)
    except Exception as exc:  # noqa: BLE001
        log.exception("reference failed for %s", truth.case_id)
        res.status, res.note, res.resolvability = "error", f"reference: {type(exc).__name__}: {exc}", "unknown"
        return res
    res.resolvability, res.seconds_reference = cls.label, cls.seconds
    for name in ("detect_found", "detect_draws", "detect_indeterminate",
                 "split_found", "split_draws", "split_indeterminate"):
        setattr(res, name, getattr(cls, name))

    t0 = time.monotonic()
    obs = draw_observation(truth, rng_for(base_seed, STREAM_DRAW_FIT, truth.stream, truth.index))
    try:
        fit = fit_observation(obs, seed=truth.index, errors_tier=errors_tier)
        answer = count_answer(fit)
    except Exception as exc:  # noqa: BLE001
        log.exception("fit failed for %s", truth.case_id)
        res.status, res.note = "fit_error", f"{type(exc).__name__}: {exc}"
        res.seconds_fit = time.monotonic() - t0
        return res
    res.seconds_fit = time.monotonic() - t0
    if answer.n_modes is None:
        res.status = "no_answer"
    else:
        res.n_fit, res.ambiguous, res.runner_up_p = answer.n_modes, answer.ambiguous, answer.runner_up_p
        if truth.n_modes <= 1:
            res.confident_extra = answer.n_modes > truth.n_modes and not answer.ambiguous
    if res.resolvability == "resolvable":
        res.outcome = ("abstain" if answer.ambiguous else
                       "correct" if answer.n_modes == truth.n_modes else "wrong")
        lam_fit = fitted_lambda(fit, obs) if answer.n_modes is not None else None
        if lam_fit is None:
            res.note = (res.note + " no fitted curve").strip()
            res.ab_pass = False
        else:
            rng = rng_for(base_seed, STREAM_DRAW_AB, truth.stream, truth.index)
            res.ab_accuracy, res.ab_se = ab_accuracy(truth_lambda(truth), lam_fit, n_ab, rng)
            res.ab_pass = res.ab_accuracy <= TARGET_AB
    else:
        res.outcome = "n/a"
    return res


# --------------------------------------------------------------------------- aggregation
def wilson(k: int, n: int, z: float = 1.96) -> tuple:
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (max(0.0, centre - half), min(1.0, centre + half))


def _figure(name: str, num: int, den: int, target: float | None, *, higher_is_better: bool,
            note: str) -> dict:
    value = num / den if den else None
    lo, hi = wilson(num, den)
    return {"name": name, "numerator": num, "denominator": den, "value": value,
            "mc_se": math.sqrt(value * (1 - value) / den) if den else None,
            "wilson95": [lo, hi] if den else None, "target": target,
            "target_reached": (None if value is None or target is None else
                               (value >= target if higher_is_better else value <= target)),
            "note": note}


def _majority(found: int, computed: int) -> bool | None:
    """Whether a strict majority of the computed draws says yes; None without computed draws."""
    return None if computed <= 0 else 2 * found > computed


def _reference_matches_truth(r) -> bool | None:
    """Does the reference's majority count for one truth equal the truth count? Its N is the
    majority of the truth draws preferring N; the weakest-removed draws must not majority-prefer N
    (a false split). None when a side has no computed draws. A tie is not a majority."""
    if r.n_true == 0:
        return None
    says_n = _majority(r.detect_found, r.detect_draws - r.detect_indeterminate)
    if says_n is None:
        return None
    if not says_n:
        return False
    split = _majority(r.split_found - r.split_indeterminate, r.split_draws - r.split_indeterminate)
    return None if split is None else not split


def summarize(results) -> dict:
    """The three figures and the counts behind them. Targets are reported, never enforced."""
    res = list(results)
    resolvable = [r for r in res if r.resolvability == "resolvable"]
    low = [r for r in res if r.n_true <= 1]
    agree = sum(r.detect_found for r in res) + sum(r.split_draws - r.split_found for r in res)
    agree_den = sum(r.detect_draws - r.detect_indeterminate for r in res) \
        + sum(r.split_draws - r.split_indeterminate for r in res)
    per_case = [_reference_matches_truth(r) for r in res]
    per_case_scored = [m for m in per_case if m is not None]
    n_abstain = sum(r.outcome == "abstain" for r in resolvable)
    figures = [
        _figure("correct_counts_on_resolvable", sum(r.outcome == "correct" for r in resolvable),
                len(resolvable), TARGET_CORRECT, higher_is_better=True,
                note=f"{n_abstain} abstentions (ambiguous answers) are in the denominator"),
        _figure("confident_extra_on_empty_or_single", sum(bool(r.confident_extra) for r in low),
                len(low), TARGET_EXTRA, higher_is_better=False,
                note="fit states more modes than the truth holds, without saying it is ambiguous"),
        _figure("ab_share_at_or_below_60pct", sum(bool(r.ab_pass) for r in resolvable),
                len(resolvable), None, higher_is_better=True,
                note="share of resolvable cases whose likelihood-ratio A/B accuracy is <= 60%; "
                     "the design sets no share target, only the per-case 60% limit"),
    ]
    ab = [r.ab_accuracy for r in resolvable if r.ab_accuracy is not None]
    return {
        "figures": figures,
        "n_cases": len(res),
        "n_resolvable": len(resolvable),
        "n_borderline": sum(r.resolvability == "borderline" for r in res),
        "n_not_applicable": sum(r.resolvability == "not_applicable" for r in res),
        "n_abstentions": n_abstain,
        "n_wrong_confident": sum(r.outcome == "wrong" for r in resolvable),
        "n_failed_fits": sum(r.status != "ok" for r in res),
        "n_reference_errors": sum(r.status == "error" for r in res),
        "mean_ab_accuracy": float(np.mean(ab)) if ab else None,
        # Pooled over draws: a truth with many draws outweighs one with few, so it can look high
        # while the reference fails on many truths. The per-case share gives each truth one vote.
        "reference_agreement_pooled_draws": {
            "numerator": int(agree), "denominator": int(agree_den),
            "value": agree / agree_den if agree_den else None,
            "note": "reference picks N on truth draws and N-1 on weakest-removed draws, pooled over "
                    "every draw with a computed evidence"},
        "reference_agreement_per_case": {
            "numerator": sum(per_case_scored), "denominator": len(per_case_scored),
            "unscored": sum(1 for r, m in zip(res, per_case) if m is None and r.n_true > 0),
            "value": (sum(per_case_scored) / len(per_case_scored)) if per_case_scored else None,
            "note": "share of generated truths (n >= 1) whose majority reference count equals the "
                    "truth count; unscored truths (no computed draws on a side) are not counted"},
        "reference_indeterminate_draws": int(sum(r.detect_indeterminate + r.split_indeterminate
                                                 for r in res)),
    }


# --------------------------------------------------------------------------- running
@dataclass
class RunInfo:
    wall_clock_s: float = 0.0
    n_requested: int = 0
    n_completed: int = 0
    stopped_by_deadline: bool = False
    killed_in_flight: int = 0


def run_benchmark(truths, *, base_seed: int, n_workers: int, budget: ReferenceBudget,
                  n_ab_draws: int = AB_DRAWS, errors_tier: str = "cheap",
                  deadline_s: float | None = None) -> tuple:
    """Run the cases on ``n_workers`` processes (1 = in this process). ``deadline_s`` is the wall-clock
    cap: no case starts in its last grace seconds (:data:`HARD_KILL_GRACE_S`, at most half the cap),
    and workers still running are killed at the cap; the cases that finished are reported. The
    in-process path can only check between cases. Returns (results sorted by case, RunInfo)."""
    if n_workers < 1:
        raise ValueError("n_workers must be >= 1")
    t0 = time.monotonic()
    info = RunInfo(n_requested=len(truths))
    tasks = [(t, base_seed, budget, n_ab_draws, errors_tier) for t in truths]
    # shortcut: a cap under two grace periods gets half the cap as grace, so it still starts cases
    grace = 0.0 if deadline_s is None else min(HARD_KILL_GRACE_S, deadline_s / 2)
    elapsed = lambda: time.monotonic() - t0   # noqa: E731
    closed = lambda: deadline_s is not None and elapsed() >= deadline_s - grace   # noqa: E731
    results: list = []
    if n_workers == 1:
        for task in tasks:
            if closed():
                break
            results.append(run_case(task))
    else:
        ex = ProcessPoolExecutor(max_workers=n_workers)
        pending, queue = set(), iter(tasks)
        try:
            while True:
                while len(pending) < n_workers and not closed():
                    task = next(queue, None)
                    if task is None:
                        break
                    pending.add(ex.submit(run_case, task))
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
    info.stopped_by_deadline = len(results) < len(tasks)
    info.n_completed, info.wall_clock_s = len(results), elapsed()
    results.sort(key=lambda r: (r.stream, r.index))
    return results, info


def write_outputs(folder: Path, results, summary: dict, meta: dict) -> tuple:
    """``figures.json`` and ``cases.csv`` in ``folder``; returns both paths."""
    folder.mkdir(parents=True, exist_ok=True)
    figures = folder / "figures.json"
    figures.write_text(json.dumps({"meta": meta, "summary": summary}, indent=2, default=str),
                       encoding="utf-8", newline="\n")
    cases = folder / "cases.csv"
    names = [f.name for f in fields(CaseResult)]
    with open(cases, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=names)
        w.writeheader()
        for r in results:
            w.writerow({k: ("" if v is None else v) for k, v in asdict(r).items()})
    return figures, cases


# --------------------------------------------------------------------------- command line
def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m isar.synth.count_bench",
        description="Peak-count benchmark: figures file and per-case table in an output folder. "
                    "Targets are reported, never gates.")
    p.add_argument("--out", type=Path, default=None,
                   help="output folder (default: out/count_bench/<timestamp> under the repo)")
    p.add_argument("--mode", choices=("evaluation", "tuning"), default="evaluation",
                   help="tuning uses the separate tuning seeds and is capped at "
                        f"{TUNING_MAX_CASES} cases / {TUNING_MAX_SECONDS // 60} min")
    p.add_argument("--n-workers", type=int, default=None,
                   help=f"worker processes (default: min({TUNING_WORKERS}, cpu-2))")
    p.add_argument("--cases", type=int, default=100, help="number of cases")
    p.add_argument("--seed", type=int, default=20261023, help="base seed")
    p.add_argument("--ceiling", type=int, default=None,
                   help=f"largest mode count generated (default: ${MODE_CEILING_ENV}, else "
                        f"{DEFAULT_MODE_CEILING})")
    p.add_argument("--draws", type=int, default=RESOLVE_DRAWS, help="reference draws of the truth")
    p.add_argument("--null-draws", type=int, default=None,
                   help="reference draws of the weakest-removed truth (default: = --draws)")
    p.add_argument("--ab-draws", type=int, default=AB_DRAWS, help="A/B draws per source")
    p.add_argument("--ref-evidence", choices=REF_EVIDENCE_KINDS, default=ReferenceBudget.evidence,
                   help="reference evidence: laplace (default, no sampler) or nested (slow, spot checks)")
    p.add_argument("--ref-n-live", type=int, default=ReferenceBudget.n_live)
    p.add_argument("--ref-random-starts", type=int, default=ReferenceBudget.random_starts)
    p.add_argument("--errors-tier", default="cheap", choices=("cheap", "fast", "auto", "full"))
    p.add_argument("--max-seconds", type=float, default=None,
                   help="wall-clock cap: no case starts in its last "
                        f"{int(HARD_KILL_GRACE_S)} s, and workers still running are killed at the cap")
    return p


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    t0 = time.monotonic()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = Path(args.out) if args.out else config.OUT_DIR / "count_bench" / stamp
    folder.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.FileHandler(folder / "count_bench.log", encoding="utf-8"),
                                  logging.StreamHandler(sys.stderr)])
    n_cases, max_seconds = args.cases, args.max_seconds
    if args.mode == "tuning":
        n_cases = min(n_cases, TUNING_MAX_CASES)
        max_seconds = TUNING_MAX_SECONDS if max_seconds is None else min(max_seconds, TUNING_MAX_SECONDS)
    n_workers = args.n_workers or max(1, min(TUNING_WORKERS, (os.cpu_count() or 2) - 2))
    ceiling = mode_ceiling(args.ceiling)
    stream = STREAM_CASE_TUNING if args.mode == "tuning" else STREAM_CASE_EVAL
    budget = ReferenceBudget(draws=args.draws, null_draws=args.null_draws or args.draws,
                             evidence=args.ref_evidence, n_live=args.ref_n_live,
                             random_starts=args.ref_random_starts)
    print(f"count_bench: {n_cases} {args.mode} cases, ceiling {ceiling}, {n_workers} workers, "
          f"reference {budget.draws}/{budget.null_draws} draws ({budget.evidence} evidence), "
          f"A/B {args.ab_draws} draws")
    truths = [generate_case(args.seed, stream, i, ceiling) for i in range(n_cases)]
    results, info = run_benchmark(truths, base_seed=args.seed, n_workers=n_workers, budget=budget,
                                  n_ab_draws=args.ab_draws, errors_tier=args.errors_tier,
                                  deadline_s=max_seconds)
    summary = summarize(results)
    meta = {"mode": args.mode, "base_seed": args.seed, "ceiling": ceiling, "n_workers": n_workers,
            "reference_budget": asdict(budget), "ab_draws": args.ab_draws,
            "errors_tier": args.errors_tier, "run": asdict(info),
            "tuning_cap_reached": bool(args.mode == "tuning" and (
                info.stopped_by_deadline or args.cases > TUNING_MAX_CASES)),
            "targets_are_not_gates": "ruling R15: reported, never enforced"}
    figures_path, cases_path = write_outputs(folder, results, summary, meta)
    for fig in summary["figures"]:
        value, se = (("n/a", "n/a") if fig["value"] is None
                     else (f"{fig['value']:.3f}", f"{fig['mc_se']:.3f}"))
        print(f"  {fig['name']}: {fig['numerator']}/{fig['denominator']} = {value} "
              f"(+/- {se}; target {fig['target'] if fig['target'] is not None else 'per case <= 0.60'})")
    for key, label in (("reference_agreement_per_case", "per case"),
                       ("reference_agreement_pooled_draws", "pooled over draws")):
        agr = summary[key]
        value = "n/a" if agr["value"] is None else f"{agr['value']:.3f}"
        print(f"  reference agreement, {label}: {agr['numerator']}/{agr['denominator']} = {value}")
    print(f"  resolvable {summary['n_resolvable']}, borderline {summary['n_borderline']}, "
          f"no-N-1 {summary['n_not_applicable']}, failed fits {summary['n_failed_fits']}")
    print(f"Output folder: {folder}")
    print(f"Wall clock: {time.monotonic() - t0:.1f} s ({info.n_completed}/{info.n_requested} cases"
          f"{', stopped at the deadline' if info.stopped_by_deadline else ''}"
          f"{f', {info.killed_in_flight} killed in flight' if info.killed_in_flight else ''})")
    return 0 if info.n_completed else 2


if __name__ == "__main__":
    sys.exit(main())

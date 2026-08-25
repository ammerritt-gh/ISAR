"""The SCENE world suite: named, seeded synthetic worlds along (2,0,0) → (3,0,0).

Each world is an adversarial question put to a target-free scene analysis — *is there an
acoustic branch here at all?*, *is that peak a mode or the resolution?*, *is the lowest
thing on this path a branch or a Bragg reflection?* — and each answers it by construction,
in the truth manifest. A world factory returns ``(config, truth)``; the truth embeds the
config, so there is exactly one copy of every number and the pair cannot drift apart.

Deliberate design points:

* No world is named for the verdict it should get. The identities in the manifest
  ('acoustic', 'optic', 'bragg', 'level', 'unknown') are the *answer key*; nothing in the
  generator reads them, so a world cannot smuggle its label into the data.
* The lowest-energy branch is not always acoustic (`no_acoustic`), the lowest feature is
  not always at a zone centre (`off_lattice_elastic`), and a real mode is not always a DHO
  (`mismatch`). Any rule of the form "lowest = acoustic" fails at least one world here.
* Intrinsic mode energies stay inside 0–10 meV; the scan window covers ±10 meV so the
  anti-Stokes side is measured (`as_warm` / `as_cold`).
"""
from __future__ import annotations

from typing import Callable

from . import config
from .dispersion import BranchSpec

# Reference count levels. `PEAK` is a comfortably-counted mode; `FAINT` sits at the
# routine detection floor (see RunConfig.target_counts_levels: ~20 is the floor, below
# that fits get very hard), so a world built on it is a budget-honesty test.
PEAK = 100.0
ELASTIC_PEAK = 400.0
BACKGROUND = 2.0
FAINT = 8.0

# Default damping, matching the settled Phase-1 happy path.
GAMMA = 0.5

# Bragg-like elastic localization: sharp in Q, at both zone centres of the path.
ZONE_CENTRES = (2.0, 3.0)
BRAGG_FWHM = 0.08

# Wider elastic H-envelope for `endpoint_under_bragg` only: at fwhm=0.08 the elastic
# component is only >=25% of the acoustic peak at H=2.05, not 2.10/2.15 (5.3 / 0.02 counts
# vs the 100-count acoustic mode). 0.20 keeps all three low-q nodes (H=2.05/2.10/2.15,
# offsets 0.05/0.10/0.15 from the zone centre) at or above that 25% floor.
ENDPOINT_BRAGG_FWHM = 0.20

ACOUSTIC = BranchSpec("acoustic", 0.0, 6.0)
OPTIC = BranchSpec("optic", 7.0, 9.0)
GAPPED = BranchSpec("gapped", 3.0, 6.0)


def _bragg(peak_counts: float = ELASTIC_PEAK, q_fwhm: float = BRAGG_FWHM) -> config.ComponentTruth:
    return config.ComponentTruth(
        kind="elastic", identity="bragg", peak_counts=peak_counts,
        q_centers=ZONE_CENTRES, q_fwhm=q_fwhm,
    )


def _mode(branch: BranchSpec, identity: str, peak_counts: float = PEAK,
          gamma: float = GAMMA) -> config.ComponentTruth:
    return config.ComponentTruth(kind="dho", identity=identity,
                                 peak_counts=peak_counts, branch=branch, gamma=gamma)


def _world(name: str, seed: int, description: str, components, *,
           T: float = 100.0, background: float = BACKGROUND):
    cfg = config.WorldConfig(name=name, seed=seed)
    truth = config.WorldTruth(config=cfg, T=T, background_counts=background,
                              components=tuple(components), description=description)
    return cfg, truth


# ----------------------------------------------------------------------------
# The worlds
# ----------------------------------------------------------------------------
def canonical(seed: int = 1001):
    """Bragg elastic line at the zone centres, gapless acoustic branch, optic branch."""
    return _world(
        "canonical", seed,
        "elastic zone-centre line + gapless acoustic branch + optic branch (7 meV gap)",
        [_bragg(), _mode(ACOUSTIC, "acoustic"), _mode(OPTIC, "optic")],
    )


def no_acoustic(seed: int = 1002):
    """The lowest finite-energy mode is GAPPED — no gapless branch exists on this path."""
    return _world(
        "no_acoustic", seed,
        "gapped lowest branch (3 meV at the zone centre) + optic branch; no acoustic mode",
        [_bragg(), _mode(GAPPED, "gapped"), _mode(OPTIC, "optic")],
    )


def pure_elastic(seed: int = 1003):
    """Elastic line and background only — every inelastic claim here is a false positive."""
    return _world("pure_elastic", seed, "zone-centre elastic line + flat background only",
                  [_bragg()])


def blank(seed: int = 1004):
    """Flat background only. Nothing is there."""
    return _world("blank", seed, "flat background only; no components at all", [])


def unknown_extra(seed: int = 1005):
    """Canonical plus a dispersionless ~5 meV level that belongs to neither branch."""
    level = BranchSpec("level", 5.0, 5.0)
    return _world(
        "unknown_extra", seed,
        "canonical + a dispersionless 5 meV level (a third mode, not a branch)",
        [_bragg(), _mode(ACOUSTIC, "acoustic"), _mode(OPTIC, "optic"),
         _mode(level, "level", peak_counts=60.0)],
    )


def endpoint_under_bragg(seed: int = 1006):
    """A shallow acoustic branch whose first few nodes are unresolvable from the elastic line.

    ω₀ = 2·|sin(π(H−2))| gives 0.31 / 0.62 / 0.92 meV at H = 2.05 / 2.10 / 2.15, all inside
    the ~1 meV resolution FWHM, and exactly 0 at the zone centres. The branch is really
    there at every one of those H; the data cannot say so.
    """
    shallow = BranchSpec("acoustic", 0.0, 2.0)
    return _world(
        "endpoint_under_bragg", seed,
        "shallow acoustic branch degenerate with the elastic line over several low-q nodes",
        [_bragg(q_fwhm=ENDPOINT_BRAGG_FWHM), _mode(shallow, "acoustic", gamma=0.25),
         _mode(OPTIC, "optic")],
    )


def crossing(seed: int = 1007):
    """Two independent branches that swap order mid-path.

    Branch A rises 2 → 7 meV, branch B falls 6.5 → 3 meV: A is below B near the zone
    centres and above it near the zone boundary, so any per-scan ordering rule
    ('lower = acoustic') relabels the same physical branch halfway along the path.
    """
    a = BranchSpec("branch_a", 2.0, 7.0)
    b = BranchSpec("branch_b", 6.5, 3.0)
    return _world(
        "crossing", seed,
        "two branches crossing mid-path (A: 2→7 meV rising, B: 6.5→3 meV falling)",
        [_bragg(), _mode(a, "branch_a"), _mode(b, "branch_b")],
    )


def as_warm(seed: int = 1008):
    """Canonical at 300 K: the anti-Stokes side is strong and must be used."""
    return _world("as_warm", seed, "canonical world at 300 K (anti-Stokes clearly detectable)",
                  [_bragg(), _mode(ACOUSTIC, "acoustic"), _mode(OPTIC, "optic")], T=300.0)


def as_cold(seed: int = 1009):
    """Canonical at 10 K: the anti-Stokes side is Bose-dead and must not be claimed."""
    return _world("as_cold", seed, "canonical world at 10 K (anti-Stokes Bose-suppressed)",
                  [_bragg(), _mode(ACOUSTIC, "acoustic"), _mode(OPTIC, "optic")], T=10.0)


def off_lattice_elastic(seed: int = 1010):
    """A resolution-limited elastic feature at H = 2.5 — a zero-energy thing off the lattice."""
    feature = config.ComponentTruth(
        kind="elastic", identity="off_lattice_elastic", peak_counts=ELASTIC_PEAK,
        q_centers=(2.5,), q_fwhm=BRAGG_FWHM,
    )
    return _world("off_lattice_elastic", seed,
                  "background + a resolution-limited elastic feature at H = 2.5",
                  [feature])


def mismatch(seed: int = 1011):
    """Canonical, except the low branch is an asymmetric flat-top, not a DHO.

    Same dispersion, same amplitude, wrong lineshape: a DHO fit converges on it and is
    wrong about the width and about which energy is the mode.
    """
    horn = config.ComponentTruth(
        kind="unknown", identity="unknown", peak_counts=PEAK, branch=ACOUSTIC,
        gamma=GAMMA, width=2.5, asymmetry=0.5,
    )
    return _world("mismatch", seed,
                  "canonical with the low branch replaced by a non-DHO two-horn lineshape",
                  [_bragg(), horn, _mode(OPTIC, "optic")])


def faint(seed: int = 1012):
    """Canonical with every mode at the detection floor (~8 counts over a 2-count background)."""
    return _world(
        "faint", seed,
        "canonical with mode amplitudes at the detection floor (~8 peak counts)",
        [_bragg(peak_counts=40.0), _mode(ACOUSTIC, "acoustic", peak_counts=FAINT),
         _mode(OPTIC, "optic", peak_counts=FAINT)],
    )


def context_probe(seed: int = 1013):
    """Canonical world plus a faint off-lattice elastic anomaly at H = 2.5.

    SCENE-4 PR 4.4 (D5): the physics half of the off-path/context-scan campaign world. The
    anomaly sits off both zone centres and off both canonical branches — exactly the kind of
    stray finding that motivates an operator, or a future producer, to take a diagnostic scan off
    the declared canonical path before trusting the region. Nothing downstream reads its
    identity; this world exists for the campaign-level claim (topology survives an off-path
    arena, the campaign continues), not for a new adversarial verdict question.
    """
    anomaly = config.ComponentTruth(
        kind="elastic", identity="context_anomaly", peak_counts=FAINT,
        q_centers=(2.5,), q_fwhm=BRAGG_FWHM,
    )
    return _world(
        "context_probe", seed,
        "canonical world (elastic zone-centre line + acoustic + optic) plus a faint off-lattice "
        "elastic anomaly at H=2.5, motivating an off-path diagnostic scan",
        [_bragg(), _mode(ACOUSTIC, "acoustic"), _mode(OPTIC, "optic"), anomaly],
    )


# ----------------------------------------------------------------------------
# The FCC world (SCENE-5 5.4): declared reflections (2.0, 4.0), an EXTINCT fold at 3.0 —
# only the physics lives here (D3's zone geometry is a SampleKnowledge the harness declares
# separately, never world config). The acoustic branch's period is 2.0 (twice every other
# world's), so it rises monotonically from zero at H=2 to its boundary maximum at the fold
# H=3, exactly the shape a real branch has between two reflections spaced 2 rlu apart. The
# Bragg line sits ONLY at the declared reflection inside this world's [2, 3] domain — H=3 is
# a fold, not a reflection, and must carry no elastic component (the extinction itself).
FCC_ACOUSTIC = BranchSpec("fcc_acoustic", 0.0, 6.0, period=2.0)
FCC_OPTIC = BranchSpec("fcc_optic", 9.0, 9.0)


def _fcc_bragg(peak_counts: float = ELASTIC_PEAK, q_fwhm: float = BRAGG_FWHM) -> config.ComponentTruth:
    return config.ComponentTruth(
        kind="elastic", identity="bragg", peak_counts=peak_counts,
        q_centers=(2.0,), q_fwhm=q_fwhm,
    )


def fcc_warm(seed: int = 1014):
    """FCC test sample at 300 K: acoustic (0->6 meV, boundary max at the H=3 extinct fold) +
    optic (~9 meV, flat) branches; the anti-Stokes side is clearly detectable."""
    return _world(
        "fcc_warm", seed,
        "FCC acoustic (period 2, 0->6 meV, boundary max at H=3) + optic (~9 meV); 300 K",
        [_fcc_bragg(), _mode(FCC_ACOUSTIC, "fcc_acoustic"), _mode(FCC_OPTIC, "fcc_optic")],
        T=300.0,
    )


def fcc_cold(seed: int = 1014):
    """FCC test sample at 5 K: same branches, anti-Stokes Bose-suppressed.

    Deliberately the SAME default seed as :func:`fcc_warm`: the pair is a controlled
    temperature A/B, and a different seed would confound the Bose factor with Poisson
    realization noise — a warm/cold divergence would then prove nothing about the physics
    (5.4 verifier advisory A2; the same-seed control is what demonstrated the divergence is
    genuinely thermal).
    """
    return _world(
        "fcc_cold", seed,
        "FCC acoustic (period 2, 0->6 meV, boundary max at H=3) + optic (~9 meV); 5 K",
        [_fcc_bragg(), _mode(FCC_ACOUSTIC, "fcc_acoustic"), _mode(FCC_OPTIC, "fcc_optic")],
        T=5.0,
    )


def fcc_no_acoustic(seed: int = 1016):
    """FCC world with the acoustic branch absent entirely: a low-omega search here comes back
    empty by construction, for the optic-pivot closed-loop world (only the optic branch is
    really there to find)."""
    return _world(
        "fcc_no_acoustic", seed,
        "FCC Bragg line + optic band (~9 meV) only; no acoustic branch exists on this path",
        [_fcc_bragg(), _mode(FCC_OPTIC, "fcc_optic")],
        T=300.0,
    )


WORLDS: dict[str, Callable[..., tuple[config.WorldConfig, config.WorldTruth]]] = {
    "canonical": canonical,
    "no_acoustic": no_acoustic,
    "pure_elastic": pure_elastic,
    "blank": blank,
    "unknown_extra": unknown_extra,
    "endpoint_under_bragg": endpoint_under_bragg,
    "crossing": crossing,
    "as_warm": as_warm,
    "as_cold": as_cold,
    "off_lattice_elastic": off_lattice_elastic,
    "mismatch": mismatch,
    "faint": faint,
    "context_probe": context_probe,
    "fcc_warm": fcc_warm,
    "fcc_cold": fcc_cold,
    "fcc_no_acoustic": fcc_no_acoustic,
}


def build(name: str, seed: int | None = None):
    """Return ``(config, truth)`` for a named world, optionally overriding its seed."""
    try:
        factory = WORLDS[name]
    except KeyError:
        raise KeyError(f"unknown world {name!r}; known worlds: {sorted(WORLDS)}") from None
    return factory() if seed is None else factory(seed)

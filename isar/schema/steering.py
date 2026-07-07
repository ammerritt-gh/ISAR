"""Serializable schemas for the autonomous-steering / confidence-ladder work
(`docs/MEASUREMENT_STEERING.md`).

Defined **up front** (contract 8) so the rescue phases don't accrete ad-hoc fields the later
BOED work then fights. Phase 0 uses :class:`IntensityMeasure` (portable, provenance-bearing
intensity — contract 2) and :class:`ParamConstraints` (the fit-time constraint object carried on
`ScanContext`, consumed from Phase 2). The descriptor and acquisition-observation schemas land with
their phases (1 and 5) and belong in this module.

Everything here is a small frozen dataclass with ``to_dict``/``from_dict`` so it round-trips through
`<exp>.state.json` and the ledger as a plain dict (the existing `dispersion_prior` pattern).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class IntensityMeasure:
    """A **portable** integrated-intensity measure for one fitted phonon peak (contract 2).

    A raw fitted amplitude ``A`` is not comparable across scans: it scales with the counting
    exposure (monitor counts or seconds) and depends on the resolution. What we store and carry
    between scans is the exposure-normalised amplitude ``value = A / exposure`` plus enough
    provenance (``exposure``, ``counting_mode``, resolution FWHMs, instrument, merge state) to
    convert it into the *expected amplitude of another scan* before use.

    ``expected_amplitude(exposure_query)`` is the only portability operation Phase 1's
    ``detection_power`` needs: it predicts the fitted amplitude a scan of the given exposure would
    show, i.e. ``value * exposure_query`` (so a store-then-predict at a different exposure recovers
    ``A * exposure_query / exposure`` — the round-trip contract test).

    The resolution FWHMs are provenance for a later height↔area refinement; ``basis`` names what
    ``value`` currently is so a future consumer can upgrade without silently mis-scaling old points.
    """

    value: float               # exposure-normalised primary-phonon amplitude (A / exposure)
    exposure: float            # per-point counting exposure the fit saw (monitor counts | seconds)
    counting_mode: str         # "monitor" | "time"
    gauss_fwhm: float          # Voigt resolution — Gaussian FWHM (meV), provenance
    lorentz_fwhm: float        # Voigt resolution — Lorentzian FWHM (meV), provenance
    instrument: str
    pooled: bool = False       # a master (combine-before-divide) fit
    n_members: int | None = None
    basis: str = "amplitude_per_exposure"

    def expected_amplitude(self, exposure: float) -> float:
        """Predicted fitted amplitude at a query scan's per-point ``exposure`` (portable)."""
        return float(self.value) * float(exposure)

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict) -> "IntensityMeasure":
        f = {k: d[k] for k in d if k in cls.__dataclass_fields__}
        return cls(**f)


@dataclass(frozen=True)
class ParamConstraints:
    """Fit-time constraints on named global parameters (e.g. ``"phonon.Gamma"``, ``"phonon.A"``).

    Carried on :class:`~isar.analyze.context.ScanContext`; consumed from Phase 2. Two kinds, kept
    apart so the fit-vs-decide contract holds (contract 1):

    - ``soft``: ``{global_name: (mu, sigma)}`` — a Gaussian-prior penalty ``((θ−mu)/sigma)²`` added
      to the **optimiser objective only** (never to the data cost read by AIC / GOF / falsification).
    - ``hard``: ``{global_name: value}`` — the parameter is removed from the free set and baked into
      the model at ``value`` (``k`` drops accordingly).

    Empty by default (an unconstrained fit); ``is_empty()`` lets the fit path skip all of it.
    """

    soft: dict = field(default_factory=dict)   # {global_name: (mu, sigma)}
    hard: dict = field(default_factory=dict)   # {global_name: value}

    def is_empty(self) -> bool:
        return not self.soft and not self.hard

    def to_dict(self) -> dict:
        return {"soft": {k: list(v) for k, v in self.soft.items()},
                "hard": dict(self.hard)}

    @classmethod
    def from_dict(cls, d: dict) -> "ParamConstraints":
        soft = {k: tuple(v) for k, v in (d.get("soft") or {}).items()}
        return cls(soft=soft, hard=dict(d.get("hard") or {}))

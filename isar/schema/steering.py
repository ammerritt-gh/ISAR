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
class DescriptorPeak:
    """One model-free feature found by the L0 descriptor: "signal at E=X, width W, intensity I".

    ``significance`` is the one-sided Gaussian-equivalent σ of ``p_value`` (the exact-Poisson tail of
    the integrated excess over the local background), so it stays meaningful at low counts.
    ``integrated_intensity`` is the raw background-subtracted counts over the peak's resolution
    window (not exposure-normalised — that is :class:`IntensityMeasure`'s job downstream).
    """

    energy: float              # peak centre (meV, same axis as the fit)
    width: float               # FWHM estimate (meV), floored at the resolution
    integrated_intensity: float  # Σ(counts − background) over the peak window (raw counts)
    significance: float        # one-sided Gaussian-equivalent σ of p_value
    p_value: float             # exact-Poisson family-wise-uncorrected tail probability

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict) -> "DescriptorPeak":
        return cls(**{k: d[k] for k in d if k in cls.__dataclass_fields__})


@dataclass(frozen=True)
class ScanDescriptor:
    """The per-scan model-free description (L0). A characterised **statistical contract**, not a
    boolean: peaks are accepted only when their exact-Poisson tail clears a Bonferroni threshold
    ``alpha / n_res`` over the ``n_res`` independent resolution elements in the window, so the
    per-scan false-positive rate is ≈ ``alpha`` under a flat Poisson background (contract 3 / §4.3).

    ``peaks`` excludes the elastic line and masked/QC-failed points. ``n_peaks`` is what the
    falsification gate compares against a fitted model's mode count.
    """

    peaks: tuple = ()          # tuple[DescriptorPeak]
    background: float = 0.0     # estimated flat background level (counts/point)
    n_res: float = 0.0          # independent resolution elements in the searched window
    alpha: float = 0.05         # target per-scan false-positive rate
    n_points: int = 0           # kept points searched

    @property
    def n_peaks(self) -> int:
        return len(self.peaks)

    def to_dict(self) -> dict:
        return {"peaks": [p.to_dict() for p in self.peaks], "background": self.background,
                "n_res": self.n_res, "alpha": self.alpha, "n_points": self.n_points}

    @classmethod
    def from_dict(cls, d: dict) -> "ScanDescriptor":
        peaks = tuple(DescriptorPeak.from_dict(p) for p in (d.get("peaks") or ()))
        return cls(peaks=peaks, background=float(d.get("background", 0.0)),
                   n_res=float(d.get("n_res", 0.0)), alpha=float(d.get("alpha", 0.05)),
                   n_points=int(d.get("n_points", 0)))


@dataclass(frozen=True)
class ProposalScore:
    """The acquisition score behind one :class:`~isar.drive.acquisition.acquire.ScanProposal` (Phase 5).

    Serialized onto the proposal ``meta`` and the session log so the ranking is auditable — which
    method ranked the scan, the EIG, the cost, and the prior/predicted σ(ω₀). ``method`` is
    ``"counts"`` (5a diminishing-returns EIG), ``"fisher"`` (5b forward-model screen), ``"explore"``
    (an unvisited q — no baseline, EIG effectively infinite), or ``"deficit"`` (the σ-deficit
    fallback when no cost history exists). Non-finite scores are stored as ``None`` so the dict
    round-trips through strict JSON.
    """

    method: str
    eig: float | None = None          # goal-weighted EIG on ω₀ (None = +inf, no baseline yet)
    cost: float = 0.0                 # ΔC = n_points × ncount (monitor-count budget)
    eig_per_cost: float | None = None  # the ranking key (None = +inf)
    sigma_prior: float | None = None  # σ(ω₀) belief before the scan (meV)
    sigma_scan: float | None = None   # predicted σ(ω₀) after the scan (meV)
    c_spent: float = 0.0              # C_q accumulated cost at this q before the scan
    reason: str = ""
    classification_eig: float | None = None  # branch-label mutual information (nats)
    classification_weight: float = 0.0
    combined_information: float | None = None
    purpose: str | None = None
    ranking_cost: float | None = None  # ΔC proxy used to choose the q before its window was emitted
    ranking_ncount: int | None = None  # per-point exposure used by an exposure-aware ranking screen
    omega_halfwidth_reduction: float | None = None
    model_entropy_reduction: float | None = None
    score_standard_error: float | None = None
    simulation_count: int | None = None
    conservative_utility: float | None = None
    evidence_transition: dict | None = None  # evidence/identity routing provenance for tracked branches

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict) -> "ProposalScore":
        return cls(**{k: d[k] for k in d if k in cls.__dataclass_fields__})


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

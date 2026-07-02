"""User fitting guidelines (UNIFICATION_DESIGN §6) — the `[fitting]` policy section, parsed.

The human's control surface, scoped to three capabilities: expected-omega hints (per Q-range or
per scan), per-scan overrides (exclude / seed), and exclusions. Guidelines **narrow, never widen**
(U8): a structure restriction can only subset the physics-gated enumeration; nothing here can force
an inadmissible model or weaken the validated fit policy. They flow into the existing analysis stack
as a `PhononPrior` (seeds the IC grid) and a label filter on `enumerate_structures` — `isar/analyze/`
is untouched.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from isar.analyze.prior import PhononPrior
from isar.schema.card import Card

_COMPONENT_INDEX = {"h": 0, "k": 1, "l": 2}


@dataclass(frozen=True)
class Expectation:
    """An expected dispersion hint: applies to a scan list OR a Q-range (with optional T)."""
    scans: tuple = ()
    q_component: str | None = None
    q_min: float | None = None
    q_max: float | None = None
    T: float | None = None
    T_tol: float = 5.0
    omega0: float | None = None
    omega0_sigma: float = 0.5
    structures: tuple | None = None

    def matches(self, *, scan_number, q, T) -> bool:
        if self.scans and scan_number in self.scans:
            return True
        if self.q_component is not None and q is not None:
            ci = _COMPONENT_INDEX.get(self.q_component)
            if ci is None or ci >= len(q):
                return False
            v = float(q[ci])
            if self.q_min is not None and v < self.q_min:
                return False
            if self.q_max is not None and v > self.q_max:
                return False
            if self.T is not None and T is not None and abs(float(T) - self.T) > self.T_tol:
                return False
            return True
        return False


@dataclass(frozen=True)
class Override:
    scan: int
    exclude: bool = False
    reason: str | None = None
    omega0: float | None = None
    omega0_sigma: float = 0.5
    structures: tuple | None = None


@dataclass
class Guidelines:
    detectors: object = "enabled"           # "enabled" | list[str]
    omega_window: tuple | None = None       # (lo, hi) meV or None
    monitor_frac: float = 0.9
    min_fit_points: int = 6
    expectations: list = field(default_factory=list)
    overrides: dict = field(default_factory=dict)   # scan -> Override

    # --- queries ------------------------------------------------------------
    def excluded(self, scan_number) -> str | None:
        ov = self.overrides.get(scan_number)
        if ov is not None and ov.exclude:
            return ov.reason or "excluded by [fitting] override"
        return None

    def expectation_for(self, *, scan_number, q, T) -> PhononPrior | None:
        """A user PhononPrior for this scan, or None. Per-scan override beats a Q-range expectation."""
        ov = self.overrides.get(scan_number)
        if ov is not None and ov.omega0 is not None:
            return PhononPrior(omega0=float(ov.omega0), amplitude=None,
                               omega0_sigma=float(ov.omega0_sigma))
        for exp in self.expectations:
            if exp.omega0 is not None and exp.matches(scan_number=scan_number, q=q, T=T):
                return PhononPrior(omega0=float(exp.omega0), amplitude=None,
                                   omega0_sigma=float(exp.omega0_sigma))
        return None

    def structure_labels_for(self, *, scan_number, q, T) -> tuple | None:
        """The label whitelist for this scan, or None (no restriction). Override beats expectation."""
        ov = self.overrides.get(scan_number)
        if ov is not None and ov.structures:
            return tuple(ov.structures)
        for exp in self.expectations:
            if exp.structures and exp.matches(scan_number=scan_number, q=q, T=T):
                return tuple(exp.structures)
        return None

    def filter_structures(self, hyps, *, scan_number, q, T):
        """Narrow the physics-gated hypotheses to the user's whitelist. Never returns empty: an
        empty match means the whitelist named no admissible structure, so we keep all + warn."""
        labels = self.structure_labels_for(scan_number=scan_number, q=q, T=T)
        if not labels:
            return hyps, None
        kept = [h for h in hyps if h.label in labels]
        if not kept:
            return hyps, (f"guideline structures {list(labels)} match none of the admissible "
                          f"{[h.label for h in hyps]} — keeping all")
        return kept, None


def resolve(card: Card) -> Guidelines:
    f = dict(card.policy.fitting or {})
    window = f.get("omega_window")
    if window and not (float(window[0]) == 0.0 and float(window[1]) == 0.0):
        omega_window = (float(window[0]), float(window[1]))
    else:
        omega_window = None

    expectations = [_parse_expectation(e) for e in (f.get("expectations") or [])]
    overrides = {}
    for o in (f.get("overrides") or []):
        ov = _parse_override(o)
        if ov is not None:
            overrides[ov.scan] = ov

    return Guidelines(
        detectors=f.get("detectors", "enabled"),
        omega_window=omega_window,
        monitor_frac=float(f.get("monitor_frac", 0.9)),
        min_fit_points=int(f.get("min_fit_points", 6)),
        expectations=expectations,
        overrides=overrides,
    )


def _parse_expectation(e: dict) -> Expectation:
    q = e.get("q") or {}
    return Expectation(
        scans=tuple(int(s) for s in (e.get("scans") or [])),
        q_component=q.get("component"),
        q_min=(float(q["min"]) if "min" in q else None),
        q_max=(float(q["max"]) if "max" in q else None),
        T=(float(e["T"]) if "T" in e else None),
        T_tol=float(e.get("T_tol", 5.0)),
        omega0=(float(e["omega0"]) if "omega0" in e else None),
        omega0_sigma=float(e.get("omega0_sigma", 0.5)),
        structures=(tuple(e["structures"]) if e.get("structures") else None),
    )


def _parse_override(o: dict) -> Override | None:
    if "scan" not in o:
        return None
    return Override(
        scan=int(o["scan"]),
        exclude=bool(o.get("exclude", False)),
        reason=o.get("reason"),
        omega0=(float(o["omega0"]) if "omega0" in o else None),
        omega0_sigma=float(o.get("omega0_sigma", 0.5)),
        structures=(tuple(o["structures"]) if o.get("structures") else None),
    )


def fit_detectors(g: Guidelines, card: Card, cli_detectors=None) -> list[str]:
    """Which detectors to fit: CLI --detectors overrides the guideline, which overrides card-enabled."""
    if cli_detectors:
        return list(cli_detectors)
    if isinstance(g.detectors, (list, tuple)):
        return [str(d) for d in g.detectors]
    return [d.detector_id for d in card.policy.detectors if d.enabled]


def guidelines_signature(g: Guidelines, *, scan_number, q, T) -> dict:
    """The guideline state that applied to a scan — hashed into the ledger's `guidelines_hash` so a
    card edit re-fits exactly the affected scans (§6 correction channel)."""
    prior = g.expectation_for(scan_number=scan_number, q=q, T=T)
    return {
        "excluded": g.excluded(scan_number),
        "prior": None if prior is None else [prior.omega0, prior.omega0_sigma],
        "structures": g.structure_labels_for(scan_number=scan_number, q=q, T=T),
        "omega_window": g.omega_window,
        "monitor_frac": g.monitor_frac,
        "min_fit_points": g.min_fit_points,
    }

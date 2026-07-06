"""User fitting guidelines (UNIFICATION_DESIGN §6) — the `[fitting]` policy section, parsed.

The human's control surface, scoped to three capabilities: expected-omega hints (per Q-range or
per scan), per-scan overrides (exclude / seed), and exclusions. Guidelines **narrow, never widen**
(U8): a structure restriction can only subset the physics-gated enumeration; nothing here can force
an inadmissible model or weaken the validated fit policy. They flow into the existing analysis stack
as a `PhononPrior` (seeds the IC grid) and a label filter on `enumerate_structures` — `isar/analyze/`
is untouched.
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass, field

from isar.analyze.prior import PhononPrior
from isar.schema.card import Card

_COMPONENT_INDEX = {"h": 0, "k": 1, "l": 2}


def _peak_hints(peaks: tuple, omega0, omega0_sigma) -> tuple:
    """Normalize a card's peak declaration to an ordered tuple of {omega0, sigma} hints.

    Back-compat: when no explicit `peaks` are given but a scalar `omega0` is, expose that scalar
    as a single 1-element hint so old cards behave exactly as before (the primary/first peak).
    """
    if peaks:
        return peaks
    if omega0 is not None:
        return ({"omega0": float(omega0), "sigma": float(omega0_sigma)},)
    return ()


@dataclass(frozen=True)
class Expectation:
    """An expected dispersion hint: applies to a scan list OR a Q-range (with optional T).

    Fit-window fields (P5 semantics): `fit_range`/`exclude_regions`/`peaks` mask points from the
    FIT only — the raw intensity arrays are never modified. `peaks` are ordered {omega0, sigma}
    hints; a scalar `omega0` (the legacy field) normalizes to a 1-element `peaks` (the primary).
    """
    scans: tuple = ()
    q_component: str | None = None
    q_min: float | None = None
    q_max: float | None = None
    T: float | None = None
    T_tol: float = 5.0
    omega0: float | None = None
    omega0_sigma: float = 0.5
    structures: tuple | None = None
    fit_range: tuple | None = None          # (lo, hi) meV: keep only points in [lo, hi]
    exclude_regions: tuple = ()             # ((lo, hi), ...): drop points inside each region
    peaks: tuple = ()                       # ({"omega0": .., "sigma": ..}, ...) ordered hints

    def peak_hints(self) -> tuple:
        """Ordered {omega0, sigma} peak hints (scalar `omega0` → 1-element, back-compat)."""
        return _peak_hints(self.peaks, self.omega0, self.omega0_sigma)

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
    """A per-scan override. Fit-window fields share the P5 semantics of `Expectation`: they mask
    points from the FIT only, never touching the intensity arrays."""
    scan: int
    exclude: bool = False
    reason: str | None = None
    omega0: float | None = None
    omega0_sigma: float = 0.5
    structures: tuple | None = None
    fit_range: tuple | None = None
    exclude_regions: tuple = ()
    peaks: tuple = ()

    def peak_hints(self) -> tuple:
        """Ordered {omega0, sigma} peak hints (scalar `omega0` → 1-element, back-compat)."""
        return _peak_hints(self.peaks, self.omega0, self.omega0_sigma)


@dataclass
class Guidelines:
    """The resolved `[fitting]` policy — the frozen artifact the human, the driver, and (fast-follow)
    the LLM proposer edit to steer fits. Fit-window fields (`fit_range`/`exclude_regions`/`peaks`)
    are P5: they mask points from the FIT only; intensity arrays are never modified. All state that
    steers a scan's fit flows through `guidelines_signature` → `guidelines_hash`, so an edit re-fits
    exactly the affected scans (§6 correction channel)."""
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

    def mask_regions_for(self, *, scan_number, q, T):
        """Fit-window mask for this scan: `(fit_range | None, exclude_regions tuple)`.

        P5: these regions mask points from the FIT only — intensity arrays are never modified.
        Per-scan override beats a Q-range expectation (same precedence as `expectation_for`).
        """
        ov = self.overrides.get(scan_number)
        if ov is not None and (ov.fit_range is not None or ov.exclude_regions):
            return ov.fit_range, tuple(ov.exclude_regions)
        for exp in self.expectations:
            if (exp.fit_range is not None or exp.exclude_regions) and \
                    exp.matches(scan_number=scan_number, q=q, T=T):
                return exp.fit_range, tuple(exp.exclude_regions)
        return None, ()

    def peaks_for(self, *, scan_number, q, T) -> tuple:
        """Ordered {omega0, sigma} peak hints for this scan (falling back to the scalar `omega0`
        normalization). Override beats expectation. Primary peak = first element. `expectation_for`
        stays the single-PhononPrior back-compat accessor (primary only)."""
        ov = self.overrides.get(scan_number)
        if ov is not None and ov.peak_hints():
            return ov.peak_hints()
        for exp in self.expectations:
            if exp.matches(scan_number=scan_number, q=q, T=T):
                hints = exp.peak_hints()
                if hints:
                    return hints
        return ()

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


def _parse_fit_range(v, *, where: str) -> tuple | None:
    """Parse `[lo, hi]` into a validated (lo, hi). Malformed → warn-and-ignore (tolerant style)."""
    if v is None:
        return None
    try:
        lo, hi = float(v[0]), float(v[1])
    except (TypeError, ValueError, KeyError, IndexError):
        warnings.warn(f"[fitting] {where}: fit_range {v!r} is not a [lo, hi] pair — ignoring")
        return None
    if not lo < hi:
        warnings.warn(f"[fitting] {where}: fit_range {v!r} needs lo < hi — ignoring")
        return None
    return (lo, hi)


def _parse_exclude_regions(v, *, where: str) -> tuple:
    """Parse `[[a,b], [c,d]]` into validated ((a,b), ...). Malformed regions warn-and-drop."""
    if not v:
        return ()
    out = []
    for r in v:
        try:
            lo, hi = float(r[0]), float(r[1])
        except (TypeError, ValueError, KeyError, IndexError):
            warnings.warn(f"[fitting] {where}: exclude region {r!r} is not a [lo, hi] pair — dropping")
            continue
        if not lo < hi:
            warnings.warn(f"[fitting] {where}: exclude region {r!r} needs lo < hi — dropping")
            continue
        out.append((lo, hi))
    return tuple(out)


def _parse_peaks(v, *, where: str) -> tuple:
    """Parse `[{omega0=.., sigma=..}, ...]` into validated ({omega0, sigma}, ...). Order preserved.
    A peak missing/malformed `omega0`, or a non-positive `sigma`, warns-and-drops that peak."""
    if not v:
        return ()
    out = []
    for p in v:
        try:
            om = float(p["omega0"])
        except (TypeError, ValueError, KeyError):
            warnings.warn(f"[fitting] {where}: peak {p!r} needs a numeric omega0 — dropping")
            continue
        try:
            sig = float(p.get("sigma", 0.5))
        except (TypeError, ValueError):
            warnings.warn(f"[fitting] {where}: peak {p!r} has a non-numeric sigma — dropping")
            continue
        if not sig > 0:
            warnings.warn(f"[fitting] {where}: peak {p!r} needs sigma > 0 — dropping")
            continue
        out.append({"omega0": om, "sigma": sig})
    return tuple(out)


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
        fit_range=_parse_fit_range(e.get("fit_range"), where="expectation"),
        exclude_regions=_parse_exclude_regions(e.get("exclude_regions"), where="expectation"),
        peaks=_parse_peaks(e.get("peaks"), where="expectation"),
    )


def _parse_override(o: dict) -> Override | None:
    if "scan" not in o:
        return None
    where = f"override scan {o['scan']}"
    return Override(
        scan=int(o["scan"]),
        exclude=bool(o.get("exclude", False)),
        reason=o.get("reason"),
        omega0=(float(o["omega0"]) if "omega0" in o else None),
        omega0_sigma=float(o.get("omega0_sigma", 0.5)),
        structures=(tuple(o["structures"]) if o.get("structures") else None),
        fit_range=_parse_fit_range(o.get("fit_range"), where=where),
        exclude_regions=_parse_exclude_regions(o.get("exclude_regions"), where=where),
        peaks=_parse_peaks(o.get("peaks"), where=where),
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
    fit_range, exclude_regions = g.mask_regions_for(scan_number=scan_number, q=q, T=T)
    peaks = g.peaks_for(scan_number=scan_number, q=q, T=T)
    return {
        "excluded": g.excluded(scan_number),
        "prior": None if prior is None else [prior.omega0, prior.omega0_sigma],
        "structures": g.structure_labels_for(scan_number=scan_number, q=q, T=T),
        "omega_window": g.omega_window,
        "monitor_frac": g.monitor_frac,
        "min_fit_points": g.min_fit_points,
        "fit_range": None if fit_range is None else [fit_range[0], fit_range[1]],
        "exclude_regions": [[lo, hi] for lo, hi in exclude_regions],
        "peaks": [[p["omega0"], p["sigma"]] for p in peaks],
    }

"""Mode linking (C-link-core): which modes of neighbouring spectra are one branch, and what the neighbours suggest.

A pure core: it reads the fits in use through ``LinkInput`` and chooses links and starting values. Its scores
never enter a fit's likelihood or evidence (the summaries come from the same counts the fit uses, so that would
count the evidence twice). Stdlib only: no numpy, Qt or session import (``Link`` is duck-typed and imported
under ``TYPE_CHECKING``), so the session's records may import it at top level.

Per mode the core reads three summaries: the energy ``E*`` where the mode's intensity peaks (``peak_energy``;
an overdamped or broad mode is never matched by its omega0), the width ``matching_width`` and the intrinsic
intensity ``matching_intensity`` (the DHO amplitude: the stored Stokes area with the Bose factor integrated
across the line removed, times the analyzer scale). ``link`` joins modes with Kruskal over all candidate pairs
of the grid, reports contradictions and near ties, and lists per spectrum the predicted branches, the changes
(add, split, drop) and the markers. A mode index is the 0-based position in the fit's modes by ascending omega0.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, fields
from functools import lru_cache
from typing import TYPE_CHECKING, Literal

from isar.analyze.coupled.identity import new_branch_id
from isar.analyze.coupled.settings import InterSettings
from isar.analyze.coupled.types import Held

if TYPE_CHECKING:
    from isar.session.inter_spectra.graph import Link

LINK_SCHEMA = "isar.mode-links/1"
KB_MEV_PER_K = 0.086173324          # a test asserts == isar.analyze.lineshapes.KB (the core stays numpy-free)
FWHM_PER_GAMMA = 2.0                # a DHO line of damping Gamma has full width 2 Gamma


# ---- the three summaries -----------------------------------------------------------------------
def matching_width(gamma: float, fwhm: float) -> float:
    """The one width definition, meV: the line's full width and the resolution's, in quadrature."""
    return math.hypot(FWHM_PER_GAMMA * gamma, fwhm)


def _y(E: float, T: float | None) -> float:
    """``(n(E)+1)·E`` as ``lineshapes.bose_numerator`` gives it; the T -> 0 limit ``max(E, 0)`` for no usable T."""
    if T is None or not math.isfinite(T) or T <= 0:
        return max(E, 0.0)
    kT = KB_MEV_PER_K * T
    return kT if E == 0.0 else E / (-math.expm1(-E / kT))


def _check_mode(omega0: float, gamma: float) -> None:
    if not (math.isfinite(omega0) and math.isfinite(gamma) and omega0 > 0 and gamma > 0):
        raise ValueError(f"omega0 and Gamma must be finite and > 0 (got {omega0!r}, {gamma!r})")


def _golden(f, lo: float, hi: float, steps: int = 60) -> float:
    g = (math.sqrt(5.0) - 1.0) / 2.0
    c, d = hi - g * (hi - lo), lo + g * (hi - lo)
    fc, fd = f(c), f(d)
    for _ in range(steps):
        if fc > fd:
            hi, d, fd = d, c, fc
            c = hi - g * (hi - lo)
            fc = f(c)
        else:
            lo, c, fc = c, d, fd
            d = lo + g * (hi - lo)
            fd = f(d)
    return 0.5 * (lo + hi)


def peak_energy(omega0: float, gamma: float, T: float | None) -> float:
    """E*, meV: where the unconvolved Stokes DHO response ``omega0·Gamma·y(E)/((E²-omega0²)²+4Γ²E²)`` peaks.

    The response is ``lineshapes._unit_dho`` with its constants dropped. Search: 257 uniform samples on
    ``[0, omega0+5Γ]`` plus 33 samples around the maximum of 1/D, then a golden-section refinement between
    the best sample's neighbours."""
    _check_mode(omega0, gamma)
    o2, g2 = omega0 * omega0, gamma * gamma

    def f(E: float) -> float:
        return omega0 * gamma * _y(E, T) / ((E * E - o2) ** 2 + 4.0 * g2 * E * E)

    hi = omega0 + 5.0 * gamma
    ec = math.sqrt(max(o2 - 2.0 * g2, 0.0))
    h = max(gamma, 1e-6) / 4.0
    pts = {hi * k / 256.0 for k in range(257)}
    pts |= {min(max(ec + k * h, 0.0), hi) for k in range(-16, 17)}
    xs = sorted(pts)
    vals = [f(x) for x in xs]
    b = max(range(len(xs)), key=lambda k: vals[k])
    refined = _golden(f, xs[max(b - 1, 0)], xs[min(b + 1, len(xs) - 1)])
    return refined if f(refined) > vals[b] else xs[b]


def _gauss_legendre(n: int) -> tuple[tuple[float, ...], tuple[float, ...]]:
    xs, ws = [], []
    for k in range(1, n + 1):
        x = math.cos(math.pi * (k - 0.25) / (n + 0.5))
        for _ in range(100):
            p0, p1 = 1.0, x
            for j in range(2, n + 1):
                p0, p1 = p1, ((2 * j - 1) * x * p1 - (j - 1) * p0) / j
            dp = n * (x * p1 - p0) / (x * x - 1.0)
            dx = p1 / dp
            x -= dx
            if abs(dx) < 1e-15:
                break
        p0, p1 = 1.0, x
        for j in range(2, n + 1):
            p0, p1 = p1, ((2 * j - 1) * x * p1 - (j - 1) * p0) / j
        dp = n * (x * p1 - p0) / (x * x - 1.0)
        xs.append(x)
        ws.append(2.0 / ((1.0 - x * x) * dp * dp))
    return tuple(xs), tuple(ws)


_GL_X, _GL_W = _gauss_legendre(24)


def _integrate(f, a: float, b: float) -> float:
    mid, half = 0.5 * (a + b), 0.5 * (b - a)
    return half * sum(w * f(mid + half * x) for x, w in zip(_GL_X, _GL_W))


@lru_cache(maxsize=4096)
def unit_stokes_area(omega0: float, gamma: float, T: float | None) -> float:
    """The E > 0 area of the unconvolved Stokes DHO at amplitude 1: ``∫ (omega0/π)·4Γ·y(E) / ((E²-omega0²)² + 4Γ²E²) dE``.

    In the narrow limit it is ``n+1``. Composite Gauss-Legendre on intervals that are geometric in the distance
    from the two peak positions (so a Γ-wide peak and its Lorentzian tails are all resolved), then the tail to
    infinity by the substitution E = upper/u.
    shortcut: the convolved E > 0 area divided by this equals the DHO amplitude only while resolution moves no
    area across E = 0; exact A would need the fit context (height / up.height), upgrade if a mode near zero
    energy links wrongly."""
    _check_mode(omega0, gamma)
    o2, g2 = omega0 * omega0, gamma * gamma
    pref = omega0 / math.pi * 4.0 * gamma

    def s(E: float) -> float:
        return pref * _y(E, T) / ((E * E - o2) ** 2 + 4.0 * g2 * E * E)

    upper = 2.0 * (omega0 + 20.0 * gamma)
    ec = math.sqrt(max(o2 - 2.0 * g2, 0.0))
    cuts = {0.0, upper}
    for c in (ec, omega0):
        off = gamma / 8.0
        while off < upper:
            cuts.update(p for p in (c - off, c + off) if 0.0 < p < upper)
            off *= 2.0
    if T is not None and math.isfinite(T) and T > 0:
        kT = KB_MEV_PER_K * T
        cuts.update(kT * 2.0 ** k for k in range(-1, 5) if kT * 2.0 ** k < upper)
    edges = sorted(cuts)
    total = sum(_integrate(s, a, b) for a, b in zip(edges, edges[1:]))
    tail = lambda u: s(upper / u) * upper / (u * u)           # noqa: E731
    total += _integrate(tail, 0.0, 0.5) + _integrate(tail, 0.5, 1.0)
    if not (math.isfinite(total) and total > 0):
        raise ValueError(f"no finite Stokes area for omega0={omega0!r}, Gamma={gamma!r}, T={T!r}")
    return total


def matching_intensity(area: float | None, *, omega0: float, gamma: float, T: float | None,
                       scale: float = 1.0) -> float | None:
    """The one intensity definition: the phonon's intrinsic intensity, the DHO amplitude.

    ``scale · area / unit_stokes_area(omega0, gamma, T)``: the stored Stokes area with the Bose factor integrated
    across the line removed (a broad mode is corrected over its whole width, not at one energy) and the analyzer
    scale applied. None for an area that is None, nan, infinite or <= 0."""
    if area is None or not math.isfinite(area) or area <= 0:
        return None
    return scale * area / unit_stokes_area(float(omega0), float(gamma), None if T is None else float(T))


def fit_stamp(fit: str, inputs: str | None, modes: Iterable[tuple[float, float, float | None]]) -> str:
    """12 hex naming a fit's identity and its modes (omega0, Gamma, area), so a view can tell a changed fit."""
    rows = []
    for o, g, a in modes:
        a = None if a is None or not math.isfinite(a) else round(float(a), 9)
        rows.append([round(float(o), 9), round(float(g), 9), a])
    rows.sort(key=lambda r: (r[0], r[1], -math.inf if r[2] is None else r[2]))
    text = json.dumps([fit, inputs, rows], separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


# ---- the data ----------------------------------------------------------------------------------
@dataclass(frozen=True)
class LinkInput:                    # one spectrum as linking reads it; the session builds it
    held: Held                      # unit, fit, inputs, window, modes (ascending omega0), T
    fwhm: float                     # meV, the spectrum's resolution FWHM (> 0)
    areas: tuple[float | None, ...]         # area_stokes per mode, per reference monitor, as stored; None/<=0/nan = unknown
    sigma_areas: tuple[float | None, ...]   # sigma_area per mode (None = unavailable)
    sigma_gammas: tuple[float | None, ...]  # sigma_Gamma per mode (None = unavailable or a delta mode)
    at_cap: tuple[bool, ...]                # the fast pass's "at_width_cap" mark per mode
    stamp: str                              # fit_stamp(...) of the fit this input was made from
    scale: float = 1.0                      # areas are multiplied by it before comparing (1 / analyzer efficiency)
    anchored: bool = False                  # batch use: held.modes[i].branch are fixed ids


@dataclass(frozen=True)
class Branch:
    id: str                                  # "link:<unit>#<idx>" of the lowest-E* founding mode (+ "@<n>" if taken), or the anchor's id
    name: str                                # "B<n>", n monotonic over the experiment
    members: tuple[tuple[str, int], ...]     # (unit, mode index), sorted


@dataclass(frozen=True)
class Prediction:                            # what the NEIGHBOURS say a branch looks like in this spectrum
    branch: str
    mode: int | None                         # this spectrum's mode on the branch, None when it has none
    E: float                                 # peak energy E*, meV
    w: float                                 # width, meV (resolution included)
    A: float                                 # intrinsic intensity in THIS spectrum's own scale; 0.0 when no carrier's is known
    tol_E: float                             # meV, uncertainty of E
    from_units: tuple[str, ...]              # the neighbours that carry the branch, sorted


@dataclass(frozen=True)
class Change:
    kind: Literal["add", "split", "drop"]
    mode: int | None                         # split, drop: the mode; add: None
    branches: tuple[str, ...]                # add: (b,); split: (b1, b2) sorted; drop: ()
    predictions: tuple[Prediction, ...]      # add: (p,); split: (p1, p2); drop: ()


@dataclass(frozen=True)
class TieGroup:                              # one local near-tie between two linked spectra
    id: str
    kind: Literal["swap", "competition"]
    units: tuple[str, str]                   # the link's two spectra, link order
    modes: dict[str, tuple[int, ...]]
    branches: tuple[str, str]                # the two branch ids involved, sorted
    alternatives: dict[str, tuple[str | None, ...]]   # unit -> that spectrum's whole labelling under the alternative
    d_best: float
    d_alt: float


@dataclass(frozen=True)
class Contradiction:
    units: tuple[str, str]
    modes: tuple[int, int]                   # the refused pair: (unit, mode) of each end
    d: float
    reason: Literal["loop", "anchor"]        # loop: two modes of one spectrum on one branch; anchor: two anchor ids joined
    words: str


@dataclass(frozen=True)
class Marker:
    unit: str
    kind: Literal["change", "at_cap", "unlinked", "contradiction"]
    mode: int
    quantity: Literal["energy", "width", "intensity"] | None   # kind "change" only
    words: str                               # plain sentence with the numbers; the GUI shows it, never computes it


@dataclass(frozen=True)
class LinkResult:
    schema: str
    made_from: dict[str, tuple[str, str | None]]     # unit -> (fit id, inputs hash)
    stamps: dict[str, str]                           # unit -> LinkInput.stamp
    branches: tuple[Branch, ...]                     # ordered by name number
    labels: dict[str, tuple[str | None, ...]]        # unit -> branch id per mode; None = on no branch
    peak_E: dict[str, tuple[float, ...]]             # unit -> E* (meV) per mode; where a view draws a mode's link markers
    settled: dict[str, tuple[bool, ...]]             # per mode: on a branch, in no tie group, in no contradiction
    predictions: dict[str, tuple[Prediction, ...]]   # unit -> every branch a neighbour carries (sorted by branch id)
    changes: dict[str, tuple[Change, ...]]           # unit -> add/split/drop, sorted
    ties: tuple[TieGroup, ...]
    contradictions: tuple[Contradiction, ...]
    markers: tuple[Marker, ...]                      # sorted (unit, mode, kind, quantity)
    excluded: dict[str, tuple[str, str | None]]      # unit -> (plain reason it was not linked, token given by the session)
    settings: dict[str, object]                      # InterSettings.to_values() the result was made with
    name_start: int                                  # provenance only: first name number this run allocated
    next_name: int                                   # name_start + number of new branches


# ---- the algorithm -----------------------------------------------------------------------------
class _S:                                    # the summaries of one mode
    __slots__ = ("u", "i", "E", "w", "lw", "a", "la", "sE", "sw", "sA")


def _finite_pos(x) -> bool:
    return x is not None and math.isfinite(x) and x > 0


def _validate(inputs: Mapping[str, LinkInput], links: Sequence, next_name: int) -> None:
    if next_name < 1:
        raise ValueError(f"next_name must be >= 1 (got {next_name!r})")
    for u, inp in inputs.items():
        n = len(inp.held.modes)
        for name in ("areas", "sigma_areas", "sigma_gammas", "at_cap"):
            if len(getattr(inp, name)) != n:
                raise ValueError(f"{u}: {name} has {len(getattr(inp, name))} entries for {n} modes")
        if not (isinstance(inp.fwhm, (int, float)) and math.isfinite(inp.fwhm) and inp.fwhm > 0):
            raise ValueError(f"{u}: fwhm must be finite and > 0 (got {inp.fwhm!r})")
        if inp.anchored:
            ids = [m.branch for m in inp.held.modes]
            if len(set(ids)) != len(ids):
                raise ValueError(f"{u}: two modes of an anchored spectrum share an incoming branch id")
    for l in links:
        if l.a == l.b:
            raise ValueError(f"a link names the unit {l.a!r} twice")


def _summaries(u: str, inp: LinkInput) -> list[_S]:
    out = []
    T = inp.held.T
    for i, m in enumerate(inp.held.modes):
        s = _S()
        s.u, s.i = u, i
        try:
            s.E = peak_energy(m.omega0, m.gamma, T)
            s.a = matching_intensity(inp.areas[i], omega0=m.omega0, gamma=m.gamma, T=T, scale=inp.scale)
        except ValueError as e:
            raise ValueError(f"{u} mode {i}: {e}") from None
        s.w = matching_width(m.gamma, inp.fwhm)
        s.lw = math.log(s.w)
        s.la = None if s.a is None else math.log(s.a)
        s.sE = m.guard if _finite_pos(m.guard) else 0.0
        sg = inp.sigma_gammas[i]
        s.sw = (FWHM_PER_GAMMA ** 2 * m.gamma * sg / s.w ** 2) if _finite_pos(sg) else 0.0
        sa = inp.sigma_areas[i]
        s.sA = (sa / inp.areas[i]) if (s.a is not None and _finite_pos(sa)) else 0.0
        out.append(s)
    return out


def _cost(m: _S, n: _S, tolE: float, wt: float, at: float) -> float:
    d = (m.E - n.E) ** 2 / (tolE ** 2 + m.sE ** 2 + n.sE ** 2) + (m.lw - n.lw) ** 2 / (wt ** 2 + m.sw ** 2 + n.sw ** 2)
    if m.la is not None and n.la is not None:
        d += (m.la - n.la) ** 2 / (at ** 2 + m.sA ** 2 + n.sA ** 2)
    return d


def _mag(v) -> float:
    return math.sqrt(sum(x * x for x in v))


def _vec(l, x: str):
    """The displacement from ``x`` to the other end of link ``l`` (q links), None when the link has no dq."""
    if l.dq is None:
        return None
    return tuple(l.dq) if l.a == x else tuple(-c for c in l.dq)


def _cos(u, v) -> float | None:
    nu, nv = _mag(u), _mag(v)
    return None if nu == 0 or nv == 0 else sum(a * b for a, b in zip(u, v)) / (nu * nv)


def _skip_triples(units: Sequence[str], L: Sequence) -> list[tuple[str, object, str, object, str]]:
    """(left, link, middle, link, right) for two links of one kind on one line through the middle spectrum."""
    out = []
    at: dict[str, list] = {}
    for l in L:
        at.setdefault(l.a, []).append(l)
        at.setdefault(l.b, []).append(l)
    for x in units:
        ls = at.get(x, [])
        for k, l1 in enumerate(ls):
            for l2 in ls[k + 1:]:
                if l1.kind != l2.kind:
                    continue
                y1, y2 = (l1.b if l1.a == x else l1.a), (l2.b if l2.a == x else l2.a)
                if y1 == y2:
                    continue
                if l1.kind == "T":
                    # the colder side is the left end; a T link has a colder, b warmer
                    if l1.b == x and l2.a == x:
                        out.append((y1, l1, x, l2, y2))
                    elif l2.b == x and l1.a == x:
                        out.append((y2, l2, x, l1, y1))
                    continue
                v1, v2 = _vec(l1, x), _vec(l2, x)
                if v1 is None or v2 is None:
                    continue
                c = _cos(tuple(-c for c in v1), v2)      # L->X against X->R
                if c is not None and c > 0.5:
                    out.append((y1, l1, x, l2, y2))
    return out


class _DSU:
    def __init__(self, keys):
        self.p = {k: k for k in keys}
        self.units = {k: {k[0]} for k in keys}
        self.anchor: dict = {k: None for k in keys}

    def find(self, k):
        while self.p[k] != k:
            self.p[k] = self.p[self.p[k]]
            k = self.p[k]
        return k

    def union(self, a, b):
        self.p[b] = a
        self.units[a] |= self.units[b]
        self.anchor[a] = self.anchor[a] or self.anchor[b]


def _name_no(name: str) -> int:
    try:
        return int(name[1:])
    except ValueError:
        return 1 << 30


def link(inputs: Mapping[str, LinkInput], links: Sequence[Link], settings: InterSettings,
         previous: LinkResult | None = None, *, next_name: int = 1,
         excluded: Mapping[str, tuple[str, str | None]] | None = None) -> LinkResult:
    """Link the modes of ``inputs`` over the graph ``links``; see the module docstring and the design (section 2.2)."""
    _validate(inputs, links, next_name)
    units = sorted(inputs)
    S = {u: _summaries(u, inputs[u]) for u in units}
    L = sorted((l for l in links if l.a in inputs and l.b in inputs), key=lambda l: (l.kind, l.a, l.b))
    wt, at, thr, margin = (settings.width_tolerance_ln, settings.intensity_tolerance_ln,
                           settings.link_threshold, settings.tie_margin)
    tol = {id(l): settings.link_tolerance(l) for l in L}

    # 1  candidate pairs of every link, and the one-partner winners the skip rule needs
    cands = []                                  # (d, ua, i, ub, j, li, skip)
    pair_d: list[dict[tuple[int, int], float]] = []
    won: list[tuple[set, set]] = []
    for li, l in enumerate(L):
        pd = {}
        for m in S[l.a]:
            for n in S[l.b]:
                d = _cost(m, n, tol[id(l)], wt, at)
                if d <= thr:
                    pd[(m.i, n.i)] = d
                    cands.append((d, l.a, m.i, l.b, n.i, li, ""))
        pair_d.append(pd)
        wa, wb = set(), set()
        for (i, j), d in sorted(pd.items(), key=lambda kv: (kv[1], kv[0])):
            if i not in wa and j not in wb:
                wa.add(i); wb.add(j)
        won.append((wa, wb))
    li_of = {id(l): k for k, l in enumerate(L)}

    # 1b  skip pairs across one missing spectrum (a branch both neighbours on a line carry, the middle lacks)
    def free_modes(l, unit):
        wa, wb = won[li_of[id(l)]]
        return [s for s in S[unit] if s.i not in (wa if unit == l.a else wb)]

    for left, l1, x, l2, right in _skip_triples(units, L):
        tolE = tol[id(l1)] + tol[id(l2)]
        for m in free_modes(l1, left):
            for n in free_modes(l2, right):
                d = _cost(m, n, tolE, wt, at)
                if d <= thr:
                    a, b = (m, n) if left < right else (n, m)
                    cands.append((d, a.u, a.i, b.u, b.i, -1, x))
    cands.sort()

    # 2  Kruskal over components
    keys = [(u, s.i) for u in units for s in S[u]]
    dsu = _DSU(keys)
    by_id: dict[str, tuple[str, int]] = {}
    for u in units:
        if inputs[u].anchored:
            for i, m in enumerate(inputs[u].held.modes):
                k = (u, i)
                dsu.anchor[k] = m.branch
                if m.branch in by_id:
                    ra, rb = dsu.find(by_id[m.branch]), dsu.find(k)
                    if ra != rb:
                        dsu.union(ra, rb)
                else:
                    by_id[m.branch] = k
    direct: dict[tuple[int, int, int], tuple[int, float]] = {}
    accepted: list[list[tuple[int, int, float]]] = [[] for _ in L]
    contradictions: list[Contradiction] = []
    for d, ua, i, ub, j, li, skip in cands:
        if li >= 0 and ((li, 0, i) in direct or (li, 1, j) in direct):
            continue
        ra, rb = dsu.find((ua, i)), dsu.find((ub, j))
        if ra != rb:
            reason = None
            if dsu.units[ra] & dsu.units[rb]:
                reason, why = "loop", "would put two modes of one spectrum on one branch"
            elif dsu.anchor[ra] and dsu.anchor[rb] and dsu.anchor[ra] != dsu.anchor[rb]:
                reason, why = "anchor", "would join two different anchored branches"
            if reason:
                contradictions.append(Contradiction(
                    (ua, ub), (i, j), d, reason,
                    f"{ua} mode {i + 1} and {ub} mode {j + 1} match (mismatch {d:.2f}) but linking them {why}."))
                continue
            dsu.union(ra, rb)
        if li >= 0:
            direct[(li, 0, i)] = (j, d)
            direct[(li, 1, j)] = (i, d)
            accepted[li].append((i, j, d))

    # 3  branches = components of two or more modes, or with an anchor id
    comps: dict[tuple, list[tuple[str, int]]] = {}
    for k in keys:
        comps.setdefault(dsu.find(k), []).append(k)
    E_of = lambda k: S[k[0]][k[1]].E                      # noqa: E731
    branch_comps = []
    for root, mem in comps.items():
        if len(mem) >= 2 or dsu.anchor[root]:
            mem = sorted(mem)
            branch_comps.append((root, mem, sum(E_of(k) for k in mem) / len(mem), dsu.anchor[root]))
    branch_comps.sort(key=lambda c: (c[2], c[1][0]))

    # 4  names
    prev_by_id = {b.id: b for b in previous.branches} if previous else {}
    voters: dict[str, set] = {}
    if previous:
        unchanged = {u for u in units
                     if previous.made_from.get(u) == (inputs[u].held.fit, inputs[u].held.inputs)
                     and previous.stamps.get(u) == inputs[u].stamp}
        for p in previous.branches:
            v = {(u, i) for u, i in p.members if u in unchanged and i < len(inputs[u].held.modes)}
            v |= {(u, i) for u in units for i, m in enumerate(inputs[u].held.modes) if m.branch == p.id}
            voters[p.id] = v
    ident: dict[tuple, tuple[str, str | None]] = {}       # root -> (id, name or None while new)
    used_prev: set[str] = set()
    for root, mem, _, anchor in branch_comps:
        if anchor:
            p = prev_by_id.get(anchor)
            ident[root] = (anchor, p.name if p else None)
            if p:
                used_prev.add(p.id)
    scored = []
    for root, mem, meanE, anchor in branch_comps:
        if anchor:
            continue
        for pid, v in voters.items():
            score = len(set(mem) & v)
            if score > 0 and pid not in used_prev:
                scored.append((-score, _name_no(prev_by_id[pid].name), meanE, mem[0], root, pid))
    scored.sort(key=lambda t: t[:4] + (t[5],))
    matched: set = set()
    for _, _, _, _, root, pid in scored:
        if root in matched or pid in used_prev:
            continue
        matched.add(root)
        used_prev.add(pid)
        ident[root] = (pid, prev_by_id[pid].name)
    taken = set(prev_by_id) | {i for i, _ in ident.values()}
    k_new = 0
    for root, mem, _, anchor in branch_comps:               # ascending (mean E*, lowest member)
        if root in ident and ident[root][1] is not None:
            continue
        name = f"B{next_name + k_new}"
        if anchor:
            bid = anchor
        else:
            u0, i0 = min(mem, key=lambda k: (E_of(k), k))
            bid = new_branch_id(f"link:{u0}", i0)
            if bid in taken:
                bid += f"@{next_name + k_new}"
            while bid in taken:
                bid += "'"
            taken.add(bid)
        ident[root] = (bid, name)
        k_new += 1
    branches = tuple(sorted((Branch(ident[r][0], ident[r][1], tuple(mem)) for r, mem, _, _ in branch_comps),
                            key=lambda b: (_name_no(b.name), b.id)))

    # 5  labels
    label_of = {k: ident[dsu.find(k)][0] for k in keys if dsu.find(k) in ident}
    labels = {u: tuple(label_of.get((u, s.i)) for s in S[u]) for u in units}
    unsettled: set = set()

    # 6  near ties, over the direct accepted pairs of one link
    def lab(u, i):
        return labels[u][i]

    def relabel(u, changes: dict[int, str | None]):
        cur = list(labels[u])
        for i, b in changes.items():
            cur[i] = b
        return tuple(cur)

    groups = []                                           # (li, kind order, key..., TieGroup without id)
    for li, l in enumerate(L):
        acc = sorted(accepted[li])
        pd = pair_d[li]
        for x in range(len(acc)):
            for y in range(x + 1, len(acc)):
                (i, j, d1), (k, m, d2) = acc[x], acc[y]
                if j == m or (i, m) not in pd or (k, j) not in pd:
                    continue
                alt = pd[(i, m)] + pd[(k, j)]
                if alt <= d1 + d2 + margin:
                    br = tuple(sorted((lab(l.a, i), lab(l.a, k))))
                    mj = tuple(sorted((j, m)))
                    groups.append((li, 0, (i, k), TieGroup(
                        "", "swap", (l.a, l.b), {l.a: (i, k), l.b: mj}, br,
                        {l.a: relabel(l.a, {i: lab(l.a, k), k: lab(l.a, i)}),
                         l.b: relabel(l.b, {j: lab(l.b, m), m: lab(l.b, j)})}, d1 + d2, alt)))
        for i, j, d1 in acc:
            for side, mode, partner in ((0, i, j), (1, j, i)):
                mu, ou = (l.a, l.b) if side == 0 else (l.b, l.a)
                for (pi, pj), d2 in sorted(pd.items()):
                    rival, mine = (pj, pi) if side == 0 else (pi, pj)
                    if mine != mode or rival == partner or (li, 1 - side, rival) in direct:
                        continue
                    rb, mb = lab(ou, rival), lab(mu, mode)
                    if rb is None or rb == mb or abs(d2 - d1) > margin or rb in labels[mu]:
                        continue
                    groups.append((li, 1, (side, mode, rival), TieGroup(
                        "", "competition", (l.a, l.b), {mu: (mode,), ou: (partner, rival)},
                        tuple(sorted((mb, rb))),
                        {mu: relabel(mu, {mode: rb}), ou: relabel(ou, {partner: None})}, d1, d2)))
    groups.sort(key=lambda g: g[:3])
    ties = []
    for n, (_, _, _, g) in enumerate(groups, 1):
        t = TieGroup(f"t{n}", g.kind, g.units, dict(sorted(g.modes.items())), g.branches,
                     dict(sorted(g.alternatives.items())), g.d_best, g.d_alt)
        ties.append(t)
        for u, ms in t.modes.items():
            unsettled.update((u, m) for m in ms)
    for c in contradictions:
        unsettled.update(zip(c.units, c.modes))
    settled = {u: tuple(labels[u][s.i] is not None and (u, s.i) not in unsettled for s in S[u]) for u in units}

    # 7  predictions from the neighbours only
    nbrs: dict[str, list] = {u: [] for u in units}
    for l in L:
        nbrs[l.a].append((l, l.b))
        nbrs[l.b].append((l, l.a))
    preds: dict[str, list[tuple[Prediction, float, float | None]]] = {}
    for x in units:
        scale_x = inputs[x].scale
        carriers: dict[str, list] = {}
        for l, y in nbrs[x]:
            for s in S[y]:
                b = labels[y][s.i]
                if b is not None:
                    carriers.setdefault(b, []).append((y, s, l))
        out = []
        for b in sorted(carriers):
            cs = carriers[b]
            lines = []                                     # (E, tol^2)
            q = [c for c in cs if c[2].kind == "q" and _vec(c[2], x) is not None]
            rest = [c for c in cs if c not in q]
            pairs_ = []
            for p in range(len(q)):
                for r in range(p + 1, len(q)):
                    cv = _cos(_vec(q[p][2], x), _vec(q[r][2], x))
                    if cv is not None and cv < -0.5:
                        pairs_.append((cv, q[p][0], q[r][0], p, r))
            pairs_.sort()
            used = set()
            two = []
            for _, _, _, p, r in pairs_:
                if p not in used and r not in used:
                    used |= {p, r}
                    two.append((q[p], q[r], _mag(_vec(q[p][2], x)), _mag(_vec(q[r][2], x))))
            singles = [q[p] for p in range(len(q)) if p not in used]
            cold = sorted((c for c in rest if c[2].b == x), key=lambda c: c[0])
            warm = sorted((c for c in rest if c[2].a == x), key=lambda c: c[0])
            for cc, ww in zip(cold, warm):
                two.append((cc, ww, cc[2].step, ww[2].step))
            singles += cold[len(warm):] + warm[len(cold):]
            for (yp, sp, lp), (yq, sq, lq), vp, vq in two:
                wp, wq = vq / (vp + vq), vp / (vp + vq)
                lines.append((wp * sp.E + wq * sq.E,
                              (wp * sp.sE) ** 2 + (wq * sq.sE) ** 2 + max(tol[id(lp)], tol[id(lq)]) ** 2))
            for _, sp, lp in singles:
                lines.append((sp.E, sp.sE ** 2 + tol[id(lp)] ** 2))
            wsum = sum(1.0 / max(t2, 1e-12) for _, t2 in lines)
            E = sum(e / max(t2, 1e-12) for e, t2 in lines) / wsum
            lw = sum(c[1].lw for c in cs) / len(cs)
            las = [c[1].la for c in cs if c[1].la is not None]
            la = sum(las) / len(las) if las else None
            mode = next((i for i, v in enumerate(labels[x]) if v == b), None)
            A = math.exp(la) / scale_x if la is not None else 0.0
            out.append((Prediction(b, mode, E, math.exp(lw), A, math.sqrt(1.0 / wsum),
                                   tuple(sorted({c[0] for c in cs}))), lw, la))
        preds[x] = out

    # 8  changes (not for an anchored spectrum)
    changes: dict[str, tuple[Change, ...]] = {}
    has_nbr = {u: any(y in inputs for _, y in nbrs[u]) for u in units}
    for x in units:
        if inputs[x].anchored:
            changes[x] = ()
            continue
        fw = inputs[x].fwhm
        lo, hi = inputs[x].held.window
        ps = [p for p, _, _ in preds[x]]
        out, in_split = [], set()
        for s in S[x]:
            near = [p for p in ps if abs(p.E - s.E) <= fw and p.mode in (s.i, None)]
            for a in range(len(near)):
                for b in range(a + 1, len(near)):
                    pa, pb = sorted((near[a], near[b]), key=lambda p: p.branch)
                    out.append(Change("split", s.i, (pa.branch, pb.branch), (pa, pb)))
                    in_split |= {pa.branch, pb.branch}
        for p in ps:
            if p.mode is None and lo <= p.E <= hi and p.branch not in in_split:
                out.append(Change("add", None, (p.branch,), (p,)))
        if has_nbr[x]:
            out += [Change("drop", s.i, (), ()) for s in S[x] if labels[x][s.i] is None]
        order = {"add": 0, "split": 1, "drop": 2}
        out.sort(key=lambda c: (order[c.kind], -1 if c.mode is None else c.mode, c.branches))
        changes[x] = tuple(out)

    # 9  markers
    markers = []
    for x in units:
        sc = inputs[x].scale
        by_branch = {p.branch: (p, lw, la) for p, lw, la in preds[x]}
        for s in S[x]:
            b = labels[x][s.i]
            if inputs[x].at_cap[s.i]:
                markers.append(Marker(x, "at_cap", s.i, None, f"mode {s.i + 1} sits at the fast-pass width cap: "
                                      "it wants to be broader than a lone spectrum may claim."))
            if b is None:
                if has_nbr[x]:
                    markers.append(Marker(x, "unlinked", s.i, None,
                                          f"mode {s.i + 1} at {s.E:.2f} meV links to no neighbouring spectrum."))
                continue
            if b not in by_branch:
                continue
            p, lw, la = by_branch[b]
            if abs(s.E - p.E) > math.sqrt(p.tol_E ** 2 + s.sE ** 2):
                markers.append(Marker(x, "change", s.i, "energy",
                                      f"mode {s.i + 1} peaks at {s.E:.2f} meV; its neighbours predict "
                                      f"{p.E:.2f} ± {p.tol_E:.2f} meV."))
            if abs(s.lw - lw) > math.sqrt(wt ** 2 + s.sw ** 2):
                markers.append(Marker(x, "change", s.i, "width",
                                      f"mode {s.i + 1} has width {s.w:.2f} meV; its neighbours predict {p.w:.2f} meV."))
            if s.la is not None and la is not None and abs(s.la - la) > math.sqrt(at ** 2 + s.sA ** 2):
                markers.append(Marker(x, "change", s.i, "intensity",
                                      f"mode {s.i + 1} has intrinsic intensity {s.a / sc:.3g}; its neighbours "
                                      f"predict {p.A:.3g}."))
    for c in contradictions:
        for u, i in zip(c.units, c.modes):
            markers.append(Marker(u, "contradiction", i, None, c.words))
    markers.sort(key=lambda m: (m.unit, m.mode, m.kind, m.quantity or ""))

    return LinkResult(
        schema=LINK_SCHEMA,
        made_from={u: (inputs[u].held.fit, inputs[u].held.inputs) for u in units},
        stamps={u: inputs[u].stamp for u in units},
        branches=branches, labels=labels,
        peak_E={u: tuple(s.E for s in S[u]) for u in units},
        settled=settled,
        predictions={u: tuple(p for p, _, _ in preds[u]) for u in units},
        changes=changes, ties=tuple(ties),
        contradictions=tuple(sorted(contradictions, key=lambda c: (c.units, c.modes))),
        markers=tuple(markers),
        excluded={u: (str(v[0]), v[1]) for u, v in sorted((excluded or {}).items())},
        settings=settings.to_values(), name_start=next_name, next_name=next_name + k_new)


# ---- JSON --------------------------------------------------------------------------------------
def _plain(x):
    if hasattr(x, "__dataclass_fields__"):
        return {f.name: _plain(getattr(x, f.name)) for f in fields(x)}
    if isinstance(x, dict):
        return {str(k): _plain(v) for k, v in x.items()}
    if isinstance(x, (tuple, list)):
        return [_plain(v) for v in x]
    if isinstance(x, float) and not math.isfinite(x):
        raise ValueError(f"a linking result holds a non-finite number ({x!r}) that JSON cannot carry")
    return x


def to_json(r: LinkResult) -> dict:
    """The result as JSON-native data (tuples as lists); raises ValueError on a nan or infinity."""
    return _plain(r)


def _need(d, key: str, where: str = "link result"):
    try:
        return d[key]
    except (KeyError, TypeError):
        raise ValueError(f"{where}: missing key {key!r}") from None


def _tup(x):
    return tuple(x)


def _pred(d) -> Prediction:
    g = lambda k: _need(d, k, "prediction")           # noqa: E731
    return Prediction(g("branch"), g("mode"), g("E"), g("w"), g("A"), g("tol_E"), _tup(g("from_units")))


def from_json(d: dict) -> LinkResult:
    """Inverse of ``to_json``; ValueError naming the key on a wrong schema or a missing key."""
    schema = _need(d, "schema")
    if schema != LINK_SCHEMA:
        raise ValueError(f"link result: schema {schema!r} is not {LINK_SCHEMA!r}")
    g = lambda k: _need(d, k)                           # noqa: E731
    unit_map = lambda k, f: {u: f(v) for u, v in g(k).items()}   # noqa: E731
    return LinkResult(
        schema=schema,
        made_from=unit_map("made_from", _tup), stamps=dict(g("stamps")),
        branches=tuple(Branch(_need(b, "id", "branch"), _need(b, "name", "branch"),
                              tuple(_tup(m) for m in _need(b, "members", "branch"))) for b in g("branches")),
        labels=unit_map("labels", _tup), peak_E=unit_map("peak_E", _tup), settled=unit_map("settled", _tup),
        predictions=unit_map("predictions", lambda v: tuple(_pred(p) for p in v)),
        changes=unit_map("changes", lambda v: tuple(
            Change(_need(c, "kind", "change"), _need(c, "mode", "change"), _tup(_need(c, "branches", "change")),
                   tuple(_pred(p) for p in _need(c, "predictions", "change"))) for c in v)),
        ties=tuple(TieGroup(
            _need(t, "id", "tie"), _need(t, "kind", "tie"), _tup(_need(t, "units", "tie")),
            {u: _tup(v) for u, v in _need(t, "modes", "tie").items()}, _tup(_need(t, "branches", "tie")),
            {u: _tup(v) for u, v in _need(t, "alternatives", "tie").items()},
            _need(t, "d_best", "tie"), _need(t, "d_alt", "tie")) for t in g("ties")),
        contradictions=tuple(Contradiction(
            _tup(_need(c, "units", "contradiction")), _tup(_need(c, "modes", "contradiction")),
            _need(c, "d", "contradiction"), _need(c, "reason", "contradiction"),
            _need(c, "words", "contradiction")) for c in g("contradictions")),
        markers=tuple(Marker(_need(m, "unit", "marker"), _need(m, "kind", "marker"), _need(m, "mode", "marker"),
                             _need(m, "quantity", "marker"), _need(m, "words", "marker")) for m in g("markers")),
        excluded=unit_map("excluded", _tup), settings=dict(g("settings")),
        name_start=g("name_start"), next_name=g("next_name"))

"""Links, curvature terms and the batch scope of the inter-spectra graph (C-graph). Pure, stdlib only.

Coordinates arrive as ``QPoint`` (C-q-provider); the graph never reads a file. S1U4 fills the bodies.
"""
from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Literal

from isar.session.inter_spectra.layout import Grid
from isar.session.inter_spectra.nodes import NodeSet, SpectrumNode
from isar.session.inter_spectra.q_provider import QPoint

Kind = Literal["T", "q"]
Direction = Literal["q", "T", "qT"]


@dataclass(frozen=True)
class Link:
    kind: Kind
    a: str; b: str                                   # spectrum keys; T: a colder, b warmer; q: a before b in grid order (row-major)
    step: float                                      # T: K (>0). q: |q_b - q_a| in its scan's q metric (rlu without a lattice epoch, inverse angstrom with one) (>0)
    dq: tuple[float, float, float] | None            # q-links: q_b - q_a in the graph's q metric, else None


@dataclass(frozen=True)
class Term:
    kind: Literal["T-change", "q-change", "curvature"]
    members: tuple[str, ...]                         # change: (a, b); curvature: (left, centre, right) along the line
    spacings: tuple[float, ...]                      # change: (step,); curvature: (|c-l|, |r-c|) in the graph's q metric
    t_frac: float | None                             # curvature only: (c-l).(r-l)/|r-l|^2, position of the centre on the chord, ~0.5 equal spacing
    line: str | None                                 # curvature only: "row 2" / "col 5"


@dataclass(frozen=True)
class LinkGraph:
    nodes: dict[str, SpectrumNode]                   # scope members (eligible or not)
    metric: dict[str, Literal["hkl", "lattice"]]     # per scan with a q-link: "lattice" when every point of that scan has cart, else "hkl"; {} with no q-link
    links: tuple[Link, ...]                          # active links: both ends in scope, both eligible; sorted by (kind, a, b)
    triplets: tuple[Term, ...]                       # curvature terms that passed the collinearity test, sorted
    unlinked: dict[str, tuple[str, ...]]             # key -> reasons it lacks a kind of link ("q: no accepted hkl", "T: only scan of its series")
    notes: tuple[str, ...]                           # summary lines for the preview (bent triplets, q pairs without curvature, repeats)

    def links_of(self, key: str, kinds: frozenset[Kind] | None = None) -> tuple[Link, ...]:
        raise NotImplementedError("stub: filled by S1U4")


def build_graph(nodes: Mapping[str, SpectrumNode], ns: NodeSet, *, scope: Collection[str], kinds: frozenset[Kind],
                grid: Grid | None, coords: Mapping[str, QPoint], collinearity_tol: float) -> LinkGraph:
    raise NotImplementedError("stub: filled by S1U4")


def anchored_scope(anchors: Collection[str], direction: Direction, ns: NodeSet) -> tuple[frozenset[str], frozenset[Kind]]:
    raise NotImplementedError("stub: filled by S1U4")

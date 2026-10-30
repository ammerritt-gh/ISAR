"""The pure checks of the q side of the inter-spectra proof (``proofs/test_letacon_inter_spectra_q.py``).

Plain functions over a batch's recorded terms (the ``terms`` list of its batch_start line: dicts with ``kind``,
``members`` and, for a curvature term, ``line``) and over the analyzer grid. No pytest, no filesystem, no gate: the
routine suite imports this module and checks it on synthetic terms.
"""
from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence

from isar.session.inter_spectra.layout import Grid
from isar.session.inter_spectra.records import Flag
from proofs.inter_spectra_checks import Verdict, flats_are_quiet


def _split(key: str) -> tuple[str, str]:
    """A spectrum key ``<experiment>:<scan>:<detector>`` as (scan, detector)."""
    scan, _, det = key.rpartition(":")
    return scan, det


def curvature_structure_ok(terms: Sequence[Mapping], grid: Grid) -> Verdict:
    """Each curvature term's three spectra are one scan's, and run consecutively in order along the grid line the
    term names. So the centre has a neighbour on each side of that line: no term is centred on an end of a line."""
    lines = dict(grid.lines())
    curv = [t for t in terms if t["kind"] == "curvature"]
    bad = []
    for t in curv:
        members = list(t["members"])
        scans = {_split(m)[0] for m in members}
        dets = tuple(_split(m)[1] for m in members)
        line = lines.get(t["line"], ())
        i = line.index(dets[0]) if dets[0] in line else -1
        if len(scans) != 1 or i < 0 or line[i:i + 3] != dets:
            bad.append(f"{t['line']} {members}")
    return Verdict(not bad, f"{len(curv)} curvature terms, each three consecutive spectra of one line" if not bad
                   else f"{len(bad)} of {len(curv)} curvature terms are not three consecutive spectra of their line "
                        f"with the centre between its neighbours, first: {bad[0]}")


def links_join_one_axis(terms: Sequence[Mapping]) -> Verdict:
    """No link joins two spectra that differ in both q and T. A T-change joins one analyzer across two scans; a
    q-change or a curvature term stays in one scan and joins different analyzers."""
    bad = []
    for t in terms:
        keys = [_split(m) for m in t["members"]]
        scans, dets = {s for s, _ in keys}, {d for _, d in keys}
        if t["kind"] == "T-change":
            ok = len(scans) == len(keys) and len(dets) == 1
        else:
            ok = len(scans) == 1 and len(dets) == len(keys)
        if not ok:
            bad.append(list(t["members"]))
    return Verdict(not bad, f"{len(terms)} terms, each along one axis" if not bad
                   else f"{len(bad)} terms join spectra that differ in both q and T, first: {bad[0]}")


def groups_conflict_free(terms: Sequence[Mapping], groups: Sequence[Collection[str]]) -> Verdict:
    """No parallel group holds two members of one term: a refit reads every other member of its terms, so two of
    them cannot be refitted in the same group."""
    bad = []
    for gi, group in enumerate(groups):
        members = set(group)
        for t in terms:
            inside = [m for m in t["members"] if m in members]
            if len(inside) > 1:
                bad.append(f"group {gi} holds {inside}")
    return Verdict(not bad, f"{len(groups)} groups, none holds two members of one term" if not bad
                   else f"{len(bad)} conflicts, first: {bad[0]}")


def q_linked_spectra_named(not_coupled: Mapping[str, str], q_linked: Collection[str], in_term: Collection[str]) -> Verdict:
    """Every q-linked spectrum sits in a term, or the plan says why its q-linked set has no curvature term (the design's
    rule for a set with no curvature term, named in the batch summary). A silent drop of a linked spectrum is a fail."""
    bad = [k for k in q_linked if k not in in_term and "no curvature term" not in not_coupled.get(k, "")]
    return Verdict(not bad, f"{len(q_linked)} q-linked spectra: each in a term or named as having no curvature term"
                   if not bad else f"{len(bad)} q-linked spectra are in no term and unnamed, first: {bad[0]}")


def flats_quiet_on_every_spectrum(spectra: Mapping[str, tuple[float, Flag | None, Mapping]],
                                  flats: tuple[str, ...]) -> Verdict:
    """``flats_are_quiet`` on every spectrum of the batch, each with its own flag and its own matched modes, not only
    the expectation's analyzer. ``spectra``: key -> (T, the spectrum's flag, its modes matched at T)."""
    bad = []
    for key in sorted(spectra):
        T, flag, matches = spectra[key]
        v = flats_are_quiet({T: flag}, {T: matches}, flats)
        if not v.ok:
            bad.append(f"{key}: {v.why}")
    return Verdict(not bad, f"{len(spectra)} spectra, no reason above info on {', '.join(flats)}" if not bad
                   else f"{len(bad)} spectra flag a flat mode, first: {bad[0]}")

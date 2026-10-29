"""The pure checks of the inter-spectra real-data proof (``proofs/test_letacon_inter_spectra.py``).

Plain functions over the records a batch writes (``records.Flag``, the mode dicts of a refit's fit record) and over
numbers the proof reads. No pytest, no filesystem, no gate: the routine suite imports this module and checks it on
synthetic records. The moving and flat mode names and the tolerances come from the proof's expectation file.
"""
from __future__ import annotations

import itertools
import math
import statistics
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from isar.analyze.coupled.types import SEVERITIES
from isar.session.inter_spectra.records import Flag

SYMBOL = {"serious": "!", "warn": "?", "info": "i"}
COST_KEYS = ("assign_s", "likelihood_s", "continuous_s", "uncertainty_s", "release_s")


@dataclass(frozen=True)
class Verdict:
    ok: bool
    why: str


def match_modes(modes: list[dict], paper: Mapping[str, float], tol: float | None) -> dict[str, dict | None]:
    """Each paper phonon's nearest mode in undamped energy E0 (``omega0``), or None when none lies within ``tol``.
    ``tol`` None: half the smallest spacing between this temperature's paper phonons (one phonon: no limit)."""
    if tol is None:
        spacings = [abs(a - b) for a, b in itertools.combinations(paper.values(), 2)]
        tol = min(spacings) / 2 if spacings else math.inf
    out: dict[str, dict | None] = {}
    for name, e in paper.items():
        best = min(modes, key=lambda m: abs(m["omega0"] - e), default=None)
        out[name] = best if best is not None and abs(best["omega0"] - e) <= tol else None
    return out


def flags_on(flag: Flag | None, mode: Mapping | None, *, min_severity: str) -> list[dict]:
    """The reasons of a flag attributed to ``mode``'s branch (a reason names its branches in ``ids``), at or above
    ``min_severity``. A score reason names the spectrum, not a branch, so it is never attributed. A reason with
    ``ids`` None names no branch (the facade adds seam and drift reasons so), so it counts against every mode."""
    if flag is None or mode is None:
        return []
    floor = SEVERITIES.index(min_severity)
    return [r for r in flag.reasons if (r["ids"] is None or mode["branch"] in r["ids"])
            and SEVERITIES.index(r["severity"]) >= floor]


def _codes(reasons) -> str:
    return ", ".join(sorted({r["code"] for r in reasons}))


def moving_is_flagged(flags_by_T: Mapping[float, Flag | None], matches_by_T: Mapping[float, Mapping],
                      tol: float | None, *, moving: str = "B") -> Verdict:
    """The moving phonon is fine when the spectrum that holds it is flagged at ``serious`` or above (the flag is per
    spectrum: a score reason names the spectrum), or, only when ``tol`` is set, it is matched within ``tol`` and held
    by its neighbours at a temperature, or it is refitted within ``tol`` at every temperature. ``flags_by_T``: the
    holding spectrum's flag at each temperature."""
    floor = SEVERITIES.index("serious")
    for T in sorted(flags_by_T):
        flag = flags_by_T[T]
        hits = [r for r in (flag.reasons if flag is not None else ()) if SEVERITIES.index(r["severity"]) >= floor]
        if hits:
            return Verdict(True, f"the spectrum holding {moving} is flagged at {T:g} K: {_codes(hits)}")
    for T in sorted(matches_by_T):
        mode = matches_by_T[T].get(moving)
        if tol is not None and mode is not None and mode.get("mode_status") == "held_by_neighbours":
            return Verdict(True, f"{moving} is held by its neighbours and matched within {tol:g} meV at {T:g} K")
    if not any(matches_by_T[T].get(moving) for T in matches_by_T):
        return Verdict(False, f"{moving} is matched at no temperature and its spectrum is not flagged")
    if tol is not None and all(matches_by_T[T].get(moving) for T in matches_by_T):
        return Verdict(True, f"{moving} refitted within {tol:g} meV at every temperature")
    return Verdict(False, f"{moving} is not flagged on its spectrum, not held by its neighbours, and not refitted "
                          f"within tolerance")


def flats_are_quiet(events_by_T: Mapping[float, Flag | None], matches_by_T: Mapping[float, Mapping],
                    flats: tuple[str, ...] = ("A", "C", "D")) -> Verdict:
    """No reason above ``info`` on the branch of any flat phonon's matched mode, at any temperature."""
    for T in sorted(events_by_T):
        for name in flats:
            hits = flags_on(events_by_T[T], matches_by_T[T].get(name), min_severity="warn")
            if hits:
                return Verdict(False, f"flat mode {name} at {T:g} K carries {_codes(hits)}")
    return Verdict(True, f"no reason above info on {', '.join(flats)} at any temperature")


def accounting_ok(progress: Mapping[str, int]) -> Verdict:
    """done + failed + not_reached + fixed == total. ``not_reached`` must come from the batch's end line (its
    unreached list), not the residual of the progress counts, which always balances."""
    got = progress["done"] + progress["failed"] + progress["not_reached"] + progress["fixed"]
    return Verdict(got == progress["total"], f"done+failed+not_reached+fixed = {got}, total = {progress['total']}")


def timing_report(batch_view: Mapping, fast_round_s: float | None, cores: int, target_s: float = 1800.0) -> dict:
    """The batch's timing (ED8). ``batch_view``: ``wall_s``, ``rings``, ``sweeps`` and ``refit_timings``, the core's
    timing dict of each refit. Sums are core-seconds; ``remainder_core_s`` is wall x cores less the sums (preparing,
    shipping, saving, idle workers). Projection: mean refit x refits / cores, against ``target_s``, a target and
    never a limit."""
    cores = max(1, int(cores))
    refits = list(batch_view.get("refit_timings") or [])
    wall = float(batch_view.get("wall_s") or 0.0)
    sums = {k: sum(float(t.get(k, 0.0)) for t in refits) for k in COST_KEYS}
    totals = [sum(float(t.get(k, 0.0)) for k in COST_KEYS) for t in refits]
    report = {"wall_s": wall, "refits": len(refits), "rings": batch_view.get("rings"),
              "sweeps": batch_view.get("sweeps"), "cores": cores, "fast_round_s": fast_round_s,
              "split_core_s": {**sums, "remainder_core_s": wall * cores - sum(sums.values())},
              "refit_total_s": None, "projection_s": None, "target_s": target_s, "verdict": None}
    if totals:
        ordered = sorted(totals)
        report["refit_total_s"] = {"median": statistics.median(totals),
                                   "p90": ordered[min(len(ordered), math.ceil(0.9 * len(ordered))) - 1],
                                   "max": ordered[-1]}
        report["projection_s"] = statistics.fmean(totals) * len(totals) / cores
        report["verdict"] = ("under the target" if report["projection_s"] <= target_s else
                             f"over the {target_s:g} s target (a target, not a limit)")
    return report


def _cell(flag: Flag | None) -> str:
    if flag is None:
        return " "
    if not flag.flagged:
        return "."
    return SYMBOL[max((r["severity"] for r in flag.reasons), key=SEVERITIES.index, default="warn")]


def flag_map(flags_by_key: Mapping[tuple[float, str], Flag | None],
             position: Callable[[str], tuple[int, int] | None]) -> str:
    """One grid per temperature of the batch's flags: ``!`` serious, ``?`` warn, ``i`` info only, ``.`` not flagged,
    blank no fit. ``flags_by_key``: (T, analyzer) -> flag; ``position``: an analyzer's (row, col), e.g.
    ``layout.grid_for("sp8_bl43").position``."""
    placed = {(T, position(det)): flag for (T, det), flag in flags_by_key.items() if position(det) is not None}
    if not placed:
        return "(no analyzer lies on the grid)"
    n_rows = 1 + max(r for _, (r, _c) in placed)
    n_cols = 1 + max(c for _, (_r, c) in placed)
    out = []
    for T in sorted({t for t, _ in placed}):
        out.append(f"{T:g} K")
        for r in range(n_rows):
            out.append(" ".join(_cell(placed[(T, (r, c))]) if (T, (r, c)) in placed else " " for c in range(n_cols)))
    return "\n".join(out)

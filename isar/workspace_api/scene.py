"""The scene layer's one door into a workspace: persisted rows in, frozen DTOs out.

Read-only and re-fit-free like the rest of :mod:`isar.workspace_api` — everything here comes
from what ``results.csv`` and ``session.json`` already hold, through :class:`FitView`, which
already reconciles the two sources. Nothing in this module imports :mod:`isar.session` (the
writer), the scene store's write surface, or Qt; ``tests/scene/test_import_ban.py`` pins that.

The scoring contract (SCENE_DESIGN §2b) says local evidence enters ``ln W(S)`` **once per
observation arena**. That is not only a rule about factors: it is a rule about *which rows the
scene is allowed to see at all*, and it is enforced here, at the door — see
:func:`_superseded_by_masters`.
"""
from __future__ import annotations

import logging
from dataclasses import replace

from isar.scene.model import CandidateRow, SceneObservation, observation_fingerprint
from isar.schema.steering import CANONICAL_MASTER

log = logging.getLogger(__name__)


def scene_observations(ws) -> list[SceneObservation]:
    """Every currently-authoritative observation arena of ``ws``, sorted by ``scan_ref``.

    ``ws`` is an :class:`isar.workspace_api.Workspace`. One observation per delivered
    ``(ledger entry, detector)`` row that carries a candidate table, minus the rows a pooled
    master supersedes. A row with no parseable candidate table yields no observation (it has
    nothing to score) and is logged.

    Revisions need no handling here: ``results.csv`` is rewritten in full from the current
    ledger on every delivery (``isar/session/outcome.py:_write_results_csv``), so it holds
    current state only — an older revision of a row is not present to be preferred.
    """
    fits = list(getattr(ws, "results", ()) or ())
    # A results.csv written before the candidates column existed has nothing to score anywhere —
    # that is one fact about the file, not a per-row anomaly, so it is said once and at INFO.
    legacy_schema = bool(fits) and not any("candidates" in fit.columns for fit in fits)
    if legacy_schema:
        log.info("scene: this results.csv predates the candidates column — no observations")
    qualified, bare = _superseded_by_masters(fits)
    out: list[SceneObservation] = []
    for fit in fits:
        ref = _scan_ref(fit)
        if ref is None:
            log.warning("scene: row with no usable identity (scan=%r detector=%r) — skipped",
                        fit.scan_number, fit.detector_id)
            continue
        if not fit.pooled and (fit.unit_id in qualified or _bare_key(fit) in bare):
            # The master fitted the SAME neutrons this row did (combine-before-divide). Letting
            # both into the scene would enter one dataset's local evidence twice.
            continue
        candidates = _candidates(fit, ref, quiet=legacy_schema)
        if candidates is None:
            continue
        # A non-canonical sink was acquired to answer a narrower question; its evidence is barred
        # from the canonical pool, so it may not score. The arena still exists — the experiment map
        # must record that this scan looked here — so it is kept, footprint-bearing but empty.
        if (fit.evidence_sink or CANONICAL_MASTER) != CANONICAL_MASTER:
            candidates = ()
        obs = SceneObservation(
            scan_ref=ref,
            q=_q(fit),
            temperature_K=fit.T,
            energy_window=_window(fit),
            gauss_fwhm=fit.gauss_fwhm,
            lorentz_fwhm=fit.lorentz_fwhm,
            effective_exposure=fit.effective_exposure,
            background=_background(fit),
            best_label=fit.best_label,
            candidates=candidates,
            fingerprint="",
        )
        out.append(replace(obs, fingerprint=observation_fingerprint(obs)))
    out.sort(key=lambda o: o.scan_ref)
    return out


# ---------------------------------------------------------------------------
# ownership / active arena
# ---------------------------------------------------------------------------
def _superseded_by_masters(fits):
    """What pooled masters that actually carry candidates claim: ``(qualified, bare)`` label sets.

    A master's row IS the interpretation of its members' pooled counts, so its members' own rows
    are not a second, independent arena. A master with no candidate table has superseded nothing
    (it delivered no interpretation), so its members stay.

    Keys on what ``results.csv`` persists: the master row's ``members`` cell, written by
    ``isar/session/master_delivery.py:_member_labels`` as the full ledger key
    ``experiment:scan:detector`` — matched exactly against the member row's ``scan_id``. That is
    the whole rule for anything this ISAR wrote.

    A workspace written BEFORE that qualification carries bare scan numbers, which name no
    detector: the collation key is (sample, q, T) and may span detectors, so a bare label cannot
    say which detector's arena it meant. Those fall back to ``(scan number, sample)``, ignoring
    the detector — deliberately the OVER-suppressing direction, and warned about once per master.
    The asymmetry is the point: under-suppression lets one dataset's evidence enter ``ln W(S)``
    twice, violating the once-per-arena invariant (SCENE_DESIGN §2b) silently; over-suppression
    drops an arena, loudly. Neither direction is ever silent.

    Deliberately NOT part of the legacy key: ``h``/``k``/``l``/``T``. A master persists its
    GROUP's collated q and T (hkl_round_step / t_round), which need not equal a member row's own
    values, so an equality test there could fail spuriously — and a spurious failure is exactly
    the under-suppression that double-counts.
    """
    qualified: set[str] = set()
    bare: set[tuple] = set()
    for fit in fits:
        if not fit.pooled or not isinstance(fit.candidates, list):
            continue
        legacy = [str(m) for m in (fit.members or []) if ":" not in str(m)]
        qualified.update(str(m) for m in (fit.members or []) if ":" in str(m))
        bare.update((label, fit.sample) for label in legacy)
        if legacy:
            log.warning("scene: master %s carries pre-qualification member labels %s — they name "
                        "no detector, so every arena with those scan numbers and sample is "
                        "suppressed (dropping an arena is preferred to counting one twice)",
                        _scan_ref(fit), legacy)
    return qualified, bare


def _bare_key(fit) -> tuple:
    """How this per-scan row would be named by a legacy (unqualified) member label."""
    return (str(fit.scan_number), fit.sample)


# ---------------------------------------------------------------------------
# per-row field extraction
# ---------------------------------------------------------------------------
def _scan_ref(fit) -> str | None:
    """The arena key: the ledger ``scan_id`` (``exp:scan:det``), detector-qualified.

    Pooled masters are ledgered as ``master:<group key>`` with no detector segment, and one
    master can deliver several detectors — so the detector is appended when it is not already
    the last segment, or two arenas would collide on one key.
    """
    unit = fit.unit_id or None
    det = fit.detector_id or None
    if unit is None:
        if fit.scan_number is None:
            return None
        unit = str(fit.scan_number)
    if det and not str(unit).endswith(f":{det}"):
        return f"{unit}:{det}"
    return str(unit)


def _candidates(fit, ref: str, *, quiet: bool = False):
    """The row's candidate table as DTOs sorted by label, or ``None`` when there is none.

    The sort is the adapter's job: ``observation_fingerprint`` is candidate-order-sensitive by
    design, so a stable digest across rebuilds needs a fixed order fixed here.
    """
    raw = fit.candidates
    if raw is None:
        # A ledger row that never produced a fit (held_for_calibration, excluded, context_applied,
        # ingested — written detectorless, or delivered with nothing converged) legitimately has no
        # candidate table: a real campaign has many, and warning on each would bury the signal that
        # matters. Only a DELIVERED fit (detector + winning label) whose table is absent or
        # unparseable is anomalous, and that is what warns.
        if fit.detector_id and fit.best_label and not quiet:
            log.warning("scene: %s has no usable candidate table (absent or malformed cell) — "
                        "no observation", ref)
        else:
            log.debug("scene: %s carries no delivered fit — no observation", ref)
        return None
    try:
        return tuple(CandidateRow.from_dict(c) for c in sorted(raw, key=lambda c: str(c["label"])))
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        log.warning("scene: %s has a malformed candidate table (%s) — no observation", ref, exc)
        return None


def _q(fit):
    q = fit.q
    if not q or len(q) != 3 or any(v is None for v in q):
        return None
    return (float(q[0]), float(q[1]), float(q[2]))


def _window(fit):
    w = fit.energy_window
    if w is None or len(w) != 2 or any(v is None for v in w):
        return None
    return (float(w[0]), float(w[1]))


def _background(fit):
    """The delivered flat-background amplitude (``bg.BG`` — ``isar.analyze.components.FlatBG``)."""
    v = (fit.amplitudes or {}).get("bg.BG")
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None

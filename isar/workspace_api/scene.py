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

import json
import logging
from dataclasses import replace

from isar.scene.model import (CandidateRow, SampleKnowledge, SceneObservation,
                              observation_fingerprint)
from isar.schema.measurement import MeasurementArena
from isar.schema.steering import CANONICAL_MASTER

log = logging.getLogger(__name__)

# Both refusals below are one ruling (H1, no backwards-facing code): a persisted format this
# adapter cannot read is re-delivered, never guessed at. A tolerant fallback here would put a
# heuristic between the stored evidence and ln W(S), which is exactly what may not happen.
REDELIVER = "re-deliver or rebuild this workspace before SCENE analysis"

# SCENE-4 PR 4.0 (#26): the counts-authoritative measurement-arena log,
# isar.session.measurement_store.FILENAME — duplicated as a literal rather than imported, since
# isar.workspace_api may not import isar.session (hard rule 6).
MEASUREMENTS_FILENAME = "measurements.jsonl"

# The lattice-epoch cell fields, in the order ``SampleKnowledge.lattice`` records them.
_CELL = ("a", "b", "c", "alpha", "beta", "gamma")


def _read_measurements(ws) -> dict | None:
    """``{scan_ref: MeasurementArena}`` off ``report/measurements.jsonl``, last line per ref
    wins — or ``None`` when the file does not exist at all (an absent log, distinct from an empty
    one, which is what a fresh workspace with no delivered scan yet legitimately has).

    An unreadable line — bad JSON, or a record whose ``schema`` this reader does not recognize —
    refuses the whole workspace (H1): a partially-readable counts ledger is not a safe partial
    answer, the same posture as every other refusal in this module.
    """
    path = ws._report_dir / MEASUREMENTS_FILENAME
    if not path.is_file():
        return None
    out: dict[str, MeasurementArena] = {}
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            record = MeasurementArena.from_dict(json.loads(line))
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"scene: {path} line {lineno} is unreadable ({exc}) — "
                             f"{REDELIVER}") from exc
        out[record.scan_ref] = record          # last record per scan_ref wins
    return out


def _synthetic_observation(record: "MeasurementArena") -> SceneObservation:
    """A :class:`SceneObservation` for an arena that ``results.csv`` has no delivered-fit row
    for: a TRUSTED measurement with no usable interpretation yet (candidates empty — the scene
    still records "no_interpretation" and fills the experiment map), or a count-untrusted gate
    outcome (excluded / held_for_calibration / undersampled — recorded as attempted, scored as
    ``non_scoring`` with NO footprint, per :func:`isar.scene.build.build_scene`)."""
    obs = SceneObservation(
        scan_ref=record.scan_ref, q=record.q,
        temperature_K=dict(record.conditions).get("temperature_K"),
        energy_window=record.energy_window, energy_bands=record.energy_bands,
        gauss_fwhm=None, lorentz_fwhm=None, effective_exposure=record.effective_exposure,
        background=None, best_label=None, candidates=(),
        evidence_comparison_status="unavailable", selection_fallback=None, arena_fingerprint=None,
        evidence_sink=None, sample=record.sample, conditions=record.conditions,
        count_trust=record.count_trust, lattice_epoch=record.lattice_epoch, fingerprint="")
    return replace(obs, fingerprint=observation_fingerprint(obs))


def scene_observations(ws) -> list[SceneObservation]:
    """Every currently-authoritative observation arena of ``ws``, sorted by ``scan_ref``.

    ``ws`` is an :class:`isar.workspace_api.Workspace`. One observation per delivered
    ``(ledger entry, detector)`` row that carries a candidate table, minus the rows a pooled
    master supersedes, PLUS one synthetic observation (:func:`_synthetic_observation`) for every
    measurement-arena record (``report/measurements.jsonl``, SCENE-4 PR 4.0 #26) that never
    produced such a row — a trusted-but-fitless arena (its fit crashed, or converged to nothing),
    or a count-untrusted gate outcome, both of which the ledger writes with an EMPTY
    ``detectors`` dict and so never reach ``results.csv`` as a detector-qualified row at all.

    Raises ``ValueError`` on a workspace this adapter cannot read faithfully — a results.csv with
    no candidates column, a delivered fit whose candidate cell is absent or malformed (a corrupt
    interpretation artifact must not be demoted to the fitless lane — re-deliver instead),
    a master with unqualified member labels, a row predating the current
    delivery contract (:func:`_refuse_stale_delivery`), a delivered scan row with no matching
    measurement-arena record, or delivered candidates with no ``measurements.jsonl`` at all
    (the same H1 precedent as the missing-``energy_bands`` refusal: re-deliver). All of them mean
    re-deliver.

    Revisions need no handling here: ``results.csv`` is rewritten in full from the current
    ledger on every delivery (``isar/session/outcome.py:_write_results_csv``), so it holds
    current state only — an older revision of a row is not present to be preferred.
    """
    fits = list(getattr(ws, "results", ()) or ())
    # A results.csv written before the candidates column existed carries no candidate table for
    # any row, so nothing in it can be scored. That is a stale file, not an empty scene: reading
    # it as "no observations" would silently present a whole campaign as unmeasured.
    if fits and not any("candidates" in fit.columns for fit in fits):
        raise ValueError(f"scene: results.csv has no candidates column — {REDELIVER}")

    measurements = _read_measurements(ws)
    # Only a NON-POOLED candidate-bearing fit demands a measurements.jsonl: a pooled master
    # legitimately carries no measurement-arena record of its own (see below), so a workspace
    # whose only candidate-bearing rows are masters is not stale — it simply never had one.
    has_delivered_candidates = any(not fit.pooled and getattr(fit, "candidates", None) is not None
                                   for fit in fits)
    if measurements is None:
        if has_delivered_candidates:
            raise ValueError(f"scene: {ws._report_dir / MEASUREMENTS_FILENAME} does not exist "
                             f"but results.csv carries delivered candidates — {REDELIVER}")
        measurements = {}

    qualified, master_provenance = _superseded_by_masters(fits, measurements)
    out: list[SceneObservation] = []
    covered: set = set()
    for fit in fits:
        ref = _scan_ref(fit)
        if ref is None:
            log.warning("scene: row with no usable identity (scan=%r detector=%r) — skipped",
                        fit.scan_number, fit.detector_id)
            continue
        if not fit.pooled and fit.unit_id in qualified:
            # The master fitted the SAME neutrons this row did (combine-before-divide). Letting
            # both into the scene would enter one dataset's local evidence twice.
            continue
        candidates = _candidates(fit, ref)
        if candidates is None:
            continue
        covered.add(ref)
        record = measurements.get(ref)
        if record is None and not fit.pooled:
            # A pooled master never goes through isar.session.scan_delivery (it has its own
            # certification path, out of this work package's scope) so it legitimately carries no
            # measurement-arena record; a plain scan row does, always, once it is delivered — an
            # absent one means a pre-4.0 delivery or a partially-rebuilt log, not silence.
            raise ValueError(f"scene: {ref} carries no measurement-arena record — {REDELIVER}")
        if (record is not None and fit.content_hash is not None
                and record.content_hash != fit.content_hash):
            # A record from a LATER delivery generation must never be read against an earlier
            # fit row (or vice versa) — the join key is scan_ref AND content_hash, not scan_ref
            # alone. ``fit.content_hash is None`` (a row predating the column) skips the check:
            # there is nothing to compare against, not license to guess a mismatch.
            raise ValueError(f"scene: {ref} carries a measurement record from a different "
                             f"delivery generation (fit content_hash={fit.content_hash!r}, "
                             f"record content_hash={record.content_hash!r}) — {REDELIVER}")
        status, fallback, arena = _evidence_comparison(fit, ref)
        # Always parsed, whether or not a record ends up supplying the observation: this is what
        # catches a malformed window/bands CELL (raises) — a check that must not vanish just
        # because the record wins the value below.
        fit_window, fit_bands = _window(fit), _bands(fit)
        if record is not None:
            # Counts own the facts (SCENE-4 PR 4.0 review response, W3): a delivered fit row is a
            # disposable INTERPRETATION of a measurement the record already describes — the fit
            # row must not be allowed to silently override what was actually measured.
            obs_q, obs_T = record.q, dict(record.conditions).get("temperature_K")
            obs_window, obs_bands = record.energy_window, record.energy_bands
            obs_exposure = record.effective_exposure
            obs_sample, obs_conditions = record.sample, record.conditions
            obs_count_trust, obs_epoch = record.count_trust, record.lattice_epoch
        else:
            # A pooled master (the only case with no record — see above). Its raw axis is its
            # members' combined counts, which this adapter does not reconstruct, so q/window/
            # bands/exposure stay fit-row-derived; only sample/conditions/lattice_epoch are
            # provenance the members' OWN records can answer.
            obs_q, obs_T = _q(fit), fit.T
            obs_window, obs_bands = fit_window, fit_bands
            obs_exposure = fit.effective_exposure
            prov = master_provenance.get(ref)
            if prov is not None:
                obs_sample, obs_conditions, obs_epoch = prov
                obs_count_trust = "trusted"
            else:
                # Incompatible or unreconstructable member provenance (already logged by
                # _superseded_by_masters with its reason): the pooled evidence cannot be
                # presented under one frame, so this arena is non-scoring rather than guessed —
                # isar.scene.build folds any non-"trusted" count_trust into non_scoring with no
                # footprint (_split_by_evidence's caller in build_scene).
                obs_sample, obs_conditions, obs_epoch = None, (), None
                obs_count_trust = "excluded"
        obs = SceneObservation(
            scan_ref=ref,
            q=obs_q,
            temperature_K=obs_T,
            energy_window=obs_window,
            energy_bands=obs_bands,
            gauss_fwhm=fit.gauss_fwhm,
            lorentz_fwhm=fit.lorentz_fwhm,
            effective_exposure=obs_exposure,
            background=_background(fit),
            best_label=fit.best_label,
            candidates=candidates,
            evidence_comparison_status=status,
            selection_fallback=fallback,
            arena_fingerprint=arena,
            # Verbatim, including a sink this adapter does not recognize: classifying routing is
            # the builder's job, and a value rewritten here could not be quarantined there.
            evidence_sink=fit.evidence_sink,
            sample=obs_sample,
            conditions=obs_conditions,
            count_trust=obs_count_trust,
            lattice_epoch=obs_epoch,
            fingerprint="",
        )
        out.append(replace(obs, fingerprint=observation_fingerprint(obs)))

    for ref, record in measurements.items():
        if ref in covered or ref in qualified:
            # ``qualified``: a member a canonical master superseded. Its measurement was real and
            # trusted, but its ARENA is intentionally suppressed (combine-before-divide) — surfacing
            # it here as a synthetic "trusted, no interpretation" observation would double-count
            # exactly the neutrons the master already scored.
            continue
        out.append(_synthetic_observation(record))

    out.sort(key=lambda o: o.scan_ref)
    return out


def sample_knowledge(ws) -> SampleKnowledge:
    """What ``ws``'s experiment card DECLARES about the sample, for the scene layer.

    Two hand-authored policy keys and one machine-written state series, and the ownership split
    is the card's (hard rule 1) — this function READS all three and writes none of them:

    * ``policy.dispersion["zone_period"]`` — the reduced-zone fold period in rlu, the same key
      the steering layer's branch-assignment geometry reads. Absent ⇒ no zone geometry, and the
      ``zone_centre_approach`` factor is inert and says so;
    * ``policy.dispersion["max_slope_meV_per_rlu"]`` — the steepest |dω/dq| any branch of this
      sample is declared to have. Absent ⇒ track continuation has no declared reach, and the scene
      declines to charge a non-continuation rather than substituting the energy resolution for a
      dispersion bound (review finding 5);
    * ``policy.sample["reflections"]`` — the scientist's declared Bragg positions along the scan
      path, in the same axis rlu as an observation's q. It is a SEPARATE
      declaration from the period on purpose: a fold position is not a reflection (FCC (300) is
      integer-indexed, sits at a fold, and is extinct), so nothing here derives one from the
      other, from integer indices, or from a space group;
    * ``state.lattice_epochs`` — the highest-versioned epoch, recorded for provenance so a
      revision cannot be replayed against a different lattice interpretation without the
      fingerprint saying so.

    Malformed declarations raise rather than degrade (H1): a card that says something unreadable
    about the sample is a card to fix, and treating it as silence would leave the ledger claiming
    the capability was never asked for.
    """
    card = ws.card
    policy = card.policy
    dispersion = dict(getattr(policy, "dispersion", None) or {}) if policy else {}
    declared_sample = dict(getattr(policy, "sample", None) or {}) if policy else {}
    period = dispersion.get("zone_period")
    slope_max = dispersion.get("max_slope_meV_per_rlu")
    reflections = declared_sample.get("reflections") or ()
    epochs = [e for e in card.lattice_epochs
              if isinstance(e, dict) and e.get("version") is not None]
    try:
        # Ordered by version as a NUMBER, and inside the guard. A string version would otherwise
        # order lexically — "9" ahead of "10", so the newest epoch silently loses — and a mixed
        # series would raise a bare TypeError instead of the message this function promises.
        epoch = max(epochs, key=lambda e: int(e["version"]), default=None)
        cell = (epoch or {}).get("lattice")
        # All six cell parameters or nothing. An ABSENT cell is a declaration that was never made
        # and stays ``None``; a PARTIAL one is malformed and raises (H1) — degrading it to "no
        # lattice declared" would leave the revision claiming a provenance the card tried to give.
        if isinstance(cell, dict) and 0 < sum(k in cell for k in _CELL) < len(_CELL):
            raise ValueError(f"lattice cell declares {sorted(cell)}, expected all of {list(_CELL)}")
        return SampleKnowledge(
            zone_period=None if period is None else float(period),
            reflections=tuple(float(r) for r in reflections),
            dispersion_slope_max=None if slope_max is None else float(slope_max),
            lattice_epoch=None if epoch is None else int(epoch["version"]),
            lattice=None if not isinstance(cell, dict) or not cell
            else tuple(float(cell[k]) for k in _CELL))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"scene: card declares an unreadable sample geometry "
            f"(dispersion.zone_period={period!r}, "
            f"dispersion.max_slope_meV_per_rlu={slope_max!r}, "
            f"sample.reflections={declared_sample.get('reflections')!r}, "
            # The candidate list, not the chosen epoch: choosing is itself inside the guard now,
            # so a failure there leaves no chosen epoch to name.
            f"lattice epochs={epochs!r}): {exc}") from exc


# ---------------------------------------------------------------------------
# ownership / active arena
# ---------------------------------------------------------------------------
def _member_provenance(members: list, measurements: dict) -> tuple:
    """``((sample, conditions, lattice_epoch), None)`` when every member's measurement-arena
    record agrees, else ``(None, reason)``.

    A pooled master's own counts are its members' — so its provenance IS its members', never the
    current frame (SCENE-4 PR 4.0 review response, W3, disposition 5). Compatible means every
    member carries a record and all records agree on sample, conditions, AND lattice_epoch; any
    disagreement (mixed epochs, mixed conditions) or a missing record means the pooled evidence
    cannot be honestly presented under one frame.
    """
    if not members:
        return None, "no member labels to derive provenance from"
    missing = [m for m in members if measurements.get(m) is None]
    if missing:
        return None, f"member(s) {sorted(missing)} carry no measurement-arena record"
    records = [measurements[m] for m in members]
    if (len({r.sample for r in records}) > 1 or len({r.conditions for r in records}) > 1
            or len({r.lattice_epoch for r in records}) > 1):
        return None, "members disagree on sample/conditions/lattice_epoch"
    r0 = records[0]
    return (r0.sample, r0.conditions, r0.lattice_epoch), None


def _superseded_by_masters(fits, measurements: dict) -> tuple:
    """``(qualified, provenance)``: the member labels CANONICAL pooled masters claim, and the
    derived ``(sample, conditions, lattice_epoch)`` for each COMPATIBLE one, keyed by the
    master's own ref.

    A master's row IS the interpretation of its members' pooled counts, so its members' own rows
    are not a second, independent arena — UNLESS the members' own measurement records disagree
    with each other (or are missing), in which case the pooled interpretation is not honestly
    presentable under one frame: the master is left OUT of ``provenance`` (its arena degrades to
    non-scoring, see :func:`scene_observations`) and its members are left OUT of ``qualified`` —
    an incompatible master must not both fail AND erase the only evidence its members have.

    Three conditions gate whether a master is even a candidate to supersede anyone, each naming
    what it prevents:

    * carries a candidate table — a master with none delivered no interpretation and has
      superseded nothing, so its members stay;
    * canonical sink — a master barred from the canonical pool did not become the authoritative
      reading of those neutrons, so erasing its members' arenas would delete the only evidence
      the scene has for them (the barred master is still kept as an arena of its own, and the
      builder is what declines to score it);
    * fully-qualified member labels — the ``members`` cell as
      ``isar/session/master_delivery.py:_member_labels`` writes it, the full ledger key
      ``experiment:scan:detector``, matched exactly against the member row's ``scan_id``.

    A bare label names no detector, and the collation key is (sample, q, T) which may span
    detectors — so it cannot say which detector's arena it meant. There is no heuristic for that
    and no tolerant path: guessing under-suppresses (one dataset's evidence enters ``ln W(S)``
    twice) or over-suppresses (an arena silently vanishes). Such a workspace is refused.
    """
    qualified: set[str] = set()
    provenance: dict[str, tuple] = {}
    for fit in fits:
        if not fit.pooled or not isinstance(fit.candidates, list):
            continue
        if (fit.evidence_sink or CANONICAL_MASTER) != CANONICAL_MASTER:
            continue
        members = [str(m) for m in (fit.members or [])]
        bare = [m for m in members if m.count(":") < 2]
        if bare:
            raise ValueError(
                f"scene: master {_scan_ref(fit)} carries unqualified member label(s) {bare} "
                f"(expected experiment:scan:detector) — {REDELIVER}")
        derived, reason = _member_provenance(members, measurements)
        if derived is None:
            log.warning("scene: master %s members are provenance-incompatible (%s) — it does "
                        "not supersede them and will not score under the current frame",
                        _scan_ref(fit), reason)
            continue
        qualified.update(members)
        provenance[_scan_ref(fit)] = derived
    return qualified, provenance


def _evidence_comparison(fit, ref: str):
    """``(status, selection_fallback, arena_fingerprint)`` from the persisted selection record.

    H7: an evidence comparison whose escalation budget expired with a contender unresolved is
    INCOMPLETE — local selection refuses a Bayesian winner and records
    ``selection_fallback="evidence_budget_exhausted"``. The surviving lnZ set is then not the
    arena, so a scene posterior built from it would be a confident answer to a question the
    comparison never finished; only the Bayesian lane with no fallback may score.

    The mapping is exhaustive over ``isar.schema.statistics.SELECTION_FALLBACKS``
    (``aic_no_evidence_inputs`` / ``aic_all_evidence_invalid`` / ``evidence_budget_exhausted``)
    and fails closed: anything unrecognized — a fallback this reader does not know, an absent or
    unreadable record, the recorded-AIC lane, ``selection_method="none"`` — is "unavailable",
    never "complete".
    """
    record = fit.fit_selection_semantics
    if not record:
        return "unavailable", None, None
    fallback = record.get("selection_fallback")
    arena = record.get("evidence_arena_fingerprint")
    if record.get("selection_method") == "laplace_evidence_selection" and fallback is None:
        return "complete", None, arena
    if fallback == "evidence_budget_exhausted":
        # The AIC lane it fell back to is a within-arena diagnostic, not a comparison the scene
        # may re-read as evidence: the missing contender's lnZ was never computed at all.
        return "incomplete", fallback, arena
    if fallback is not None and fallback not in ("aic_no_evidence_inputs",
                                                 "aic_all_evidence_invalid"):
        log.warning("scene: %s records an unrecognized selection_fallback %r — treated as "
                    "unavailable evidence", ref, fallback)
    return "unavailable", fallback, arena


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


def _candidates(fit, ref: str):
    """The row's candidate table as DTOs sorted by label, or ``None`` when there is none.

    The sort is the adapter's job: ``observation_fingerprint`` is candidate-order-sensitive by
    design, so a stable digest across rebuilds needs a fixed order fixed here.
    """
    raw = fit.candidates
    if raw is None:
        # A ledger row that never produced a fit (held_for_calibration, excluded, context_applied,
        # ingested — written detectorless, or delivered with nothing converged) legitimately has no
        # candidate table: a real campaign has many, and warning on each would bury the signal that
        # matters. A DELIVERED per-scan fit (detector + winning label) whose table is absent is a
        # corrupt interpretation artifact — a different case that must not share the fitless lane:
        # demoting it to "no interpretation" would silently drop local evidence a clean re-delivery
        # restores (#26; the same H1 refuse-don't-degrade posture as _refuse_stale_delivery). A
        # candidateless POOLED MASTER stays the tolerated SCENE-3 contract (supersedes nothing,
        # members keep scoring) — master candidate persistence is a separate pipeline and a named
        # future work package.
        if fit.detector_id and fit.best_label:
            if not fit.pooled:
                raise ValueError(f"scene: {ref} is a delivered fit with no candidate table — the "
                                 f"interpretation artifact is corrupt; re-deliver this scan "
                                 f"before building a scene")
            log.warning("scene: master %s carries no candidate table — it supersedes nothing "
                        "and contributes no observation", ref)
            return None
        log.debug("scene: %s carries no delivered fit — no observation", ref)
        return None
    if not raw and not fit.pooled and fit.detector_id and fit.best_label:
        # A DELIVERED non-pooled winner (detector + best_label) whose candidates cell parses to
        # an EMPTY list is contradictory, not fitless: something won, so a candidate table must
        # exist. Demoting it to "no interpretation" would silently drop local evidence a clean
        # re-delivery restores — the same refuse-don't-degrade posture as the absent/malformed
        # cases just above.
        raise ValueError(f"scene: {ref} is a delivered fit with an empty candidate table — the "
                         f"interpretation artifact is contradictory; re-deliver this scan "
                         f"before building a scene")
    _refuse_stale_delivery(fit, ref, raw)
    try:
        return tuple(CandidateRow.from_dict(c) for c in sorted(raw, key=lambda c: str(c["label"])))
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"scene: {ref} has a malformed candidate table ({exc}) — the "
                         f"interpretation artifact is corrupt; re-deliver this scan before "
                         f"building a scene") from exc


def _refuse_stale_delivery(fit, ref: str, raw) -> None:
    """Raise unless ``fit`` carries the current delivery contract's scene inputs.

    Two fields, one refusal, because a row missing either was written by a delivery that predates
    this contract and there is no honest way to fill in what it did not record:

    * ``candidates[].components`` — what each candidate is MADE OF. Recovering it from the label
      text is what this contract exists to stop: a display string is an identifier, and a scene
      that parses it decides physics on a grammar nobody promised to keep.
    * ``energy_bands`` — the contiguous kept runs behind ``energy_window``. Reading the envelope
      alone would present the QC-masked holes inside it as searched coverage, so an absence in a
      hole would be priced as evidence.

    Refused rather than degraded, per the standing no-legacy rule (H1): the same posture as the
    unqualified-member-label and missing-candidates-column refusals above.
    """
    stale = []
    # ``"label" in c`` keeps this about STALE rows: a cell that is not a candidate table at all is
    # malformed, and the parse path below logs it as such rather than blaming the contract.
    if isinstance(raw, list) and any(isinstance(c, dict) and "label" in c and "components" not in c
                                     for c in raw):
        stale.append("candidate rows without component descriptors")
    if fit.energy_window is not None and fit.energy_bands is None:
        stale.append("an energy window with no energy_bands")
    if stale:
        raise ValueError(f"scene: {ref} predates the current delivery contract "
                         f"({'; '.join(stale)}) — {REDELIVER}")


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


def _bands(fit):
    """The kept energy runs as ``((lo, hi), …)``, or ``None`` when the row records none.

    ABSENT and MALFORMED are two different diagnoses and get two different errors. Absent (``None``)
    is a row that predates the field, which :func:`_refuse_stale_delivery` answers with re-deliver.
    Present-but-unreadable — a cell that failed to parse as JSON arrives here as the raw string —
    is a CORRUPT row, and telling its owner to re-deliver a stale workspace would send them after
    the wrong problem.
    """
    bands = fit.energy_bands
    if bands is None:
        return None
    try:
        if not isinstance(bands, list):
            raise TypeError(f"expected a list of [lo, hi] pairs, got {type(bands).__name__}")
        return tuple((float(b[0]), float(b[1])) for b in bands)
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"scene: {_scan_ref(fit)} has a malformed energy_bands cell "
                         f"({bands!r}): {exc}") from exc


def _background(fit):
    """The delivered flat-background amplitude (``bg.BG`` — ``isar.analyze.components.FlatBG``)."""
    v = (fit.amplitudes or {}).get("bg.BG")
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None

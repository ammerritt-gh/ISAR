"""PROOF (real data, run on purpose): the q side of the inter-spectra pass on the le Tacon BL43LXU CsV3Sb5 spectra.

Gated like the other le Tacon proofs: the module is skipped unless ``ISAR_LETACON_INTER=1``. Run it with
``tools/run-isar-proofs.bat inter`` (every ``proofs/test_letacon_inter_spectra*.py``) or

    ISAR_LETACON_INTER=1 python -m pytest proofs/test_letacon_inter_spectra_q.py -q -s

The 302 K and 97 K sets go through init, ingest and the fast pass as one workspace (56 spectra, the S1 proof's
fixture). Then the batches run through the analysis service as the GUI starts them:

1. Preconditions, named failures: every spectrum has an accepted hkl; the qT graph has q-links and curvature terms.
   The curvature term is the one that fails at the current default collinearity tolerance (the tolerance is the
   operator's to rule); the failure names it and prints the tolerance in use.
2. Scope q: an anchored batch spread in q from one 302 K analyzer. The 302 K-only simultaneous batch is not run: it has
   no links at the default collinearity tolerance, so it cannot start (design/inter_spectra.md section 9).
3. Scope qT: one simultaneous batch of all 56 spectra.
4. Checks: accounting per batch; the fast solo fits and results.csv untouched; the recorded terms of the qT batch
   (curvature lines, one axis per link, parallel groups conflict-free, every q-linked spectrum in a term or named);
   the moving mode flagged or held, the flat modes quiet on every spectrum, with q-links on; the operator's named
   kink analyzers flagged; the timing. The q anchored batch runs after every check of the qT batch, so the verdicts
   judge the qT batch's authoritative fits.

The 30-minute figure is a target: timing is printed and written to ``summary.json``, never asserted. Numbers the
operator sets in the expectation file the S1 proof reads (``real_data/leTacon_SP8_CsV3Sb5/`` and its
``test_letacon_inter_spectra.expect.json``, not committed) are asserted only when not null: ``min_q_links``,
``min_curvature_triplets``, ``q_kink_analyzers``. A missing real file or expectation key is an error naming it,
never a skip.
Results land in ``out/letacon_inter_spectra_q/<timestamp>/`` (gitignored).
"""
import json
import os
from datetime import datetime

import pytest

if os.environ.get("ISAR_LETACON_INTER") != "1":
    pytest.skip("the inter-spectra q proof runs only when named: tools/run-isar-proofs.bat inter "
                "(sets ISAR_LETACON_INTER=1)", allow_module_level=True)

from isar.intake import inter_values                                    # noqa: E402
from isar.intake.inter_spectra import read_inter_settings               # noqa: E402
from isar.schema.card import load_policy                                # noqa: E402
from isar.session.inter_spectra import records, runner                 # noqa: E402
from isar.session.inter_spectra.graph import anchored_scope            # noqa: E402
from isar.session.inter_spectra.layout import grid_for                 # noqa: E402
from isar.session.inter_spectra.nodes import read_nodes                # noqa: E402
from isar.session.inter_spectra.preview import preview_anchored, preview_simultaneous  # noqa: E402
from isar.session.inter_spectra.q_provider import q_coordinates        # noqa: E402
from isar.session.inter_spectra.rings import plan_anchored             # noqa: E402
from isar.session.inter_spectra.schedule import plan_simultaneous      # noqa: E402
from isar.session.workspace import POLICY_NAME                         # noqa: E402
from isar.workspace_api.inter_spectra import inter_overview, is_flagged  # noqa: E402
from proofs import inter_spectra_checks as chk                         # noqa: E402
from proofs import inter_spectra_q_checks as qc                        # noqa: E402
from proofs.letacon_common import (BATCH_WORKERS, N_ANALYZERS, ROOT, TEMPERATURES, _fits, _timings)  # noqa: E402
# The S1 proof's helpers and fixtures: the service protocol, the graph build, the flag reading, the workspace fast pass.
from proofs.test_letacon_inter_spectra import (EXPECT, TARGET_S, _Service, _as_flag, _authoritative_line,  # noqa: E402
                                               _build, _key, _peaks_of, _run_batch, _snapshot, real, ws)  # noqa: F401

# The batches run for as long as they take; each wait is limited by the service's WAIT_LIMIT_S, as in the S1 proof.
pytestmark = pytest.mark.timeout(0)

Q_T = 302                               # the 302 K set: the q scope's temperature
REQUIRED = ("analyzer", "moving_mode", "flat_modes", "paper")
RESULTS: dict = {}                      # filled by the tests in file order; the last test writes summary.json


# ---------------------------------------------------------------- fixtures
@pytest.fixture(scope="module")
def expectation(real):                  # the S1 proof's ``real``: (folder, rf)
    path = real[0] / EXPECT
    if not path.is_file():
        pytest.fail(f"proof needs its expectation file, which is missing: {path}", pytrace=False)
    exp = json.loads(path.read_text(encoding="utf-8"))
    missing = [k for k in REQUIRED if k not in exp]
    if missing:
        pytest.fail(f"the expectation file {path} has no key {missing}", pytrace=False)
    return exp


@pytest.fixture(scope="module")
def out_dir():
    path = ROOT / "out" / "letacon_inter_spectra_q" / datetime.now().strftime("%Y%m%d-%H%M%S")
    path.mkdir(parents=True)
    print(f"\nle Tacon inter-spectra q proof output: {path}")
    return path


@pytest.fixture(scope="module")
def nodes(ws):
    return read_nodes(ws)


@pytest.fixture(scope="module")
def service(ws, out_dir):
    svc = _Service(ws, out_dir / "service.stderr.log")
    yield svc
    svc.close()


@pytest.fixture(scope="module")
def q_anchor(ws, nodes, expectation):
    key = _key(nodes, Q_T, expectation["analyzer"])
    saved = inter_values.set_anchors(ws, [key], anchored=True)
    assert saved.ok, saved
    print(f"\nq anchor: the {expectation['analyzer']} spectrum at {Q_T} K, written through the intake anchor writer")
    return key


@pytest.fixture(scope="module")
def q_anchored_run(snapshot, service, ws, q_anchor, nodes):
    scope, kinds = anchored_scope((q_anchor,), "q", nodes)
    g = _build(ws, nodes, scope, kinds)
    pv = preview_anchored(g, plan_anchored(g, (q_anchor,), set()), "q")
    print(f"q anchored preview: {pv.text}")
    for note in pv.notes:
        print(f"  note: {note}")
    assert pv.can_start, f"the q anchored batch cannot start: {pv.blocked}"
    spec = runner.BatchSpec(records.new_batch_id(), "anchored", "q", (q_anchor,), tuple(sorted(g.nodes)))
    return _run_batch(service, "inter-q-anchored", spec, "q anchored")


@pytest.fixture(scope="module")
def qt_run(snapshot, service, ws, nodes, q_anchor):     # the q anchor is set first, so this batch sees it as before
    keys = tuple(sorted(nodes.nodes))
    assert len(keys) == len(TEMPERATURES) * N_ANALYZERS, f"the import record holds {len(keys)} spectra"
    g = _build(ws, nodes, frozenset(keys), frozenset({"q", "T"}))
    pv = preview_simultaneous(g, plan_simultaneous(g, set()))
    print(f"qT simultaneous preview: {pv.text}")
    for note in pv.notes:
        print(f"  note: {note}")
    assert pv.can_start, f"the qT simultaneous batch cannot start: {pv.blocked}"
    spec = runner.BatchSpec(records.new_batch_id(), "simultaneous", None, (), keys)
    return _run_batch(service, "inter-qT-simultaneous", spec, "qT simultaneous")


@pytest.fixture(scope="module")
def snapshot(ws):
    """Taken before the first batch: the frozen Stage II/III contract the batches must leave alone."""
    return _snapshot(ws)


# ---------------------------------------------------------------- helpers
def _graph_qT(ws, nodes):
    return _build(ws, nodes, frozenset(nodes.nodes), frozenset({"q", "T"}))


def _timing_view(ws, batch) -> dict:
    timings = []
    for line in records.read_log(ws).refits:
        if line.batch == batch.spec.batch_id and line.payload is not None:
            came_out = (records.read_payload(ws, line) or {}).get("came_out") or {}
            if isinstance(came_out.get("timing"), dict):
                timings.append(came_out["timing"])
    return {"wall_s": batch.end["timing"]["wall_s"], "rings": batch.end.get("rings_done"),
            "sweeps": batch.end.get("sweeps"), "refit_timings": timings, "timing": batch.end["timing"]}


def _judged_spectra(ws, nodes, exp) -> dict:
    """Every spectrum of the qT batch: key -> (T, its flag as the facade shows it, its modes matched to the paper's
    phonons at its temperature), read from its authoritative refit (its fast solo fit when none stands)."""
    log, by_key, fast = records.read_log(ws), inter_overview(ws).by_key, _fits(ws)
    tol = exp.get("match_tolerance_meV")
    out = {}
    for key, n in nodes.nodes.items():
        T = round(n.T)
        line = _authoritative_line(log, by_key, key)
        paper = {name: ref["omega0"] for name, ref in exp["paper"][str(T)].items()}
        matches = chk.match_modes(_peaks_of(ws, line, fast[(T, n.detector)][1]), paper, tol)
        out[key] = (T, _as_flag(by_key[key].flag, line) if key in by_key else None, matches)
    return out


# ---------------------------------------------------------------- tests
def test_precondition_every_spectrum_has_an_accepted_hkl(ws, nodes):
    coords = q_coordinates(ws, list(nodes.nodes))
    missing = {k: coords.reasons.get(k, "no point") for k in nodes.nodes if k not in coords.points}
    print(f"\naccepted hkl: {len(coords.points)} of {len(nodes.nodes)} spectra; "
          f"lattice epochs in force for {len(coords.lattices)} scan(s)")
    assert not missing, (f"{len(missing)} spectra have no accepted hkl, so no q-link can form on them; "
                         f"first: {sorted(missing.items())[:3]}")


def test_precondition_q_links_and_curvature_terms_exist(ws, nodes, expectation):
    g = _graph_qT(ws, nodes)
    plan = plan_simultaneous(g, set())
    n_q = sum(1 for link in g.links if link.kind == "q")
    n_curv = sum(1 for t in plan.terms if t.kind == "curvature")
    tol = read_inter_settings(ws).collinearity_tol
    rejected = [n for n in g.notes if n.startswith(("row ", "col ")) and "no curvature term" in n]
    print(f"\nq-links {n_q}; curvature terms {n_curv}; collinearity_tol in use {tol:g}; "
          f"metric per scan {sorted(set(g.metric.values()))}; collinear candidates rejected {len(rejected)}")
    for note in rejected[:3]:
        print(f"  rejected: {note}")
    RESULTS["precondition"] = {"q_links": n_q, "curvature_terms": n_curv, "collinearity_tol": tol,
                               "candidates_rejected": len(rejected)}
    assert n_q > 0, f"no q-link forms on the qT graph (accepted hkl, grid adjacency); the graph notes: {g.notes[:3]}"
    if n_curv == 0:
        cause = (f"all {len(rejected)} collinear candidates were rejected at collinearity_tol {tol:g} "
                 f"(the default, under the operator's review, design/inter_spectra.md section 9)") if rejected \
            else "the grid has no collinear triplet of q-linked spectra"
        pytest.fail(f"no curvature term forms, so simultaneous batches couple through T-links only: {cause}",
                    pytrace=False)
    floor = expectation.get("min_q_links")
    if floor is not None:
        assert n_q >= floor, f"{n_q} q-links, the expectation file asks for at least {floor}"
    floor = expectation.get("min_curvature_triplets")
    if floor is not None:
        assert n_curv >= floor, f"{n_curv} curvature terms, the expectation file asks for at least {floor}"


def _accounting(ws, b) -> str | None:
    """None when the batch accounts for every member; else the problem."""
    info = records.read_log(ws).batches.get(b.spec.batch_id)
    if info is None or info.end is None:
        return f"{b.label}: the log has no end line for {b.spec.batch_id}"
    progress = {"done": info.end["done"], "failed": info.end["failed"],
                "not_reached": len(info.end["not_reached"]), "fixed": len(info.fixed), "total": len(b.spec.members)}
    verdict = chk.accounting_ok(progress)
    print(f"{b.label}: {verdict.why}")
    RESULTS.setdefault("accounting", {})[b.label] = {**progress, "ok": verdict.ok}
    return None if verdict.ok else f"{b.label}: {verdict.why}"


def test_qT_batch_accounts_for_every_spectrum(ws, qt_run):
    problem = _accounting(ws, qt_run)
    assert problem is None, problem


def test_fast_solo_and_results_csv_are_untouched(snapshot, qt_run, ws):
    after = _snapshot(ws)
    changed = sorted(k for k, d in snapshot["fast"].items() if after["fast"].get(k) != d)
    print(f"\nfast-pass records hashed before: {len(snapshot['fast'])}, changed by the batches: {len(changed)}; "
          f"results.csv {'unchanged' if after['results_csv'] == snapshot['results_csv'] else 'CHANGED'}")
    assert after["results_csv"] == snapshot["results_csv"], "report/results.csv changed during the batches"
    assert not changed, f"fast-pass records changed: {changed}"


def test_qT_batch_terms_are_structurally_right(ws, nodes, qt_run):
    """The recorded terms of the qT batch, read from its batch record, against the grid and the groups recomputed from
    the same graph. Vacuous on zero curvature terms; the precondition test names that case."""
    batch = records.read_log(ws).batches[qt_run.spec.batch_id]
    terms = list(batch.terms)                        # the batch's own terms, from its start line
    g = _graph_qT(ws, nodes)
    plan = plan_simultaneous(g, set())              # recomputed: the parallel groups and the q-linked set only
    grid = grid_for(load_policy(ws / POLICY_NAME).plugin)
    q_linked = {k for link in g.links if link.kind == "q" for k in (link.a, link.b)}
    in_term = {m for t in terms for m in t["members"]}
    # the named reasons are the batch's own (its unreached map), not a recomputed plan's
    planned = sum(1 for t in plan.terms if t.kind == "curvature")
    verdicts = {
        "curvature_recorded": qc.curvature_terms_recorded(terms, planned),
        "curvature_structure": qc.curvature_structure_ok(terms, grid),
        "one_axis_per_link": qc.links_join_one_axis(terms),
        "groups_conflict_free": qc.groups_conflict_free(terms, [list(g_) for g_ in plan.groups]),
        "q_linked_in_term_or_named": qc.q_linked_spectra_named(batch.unreached, q_linked, in_term),
    }
    for name, v in verdicts.items():
        print(f"{name}: {v.why}")
    RESULTS["structure"] = {name: {"ok": v.ok, "why": v.why} for name, v in verdicts.items()}
    bad = {name: v.why for name, v in verdicts.items() if not v.ok}
    assert not bad, f"the qT batch's terms are structurally wrong: {bad}"


def test_authority_follows_the_rules(qt_run, ws, nodes):
    log = records.read_log(ws)
    newest = {}
    for line in log.refits:                          # file order: a later line is a newer refit
        if line.accept is not None and line.accept.decision == "authoritative":
            newest[line.unit] = line.fit
    by_key = inter_overview(ws).by_key
    problems = []
    for key in sorted(nodes.nodes):
        got = by_key[key].fit_id if key in by_key else "fast"
        want = newest.get(key, "fast")
        if got != want:
            problems.append(f"{key}: authoritative {got}, its newest authoritative refit is {want}")
    assert not problems, "; ".join(problems[:10])


def test_moving_mode_flagged_and_flats_quiet_on_every_spectrum(qt_run, ws, nodes, expectation):
    exp = expectation
    analyzer, moving, flats = exp["analyzer"], exp["moving_mode"], tuple(exp["flat_modes"])
    tol = exp.get("match_tolerance_meV")
    spectra = _judged_spectra(ws, nodes, exp)
    at = {T: spectra[_key(nodes, T, analyzer)] for T in TEMPERATURES}     # the moving mode's analyzer
    moving_v = chk.moving_is_flagged({T: at[T][1] for T in at}, {T: at[T][2] for T in at}, tol, moving=moving)
    flats_v = qc.flats_quiet_on_every_spectrum(spectra, flats)           # every spectrum, not only that analyzer
    print(f"\nmoving: {moving_v.why}\nflats: {flats_v.why}")
    RESULTS["moving"] = {"ok": moving_v.ok, "why": moving_v.why}
    RESULTS["flats"] = {"ok": flats_v.ok, "why": flats_v.why}
    assert moving_v.ok, f"moving mode with q-links on: {moving_v.why}"
    assert flats_v.ok, f"flat modes with q-links on: {flats_v.why}"


def test_named_kink_analyzers_are_flagged(qt_run, ws, nodes, expectation):
    kinks = expectation.get("q_kink_analyzers")
    if not kinks:
        print("\nq_kink_analyzers: null or empty, so printed only")
        return
    by_key = inter_overview(ws).by_key
    unflagged = []
    for det in kinks:
        keys = [k for k, n in nodes.nodes.items() if n.detector == det and k in by_key]
        flagged = [k for k in keys if is_flagged(by_key[k].flag)]
        print(f"{det}: {len(flagged)} of {len(keys)} spectra flagged")
        if not flagged:
            unflagged.append(det)
    # the flag codes carry no curvature code, so this reads the spectrum's own flag, not a curvature reason
    assert not unflagged, f"no spectrum of {unflagged} is flagged, though the expectation names them as kinks"


def test_q_anchored_batch_accounts_for_every_spectrum(ws, q_anchored_run):
    # Runs after every check of the qT batch, so those verdicts judge the qT batch's authoritative fits.
    problem = _accounting(ws, q_anchored_run)
    assert problem is None, problem


def test_timing_is_reported_never_asserted(q_anchored_run, qt_run, ws, out_dir, expectation):
    fast_round = next((s["seconds"] for s in _timings(ws)["stages"] if s["stage"] == "fit_scans"), None)
    reports = {}
    for b in (q_anchored_run, qt_run):
        rep = chk.timing_report(_timing_view(ws, b), fast_round, cores=BATCH_WORKERS, target_s=TARGET_S)
        rep["segments"] = b.segments
        reports[b.label] = rep
        print(f"\n{b.label}: wall {rep['wall_s']:.1f} s, {rep['refits']} refits, rings {rep['rings']}, "
              f"sweeps {rep['sweeps']}, cores {rep['cores']}, fast round {fast_round} s")
        print(f"  split (core s): {({k: round(v, 1) for k, v in rep['split_core_s'].items()})}")
        print(f"  per refit: {rep['refit_total_s']}")
        print(f"  projection {rep['projection_s']} s: {rep['verdict']}")
    RESULTS["timing"] = reports
    summary = {"workspace": str(ws), "settings": inter_overview(ws).settings,
               "batches": {b.label: {"batch_id": b.spec.batch_id, "kind": b.spec.kind, "direction": b.spec.direction,
                                     "anchors": list(b.spec.anchors), "members": len(b.spec.members),
                                     "end": b.end, "wall_s": b.wall} for b in (q_anchored_run, qt_run)},
               **RESULTS}
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8", newline="\n")
    print(f"\nsummary: {out_dir / 'summary.json'}")
    for label, rep in reports.items():
        assert rep["refits"] > 0 and rep["split_core_s"], f"{label}: the batch's refit timings are missing from its records"

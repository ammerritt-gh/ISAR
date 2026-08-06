"""Full-tier predictive promotion (R1-C): who is owed one, and exactly once.

The unit under test is the pair ``_predictive_design_measure_ready`` (is this master's particle
basis an EIG design measure?) and ``_predictive_promotion_target`` (which master, if any, does the
*selected* request owe a charged full-tier reanalysis?). Both were previously conflated with "does a
predictive artifact of any kind exist?", which let a cheap Laplace/HESSE artifact suppress the
promotion the scan-window EIG needs.
"""
from __future__ import annotations

import types
from dataclasses import dataclass, field
from typing import Any

import isar.drive.loop as loop_mod
from isar.drive.acquisition.acquire import ScanProposal
from isar.drive.acquisition.inventory import MeasurementIntent
from isar.drive.session_log import log_event
from isar.schema.steering import AUXILIARY_TRACK_NODE, CANONICAL_MASTER


@dataclass
class _GridItem:
    """The three attributes the promotion target reads off a GridQ."""
    hkl: tuple[float, float, float]
    master: dict[str, Any] = field(default_factory=dict)
    converged: bool = False


def _artifact(basis: str, *, complete: bool = True) -> dict:
    return {"models": [{"draw_basis": basis}, {"draw_basis": basis}],
            "uncertainty_provenance": {"draw_basis": [basis]},
            "completeness": {"predictive_complete_for_eig": complete}}


def _master(basis: str | None, **extra) -> dict:
    master = {"usable": True, "content_hash": "hash-a", "master_revision": "rev-1", **extra}
    if basis is not None:
        master["predictive"] = _artifact(basis)
    return master


def _proposal(hkl=(2.1, 0.0, 0.0), *, kind="production", sink=CANONICAL_MASTER) -> ScanProposal:
    h, k, l = hkl
    intent = MeasurementIntent(
        action_class="production", hkl=(h, k, l), energy_lo=0.0, energy_hi=2.0,
        window_family="target", evidence_sink=sink)
    return ScanProposal(h=h, k=k, l=l, e_lo=0.0, e_hi=2.0, e_step=0.1, ncount=10_000_000,
                        kind=kind, reason="test", intent=intent)


def _log(tmp_path):
    path = tmp_path / "session_log.jsonl"
    path.write_text("", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- readiness predicate
def test_bootstrap_basis_is_the_eig_design_measure():
    assert loop_mod._predictive_design_measure_ready(_master("parametric_bootstrap"))
    assert loop_mod._predictive_design_measure_ready(_master("nested_sampling"))


def test_a_laplace_artifact_is_present_but_not_an_eig_design_measure():
    # The distinction the fix exists for: the artifact is valid and usable for expected counts, and
    # is still not a particle basis the design measure will accept.
    master = _master("laplace_hesse")
    assert master["predictive"]                      # "some predictive artifact present"
    assert not loop_mod._predictive_design_measure_ready(master)


def test_a_heterogeneous_particle_basis_is_accepted():
    # Only converged candidates are bootstrapped, so a retained Akaike set routinely mixes bases —
    # on the O41 Test 10 masters the unconverged models carried ~1e-11 of the weight, and demanding
    # homogeneity rejected 19 of 21 masters over them. The design measure marginalises over the
    # bootstrap-grade models and renormalises; sufficiency of the surviving mass is adjudicated on
    # the real masses downstream (predictive_insufficient_bootstrap_mass), not on the basis list.
    master = _master("parametric_bootstrap")
    master["predictive"]["models"][1]["draw_basis"] = "laplace_hesse"
    master["predictive"]["uncertainty_provenance"]["draw_basis"] = [
        "laplace_hesse", "parametric_bootstrap"]
    assert loop_mod._predictive_design_measure_ready(master)


def test_an_all_laplace_particle_basis_is_still_refused():
    # Nothing simulation-grade to design against at all: this is the case the predicate exists for,
    # and it is what kept the R1-E replay's cheap-particle masters correctly blocked.
    master = _master("laplace_hesse")
    master["predictive"]["uncertainty_provenance"]["draw_basis"] = ["laplace_hesse"]
    assert not loop_mod._predictive_design_measure_ready(master)


def test_an_incomplete_artifact_is_refused_and_a_missing_one_is_not_ready():
    incomplete = _master("parametric_bootstrap")
    incomplete["predictive"]["completeness"]["predictive_complete_for_eig"] = False
    assert not loop_mod._predictive_design_measure_ready(incomplete)
    assert not loop_mod._predictive_design_measure_ready(_master(None))
    assert not loop_mod._predictive_design_measure_ready({})


# --------------------------------------------------------------------------- who is owed a promotion
def test_promotion_targets_only_the_selected_proposals_source_master(tmp_path):
    log_path = _log(tmp_path)
    grid = [_GridItem((2.0, 0.0, 0.0), _master("laplace_hesse")),
            _GridItem((2.1, 0.0, 0.0), _master("laplace_hesse")),
            _GridItem((2.2, 0.0, 0.0), _master("laplace_hesse"))]
    target = loop_mod._predictive_promotion_target(_proposal((2.1, 0.0, 0.0)), grid, log_path)
    assert target is not None
    item, revision_key = target
    assert item.hkl == (2.1, 0.0, 0.0)               # never a global sweep of every stale master
    assert revision_key == "hash-a:rev-1"


def test_a_ready_source_is_owed_nothing(tmp_path):
    grid = [_GridItem((2.1, 0.0, 0.0), _master("parametric_bootstrap"))]
    assert loop_mod._predictive_promotion_target(
        _proposal(), grid, _log(tmp_path)) is None


def test_a_converged_source_is_still_promoted(tmp_path):
    # Stopping convergence and EIG readiness are different questions: a converged master still
    # supplies the design measure for the requests that follow it.
    grid = [_GridItem((2.1, 0.0, 0.0), _master("laplace_hesse"), converged=True)]
    assert loop_mod._predictive_promotion_target(
        _proposal(), grid, _log(tmp_path)) is not None


def test_only_canonical_production_or_confirmation_requests_provision(tmp_path):
    log_path = _log(tmp_path)
    grid = [_GridItem((2.1, 0.0, 0.0), _master("laplace_hesse"))]
    assert loop_mod._predictive_promotion_target(
        _proposal(kind="quicklook"), grid, log_path) is None
    assert loop_mod._predictive_promotion_target(
        _proposal(sink=AUXILIARY_TRACK_NODE), grid, log_path) is None
    assert loop_mod._predictive_promotion_target(
        _proposal(kind="confirm"), grid, log_path) is not None
    assert loop_mod._predictive_promotion_target(None, grid, log_path) is None


def test_an_unmeasured_or_unusable_source_is_not_promoted(tmp_path):
    log_path = _log(tmp_path)
    assert loop_mod._predictive_promotion_target(
        _proposal(), [_GridItem((2.1, 0.0, 0.0), {})], log_path) is None
    assert loop_mod._predictive_promotion_target(
        _proposal(), [_GridItem((2.1, 0.0, 0.0), _master(None, usable=False))], log_path) is None
    # A q the grid does not carry at all (an unrelated request) provisions nothing.
    assert loop_mod._predictive_promotion_target(
        _proposal((2.9, 0.0, 0.0)),
        [_GridItem((2.1, 0.0, 0.0), _master("laplace_hesse"))], log_path) is None


# --------------------------------------------------------------------------- exactly once, incl. resume
def test_promotion_happens_once_and_the_source_is_ready_afterwards(tmp_path):
    log_path = _log(tmp_path)
    item = _GridItem((2.1, 0.0, 0.0), _master("laplace_hesse"))
    proposal = _proposal((2.1, 0.0, 0.0))

    target = loop_mod._predictive_promotion_target(proposal, [item], log_path)
    assert target is not None
    _item, revision_key = target
    # What _promote_master_predictive_basis records on a completed reanalysis.
    log_event(log_path, "escalate_fit", {
        "phase": "promote", "reason": "master_predictive_basis_missing",
        "revision_key": revision_key})
    log_event(log_path, "escalate_fit", {
        "phase": "outcome", "reason": "master_predictive_basis_missing",
        "revision_key": revision_key, "outcome": "completed"})

    # The reanalysis republished the master on a bootstrap basis: now ready, and owed nothing more.
    item.master = _master("parametric_bootstrap")
    assert loop_mod._predictive_design_measure_ready(item.master)
    assert loop_mod._predictive_promotion_target(proposal, [item], log_path) is None


# --------------------------------------------------------------------------- targeted refit scope
class _RecordingRunner:
    """Runner-protocol double that records each ``run`` call's kwargs and reports success."""

    def __init__(self):
        self.calls: list[dict] = []

    def run(self, workspace, data_paths, **kwargs):
        self.calls.append(kwargs)
        return types.SimpleNamespace(completed=True)

    def close(self):
        pass


def _promotion_cfg(tmp_path, runner):
    return types.SimpleNamespace(log_path=_log(tmp_path), runner=runner, workspace=tmp_path,
                                 data_dir=tmp_path / "data", bayes_errors=False)


def test_promotion_is_a_targeted_master_refit(tmp_path):
    # The escalation charges ONE master's full-tier basis; it must not force the whole batch
    # (which re-fit every master on the parallel path whose per-child bootstrap is serial).
    runner = _RecordingRunner()
    cfg = _promotion_cfg(tmp_path, runner)
    objective = types.SimpleNamespace(analysis_limit_seconds=None)
    item = _GridItem((2.1, 0.0, 0.0),
                     _master("laplace_hesse", master_session_key="master:2.1_0.0_0.0_300.0"))
    outcome, error = loop_mod._promote_master_predictive_basis(
        cfg, objective, item, "hash-a:rev-1", it=3)
    assert outcome == "completed" and error is None
    (call,) = runner.calls
    assert call["force_masters"] == ["master:2.1_0.0_0.0_300.0"]
    assert call["force"] is False
    assert call["master_errors_tier"] == "full"


def test_promotion_falls_back_when_the_runner_predates_targeted_refits(tmp_path):
    # The runner is a configurable seam and the promotion charge is durable, so a runner that
    # cannot take `force_masters` must degrade to the slow whole-batch force rather than raise
    # and deny this master revision its basis forever.
    class _OldRunner(_RecordingRunner):
        def run(self, workspace, data_paths, *, force=False, errors_tier="full", render=True,
                scan_errors_tier=None, master_errors_tier=None):
            self.calls.append({"force": force, "errors_tier": errors_tier})
            return types.SimpleNamespace(completed=True)

    runner = _OldRunner()
    cfg = _promotion_cfg(tmp_path, runner)
    objective = types.SimpleNamespace(analysis_limit_seconds=None)
    item = _GridItem((2.1, 0.0, 0.0),
                     _master("laplace_hesse", master_session_key="master:2.1_0.0_0.0_300.0"))
    outcome, error = loop_mod._promote_master_predictive_basis(
        cfg, objective, item, "hash-a:rev-1", it=3)
    assert outcome == "completed" and error is None
    assert runner.calls == [{"force": True, "errors_tier": "full"}]


def test_promotion_without_a_session_key_falls_back_to_whole_batch_force(tmp_path):
    # An old results.csv row with no scan_id cannot be targeted; the promotion must still happen
    # (whole-batch force), never silently no-op with a durable "completed" charge.
    runner = _RecordingRunner()
    cfg = _promotion_cfg(tmp_path, runner)
    objective = types.SimpleNamespace(analysis_limit_seconds=None)
    item = _GridItem((2.1, 0.0, 0.0), _master("laplace_hesse"))
    outcome, _error = loop_mod._promote_master_predictive_basis(
        cfg, objective, item, "hash-a:rev-1", it=3)
    assert outcome == "completed"
    (call,) = runner.calls
    assert call["force"] is True
    assert "force_masters" not in call


def test_a_spent_promotion_is_not_repeated_on_resume(tmp_path):
    # The durable log is the record, so a resume never re-charges a promotion already paid for —
    # even when the reanalysis failed to move the basis (it stays visible, it does not retry).
    log_path = _log(tmp_path)
    item = _GridItem((2.1, 0.0, 0.0), _master("laplace_hesse"))
    revision_key = loop_mod._master_revision_key(item.master)
    log_event(log_path, "escalate_fit", {
        "phase": "outcome", "reason": "master_predictive_basis_missing",
        "revision_key": revision_key, "outcome": "failed", "error": "boom"})
    assert revision_key in loop_mod._promoted_master_revisions(log_path)
    assert loop_mod._predictive_promotion_target(_proposal(), [item], log_path) is None
    # A NEW master revision at the same q is a different question and is promotable again.
    item.master = _master("laplace_hesse", master_revision="rev-2")
    assert loop_mod._predictive_promotion_target(_proposal(), [item], log_path) is not None

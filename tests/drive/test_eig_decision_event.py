"""The per-iteration ``eig_decision`` operator event — what the driver tells a watching scientist.

The authority artifacts already record *why* EIG did or did not change a window, but only as
content-addressed JSON nobody reads live. This event is the running commentary, and these tests pin
the two things that make it trustworthy: it carries evidence the engine already wrote (never a
recomputation), and it cannot break the campaign that emits it.
"""
from __future__ import annotations

import runpy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from isar.drive import loop
from isar.drive.acquisition.scan_design import design_scan_windows
from isar.drive.session_log import EVENT_KINDS, read_log


def _fixtures():
    return runpy.run_path(str(Path(__file__).with_name("test_realized_comparison.py")))


def _cfg(tmp_path: Path):
    return SimpleNamespace(log_path=tmp_path / "report" / "session_log.jsonl",
                           workspace=tmp_path)


def _events(tmp_path: Path) -> list[dict]:
    return [record["payload"] for record in read_log(tmp_path / "report" / "session_log.jsonl")
            if record["kind"] == "eig_decision"]


def test_eig_decision_is_a_known_kind():
    # An unknown kind is written with a ``kind_unknown`` marker and counted as unknown by the
    # campaign diagnostic — which would make every EIG iteration look like a defect.
    assert "eig_decision" in EVENT_KINDS


def test_shadow_mode_reports_the_planned_counts_and_the_first_blocker(tmp_path):
    fixtures = _fixtures()
    inventory = fixtures["_inventory"]()
    proposal = fixtures["_proposal"](inventory)

    assert loop._log_eig_decision(_cfg(tmp_path), proposal, it=3) is True
    payload, = _events(tmp_path)
    assert payload["iter"] == 3 and payload["mode"] == "authoritative"
    assert payload["hkl"] == [proposal.h, proposal.k, proposal.l]
    assert payload["ready"] is False and payload["blocker"]
    assert payload["point_count_basis"] == "planned_inventory"
    legacy = next(item for item in inventory.candidates
                  if item.candidate_family == "legacy_uniform/1")
    assert payload["legacy_points"] == legacy.n_points
    assert payload["selected_role"] is None and payload["decision_ref"] is None


def test_a_granted_authority_reports_its_validated_counts_margin_and_decision(tmp_path):
    fixtures = _fixtures()
    inventory = fixtures["_inventory"]()
    inventory = replace(
        inventory, context=replace(inventory.context, scan_window_eig_mode="authoritative"))
    proposal = fixtures["_proposal"](inventory)
    proposal.meta.update({
        "scan_design_authority_status": "granted",
        "scan_design_authority_selected_role": "target_core",
        "scan_design_authority_outcome": "select_target_core",
        "scan_design_authority_legacy_point_count": 46,
        "scan_design_authority_target_point_count": 17,
        "scan_design_authority_delta": 0.02412,
        "scan_design_authority_standard_error": 0.00318,
        "scan_design_authority_lower_margin": 0.01776,
        "scan_design_authority_decision_ref": {
            "kind": "scan_design_authority_decision", "sha256": "d" * 64,
            "path": f"report/scan-design-authority/{'d' * 64}.json"},
    })

    assert loop._log_eig_decision(_cfg(tmp_path), proposal, it=12) is True
    payload, = _events(tmp_path)
    assert payload["mode"] == "authoritative" and payload["ready"] is True
    assert payload["blocker"] is None
    assert payload["point_count_basis"] == "authority_decision"
    assert (payload["legacy_points"], payload["target_points"]) == (46, 17)
    assert payload["delta_nats_per_point"] == 0.02412
    assert payload["standard_error_nats"] == 0.00318
    assert payload["selected_role"] == "target_core"
    assert payload["decision_ref"] == "d" * 64


def test_an_authority_refusal_leads_with_its_own_reason_code(tmp_path):
    fixtures = _fixtures()
    inventory = fixtures["_inventory"]()
    inventory = replace(
        inventory, context=replace(inventory.context, scan_window_eig_mode="authoritative"))
    proposal = fixtures["_proposal"](inventory)
    proposal.meta.update({
        "scan_design_authority_status": "not_granted",
        "scan_design_authority_outcome": "use_non_eig_path",
        "scan_design_authority_reason_codes": ["o36_ineligible"],
    })

    loop._log_eig_decision(_cfg(tmp_path), proposal, it=5)
    payload, = _events(tmp_path)
    assert payload["ready"] is False and payload["blocker"] == "o36_ineligible"


def test_an_iteration_without_scan_window_eig_says_nothing(tmp_path):
    fixtures = _fixtures()
    helpers = runpy.run_path(str(Path(__file__).with_name("test_scan_design.py")))
    inventory = design_scan_windows(helpers["_context"](scan_window_eig_mode="off"))
    proposal = fixtures["_proposal"](inventory)

    assert loop._log_eig_decision(_cfg(tmp_path), proposal, it=1) is False
    assert _events(tmp_path) == []


def test_a_broken_payload_is_reported_and_never_raised(tmp_path):
    """The operator line is commentary: it may fail, but it may not take the campaign with it."""
    broken = SimpleNamespace(scan_design_inventory=SimpleNamespace(
        context=SimpleNamespace(scan_window_eig_mode="authoritative")))

    assert loop._log_eig_decision(_cfg(tmp_path), broken, it=2) is True
    assert _events(tmp_path) == []
    actions = [record["payload"].get("action")
               for record in read_log(tmp_path / "report" / "session_log.jsonl")]
    assert "eig_decision_event_failed" in actions


def test_the_event_carries_the_eligibility_diagnostics_the_envelope_cannot(tmp_path):
    """``o36_ineligible`` alone cannot separate an honest fork from an unpooled master.

    The six-field eligibility envelope is re-checked key for key and folded into
    ``design_measure_fingerprint``, so the explanatory facts cannot ride there without moving the
    scorer seed.  They travel on the design measure's non-fingerprinted diagnostics channel and
    surface here, which is what makes a blocker histogram readable at all.
    """
    fixtures = _fixtures()
    inventory = fixtures["_inventory"]()
    inventory = replace(
        inventory, context=replace(inventory.context, scan_window_eig_mode="authoritative"))
    proposal = fixtures["_proposal"](inventory)
    shadow = proposal.scan_design_inventory.shadow_scoring
    design = dict(shadow.design_measure or {})
    design["diagnostics"] = {"eligibility_target_modality": "fold",
                             "predictive_validation_message": "not an eligibility fact"}
    proposal.scan_design_inventory = replace(
        proposal.scan_design_inventory,
        shadow_scoring=replace(shadow, design_measure=design))

    loop._log_eig_decision(_cfg(tmp_path), proposal, it=11)
    payload, = _events(tmp_path)
    assert payload["eligibility_diagnostics"] == {"eligibility_target_modality": "fold"}


def test_a_generic_authority_blocker_still_names_the_underlying_evidence_reason(tmp_path):
    # O41's refusal codes say only that the evidence was unavailable. Without the realized reason
    # beside them a campaign-wide policy-binding failure reads as an ordinary unscoreable pair.
    fixtures = _fixtures()
    inventory = fixtures["_inventory"]()
    inventory = replace(
        inventory, context=replace(inventory.context, scan_window_eig_mode="authoritative"))
    proposal = fixtures["_proposal"](inventory)
    proposal.meta.update({
        "scan_design_authority_status": "not_granted",
        "scan_design_authority_outcome": "use_non_eig_path",
        "scan_design_authority_reason_codes": ["primary_score_evidence_unavailable"],
        "scan_design_realized_reason_codes": ["qoi_policy_missing"],
    })

    loop._log_eig_decision(_cfg(tmp_path), proposal, it=9)
    payload, = _events(tmp_path)
    assert payload["blocker"] == "primary_score_evidence_unavailable"
    assert "qoi_policy_missing" in payload["blocker_detail"]

"""``isar diagnose --eig`` — the scan-window EIG audit trail as an operator-readable table.

The workspaces here are built with the same authenticating writers the driver uses (via the Slice-7
fixtures in ``tests/drive``), so a change to the artifact contract shows up as a changed table rather
than as an empty one. The command's defining property is that it only ever reads: one test asserts
that byte-for-byte.
"""
from __future__ import annotations

import hashlib
import json
import runpy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from isar.cli import diagnose_cmd
from isar.drive.acquisition.authority import (
    build_authority_decision,
    persist_authority_decision,
)
from isar.drive.acquisition.realized_comparison import (
    build_realized_comparison,
    construct_realized_pair,
    persist_realized_comparison,
)
from isar.eig.o41_authority import O41AuthoritativeProfile


def _drive_fixtures():
    """The Slice-7 inventory/proposal builders, loaded from the drive suite that owns them."""
    return runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "drive" / "test_realized_comparison.py"))


def _args(workspace, *, eig=True):
    return SimpleNamespace(workspace=str(workspace), eig=eig)


def _campaign(tmp_path: Path, *, iteration: int = 7):
    """A workspace holding one authenticated decision, its comparison, and a session log."""
    fixtures = _drive_fixtures()
    inventory = fixtures["_inventory"]()
    inventory = replace(
        inventory, context=replace(inventory.context, scientific_action_fingerprint="9" * 64))
    proposal = fixtures["_proposal"](inventory)

    def finalize(target, *_args, **_kwargs):
        target.meta["proposal_fingerprint"] = "e" * 64

    pair = construct_realized_pair(
        proposal, inventory, SimpleNamespace(), SimpleNamespace(), {},
        it=iteration, finalizer=finalize)
    inventory_ref = fixtures["_inventory_reference"](tmp_path, inventory)
    comparison = build_realized_comparison(
        inventory, inventory_ref, pair, remaining_seconds=0.0, rescorer=None)
    comparison_ref = persist_realized_comparison(tmp_path, comparison)
    decision = build_authority_decision(
        inventory=inventory, inventory_ref=inventory_ref, comparison=comparison,
        comparison_ref=comparison_ref,
        profile=O41AuthoritativeProfile(
            policy_fingerprint="1" * 64, variant_fingerprint="2" * 64,
            policy_set_fingerprint="3" * 64, scoring_config_fingerprint="4" * 64),
        legacy_validation_status="validated", target_validation_status="validated")
    reference = persist_authority_decision(tmp_path, decision)

    (tmp_path / "card.policy.toml").write_text("# test workspace\n", encoding="utf-8")
    (tmp_path / "report" / "session_log.jsonl").write_text(json.dumps({
        "ts": 1785862676.0, "kind": "scan_design_authority_decision",
        "payload": {"iter": iteration, "mode": "authoritative",
                    "decision_fingerprint": decision.authority_decision_fingerprint,
                    "selected_role": decision.physical_role}}) + "\n", encoding="utf-8")
    return decision, reference


def _snapshot(root: Path) -> dict[str, str]:
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*")) if path.is_file()}


def test_diagnose_eig_tabulates_the_decision_and_counts_its_reason_codes(tmp_path, capsys):
    decision, _reference = _campaign(tmp_path)
    assert diagnose_cmd.run(_args(tmp_path)) == 0
    out = capsys.readouterr().out

    assert "1 authority decision(s), 1 realized comparison(s)" in out
    assert decision.decision_outcome in out
    row = next(line for line in out.splitlines() if line.strip().startswith("H="))
    assert " 7 " in row                                    # the iteration, joined from the log
    legacy = decision.legacy_validation["executed_point_count"]
    target = decision.target_core_validation["executed_point_count"]
    assert f"{legacy} / {target}" in row
    assert decision.reason_codes[0] in row
    assert "Primary reason code across all 1 record(s):" in out
    # Fingerprints belong in the artifacts, not in a table meant to be read at a beamline.
    assert decision.authority_decision_fingerprint not in out


def test_diagnose_eig_writes_nothing_into_the_workspace(tmp_path):
    _campaign(tmp_path)
    before = _snapshot(tmp_path)
    assert diagnose_cmd.run(_args(tmp_path)) == 0
    assert _snapshot(tmp_path) == before


def test_diagnose_eig_says_so_when_the_campaign_ran_without_scan_window_eig(tmp_path, capsys):
    (tmp_path / "card.policy.toml").write_text("# test workspace\n", encoding="utf-8")
    assert diagnose_cmd.run(_args(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "0 authority decision(s), 0 realized comparison(s)" in out
    assert "scan-window EIG off" in out


def test_diagnose_eig_reports_an_artifact_that_does_not_authenticate(tmp_path, capsys):
    _decision, reference = _campaign(tmp_path)
    # A file parked at an address it does not hash to: exactly what a truncated or edited artifact
    # looks like. It must be named as unreadable, never quietly folded into the table.
    good = tmp_path / reference["path"]
    (good.parent / f"{'a' * 64}.json").write_bytes(good.read_bytes())
    assert diagnose_cmd.run(_args(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "1 artifact(s) did not authenticate:" in out
    assert "aaaaaaaaaaaa… authority decision:" in out


def test_diagnose_without_the_flag_still_renders_the_campaign_diagnostic(tmp_path, capsys):
    """The flag is additive: an unflagged ``diagnose`` keeps its Part-2 markdown behaviour."""
    assert diagnose_cmd.run(_args(tmp_path, eig=False)) == 1
    assert "no workspace at" in capsys.readouterr().err


def test_diagnose_eig_rejects_a_missing_workspace(tmp_path, capsys):
    assert diagnose_cmd.run(_args(tmp_path / "absent")) == 1
    assert "no workspace at" in capsys.readouterr().err

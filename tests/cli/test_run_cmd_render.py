"""`isar run --no-render` (Stage 3 report deferral).

Locks the contract that ``--no-render`` skips the report layer while replacing canonical human-report
artifacts with revision-bound availability records, and that omitting the flag renders as before.
The report-writing seam is monkeypatched so the test stays fast (no fits, no matplotlib).
"""
import json
import types
from pathlib import Path

import pytest

from isar.cli import main as main_mod
from isar.cli import run_cmd


def test_parser_no_render_sets_render_false():
    args = main_mod.build_parser().parse_args(["run", "d", "--workspace", "ws", "--no-render"])
    assert args.render is False
    args = main_mod.build_parser().parse_args(["run", "d", "--workspace", "ws"])
    assert args.render is True   # default renders (for humans)


def _fake_summary(*, analysis_incomplete=False):
    return types.SimpleNamespace(
        n_reprocessed=0, n_fitted_this_run=0, outcomes=[], gaps=[], context_lines=[],
        counts={"by_status": {}, "by_disposition": {}},
        analysis_incomplete=analysis_incomplete)


def _run_args(ws, *, render):
    return types.SimpleNamespace(
        workspace=str(ws), data=["d"], force=False, llm=False, M=200, formats="html,markdown",
        detectors=None, workers=None, errors="fast", render=render)


def _install_fakes(monkeypatch, ws, rendered, *, summary=None):
    """Fake Workspace + run_batch so run_cmd exercises only the render gate. ``rendered`` accumulates
    each write_run_report call so the test can assert whether the report layer was invoked."""
    report_dir = ws / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "results.csv").write_text("scan\n", encoding="utf-8")

    fake_ws = types.SimpleNamespace(
        root=ws, policy_path=ws / "card.policy.toml", report_dir=report_dir,
        exists=lambda: True, load=lambda: ("card", "session"))
    monkeypatch.setattr(run_cmd.Workspace, "open", staticmethod(lambda p: fake_ws))
    monkeypatch.setattr(run_cmd.card_mod, "unknown_toml_tables", lambda p: [])
    monkeypatch.setattr(run_cmd.driver, "run_batch", lambda *a, **k: summary or _fake_summary())

    import isar.report.run_report as rr
    monkeypatch.setattr(rr, "write_run_report",
                        lambda *a, **k: rendered.append(True) or {"markdown": "report.md"})


def test_no_render_skips_report_but_keeps_results(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    rendered = []
    _install_fakes(monkeypatch, ws, rendered)

    rc = run_cmd.run(_run_args(ws, render=False))

    assert rc == 0
    assert rendered == []                                   # report layer never invoked
    assert (ws / "report" / "results.csv").exists()         # data contract still written
    status = json.loads((ws / "report" / "report.json").read_text(encoding="utf-8"))
    assert status["status"] == "not_rendered"
    assert status["analysis_revision"]


def test_render_default_invokes_report(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    rendered = []
    _install_fakes(monkeypatch, ws, rendered)

    rc = run_cmd.run(_run_args(ws, render=True))

    assert rc == 0
    assert rendered == [True]                               # report layer invoked
    marker = json.loads((ws / "report" / "report.complete.json").read_text(encoding="utf-8"))
    assert marker["status"] == "complete"


def test_incomplete_run_replaces_stale_canonical_reports(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    rendered = []
    _install_fakes(monkeypatch, ws, rendered, summary=_fake_summary(analysis_incomplete=True))
    for name in ("report.md", "report.html", "report.json"):
        (ws / "report" / name).write_text("STALE SUCCESS", encoding="utf-8")

    rc = run_cmd.run(_run_args(ws, render=True))

    assert rc == 75
    assert rendered == []
    for name in ("report.md", "report.html", "report.json"):
        assert "STALE SUCCESS" not in (ws / "report" / name).read_text(encoding="utf-8")
    status = json.loads((ws / "report" / "report.json").read_text(encoding="utf-8"))
    marker = json.loads((ws / "report" / "report.complete.json").read_text(encoding="utf-8"))
    assert status == marker
    assert status["status"] == "incomplete"
    assert status["analysis_revision"]


def test_render_failure_replaces_stale_canonical_reports(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    rendered = []
    _install_fakes(monkeypatch, ws, rendered)
    import isar.report.run_report as rr
    monkeypatch.setattr(
        rr, "write_run_report",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("render broke")))
    for name in ("report.md", "report.html", "report.json"):
        (ws / "report" / name).write_text("STALE SUCCESS", encoding="utf-8")

    assert run_cmd.run(_run_args(ws, render=True)) == 0
    status = json.loads((ws / "report" / "report.json").read_text(encoding="utf-8"))
    assert status["status"] == "render_failed"
    assert status["analysis_revision"]


def test_analysis_failure_replaces_stale_canonical_reports(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    rendered = []
    _install_fakes(monkeypatch, ws, rendered)
    monkeypatch.setattr(
        run_cmd.driver, "run_batch",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("analysis broke")))
    for name in ("report.md", "report.html", "report.json"):
        (ws / "report" / name).write_text("STALE SUCCESS", encoding="utf-8")

    with pytest.raises(RuntimeError, match="analysis broke"):
        run_cmd.run(_run_args(ws, render=True))
    status = json.loads((ws / "report" / "report.json").read_text(encoding="utf-8"))
    assert status["status"] == "analysis_failed"
    assert status["analysis_revision"]

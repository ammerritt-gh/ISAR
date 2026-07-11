"""`isar score --truth-fn ... --interval {bootstrap,bayes}` — the σ-calibration CLI in truth mode.

Pins that the `--interval` flag threads through to `score_results` and that the printed report
header names the interval source, so a bayes coverage number can never masquerade as bootstrap.
Builds a minimal workspace (a policy file to satisfy `Workspace.exists()` + a `report/results.csv`)
so no run/pipeline is needed.
"""
import argparse

from isar.cli import score_cmd
from isar.session.workspace import POLICY_NAME, REPORT_DIRNAME


def _ws(tmp_path, results_body):
    (tmp_path / POLICY_NAME).write_text("# minimal policy\n", encoding="utf-8")
    rdir = tmp_path / REPORT_DIRNAME
    rdir.mkdir()
    (rdir / "results.csv").write_text(results_body, encoding="utf-8")
    return tmp_path


def _args(workspace, *, interval="bootstrap"):
    return argparse.Namespace(
        workspace=str(workspace), gold=None, truth_fn="phonon_dft", truth_csv=None,
        bucket_by=None, json=None, interval=interval)


# H=3 folds to |q|=1 → phonon_dft truth 6.0. Bootstrap bar [5.5,6.5] covers it; bayes [6.4,6.6] not.
_RESULTS = (
    "scan,detector,h,k,l,verdict,omega0,interval_lo,interval_hi,bayes_lo,bayes_hi,bayes_ok\n"
    "1,a,3,0,0,resolved,6.5,5.5,6.5,6.4,6.6,True\n"
)


def test_truth_cli_default_bootstrap(tmp_path, capsys):
    ws = _ws(tmp_path, _RESULTS)
    rc = score_cmd.run(_args(ws))
    assert rc == 0
    out = capsys.readouterr().out
    assert "interval source: bootstrap (68% bar)" in out
    assert "68% interval coverage: 1.00" in out          # bootstrap bar covers truth


def test_truth_cli_interval_bayes_threads_and_names_source(tmp_path, capsys):
    ws = _ws(tmp_path, _RESULTS)
    rc = score_cmd.run(_args(ws, interval="bayes"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "interval source: bayes (posterior credible)" in out
    assert "68% interval coverage: 0.00" in out           # tighter bayes bar misses truth

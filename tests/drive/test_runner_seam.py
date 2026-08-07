"""Runner-seam unit tests (plan Stage 1): RunResult / SubprocessRunner / as_runner.

These lock the compat contract that keeps every existing config-supplied runner and test fake working
unchanged: ``as_runner`` must invoke a bare callable EXACTLY as the loop invoked ``run_isar`` before the
seam existed — ``obj(workspace, data_paths, extra_args=[...])`` with ``extra_args`` reconstructed from
the structured ``(force, errors_tier)`` kwargs.
"""
import pytest

import isar.drive.isar_io.runner as runner_mod
from isar.drive.isar_io.runner import (RunResult, SubprocessRunner, as_runner, refresh_analysis,
                                       runner_accepts)
from isar.drive.session_log import AnalysisLimitExceeded


def test_as_runner_wraps_bare_callable_reconstructs_fast_tier():
    calls = []

    def fake(workspace, data_paths, extra_args=None):
        calls.append((workspace, list(data_paths), list(extra_args)))
        return None

    r = as_runner(fake)
    result = r.run("ws", ["data"], errors_tier="fast")

    assert calls == [("ws", ["data"], ["--errors", "fast"])]
    # None from the callable is coerced to a success RunResult.
    assert isinstance(result, RunResult)
    assert result.returncode == 0


def test_as_runner_wraps_bare_callable_reconstructs_full_force():
    calls = []

    def fake(workspace, data_paths, extra_args=None):
        calls.append(list(extra_args))
        return None

    r = as_runner(fake)
    r.run("ws", ["data"], errors_tier="full", force=True)

    # Exact argv the certify call site produced pre-seam: --errors full --force.
    assert calls == [["--errors", "full", "--force"]]


def test_as_runner_coerces_int_and_completedprocess_returns():
    class _Proc:
        returncode = 3
        stdout = "out"
        stderr = "err"

    def returns_int(workspace, data_paths, extra_args=None):
        return 0

    def returns_proc(workspace, data_paths, extra_args=None):
        return _Proc()

    rint = as_runner(returns_int).run("ws", ["d"], errors_tier="fast")
    assert isinstance(rint, RunResult) and rint.returncode == 0

    rproc = as_runner(returns_proc).run("ws", ["d"], errors_tier="fast")
    assert (rproc.returncode, rproc.stdout, rproc.stderr) == (3, "out", "err")


def test_as_runner_passthrough_for_runner_object():
    sentinel = SubprocessRunner()
    assert as_runner(sentinel) is sentinel

    class FakeRunner:
        def run(self, *a, **k):
            return RunResult(0, "", "")

        def close(self):
            pass

    fr = FakeRunner()
    assert as_runner(fr) is fr


def test_subprocess_runner_argv_construction(monkeypatch):
    captured = {}

    class _CP:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, capture_output=False, text=False, timeout=None):
        captured["cmd"] = list(cmd)
        captured["timeout"] = timeout
        return _CP()

    monkeypatch.setattr(runner_mod.subprocess, "run", fake_run)

    res = SubprocessRunner().run("myws", ["d1", "d2"], errors_tier="fast", timeout=42.0)
    assert isinstance(res, RunResult) and res.returncode == 0
    cmd = captured["cmd"]
    # ... run <data...> --workspace myws --errors fast ; no --force when not forced.
    assert cmd[1:3] == ["-m", "isar.cli.main"]
    assert cmd[3] == "run"
    assert "d1" in cmd and "d2" in cmd
    assert cmd[cmd.index("--workspace") + 1] == "myws"
    assert cmd[cmd.index("--errors") + 1] == "fast"
    assert "--force" not in cmd
    assert captured["timeout"] == 42.0


def test_subprocess_runner_argv_force(monkeypatch):
    captured = {}

    class _CP:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(runner_mod.subprocess, "run",
                        lambda cmd, **k: (captured.__setitem__("cmd", list(cmd)), _CP())[1])

    SubprocessRunner().run("ws", ["d"], errors_tier="full", force=True)
    cmd = captured["cmd"]
    # --force precedes --errors full (structured force kwarg).
    assert "--force" in cmd
    assert cmd.index("--force") < cmd.index("--errors")
    assert cmd[cmd.index("--errors") + 1] == "full"


def test_runner_seams_forward_optional_role_tiers_without_changing_legacy_shape(monkeypatch):
    captured = _capture_subprocess(monkeypatch)
    SubprocessRunner().run(
        "ws", ["d"], errors_tier="full", scan_errors_tier="fast",
        master_errors_tier="full")
    cmd = captured["cmd"]
    assert cmd[cmd.index("--scan-errors") + 1] == "fast"
    assert cmd[cmd.index("--master-errors") + 1] == "full"

    calls = []
    runner = as_runner(lambda workspace, paths, extra_args=None: calls.append(list(extra_args)))
    runner.run("ws", ["d"], errors_tier="full", scan_errors_tier="fast",
               master_errors_tier="bayes")
    assert calls == [["--errors", "full", "--scan-errors", "fast",
                      "--master-errors", "bayes"]]


def test_subprocess_runner_soft_deadline_is_a_checkpointed_nonerror(monkeypatch):
    captured = {}

    class _CP:
        returncode = 75
        stdout = "checkpointed incomplete analysis"
        stderr = ""

    monkeypatch.setattr(runner_mod.subprocess, "run",
                        lambda cmd, **_kwargs: (captured.__setitem__("cmd", list(cmd)), _CP())[1])
    result = SubprocessRunner().run(
        "ws", ["d"], errors_tier="full", analysis_allowance_seconds=12.5,
        finish_initial_wave=True)
    assert not result.completed and result.returncode == 75
    assert captured["cmd"][captured["cmd"].index("--analysis-allowance-seconds") + 1] == "12.5"
    assert "--finish-initial-wave" in captured["cmd"]


def test_subprocess_runner_passes_session_owned_certification_request(monkeypatch):
    captured = _capture_subprocess(monkeypatch)

    SubprocessRunner().run(
        "ws", ["d"], errors_tier="full", certification_request="request.json")

    cmd = captured["cmd"]
    assert cmd[cmd.index("--certification-request") + 1] == "request.json"


def test_subprocess_runner_argv_bayes_tier(monkeypatch):
    # The new "bayes" tier (Stage 6 campaign opt-in) round-trips verbatim to `--errors bayes`, exactly
    # like every other tier string — SubprocessRunner does not validate tier membership.
    captured = {}

    class _CP:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(runner_mod.subprocess, "run",
                        lambda cmd, **k: (captured.__setitem__("cmd", list(cmd)), _CP())[1])

    SubprocessRunner().run("ws", ["d"], errors_tier="bayes")
    cmd = captured["cmd"]
    assert cmd[cmd.index("--errors") + 1] == "bayes"


def _capture_subprocess(monkeypatch):
    captured = {}

    class _CP:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(runner_mod.subprocess, "run",
                        lambda cmd, **k: (captured.__setitem__("cmd", list(cmd)), _CP())[1])
    return captured


def test_subprocess_runner_render_false_appends_no_render(monkeypatch):
    captured = _capture_subprocess(monkeypatch)
    SubprocessRunner().run("ws", ["d"], errors_tier="fast", render=False)
    assert "--no-render" in captured["cmd"]


def test_subprocess_runner_render_true_default_has_no_flag(monkeypatch):
    captured = _capture_subprocess(monkeypatch)
    SubprocessRunner().run("ws", ["d"], errors_tier="fast")   # render defaults True
    assert "--no-render" not in captured["cmd"]


def test_callable_runner_never_gets_no_render(monkeypatch):
    # The bare-callable shim's extra_args contract is frozen at the Stage-1 shape: render=False must
    # NOT leak --no-render into extra_args (fakes don't parse argv).
    calls = []

    def fake(workspace, data_paths, extra_args=None):
        calls.append(list(extra_args))
        return None

    as_runner(fake).run("ws", ["d"], errors_tier="fast", render=False)
    assert calls == [["--errors", "fast"]]
    assert all("--no-render" not in a for a in calls)


# --------------------------------------------------------------- refresh_analysis (plan P2.5)
# ``refresh_analysis`` owns every mechanical difference between runner shapes that the campaign loop
# used to coordinate inline. These lock the two things that must not drift: an optional keyword is
# omitted (never passed) when the configured runner cannot take it, and the kwargs reaching
# ``runner.run`` at each of the loop's three call sites are exactly the pre-seam ones.


# Distinguishes "keyword never passed" from an explicit None/False: refresh_analysis's contract is
# to OMIT unsupported/inapplicable keywords, and a None default cannot prove omission.
_OMITTED = object()


class _Recorder:
    """Full-protocol runner double that records the kwargs it was called with."""

    def __init__(self, completed=True):
        self.calls = []
        self._completed = completed

    def run(self, workspace, data_paths, *, force=False, errors_tier="full", render=True,
            timeout=None, scan_errors_tier=None, master_errors_tier=None, force_masters=None,
            certification_manifest=None, certification_request=None,
            analysis_allowance_seconds=_OMITTED, finish_initial_wave=_OMITTED):
        self.calls.append({
            "workspace": workspace, "data_paths": list(data_paths), "force": force,
            "errors_tier": errors_tier, "render": render, "scan_errors_tier": scan_errors_tier,
            "master_errors_tier": master_errors_tier, "force_masters": force_masters,
            "certification_manifest": certification_manifest,
            "certification_request": certification_request,
            "analysis_allowance_seconds": analysis_allowance_seconds,
            "finish_initial_wave": finish_initial_wave})
        return RunResult(0, "", "", completed=self._completed)

    def close(self):
        pass


class _Narrow:
    """A config-supplied runner predating every optional keyword — passing one would TypeError."""

    def __init__(self):
        self.calls = []

    def run(self, workspace, data_paths, *, force=False, errors_tier="full", render=True):
        self.calls.append({"force": force, "errors_tier": errors_tier, "render": render})
        return RunResult(0, "", "")

    def close(self):
        pass


def test_runner_accepts_reads_the_signature_and_defaults_to_accepting():
    assert runner_accepts(SubprocessRunner(), "force_masters")
    assert not runner_accepts(_Narrow(), "force_masters")

    class _Kwargs:
        def run(self, *a, **k):
            return RunResult(0, "", "")

    assert runner_accepts(_Kwargs(), "anything_at_all")


def test_refresh_analysis_omits_force_masters_a_narrow_runner_cannot_take():
    narrow = _Narrow()
    # No TypeError: the unsupported keyword is dropped, not forwarded.
    refresh_analysis(narrow, "ws", ["d"], errors_tier="full", force=True,
                     force_masters=["master-1"])
    assert narrow.calls == [{"force": True, "errors_tier": "full", "render": False}]

    wide = _Recorder()
    refresh_analysis(wide, "ws", ["d"], errors_tier="full", force_masters=["master-1"])
    assert wide.calls[0]["force_masters"] == ["master-1"]


def test_refresh_analysis_omits_certification_request_a_narrow_runner_cannot_take():
    narrow = _Narrow()
    refresh_analysis(narrow, "ws", ["d"], errors_tier="full", certification_request="req.json")
    assert "certification_request" not in narrow.calls[0]

    wide = _Recorder()
    refresh_analysis(wide, "ws", ["d"], errors_tier="full", certification_request="req.json")
    assert wide.calls[0]["certification_request"] == "req.json"


def test_refresh_analysis_forwards_the_allowance_pair_only_when_the_campaign_has_a_limit():
    # An unlimited campaign passes allowance None, and NEITHER deadline keyword reaches the runner —
    # the pre-seam behaviour of ``_analysis_runner_kwargs`` returning {} on a null limit.
    wide = _Recorder()
    refresh_analysis(wide, "ws", ["d"], errors_tier="full",
                     analysis_allowance_seconds=None, finish_initial_wave=None)
    assert wide.calls[0]["analysis_allowance_seconds"] is _OMITTED   # never passed, not passed-None
    assert wide.calls[0]["finish_initial_wave"] is _OMITTED

    refresh_analysis(wide, "ws", ["d"], errors_tier="full",
                     analysis_allowance_seconds=12.5, finish_initial_wave=True)
    assert wide.calls[1]["analysis_allowance_seconds"] == 12.5
    assert wide.calls[1]["finish_initial_wave"] is True

    narrow = _Narrow()
    refresh_analysis(narrow, "ws", ["d"], errors_tier="full",
                     analysis_allowance_seconds=12.5, finish_initial_wave=True)
    assert narrow.calls == [{"force": False, "errors_tier": "full", "render": False}]


def test_refresh_analysis_enforce_completion_raises_on_a_checkpointed_run():
    checkpointed = _Recorder(completed=False)
    with pytest.raises(AnalysisLimitExceeded) as raised:
        refresh_analysis(checkpointed, "ws", ["d"], errors_tier="full",
                         enforce_completion=True, analysis_limit_seconds=3600.0)
    assert "limit 3600.0s" in str(raised.value)

    # Without enforcement the soft checkpoint is just a result the caller may inspect.
    result = refresh_analysis(_Recorder(completed=False), "ws", ["d"], errors_tier="full")
    assert result.completed is False


def test_refresh_analysis_reproduces_each_loop_call_site_kwargs():
    """The three loop call sites, pre-seam kwargs (derived from loop.py at HEAD before P2.5)."""
    ws, data = "ws", "datadir"

    # 1. Predictive promotion: targeted master refit, full/fast/full tiers, no whole-batch force.
    promotion = _Recorder()
    refresh_analysis(promotion, ws, [data], errors_tier="full", scan_errors_tier="fast",
                     master_errors_tier="full", force=False, render=False,
                     force_masters=["master-1"], analysis_allowance_seconds=30.0,
                     finish_initial_wave=False, enforce_completion=True)
    assert promotion.calls == [{
        "workspace": ws, "data_paths": [data], "force": False, "errors_tier": "full",
        "render": False, "scan_errors_tier": "fast", "master_errors_tier": "full",
        "force_masters": ["master-1"], "certification_manifest": None,
        "certification_request": None,
        "analysis_allowance_seconds": 30.0, "finish_initial_wave": False}]

    # 2. Submission finalization: the initial wave must finish; no force, no targeted masters.
    finalize = _Recorder()
    refresh_analysis(finalize, ws, [data], errors_tier="fast", scan_errors_tier="cheap",
                     master_errors_tier="fast", render=False, analysis_allowance_seconds=30.0,
                     finish_initial_wave=True, enforce_completion=True)
    assert finalize.calls == [{
        "workspace": ws, "data_paths": [data], "force": False, "errors_tier": "fast",
        "render": False, "scan_errors_tier": "cheap", "master_errors_tier": "fast",
        "force_masters": None, "certification_manifest": None, "certification_request": None,
        "analysis_allowance_seconds": 30.0, "finish_initial_wave": True}]

    # 3. Certification: master-only pass — no role tiers, no force, a session-owned request file.
    certify = _Recorder()
    refresh_analysis(certify, ws, [data], errors_tier="full", force=False, render=False,
                     certification_request="req.json", analysis_allowance_seconds=30.0,
                     finish_initial_wave=False, enforce_completion=True)
    assert certify.calls == [{
        "workspace": ws, "data_paths": [data], "force": False, "errors_tier": "full",
        "render": False, "scan_errors_tier": None, "master_errors_tier": None,
        "force_masters": None, "certification_manifest": None,
        "certification_request": "req.json",
        "analysis_allowance_seconds": 30.0, "finish_initial_wave": False}]

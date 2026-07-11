"""Runner-seam unit tests (plan Stage 1): RunResult / SubprocessRunner / as_runner.

These lock the compat contract that keeps every existing config-supplied runner and test fake working
unchanged: ``as_runner`` must invoke a bare callable EXACTLY as the loop invoked ``run_isar`` before the
seam existed — ``obj(workspace, data_paths, extra_args=[...])`` with ``extra_args`` reconstructed from
the structured ``(force, errors_tier)`` kwargs.
"""
import isar.drive.isar_io.runner as runner_mod
from isar.drive.isar_io.runner import RunResult, SubprocessRunner, as_runner


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

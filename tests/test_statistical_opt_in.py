"""The campaign gate must hold even for an explicitly named test or marker; slow checks are opt-in too."""
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]


@pytest.mark.parametrize("args,passed,deselected", [
    ((), 1, 1),
    (("-m", "statistical"), 0, 2),
    (("test_statistical_campaign_requires_opt_in.py::test_campaign",), 0, 1),
    (("--run-statistical", "-m", "statistical"), 1, 1),
])
def test_statistical_campaign_requires_opt_in(pytester, args, passed, deselected):
    # Run the actual collection hook in an isolated, cheap synthetic suite.
    source = Path(__file__).resolve().parents[1] / "conftest.py"
    with source.open(encoding="utf-8", newline="") as stream:
        pytester.makeconftest(stream.read())
    pytester.makeini("[pytest]\nmarkers = statistical: explicit campaign")
    pytester.makepyfile(test_statistical_campaign_requires_opt_in="""
        import pytest

        def test_ordinary():
            pass

        @pytest.mark.statistical
        def test_campaign():
            pass
    """)
    result = pytester.runpytest_subprocess(*args, timeout=30)
    result.assert_outcomes(passed=passed, deselected=deselected)
    assert result.ret == (pytest.ExitCode.OK if passed else pytest.ExitCode.NO_TESTS_COLLECTED)


@pytest.mark.parametrize("args,passed,deselected", [
    ((), 1, 2),
    (("--run-slow",), 2, 1),
    (("--run-statistical", "-m", "statistical"), 1, 2),
])
def test_slow_check_requires_opt_in(pytester, args, passed, deselected):
    # The routine gate leaves the slow checks out; --run-slow brings them back. A campaign marked
    # both statistical and slow answers to --run-statistical alone.
    source = Path(__file__).resolve().parents[1] / "conftest.py"
    with source.open(encoding="utf-8", newline="") as stream:
        pytester.makeconftest(stream.read())
    pytester.makeini("[pytest]\nmarkers =\n    statistical: explicit campaign\n    slow: slow check")
    pytester.makepyfile(test_slow_check_requires_opt_in="""
        import pytest

        def test_ordinary():
            pass

        @pytest.mark.slow
        def test_check():
            pass

        @pytest.mark.statistical
        @pytest.mark.slow
        def test_campaign():
            pass
    """)
    result = pytester.runpytest_subprocess(*args, timeout=30)
    result.assert_outcomes(passed=passed, deselected=deselected)

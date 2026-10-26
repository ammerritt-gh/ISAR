"""The benchmark's routine slice: one real fit of one isolated peak through ``fit_one``.

The other benchmark tests (``test_count_bench.py``) run canned records; this one keeps the real
engine, the scoring and the writer in the routine suite with a single small case. The full run is
deliberate (``run-isar-count-bench.bat``).
"""
import json

import isar
from isar.synth import count_bench as cb


def test_one_isolated_peak_through_the_real_fit(tmp_path):
    assert cb.__file__.startswith(str(isar.__path__[0]))        # this worktree's benchmark
    # a clean_three-shaped case reduced to its strongest peak; the ceiling is the card's max_modes = 2,
    # passed explicitly so pytest's ISAR_MODE_CEILING=3 never decides it
    truth = cb._build(stream=cb.STREAM_NAMED, index=0, kind="clean_three", drawn=[(4.0, 0.3, 1.0)],
                      planted=None, level="high", resolution=cb.ANALYZERS[0], background_on=True,
                      ceiling=2, name="clean_one")
    results, info = cb.run_benchmark([(truth, 0)], base_seed=1, n_workers=1, draw_stream=cb.STREAM_DRAW_FIT)
    assert info.n_completed == 1
    (res,) = results
    assert res.status == "ok" and res.record is not None
    sf = [p for p in res.true_peaks if p["should_find"]]
    assert len(sf) == 1 and sf[0]["outcome"] in ("confident", "tentative_or_abstained", "missed")
    paths = cb.write_outputs(tmp_path / "out", results, cb.summarize(results), {"note": "routine"}, base_seed=1)
    lines = [json.loads(s) for s in paths["records.jsonl"].read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 1 and lines[0]["fast_pass"] == res.record and lines[0]["truth"]["name"] == "clean_one"

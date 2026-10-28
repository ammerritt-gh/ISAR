"""deep_pass with a process pool as map_fn gives the same result as serial (picklability and
determinism, plan S2U1). ``Gauss`` stays at module level so spawned workers unpickle it."""
import math
import json
import os
from concurrent.futures import ProcessPoolExecutor

import numpy as np

from isar.analyze import deep_pass as dp


class Gauss:
    def __init__(self, d, s=0.02, offset=0.0):
        self.d, self.s, self.offset = d, s, offset

    def __call__(self):
        d, s, off = self.d, self.s, self.offset
        c = -0.5 * d * math.log(2 * math.pi * s * s) + off

        def loglike(x):
            return c - 0.5 * float(np.sum((x - 0.5) ** 2)) / (s * s)

        return loglike, (lambda u: u), d


def entry(label, count, d, offset):
    return dp.ModelEntry(label, count, d, offset, None, Gauss(d, offset=offset))


def test_pooled_deep_pass_equals_serial(capfd):
    fast = dp.SamplerSettings(n_live=100, dlogz=0.1, calibrated_sigma=0.5)
    expl = dp.SamplerSettings(n_live=50, dlogz=0.5, calibrated_sigma=2.0)
    es = [entry("c1", 1, 4, 0.0), entry("c2", 2, 5, -1.0), entry("c3", 3, 6, -30.0)]
    serial = dp.deep_pass(es, accepted=fast, exploratory=expl, seed=9, chunk=2)
    with ProcessPoolExecutor(2) as ex:
        pooled = dp.deep_pass(es, accepted=fast, exploratory=expl, seed=9, map_fn=ex.map, chunk=2)
    a = [(m.label, m.status, m.log_z) for m in serial.models]
    b = [(m.label, m.status, m.log_z) for m in pooled.models]
    assert a == b
    assert [r.n_like_calls for m in serial.models for r in m.runs] == \
        [r.n_like_calls for m in pooled.models for r in m.runs]
    # Spawned workers flush to the inherited stderr, the same stream the GUI's queue log retains.
    rows = [json.loads(line.split("deep_timing ", 1)[1])
            for line in capfd.readouterr().err.splitlines() if "deep_timing " in line]
    completed = [r for r in rows if r["event"] == "run_completed" and r["pid"] != os.getpid()]
    assert {r["label"] for r in completed} == {e.label for e in es}
    assert all(r["workers"] == 2 for r in completed)

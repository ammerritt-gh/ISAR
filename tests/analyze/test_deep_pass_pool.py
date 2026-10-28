"""deep_pass with a process pool as executor gives the same result as serial (picklability and
determinism, plan S2U1). ``Gauss`` stays at module level so spawned workers unpickle it."""
import math
import os
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context

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


def test_pooled_deep_pass_equals_serial():
    fast = dp.SamplerSettings(n_live=100, dlogz=0.1, calibrated_sigma=0.5)
    expl = dp.SamplerSettings(n_live=50, dlogz=0.5, calibrated_sigma=2.0)
    es = [entry("c1", 1, 4, 0.0), entry("c2", 2, 5, -1.0), entry("c3", 3, 6, -30.0)]
    serial = dp.deep_pass(es, accepted=fast, exploratory=expl, seed=9, workers=2)
    events = []
    with ProcessPoolExecutor(2, mp_context=get_context("spawn")) as ex:
        pooled = dp.deep_pass(es, accepted=fast, exploratory=expl, seed=9, executor=ex, workers=2,
                              progress=events.append)
    a = [(m.label, m.status, m.log_z) for m in serial.models]
    b = [(m.label, m.status, m.log_z) for m in pooled.models]
    assert a == b
    assert [r.n_like_calls for m in serial.models for r in m.runs] == \
        [r.n_like_calls for m in pooled.models for r in m.runs]
    rows = [row for event in events for row in event.get("timing", [])]
    completed = [r for r in rows if r["event"] == "run_completed" and r["pid"] != os.getpid()]
    assert {r["label"] for r in completed} == {e.label for e in es}
    assert all(r["workers"] == 2 for r in completed)

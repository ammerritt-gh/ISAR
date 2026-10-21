"""Proofs: real-data checks the operator runs on purpose (``python -m pytest proofs``).

A proof is not a test: it reads the gitignored ``real_data/`` folder that exists only in the main
checkout, and it is never collected by the routine suite (``pyproject.toml`` ``testpaths``).
Because proofs are run deliberately, a missing real file or a missing expectation file is an
ERROR naming the path — never a skip, which would read as a pass.

Measured expectations (scan and point counts read from data, vetted anchor energies, comparison
statistics) are not committed. Each proof module keeps them in
``real_data/<data folder>/<proof module stem>.expect.json`` — one flat JSON object, keys named after
the quantity — and loads them through the ``expect`` fixture. There are no default values.
Facts that come from headers or instrument parameters may stay in a proof's code.
"""
import json
import os
from pathlib import Path

import pytest

# A job worktree reads the main checkout's real data through ISAR_REAL_DATA (read only).
REAL_DATA = Path(os.environ.get("ISAR_REAL_DATA") or Path(__file__).resolve().parents[1] / "real_data")


@pytest.fixture
def real_file():
    """``real_file("APS S30", "x.spec")`` -> path under ``real_data/``; fails naming it if absent."""
    def get(*parts):
        path = REAL_DATA.joinpath(*parts)
        if not path.exists():
            pytest.fail(f"proof needs real data that is missing: {path}", pytrace=False)
        return path
    return get


@pytest.fixture
def expect(request):
    """``expect("APS S30")`` -> this proof module's expectation dict from that data folder."""
    def load(data_folder):
        path = REAL_DATA / data_folder / f"{request.node.path.stem}.expect.json"
        if not path.is_file():
            pytest.fail(f"proof needs its expectation file, which is missing: {path}", pytrace=False)
        return json.loads(path.read_text(encoding="utf-8"))
    return load

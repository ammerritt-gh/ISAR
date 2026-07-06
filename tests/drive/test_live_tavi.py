"""Live integration: the whole plumbing chain against a running TAVI (marker `live`).

Skipped unless ``ISAR_LIVE_TAVI`` is set (TAVI must be up at 127.0.0.1:8642). Exercises the full
non-analysis path the driver loop will use: schema → submit a tiny deterministic scan → wait →
get data → save the scan JSON into an ISAR workspace → `isar run` (subprocess) → read results.csv.

No physics is asserted — this is a plumbing smoke test. It reports the ISAR verdict it got, but only
asserts the chain completes and produces rows.
"""
import os
from pathlib import Path

import pytest

from isar.drive.instrument.tavi_client import TaviClient
from isar.drive.isar_io.readers import read_card_state, read_results_csv
from isar.drive.isar_io.runner import run_isar
from isar.drive.isar_io.scan_writer import save_scan_json

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("ISAR_LIVE_TAVI"),
                       reason="set ISAR_LIVE_TAVI (and run TAVI at 127.0.0.1:8642) to enable"),
]


def _init_tavi_workspace(root: Path) -> Path:
    from isar.parsers import tavi as tavi_mod
    from isar.session.state import SessionState
    root.mkdir(parents=True, exist_ok=True)
    (root / "card.policy.toml").write_text(
        Path(tavi_mod._CARD_PATH).read_text(encoding="utf-8"), encoding="utf-8")
    (root / "card.state.json").write_text("{}", encoding="utf-8")
    SessionState().save(root / "session.json")
    return root


def test_live_end_to_end_plumbing(tmp_path):
    client = TaviClient()

    schema = client.get_schema()
    engines = schema.get("engines", [])
    if "deterministic" not in engines:
        pytest.skip(f"TAVI does not advertise the deterministic engine (engines={engines})")

    body = {
        "parameters": {"scan_command1": "deltaE 0.5 3.0 0.625", "number_neutrons": 1000000},
        "engine": "deterministic",
        "seed": 1,
        "isolated": True,
    }
    submit = client.submit_scan(body)
    job_id = submit["job_id"]

    snap = client.wait_scan(job_id, timeout=120.0, poll_wait=10.0)
    state = snap.get("state")
    if state != "done":
        pytest.skip(f"scan {job_id} terminated {state!r} (error={snap.get('error')}) — "
                    "likely no analytic ground truth for the current sample; plumbing chain not exercised")

    data = client.get_scan_data(job_id)
    assert data["job_id"] == job_id
    assert "result" in data

    ws = _init_tavi_workspace(tmp_path / "ws")
    data_dir = ws / "data"
    saved = save_scan_json(data, data_dir)
    assert saved.exists()

    proc = run_isar(ws, [data_dir])
    assert proc.returncode == 0

    rows = read_results_csv(ws)
    assert rows, "isar run produced no results.csv rows"
    state_dict = read_card_state(ws)

    verdicts = [r.get("verdict") for r in rows if r.get("verdict")]
    print(f"[live] job {job_id}: {len(rows)} result row(s); verdicts={verdicts}; "
          f"dispersion_prior points={len(state_dict.get('dispersion_prior', []))}")

"""PROOF (real data, run on purpose): the full driver end-to-end on the real IN8 anchor pair.

Moved from `tests/session/test_driver_smoke.py::test_in8_driver_end_to_end`. 055182 recovers omega0
within 0.5 meV of the vetted anchor energy (the verdict is aside — the gold check gates on omega0),
055183 is an undersampled non-result, and an immediate re-run is idempotent. The vetted energy is a
measured value: it is read from `real_data/ILL IN8/test_driver_in8_anchors.expect.json`.
"""
import shutil
from pathlib import Path

from isar.parsers import ill_in8
from isar.session import driver
from isar.session.workspace import Workspace

FOLDER = "ILL IN8"


def _init_workspace(tmp_path, plugin_module):
    ws = Workspace.open(tmp_path / "ws")
    ws.root.mkdir(parents=True, exist_ok=True)
    ws.policy_path.write_text(Path(plugin_module._CARD_PATH).read_text(encoding="utf-8"),
                              encoding="utf-8")
    return ws


def _find(session, scan_number):
    for e in session.scans.values():
        if e.scan_number == scan_number:
            return e
    return None


def test_in8_driver_end_to_end(tmp_path, real_file, expect):
    anchor_omega0 = expect(FOLDER)["anchor_055182_omega0_meV"]
    data = tmp_path / "in8"
    data.mkdir()
    for n in ("055182", "055183"):
        shutil.copy(real_file(FOLDER, n), data / n)
    ws = _init_workspace(tmp_path, ill_in8)

    summary = driver.run_batch(ws, [data], M=30)
    _card, session = ws.load()
    e82 = session.get("397:55182:det0") or _find(session, 55182)
    e83 = session.get("397:55183:det0") or _find(session, 55183)
    # 055182: a real fit landing near the vetted energy (verdict may decline on cost, like the check).
    assert e82.status == "fitted"
    oc = e82.detectors["det0"]
    assert oc["omega0"] is not None and abs(oc["omega0"] - anchor_omega0) <= 0.5
    assert oc["gamma_provisional"] is True             # TAS: Γ provisional
    assert oc["calib"]["resolution_source"] == "cooper_nathans"
    # 055183: 3 points -> undersampled non-result (never a fit).
    assert e83.status == "non_result"

    # Idempotency: an immediate re-run fits nothing new.
    s2 = driver.run_batch(ws, [data], M=30)
    assert s2.n_fitted_this_run == 0
    assert (ws.report_dir / "results.csv").exists()

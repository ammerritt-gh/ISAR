"""PROOF (real data, run on purpose): BL43LXU tabulated resolution on the real CsV3Sb5 set.

The real folder goes through discovery, parsing, reduction and QC, then the calibration the run uses
(``router.route`` -> ``calibrations.refresh_tabulated``), in a fresh workspace. The card names the real
``rf/`` resolution folder (read only, by absolute path) and declares no slit, so each scan's slit is
inferred. Checks: the 302 K and 97 K scans infer the same slit; every analyzer's zero is a fitted
elastic zero within ZERO_TOL of the file's axis zero, or a labelled pipeline zero. Only structure and
bounds are asserted and printed: no measured value is written here, so no expectation file is needed.
"""
import shutil
from pathlib import Path

import pytest

from isar.parsers import sp8_bl43
from isar.reduce import combine, qc
from isar.session import calibrations, ingest, router
from isar.session.state import SessionState
from isar.session.workspace import Workspace

FOLDER = "leTacon_SP8_CsV3Sb5"
N_ANALYZERS = 28
ZERO_TOL = 0.3          # meV: a fitted elastic zero sits this close to the file's own axis zero
SCANS = {302: "302 K", 97: "97 K"}     # the two scans the proof compares, by their temperature setpoint


@pytest.fixture
def calibrated(real_file, tmp_path):
    """(card, {temperature: (scan state entry, record)}, pipeline_zero_count) for the real folder."""
    folder = real_file(FOLDER)
    rf = real_file(FOLDER, "rf")
    ws = Workspace.open(tmp_path / "ws")
    ws.root.mkdir()
    text = Path(sp8_bl43._CARD_PATH).read_text(encoding="utf-8")
    assert 'resolution_folder = "rf"' in text
    ws.policy_path.write_text(text.replace('resolution_folder = "rf"', f'resolution_folder = "{rf.as_posix()}"'),
                              encoding="utf-8", newline="\n")
    ws.state_path.write_text("{}", encoding="utf-8")
    SessionState().save(ws.session_path)
    card, _session = ws.load()
    records = ingest.parse_all(ingest.discover([folder], card), card)
    for rec in records:
        combine.reduce_record(rec, card)
        qc.qc_record(rec, card)
    calibrations.refresh_tabulated(card, router.route(records).measurements, base_dir=ws.root)
    by_t = {}
    for rec in records:
        key = f"{rec.experiment_id}:{rec.scan_number}"
        t = round(float(rec.facts["T_K"]["value"]))
        by_t[t] = (card.state.tabulated_calibration[key], rec)
    assert set(by_t) == set(SCANS), f"expected the 302 K and 97 K scans, got {sorted(by_t)}"
    return by_t


def test_each_scan_has_one_zero_per_analyzer(calibrated):
    for t, (entry, _rec) in calibrated.items():
        assert len(entry["detectors"]) == N_ANALYZERS, f"{t} K"


def test_slit_inferred_is_the_same_at_302_K_and_97_K(calibrated):
    slits = {}
    for t, (entry, _rec) in calibrated.items():
        assert entry["declared"] is None and entry["setting_source"] == "inferred", f"{t} K: slit was declared"
        slits[t] = entry["setting"]
    assert slits[302] is not None and slits[302] == slits[97], f"slit disagrees: {slits}"


def test_every_analyzer_zero_is_fitted_within_tolerance_or_a_labelled_pipeline_zero(calibrated):
    problems = []
    for t, (entry, _rec) in calibrated.items():
        for det, d in entry["detectors"].items():
            if "held_reason" in d:
                problems.append(f"{t} K {det}: held ({d['held_reason']}), has no zero")
            elif d["zero_source"] == "elastic_fit":
                if not abs(d["zero"]) <= ZERO_TOL:
                    problems.append(f"{t} K {det}: fitted zero {d['zero']:.3f} meV is outside +/-{ZERO_TOL}")
            elif d["zero_source"] == "pipeline_zero":
                types = [f["type"] for f in d["flags"]]
                if d["zero"] != 0.0 or types != ["pipeline_zero"]:
                    problems.append(f"{t} K {det}: pipeline zero not labelled ({d['zero']}, {types})")
            else:
                problems.append(f"{t} K {det}: unknown zero source {d['zero_source']!r}")
    assert not problems, f"{len(problems)} analyzer zero problem(s):\n" + "\n".join(problems)


def test_operator_summary(calibrated, capsys):
    """Prints the inferred slit and its evidence for the operator, uncaptured; asserts nothing here."""
    with capsys.disabled():
        for t in sorted(calibrated, reverse=True):
            entry, _rec = calibrated[t]
            print(f"\n{SCANS[t]}: slit {entry['setting']} ({entry['setting_source']})")
            print("  summed deviance per candidate: " + ", ".join(
                f"{s} {v:.1f}" for s, v in sorted(entry["deviances"].items())))
            print("  flags: " + (", ".join(f["type"] for f in entry["flags"]) or "none"))
            fallbacks = [det for det, d in entry["detectors"].items() if d.get("zero_source") == "pipeline_zero"]
            held = [det for det, d in entry["detectors"].items() if "held_reason" in d]
            print(f"  pipeline-zero fallbacks: {len(fallbacks)} ({', '.join(fallbacks) or 'none'})")
            print(f"  held analyzers: {len(held)} ({', '.join(held) or 'none'})")

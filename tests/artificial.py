"""Where the committed artificial copies of facility files live, and their truth files.

`artificial_data/` holds copies made by `tools/artificial_copy.py`: the facility's own layout with
every data-row number replaced. Each copy has a `<name>.truth.json` listing what the tool wrote
(scans, points, column names, header values as text, each data column's tokens). Parser tests
assert against that truth, never against a number read from the copy by the parser under test.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "artificial_data"

S30 = ROOT / "APS S30" / "FeSe_1-xS_x_1.spec_supplementary.dat"
RAW = ROOT / "APS S30 raw" / "Fe2Mo3O8_trimmed.spec"
RAW_SUPP = ROOT / "APS S30 raw" / "Fe2Mo3O8_trimmed.spec_supplementary.dat"
IN8_ALIGN = ROOT / "ILL IN8" / "055177"      # sc a3 0 da3 2 np 31 ti 10  (motor alignment)
IN8_MEAS = ROOT / "ILL IN8" / "055181"       # bs qh 4 -0.2 0 1 ... np 41 mn 100000  (Q-E measurement)
IN8_ANCHOR = ROOT / "ILL IN8" / "055182"     # bs qh 4 -0.4 0 1 ... np 51 mn 50000   (ends at 25 points)

# BL43LXU (SPring-8): whole-file tables, so a truth file has one "scan" (number 0): scan_truth(path, 0).
BL43 = ROOT / "SP8 BL43LXU"
BL43_STEM = "CsV3Sb5_302K_0p5_0p5_3_sum"      # the set key of the CsV3Sb5 layout below
BL43_A03 = BL43 / f"{BL43_STEM}_a03"         # per-analyzer file, `_i` and `_lr` families
BL43_A04 = BL43 / f"{BL43_STEM}_a04"         # another analyzer, one bin longer than a03
BL43_VAR = BL43 / f"{BL43_STEM}.var"         # the stem's sidecar: lists all 28 analyzers
BA122 = BL43 / "spec1_T26K__scan_58_A6_Q_4_0p075_0_inel.dat"   # Ba122 export: analyzer 6, Q in the name
SARF_60 = BL43 / "sarf_24Nov10_s48_60_60_a03"          # resolution curve, analyzer a03, slit 60x60
SARF_60_GPI = BL43 / "sarf_24Nov10_s48_60_60_a03.gpi"  # its fitted summary (name = value lines)
SARF_20 = BL43 / "sarf_24Nov10_s52_20_20_a03"          # the same analyzer at slit 20x20
SARF_20_GPI = BL43 / "sarf_24Nov10_s52_20_20_a03.gpi"


def truth(path: Path) -> dict:
    return json.loads(Path(str(path) + ".truth.json").read_text(encoding="utf-8"))


def scan_truth(path: Path, scan_number: int) -> dict:
    return next(s for s in truth(path)["scans"] if s["scan_number"] == scan_number)


def tokens(scan: dict, column: str) -> list[str]:
    """The text tokens the tool wrote for the first column of this name."""
    return next(c["tokens"] for c in scan["data_columns"] if c["name"] == column)


def values(scan: dict, column: str) -> list[float]:
    return [float(t) for t in tokens(scan, column)]

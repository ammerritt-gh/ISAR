"""The committed artificial copy of a real BL43 gpi macro (`artificial_data/SP8 BL43LXU/gpi_files/`), read by ISAR's reader.

tools/artificial_copy.py replaced every number in it; the set name, the slit and the curve set are the real file's
kept settings, so the asserts here are on what the reader makes of the structure, not on any measured value.
"""
from pathlib import Path

from isar.intake.gpi import read_gpi

GPI = (Path(__file__).resolve().parents[2] / "artificial_data" / "SP8 BL43LXU" / "gpi_files"
       / "gpi.CsV3Sb5_302K_0p5_0p5_3_sum")
ANALYZERS = ["a03", "a04", "a05", "a06", "a07", "a08", "a09", "a14", "a15", "a16", "a17", "a18", "a19", "a20",
             "a25", "a26", "a27", "a28", "a29", "a30", "a31", "a35", "a36", "a37", "a38", "a39", "a40", "a41"]


def test_the_copy_gives_every_analyzers_hkl_and_q_and_the_kept_settings():
    m = read_gpi(GPI)
    assert sorted(m.hkl) == ANALYZERS and sorted(m.q_nm) == ANALYZERS
    assert all(v is not None and len(v) == 3 for v in m.hkl.values())
    assert all(q is not None and q > 0 for q in m.q_nm.values())
    assert (m.problem_name, m.slit, m.sarf_name) == ("CsV3Sb5_302K_0p5_0p5_3_sum", "40x40", "sarf_24Nov10_s50_40_40")
    assert m.unread == ()

"""Emitter byte-fidelity to the real S30 format (sharpens the §4 eyeball; D5 only requires
schema-faithful, but a realistic synthetic file makes the human pause meaningful).

Verified against the real file: header/#V/#Q use SHORT native number format (`2  0.050001
-5.65969e-06`), not verbose `%.18e`; data rows use `%.18e`; the per-scan block carries #G/#P
geometry+motor blocks, a full #V0..#V15, and the global header carries #O motor names.
"""
import numpy as np

from isar.parsers import spec_s30
from isar.synth_format import run_builder


def _emit_text(n_scans=1):
    card = spec_s30.card_template()
    run = run_builder.build_s30_run("FID", n_scans=n_scans)
    return spec_s30.emit_file(run, card).decode("utf-8"), run


def test_header_values_use_short_native_format_not_verbose_scientific():
    text, _ = _emit_text()
    qline = next(l for l in text.splitlines() if l.startswith("#Q "))
    v8 = next(l for l in text.splitlines() if l.startswith("#V8 "))
    # Real S30 writes "#Q 2 0.050001 ..." — short. The emitter must not write "2.2500...e+00".
    assert "e+00" not in qline and "e+00" not in v8
    assert qline.split()[1] == "2.25"        # central q_h, clean


def test_data_rows_still_use_high_precision_scientific():
    text, _ = _emit_text()
    data_tok = next(l for l in text.splitlines()
                    if l and not l.startswith("#"))[:1] and \
        next(l for l in text.splitlines() if l and not l.startswith("#")).split()[0]
    assert "e" in data_tok                    # data table stays %.18e like the real file


def test_emitted_block_carries_geometry_motor_and_full_v_block():
    text, _ = _emit_text()
    markers = {l.split()[0] for l in text.splitlines() if l.startswith("#")}
    for m in ["#O0", "#O20", "#G0", "#G4", "#P0", "#P20",
              "#V0", "#V1", "#V3", "#V13", "#V14", "#V15"]:
        assert m in markers, f"missing {m}"


def test_emitted_file_reparses_with_full_v_groups():
    # The full #V0..#V15 must re-read; the parser still keys on #V2/#V4..#V12 (unchanged).
    import tempfile
    from pathlib import Path
    from isar.parsers import spec_base

    text, run = _emit_text()
    p = Path(tempfile.mkdtemp()) / "fid.spec_supplementary.dat"
    p.write_bytes(text.encode("utf-8"))
    _, blocks = spec_base.read_spec(p)
    b = blocks[0]
    assert set(range(0, 16)) <= set(b.V_groups)           # #V0..#V15 all present
    assert np.allclose(b.V_groups[8], run.scans[0].central_q)  # #V8 still the central Q

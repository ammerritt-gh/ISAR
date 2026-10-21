"""BL43LXU reader on files written at test time: everything that depends on the numbers.

Counts are raw (recovered from printed intensity and error), monitor and time travel per analyzer and
are never divided in, `_lr` is kept unrounded and flagged, a header fact beats the file name's, header
slit keys become the one generic `slit` fact, and analyzers with different bins share a union axis
whose missing bins are masked and flagged. The union axis is tested on files written directly here,
never through `emit_file`: a written file cannot tell an absent bin from a measured zero.
"""
from __future__ import annotations

import zlib
from pathlib import Path

import numpy as np
import pytest

from isar.parsers import sp8_bl43
from isar.schema import axes, scan
from isar.session import ingest

STEM = "Smp_100K_1_0p5_m2_run"
LABEL = ("#      meV   meV_sft   tmK_lin     lrn      secs        norm_lr         {a}_lr       error_lr"
         "       y_unitpk          error            ytc           ytce    npts         norm_i"
         "            y_i           ye_i")


def lattice(lo: int, hi: int, step: float = 0.2) -> np.ndarray:
    """Bin centres lo..hi-1 steps of `step` meV from zero."""
    return np.round(np.arange(lo, hi) * step, 4)


def write_member(folder: Path, det: str, energy, counts, monitor, *, secs=10.0, header=(), lr=None,
                 stem: str = STEM, fmt: str = "%.6E", label: str | None = None) -> Path:
    """One CsV3Sb5-layout analyzer file in the beamline's 16 columns. `_i`: y = N/norm, e = sqrt(N)/norm
    (0 over 0 for N = 0). `lr` (non-integer counts) fills the `_lr` family, whose time is lrn x 0.25 s."""
    energy, counts = np.asarray(energy, float), np.asarray(counts, float)
    monitor = np.broadcast_to(np.asarray(monitor, float), energy.shape)
    secs = np.broadcast_to(np.asarray(secs, float), energy.shape)
    lr = counts if lr is None else np.asarray(lr, float)
    lines = [h for h in header] + [label or LABEL.format(a=det)]
    for i in range(len(energy)):
        f = lambda v: fmt % v                                              # noqa: E731
        row = [f"{energy[i]:10.2f}", f"{energy[i]:9.2f}", f"{0.0:9.1f}", f"{int(secs[i] * 4):7d}",
               f"{secs[i]:9.1f}", f(monitor[i]), f(lr[i] / monitor[i]), f(lr[i] ** 0.5 / monitor[i]),
               f(0.0), f(0.0), f(0.0), f(0.0), f"{int(round(secs[i] / 10)):7d}", f(monitor[i]),
               f(counts[i] / monitor[i]), f(counts[i] ** 0.5 / monitor[i])]
        lines.append(" ".join(row))
    lines += ["#Q=   18.815 /nm", "#T=  100.000 K"]                      # trailing comments are skipped
    path = folder / f"{stem}_{det}"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return path


def write_var(folder: Path, analyzers, stem: str = STEM, bin_meV: float = 0.2) -> Path:
    lines = [f' file_name_prefix = "{stem}"', " silicon_order =    12", f" meV_bin_size =  {bin_meV:6.2f}",
             " nscans =     2", "", f"array ano[{len(analyzers)}]", ""]
    for i, a in enumerate(analyzers, 1):
        lines += [f"ano[{i}] = {a}", f"analeff[{i}] = 0.5{i}", f"anal_polfactor[{i}] = 0.9{i}",
                  f"elpk_centroid_meV[{i}] = 0.0{i}", f"elpk_area_meV[{i}] = 0.2E-0{i}", ""]
    path = folder / f"{stem}.var"
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    return path


def parse(folder: Path, **card_edits):
    card = sp8_bl43.card_template()
    for key, value in card_edits.items():
        setattr(card.policy, key, value)
    plan = ingest.discover([folder], card)
    assert len(plan.file_sets) == 1, [fs.key for fs in plan.file_sets]
    records = sp8_bl43.parse_set(plan.file_sets[0], card)
    assert len(records) == 1
    return records[0]


def flags(rec: scan.ScanRecord, kind: str) -> list[scan.Flag]:
    return [f for f in rec.scan_flags if f.type == kind]


# ---------------------------------------------------------------------------
# Raw counts, exposure beside them
# ---------------------------------------------------------------------------
def test_integer_counts_recovered_exactly_and_zero_zero_bin_kept(tmp_path):
    rng = np.random.default_rng(7)
    counts = rng.integers(0, 3000, 60).astype(float)
    counts[[0, 17, 59]] = 0                                               # zero counts: y = e = 0
    write_member(tmp_path, "a03", lattice(-10, 50), counts, rng.uniform(1e6, 2e6, 60))
    sp = parse(tmp_path).spectra["a03"]
    assert np.array_equal(sp.intensity, counts)                           # exact, no rounding drift
    assert np.array_equal(sp.variance, counts)                            # sigma^2 = N, no floor
    assert sp.variance[[0, 17, 59]].tolist() == [0.0, 0.0, 0.0]
    assert sp.mask.all() and not sp.point_flags and not sp.spectrum_flags


def test_monitor_and_time_are_per_analyzer_and_never_folded_in(tmp_path):
    e = lattice(0, 20)
    write_member(tmp_path, "a03", e, np.full(20, 40.0), np.full(20, 2.0e6), secs=10.0)
    write_member(tmp_path, "a04", e, np.full(20, 40.0), np.full(20, 5.0e5), secs=30.0)
    rec = parse(tmp_path)
    assert np.array_equal(rec.spectra["a03"].intensity, rec.spectra["a04"].intensity)   # not divided by norm
    assert np.all(rec.spectra["a03"].monitor == 2.0e6) and np.all(rec.spectra["a04"].monitor == 5.0e5)
    assert np.all(rec.spectra["a03"].time == 10.0) and np.all(rec.spectra["a04"].time == 30.0)
    assert np.isnan(rec.normalization.monitor).all() and np.isnan(rec.normalization.time).all()
    assert scan.exposure_for(rec, "a04").time[0] == 30.0                  # the one place that applies it
    assert rec.energy_calibration.status == "needs_zero"
    assert rec.energy_calibration.elastic_zero_offset is None


def test_lr_family_is_unrounded_flagged_and_timed_by_lrn(tmp_path):
    e = lattice(0, 10)
    lr = np.linspace(10.3, 55.7, 10)
    write_member(tmp_path, "a03", e, np.full(10, 20.0), np.full(10, 1e6), secs=40.0, lr=lr)
    rec = parse(tmp_path, reader={"intensity_family": "_lr"})
    sp = rec.spectra["a03"]
    assert np.allclose(sp.intensity, lr, rtol=1e-5)
    assert not np.allclose(sp.intensity, np.round(sp.intensity), atol=1e-3)   # kept unrounded
    flag = [f for f in sp.spectrum_flags if f.type == "counts_not_integer"]
    assert len(flag) == 1 and flag[0].severity == "warning" and "a03" in flag[0].reason
    assert np.all(sp.time == 40.0 * 4 * 0.25)                              # lrn x 0.25 s


def test_unknown_intensity_family_is_refused(tmp_path):
    write_member(tmp_path, "a03", lattice(0, 5), np.ones(5), 1e6)
    with pytest.raises(ValueError, match="intensity_family"):
        parse(tmp_path, reader={"intensity_family": "_zz"})


# ---------------------------------------------------------------------------
# Facts and their sources
# ---------------------------------------------------------------------------
def test_header_fact_overrides_filename_and_raises_fact_conflict(tmp_path):
    write_member(tmp_path, "a03", lattice(0, 10), np.ones(10), 1e6, header=["# T_K 250"])
    rec = parse(tmp_path)
    t = rec.facts["T_K"]
    assert (t["value"], t["source"]) == (250.0, "header")
    assert t["conflicts"] == [{"source": "filename", "value": 100.0}]
    assert [f.severity for f in flags(rec, "fact_conflict")] == ["warning"]
    assert np.all(rec.temperature.setpoint == 250.0)
    assert rec.facts["hkl_nominal"] == {"value": [1.0, 0.5, -2.0], "source": "filename"}


def test_header_slit_keys_become_the_generic_slit_fact(tmp_path):
    write_member(tmp_path, "a03", lattice(0, 10), np.ones(10), 1e6,
                 header=["# agaph_mm 60", "# agapv_mm 40"])
    rec = parse(tmp_path)
    assert rec.facts["slit"] == {"value": "60x40", "source": "header"}
    assert rec.facts["agaph_mm"] == {"value": 60.0, "source": "header"}
    assert not flags(rec, "fact_conflict")
    # A card-declared slit ranks below the header and a disagreement is reported.
    rec = parse(tmp_path, facts={"slit": "50x50"})
    assert rec.facts["slit"]["value"] == "60x40"
    assert rec.facts["slit"]["conflicts"] == [{"source": "card", "value": "50x50"}]
    assert len(flags(rec, "fact_conflict")) == 1


def test_card_slit_is_used_when_no_header_gives_one(tmp_path):
    write_member(tmp_path, "a03", lattice(0, 10), np.ones(10), 1e6)
    assert parse(tmp_path).facts["slit"]["source"] == "missing"
    assert parse(tmp_path, facts={"slit": "50x50"}).facts["slit"] == {"value": "50x50", "source": "card"}


def test_header_hkl_is_rung_one_and_the_filename_hkl_is_never_a_spectrum_q(tmp_path):
    write_member(tmp_path, "a03", lattice(0, 10), np.ones(10), 1e6,
                 header=["# hkl 0.4 0.6 3.1", "# dq 0.02", "# Q_nm 19.5"])
    write_member(tmp_path, "a04", lattice(0, 10), np.ones(10), 1e6)
    rec = parse(tmp_path)
    a03, a04 = rec.spectra["a03"], rec.spectra["a04"]
    assert (a03.q, a03.q_source) == ([0.4, 0.6, 3.1], "header")
    assert scan.Q_SOURCE_RUNG[a03.q_source] == 1
    assert a03.facts["dq"] == {"value": 0.02, "source": "header"}
    assert a03.facts["Q_nm"] == {"value": 19.5, "source": "header"}
    assert (a04.q, a04.q_source) == (None, "none")                        # rung 4: no Q at all
    assert scan.Q_SOURCE_RUNG[a04.q_source] == 4
    assert a04.facts["hkl"] == {"value": None, "source": "missing"}
    assert "hkl_nominal" not in a03.facts and "hkl_nominal" not in a04.facts
    assert rec.facts["hkl_nominal"]["value"] == [1.0, 0.5, -2.0]


def test_identity_is_the_scan_number_when_known_else_a_hash_of_the_stem(tmp_path):
    write_member(tmp_path, "a03", lattice(0, 10), np.ones(10), 1e6)
    rec = parse(tmp_path)
    assert rec.scan_number == 1_000_000_000 + zlib.crc32(STEM.encode()) % 1_000_000_000
    assert rec.facts["scan_label"] == {"value": STEM, "source": "filename"}
    assert rec.experiment_id == "bl43lxu"
    (tmp_path / f"{STEM}_a03").write_text(
        "# scan 12 13\n" + (tmp_path / f"{STEM}_a03").read_text(encoding="utf-8"), encoding="utf-8")
    rec = parse(tmp_path)
    assert rec.scan_number == 12 and rec.facts["scan_numbers"] == {"value": [12, 13], "source": "header"}


# ---------------------------------------------------------------------------
# Union axis, absent bins, members left out
# ---------------------------------------------------------------------------
def test_union_axis_masks_absent_bins_and_keeps_a_measured_zero(tmp_path):
    ea, eb = lattice(0, 10), lattice(4, 16)                               # a03: bins 0-9, a04: bins 4-15
    ca = np.arange(10) + 5.0
    ca[0] = 0                                                             # a measured zero, present bin
    cb = np.arange(12) + 100.0
    write_member(tmp_path, "a03", ea, ca, np.full(10, 1e6), secs=20.0)
    write_member(tmp_path, "a04", eb, cb, np.full(12, 2e6), secs=30.0)
    rec = parse(tmp_path)
    grid = axes.scanned_axes(rec)[0].values
    assert np.allclose(grid, lattice(0, 16))                              # union, nothing resampled
    a, b = rec.spectra["a03"], rec.spectra["a04"]
    absent_a, absent_b = np.arange(10, 16), np.arange(0, 4)
    assert sorted(p.index for p in a.point_flags if p.type == "bin_absent") == absent_a.tolist()
    assert sorted(p.index for p in b.point_flags if p.type == "bin_absent") == absent_b.tolist()
    for sp, idx in ((a, absent_a), (b, absent_b)):
        assert not sp.mask[idx].any()
        assert np.all(sp.intensity[idx] == 0) and np.all(sp.variance[idx] == 0)
        assert np.all(sp.monitor[idx] == 0) and np.all(sp.time[idx] == 0)
    assert np.array_equal(a.intensity[:10], ca) and np.array_equal(b.intensity[4:], cb)
    assert a.mask[0] and not any(p.index == 0 for p in a.point_flags)     # zero counts is not "absent"
    assert np.all(a.monitor[:10] == 1e6) and np.all(b.time[4:] == 30.0)


def test_member_off_the_bin_lattice_or_lacking_the_family_is_left_out_and_named(tmp_path):
    e = lattice(0, 12)
    write_member(tmp_path, "a03", e, np.ones(12), 1e6)
    write_member(tmp_path, "a04", e + 0.1, np.ones(12), 1e6)             # half a bin off
    write_member(tmp_path, "a05", e, np.ones(12), 1e6,
                 label=LABEL.format(a="a05").replace("ye_i", "zz_i"))     # no error column of the _i family
    (tmp_path / f"{STEM}_a06").write_text("garbage\n", encoding="utf-8")  # unreadable
    write_var(tmp_path, [3, 4, 5, 6, 7])
    rec = parse(tmp_path)
    assert sorted(rec.spectra) == ["a03"]
    reason = flags(rec, "missing_analyzer")[0].reason
    assert "a04: " in reason and "off the bin lattice" in reason
    assert "a05: " in reason and "lacks the _i column family" in reason
    assert "a06: " in reason and "a07: no file" in reason
    assert len(flags(rec, "missing_analyzer")) == 1                       # one flag naming them all


def test_set_with_no_readable_member_is_an_error_not_an_empty_scan(tmp_path):
    (tmp_path / f"{STEM}_a03").write_text("garbage\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no analyzer could be read"):
        parse(tmp_path)


def test_sidecar_gives_bin_size_and_analyzer_facts_and_a_missing_one_is_flagged(tmp_path):
    write_member(tmp_path, "a03", lattice(0, 10), np.ones(10), 1e6)
    rec = parse(tmp_path)                                                 # no .var
    assert [f.type for f in rec.scan_flags if f.type.startswith("missing")] == ["missing_sidecar"]
    assert rec.facts["bin_meV"] == {"value": None, "source": "missing"}
    assert rec.spectra["a03"].facts["efficiency"]["source"] == "missing"
    write_var(tmp_path, [3])
    rec = parse(tmp_path)
    assert rec.facts["bin_meV"] == {"value": 0.2, "source": "sidecar"}
    assert rec.facts["si_order"] == {"value": 12, "source": "sidecar"}
    assert rec.facts["energy_keV"]["source"] == "inferred"
    assert rec.facts["energy_keV"]["value"] == pytest.approx(12 * 1.977)
    assert rec.spectra["a03"].facts["efficiency"] == {"value": 0.51, "source": "sidecar"}
    assert not flags(rec, "missing_sidecar") and not flags(rec, "missing_analyzer")


# ---------------------------------------------------------------------------
# emit_file inverts the reader for whole, aligned analyzers
# ---------------------------------------------------------------------------
def test_emit_file_round_trips_whole_aligned_analyzers(tmp_path):
    rng = np.random.default_rng(3)
    src, out = tmp_path / "src", tmp_path / "out"
    src.mkdir()
    e = lattice(-8, 40)
    for det, k in (("a03", 1), ("a06", 2)):
        write_member(src, det, e, rng.integers(0, 900, len(e)), rng.uniform(1e6, 2e6, len(e)),
                     secs=rng.integers(1, 5, len(e)) * 10.0)
    write_var(src, [3, 6])
    rec = parse(src)
    sp8_bl43.emit_file(rec, sp8_bl43.card_template(), out)
    back = parse(out)
    assert sorted(back.spectra) == ["a03", "a06"]
    assert back.facts["scan_label"]["value"] == STEM
    assert np.allclose(axes.scanned_axes(back)[0].values, e)
    for det in ("a03", "a06"):
        assert np.array_equal(back.spectra[det].intensity, rec.spectra[det].intensity)
        assert np.allclose(back.spectra[det].monitor, rec.spectra[det].monitor, rtol=1e-6)
        assert np.allclose(back.spectra[det].time, rec.spectra[det].time)
    assert not [f for f in back.scan_flags if f.type in ("missing_analyzer", "missing_sidecar")]

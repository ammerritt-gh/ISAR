"""The q-coordinate provider (C-q-provider): accepted per-analyzer hkl become points; Cartesian q only under a
lattice epoch. Every workspace is generated at test time; every number is made up (hard rule 10)."""
from __future__ import annotations

import math
from pathlib import Path

import pytest

import isar
from inter_q_fixtures import epoch, scan_number, set_epochs, spectrum_keys, workspace_with_gpi
from isar.session.inter_spectra.q_provider import q_coordinates

REPO = Path(__file__).resolve().parents[3]
STEM = "Smp_100K_0p5_0p5_3_A"            # the BL43 set layout: its file-name hkl is nominal, so an analyzer's hkl is gpi's alone
HKL = {"a03": (0.0, 0.0, 0.0), "a04": (0.1, 0.1, 0.0), "a05": (0.1, -0.1, 0.0)}
INVALID_CELL = "the recorded lattice epoch is not a valid cell"


def test_import_root_is_the_worktree():
    assert Path(isar.__file__).resolve().parents[1] == REPO


def test_an_accepted_gpi_hkl_is_a_point_with_no_cartesian_without_an_epoch(tmp_path):
    root = workspace_with_gpi(tmp_path, {STEM: HKL})
    keys = spectrum_keys(root)
    qc = q_coordinates(root, keys)
    assert set(qc.points) == set(keys) and qc.reasons == {} and qc.lattices == {}
    p = qc.points[f"{keys[0].rsplit(':', 1)[0]}:a04"]
    assert (p.hkl, p.source, p.cart, p.epoch) == ((0.1, 0.1, 0.0), "gpi", None, None)


def test_a_cubic_epoch_gives_the_true_reciprocal_spacing(tmp_path):
    root = workspace_with_gpi(tmp_path, {STEM: {"a03": (1.0, 0.0, 0.0), "a04": (1.1, 0.0, 0.0), "a05": None}})
    set_epochs(root, epoch(1, a=4.0))
    scan = spectrum_keys(root)[0].rsplit(":", 1)[0]
    qc = q_coordinates(root, spectrum_keys(root))
    step = math.dist(qc.points[f"{scan}:a03"].cart, qc.points[f"{scan}:a04"].cart)
    assert step == pytest.approx(0.1 * 2 * math.pi / 4)                    # 0.1571 1/A
    assert qc.points[f"{scan}:a04"].epoch == "lattice epoch v1"
    assert qc.lattices == {scan: ((4.0, 4.0, 4.0, 90.0, 90.0, 90.0), "lattice epoch v1")}


def test_a_hexagonal_epoch_gives_spacings_in_the_ratio_root_three(tmp_path):
    root = workspace_with_gpi(tmp_path, {STEM: HKL})
    set_epochs(root, epoch(1, a=4.0, c=7.0, gamma=120.0))
    qc = q_coordinates(root, spectrum_keys(root))
    scan = spectrum_keys(root)[0].rsplit(":", 1)[0]
    c = {d: qc.points[f"{scan}:{d}"].cart for d in ("a03", "a04", "a05")}
    long, short = math.dist(c["a03"], c["a04"]), math.dist(c["a03"], c["a05"])
    assert (round(long, 4), round(short, 4)) == (0.3142, 0.1814)
    assert long / short == pytest.approx(math.sqrt(3))


def test_an_analyzer_without_an_accepted_hkl_is_a_reason_and_an_unknown_key_says_so(tmp_path):
    root = workspace_with_gpi(tmp_path, {STEM: {"a03": (0.0, 0.0, 0.0), "a04": (0.1, 0.0, 0.0), "a05": None}})
    keys = spectrum_keys(root)
    scan = keys[0].rsplit(":", 1)[0]
    qc = q_coordinates(root, [*keys, f"{scan}:a99"])
    assert set(qc.points) == {f"{scan}:a03", f"{scan}:a04"}
    assert qc.reasons == {f"{scan}:a05": "no accepted hkl", f"{scan}:a99": "unknown spectrum"}


@pytest.mark.parametrize("bad", [dict(a=0.0), dict(gamma=180.0), dict(gamma=0.0)])
def test_a_scan_whose_epoch_is_not_a_valid_cell_gets_no_points_and_says_why(tmp_path, bad):
    root = workspace_with_gpi(tmp_path, {STEM: HKL})
    set_epochs(root, epoch(1, **bad))
    keys = spectrum_keys(root)
    qc = q_coordinates(root, keys)
    assert qc.points == {} and qc.lattices == {}
    assert qc.reasons == {k: INVALID_CELL for k in keys}


def test_each_scan_takes_the_epoch_in_force_for_its_own_scan(tmp_path):
    along = {"a03": (0.0, 0.0, 0.0), "a04": (0.1, 0.0, 0.0), "a05": None}
    root = workspace_with_gpi(tmp_path, {"Smp_100K_0p5_0p5_3_A": along, "Smp_200K_0p5_0p5_3_B": along})
    keys = spectrum_keys(root)
    first, second = dict.fromkeys(k.rsplit(":", 1)[0] for k in keys)
    set_epochs(root, epoch(1, a=4.0), epoch(2, a=5.0, scan_ref=scan_number(root, second)))
    qc = q_coordinates(root, keys)
    for scan, a, version in ((first, 4.0, 1), (second, 5.0, 2)):
        step = math.dist(qc.points[f"{scan}:a03"].cart, qc.points[f"{scan}:a04"].cart)
        assert step == pytest.approx(0.1 * 2 * math.pi / a)
        assert qc.points[f"{scan}:a04"].epoch == f"lattice epoch v{version}"
        assert qc.lattices[scan][1] == f"lattice epoch v{version}"


def test_an_epoch_without_a_source_still_names_its_version(tmp_path):
    root = workspace_with_gpi(tmp_path, {STEM: HKL})
    ep = epoch(3, a=4.0)
    del ep["source"]
    set_epochs(root, ep)
    qc = q_coordinates(root, spectrum_keys(root))
    assert [label for _cell, label in qc.lattices.values()] == ["lattice epoch v3"]

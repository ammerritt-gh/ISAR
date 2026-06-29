"""Step-6 energy-zero calibration from dedicated Bragg/elastic (herixE) scans (§7, D8) — ISAR fits
the elastic-line scans itself to fix the zero, the FeSe_1 technique. `read_elastic_scans` ingests the
raw .spec herixE ascans (multi-word #L, Ana7 column, K from #Q); `build_energy_calibration` fits each
and builds a scan#-keyed zero series + per-slit resolution. No raw data needed — synthetic .spec."""
import numpy as np

from isar.calibrate import bragg
from isar.calibrate.energy_zero import EnergyCalibration, build_energy_calibration


def _gauss(x, center, fwhm=1.0, amp=600.0, bg=10.0):
    sigma = fwhm / 2.35482
    return bg + amp * np.exp(-0.5 * ((np.asarray(x, float) - center) / sigma) ** 2)


def _spec_text():
    """A minimal raw SPEC file: a K=0 herixE elastic scan (keep), a K=0.15 herixE scan (drop on
    k_max), and a micro_x ascan (skip — not herixE). The #L carries a multi-word column name."""
    x = np.linspace(-5.0, 5.0, 21)
    L = ["#F /test/synthetic.spec", "#E 1700000000", "#D Fri Mar 07 14:00:00 2025",
         "#O0 micro_x  micro_y", ""]

    def herixE(scan, kval, center, dtime):
        rows = [f"#S {scan}  ascan  herixE -5 5  20 1",
                f"#D {dtime}", f"#Q 4 {kval} 0.0",
                "#P17 0.5 0.5 0.5 95 18.3 95 95 13", "#N 6",
                "#L HerixE  K  Heat Stage T1  Ana7  ICO-C  Seconds"]
        for xi, ci in zip(x, _gauss(x, center)):
            rows.append(f"{xi:.4f} {kval} 1.0 {ci:.4f} 100 1")
        rows.append("")
        return rows

    L += herixE(10, "0.0", 0.20, "Fri Mar 07 15:00:00 2025")
    L += herixE(12, "0.15", 0.0, "Fri Mar 07 15:30:00 2025")     # K!=0 -> filtered by k_max
    L += ["#S 14  ascan  micro_x 1 3  10 1", "#Q 4 0.0 0.0",
          "#P17 0.5 0.5 0.5 95 18.3 95 95 13", "#N 3", "#L micro_x  Ana7  Seconds"]
    for i in range(11):
        L.append(f"{1 + i * 0.2:.4f} {50 + i} 1")
    return "\n".join(L) + "\n"


def test_read_elastic_scans_selects_k0_herixE(tmp_path):
    p = tmp_path / "synthetic.spec"
    p.write_text(_spec_text(), encoding="utf-8")
    scans = bragg.read_elastic_scans(p)
    assert [s.scan_number for s in scans] == [10]      # K=0 herixE only
    s = scans[0]
    assert s.slit == 18                                # #P17[4] = 18.3 -> 18
    assert s.energy.size == 21
    assert abs(s.K) < 1e-9
    assert s.counts.max() > 400                        # Ana7 column resolved past the multi-word name


def test_read_elastic_scans_k_max_admits_higher_k(tmp_path):
    p = tmp_path / "synthetic.spec"
    p.write_text(_spec_text(), encoding="utf-8")
    scans = bragg.read_elastic_scans(p, k_max=0.5)
    assert [s.scan_number for s in scans] == [10, 12]  # K=0.15 now admitted


def test_build_energy_calibration_interpolates_drift():
    x = np.linspace(-5.0, 5.0, 21)
    scans = [bragg.ElasticScan(scan_number=10, time=0.0, K=0.0, slit=18,
                               energy=x, counts=_gauss(x, -0.20)),
             bragg.ElasticScan(scan_number=20, time=0.0, K=0.0, slit=18,
                               energy=x, counts=_gauss(x, 0.20)),
             bragg.ElasticScan(scan_number=30, time=0.0, K=0.0, slit=18,
                               energy=x, counts=_gauss(x, 0.60))]
    calib = build_energy_calibration(scans)
    assert isinstance(calib, EnergyCalibration)
    assert abs(calib.zero(20) - 0.20) < 0.1            # own node
    assert abs(calib.zero(15) - 0.0) < 0.1             # interpolated between -0.2 and +0.2
    assert calib.zero(5) is None and calib.zero(40) is None   # outside coverage -> held


def test_energy_calibration_resolution_by_slit():
    x = np.linspace(-5.0, 5.0, 21)
    scans = [bragg.ElasticScan(10, 0.0, 0.0, 18, x, _gauss(x, 0.0, fwhm=1.0)),
             bragg.ElasticScan(20, 0.0, 0.0, 18, x, _gauss(x, 0.0, fwhm=1.0)),
             bragg.ElasticScan(30, 0.0, 0.0, 60, x, _gauss(x, 0.0, fwhm=1.6))]
    calib = build_energy_calibration(scans)
    g18, l18 = calib.resolution(18)
    g60, l60 = calib.resolution(60)
    assert g18 > 0 and g60 > g18                        # wider slit -> wider resolution
    assert calib.resolution(999) == calib.default_resolution   # unknown slit -> default

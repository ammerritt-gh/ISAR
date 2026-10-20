"""Build a synthetic facility "run" from Phase 1 ground truth (§4).

One physical S30 scan carries nine analyzer spectra at nine Q values. This wraps Phase 1's
production generator — `make_ground_truth` + `draw_counts` (seeded Poisson on the real lineshape)
— into a multi-analyzer `Run` the emitter serializes to SPEC bytes. The signal is *fanned* across
the analyzers by giving each its own `GroundTruth` at its own H (its own Q). Scan generation is
**not** re-implemented here — it is Phase 1's, reused.

Other scan types (§5.6) would add new `RunScan` kinds without reworking this; Phase 2 builds only
the constant-Q-energy case.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from isar.analyze.lineshapes import Voigt
from isar.synth import config, generate, noise

# Nine analyzer H values spanning a constant-Q scan's analyzer fan (avoids the 2.0 zone center,
# where make_ground_truth refuses an unmeasurable cell). ana5 (index 4) is central. Rounded to two
# decimals so emitted #V lines read cleanly (2.05, 2.1, ...), matching the real file's short format.
_DEFAULT_H_FAN = tuple(round(float(v), 2) for v in np.linspace(2.05, 2.45, 9))
_DEFAULT_QK = 0.05
_DEFAULT_QL = 0.0


@dataclass
class RunDetector:
    detector_id: str
    q: list                       # [h, k, l]
    counts: np.ndarray            # per-point signal (integer Poisson draw); == sum of miniscan passes
    miniscan_counts: np.ndarray | None = None   # raw [nP, n_mini] per-pass counts (None for supp runs)


@dataclass
class RunScan:
    scan_number: int
    scan_type: str
    command: str
    energy: np.ndarray
    central_q: list
    detectors: list[RunDetector]
    monitor: np.ndarray
    time: np.ndarray
    temperature_setpoint: float
    temperature_sensor: float
    truth: dict = field(default_factory=lambda: {"quirks": []})
    # Raw-S30 only (None for the supplementary run): per-miniscan monitor/time (shared across the
    # nine analyzers) and, for an ascan, the scanned-motor column name.
    miniscan_monitor: np.ndarray | None = None    # [nP, n_mini]
    miniscan_time: np.ndarray | None = None        # [nP, n_mini]
    scanned_motor: str | None = None               # e.g. "micro_z" for an ascan; None → fscan/HerixE
    slit: int | None = None                        # raw S30 only: written to #P17[4] when set


@dataclass
class Run:
    experiment_id: str
    scans: list[RunScan]
    header: dict = field(default_factory=dict)


def build_scan_from_truths(
    gts: list[config.GroundTruth],
    q_list: list[list[float]],
    *,
    scan_number: int,
    central_index: int = 4,
    monitor_level: float = 4.0e6,
    count_time: float = 60.0,
    temperature_setpoint: float = 290.0,
    temperature_sensor: float = 291.0,    # within the 5 K divergence_tol of the setpoint (clean)
    scan_type: str = "measurement",
    command: str = "fscan synthetic times_from_file",
) -> RunScan:
    """One physical scan: nine analyzer spectra fanned from nine GroundTruths on a shared grid."""
    energy = noise.energy_grid(gts[0].x_min, gts[0].x_max, gts[0].x_step)
    nP = len(energy)
    detectors = [
        RunDetector(detector_id=f"ana{i + 1}", q=list(q_list[i]),
                    counts=noise.draw_counts(gt).astype("float64"))
        for i, gt in enumerate(gts)
    ]
    return RunScan(
        scan_number=scan_number, scan_type=scan_type, command=command,
        energy=energy, central_q=list(q_list[central_index]), detectors=detectors,
        monitor=np.full(nP, float(monitor_level)), time=np.full(nP, float(count_time)),
        temperature_setpoint=temperature_setpoint, temperature_sensor=temperature_sensor,
    )


def build_s30_run(
    experiment_id: str,
    *,
    n_scans: int = 3,
    seed0: int = 1000,
    target_peak_counts: float = 100.0,
    cfg: config.RunConfig | None = None,
) -> Run:
    """A small multi-scan S30 run for the round-trip gate (nine analyzers per scan)."""
    cfg = cfg or config.default_run_config()
    h_fan = _DEFAULT_H_FAN
    q_list = [[float(h), _DEFAULT_QK, _DEFAULT_QL] for h in h_fan]

    scans: list[RunScan] = []
    for s in range(n_scans):
        gts = [
            generate.make_ground_truth(cfg, H=h, target_peak_counts=target_peak_counts,
                                       seed=seed0 + s * 100 + i)
            for i, h in enumerate(h_fan)
        ]
        scans.append(build_scan_from_truths(gts, q_list, scan_number=s + 1))
    return Run(experiment_id=experiment_id, scans=scans)


# ---------------------------------------------------------------------------
# Raw S30 run: per-point counts split into individual miniscan passes (§ raw ingestion)
# ---------------------------------------------------------------------------
def build_in8_run(
    experiment_id: str,
    *,
    n_scans: int = 3,
    seed0: int = 2000,
    target_peak_counts: float = 200.0,
    q_point: list | None = None,
    temperature_setpoint: float = 290.0,
    monitor_level: float = 1.0e5,
    count_time: float = 60.0,
    first_numor: int = 55000,
    cfg: config.RunConfig | None = None,
) -> Run:
    """A small IN8 run: single-`det0` constant-Q energy scans, all at one (Q, T) so the merge has
    repeats to combine. Each scan is a fresh Poisson draw of the same lineshape on the same grid.
    """
    cfg = cfg or config.default_run_config()
    q = list(q_point) if q_point is not None else [2.15, 0.05, 0.0]   # avoid the 2.0 zone centre
    scans: list[RunScan] = []
    for s in range(n_scans):
        gt = generate.make_ground_truth(cfg, H=q[0], target_peak_counts=target_peak_counts,
                                        seed=seed0 + s * 100)
        energy = noise.energy_grid(gt.x_min, gt.x_max, gt.x_step)
        nP = len(energy)
        det = RunDetector(detector_id="det0", q=list(q),
                          counts=noise.draw_counts(gt).astype("float64"))
        step = float(gt.x_step)
        command = (f"bs qh {q[0]} {q[1]} {q[2]} {energy[0]:.4g} "
                   f"dqh 0 0 0 {step:g} np {nP} mn {int(monitor_level)}")
        scans.append(RunScan(
            scan_number=first_numor + s, scan_type="measurement", command=command,
            energy=energy, central_q=list(q), detectors=[det],
            monitor=np.full(nP, float(monitor_level)), time=np.full(nP, float(count_time)),
            temperature_setpoint=temperature_setpoint, temperature_sensor=temperature_setpoint + 1.0))
    return Run(experiment_id=experiment_id, scans=scans)


def _split_into_miniscans(per_point_counts: np.ndarray, n_mini: int, rng) -> np.ndarray:
    """Distribute each point's integer counts across `n_mini` passes (Poisson thinning).

    `multinomial(C, uniform)` splits C counts into n_mini bins whose sum is exactly C and whose
    marginals are Poisson(λ/n_mini) — i.e. a faithful per-pass acquisition whose sum reproduces the
    Phase-1 per-point draw. The summed truth is therefore preserved exactly.
    """
    nP = len(per_point_counts)
    out = np.zeros((nP, n_mini), dtype="float64")
    p = [1.0 / n_mini] * n_mini
    for i, c in enumerate(per_point_counts):
        out[i] = rng.multinomial(int(round(float(c))), p)
    return out


def build_s30_raw_run(
    experiment_id: str,
    *,
    n_scans: int = 1,
    n_mini: int = 15,
    seed0: int = 3000,
    target_peak_counts: float = 300.0,
    monitor_per_point: float = 3.0e5,
    count_time: float = 60.0,
    flux_drift: float = 0.10,
    include_ascans: bool = True,
    cfg: config.RunConfig | None = None,
) -> Run:
    """A raw S30 run whose fscans carry the full per-miniscan ensemble (the raw-ingestion analogue
    of `build_s30_run`). Each energy point's Phase-1 counts are split into `n_mini` passes; the
    monitor is split with a ~`flux_drift` per-pass random walk (the real ICO-C drift). Two short
    ascans (HerixE calibration, micro_z alignment) are appended for emit/classification coverage.
    """
    cfg = cfg or config.default_run_config()
    h_fan = _DEFAULT_H_FAN
    q_list = [[float(h), _DEFAULT_QK, _DEFAULT_QL] for h in h_fan]
    rng = np.random.default_rng(seed0)

    scans: list[RunScan] = []
    sn = 1
    for s in range(n_scans):
        gts = [generate.make_ground_truth(cfg, H=h, target_peak_counts=target_peak_counts,
                                          seed=seed0 + s * 100 + i)
               for i, h in enumerate(h_fan)]
        energy = noise.energy_grid(gts[0].x_min, gts[0].x_max, gts[0].x_step)
        nP = len(energy)
        detectors = []
        for i, gt in enumerate(gts):
            per_point = noise.draw_counts(gt).astype("float64")
            mini = _split_into_miniscans(per_point, n_mini, rng)
            detectors.append(RunDetector(detector_id=f"ana{i + 1}", q=list(q_list[i]),
                                         counts=mini.sum(axis=1), miniscan_counts=mini))
        drift = 1.0 + flux_drift * rng.standard_normal((nP, n_mini))
        mini_monitor = np.maximum((monitor_per_point / n_mini) * drift, 1.0)
        mini_time = np.full((nP, n_mini), count_time / n_mini)
        scans.append(RunScan(
            scan_number=sn, scan_type="measurement",
            command="fscan synthetic times_from_file",
            energy=energy, central_q=list(q_list[4]), detectors=detectors,
            monitor=mini_monitor.sum(axis=1), time=mini_time.sum(axis=1),
            temperature_setpoint=290.0, temperature_sensor=291.0,   # within divergence_tol (clean)
            miniscan_monitor=mini_monitor, miniscan_time=mini_time))
        sn += 1

    if include_ascans:
        scans.append(_build_raw_ascan(cfg, q_list, scan_number=sn,
                                      motor="HerixE", command="ascan  herixE -10 10  20 1",
                                      scan_type="calibration", seed=seed0 + 9001))
        scans.append(_build_raw_ascan(cfg, q_list, scan_number=sn + 1,
                                      motor="micro_z", command="ascan  micro_z -0.8 1.1  20 1",
                                      scan_type="alignment", seed=seed0 + 9002))
    return Run(experiment_id=experiment_id, scans=scans)


def _build_raw_ascan(cfg, q_list, *, scan_number, motor, command, scan_type, seed,
                     nP=20, monitor_level=3.0e5, count_time=2.0) -> RunScan:
    """A short single-pass motor scan (no miniscans) for emit/classification coverage."""
    rng = np.random.default_rng(seed)
    positions = np.linspace(-1.0, 1.0, nP)
    # a mild Gaussian alignment bump on each analyzer (peak at centre), Poisson-drawn
    bump = 40.0 * np.exp(-(positions ** 2) / 0.2)
    detectors = [RunDetector(detector_id=f"ana{i + 1}", q=list(q_list[i]),
                             counts=rng.poisson(bump).astype("float64"))
                 for i in range(9)]
    return RunScan(
        scan_number=scan_number, scan_type=scan_type, command=command,
        energy=positions, central_q=list(q_list[4]), detectors=detectors,
        monitor=np.full(nP, float(monitor_level)), time=np.full(nP, float(count_time)),
        temperature_setpoint=290.0, temperature_sensor=291.0, scanned_motor=motor)


# ---------------------------------------------------------------------------
# S30 calibration set: measurement run + companion raw SPEC of elastic scans + gold table
# ---------------------------------------------------------------------------
# slit -> (gauss FWHM, lorentz FWHM) injected on that slit's elastic lines. Inside `fit_elastic`'s
# bounds (gauss 0.5..2.5, lorentz 0..1), and different enough that the by-slit path is visible.
_CAL_SLIT_RESOLUTION = {95: (1.10, 0.30), 60: (0.80, 0.25)}


@dataclass
class InjectedElastic:
    scan_number: int
    slit: int
    zero: float          # the injected elastic-line centre (meV)
    gauss_fwhm: float
    lorentz_fwhm: float


@dataclass
class S30CalibrationSet:
    measurement_path: Path           # supplementary-format run (parsed by spec_s30)
    companion_path: Path             # raw-format SPEC: herixE elastic ascans + the measurement fscans
    gold_path: Path                  # CSV for isar.analyze.gold.load_gold
    elastic: list[InjectedElastic]   # what was injected, one per elastic scan
    measurement_scans: list[int]     # scan numbers in the measurement file
    held_scans: list[int]            # measurement scans outside the elastic span (no zero by design)
    measurement_slits: dict          # measurement scan number -> slit written to the companion
    n_gold_matched: int              # gold rows whose scan is in the measurement file


def _cal_elastic_scan(cfg, scan_number, slit, zero, rng, *, area=3000.0, bg=2.0) -> RunScan:
    """One herixE ascan across the elastic line at `zero`; slit goes to #P17. Gauss/Lorentz widths
    follow the slit. All nine analyzers carry an independent Poisson draw of the same line."""
    gfw, lfw = _CAL_SLIT_RESOLUTION[slit]
    x = np.round(np.arange(-3.0, 3.0 + 1e-9, 0.1), 6)
    mean = Voigt(x, area, gfw, lfw, zero) + bg
    q = [2.0, 0.0, 0.0]
    nP = len(x)
    return RunScan(
        scan_number=scan_number, scan_type="calibration", command=f"ascan  herixE -3 3  {nP - 1} 1",
        energy=x, central_q=list(q),
        detectors=[RunDetector(detector_id=f"ana{i + 1}", q=list(q),
                               counts=rng.poisson(mean).astype("float64")) for i in range(9)],
        monitor=np.full(nP, 3.0e5), time=np.full(nP, 2.0),
        temperature_setpoint=290.0, temperature_sensor=291.0, slit=slit)


def build_s30_calibration_set(
    out_dir,
    *,
    seed: int = 4000,
    elastic_scans: tuple = (10, 20, 30, 40, 50, 60),
    elastic_slits: tuple = (95, 95, 60, 60, 95, 95),
    measurement_scans: tuple = (2, 15, 25, 35, 45, 55, 70),
    measurement_slits: tuple = (95, 95, 95, 60, 60, 95, 95),
    skip_gold: tuple = (45,),
    cfg: config.RunConfig | None = None,
) -> S30CalibrationSet:
    """Write an S30 calibration set into `out_dir` (made if missing); fully determined by `seed`.

    Elastic scans span scan numbers 10..60, so measurement scans 2 and 70 fall outside the span and
    must be held (no zero); the others interpolate. The injected zeros drift by scan. The companion
    also carries a stub fscan block per measurement scan, because that is where
    `calibrations._slit_map_from_spec` reads each measurement scan's slit. `skip_gold` measurement
    scans get no gold row.
    """
    from isar.parsers import spec_s30, spec_s30_raw

    cfg = cfg or config.default_run_config()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)

    zeros = rng.uniform(-0.4, 0.4, len(elastic_scans))
    elastic = [InjectedElastic(sn, sl, round(float(z), 4), *_CAL_SLIT_RESOLUTION[sl])
               for sn, sl, z in zip(elastic_scans, elastic_slits, zeros)]
    companion_scans = [_cal_elastic_scan(cfg, e.scan_number, e.slit, e.zero, rng) for e in elastic]

    h_fan = _DEFAULT_H_FAN
    q_list = [[float(h), _DEFAULT_QK, _DEFAULT_QL] for h in h_fan]
    meas: list[RunScan] = []
    for k, (sn, slit) in enumerate(zip(measurement_scans, measurement_slits)):
        gts = [generate.make_ground_truth(cfg, H=h, target_peak_counts=100.0,
                                          seed=seed + 100 * (k + 1) + i)
               for i, h in enumerate(h_fan)]
        sc = build_scan_from_truths(gts, q_list, scan_number=sn)
        meas.append(sc)
        nP = len(sc.energy)
        stub = RunScan(    # same scan as an n_mini=1 raw fscan; only its #P17 slit matters
            scan_number=sn, scan_type="measurement", command=sc.command, energy=sc.energy,
            central_q=sc.central_q,
            detectors=[RunDetector(d.detector_id, d.q, d.counts, d.counts[:, None])
                       for d in sc.detectors],
            monitor=sc.monitor, time=sc.time, temperature_setpoint=sc.temperature_setpoint,
            temperature_sensor=sc.temperature_sensor,
            miniscan_monitor=np.ones((nP, 1)), miniscan_time=np.ones((nP, 1)), slit=slit)
        companion_scans.append(stub)
    companion_scans.sort(key=lambda s: s.scan_number)

    exp = "SYNCAL"
    measurement_path = out_dir / f"{exp}.spec_supplementary.dat"
    companion_path = out_dir / f"{exp}.spec"
    gold_path = out_dir / f"{exp}_fit_results.csv"
    measurement_path.write_bytes(
        spec_s30.emit_file(Run(experiment_id=exp, scans=meas), spec_s30.card_template()))
    companion_path.write_bytes(
        spec_s30_raw.emit_file(Run(experiment_id=exp, scans=companion_scans),
                               spec_s30_raw.card_template()))

    slit_of = dict(zip(measurement_scans, measurement_slits))
    lo, hi = min(elastic_scans), max(elastic_scans)
    gold_scans = [sn for sn in measurement_scans if sn not in skip_gold]
    lines = ["Scan,Sample,T(K),K(rlu),phonon_energy,phonon_energy_err,damping,damping_err,"
             "center,Chi2,Quality,Slit(mm),Analyzer,Label"]
    for sn in gold_scans:
        lines.append(f"{sn},1,290,{_DEFAULT_QK},{rng.uniform(4, 8):.3f},0.1,0.5,0.05,0,1.0,OK,"
                     f"{slit_of[sn]},ana7,s{sn}")
    gold_path.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8"))

    return S30CalibrationSet(
        measurement_path=measurement_path, companion_path=companion_path, gold_path=gold_path,
        elastic=elastic, measurement_scans=list(measurement_scans),
        held_scans=[sn for sn in measurement_scans if not lo <= sn <= hi],
        measurement_slits=slit_of, n_gold_matched=len(gold_scans))

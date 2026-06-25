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

import numpy as np

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

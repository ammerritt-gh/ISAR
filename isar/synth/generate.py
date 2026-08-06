"""Synthetic-scan generation from the production model + ground-truth manifests.

`make_ground_truth` chooses absolute amplitudes so that, in count units:
  - the Stokes phonon maximum (above background) = ``target_peak_counts``,
  - the elastic peak height = ``elastic_to_phonon_height_ratio × target``,
  - the flat background = ``bg_to_phonon_height_ratio × target``.
Because the production model is linear in those three amplitudes, this is exact.
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # headless: write figures, never open a window
import matplotlib.pyplot as plt
import numpy as np

from . import config, dispersion, noise
from isar.analyze.lineshapes import convolute_voigt_dho, voigt_area_for_height


def energy_grid(cfg: config.RunConfig) -> np.ndarray:
    """The scan energy axis for this run config."""
    return noise.energy_grid(cfg.x_min, cfg.x_max, cfg.x_step)


def damped_peak_position(omega0: float, gamma: float) -> float:
    """Ω_d = √(ω₀² − Γ²): where the Stokes peak actually sits (NaN if overdamped)."""
    disc = omega0**2 - gamma**2
    return float(np.sqrt(disc)) if disc > 0 else float("nan")


def make_ground_truth(cfg: config.RunConfig, H: float, target_peak_counts: float,
                      seed: int, *, gamma: float | None = None, T: float | None = None,
                      elastic_to_phonon_height_ratio: float | None = None,
                      bg_to_phonon_height_ratio: float | None = None) -> config.GroundTruth:
    """Build the exact-truth manifest for one synthetic scan.

    The keyword-only overrides (Γ, T, elastic ratio, BG ratio) default to the run
    config but let callers sweep the full data-profile space. Raises ValueError for
    unmeasurable (zone-center) cells where ω₀ < ε.
    """
    gamma = cfg.gamma if gamma is None else gamma
    T = cfg.T if T is None else T
    elastic_ratio = (cfg.elastic_to_phonon_height_ratio
                     if elastic_to_phonon_height_ratio is None
                     else elastic_to_phonon_height_ratio)
    bg_ratio = (cfg.bg_to_phonon_height_ratio
                if bg_to_phonon_height_ratio is None else bg_to_phonon_height_ratio)

    omega0 = float(dispersion.omega0_of_H(H))
    if omega0 < cfg.eps_omega_d:
        raise ValueError(
            f"H={H} is unmeasurable (ω₀={omega0:.3g} < ε={cfg.eps_omega_d}); "
            "the phonon is buried in the elastic line."
        )

    x = energy_grid(cfg)
    # Unit phonon peak height (Stokes+anti-Stokes, amplitude=1, no elastic/BG).
    h_unit = noise.phonon_peak_height(x, omega0, gamma, 1.0,
                                      cfg.gauss_fwhm, cfg.lorentz_fwhm, T)
    counts_scale = target_peak_counts / h_unit

    phonon_amplitude = counts_scale  # amplitude=1 → height h_unit, so amp=scale → target
    elastic_amplitude = voigt_area_for_height(
        elastic_ratio * target_peak_counts, cfg.gauss_fwhm, cfg.lorentz_fwhm,
    )
    BG = bg_ratio * target_peak_counts

    return config.GroundTruth(
        H=float(H), omega0=omega0, gamma=gamma,
        phonon_amplitude=phonon_amplitude, elastic_amplitude=elastic_amplitude, BG=BG,
        gauss_fwhm=cfg.gauss_fwhm, lorentz_fwhm=cfg.lorentz_fwhm, T=T,
        x_min=cfg.x_min, x_max=cfg.x_max, x_step=cfg.x_step,
        target_peak_counts=float(target_peak_counts), seed=int(seed),
        counts_scale=counts_scale,
    )


def generate_scan(gt: config.GroundTruth):
    """Return (x, counts, gt): the energy axis, one Poisson realization, and the truth."""
    x = noise.energy_grid(gt.x_min, gt.x_max, gt.x_step)
    counts = noise.draw_counts(gt)
    return x, counts, gt


# ----------------------------------------------------------------------------
# Manifest I/O
# ----------------------------------------------------------------------------
def manifest_path(gt: config.GroundTruth) -> Path:
    fname = f"H{gt.H:.2f}_c{int(gt.target_peak_counts)}_s{gt.seed}.json"
    return config.MANIFEST_DIR / fname


def write_manifest(gt: config.GroundTruth, path: Path | None = None) -> Path:
    if path is None:
        config.ensure_out_dirs()
        path = manifest_path(gt)
    path = Path(path)
    path.write_text(json.dumps(gt.to_dict(), indent=2))
    return path


def read_manifest(path: Path) -> config.GroundTruth:
    return config.GroundTruth.from_dict(json.loads(Path(path).read_text()))


# ----------------------------------------------------------------------------
# The (2,0,0) → (3,0,0) Q-series
# ----------------------------------------------------------------------------
def generate_series(cfg: config.RunConfig, target_peak_counts: float, write: bool = True):
    """One scan per measurable H (endpoints skipped). Returns [(x, counts, gt), ...]."""
    scans = []
    for i, H in enumerate(cfg.H_values):
        if dispersion.classify_cell(H, cfg.gamma, cfg.eps_omega_d) == "unmeasurable":
            continue
        gt = make_ground_truth(cfg, H, target_peak_counts, seed=cfg.base_seed + i)
        x, counts, gt = generate_scan(gt)
        if write:
            write_manifest(gt)
        scans.append((x, counts, gt))
    return scans


# ----------------------------------------------------------------------------
# Step-1 eyeball figure
# ----------------------------------------------------------------------------
def _components(gt: config.GroundTruth, x: np.ndarray) -> dict:
    """Noise-free model pieces for plotting context, evaluated at energies x."""
    common = (gt.gauss_fwhm, gt.lorentz_fwhm, gt.gamma)
    phonon = convolute_voigt_dho(x, gt.omega0, gt.center, *common,
                                 gt.phonon_amplitude, 0.0, 0.0, gt.T, peak="stokes+astokes")
    elastic = convolute_voigt_dho(x, gt.omega0, gt.center, *common,
                                  0.0, gt.elastic_amplitude, 0.0, gt.T, peak="elastic")
    return {"phonon": phonon, "elastic": elastic, "BG": np.full_like(x, gt.BG)}


# Fine grid for smooth model curves (data stays on the coarse 0.25 meV grid).
_FINE_STEP = 0.02


def _plot_panel(ax, gt: config.GroundTruth, title: str) -> None:
    """One scan panel: Poisson draw on the data grid + smooth model on a fine grid."""
    x_data = noise.energy_grid(gt.x_min, gt.x_max, gt.x_step)
    counts = noise.draw_counts(gt)
    x_fine = noise.energy_grid(gt.x_min, gt.x_max, _FINE_STEP)

    lam = noise.model_on_grid(gt, x_fine)
    comp = _components(gt, x_fine)
    omega_d = damped_peak_position(gt.omega0, gt.gamma)

    ax.errorbar(x_data, counts, yerr=np.sqrt(counts + 1.0), fmt=".", ms=4,
                color="k", alpha=0.7, label="Poisson draw ±√(N+1)")
    ax.plot(x_fine, lam, "-", color="C0", lw=1.6, label="noise-free λ")
    ax.plot(x_fine, comp["phonon"] + comp["BG"], "--", color="C1", lw=1.0, label="phonon+BG")
    ax.plot(x_fine, comp["elastic"] + comp["BG"], ":", color="C2", lw=1.0, label="elastic+BG")
    for xc, style in ((0.0, "-"), (omega_d, "--"), (-omega_d, "--")):
        if np.isfinite(xc):
            ax.axvline(xc, color="gray", ls=style, lw=0.6, alpha=0.6)
    ax.set_title(title, fontsize=9)
    ax.set_xlabel("energy (meV)")
    ax.set_ylabel("counts")
    ax.legend(fontsize=6.5, loc="upper right")


def _grid_figure(panels, suptitle: str, out_path: Path) -> Path:
    """Lay out a list of (gt, title) panels in a 2-column grid and save."""
    n = len(panels)
    ncol = 2
    nrow = int(np.ceil(n / ncol))
    fig, axes = plt.subplots(nrow, ncol, figsize=(6.0 * ncol, 3.6 * nrow), squeeze=False)
    axes = axes.ravel()
    for ax, (gt, title) in zip(axes, panels):
        _plot_panel(ax, gt, title)
    for ax in axes[n:]:
        ax.set_visible(False)
    fig.suptitle(suptitle, fontsize=11)
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    return out_path


def plot_example_spectra(cfg: config.RunConfig, H_list=(2.05, 2.25, 2.5, 2.95),
                         target_peak_counts: float = 100.0, seed: int = 2024,
                         out_path: Path | None = None) -> Path:
    """Dispersion sweep: one panel per H at a fixed (good) count level."""
    if out_path is None:
        config.ensure_out_dirs()
        out_path = config.FIGURE_DIR / "step1_dispersion_sweep.png"
    out_path = Path(out_path)
    panels = []
    for H in H_list:
        gt = make_ground_truth(cfg, H, target_peak_counts, seed)
        omega_d = damped_peak_position(gt.omega0, gt.gamma)
        panels.append((gt, f"H={H:.2f}  ω₀={gt.omega0:.2f}  Ω_d={omega_d:.2f}  "
                           f"Γ={gt.gamma:.2f} meV  (peak {target_peak_counts:.0f} cts)"))
    return _grid_figure(
        panels,
        f"ISAR Phase 1 — dispersion sweep (peak {target_peak_counts:.0f} cts, "
        f"T={cfg.T:.0f} K, elastic {cfg.elastic_to_phonon_height_ratio:g}×)",
        out_path,
    )


def plot_count_sweep(cfg: config.RunConfig, H: float = 2.5,
                     targets=(15, 30, 100, 300), seed: int = 2024,
                     out_path: Path | None = None) -> Path:
    """Count sweep at fixed H: 'how low can we go' before the phonon drowns in noise."""
    if out_path is None:
        config.ensure_out_dirs()
        out_path = config.FIGURE_DIR / "step1_count_sweep.png"
    out_path = Path(out_path)
    panels = []
    for i, t in enumerate(targets):
        gt = make_ground_truth(cfg, H, float(t), seed + i)
        omega_d = damped_peak_position(gt.omega0, gt.gamma)
        panels.append((gt, f"phonon peak ≈ {t:.0f} cts  (H={H:.2f}, Ω_d={omega_d:.2f} meV)"))
    return _grid_figure(
        panels,
        f"ISAR Phase 1 — count sweep at H={H:.2f} "
        f"(T={cfg.T:.0f} K, elastic {cfg.elastic_to_phonon_height_ratio:g}×, "
        f"BG {cfg.bg_to_phonon_height_ratio:g}×)",
        out_path,
    )


def plot_profile_variety(cfg: config.RunConfig, H: float = 2.5,
                         target_peak_counts: float = 100.0, seed: int = 2024,
                         out_path: Path | None = None) -> Path:
    """Show the tunable profile space: elastic-ratio range + an INS-like background."""
    if out_path is None:
        config.ensure_out_dirs()
        out_path = config.FIGURE_DIR / "step1_profile_variety.png"
    out_path = Path(out_path)
    # (label, elastic_ratio, bg_ratio)
    profiles = [
        ("weak elastic 0.5×, IXS BG", 0.5, 0.02),
        ("default elastic 3×, IXS BG", 3.0, 0.02),
        ("strong elastic 10×, IXS BG", 10.0, 0.02),
        ("elastic 3×, INS-like BG 0.3×", 3.0, 0.30),
    ]
    panels = []
    for i, (label, er, br) in enumerate(profiles):
        gt = make_ground_truth(cfg, H, target_peak_counts, seed + i,
                               elastic_to_phonon_height_ratio=er,
                               bg_to_phonon_height_ratio=br)
        panels.append((gt, f"{label}  (peak {target_peak_counts:.0f} cts, H={H:.2f})"))
    return _grid_figure(
        panels,
        f"ISAR Phase 1 — profile variety (tunable elastic & background), T={cfg.T:.0f} K",
        out_path,
    )


# ----------------------------------------------------------------------------
# Step-1 entry point: emit the Q-series + eyeball figures, then PAUSE.
# ----------------------------------------------------------------------------
def main() -> None:
    cfg = config.default_run_config()
    config.ensure_out_dirs()

    good = 100.0  # representative "good" phonon peak counts
    eyeball_H = (2.05, 2.25, 2.50, 2.95)

    scans = generate_series(cfg, target_peak_counts=good, write=True)
    fig_disp = plot_example_spectra(cfg, H_list=eyeball_H, target_peak_counts=good,
                                    seed=cfg.base_seed)
    fig_counts = plot_count_sweep(cfg, H=2.5, targets=(15, 30, 100, 300),
                                  seed=cfg.base_seed)
    fig_profiles = plot_profile_variety(cfg, H=2.5, target_peak_counts=good,
                                        seed=cfg.base_seed)

    as_ratio = np.exp(-6.0 / (0.086173324 * cfg.T))
    print(f"[step 1] wrote {len(scans)} manifests to {config.MANIFEST_DIR}")
    print(f"[step 1] T = {cfg.T:.0f} K, Gamma = {cfg.gamma:.2f} meV; "
          f"anti-Stokes/Stokes at w0=6 meV ~ {as_ratio:.2f}")
    print(f"[step 1] elastic = {cfg.elastic_to_phonon_height_ratio:g}x phonon, "
          f"BG = {cfg.bg_to_phonon_height_ratio:g}x phonon (tunable per scan)")
    print(f"[step 1] count levels swept: {tuple(cfg.target_counts_levels)} phonon peak cts")
    print(f"[step 1] grid: {cfg.x_min:g}..{cfg.x_max:g} meV step {cfg.x_step:g} "
          f"({len(energy_grid(cfg))} channels); model curves rendered at {_FINE_STEP:g} meV")
    print()
    print("=" * 70)
    print("PAUSE - human sanity-check required before fitting (step 2).")
    print(f"  REVIEW: {fig_disp}")
    print(f"  REVIEW: {fig_counts}")
    print(f"  REVIEW: {fig_profiles}")
    print("  Check: elastic at 0; Stokes at +Omega_d, anti-Stokes at -Omega_d")
    print("  (~50% of Stokes at T=100 K); smooth curves; flat BG; Poisson noise;")
    print("  peak ~ target; and where the phonon starts drowning at low counts.")
    print("=" * 70)


if __name__ == "__main__":
    main()

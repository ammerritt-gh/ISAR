# ISAR — Inelastic Scattering Automated Researcher

> **Status:** live
> **Authority:** canonical for product overview and quickstart
> **Verified against code revision:** `c20e23d` (2026-10-29)

[![DOI](https://zenodo.org/badge/1277919462.svg)](https://doi.org/10.5281/zenodo.23771477)

ISAR helps a researcher during an inelastic scattering beamtime. It reads the scans as they come
in from inelastic X-ray (IXS) and triple-axis instruments — so far APS Sector 30, SPring-8 BL43LXU
and ILL IN8 — analyzes them, and can choose and run the next measurement itself.

> **ISAR is an early prototype and still very much in development.** Expect rough edges and
> changes between versions.

## The pipeline

1. **Parse data.** ISAR detects which instrument a file came from, reads every scan into one
   common form, and checks its quality. Questionable points are flagged and masked, never deleted.
2. **Analyze data.** Each spectrum is fitted, and every scan gets a verdict on how far to trust the
   result: use this number, glance at it, or look at it yourself.
3. **Drive data acquisition.** From a measurement goal, ISAR chooses the next scan, runs it, fits
   what comes back, and decides where to measure next and when to stop. So far this runs against
   [TAVI](https://github.com/ammerritt-gh/TAVI-Triple-Axis-Virtual-Instrument)'s simulated
   instrument only; no campaign has yet run on a real instrument.

## Installation

On Windows, download **`WINDOWS-install-ISAR-vX.Y.Z.bat`** from the
[latest release](https://github.com/ammerritt-gh/ISAR/releases/latest) and double-click it. It
asks where to install, builds a private environment from a pinned package lock inside that
folder, checks that ISAR runs, and offers a desktop shortcut to the ISAR Launcher.
`WINDOWS-install-ISAR-latest.bat` on the same page always installs the newest release, and
`WINDOWS-uninstall-ISAR.bat` removes an installation when the launcher is gone. Your workspaces
are never deleted. The [installation guide](installer/ISAR-Installation-README.md) has the details.

ISAR does not install TAVI; campaigns against the simulated instrument need TAVI's own installer.

Working from a source checkout instead: double-click `setup-isar-env.bat` to build the `isar`
environment, then `run-isar-gui.bat` to start the GUI.

## Running ISAR

Open the **ISAR Launcher** (the desktop shortcut, or `ISAR-Launcher.bat` in the install folder)
and choose Start. The same menu offers Repair, Collect diagnostics and Uninstall. ISAR also has a
command line: `isar --help` lists its commands.

## Troubleshooting

For help, please raise an issue on this GitHub repository, or contact the author through GitHub.

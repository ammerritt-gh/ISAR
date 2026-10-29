# ISAR — Inelastic Scattering Automated Researcher

> **Status:** live
> **Authority:** canonical for product overview and quickstart
> **Verified against code revision:** `c20e23d` (2026-10-29)

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

On Windows, double-click **`setup-isar-env.bat`**. It fetches micromamba if needed, builds the
`isar` environment and installs ISAR into it; run it again after updating ISAR. A one-click
installer like TAVI's is planned.

ISAR does not install TAVI; campaigns against the simulated instrument need TAVI's own installer.

## Running ISAR

Double-click **`run-isar-gui.bat`** to open the GUI, or drag a workspace folder onto it to open
that workspace. ISAR also has a command line: in the `isar` environment, `isar --help` lists its
commands.

## Troubleshooting

For help, please raise an issue on this GitHub repository, or contact the author through GitHub.

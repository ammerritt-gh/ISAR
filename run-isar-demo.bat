@echo off
rem Launch the ISAR GUI for a live demo campaign. Identical to run-isar-gui.bat except that the
rem evidence sampler's escalation rung runs at 32 live points instead of the production 128
rem (ISAR_ESCALATION_N_LIVE, see isar/analyze/evidence.py): measured 2026-09-02 on the
rem (2,0,0)->(2.3,0,0) acoustic campaign, that is ~1 min per fit instead of ~5 min, with the same
rem certified result. In the Drive dock set the analysis limit to "none" for a full-line campaign.
rem Drag a workspace folder onto this file to open it, or run:  run-isar-demo.bat C:\path\to\workspace
cd /d "%~dp0"
set "ISAR_ESCALATION_N_LIVE=32"
if defined MICROMAMBA_EXE (set "MM=%MICROMAMBA_EXE%") else (set "MM=%USERPROFILE%\AppData\Local\micromamba\micromamba.exe")
if not defined MAMBA_ROOT_PREFIX set "MAMBA_ROOT_PREFIX=%USERPROFILE%\AppData\Roaming\mamba"
if not defined ISAR_ENV (
    if exist "%MAMBA_ROOT_PREFIX%\envs\isar\python.exe" (set "ISAR_ENV=isar") else (set "ISAR_ENV=tavi-dev")
)
"%MM%" run -n %ISAR_ENV% python -m isar_gui %*
if errorlevel 1 pause

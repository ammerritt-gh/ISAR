@echo off
rem Launch the ISAR GUI (isar view). Double-click to open the most recent workspace,
rem drag a workspace folder onto this file to open that one, or run:
rem   run-isar-gui.bat C:\path\to\workspace
rem Set MICROMAMBA_EXE to override the micromamba location, or ISAR_ENV to force an environment.
cd /d "%~dp0"
if defined MICROMAMBA_EXE (set "MM=%MICROMAMBA_EXE%") else (set "MM=%USERPROFILE%\AppData\Local\micromamba\micromamba.exe")

rem Prefer ISAR's own environment (created by setup-isar-env.bat); fall back to tavi-dev (the dev
rem environment) when 'isar' hasn't been built yet, so the developer's launch path is unchanged.
if not defined MAMBA_ROOT_PREFIX set "MAMBA_ROOT_PREFIX=%USERPROFILE%\AppData\Roaming\mamba"
if not defined ISAR_ENV (
    if exist "%MAMBA_ROOT_PREFIX%\envs\isar\python.exe" (set "ISAR_ENV=isar") else (set "ISAR_ENV=tavi-dev")
)

"%MM%" run -n %ISAR_ENV% python -m isar_gui %*
if errorlevel 1 pause

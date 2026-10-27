@echo off
rem Launch the ISAR GUI (isar view) with no console window. Double-click to open the most recent
rem workspace, drag a workspace folder onto this file to open that one, or run:
rem   run-isar-gui.bat C:\path\to\workspace
rem Set ISAR_ENV to force an environment. A start-up failure is written to the GUI log and shown
rem in a message box.
cd /d "%~dp0"

rem Prefer ISAR's own environment (created by setup-isar-env.bat); fall back to tavi-dev (the dev
rem environment) when 'isar' hasn't been built yet, so the developer's launch path is unchanged.
if not defined MAMBA_ROOT_PREFIX set "MAMBA_ROOT_PREFIX=%USERPROFILE%\AppData\Roaming\mamba"
if not defined ISAR_ENV (
    if exist "%MAMBA_ROOT_PREFIX%\envs\isar\python.exe" (set "ISAR_ENV=isar") else (set "ISAR_ENV=tavi-dev")
)
set "PREFIX=%MAMBA_ROOT_PREFIX%\envs\%ISAR_ENV%"

if not exist "%PREFIX%\pythonw.exe" (
    echo [ERROR] No Python environment for ISAR at "%PREFIX%".
    echo         Run setup-isar-env.bat first, or set ISAR_ENV to an existing environment.
    pause
    exit /b 1
)

rem The environment's activation directories, as micromamba run put them on PATH for the GUI and
rem its engine processes. pythonw is started directly: micromamba run would open a console.
set "PATH=%PREFIX%;%PREFIX%\Library\mingw-w64\bin;%PREFIX%\Library\usr\bin;%PREFIX%\Library\bin;%PREFIX%\Scripts;%PREFIX%\bin;%PATH%"
set "CONDA_PREFIX=%PREFIX%"

start "" "%PREFIX%\pythonw.exe" -m isar_gui %*
if errorlevel 1 (
    echo [ERROR] Could not start the ISAR GUI with "%PREFIX%\pythonw.exe".
    pause
    exit /b 1
)

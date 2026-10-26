@echo off
rem Run ISAR's proofs: the checks that read the real facility data in real_data\ (main checkout only).
rem Proofs are not tests - they never run in the routine suite. Double-click to run them all;
rem a missing real file is an ERROR naming it, never a skip. Results show on screen only.
rem
rem   run-isar-proofs.bat          every proof except the sealed golden scenarios
rem   run-isar-proofs.bat golden   ONLY the sealed golden scenarios (sets ISAR_GOLDEN=1);
rem                                run it only when you have named a golden gate run
rem   run-isar-proofs.bat letacon   ONLY the le Tacon fast-pass proof (sets ISAR_LETACON=1): two
rem                                28-spectrum batches plus the prior sensitivity check, one to two
rem                                hours; results land in out\letacon_fast_pass\<timestamp>\
rem
rem Set MICROMAMBA_EXE to override the micromamba location, or ISAR_ENV to force an environment.
rem The environment needs pytest (pip install -e ".[test]").
setlocal
cd /d "%~dp0"
if defined MICROMAMBA_EXE (set "MM=%MICROMAMBA_EXE%") else (set "MM=%USERPROFILE%\AppData\Local\micromamba\micromamba.exe")

rem Same environment choice as run-isar-gui.bat: 'isar', else the dev environment 'tavi-dev'.
if not defined MAMBA_ROOT_PREFIX set "MAMBA_ROOT_PREFIX=%USERPROFILE%\AppData\Roaming\mamba"
if not defined ISAR_ENV (
    if exist "%MAMBA_ROOT_PREFIX%\envs\isar\python.exe" (set "ISAR_ENV=isar") else (set "ISAR_ENV=tavi-dev")
)

rem Fail with the fix named, not with "No module named pytest" in a window that closes.
"%MM%" run -n %ISAR_ENV% python -c "import pytest" >nul 2>&1
if errorlevel 1 goto nopytest

if /i "%~1"=="golden" goto golden
if /i "%~1"=="letacon" goto letacon

echo Running the proofs against real_data\ ...
rem Clear ISAR_GOLDEN so a switch inherited from an earlier gate cannot run the sealed scenarios.
set "ISAR_GOLDEN="
set "ISAR_LETACON="
"%MM%" run -n %ISAR_ENV% python -m pytest proofs -q
goto done

:golden
echo Running the sealed golden scenarios (ISAR_GOLDEN=1) ...
set "ISAR_GOLDEN=1"
"%MM%" run -n %ISAR_ENV% python -m pytest proofs\test_golden_scenarios.py -m golden -q
goto done

:letacon
echo Running the le Tacon fast-pass proof (ISAR_LETACON=1): one to two hours, results in out\letacon_fast_pass ...
set "ISAR_GOLDEN="
set "ISAR_LETACON=1"
"%MM%" run -n %ISAR_ENV% python -m pytest proofs\test_letacon_fast_pass.py -q -s
goto done

:done
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (echo RESULT: all selected proofs passed.) else (echo RESULT: FAILED or errored - see the output above. Exit code %RC%.)
pause
exit /b %RC%

:nopytest
echo.
echo ISAR's proofs need pytest, and environment "%ISAR_ENV%" does not have it.
echo To add it, run this from the repository folder, then double-click this file again:
echo     "%MM%" run -n %ISAR_ENV% python -m pip install -e ".[test]"
pause
exit /b 1

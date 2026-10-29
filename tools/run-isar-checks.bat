@echo off
rem Run ISAR's slow checks: the whole test suite INCLUDING the tests marked slow (campaigns,
rem closed-loop worlds, end-to-end deliveries). They are not part of the routine gate; run them on
rem purpose - before a release, when a change touches drive, scene or session delivery, or when
rem asked. A failure here is a finding to file, not a blocker of the change that ran it.
rem
rem   tools\run-isar-checks.bat          the full suite with the slow checks, 16 workers (~6 min);
rem                                stopped after 30 minutes
rem   tools\run-isar-checks.bat serial   the same, one process (~25 min): only after test-infrastructure
rem                                changes or when asked; stopped after 60 minutes
rem
rem Set MICROMAMBA_EXE to override the micromamba location, or ISAR_ENV to force an environment.
rem The environment needs pytest, pytest-xdist and pytest-timeout (pip install -e ".[test,test-gui,gui]").
setlocal
cd /d "%~dp0.."
if defined MICROMAMBA_EXE (set "MM=%MICROMAMBA_EXE%") else (set "MM=%USERPROFILE%\AppData\Local\micromamba\micromamba.exe")

rem Same environment choice as run-isar-gui.bat: 'isar', else the dev environment 'tavi-dev'.
if not defined MAMBA_ROOT_PREFIX set "MAMBA_ROOT_PREFIX=%USERPROFILE%\AppData\Roaming\mamba"
if not defined ISAR_ENV (
    if exist "%MAMBA_ROOT_PREFIX%\envs\isar\python.exe" (set "ISAR_ENV=isar") else (set "ISAR_ENV=tavi-dev")
)

rem Fail with the fix named, not with "No module named pytest" in a window that closes.
"%MM%" run -n %ISAR_ENV% python -c "import pytest, xdist, pytest_timeout" >nul 2>&1
if errorlevel 1 goto nopytest

echo Started %DATE% %TIME%
if /i "%~1"=="serial" goto serial

echo Running the full suite with the slow checks, 16 workers ...
"%MM%" run -n %ISAR_ENV% python -m pytest --run-slow -n 16 --dist worksteal --session-timeout 1800 -q
goto done

:serial
echo Running the full suite with the slow checks, serially ...
"%MM%" run -n %ISAR_ENV% python -m pytest --run-slow --session-timeout 3600 -q
goto done

:done
set "RC=%ERRORLEVEL%"
echo.
echo Finished %DATE% %TIME%
if "%RC%"=="0" (echo RESULT: all checks passed.) else (echo RESULT: FAILED or stopped - see the output above. Exit code %RC%.)
pause
exit /b %RC%

:nopytest
echo.
echo ISAR's checks need pytest, pytest-xdist and pytest-timeout, and environment "%ISAR_ENV%" does not have them.
echo To add them, run this from the repository folder, then double-click this file again:
echo     "%MM%" run -n %ISAR_ENV% python -m pip install -e ".[test,test-gui,gui]"
pause
exit /b 1

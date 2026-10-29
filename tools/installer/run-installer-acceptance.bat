@echo off
setlocal DisableDelayedExpansion
rem Installer acceptance: runs the one-click Windows installer, from a pushed branch, through
rem every acceptance case in a scrubbed environment with temporary profile folders.
rem
rem   tools\installer\run-installer-acceptance.bat <branch> [case,case,...|all]
rem
rem This takes tens of minutes and downloads the full environment several times. Run it on purpose,
rem never as part of the routine tests. Push the branch first: the run refuses a stale push.
rem The report and each run's captured output and logs are kept in the folder it prints at the end.
rem Set MICROMAMBA_EXE to override the micromamba location, or ISAR_ENV to force an environment.
cd /d "%~dp0..\.."
if "%~1"=="" goto usage
set "CASES=all"
if not "%~2"=="" set "CASES=%~2"
if defined MICROMAMBA_EXE (set "MM=%MICROMAMBA_EXE%") else (set "MM=%USERPROFILE%\AppData\Local\micromamba\micromamba.exe")
if not defined MAMBA_ROOT_PREFIX set "MAMBA_ROOT_PREFIX=%USERPROFILE%\AppData\Roaming\mamba"
if not defined ISAR_ENV (
    if exist "%MAMBA_ROOT_PREFIX%\envs\isar\python.exe" (set "ISAR_ENV=isar") else (set "ISAR_ENV=tavi-dev")
)

echo Installer acceptance for branch "%~1", cases: %CASES%
echo This takes tens of minutes and downloads the full environment several times.
echo Started %DATE% %TIME%
echo.
"%MM%" run -n %ISAR_ENV% python tools\installer\installer_acceptance.py --ref "%~1" --cases "%CASES%"
set "RC=%ERRORLEVEL%"
echo.
echo Finished %DATE% %TIME%
if "%RC%"=="0" (echo RESULT: every case passed or was skipped.) else (echo RESULT: FAILED - see the report named above. Exit code %RC%.)
pause
exit /b %RC%

:usage
echo Usage: run-installer-acceptance.bat ^<pushed branch^> [case,case,...^|all]
echo The branch must be pushed and checked out here. It takes tens of minutes.
pause
exit /b 2

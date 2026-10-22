@echo off
rem Run ISAR's peak-count benchmark (isar.synth.count_bench): synthetic spectra with 0..6 modes, fitted
rem through the production fit path, scored for correct counts, confident extra modes and the A/B test.
rem The figures are TARGETS, reported with their denominators; none is a pass/fail gate.
rem
rem The full characterisation is SLOW (hours: a nested-sampling reference on 200 draws per case).
rem It runs only when you start it. Shrink it with arguments, for example a quick look:
rem   run-isar-count-bench.bat --cases 4 --draws 3 --null-draws 3 --ab-draws 200
rem or the capped tuning run (60 cases, 30 minutes):
rem   run-isar-count-bench.bat --mode tuning
rem All options:  run-isar-count-bench.bat --help
rem
rem Results go to out\count_bench\<timestamp>\ (figures.json, cases.csv, count_bench.log); the folder
rem and the wall-clock time are printed at the end. Set ISAR_COUNT_BENCH_MAX_SECONDS to change the
rem time limit (default 7200 s): no new case starts after it, and workers still running 5 minutes later
rem are killed and the finished cases are reported. Set MICROMAMBA_EXE to override the micromamba
rem location, or ISAR_ENV to force an environment.
setlocal
cd /d "%~dp0"
if defined MICROMAMBA_EXE (set "MM=%MICROMAMBA_EXE%") else (set "MM=%USERPROFILE%\AppData\Local\micromamba\micromamba.exe")

rem Same environment choice as run-isar-gui.bat: 'isar', else the dev environment 'tavi-dev'.
if not defined MAMBA_ROOT_PREFIX set "MAMBA_ROOT_PREFIX=%USERPROFILE%\AppData\Roaming\mamba"
if not defined ISAR_ENV (
    if exist "%MAMBA_ROOT_PREFIX%\envs\isar\python.exe" (set "ISAR_ENV=isar") else (set "ISAR_ENV=tavi-dev")
)
if not defined ISAR_COUNT_BENCH_MAX_SECONDS set "ISAR_COUNT_BENCH_MAX_SECONDS=7200"

if not exist "%MM%" goto nomamba

echo Peak-count benchmark. Time limit %ISAR_COUNT_BENCH_MAX_SECONDS% s. Started %DATE% %TIME%.
"%MM%" run -n %ISAR_ENV% python -m isar.synth.count_bench --max-seconds %ISAR_COUNT_BENCH_MAX_SECONDS% %*
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (echo RESULT: finished. The output folder and wall clock are printed above.) else (echo RESULT: FAILED, exit code %RC%. See the messages above and count_bench.log in the output folder.)
echo Finished %DATE% %TIME%.
pause
exit /b %RC%

:nomamba
echo.
echo micromamba was not found at "%MM%".
echo Run setup-isar-env.bat first, or set MICROMAMBA_EXE to its location, then try again.
pause
exit /b 1

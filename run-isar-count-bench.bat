@echo off
rem Run ISAR's fast-pass peak benchmark (isar.synth.count_bench): synthetic spectra with 0..6 modes on the
rem committed BL43LXU resolution curves, fitted through the production fast pass and scored for the peaks
rem a human would call clear: missed and false peaks, the count states and the within-spectrum flags.
rem The figures are TARGETS, reported with their denominators; none is a pass/fail gate.
rem
rem --mode development (the tuning streams) is for setting thresholds. --mode evaluation (the default)
rem reports the figures on streams development never saw. Both are deliberate and slow: each spectrum is
rem a full fast-pass fit. Estimates at 30 s a fit on 12 workers: development about 12 min, evaluation
rem about 23 min. Shrink either with arguments, for example a quick look:
rem   run-isar-count-bench.bat --mode development --named-draws 1 --random 4 --clean 2
rem All options:  run-isar-count-bench.bat --help
rem
rem Results go to out\count_bench\<timestamp>\: figures.json, cases.csv, true_peaks.csv, fitted_peaks.csv,
rem records.jsonl, count_bench.log and gallery\page_NN.png (truth-versus-fit pages). The folder and the
rem wall-clock time are printed at the end. ISAR_COUNT_BENCH_MAX_SECONDS sets the wall-clock cap (default
rem 5400 s): no new spectrum starts in the last 300 s before it, and fits still running at the cap are
rem killed and the finished spectra are reported. Set MICROMAMBA_EXE to override the micromamba location,
rem or ISAR_ENV to force an environment.
setlocal
cd /d "%~dp0"
if defined MICROMAMBA_EXE (set "MM=%MICROMAMBA_EXE%") else (set "MM=%USERPROFILE%\AppData\Local\micromamba\micromamba.exe")

rem Same environment choice as run-isar-gui.bat: 'isar', else the dev environment 'tavi-dev'.
if not defined MAMBA_ROOT_PREFIX set "MAMBA_ROOT_PREFIX=%USERPROFILE%\AppData\Roaming\mamba"
if not defined ISAR_ENV (
    if exist "%MAMBA_ROOT_PREFIX%\envs\isar\python.exe" (set "ISAR_ENV=isar") else (set "ISAR_ENV=tavi-dev")
)
if not defined ISAR_COUNT_BENCH_MAX_SECONDS set "ISAR_COUNT_BENCH_MAX_SECONDS=5400"

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

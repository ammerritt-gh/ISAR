@echo off
setlocal
cd /d "%~dp0.."

set "OMP_NUM_THREADS=1"
set "OPENBLAS_NUM_THREADS=1"
set "MKL_NUM_THREADS=1"
set "NUMEXPR_NUM_THREADS=1"
set "BLIS_NUM_THREADS=1"
if "%ISAR_PHYSICAL_CORES%"=="" (
    for /f %%C in ('powershell -NoProfile -Command "(Get-CimInstance Win32_Processor | Measure-Object -Property NumberOfCores -Sum).Sum"') do set "ISAR_PHYSICAL_CORES=%%C"
)

set "ISAR_MAMBA=%USERPROFILE%\AppData\Local\micromamba\micromamba.exe"
if not exist "%ISAR_MAMBA%" (
    echo ERROR: micromamba was not found at:
    echo   %ISAR_MAMBA%
    echo Run setup-isar-env.bat first.
    pause
    exit /b 1
)

rem Durable console log, like run-stat2b-campaign.bat: a failure must survive the console buffer.
if not exist "out\stat2b-scaling" mkdir "out\stat2b-scaling"
set "STAT2B_LOG=out\stat2b-scaling\stat2b-scaling-console.log"
echo Running STAT-2b scaling preflight...
echo Durable console log: %STAT2B_LOG%
> "%STAT2B_LOG%" echo [%DATE% %TIME%] Starting STAT-2b scaling preflight.
"%ISAR_MAMBA%" run -n isar python -m isar.synth.stat2b.scaling ^
    --spec tools\stats\stat2b-development.toml ^
    --output-root out\stat2b-scaling >> "%STAT2B_LOG%" 2>&1
if errorlevel 1 (
    echo.
    echo STAT-2b scaling preflight failed. Check:
    echo   %STAT2B_LOG%
    echo Existing scaling output is never resumed; remove it explicitly or use
    echo --recreate from a command prompt when a fully fresh rerun is intended.
    pause
    exit /b 1
)

echo.
echo Scaling summary:
echo   %CD%\out\stat2b-scaling\scaling-summary.json
pause

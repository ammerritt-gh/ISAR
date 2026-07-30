@echo off
setlocal
cd /d "%~dp0"

set "MICROMAMBA=%USERPROFILE%\AppData\Local\micromamba\micromamba.exe"
if not exist "%MICROMAMBA%" (
    echo ERROR: micromamba was not found at:
    echo   %MICROMAMBA%
    echo Run setup-isar-env.bat first.
    pause
    exit /b 1
)

rem Dataset processes own parallelism. Prevent nested BLAS/OpenMP pools in every worker.
set "OMP_NUM_THREADS=1"
set "OPENBLAS_NUM_THREADS=1"
set "MKL_NUM_THREADS=1"
set "NUMEXPR_NUM_THREADS=1"
set "BLIS_NUM_THREADS=1"
if "%ISAR_PHYSICAL_CORES%"=="" (
    for /f %%C in ('powershell -NoProfile -Command "(Get-CimInstance Win32_Processor | Measure-Object -Property NumberOfCores -Sum).Sum"') do set "ISAR_PHYSICAL_CORES=%%C"
)

set "STAT2B_PHASE=%~1"
if "%STAT2B_PHASE%"=="" set "STAT2B_PHASE=development"
if /i "%STAT2B_PHASE%"=="development" (
    set "STAT2B_SPEC=tools\stats\stat2b-development.toml"
) else if /i "%STAT2B_PHASE%"=="heldout" (
    set "STAT2B_SPEC=tools\stats\stat2b-heldout.toml"
) else (
    echo ERROR: phase must be development or heldout.
    pause
    exit /b 1
)

rem STAT-2b.1 scaling selected 24 as the smallest tier within 90%% of best throughput.
if "%ISAR_WORKERS%"=="" set "ISAR_WORKERS=24"
if not exist "out\stat2b" mkdir "out\stat2b"
set "STAT2B_LOG=out\stat2b\stat2b-%STAT2B_PHASE%-console.log"
echo Running STAT-2b %STAT2B_PHASE% campaign with %ISAR_WORKERS% dataset workers...
echo Durable console log: %STAT2B_LOG%
> "%STAT2B_LOG%" echo [%DATE% %TIME%] Starting STAT-2b %STAT2B_PHASE% campaign with %ISAR_WORKERS% dataset workers.
"%MICROMAMBA%" run -n isar python -m isar.synth.stat2b ^
    --spec "%STAT2B_SPEC%" ^
    --phase "%STAT2B_PHASE%" ^
    --output-root "out\stat2b" ^
    --workers %ISAR_WORKERS% >> "%STAT2B_LOG%" 2>&1
if errorlevel 1 (
    echo.
    echo STAT-2b campaign failed. Check:
    echo   %STAT2B_LOG%
    echo Resumable checkpoints remain under out\stat2b.
    pause
    exit /b 1
)

echo.
echo Results are under out\stat2b.
pause

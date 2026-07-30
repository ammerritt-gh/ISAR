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

if "%ISAR_WORKERS%"=="" set "ISAR_WORKERS=4"
echo Running STAT-2b %STAT2B_PHASE% campaign with %ISAR_WORKERS% dataset workers...
"%MICROMAMBA%" run -n isar python -m isar.synth.stat2b ^
    --spec "%STAT2B_SPEC%" ^
    --phase "%STAT2B_PHASE%" ^
    --output-root "out\stat2b" ^
    --workers %ISAR_WORKERS%
if errorlevel 1 (
    echo.
    echo STAT-2b campaign failed. Check the message above and the resumable checkpoints under out\stat2b.
    pause
    exit /b 1
)

echo.
echo Results are under out\stat2b.
pause

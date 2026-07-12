@echo off
setlocal EnableExtensions DisableDelayedExpansion
rem ============================================================================
rem  ISAR environment setup (D1) — reach "running the viewer/analysis half" on a
rem  clean Windows machine with zero technical steps. Double-click this file.
rem
rem  It installs (or reuses) micromamba, creates the 'isar' environment from
rem  environment-isar.yml, and pip-installs ISAR editable with the [gui] extra.
rem  It does NOT install TAVI or McStas — campaigns / `isar drive` need TAVI's
rem  own installer. After this finishes, launch with run-isar-gui.bat.
rem
rem  Modelled on TAVI's installer bootstrap (installer\WINDOWS-install-TAVI*.bat),
rem  trimmed to ISAR's pure-Python needs (no compiler / MPI / McStas).
rem ============================================================================

set "MAMBA_VERSION=2.5.0-1"
set "ENV_NAME=isar"
set "MICROMAMBA_DIR=%USERPROFILE%\AppData\Local\micromamba"
if not defined MICROMAMBA_EXE set "MICROMAMBA_EXE=%MICROMAMBA_DIR%\micromamba.exe"

rem Run from the repo root (this script's own folder) so `pip install -e .` sees pyproject.toml.
cd /d "%~dp0"

echo ============================================================================
echo   ISAR environment setup
echo ============================================================================
echo   Environment name : %ENV_NAME%
echo   Micromamba       : %MICROMAMBA_EXE%
echo   Repo             : %CD%
echo.

echo [1/3] Ensuring micromamba is available...
if not exist "%MICROMAMBA_DIR%" mkdir "%MICROMAMBA_DIR%"
if not exist "%MICROMAMBA_EXE%" (
    echo   Downloading micromamba %MAMBA_VERSION% ...
    curl -L -o "%MICROMAMBA_EXE%.tmp" "https://github.com/mamba-org/micromamba-releases/releases/download/%MAMBA_VERSION%/micromamba-win-64"
    if errorlevel 1 (
        echo   [ERROR] Could not download micromamba. Check your internet connection.
        pause
        exit /b 1
    )
    move /Y "%MICROMAMBA_EXE%.tmp" "%MICROMAMBA_EXE%" >nul
)
if not exist "%MICROMAMBA_EXE%" (
    echo   [ERROR] micromamba.exe not found at %MICROMAMBA_EXE%
    pause
    exit /b 1
)
"%MICROMAMBA_EXE%" --version
echo   [OK] micromamba ready.
echo.

echo [2/3] Creating or updating the '%ENV_NAME%' environment...
"%MICROMAMBA_EXE%" env list > "%TEMP%\isar_envs.txt" 2>nul
findstr /i /c:"\envs\%ENV_NAME%" "%TEMP%\isar_envs.txt" >nul 2>nul
if "%ERRORLEVEL%"=="0" (
    echo   Environment '%ENV_NAME%' already exists — reusing it (updating the ISAR install below).
) else (
    "%MICROMAMBA_EXE%" create -y -n %ENV_NAME% -f environment-isar.yml
    if errorlevel 1 (
        echo   [ERROR] Failed to create the '%ENV_NAME%' environment.
        pause
        exit /b 1
    )
)
del "%TEMP%\isar_envs.txt" >nul 2>nul
echo   [OK] environment present.
echo.

echo [3/3] Installing ISAR (editable, with the GUI extra)...
"%MICROMAMBA_EXE%" run -n %ENV_NAME% python -m pip install -e ".[gui]"
if errorlevel 1 (
    echo   [ERROR] pip install failed. See the messages above.
    pause
    exit /b 1
)
echo.
echo ============================================================================
echo   Done. ISAR is installed in the '%ENV_NAME%' environment.
echo   Launch the GUI by double-clicking:  run-isar-gui.bat
echo ============================================================================
pause
exit /b 0

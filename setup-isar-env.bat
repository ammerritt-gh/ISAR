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
rem SHA-256 of the micromamba-win-64 release asset, pinned so a compromised release,
rem redirect, or cache cannot hand the operator an arbitrary binary. Provenance:
rem conda-forge's trusted metadata (api.anaconda.org) publishes sha256
rem 01a89be3d83786b1258ff388f70e641b036852f7cac0a0f6fd051889f21962ea for
rem win-64/micromamba-2.5.0-1.tar.bz2; that package's Library\bin\micromamba.exe hashes
rem to the value below, and the GitHub-mirrored bare asset matches it byte-for-byte.
rem When bumping MAMBA_VERSION, re-derive via the same chain (conda-forge package hash
rem from api.anaconda.org -> extract -> hash the exe) before updating this pin.
set "MAMBA_SHA256=56e3a55be1d8858f51ec9902bbc0825d7a18dc43c8558cd8d8b4e1f3d9af7bb4"
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
rem An already-installed micromamba.exe is trusted as-is (documented residual risk);
rem the checksum below guards only binaries this script downloads itself.
if not exist "%MICROMAMBA_EXE%" (
    call :download_micromamba
    if errorlevel 1 (
        pause
        exit /b 1
    )
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
    echo   Environment '%ENV_NAME%' already exists — reusing it ^(updating the ISAR install below^).
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

rem ----------------------------------------------------------------------------
rem Download micromamba over HTTPS only and refuse to install it unless its
rem SHA-256 matches the pinned digest. Every failure path removes the partial
rem download. Returns errorlevel 0 on success, 1 on any failure.
rem ----------------------------------------------------------------------------
:download_micromamba
echo   Downloading micromamba %MAMBA_VERSION% ...
curl --fail --location --proto =https --proto-redir =https -o "%MICROMAMBA_EXE%.tmp" "https://github.com/mamba-org/micromamba-releases/releases/download/%MAMBA_VERSION%/micromamba-win-64"
if errorlevel 1 (
    echo   [ERROR] Could not download micromamba. Check your internet connection.
    if exist "%MICROMAMBA_EXE%.tmp" del "%MICROMAMBA_EXE%.tmp"
    exit /b 1
)
echo   Verifying SHA-256 checksum ...
certutil -hashfile "%MICROMAMBA_EXE%.tmp" SHA256 > "%TEMP%\isar_mamba_hash.txt"
rem certutil reports failure with a NEGATIVE exit code (e.g. 0x80070002), which
rem `if errorlevel 1` (a >=1 test) would miss; this line-level equality check is
rem expanded at execution time, so it is not the stale-%ERRORLEVEL%-in-parens trap.
if not "%ERRORLEVEL%"=="0" (
    echo   [ERROR] certutil failed; cannot verify the downloaded micromamba binary.
    del "%MICROMAMBA_EXE%.tmp"
    del "%TEMP%\isar_mamba_hash.txt" >nul 2>nul
    exit /b 1
)
findstr /i /x /c:"%MAMBA_SHA256%" "%TEMP%\isar_mamba_hash.txt" >nul
if errorlevel 1 (
    echo   [ERROR] micromamba checksum mismatch - refusing to run the downloaded binary.
    echo           Expected %MAMBA_SHA256%
    del "%MICROMAMBA_EXE%.tmp"
    del "%TEMP%\isar_mamba_hash.txt" >nul 2>nul
    exit /b 1
)
del "%TEMP%\isar_mamba_hash.txt" >nul 2>nul
move /Y "%MICROMAMBA_EXE%.tmp" "%MICROMAMBA_EXE%" >nul
echo   [OK] checksum verified.
exit /b 0

@echo off
setlocal DisableDelayedExpansion

:: Starts ISAR from this installation: <base>\launcher\run-isar.bat.
:: Every path comes from this file's location. Arguments go to the GUI (a
:: workspace folder, for instance); the launcher menu passes none.
:: The start is written to a launch log, %LOCALAPPDATA%\ISAR\logs\launch-*.log
:: (%TEMP%\ISAR-logs when that cannot be written); the ten newest are kept.
:: The GUI runs without a console (pythonw), so its own messages go to its
:: log in %APPDATA%\isar_gui. Rules as in the installer: no pipes, no
:: command-form "for /f", redirections first on echo lines.

for %%I in ("%~dp0..") do set "BASE=%%~fI"
set "ENVP=%BASE%\env"
set "SYS_PATH=%SystemRoot%\System32;%SystemRoot%;%SystemRoot%\System32\Wbem;%SystemRoot%\System32\WindowsPowerShell\v1.0"
set "PATH=%SYS_PATH%"
set "LAUNCH_LOG="
set "LOG=nul"
call :open_log

>>"%LOG%" echo(ISAR launch from %BASE%, started %STAMP%
:: Inherited conda, mamba, Python and Qt settings must not reach this ISAR;
:: the names come from a "set" listing in a file (no command-form for).
set "SCRATCH=%TEMP%\isar-launch-%RANDOM%%RANDOM%%RANDOM%.txt"
> "%SCRATCH%" set
for /f "usebackq delims==" %%V in ("%SCRATCH%") do call :clear_one "%%V"
del /f /q "%SCRATCH%" >nul 2>nul
:: -I below covers this Python; the GUI's own Python children (the analysis
:: service) inherit this and ignore user-site packages too.
set "PYTHONNOUSERSITE=1"

if not exist "%ENVP%\pythonw.exe" goto no_env
set "PATH=%ENVP%;%ENVP%\Library\mingw-w64\bin;%ENVP%\Library\usr\bin;%ENVP%\Library\bin;%ENVP%\Scripts;%ENVP%\bin;%SYS_PATH%"
set "CONDA_PREFIX=%ENVP%"
cd /d "%BASE%"
start "" "%ENVP%\pythonw.exe" -I -m isar_gui %*
set "RC=%ERRORLEVEL%"
>>"%LOG%" echo(Started %ENVP%\pythonw.exe -I -m isar_gui; start returned %RC%
if not "%RC%"=="0" goto start_failed
echo ISAR is starting. Its window appears in a few seconds.
endlocal & exit /b 0

:start_failed
echo.
echo [ERROR] ISAR could not be started (code %RC%).
goto show_and_wait

:no_env
>>"%LOG%" echo(No environment at %ENVP%: pythonw.exe is missing.
echo.
echo [ERROR] This installation's Python environment is missing or broken:
echo         %ENVP%
echo         Open the ISAR Launcher and choose Repair: it rebuilds the
echo         environment from the files kept beside the launcher.
goto show_and_wait

:show_and_wait
if defined LAUNCH_LOG echo.
if defined LAUNCH_LOG echo The launch log, %LAUNCH_LOG%:
if defined LAUNCH_LOG type "%LAUNCH_LOG%"
echo.
choice /C C /N /M "Press C to continue."
endlocal & exit /b 1

:clear_one
set "CV=%~1"
if /i "%CV:~0,5%"=="CONDA" set "%CV%="
if /i "%CV:~0,5%"=="MAMBA" set "%CV%="
if /i "%CV:~0,6%"=="PYTHON" set "%CV%="
if /i "%CV:~0,3%"=="QT_" set "%CV%="
goto :eof

:: The launch log: a fresh name (stamp and random suffix from PowerShell, read
:: through a file and checked), never one another run is writing.
:open_log
set "STAMP=unknown"
set "SUFFIX="
set "IDS=%TEMP%\isar-launch-ids-%RANDOM%%RANDOM%%RANDOM%.txt"
powershell -NoProfile -NonInteractive -Command "(Get-Date -Format 'yyyyMMdd-HHmmss') + ' ' + [guid]::NewGuid().ToString('N').Substring(0,8)" > "%IDS%" 2>nul <nul
findstr /r /x /c:"[0123456789]*-[0123456789]* [0123456789abcdef]*" "%IDS%" >nul 2>nul
if errorlevel 1 goto open_log_no_ids
for /f "usebackq tokens=1,2" %%A in ("%IDS%") do set "STAMP=%%A"
for /f "usebackq tokens=1,2" %%A in ("%IDS%") do set "SUFFIX=%%B"
:open_log_no_ids
del /f /q "%IDS%" >nul 2>nul
if not defined SUFFIX set "SUFFIX=%RANDOM%%RANDOM%"
set "LOG_DIR="
if defined LOCALAPPDATA set "LOG_DIR=%LOCALAPPDATA%\ISAR\logs"
if defined LOG_DIR if not exist "%LOG_DIR%\" mkdir "%LOG_DIR%" >nul 2>nul
if defined LOG_DIR call :try_log
if defined LAUNCH_LOG goto prune_logs
set "LOG_DIR=%TEMP%\ISAR-logs"
if not exist "%LOG_DIR%\" mkdir "%LOG_DIR%" >nul 2>nul
call :try_log
if defined LAUNCH_LOG goto prune_logs
goto :eof

:try_log
if not exist "%LOG_DIR%\" goto :eof
set "TRY_LOG=%LOG_DIR%\launch-%STAMP%-%SUFFIX%.log"
if exist "%TRY_LOG%" set "TRY_LOG=%LOG_DIR%\launch-%STAMP%-%SUFFIX%-%RANDOM%.log"
if exist "%TRY_LOG%" goto :eof
type nul > "%TRY_LOG%" 2>nul
if not exist "%TRY_LOG%" goto :eof
set "LAUNCH_LOG=%TRY_LOG%"
set "LOG=%TRY_LOG%"
goto :eof

:: The ten newest launch logs stay; a log another ISAR still holds open is left.
:prune_logs
set "LIST=%TEMP%\isar-launch-list-%RANDOM%%RANDOM%%RANDOM%.txt"
dir /b /a-d /o-d "%LOG_DIR%\launch-*.log" > "%LIST%" 2>nul
for /f "usebackq skip=10 delims=" %%F in ("%LIST%") do del /f /q "%LOG_DIR%\%%F" >nul 2>nul
del /f /q "%LIST%" >nul 2>nul
goto :eof

@echo off
setlocal DisableDelayedExpansion

:: Collect diagnostics: <base>\launcher\collect-diagnostics.bat [/yes]
:: Zips copies of the install, launch, repair and uninstall logs, ISAR's own
:: GUI logs (%APPDATA%\isar_gui, and the %USERPROFILE%\isar_gui fallback), the
:: install records and a short machine summary into
:: %LOCALAPPDATA%\ISAR\diagnostics\isar-diagnostics-<stamp>-<8 hex>.zip
:: (%TEMP%\ISAR-diagnostics when that cannot be written). It needs nothing
:: from the Python environment, so it works when that is broken or gone. The
:: zip stays on this machine; sending it is the user's choice.
:: /yes: no Explorer window and no key to press.
:: Exit codes: 0 the zip was made, 1 it was not.
:: Rules as in the installer: no pipes, no command-form "for /f",
:: redirections first on echo lines, PowerShell inputs through variables.

for %%I in ("%~dp0..") do set "BASE=%%~fI"
set "PATH=%SystemRoot%\System32;%SystemRoot%;%SystemRoot%\System32\Wbem;%SystemRoot%\System32\WindowsPowerShell\v1.0"
set "YES="
if /i "%~1"=="/yes" set "YES=yes"
set "STAGE="
set "ZIP="

echo Collecting ISAR's logs and install records...
call :new_ids
set "ERR=PowerShell did not answer, so no file name could be made"
if not defined SUFFIX goto failed
set "STAGE=%TEMP%\isar-diagnostics-%STAMP%-%SUFFIX%"
set "ERR=the folder %STAGE% already exists"
if exist "%STAGE%" set "STAGE="
if not defined STAGE goto failed
set "S=%STAGE%\isar-diagnostics"
mkdir "%S%\records" 2>nul
set "ERR=could not make a working folder in %TEMP%"
if not exist "%S%\records\" goto failed

:: Copies only; a missing source is simply absent from the zip.
mkdir "%S%\logs" "%S%\logs-temp" "%S%\gui-logs-appdata" "%S%\gui-logs-home" 2>nul
if defined LOCALAPPDATA copy /y "%LOCALAPPDATA%\ISAR\logs\*.log" "%S%\logs\" >nul 2>nul
copy /y "%TEMP%\ISAR-logs\*.log" "%S%\logs-temp\" >nul 2>nul
if defined APPDATA copy /y "%APPDATA%\isar_gui\isar-gui.log*" "%S%\gui-logs-appdata\" >nul 2>nul
if defined USERPROFILE copy /y "%USERPROFILE%\isar_gui\isar-gui.log*" "%S%\gui-logs-home\" >nul 2>nul
copy /y "%BASE%\.isar-install-root" "%S%\records\" >nul 2>nul
copy /y "%BASE%\INSTALL_INFO.txt" "%S%\records\" >nul 2>nul
copy /y "%BASE%\tracked-workspace-markers.txt" "%S%\records\" >nul 2>nul
if defined LOCALAPPDATA copy /y "%LOCALAPPDATA%\ISAR\install-record.txt" "%S%\records\" >nul 2>nul

set "SUM=%S%\summary.txt"
>"%SUM%" echo(ISAR diagnostics, collected %STAMP%
>>"%SUM%" echo(Installation folder: %BASE%
>>"%SUM%" ver
>>"%SUM%" echo(PROCESSOR_ARCHITECTURE=%PROCESSOR_ARCHITECTURE%
>>"%SUM%" echo(NUMBER_OF_PROCESSORS=%NUMBER_OF_PROCESSORS%
set "HAS=missing"
if exist "%BASE%\env\python.exe" set "HAS=present"
>>"%SUM%" echo(env\python.exe: %HAS%
set "HAS=missing"
if exist "%BASE%\env\pythonw.exe" set "HAS=present"
>>"%SUM%" echo(env\pythonw.exe: %HAS%
set "HAS=missing"
if exist "%BASE%\app\.git\" set "HAS=present"
>>"%SUM%" echo(app\.git: %HAS%
>>"%SUM%" echo(
>>"%SUM%" echo(The installation folder (its last line gives the free space):
>>"%SUM%" dir /a "%BASE%"

set "OUT_DIR="
if defined LOCALAPPDATA set "OUT_DIR=%LOCALAPPDATA%\ISAR\diagnostics"
if defined OUT_DIR if not exist "%OUT_DIR%\" mkdir "%OUT_DIR%" >nul 2>nul
if defined OUT_DIR if not exist "%OUT_DIR%\" set "OUT_DIR="
if not defined OUT_DIR set "OUT_DIR=%TEMP%\ISAR-diagnostics"
if not exist "%OUT_DIR%\" mkdir "%OUT_DIR%" >nul 2>nul
set "ERR=could not make the folder for the zip, %OUT_DIR%"
if not exist "%OUT_DIR%\" goto failed
set "ZIP=%OUT_DIR%\isar-diagnostics-%STAMP%-%SUFFIX%.zip"
set "ERR=a zip of that name already exists: %ZIP%"
if exist "%ZIP%" set "ZIP="
if not defined ZIP goto failed

set "PSI_A=%S%"
set "PSI_B=%ZIP%"
powershell -NoProfile -NonInteractive -Command "try { Compress-Archive -LiteralPath $env:PSI_A -DestinationPath $env:PSI_B -ErrorAction Stop; 'ok' } catch { 'fail ' + $_.Exception.Message }" > "%STAGE%\zip.txt" 2>nul <nul
findstr /b /l /c:"ok" "%STAGE%\zip.txt" >nul 2>nul
set "ERR=the zip could not be made"
if errorlevel 1 goto zip_failed
if not exist "%ZIP%" goto failed
call :remove_stage

echo.
echo The diagnostics are in:
echo     %ZIP%
echo It holds ISAR's logs and install records, which name your folders. It
echo stays on this computer; send it to whoever helps you with ISAR if you choose.
if defined YES endlocal & exit /b 0
start "" "%SystemRoot%\explorer.exe" /select,"%ZIP%"
echo.
choice /C C /N /M "Press C to continue."
endlocal & exit /b 0

:zip_failed
type "%STAGE%\zip.txt"

:failed
call :remove_stage
echo.
echo [ERROR] The diagnostics could not be collected: %ERR%
echo         The logs themselves are in %LOCALAPPDATA%\ISAR\logs
if defined YES endlocal & exit /b 1
echo.
choice /C C /N /M "Press C to continue."
endlocal & exit /b 1

:remove_stage
if not defined STAGE goto :eof
cd /d "%SystemRoot%" 2>nul
rd /s /q "%STAGE%" 2>nul
goto :eof

:: STAMP (yyyyMMdd-HHmmss) and SUFFIX (8 hex) from PowerShell, through a
:: uniquely named file, checked before use.
:new_ids
set "STAMP="
set "SUFFIX="
set "IDS=%TEMP%\isar-diag-ids-%RANDOM%%RANDOM%%RANDOM%.txt"
powershell -NoProfile -NonInteractive -Command "(Get-Date -Format 'yyyyMMdd-HHmmss') + ' ' + [guid]::NewGuid().ToString('N').Substring(0,8)" > "%IDS%" 2>nul <nul
findstr /r /x /c:"[0123456789]*-[0123456789]* [0123456789abcdef]*" "%IDS%" >nul 2>nul
if errorlevel 1 goto new_ids_done
for /f "usebackq tokens=1,2" %%A in ("%IDS%") do set "STAMP=%%A"
for /f "usebackq tokens=1,2" %%A in ("%IDS%") do set "SUFFIX=%%B"
:new_ids_done
del /f /q "%IDS%" >nul 2>nul
goto :eof

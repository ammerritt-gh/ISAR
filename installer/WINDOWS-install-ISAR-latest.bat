@echo off
setlocal DisableDelayedExpansion

:: ISAR Windows installer, latest. It asks GitHub for the newest published release,
:: downloads that release's own WINDOWS-install-ISAR.bat, checks that the file names
:: that release, and runs it with the switches given here (plan S1U5, design D3).
:: Nothing else is installed by this file.
::   WINDOWS-install-ISAR-latest.bat [/dir <path>] [/yes] [/desktop yes or no]
:: Refused here, because each names a release or a test: /ref, /test-fail, /repair
:: and /uninstall. Exit codes: the release installer's own (0, 1, 2 or 3); 1 when this
:: file fails; 2 when no release is published yet (nothing was changed).
::
:: Batch rules as in WINDOWS-install-ISAR.bat: no pipes, no command-form "for /f",
:: every value from GitHub checked before a line expands it, PowerShell only with
:: -NoProfile -NonInteractive, and the release installer started with call.

set "GH_REPO=ammerritt-gh/ISAR"
:: Only Windows' own tools on PATH: a user's PATH may put another curl or PowerShell first.
set "PATH=%SystemRoot%\System32;%SystemRoot%;%SystemRoot%\System32\Wbem;%SystemRoot%\System32\WindowsPowerShell\v1.0"
set "CURL=%SystemRoot%\System32\curl.exe"

set "OUTCOME="
set "ERR="
set "YES="
set "BAD_SWITCH="
set "TAG="
set "LOG=nul"
set "LOGFILE="

call :new_ids
if not defined HEX goto no_work
set "WORK=%TEMP%\isar-latest-%STAMP%-%HEX:~0,8%"
if exist "%WORK%\" goto no_work
mkdir "%WORK%" 2>nul
if not exist "%WORK%\" goto no_work
call :open_log
>>"%LOG%" echo(ISAR latest installer: started %STAMP%
>>"%LOG%" ver

:: The switches are read here, then the release installer gets them as typed,
:: so its own checks see every value.
:check_args
if "%~1"=="" goto args_done
if /i "%~1"=="/yes" set "YES=yes"
if /i "%~1"=="/ref" set "BAD_SWITCH=/ref"
if /i "%~1"=="/test-fail" set "BAD_SWITCH=/test-fail"
if /i "%~1"=="/repair" set "BAD_SWITCH=/repair"
if /i "%~1"=="/uninstall" set "BAD_SWITCH=/uninstall"
shift
goto check_args

:args_done
if not defined BAD_SWITCH goto get_release
set "ERR=%BAD_SWITCH% is for test installs only, and this installer takes only the newest release. Nothing was changed."
if "%BAD_SWITCH%"=="/repair" set "ERR=/repair works on an installation that is already there: use its ISAR-Launcher.bat menu. Nothing was changed."
if "%BAD_SWITCH%"=="/uninstall" set "ERR=/uninstall works on an installation that is already there: use WINDOWS-uninstall-ISAR.bat. Nothing was changed."
goto refuse

:get_release
set "GH_URL=https://api.github.com/repos/%GH_REPO%/releases/latest"
>>"%LOG%" echo(GET %GH_URL%
"%CURL%" -sS -L --proto =https -H "Accept: application/vnd.github+json" -H "User-Agent: isar-installer" -o "%WORK%\latest.json" -w "%%{http_code}" "%GH_URL%" > "%WORK%\latest.code" 2>>"%LOG%" <nul
set "GH_CODE="
set /p "GH_CODE=" < "%WORK%\latest.code" 2>nul
>>"%LOG%" echo(HTTP %GH_CODE%
if "%GH_CODE%"=="404" goto no_release
if "%GH_CODE%"=="403" goto rate_limit
if "%GH_CODE%"=="429" goto rate_limit
if not "%GH_CODE%"=="200" goto no_answer

:: The tag must read v<digits>.<digits>.<digits>; PowerShell checks it and writes it only then.
set "PSI_A=%WORK%\latest.json"
powershell -NoProfile -NonInteractive -Command "try { $j=ConvertFrom-Json ([IO.File]::ReadAllText($env:PSI_A)); $t=[string]$j.tag_name; if ($t -cmatch '^v[0-9]+\.[0-9]+\.[0-9]+$') { Write-Output $t } } catch { }" > "%WORK%\tag.txt" 2>nul <nul
set "TAG="
set /p "TAG=" < "%WORK%\tag.txt" 2>nul
if not defined TAG goto no_tag
>>"%LOG%" echo(NEWEST_RELEASE=%TAG%

set "ENGINE_URL=https://raw.githubusercontent.com/%GH_REPO%/%TAG%/installer/WINDOWS-install-ISAR.bat"
set "ENGINE=%WORK%\WINDOWS-install-ISAR.bat"
"%CURL%" --fail -sS -L --proto =https -o "%ENGINE%" "%ENGINE_URL%" >>"%LOG%" 2>&1 <nul
if errorlevel 1 goto download_failed

:: The downloaded file must name this release and keep CRLF line endings (raw files are served as stored).
set "PSI_F=%ENGINE%"
set "PSI_T=%TAG%"
powershell -NoProfile -NonInteractive -Command "$t=[IO.File]::ReadAllText($env:PSI_F, [Text.Encoding]::ASCII); $q=[char]34; if ($t.Contains('set ' + $q + 'ISAR_RELEASE=' + $env:PSI_T + $q) -and -not ($t -match '(?<!\r)\n')) { exit 0 }; exit 1" >>"%LOG%" 2>&1 <nul
if errorlevel 1 goto not_release_file

>>"%LOG%" echo(RUNNING the installer of %TAG%
echo.
echo Installing ISAR %TAG%, the newest published release. This takes several minutes.
echo.
call "%ENGINE%" %*
set "RC=%ERRORLEVEL%"
>>"%LOG%" echo(RELEASE INSTALLER EXIT=%RC%
set "OUTCOME=failed"
if "%RC%"=="0" set "OUTCOME=installed"
if "%RC%"=="2" set "OUTCOME=refused"
if "%RC%"=="3" set "OUTCOME=installed-selftest-failed"
>>"%LOG%" echo(OUTCOME=%OUTCOME%
cd /d "%SystemRoot%" 2>nul
rd /s /q "%WORK%" 2>nul
endlocal & exit /b %RC%

:no_release
set "ERR=no ISAR release has been published on GitHub yet, so there is nothing to install. Nothing was changed; try again once a release is out."
goto refuse

:rate_limit
set "ERR=GitHub's limit for anonymous requests was reached; try again in an hour"
goto fail

:no_answer
set "ERR=GitHub did not answer; check the internet connection and try again"
goto fail

:no_tag
>>"%LOG%" echo(GitHub's answer named no release tag this installer can use
set "ERR=GitHub's answer named no release tag this installer can use; try again later"
goto fail

:download_failed
set "ERR=the installer of release %TAG% could not be downloaded from GitHub; check the internet connection and try again"
goto fail

:not_release_file
set "ERR=the installer downloaded for release %TAG% does not name that release, or has LF line endings; nothing was run"
goto fail

:refuse
set "OUTCOME=refused"
set "RC=2"
goto finish

:fail
set "OUTCOME=failed"
set "RC=1"
goto finish

:no_work
echo.
echo [ERROR] The installer could not make its working folder in:
echo         %TEMP%
echo         PowerShell may be missing or blocked, or the folder is not writable.
endlocal & exit /b 1

:finish
cd /d "%SystemRoot%" 2>nul
rd /s /q "%WORK%" 2>nul
echo.
echo ============================================================================
if "%OUTCOME%"=="refused" echo   THE LATEST INSTALL WAS REFUSED
if "%OUTCOME%"=="failed" echo   THE LATEST INSTALL FAILED
echo ============================================================================
echo Reason: %ERR%
>>"%LOG%" echo(REASON: %ERR%
>>"%LOG%" echo(OUTCOME=%OUTCOME%
if defined LOGFILE echo The log of this run: %LOGFILE%
if defined YES goto finish_exit
echo.
choice /C C /N /M "Press C to close this window."
:finish_exit
endlocal & exit /b %RC%

:: STAMP (yyyyMMdd-HHmmss) and HEX (32 hex) from PowerShell, which writes them only in that form.
:new_ids
set "STAMP="
set "HEX="
set "IDS=%TEMP%\isar-latest-ids-%RANDOM%%RANDOM%.txt"
powershell -NoProfile -NonInteractive -Command "$s=Get-Date -Format 'yyyyMMdd-HHmmss'; $h=[guid]::NewGuid().ToString('N'); if ($s -cmatch '^[0-9]{8}-[0-9]{6}$' -and $h -cmatch '^[0-9a-f]{32}$') { Write-Output ($s + ' ' + $h) }" > "%IDS%" 2>nul <nul
set "ID_LINE="
set /p "ID_LINE=" < "%IDS%" 2>nul
del /f /q "%IDS%" >nul 2>nul
if not defined ID_LINE goto :eof
if not "%ID_LINE:~48,1%"=="" goto :eof
set "STAMP=%ID_LINE:~0,15%"
set "HEX=%ID_LINE:~16,32%"
goto :eof

:: The log: %LOCALAPPDATA%\ISAR\logs, else %TEMP%\ISAR-logs. Its name holds this run's stamp and id.
:open_log
if defined LOCALAPPDATA call :try_log "%LOCALAPPDATA%\ISAR\logs"
if not defined LOGFILE call :try_log "%TEMP%\ISAR-logs"
if not defined LOGFILE echo [WARN] No log file could be written; this run goes ahead without one.
goto :eof

:try_log
set "TRY_DIR=%~1"
mkdir "%TRY_DIR%" >nul 2>nul
set "TRY_LOG=%TRY_DIR%\install-latest-%STAMP%-%HEX:~0,8%.log"
if exist "%TRY_LOG%" goto :eof
type nul > "%TRY_LOG%" 2>nul
if not exist "%TRY_LOG%" goto :eof
set "LOGFILE=%TRY_LOG%"
set "LOG=%TRY_LOG%"
goto :eof

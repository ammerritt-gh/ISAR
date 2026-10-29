@echo off
setlocal DisableDelayedExpansion

:: The ISAR launcher menu, <base>\ISAR-Launcher.bat. Every path comes from this
:: file's own location.
::   ISAR-Launcher.bat                     the menu
::   ISAR-Launcher.bat start [switches]    start ISAR (switches go to run-isar.bat)
::   ISAR-Launcher.bat repair [switches]   Repair (the installer copy in launcher\)
::   ISAR-Launcher.bat diagnostics [/yes]  Collect diagnostics
::   ISAR-Launcher.bat uninstall [switches] Uninstall
:: Exit codes as the installer's: 0 success, 1 failed, 2 cancelled or refused.

set "BASE=%~dp0"
if "%BASE:~-1%"=="\" set "BASE=%BASE:~0,-1%"
set "ENGINE=%BASE%\launcher\WINDOWS-install-ISAR.bat"
set "RUNNER=%BASE%\launcher\run-isar.bat"
set "COLLECTOR=%BASE%\launcher\collect-diagnostics.bat"
set "PATH=%SystemRoot%\System32;%SystemRoot%;%SystemRoot%\System32\Wbem;%SystemRoot%\System32\WindowsPowerShell\v1.0"
call :read_banner

if "%~1"=="" goto menu
set "ACTION=%~1"
shift
set "REST="

:collect_rest
if "%~1"=="" goto dispatch
set "REST=%REST% %1"
shift
goto collect_rest

:dispatch
if /i "%ACTION%"=="start" goto act_start
if /i "%ACTION%"=="repair" goto act_repair
if /i "%ACTION%"=="diagnostics" goto act_diagnostics
if /i "%ACTION%"=="uninstall" goto act_uninstall
echo [ERROR] Unknown action. Use start, repair, diagnostics or uninstall.
endlocal & exit /b 2

:act_start
if not exist "%RUNNER%" goto runner_missing
call "%RUNNER%" %REST%
endlocal & exit /b %ERRORLEVEL%

:act_repair
if not exist "%ENGINE%" goto engine_missing
call "%ENGINE%" /repair /dir "%BASE%" %REST%
endlocal & exit /b %ERRORLEVEL%

:act_diagnostics
if not exist "%COLLECTOR%" goto collector_missing
call "%COLLECTOR%" %REST%
endlocal & exit /b %ERRORLEVEL%

:act_uninstall
if not exist "%ENGINE%" goto engine_missing
:: Control passes to the installer copy (no call, no start): this file is
:: about to be removed, so cmd must never come back to read another line of it.
"%ENGINE%" /uninstall /dir "%BASE%" %REST%

:menu
cls
echo ============================================================================
echo                               ISAR Launcher
echo   %BANNER%
echo ============================================================================
echo.
echo   Installed in: %BASE%
echo.
echo   [1] Start ISAR
echo   [2] Repair
echo   [3] Collect diagnostics
echo   [4] Uninstall
echo   [5] Exit
echo.
choice /C 12345 /N /M "Choose 1-5: "
:: Exact comparisons, never an "if errorlevel" ladder: choice returns 255 on an
:: error and 0 on Ctrl-C, and a descending ladder would send both into the
:: option at its top.
set "PICK=%ERRORLEVEL%"
if "%PICK%"=="1" goto menu_start
if "%PICK%"=="2" goto menu_repair
if "%PICK%"=="3" goto menu_diagnostics
if "%PICK%"=="4" goto menu_uninstall
if "%PICK%"=="5" goto quit
if "%PICK%"=="255" goto quit
goto menu

:menu_start
if not exist "%RUNNER%" goto runner_missing
call "%RUNNER%"
if "%ERRORLEVEL%"=="0" goto quit
goto menu

:menu_repair
if not exist "%ENGINE%" goto engine_missing
call "%ENGINE%" /repair /dir "%BASE%"
goto menu

:menu_diagnostics
if not exist "%COLLECTOR%" goto collector_missing
call "%COLLECTOR%"
goto menu

:menu_uninstall
if not exist "%ENGINE%" goto engine_missing
"%ENGINE%" /uninstall /dir "%BASE%"

:runner_missing
echo.
echo [ERROR] launcher\run-isar.bat is missing from:
echo         %BASE%
echo         Run the ISAR installer again into this folder to restore it.
goto back

:collector_missing
echo.
echo [ERROR] launcher\collect-diagnostics.bat is missing from:
echo         %BASE%
goto back

:engine_missing
echo.
echo [ERROR] launcher\WINDOWS-install-ISAR.bat is missing from:
echo         %BASE%
echo         To uninstall, download WINDOWS-uninstall-ISAR.bat from the ISAR
echo         releases page on GitHub and double-click it.
goto back

:back
if defined ACTION endlocal & exit /b 1
echo.
choice /C C /N /M "Press C to go back to the menu."
goto menu

:quit
endlocal & exit /b 0

:: BANNER: the release, or "TEST INSTALLATION of <branch> at <commit>", read
:: from INSTALL_INFO.txt. Each value is checked against its grammar before
:: anything expands it; a value that fails shows as unknown.
:read_banner
set "BANNER=release unknown"
set "B_KIND="
set "B_REL="
set "B_REF="
set "B_COMMIT="
if not exist "%BASE%\INSTALL_INFO.txt" goto :eof
for /f "usebackq tokens=1,* delims==" %%A in ("%BASE%\INSTALL_INFO.txt") do if "%%A"=="INSTALL_KIND" set "B_KIND=%%B"
for /f "usebackq tokens=1,* delims==" %%A in ("%BASE%\INSTALL_INFO.txt") do if "%%A"=="ISAR_RELEASE" set "B_REL=%%B"
for /f "usebackq tokens=1,* delims==" %%A in ("%BASE%\INSTALL_INFO.txt") do if "%%A"=="SOURCE_REF" set "B_REF=%%B"
for /f "usebackq tokens=1,* delims==" %%A in ("%BASE%\INSTALL_INFO.txt") do if "%%A"=="SOURCE_COMMIT" set "B_COMMIT=%%B"
set "B_FILE=%TEMP%\isar-launcher-%RANDOM%%RANDOM%%RANDOM%.txt"
> "%B_FILE%" set B_
set "D=[0123456789]"
set "AL=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
set "B_OK="
findstr /r /x /c:"B_REL=v%D%%D%*\.%D%%D%*\.%D%%D%*" "%B_FILE%" >nul 2>nul
if not errorlevel 1 set "B_OK=rel"
findstr /x /c:"B_KIND=test" "%B_FILE%" >nul 2>nul
if errorlevel 1 goto banner_done
set "B_OK="
findstr /r /x /c:"B_REF=[%AL%][%AL%._/-]*" "%B_FILE%" >nul 2>nul
if errorlevel 1 goto banner_done
findstr /r /x /c:"B_COMMIT=[0123456789abcdef][0123456789abcdef]*" "%B_FILE%" >nul 2>nul
if errorlevel 1 goto banner_done
set "B_OK=test"
:banner_done
del /f /q "%B_FILE%" >nul 2>nul
if "%B_OK%"=="rel" set "BANNER=Release %B_REL%"
if "%B_OK%"=="test" set "BANNER=TEST INSTALLATION of %B_REF% at %B_COMMIT:~0,8%"
goto :eof

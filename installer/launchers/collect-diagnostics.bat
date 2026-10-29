@echo off
setlocal DisableDelayedExpansion

:: Collect diagnostics: <base>\launcher\collect-diagnostics.bat [/yes].
:: Placeholder until the collector is built (it zips the logs and install
:: records into %LOCALAPPDATA%\ISAR\diagnostics). Until then it names the
:: logs folder, so a user can send the newest install log by hand.

echo.
echo Collecting diagnostics is not available in this build of ISAR yet.
echo The logs of every install and start are in:
echo     %LOCALAPPDATA%\ISAR\logs
echo Send the newest install-*.log from there to whoever helps you with ISAR.
if /i "%~1"=="/yes" endlocal & exit /b 1
echo.
choice /C C /N /M "Press C to continue."
endlocal & exit /b 1

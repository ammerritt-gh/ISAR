@echo off
rem Launch the ISAR GUI (isar view). Double-click to open the most recent workspace,
rem drag a workspace folder onto this file to open that one, or run:
rem   run-isar-gui.bat C:\path\to\workspace
rem Set MICROMAMBA_EXE to override the micromamba location.
cd /d "%~dp0"
if defined MICROMAMBA_EXE (set "MM=%MICROMAMBA_EXE%") else (set "MM=%USERPROFILE%\AppData\Local\micromamba\micromamba.exe")
"%MM%" run -n tavi-dev python -m isar_gui %*
if errorlevel 1 pause

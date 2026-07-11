@echo off
rem Launch the ISAR GUI (isar view). Double-click to open the most recent workspace,
rem drag a workspace folder onto this file to open that one, or run:
rem   run-isar-gui.bat C:\path\to\workspace
cd /d C:\Users\AMM\Documents\Github\ISAR
%USERPROFILE%\AppData\Local\micromamba\micromamba.exe run -n tavi-dev python -m isar_gui %*
if errorlevel 1 pause

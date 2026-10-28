@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\open_pages.ps1" -Page Monitor
if errorlevel 1 pause

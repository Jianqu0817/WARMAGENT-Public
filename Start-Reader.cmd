@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_reader.ps1"
if errorlevel 1 pause

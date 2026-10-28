@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_wechat.ps1" -Dual
if errorlevel 1 pause

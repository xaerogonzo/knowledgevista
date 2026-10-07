@echo off
REM Generated from nuitka-build.bat.template
REM Launcher for build.ps1 - bypasses execution policy and keeps the window
REM open at the end so build output stays visible.
powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0build.ps1"
pause

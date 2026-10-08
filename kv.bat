@echo off
rem Run Knowledge Vista from source. No arguments opens the library window;
rem any arguments go to the CLI, e.g.  kv.bat scan   or   kv.bat search "term"
setlocal
cd /d "%~dp0"

where uv >nul 2>nul
if errorlevel 1 (
    echo uv was not found on PATH. Install it from https://docs.astral.sh/uv/
    pause
    exit /b 1
)

if "%~1"=="" (
    uv run --extra gui --extra extract kv gui
) else (
    uv run --extra extract kv %*
)
set RC=%ERRORLEVEL%
if not "%RC%"=="0" if "%~1"=="" pause
exit /b %RC%

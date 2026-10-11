@echo off
rem NOTE: keep this file pure ASCII so cmd.exe (code page 936) never garbles it.
chcp 65001 >nul
setlocal
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
echo === Darkzone auto-refresh (DRY RUN: screen read + coordinate math only, never taps) ===
rem pick a Python: project .venv first, then the DSH bundled runtime, then PATH
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" -m darkzone.runner %*
echo.
pause

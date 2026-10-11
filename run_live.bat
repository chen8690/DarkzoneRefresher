@echo off
rem NOTE: keep this file pure ASCII so cmd.exe (code page 936) never garbles it.
chcp 65001 >nul
setlocal
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
echo === Darkzone LIVE: really taps the refresh key, stops when all rows are in pool ===
echo     It never taps Dispatch. Per-row refresh cap is 40; change it: run_live.bat --limit 20
echo     To stop early: close this window / press Ctrl+C
echo.
rem pick a Python: project .venv first, then the DSH bundled runtime, then PATH
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" -m darkzone.runner --live %*
echo.
pause

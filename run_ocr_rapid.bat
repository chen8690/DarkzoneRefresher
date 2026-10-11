@echo off
rem NOTE: keep this file pure ASCII so cmd.exe (code page 936) never garbles it.
chcp 65001 >nul
setlocal
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
echo === Darkzone auto-refresh with the RapidOCR engine (more accurate, ~10 s per screen) ===
echo     Same usage as dry_run.bat / run_live.bat: no args = dry run, add --live to really tap.
echo     Example: run_ocr_rapid.bat --live --limit 30 --no-sweep
echo.
rem RapidOCR lives in the project .venv only; see README section 3.3
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [NG] not found: %PY%
  echo      create the venv first, e.g.:
  echo        python -m venv .venv
  echo        .venv\Scripts\pip install -r requirements.txt rapidocr-onnxruntime==1.4.4
  pause
  exit /b 2
)
"%PY%" -m darkzone.runner --ocr rapid %*
echo.
pause

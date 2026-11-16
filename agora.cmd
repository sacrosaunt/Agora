@echo off
rem agora on Windows: put this folder on PATH (or call this file by its full path).
setlocal
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
  where py >nul 2>nul && (set "PY=py" & set "PYARG=-3") || set "PY=python"
)
"%PY%" %PYARG% -X utf8 "%~dp0agora" %*

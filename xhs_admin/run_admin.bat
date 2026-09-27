@echo off
rem Start XHS Admin (http://127.0.0.1:8000)
rem Working dir is this script's folder, so the package can be moved anywhere.
cd /d "%~dp0"
set PYTHONUTF8=1
set NO_PROXY=*
set HTTP_PROXY=
set HTTPS_PROXY=
set http_proxy=
set https_proxy=

rem Prefer the bundled venv, fall back to whatever python is on PATH.
set PYTHON=C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe
if not exist "%PYTHON%" set PYTHON=python

echo Starting XHS Admin on http://127.0.0.1:8000 ...
"%PYTHON%" -m uvicorn app:app --host 127.0.0.1 --port 8000
pause

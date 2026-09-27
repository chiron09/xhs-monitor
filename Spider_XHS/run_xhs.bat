@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY=C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

if "%1"=="" (
    "%PY%" -m spider.spider
) else (
    "%PY%" %*
)
pause

@echo off
REM run_weekly.bat — invoked by Windows Task Scheduler on Sundays.
REM Builds the weekly digest and posts to Notion's daily-reports DB.

setlocal
set PROJECT="D:\AI-Workstation\Antigravity\apps\Profit Generation"
cd /d %PROJECT%

set PYTHONIOENCODING=utf-8
set PY="D:\AI-Hub\environments\conda-envs\trading\python.exe"

%PY% -m monitoring.weekly_digest
exit /b %ERRORLEVEL%

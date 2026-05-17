@echo off
REM run_weekly.bat — invoked by Windows Task Scheduler on Sundays.
REM Builds the weekly digest AND the live-vs-backtest divergence report,
REM posting both to Notion's daily-reports DB as separate pages.

setlocal
set PROJECT="D:\AI-Workstation\Antigravity\apps\Profit Generation"
cd /d %PROJECT%

set PYTHONIOENCODING=utf-8
set PY="D:\AI-Hub\environments\conda-envs\trading\python.exe"

%PY% -m monitoring.weekly_digest
set RC1=%ERRORLEVEL%

%PY% -m monitoring.live_vs_backtest
set RC2=%ERRORLEVEL%

REM Non-zero from either subscript propagates so Task Scheduler shows
REM the failure. Prefer the first non-zero, then the second.
if not "%RC1%"=="0" exit /b %RC1%
exit /b %RC2%

@echo off
REM run_daily.bat — invoked by Windows Task Scheduler at EOD.
REM Generates today's daily report (snapshots + fires + news + outcomes),
REM auto-posts to Notion, pushes a one-line summary to Telegram if configured.

setlocal
set PROJECT="D:\AI-Workstation\Antigravity\apps\Profit Generation"
cd /d %PROJECT%

set PYTHONIOENCODING=utf-8

REM Use the conda env Python directly to skip "conda run" temp-file races.
set PY="D:\AI-Hub\environments\conda-envs\trading\python.exe"

%PY% -m monitoring.daily_report
set EXITCODE=%ERRORLEVEL%

REM Best-effort cache pruning.
%PY% -c "from config.cache import cache_purge_expired; print('cache_purge_expired:', cache_purge_expired())" 2>nul

exit /b %EXITCODE%

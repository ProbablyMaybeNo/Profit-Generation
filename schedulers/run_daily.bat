@echo off
REM run_daily.bat — invoked by Windows Task Scheduler at EOD.
REM Generates today's daily report (snapshots + fires + news + outcomes),
REM auto-posts to Notion, pushes a one-line summary to Telegram if configured.

setlocal
set PROJECT=D:\AI-Workstation\Antigravity\apps\Profit Generation
cd /d "%PROJECT%"

set PYTHONIOENCODING=utf-8
set LOGFILE=%PROJECT%\logs\schtask_run_daily.log

echo. >> "%LOGFILE%"
echo === run_daily start %DATE% %TIME% === >> "%LOGFILE%"

REM Use the conda env Python directly to skip "conda run" temp-file races.
set PY=D:\AI-Hub\environments\conda-envs\trading\python.exe

REM 5.5.2 — Refresh liquidity_snapshots before the daily report runs so the
REM trend scanner has fresh ADV data. Best-effort; failure isolated from the
REM main daily report.
"%PY%" scripts\bootstrap_liquidity.py >> "%LOGFILE%" 2>&1

"%PY%" -m monitoring.daily_report >> "%LOGFILE%" 2>&1
set EXITCODE=%ERRORLEVEL%

REM 5.5.3 — EOD close-out of intraday-strategy positions. Best-effort.
REM Failures here must not poison the daily-report exit code.
"%PY%" -m monitoring.close_intraday_positions >> "%LOGFILE%" 2>&1

REM Best-effort cache pruning.
"%PY%" -c "from config.cache import cache_purge_expired; print('cache_purge_expired:', cache_purge_expired())" >> "%LOGFILE%" 2>&1

echo === run_daily exit %EXITCODE% %DATE% %TIME% === >> "%LOGFILE%"
exit /b %EXITCODE%

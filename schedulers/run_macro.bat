@echo off
REM run_macro.bat — invoked daily by Windows Task Scheduler.
REM Pulls latest VIX / T10Y2Y / DXY from FRED into trading.db.macro.
REM Idempotent on (series_id, bar_date) — safe to re-run.

setlocal
set PROJECT=D:\AI-Workstation\Antigravity\apps\Profit Generation
cd /d "%PROJECT%"

set PYTHONIOENCODING=utf-8
set LOGFILE=%PROJECT%\logs\schtask_run_macro.log
set PY=D:\AI-Hub\environments\conda-envs\trading\python.exe

echo. >> "%LOGFILE%"
echo === run_macro start %DATE% %TIME% === >> "%LOGFILE%"

"%PY%" -m monitoring.macro_fetcher >> "%LOGFILE%" 2>&1
set EXITCODE=%ERRORLEVEL%

echo === run_macro exit %EXITCODE% %DATE% %TIME% === >> "%LOGFILE%"
exit /b %EXITCODE%

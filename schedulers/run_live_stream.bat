@echo off
REM run_live_stream.bat — long-running Alpaca IEX WebSocket listener (7.5.1).
REM Subscribes to bars + trades for TRACKED_STOCKS + TRACKED_SECTORS (10
REM symbols) and upserts every minute bar into intraday_bars.
REM
REM Designed to be launched once at boot (or registered as a Windows task
REM with /sc onstart) and stay running indefinitely. Reconnects on socket
REM drops with exponential backoff (1, 2, 4, 8, ..., capped at 60s) so the
REM only restart needed is on a hard machine reboot.
REM
REM AUGMENT-NOT-REPLACE: the existing 15m intraday loop in run_intraday.bat
REM keeps running unchanged. This listener is pure data ingestion — no
REM behavior change on existing trades.

setlocal
set PROJECT=D:\AI-Workstation\Antigravity\apps\Profit Generation
cd /d "%PROJECT%"

set PYTHONIOENCODING=utf-8
set LOGFILE=%PROJECT%\logs\schtask_run_live_stream.log
set PY=D:\AI-Hub\environments\conda-envs\trading\python.exe

echo. >> "%LOGFILE%"
echo === run_live_stream start %DATE% %TIME% === >> "%LOGFILE%"

"%PY%" -m monitoring.live_stream >> "%LOGFILE%" 2>&1
set EXITCODE=%ERRORLEVEL%

echo === run_live_stream exit %EXITCODE% %DATE% %TIME% === >> "%LOGFILE%"
exit /b %EXITCODE%

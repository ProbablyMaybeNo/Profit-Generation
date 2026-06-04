@echo off
REM run_daily_brief.bat — Invoked by Windows Task Scheduler at 15:00 PT.
REM Sends the detailed daily trading brief to Telegram.

setlocal
set PROJECT=D:\AI-Workstation\Antigravity\apps\Profit Generation
cd /d "%PROJECT%"

set PYTHONIOENCODING=utf-8
set LOGFILE=%PROJECT%\logs\schtask_run_daily_brief.log

echo. >> "%LOGFILE%"
echo === run_daily_brief start %DATE% %TIME% === >> "%LOGFILE%"

set PY=D:\AI-Hub\environments\conda-envs\trading\python.exe

"%PY%" -m monitoring.daily_brief >> "%LOGFILE%" 2>&1
set EXITCODE=%ERRORLEVEL%

echo === run_daily_brief exit %EXITCODE% %DATE% %TIME% === >> "%LOGFILE%"
exit /b %EXITCODE%

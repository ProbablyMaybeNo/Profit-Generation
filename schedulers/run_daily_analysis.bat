@echo off
REM run_daily_analysis.bat — Invoked by Windows Task Scheduler at 15:20 PT.
REM Sends the LLM deep-analysis report to Telegram.

setlocal
set PROJECT=D:\AI-Workstation\Antigravity\apps\Profit Generation
cd /d "%PROJECT%"

set PYTHONIOENCODING=utf-8
set LOGFILE=%PROJECT%\logs\schtask_run_daily_analysis.log

echo. >> "%LOGFILE%"
echo === run_daily_analysis start %DATE% %TIME% === >> "%LOGFILE%"

set PY=D:\AI-Hub\environments\conda-envs\trading\python.exe

"%PY%" -m monitoring.daily_analysis >> "%LOGFILE%" 2>&1
set EXITCODE=%ERRORLEVEL%

echo === run_daily_analysis exit %EXITCODE% %DATE% %TIME% === >> "%LOGFILE%"
exit /b %EXITCODE%

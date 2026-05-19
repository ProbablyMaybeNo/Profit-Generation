@echo off
REM run_monitor.bat — invoked every 15 min by Windows Task Scheduler.
REM Runs the heartbeat / monitor loop as a one-shot tick.

setlocal
set PROJECT=D:\AI-Workstation\Antigravity\apps\Profit Generation
cd /d "%PROJECT%"

set PYTHONIOENCODING=utf-8
set LOGFILE=%PROJECT%\logs\schtask_run_monitor.log
set PY=D:\AI-Hub\environments\conda-envs\trading\python.exe

echo. >> "%LOGFILE%"
echo === run_monitor start %DATE% %TIME% === >> "%LOGFILE%"

"%PY%" monitor.py >> "%LOGFILE%" 2>&1
set EXITCODE=%ERRORLEVEL%

echo === run_monitor exit %EXITCODE% %DATE% %TIME% === >> "%LOGFILE%"
exit /b %EXITCODE%

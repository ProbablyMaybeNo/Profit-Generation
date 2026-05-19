@echo off
REM run_reconcile.bat — invoked nightly by Windows Task Scheduler.
REM Compares Alpaca-reported open positions to the paper_trades table.

setlocal
set PROJECT=D:\AI-Workstation\Antigravity\apps\Profit Generation
cd /d "%PROJECT%"

set PYTHONIOENCODING=utf-8
set LOGFILE=%PROJECT%\logs\schtask_run_reconcile.log
set PY=D:\AI-Hub\environments\conda-envs\trading\python.exe

echo. >> "%LOGFILE%"
echo === run_reconcile start %DATE% %TIME% === >> "%LOGFILE%"

"%PY%" -m monitoring.reconcile_positions >> "%LOGFILE%" 2>&1
set EXITCODE=%ERRORLEVEL%

echo === run_reconcile exit %EXITCODE% %DATE% %TIME% === >> "%LOGFILE%"
exit /b %EXITCODE%

@echo off
REM run_backup.bat — invoked by the \TradingSystem\Backup schtask.
REM Snapshots trading.db + records.jsonl + settings.json into
REM D:\Backups\profit-generation\YYYY-MM-DD\ and prunes anything older
REM than 30 days.

setlocal
set PROJECT=D:\AI-Workstation\Antigravity\apps\Profit Generation
cd /d "%PROJECT%"

set PYTHONIOENCODING=utf-8
set LOGFILE=%PROJECT%\logs\schtask_run_backup.log

echo. >> "%LOGFILE%"
echo === run_backup start %DATE% %TIME% === >> "%LOGFILE%"

py -3.13 scripts\backup.py >> "%LOGFILE%" 2>&1
set EXITCODE=%ERRORLEVEL%

echo === run_backup exit %EXITCODE% %DATE% %TIME% === >> "%LOGFILE%"
exit /b %EXITCODE%

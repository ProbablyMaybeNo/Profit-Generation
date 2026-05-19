@echo off
REM run_intraday.bat — invoked by Windows Task Scheduler every 15 min.
REM Three-step intraday sequence (each step self-checks market hours):
REM   (a) intraday_monitor --once  : synthesize today's bar for EOD strategies (informational)
REM   (b) intraday_fires           : commit intraday bar fires to signals table  (5.1.2)
REM   (c) auto_trader_intraday     : submit paper orders for new intraday signals (5.2.2)
REM Purges expired cache entries at the end.

setlocal
set PROJECT=D:\AI-Workstation\Antigravity\apps\Profit Generation
cd /d "%PROJECT%"

set PYTHONIOENCODING=utf-8
set LOGFILE=%PROJECT%\logs\schtask_run_intraday.log
set PY=D:\AI-Hub\environments\conda-envs\trading\python.exe

echo. >> "%LOGFILE%"
echo === run_intraday start %DATE% %TIME% === >> "%LOGFILE%"

REM (a) Synthesis pass — projects EOD signals from today's intraday bars.
"%PY%" -m monitoring.intraday_monitor --once >> "%LOGFILE%" 2>&1
set EC_SYNTH=%ERRORLEVEL%
echo --- intraday_monitor exit %EC_SYNTH% --- >> "%LOGFILE%"

REM (b) Commit intraday bar fires to the signals table.
"%PY%" -m monitoring.intraday_fires >> "%LOGFILE%" 2>&1
set EC_FIRES=%ERRORLEVEL%
echo --- intraday_fires exit %EC_FIRES% --- >> "%LOGFILE%"

REM (c) Auto-trader picks up newly-recorded intraday signals.
"%PY%" -m monitoring.auto_trader_intraday >> "%LOGFILE%" 2>&1
set EC_AT=%ERRORLEVEL%
echo --- auto_trader_intraday exit %EC_AT% --- >> "%LOGFILE%"

"%PY%" -c "from config.cache import cache_purge_expired; print('cache_purge_expired:', cache_purge_expired())" >> "%LOGFILE%" 2>&1

REM Propagate the worst exit code so schtask history surfaces failures.
set EXITCODE=0
if %EC_SYNTH% NEQ 0 set EXITCODE=%EC_SYNTH%
if %EC_FIRES% NEQ 0 set EXITCODE=%EC_FIRES%
if %EC_AT%    NEQ 0 set EXITCODE=%EC_AT%

echo === run_intraday exit %EXITCODE% %DATE% %TIME% === >> "%LOGFILE%"
exit /b %EXITCODE%

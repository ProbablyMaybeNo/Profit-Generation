@echo off
REM run_daily_report.bat
REM Generate daily trading report and post to Notion.
REM Invoked by Windows Task Scheduler at ~09:00 ET each weekday.

setlocal

set TRADING_ROOT=D:\AI-Workstation\Antigravity\apps\Trading
cd /d "%TRADING_ROOT%"

REM Activate conda env via miniconda's activate.bat (avoids shell init issues)
call C:\miniconda3\Scripts\activate.bat trading

if errorlevel 1 (
  echo [%DATE% %TIME%] Failed to activate conda env "trading" >> "%TRADING_ROOT%\logs\monitoring.log"
  exit /b 1
)

python -m monitoring.run_daily
set EXITCODE=%ERRORLEVEL%

call conda deactivate

exit /b %EXITCODE%

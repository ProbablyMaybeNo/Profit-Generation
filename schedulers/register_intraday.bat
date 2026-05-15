@echo off
REM register_intraday.bat — Run as Administrator.
REM Registers the intraday monitor as a Windows Scheduled Task that fires
REM every 15 minutes. The script self-checks market_is_open, so off-hours
REM ticks exit immediately without doing work.

echo Registering TradingSystem\Intraday with Windows Task Scheduler...

schtasks /create /tn "TradingSystem\Intraday" ^
  /tr "\"D:\AI-Workstation\Antigravity\apps\Profit Generation\schedulers\run_intraday.bat\"" ^
  /sc minute /mo 15 ^
  /sd 01/01/2026 ^
  /ru "%USERNAME%" ^
  /f

echo.
echo Done. Inspect:    schtasks /query /tn "TradingSystem\Intraday" /fo LIST
echo Trigger one run:  schtasks /run   /tn "TradingSystem\Intraday"
echo Pause:            schtasks /change /tn "TradingSystem\Intraday" /disable
echo Resume:           schtasks /change /tn "TradingSystem\Intraday" /enable
echo Remove:           schtasks /delete /tn "TradingSystem\Intraday" /f
echo.
schtasks /query /tn "TradingSystem\Intraday" /fo LIST
pause

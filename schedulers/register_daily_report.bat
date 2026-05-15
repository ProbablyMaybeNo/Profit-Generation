@echo off
REM register_daily_report.bat
REM Registers the daily trading report with Windows Task Scheduler.
REM Run as Administrator once. Job runs at 09:05 ET, weekdays only.

echo Registering TradingSystem\DailyReport with Windows Task Scheduler...

schtasks /create /tn "TradingSystem\DailyReport" ^
  /tr "D:\AI-Workstation\Antigravity\apps\Trading\schedulers\run_daily_report.bat" ^
  /sc weekly ^
  /d MON,TUE,WED,THU,FRI ^
  /st 09:05 ^
  /sd 04/27/2026 ^
  /ru "%USERNAME%" ^
  /rl HIGHEST ^
  /f

echo.
echo Done. Verify with: schtasks /query /tn "TradingSystem\DailyReport" /fo LIST
schtasks /query /tn "TradingSystem\DailyReport" /fo LIST
echo.
echo To run manually now: schtasks /run /tn "TradingSystem\DailyReport"
echo To disable temporarily: schtasks /change /tn "TradingSystem\DailyReport" /disable
echo To re-enable: schtasks /change /tn "TradingSystem\DailyReport" /enable
echo To remove: schtasks /delete /tn "TradingSystem\DailyReport" /f
pause

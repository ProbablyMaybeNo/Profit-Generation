@echo off
REM register_daily_brief.bat — Run as Administrator.
REM Registers TradingSystem\DailyBrief at 15:00 local time (3 PM Pacific).
REM Machine is on Pacific time; Task Scheduler local time tracks DST automatically.
REM Fires Mon-Fri, 20 min after DailyReport (14:30) and 20 min before DailyAnalysis (15:20).

echo Registering TradingSystem\DailyBrief with Windows Task Scheduler...

REM Unregister first (idempotent)
schtasks /delete /tn "TradingSystem\DailyBrief" /f 2>nul

schtasks /create /tn "TradingSystem\DailyBrief" ^
  /tr "cmd /c \"D:\AI-Workstation\Antigravity\apps\Profit Generation\schedulers\run_daily_brief.bat\"" ^
  /sc weekly /d MON,TUE,WED,THU,FRI ^
  /st 15:00 ^
  /sd 01/01/2026 ^
  /ru "%USERNAME%" ^
  /f

echo.
echo Done. Inspect:    schtasks /query /tn "TradingSystem\DailyBrief" /fo LIST
echo Trigger one run:  schtasks /run   /tn "TradingSystem\DailyBrief"
echo Pause:            schtasks /change /tn "TradingSystem\DailyBrief" /disable
echo Resume:           schtasks /change /tn "TradingSystem\DailyBrief" /enable
echo Remove:           schtasks /delete /tn "TradingSystem\DailyBrief" /f
echo.
schtasks /query /tn "TradingSystem\DailyBrief" /fo LIST
pause

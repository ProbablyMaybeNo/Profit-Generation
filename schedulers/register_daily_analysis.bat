@echo off
REM register_daily_analysis.bat — Run as Administrator.
REM Registers TradingSystem\DailyAnalysis at 15:20 local time (3:20 PM Pacific).
REM Fires 20 min after DailyBrief so the brief's data is settled before LLM analysis.
REM Machine is on Pacific time; Task Scheduler local time tracks DST automatically.

echo Registering TradingSystem\DailyAnalysis with Windows Task Scheduler...

REM Unregister first (idempotent)
schtasks /delete /tn "TradingSystem\DailyAnalysis" /f 2>nul

schtasks /create /tn "TradingSystem\DailyAnalysis" ^
  /tr "cmd /c \"D:\AI-Workstation\Antigravity\apps\Profit Generation\schedulers\run_daily_analysis.bat\"" ^
  /sc weekly /d MON,TUE,WED,THU,FRI ^
  /st 15:20 ^
  /sd 01/01/2026 ^
  /ru "%USERNAME%" ^
  /f

echo.
echo Done. Inspect:    schtasks /query /tn "TradingSystem\DailyAnalysis" /fo LIST
echo Trigger one run:  schtasks /run   /tn "TradingSystem\DailyAnalysis"
echo Pause:            schtasks /change /tn "TradingSystem\DailyAnalysis" /disable
echo Resume:           schtasks /change /tn "TradingSystem\DailyAnalysis" /enable
echo Remove:           schtasks /delete /tn "TradingSystem\DailyAnalysis" /f
echo.
schtasks /query /tn "TradingSystem\DailyAnalysis" /fo LIST
pause

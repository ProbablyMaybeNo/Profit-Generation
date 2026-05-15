@echo off
echo Registering trading system monitor with Windows Task Scheduler...

schtasks /create /tn "TradingSystem\Heartbeat" ^
  /tr "cmd /c conda activate trading && python \"D:\AI-Workstation\Antigravity\apps\Profit Generation\monitor.py\"" ^
  /sc minute /mo 15 ^
  /sd 01/01/2025 ^
  /ru "%USERNAME%" ^
  /f

echo.
echo Done. Verify with: schtasks /query /tn "TradingSystem\Heartbeat"
schtasks /query /tn "TradingSystem\Heartbeat" /fo LIST
pause

@echo off
REM register_crypto.bat — Run as Administrator.
REM Registers the 24/7 crypto scan as a Windows Scheduled Task.
REM Crypto markets never close, so this fires every 15 minutes around
REM the clock — no market_is_open gate.

echo Registering TradingSystem\Crypto with Windows Task Scheduler...

schtasks /create /tn "TradingSystem\Crypto" ^
  /tr "cmd /c \"D:\AI-Workstation\Antigravity\apps\Profit Generation\schedulers\run_crypto.bat\"" ^
  /sc minute /mo 15 ^
  /sd 01/01/2026 ^
  /ru "%USERNAME%" ^
  /f

echo.
echo Done. Inspect:    schtasks /query /tn "TradingSystem\Crypto" /fo LIST
echo Trigger one run:  schtasks /run   /tn "TradingSystem\Crypto"
echo Pause:            schtasks /change /tn "TradingSystem\Crypto" /disable
echo Resume:           schtasks /change /tn "TradingSystem\Crypto" /enable
echo Remove:           schtasks /delete /tn "TradingSystem\Crypto" /f
echo.
schtasks /query /tn "TradingSystem\Crypto" /fo LIST
pause

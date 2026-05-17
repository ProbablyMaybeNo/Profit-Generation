@echo off
REM register_backup.bat — Run as Administrator.
REM Registers a nightly snapshot at 22:30 local time.

echo Registering TradingSystem\Backup with Windows Task Scheduler...

schtasks /create /tn "TradingSystem\Backup" ^
  /tr "cmd /c \"D:\AI-Workstation\Antigravity\apps\Profit Generation\schedulers\run_backup.bat\"" ^
  /sc daily /st 22:30 ^
  /sd 01/01/2026 ^
  /ru "%USERNAME%" ^
  /f

echo.
echo Done. Inspect:    schtasks /query /tn "TradingSystem\Backup" /fo LIST
echo Trigger one run:  schtasks /run   /tn "TradingSystem\Backup"
echo Pause:            schtasks /change /tn "TradingSystem\Backup" /disable
echo Resume:           schtasks /change /tn "TradingSystem\Backup" /enable
echo Remove:           schtasks /delete /tn "TradingSystem\Backup" /f
echo.
schtasks /query /tn "TradingSystem\Backup" /fo LIST
pause

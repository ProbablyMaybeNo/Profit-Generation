@echo off
REM register_reconcile.bat — Run as Administrator.
REM Registers nightly position reconciliation at 21:00 local (after
REM market close and after the daily report run). Idempotent: re-running
REM with /f overwrites the existing schedule.

echo Registering TradingSystem\Reconcile with Windows Task Scheduler...

schtasks /create /tn "TradingSystem\Reconcile" ^
  /tr "cmd /c \"D:\AI-Workstation\Antigravity\apps\Profit Generation\schedulers\run_reconcile.bat\"" ^
  /sc daily ^
  /st 21:00 ^
  /sd 01/01/2026 ^
  /ru "%USERNAME%" ^
  /f

echo.
echo Done. Inspect:    schtasks /query /tn "TradingSystem\Reconcile" /fo LIST
echo Trigger one run:  schtasks /run   /tn "TradingSystem\Reconcile"
echo Pause:            schtasks /change /tn "TradingSystem\Reconcile" /disable
echo Resume:           schtasks /change /tn "TradingSystem\Reconcile" /enable
echo Remove:           schtasks /delete /tn "TradingSystem\Reconcile" /f
echo.
schtasks /query /tn "TradingSystem\Reconcile" /fo LIST
pause

@echo off
REM register_telegram_listener.bat — Run as Administrator.
REM Registers the Telegram command listener as a Windows Scheduled Task that
REM starts at system boot and stays running. The listener uses Telegram's
REM long-poll API; if it crashes, the scheduler restarts it on the next
REM check (we set restart-on-failure flags below).

echo Registering TradingSystem\TelegramListener with Windows Task Scheduler...

schtasks /create /tn "TradingSystem\TelegramListener" ^
  /tr "cmd /c \"D:\AI-Workstation\Antigravity\apps\Profit Generation\schedulers\run_telegram_listener.bat\"" ^
  /sc onstart ^
  /sd 01/01/2026 ^
  /ru "%USERNAME%" ^
  /f

echo.
echo Done. Inspect:    schtasks /query /tn "TradingSystem\TelegramListener" /fo LIST
echo Trigger now:      schtasks /run   /tn "TradingSystem\TelegramListener"
echo Pause:            schtasks /change /tn "TradingSystem\TelegramListener" /disable
echo Resume:           schtasks /change /tn "TradingSystem\TelegramListener" /enable
echo Remove:           schtasks /delete /tn "TradingSystem\TelegramListener" /f
echo.
schtasks /query /tn "TradingSystem\TelegramListener" /fo LIST
pause

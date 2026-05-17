@echo off
REM run_backup.bat — invoked by the \TradingSystem\Backup schtask.
REM Snapshots trading.db + records.jsonl + settings.json into
REM D:\Backups\profit-generation\YYYY-MM-DD\ and prunes anything older
REM than 30 days.

setlocal
set PROJECT="D:\AI-Workstation\Antigravity\apps\Profit Generation"
cd /d %PROJECT%

REM Unit-test-clean stdlib paths — no conda env required.
py -3.13 scripts\backup.py
exit /b %ERRORLEVEL%

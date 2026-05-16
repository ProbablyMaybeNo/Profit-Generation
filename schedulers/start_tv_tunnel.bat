@echo off
REM start_tv_tunnel.bat — Starts a Cloudflare quick-tunnel that exposes the
REM local TV webhook receiver to the public internet, and captures the
REM ephemeral *.trycloudflare.com URL into data\tunnel_url.txt so the
REM dashboard can surface it.
REM
REM Prerequisites:
REM   - cloudflared on PATH (https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)
REM   - tv_webhook running on http://localhost:8090
REM     (py -3.13 -m monitoring.tv_webhook)
REM
REM This script:
REM   1. Starts cloudflared with --url http://localhost:8090
REM   2. Tees stdout to logs\cloudflared.log
REM   3. Polls the log for the trycloudflare.com URL and writes it to
REM      data\tunnel_url.txt the moment it appears (Cloudflare emits it
REM      ~3-5 seconds after launch)
REM
REM Press Ctrl+C to tear down the tunnel — cloudflared exits and the
REM URL stops working.

setlocal
set PROJECT=D:\AI-Workstation\Antigravity\apps\Profit Generation
set LOG=%PROJECT%\logs\cloudflared.log
set URLFILE=%PROJECT%\data\tunnel_url.txt
set PORT=8090

if not exist "%PROJECT%\logs" mkdir "%PROJECT%\logs"
if not exist "%PROJECT%\data" mkdir "%PROJECT%\data"

echo Starting cloudflared quick-tunnel to http://localhost:%PORT% ...
echo Log:    %LOG%
echo URLfile: %URLFILE%
echo Press Ctrl+C to stop.

REM Truncate previous log so the URL parser only sees this run.
break > "%LOG%"
del "%URLFILE%" 2>nul

REM Background watcher: poll the log for the trycloudflare URL.
start "" /b cmd /c "powershell -NoProfile -Command \"do { Start-Sleep -Milliseconds 500; $m = Select-String -Path '%LOG%' -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' -ErrorAction SilentlyContinue ^| Select-Object -First 1; if ($m) { $m.Matches[0].Value ^| Out-File -FilePath '%URLFILE%' -Encoding ASCII -NoNewline; break } } while ($true)\""

REM Run cloudflared in the foreground so Ctrl+C tears it down cleanly.
cloudflared tunnel --url http://localhost:%PORT% --no-autoupdate 1>> "%LOG%" 2>&1

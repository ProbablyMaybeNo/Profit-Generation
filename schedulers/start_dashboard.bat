@echo off
cd /d "D:\AI-Workstation\Antigravity\apps\Profit Generation"
start "Trading Dashboard" cmd /k "conda activate trading && python dashboard/server.py"
timeout /t 3
start http://localhost:8080

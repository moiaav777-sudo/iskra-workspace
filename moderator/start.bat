@echo off
rem Moderator server start (scheduled task ModeratorServer).
rem App logs: logs\server.log, logs\access.log (rotated). console.log gets only crash output.
cd /d C:\Users\user\moderator
if not exist logs mkdir logs
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8765 --log-config log_config.json >> logs\console.log 2>&1

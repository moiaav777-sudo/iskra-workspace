@echo off
rem Запуск «Брифа» за порталом (задача BriefServer). Данные и .env (PORTAL_SECRET) — в C:\Users\user\brief.
chcp 65001 >nul
cd /d C:\Users\user\brief
if not exist logs mkdir logs
set PYTHONUTF8=1
set BRIEF_HOME=C:\Users\user\brief
set BRIEF_PORT=8931
set BRIEF_NO_BROWSER=1
"C:\Program Files\Python312\python.exe" app\server.py --no-browser >> logs\console.log 2>&1

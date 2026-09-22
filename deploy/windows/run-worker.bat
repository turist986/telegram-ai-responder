@echo off
chcp 65001 >nul
REM Запускает воркера (ответы в Telegram) для фоновой задачи (см. install-services.ps1).
cd /d "%~dp0..\.."
if not exist "data" mkdir "data"
venv\Scripts\python.exe run_worker.py >> data\service-worker.log 2>&1

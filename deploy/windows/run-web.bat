@echo off
chcp 65001 >nul
REM Запускает веб-панель для фоновой задачи (см. install-services.ps1).
REM %~dp0 = deploy\windows\, поднимаемся на два уровня — в корень проекта.
cd /d "%~dp0..\.."
if not exist "data" mkdir "data"
venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 >> data\service-web.log 2>&1

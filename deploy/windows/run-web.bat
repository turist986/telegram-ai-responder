@echo off
chcp 65001 >nul
REM Запускает веб-панель для фоновой задачи (см. install-services.ps1).
REM %~dp0 = deploy\windows\, поднимаемся на два уровня — в корень проекта.
cd /d "%~dp0..\.."
if not exist "data" mkdir "data"
REM --timeout-keep-alive 130: у uvicorn по умолчанию 5 с, у Caddy соединение к панели простаивает до
REM 2 минут и переиспользуется — панель закрывала его ровно в момент, когда Caddy слал по нему запрос,
REM и на странице появлялся случайный 502. Значение должно быть БОЛЬШЕ времени простоя у прокси.
venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --timeout-keep-alive 130 >> data\service-web.log 2>&1

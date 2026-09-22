@echo off
chcp 65001 >nul
rem ^ кодовая страница консоли на UTF-8 — иначе кириллица ниже отображается кракозябрами
rem Запускает сайт (панель) и воркер (ответы в Telegram).
rem Освободить аккаунты для Telegram Desktop: кнопка «Освободить для Desktop» в панели (сайт остаётся включённым).
cd /d "%~dp0"
start "AI Responder - site" /min python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
timeout /t 4 /nobreak >nul
start "AI Responder - worker" /min python run_worker.py
echo Запущено: панель http://localhost:8000
timeout /t 3 /nobreak >nul

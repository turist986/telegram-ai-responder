@echo off
setlocal
cd /d "%~dp0"

REM Этот файл запускает веб-панель локально на Windows.
REM НЕ открывайте файлы из app\templates\ напрямую в браузере — это
REM серверные шаблоны, они рендерятся только через этот запущенный сервер.

if not exist ".env" (
    echo [!] Файл .env не найден.
    echo     Копирую .env.example -^> .env
    copy /Y ".env.example" ".env" >nul
    echo.
    echo     ВАЖНО: откройте .env ^(он сейчас откроется в блокноте^) и впишите
    echo     реальные значения: TELEGRAM_API_ID, TELEGRAM_API_HASH,
    echo     DEEPSEEK_API_KEY, ADMIN_PASSWORD_HASH, SECRET_KEY.
    echo     Хэш пароля: venv\Scripts\python.exe scripts\hash_password.py
    echo     Подробности — README.md, раздел "Установка".
    echo.
    notepad ".env"
    echo Сохраните .env и запустите start_local.bat ещё раз.
    pause
    exit /b 1
)

if not exist "venv\Scripts\python.exe" (
    echo [*] Первый запуск: создаю виртуальное окружение и ставлю зависимости...
    python -m venv venv
    if errorlevel 1 (
        echo [!] Не удалось создать venv. Убедитесь, что Python установлен и есть в PATH.
        pause
        exit /b 1
    )
    call venv\Scripts\activate.bat
    pip install -r requirements.txt
) else (
    call venv\Scripts\activate.bat
)

echo.
echo [*] Запускаю сервер на http://localhost:8000
echo     Окно должно оставаться открытым, пока вы пользуетесь панелью.
echo     Чтобы остановить сервер — закройте это окно или нажмите Ctrl+C.
echo.

start "" cmd /c "timeout /t 2 >nul & start http://localhost:8000"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

pause

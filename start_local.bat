@echo off
chcp 65001 >nul
REM ^ кодовая страница консоли на UTF-8 — иначе кириллица ниже отображается кракозябрами
setlocal
cd /d "%~dp0"

REM Этот файл поднимает ВЕСЬ сайт локально на Windows: и веб-панель, и
REM воркера (ответы в Telegram) — двумя отдельными окнами.
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
    echo [*] Первый запуск: создаю виртуальное окружение...
    python -m venv venv
    if errorlevel 1 (
        echo [!] Не удалось создать venv. Убедитесь, что Python установлен и есть в PATH.
        pause
        exit /b 1
    )
)

REM Зависимости сверяем при КАЖДОМ запуске: после git pull в requirements.txt
REM могли появиться новые пакеты, а без них сайт падает при старте. Если всё уже
REM установлено — команда отрабатывает за пару секунд.
echo [*] Проверяю зависимости...
venv\Scripts\python.exe -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
    echo [!] Не удалось установить зависимости из requirements.txt — см. сообщения выше.
    pause
    exit /b 1
)

REM Импорт TData: opentele ставится отдельным скриптом (без компилятора C++).
venv\Scripts\python.exe -c "import importlib.util,sys;sys.exit(0 if importlib.util.find_spec('opentele') else 1)"
if errorlevel 1 (
    echo [*] Ставлю зависимости импорта TData ^(один раз^)...
    venv\Scripts\python.exe scripts\install_tdata_deps.py
    if errorlevel 1 echo [!] Импорт TData пока недоступен ^(остальное работает^). Повторить: venv\Scripts\python.exe scripts\install_tdata_deps.py
)

echo.
echo [*] Запускаю сайт: веб-панель + воркер (ответы в Telegram)
echo     Откроются ДВА окна — «AI Responder - site» и «AI Responder - worker».
echo     Не закрывайте их, пока пользуетесь системой: закрытие окна = остановка
echo     этой части (панели или воркера). Если что-то упало — окно останется
echo     открытым с текстом ошибки (не закроется само).
echo.

REM Через venv\Scripts\python.exe явно (не просто "python") — каждое окно это
REM отдельный процесс, который не наследует activate.bat из этого скрипта.
start "AI Responder - site" cmd /k venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
timeout /t 4 /nobreak >nul
start "AI Responder - worker" cmd /k venv\Scripts\python.exe run_worker.py

REM update.bat запускает этот файл с аргументом nobrowser — без открытия браузера
if /i not "%~1"=="nobrowser" start "" cmd /c "timeout /t 2 >nul & start http://localhost:8000"

echo Запущено: панель http://localhost:8000
timeout /t 3 /nobreak >nul

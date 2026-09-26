@echo off
chcp 65001 >nul
REM Обновление проекта одной командой: git pull -> зависимости -> перезапуск -> проверка.
REM Запускайте из папки проекта (на VPS лучше от имени администратора). Подробности и
REM параметры (-Force, -NoRestart) — в deploy\windows\update.ps1.
cd /d "%~dp0"
REM Вызов и выход — в ОДНОЙ строке: cmd читает .bat по частям, а git pull может заменить
REM этот самый файл посреди выполнения; строка уже прочитана целиком, продолжения после неё нет.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\windows\update.ps1" %* & exit /b

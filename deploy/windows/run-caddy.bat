@echo off
chcp 65001 >nul
REM Запускает Caddy (реверс-прокси + HTTPS) для фоновой задачи (см. install-services.ps1).
REM Требует, чтобы caddy.exe был в PATH (winget install CaddyServer.Caddy) —
REM иначе замените "caddy" ниже на полный путь к caddy.exe.
cd /d "%~dp0..\.."
if not exist "data" mkdir "data"
caddy run --config deploy\Caddyfile --adapter caddyfile >> data\service-caddy.log 2>&1

<#
.SYNOPSIS
    Регистрирует веб-панель, воркера и Caddy как фоновые задачи Планировщика
    заданий Windows: запускаются при старте системы, перезапускаются при сбое,
    работают без открытого окна консоли. Никаких сторонних инструментов
    (NSSM и т.п.) не требуется — только штатный Планировщик заданий Windows.

.DESCRIPTION
    Запускать ОДИН РАЗ, от имени администратора, из корня проекта — после того как:
      1) venv создан и зависимости установлены:
           python -m venv venv
           venv\Scripts\pip install -r requirements.txt
      2) .env заполнен (см. README.md, раздел "Установка")
      3) Caddy установлен и в PATH:
           winget install CaddyServer.Caddy
         (после установки откройте новое окно PowerShell, чтобы PATH обновился)
      4) deploy\Caddyfile отредактирован — свой домен и свой путь к проекту

.EXAMPLE
    cd C:\ai-responder
    .\deploy\windows\install-services.ps1
#>
param(
    [string]$ProjectDir = (Get-Location).Path
)

$ErrorActionPreference = "Stop"

$pythonExe = Join-Path $ProjectDir "venv\Scripts\python.exe"
if (-not (Test-Path $pythonExe)) {
    Write-Error "Не найден venv\Scripts\python.exe в $ProjectDir. Сначала создайте venv и поставьте зависимости (см. README.md)."
}
if (-not (Test-Path (Join-Path $ProjectDir ".env"))) {
    Write-Warning ".env не найден — веб-панель и воркер не запустятся без него. Заполните его перед стартом задач."
}
if (-not (Get-Command caddy -ErrorAction SilentlyContinue)) {
    Write-Warning "caddy.exe не найден в PATH. Установите: winget install CaddyServer.Caddy — и откройте новое окно PowerShell."
}

function Register-AIResponderTask {
    param([string]$Name, [string]$BatPath, [string]$Description)

    if (-not (Test-Path $BatPath)) {
        Write-Error "Не найден $BatPath"
    }
    if (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
    }

    $action = New-ScheduledTaskAction -Execute $BatPath -WorkingDirectory $ProjectDir
    $trigger = New-ScheduledTaskTrigger -AtStartup
    # ExecutionTimeLimit = 0 обязателен: по умолчанию Планировщик сам убивает
    # задачу через 3 дня работы — для постоянно работающего сервиса это не годится.
    $settings = New-ScheduledTaskSettingsSet `
        -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable
    # SYSTEM: без пароля, полные права, стандартный выбор для выделенного VPS
    # под одну задачу. Если нужен менее привилегированный пользователь — меняйте
    # вручную через Планировщик заданий (там же можно ввести пароль интерактивно).
    $principal = New-ScheduledTaskPrincipal -UserId "NT AUTHORITY\SYSTEM" -LogonType ServiceAccount -RunLevel Highest

    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Description $Description | Out-Null
    Start-ScheduledTask -TaskName $Name
    Write-Host "[+] $Name зарегистрирован и запущен"
}

Register-AIResponderTask -Name "AIResponderWeb" `
    -BatPath (Join-Path $ProjectDir "deploy\windows\run-web.bat") `
    -Description "AI Auto-Responder: веб-панель (uvicorn, 127.0.0.1:8000)"

Register-AIResponderTask -Name "AIResponderWorker" `
    -BatPath (Join-Path $ProjectDir "deploy\windows\run-worker.bat") `
    -Description "AI Auto-Responder: воркер Telegram (Telethon)"

Register-AIResponderTask -Name "AIResponderCaddy" `
    -BatPath (Join-Path $ProjectDir "deploy\windows\run-caddy.bat") `
    -Description "AI Auto-Responder: Caddy (реверс-прокси + автоматический HTTPS)"

New-NetFirewallRule -DisplayName "AI Responder HTTP" -Direction Inbound -Protocol TCP -LocalPort 80 -Action Allow -ErrorAction SilentlyContinue | Out-Null
New-NetFirewallRule -DisplayName "AI Responder HTTPS" -Direction Inbound -Protocol TCP -LocalPort 443 -Action Allow -ErrorAction SilentlyContinue | Out-Null

Write-Host ""
Write-Host "Готово."
Write-Host "Статус задач:   Get-ScheduledTask -TaskName AIResponderWeb,AIResponderWorker,AIResponderCaddy | Get-ScheduledTaskInfo"
Write-Host "Логи:           data\service-web.log, data\service-worker.log, data\service-caddy.log"
Write-Host "Остановить всё: Stop-ScheduledTask -TaskName AIResponderWeb,AIResponderWorker,AIResponderCaddy"
Write-Host "Удалить всё:    Unregister-ScheduledTask -TaskName AIResponderWeb,AIResponderWorker,AIResponderCaddy -Confirm:`$false"

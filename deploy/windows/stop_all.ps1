<#
.SYNOPSIS
    Аварийная остановка: гарантированно останавливает панель И воркера, даже когда сайт
    не открывается (завис, 502) и нажать кнопку "Остановить воркер" в самой панели уже
    нельзя. Не зависит от того, работает сайт или нет — просто ищет и убивает процессы.

.DESCRIPTION
    Обычные способы остановки (тумблер "Глобальный автоответчик", кнопка "Остановить
    воркер") живут В САМОЙ панели — если панель зависла или упала, ими нельзя
    воспользоваться, а воркер как отдельный процесс продолжает вести диалоги в Telegram
    независимо от панели. Этот скрипт не заходит на сайт вообще: работает напрямую с
    процессами Windows.

    Запуск (из папки проекта):
        stop_all.bat

    Что делает:
      1) Если панель/воркер зарегистрированы фоновыми задачами Планировщика
         (AIResponderWeb/AIResponderWorker, см. install-services.ps1) — останавливает и их.
      2) Находит и убивает процессы Python ЭТОГО проекта: тот, что запущен из
         venv\Scripts\python.exe, и любой другой, в командной строке которого встречается
         путь до этой папки (например, если раньше пользовались системным python вместо
         venv). Не трогает python других программ на этой же машине.
      3) Проверяет, что панель (127.0.0.1:8000) действительно перестала отвечать.

    -All — снести ВСЕ процессы python.exe/pythonw.exe на этой машине, а не только этого
    проекта. Нужен на выделенном VPS, где кроме бота ничего на Python не работает, а
    обычный (по пути) поиск почему-то не находит все процессы. НЕ используйте на машине,
    где Python нужен для чего-то ещё, — остановит и это тоже.

.EXAMPLE
    .\stop_all.bat
.EXAMPLE
    .\stop_all.bat -All
#>
param(
    [string]$ProjectDir = (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)),
    [switch]$All
)

$ErrorActionPreference = "Stop"

function Say($text, $color = "Gray") { Write-Host $text -ForegroundColor $color }

Say ""
Say "=== Аварийная остановка AI Auto-Responder ===" "Cyan"
Say "Папка проекта: $ProjectDir"
Say ""

# --- 1) Фоновые задачи Планировщика, если сайт зарегистрирован как служба
$taskNames = @("AIResponderWeb", "AIResponderWorker")
$foundTasks = @($taskNames | Where-Object { Get-ScheduledTask -TaskName $_ -ErrorAction SilentlyContinue })
if ($foundTasks.Count -gt 0) {
    Say "[1/3] Останавливаю фоновые задачи..." "Cyan"
    foreach ($t in $foundTasks) {
        try {
            Stop-ScheduledTask -TaskName $t -ErrorAction Stop
            Say "    остановлена: $t" "Green"
        } catch {
            Say "    не удалось остановить $t (нужны права администратора?): $($_.Exception.Message)" "Yellow"
        }
    }
} else {
    Say "[1/3] Фоновых задач не найдено (сайт запущен вручную/через start_local.bat)."
}
Say ""

# --- 2) Сами процессы python
Say "[2/3] Ищу процессы Python..." "Cyan"
$venvPrefix = (Join-Path $ProjectDir "venv").TrimEnd('\') + "\"
$pythonProcs = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^python(w)?\.exe$' })

if ($All) {
    $targets = $pythonProcs
    Say "    режим -All: ВСЕ python-процессы на этой машине ($($targets.Count))" "Yellow"
} else {
    $targets = @($pythonProcs | Where-Object {
        ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($venvPrefix, [StringComparison]::OrdinalIgnoreCase)) -or
        ($_.CommandLine -and $_.CommandLine.IndexOf($ProjectDir, [StringComparison]::OrdinalIgnoreCase) -ge 0)
    })
    Say "    процессы этого проекта: $($targets.Count) (всего python-процессов на машине: $($pythonProcs.Count))"
}

if ($targets.Count -eq 0) {
    Say "    ни одного не найдено — похоже, уже остановлено." "Green"
} else {
    foreach ($p in $targets) {
        $cmd = if ($p.CommandLine) { $p.CommandLine.Substring(0, [Math]::Min(110, $p.CommandLine.Length)) } else { $p.Name }
        try {
            & taskkill.exe /PID $p.ProcessId /T /F 2>&1 | Out-Null
            Say "    остановлен PID $($p.ProcessId): $cmd" "Green"
        } catch {
            Say "    не удалось остановить PID $($p.ProcessId): $($_.Exception.Message)" "Red"
        }
    }
}
Say ""

# --- 3) Проверка
Say "[3/3] Проверяю, что панель действительно не отвечает..." "Cyan"
Start-Sleep -Seconds 1
try {
    Invoke-WebRequest "http://127.0.0.1:8000/login" -UseBasicParsing -TimeoutSec 3 | Out-Null
    Say "    панель ВСЁ ЕЩЁ отвечает на 127.0.0.1:8000 — что-то не остановилось." "Red"
    Say "    Повторите со флагом -All: .\stop_all.bat -All" "Yellow"
} catch {
    Say "    панель не отвечает — панель и воркер остановлены." "Green"
}

Say ""
Say "Запустить заново: .\start_local.bat" "Cyan"
Say "(или, если сайт зарегистрирован как служба: Start-ScheduledTask -TaskName AIResponderWeb,AIResponderWorker)"
Say ""

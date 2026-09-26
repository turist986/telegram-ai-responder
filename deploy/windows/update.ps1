<#
.SYNOPSIS
    Обновление проекта на Windows VPS одной командой: git pull -> зависимости ->
    перезапуск сайта и воркера -> проверка, что сайт поднялся.

.DESCRIPTION
    Запуск (из папки проекта, лучше от имени администратора — иначе задачи Планировщика
    не перезапустить):
        update.bat

    Что делает:
      1) git pull --ff-only --autostash. Локальные правки отслеживаемых файлов (например,
         свой домен в deploy\Caddyfile) сохраняются и накладываются обратно — обычный
         git pull на таких файлах падает. Файлы вне git (.env, data\, venv) не трогаются.
      2) pip install -r requirements.txt (после обновления могли появиться новые пакеты).
      3) Если нет opentele — ставит зависимости импорта TData (scripts\install_tdata_deps.py).
      4) Перезапускает задачи AIResponderWeb / AIResponderWorker (см. install-services.ps1),
         а если изменился deploy\Caddyfile — и AIResponderCaddy. Если задач нет (запуск через
         start_local.bat) — подсказывает, что перезапустить вручную.
      5) Ждёт, пока панель ответит на http://127.0.0.1:8000/login; при неудаче показывает
         конец data\service-web.log.

    Если сам скрипт обновления изменился при git pull, он перезапускает себя уже новой версией.

.PARAMETER Force
    Не останавливаться на «уже последняя версия»: переустановить зависимости и перезапустить.
.PARAMETER NoRestart
    Только обновить код и зависимости, ничего не перезапускать.
#>
param(
    [string]$ProjectDir = (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)),
    [switch]$Force,
    [switch]$NoRestart,
    [switch]$SkipPull,        # служебный: после самообновления скрипта
    [string]$FromCommit = ""  # служебный: с какого коммита считать список изменений
)

$ErrorActionPreference = "Stop"
$WebTasks = @("AIResponderWeb", "AIResponderWorker")
$CaddyTask = "AIResponderCaddy"
$HealthUrl = "http://127.0.0.1:8000/login"


function Say($text, $color = "Gray") { Write-Host $text -ForegroundColor $color }

function Invoke-Native {
    # Нативные команды пишут прогресс в stderr; при $ErrorActionPreference=Stop PowerShell 5.1
    # превращает это в исключение. Здесь важен только код возврата.
    param([string]$Exe, [string[]]$Arguments, [switch]$Quiet)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $lines = @(& $Exe @Arguments 2>&1 | ForEach-Object { "$_" } |
            Where-Object { $_ -ne "System.Management.Automation.RemoteException" -and $_ -notmatch '^\[notice\]' })
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
    if (-not $Quiet) { $lines | ForEach-Object { if ($_.Trim()) { Say "    $_" } } }
    return @{ Code = $code; Output = $lines }
}

function Find-Git {
    $cmd = Get-Command git -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($p in @("C:\Program Files\Git\cmd\git.exe", "C:\Program Files (x86)\Git\cmd\git.exe")) {
        if (Test-Path $p) { return $p }
    }
    throw "Git не найден. Установите: winget install Git.Git (и откройте новое окно)."
}

function Test-IsAdmin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Stop-ProjectProcesses {
    # Страховка после Stop-ScheduledTask: процессы venv этого проекта (uvicorn / run_worker),
    # которые могли пережить остановку задачи, иначе новый экземпляр не займёт порт 8000.
    # Ищем по пути python.exe внутри ЭТОЙ папки — чужие python не трогаем. venv-python на
    # Windows запускает настоящий интерпретатор дочерним процессом, поэтому убиваем дерево.
    param([string]$Dir)
    $venv = (Join-Path $Dir "venv").TrimEnd('\') + "\"
    $found = @(Get-CimInstance Win32_Process | Where-Object {
        $_.ProcessId -ne $PID -and $_.ExecutablePath -and $_.ExecutablePath.StartsWith($venv, [StringComparison]::OrdinalIgnoreCase) `
            -and $_.CommandLine -match 'uvicorn|run_worker\.py'
    })
    foreach ($p in $found) { & taskkill.exe /PID $p.ProcessId /T /F 2>&1 | Out-Null }
    return $found.Count
}

function Wait-Panel([int]$Seconds = 40) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -Uri $HealthUrl -UseBasicParsing -TimeoutSec 3
            if ($r.StatusCode -eq 200) { return $true }
        } catch { }
        Start-Sleep -Seconds 2
    }
    return $false
}

function Update-Project {
    $py = Join-Path $ProjectDir "venv\Scripts\python.exe"
    if (-not (Test-Path $py)) { throw "Не найден $py — сначала выполните установку (см. README.md, раздел 'Деплой на VPS под Windows')." }
    $git = Find-Git
    Set-Location $ProjectDir
    if (-not (Test-Path (Join-Path $ProjectDir ".git"))) { throw "$ProjectDir — не git-репозиторий (нет папки .git)." }

    $old = $FromCommit
    $changed = @()

    if (-not $SkipPull) {
        Say "[1/4] Получаю обновления (git pull)..." "Cyan"
        $old = (Invoke-Native $git @("rev-parse", "HEAD") -Quiet).Output | Select-Object -First 1
        $pull = Invoke-Native $git @("pull", "--ff-only", "--autostash")
        if ($pull.Code -ne 0) {
            Say "" ; Say "git pull не удался (код $($pull.Code))." "Red"
            Say "Частые причины: нет доступа к GitHub; локальная ветка разошлась с удалённой (правили код на сервере и коммитили);" "Yellow"
            Say "локальные правки конфликтуют с обновлением (тогда они сохранены: git stash list)." "Yellow"
            throw "Обновление остановлено — ничего не перезапущено."
        }
    } else {
        Say "[1/4] Скрипт обновления сам обновился — продолжаю новой версией." "Cyan"
    }

    $new = (Invoke-Native $git @("rev-parse", "HEAD") -Quiet).Output | Select-Object -First 1
    if ($old -and $old -ne $new) {
        $changed = @((Invoke-Native $git @("diff", "--name-only", $old, $new) -Quiet).Output | Where-Object { $_.Trim() })
        Say "    Обновлено: $($old.Substring(0,7)) -> $($new.Substring(0,7)), изменено файлов: $($changed.Count)" "Green"
        (Invoke-Native $git @("log", "--oneline", "$old..$new") -Quiet).Output | Select-Object -First 15 | ForEach-Object { Say "      $_" }
    } elseif (-not $SkipPull) {
        Say "    Уже последняя версия ($($new.Substring(0,7)))." "Green"
        if (-not $Force) { Say "Обновлять нечего. Чтобы всё равно переустановить зависимости и перезапустить: update.bat -Force"; return }
    }

    # Сам скрипт обновления изменился — дальше выполняем его новую версию (текущий процесс уже
    # прочитал старую в память), передав, с какого коммита считать изменения.
    if (-not $SkipPull -and ($changed -contains "deploy/windows/update.ps1")) {
        Say "    Скрипт обновления изменился — перезапускаю его новой версией..." "Yellow"
        $self = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $PSCommandPath, "-ProjectDir", $ProjectDir, "-SkipPull", "-FromCommit", $old)
        if ($Force) { $self += "-Force" }
        if ($NoRestart) { $self += "-NoRestart" }
        & powershell.exe @self
        exit $LASTEXITCODE
    }

    Say "[2/4] Проверяю зависимости (requirements.txt)..." "Cyan"
    $pip = Invoke-Native $py @("-m", "pip", "install", "-q", "--disable-pip-version-check", "-r", "requirements.txt")
    if ($pip.Code -ne 0) { throw "Не удалось установить зависимости (см. сообщения выше). Сайт не перезапущен." }

    Say "[3/4] Проверяю зависимости импорта TData..." "Cyan"
    $has = Invoke-Native $py @("-c", "import importlib.util,sys;sys.exit(0 if importlib.util.find_spec('opentele') else 1)") -Quiet
    if ($has.Code -ne 0) {
        $inst = Invoke-Native $py @("scripts\install_tdata_deps.py")
        if ($inst.Code -ne 0) { Say "    Импорт TData пока недоступен (остальное работает). Повторить: venv\Scripts\python.exe scripts\install_tdata_deps.py" "Yellow" }
    } else { Say "    ок" }

    if ($NoRestart) { Say "[4/4] Перезапуск пропущен (-NoRestart)." "Yellow"; return }

    Say "[4/4] Перезапускаю сайт и воркера..." "Cyan"
    $registered = @($WebTasks | Where-Object { Get-ScheduledTask -TaskName $_ -ErrorAction SilentlyContinue })
    if ($registered.Count -eq 0) {
        Say "    Фоновые задачи не найдены (сайт запущен через start_local.bat или вручную)." "Yellow"
        Say "    Закройте окна 'AI Responder - site' и 'AI Responder - worker' и запустите start_local.bat заново." "Yellow"
        return
    }
    if (-not (Test-IsAdmin)) {
        throw "Для перезапуска задач нужны права администратора: откройте PowerShell/cmd 'От имени администратора' и повторите update.bat. Код и зависимости уже обновлены."
    }
    $toRestart = @($registered)
    if ($changed -contains "deploy/Caddyfile" -and (Get-ScheduledTask -TaskName $CaddyTask -ErrorAction SilentlyContinue)) {
        $toRestart += $CaddyTask
        Say "    Изменился deploy\Caddyfile — перезапускаю и Caddy." "Yellow"
    }
    foreach ($t in $toRestart) { Stop-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
    $killed = Stop-ProjectProcesses -Dir $ProjectDir
    if ($killed) { Say "    Добито зависших процессов: $killed" }
    foreach ($t in $toRestart) { Start-ScheduledTask -TaskName $t; Say "    запущен: $t" }

    Say "    Жду, пока панель ответит на $HealthUrl ..."
    if (Wait-Panel) {
        Say ""; Say "Готово: сайт и воркер обновлены и работают." "Green"
    } else {
        Say ""; Say "Панель не ответила за 40 секунд. Последние строки лога:" "Red"
        $log = Join-Path $ProjectDir "data\service-web.log"
        if (Test-Path $log) { Get-Content $log -Tail 15 | ForEach-Object { Say "    $_" } }
        throw "Сайт не поднялся после обновления."
    }
}

# Точка входа. Если скрипт подключён через точку (". update.ps1") — только определения функций, без запуска
# (так его функции проверяются в тестах).
if ($MyInvocation.InvocationName -ne ".") {
    try {
        Update-Project
    } catch {
        Say ""; Say "ОШИБКА: $($_.Exception.Message)" "Red"
        exit 1
    }
}

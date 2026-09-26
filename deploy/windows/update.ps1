<#
.SYNOPSIS
    One-command update of the Windows server: git pull -> dependencies -> restart site + worker ->
    verify that the NEW code is really running.

.DESCRIPTION
    Run from the project folder (as Administrator when the site runs as scheduled tasks):
        update.bat

    Output is ASCII/English on purpose (Cyrillic turns into '?' in some server consoles).
    The full log (UTF-8) is written to data\update.log - send that file if something fails.

    1) git pull --ff-only --autostash  (local edits of tracked files, e.g. your domain in
       deploy\Caddyfile, are kept; .env, data\ and venv are outside git and never touched).
    2) pip install -r requirements.txt; TData dependencies if opentele is missing.
    3) Restart the site and the worker:
         - scheduled tasks AIResponderWeb / AIResponderWorker (+ AIResponderCaddy when
           deploy\Caddyfile changed) if they are registered (needs Administrator), otherwise
         - the "AI Responder" console windows started by start_local.bat: they are closed and
           start_local.bat is launched again.
    4) Ask http://127.0.0.1:PORT/version and compare it with the git commit on disk. If the
       running version is different (old process still holds the port) the process on the port
       is stopped and the site is started again. The script only says "DONE" when the running
       version equals the commit on disk.

    "Already up to date" is also checked against the running version: pulled by hand but not
    restarted? The script notices that and restarts.

.PARAMETER Force
    Reinstall dependencies and restart even if the running version is already current.
.PARAMETER NoRestart
    Update code and dependencies only.
.PARAMETER Port
    Port of the panel (default 8000).
#>
param(
    [string]$ProjectDir = (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)),
    [switch]$Force,
    [switch]$NoRestart,
    [int]$Port = 8000,
    [switch]$SkipPull,        # internal: after the script replaced itself
    [string]$FromCommit = ""  # internal: commit to diff from after self-update
)

$ErrorActionPreference = "Stop"
$WebTasks = @("AIResponderWeb", "AIResponderWorker")
$CaddyTask = "AIResponderCaddy"
$LogFile = Join-Path $ProjectDir "data\update.log"


function Say($text, $color = "Gray") {
    Write-Host $text -ForegroundColor $color
    try {
        $dir = Split-Path $LogFile
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force $dir | Out-Null }
        Add-Content -Path $LogFile -Value ("{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $text) -Encoding UTF8
    } catch { }
}

function Invoke-Native {
    # Native tools write progress to stderr; with $ErrorActionPreference=Stop Windows PowerShell 5.1
    # turns that into an exception. Only the exit code matters here.
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
    throw "Git not found. Install it (winget install Git.Git) and open a new window."
}

function Test-IsAdmin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Stop-Tree([int]$ProcessId) {
    & taskkill.exe /PID $ProcessId /T /F 2>&1 | Out-Null
}

function Stop-ProjectProcesses {
    # Stops uvicorn / run_worker.py of THIS project (also the console windows started by
    # start_local.bat) so that a new instance can take the port. Other python programs are left alone.
    # A venv python on Windows is a launcher that starts the real interpreter as a child, so whole
    # process trees are killed.
    param([string]$Dir)
    $venv = (Join-Path $Dir "venv").TrimEnd('\') + "\"
    $all = @(Get-CimInstance Win32_Process)
    $mine = @($all | Where-Object {
        $_.ProcessId -ne $PID -and $_.CommandLine -match 'uvicorn|run_worker\.py' -and (
            ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($venv, [StringComparison]::OrdinalIgnoreCase)) -or
            $_.CommandLine.IndexOf($Dir, [StringComparison]::OrdinalIgnoreCase) -ge 0)
    })
    $done = @{}
    foreach ($p in $mine) {
        $target = $p.ProcessId
        $parent = $all | Where-Object { $_.ProcessId -eq $p.ParentProcessId } | Select-Object -First 1
        if ($parent -and $parent.Name -eq "cmd.exe" -and $parent.CommandLine -match 'uvicorn|run_worker\.py') { $target = $parent.ProcessId }
        if (-not $done.ContainsKey($target)) { Stop-Tree $target; $done[$target] = $true }
    }
    # Empty leftover windows ("cmd /k venv\Scripts\python.exe ..." whose python already died) - close them too,
    # otherwise every update piles up dead windows.
    $windows = @($all | Where-Object { $_.Name -eq "cmd.exe" -and $_.CommandLine -match '/k\s+venv\\Scripts\\python\.exe\s+(-m uvicorn|run_worker)' })
    foreach ($w in $windows) {
        # conhost.exe is a child of every console window - only a python child means the window is alive
        $hasChild = @($all | Where-Object { $_.ParentProcessId -eq $w.ProcessId -and $_.Name -match 'python' }).Count -gt 0
        if (-not $hasChild -and -not $done.ContainsKey($w.ProcessId)) { Stop-Tree $w.ProcessId; $done[$w.ProcessId] = $true }
    }
    return $done.Count
}

function Stop-PortListener([int]$LocalPort) {
    # Last resort: whatever python/uvicorn of our app still holds the panel port.
    $n = 0
    $conns = @(Get-NetTCPConnection -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue)
    foreach ($c in $conns) {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$($c.OwningProcess)" -ErrorAction SilentlyContinue
        if ($proc) {
            Say ("    port {0} is held by PID {1}: {2}" -f $LocalPort, $proc.ProcessId, $proc.CommandLine)
            if ($proc.CommandLine -match 'uvicorn' -and $proc.CommandLine -match 'app\.main') { Stop-Tree $proc.ProcessId; $n++ }
        }
    }
    return $n
}

function Get-RunningVersion {
    # -> @{ Ok = <bool>; Value = <string to show> }
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/version" -UseBasicParsing -TimeoutSec 3
        return @{ Ok = $true; Value = ([string]$r.Content).Trim() }
    } catch {
        $resp = $_.Exception.Response
        if ($resp) { return @{ Ok = $false; Value = "HTTP $([int]$resp.StatusCode) (old code without /version, or a different program)" } }
        return @{ Ok = $false; Value = "no answer" }
    }
}

function Wait-Version([string]$Expected, [int]$Seconds = 45) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    $seen = @{ Ok = $false; Value = "no answer" }
    while ((Get-Date) -lt $deadline) {
        $seen = Get-RunningVersion
        if ($seen.Ok -and $seen.Value -eq $Expected) { return @{ Ok = $true; Value = $seen.Value } }
        Start-Sleep -Seconds 2
    }
    return @{ Ok = $false; Value = $seen.Value }
}

function Start-Site([string[]]$Tasks) {
    if ($Tasks.Count -gt 0) {
        foreach ($t in $Tasks) { Start-ScheduledTask -TaskName $t; Say "    started task: $t" }
    } else {
        Say "    starting start_local.bat (two console windows: site + worker)"
        # ".\" is required: cmd does not always search the current folder (NoDefaultCurrentDirectoryInExePath)
        Start-Process -FilePath "cmd.exe" -ArgumentList @("/c", ".\start_local.bat", "nobrowser") -WorkingDirectory $ProjectDir
    }
}

function Restart-Site([string]$Expected, [string[]]$Changed) {
    $registered = @($WebTasks | Where-Object { Get-ScheduledTask -TaskName $_ -ErrorAction SilentlyContinue })
    $tasks = @($registered)
    if ($registered.Count -gt 0) {
        if (-not (Test-IsAdmin)) {
            throw "The site runs as scheduled tasks; restarting them needs Administrator. Open PowerShell/cmd 'as Administrator' and run update.bat again (code and dependencies are already updated)."
        }
        if ($Changed -contains "deploy/Caddyfile" -and (Get-ScheduledTask -TaskName $CaddyTask -ErrorAction SilentlyContinue)) {
            $tasks += $CaddyTask
            Say "    deploy\Caddyfile changed - Caddy will be restarted too" "Yellow"
        }
        foreach ($t in $tasks) { Stop-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue }
        Start-Sleep -Seconds 2
        Say "    mode: scheduled tasks"
    } else {
        Say "    mode: console windows (start_local.bat)"
    }

    $killed = Stop-ProjectProcesses -Dir $ProjectDir
    if ($killed) { Say "    stopped old processes/windows: $killed" }
    Start-Sleep -Seconds 1
    Start-Site $tasks

    Say "    waiting for the panel to report version $Expected ..."
    $v = Wait-Version $Expected
    if ($v.Ok) { return $v }

    Say "    running version is '$($v.Value)', expected '$Expected' - an old process may still hold port $Port" "Yellow"
    $n = Stop-PortListener $Port
    $n += Stop-ProjectProcesses -Dir $ProjectDir
    if ($n -gt 0) {
        Start-Sleep -Seconds 2
        Start-Site $tasks
        $v = Wait-Version $Expected
    }
    return $v
}

function Update-Project {
    $py = Join-Path $ProjectDir "venv\Scripts\python.exe"
    if (-not (Test-Path $py)) { throw "$py not found - run start_local.bat once (creates venv) or follow README.md (Windows VPS deployment)." }
    $git = Find-Git
    Set-Location $ProjectDir
    if (-not (Test-Path (Join-Path $ProjectDir ".git"))) { throw "$ProjectDir is not a git clone (no .git folder)." }
    Say ""
    Say "=== update started $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss'); log: $LogFile ==="

    $old = $FromCommit
    $changed = @()

    if (-not $SkipPull) {
        Say "[1/5] git pull ..." "Cyan"
        $old = (Invoke-Native $git @("rev-parse", "HEAD") -Quiet).Output | Select-Object -First 1
        $pull = Invoke-Native $git @("pull", "--ff-only", "--autostash")
        if ($pull.Code -ne 0) {
            Say ""
            Say "git pull FAILED (exit code $($pull.Code))." "Red"
            Say "Usual causes: no access to GitHub; local branch diverged (commits made on the server); local edits conflict with the update (they are saved: git stash list)." "Yellow"
            throw "Update stopped - nothing was restarted."
        }
    } else {
        Say "[1/5] update script was replaced by git pull - continuing with the new version." "Cyan"
    }

    $new = (Invoke-Native $git @("rev-parse", "HEAD") -Quiet).Output | Select-Object -First 1
    $expected = $new.Substring(0, 7)
    $pulled = ($old -and $old -ne $new)
    if ($pulled) {
        $changed = @((Invoke-Native $git @("diff", "--name-only", $old, $new) -Quiet).Output | Where-Object { $_.Trim() })
        Say "    updated: $($old.Substring(0,7)) -> $expected, files changed: $($changed.Count)" "Green"
        (Invoke-Native $git @("log", "--oneline", "$old..$new") -Quiet).Output | Select-Object -First 15 | ForEach-Object { Say "      $_" }
    } elseif (-not $SkipPull) {
        Say "    code on disk is already the latest ($expected)" "Green"
        if (-not $Force -and -not $NoRestart) {
            $run = Get-RunningVersion
            if ($run.Ok -and $run.Value -eq $expected) {
                Say "Running version is $expected too - nothing to do. (update.bat -Force restarts anyway)" "Green"
                return
            }
            Say "    but the RUNNING site reports '$($run.Value)' - it was not restarted after the last pull; restarting now." "Yellow"
        }
    }

    # The update script itself changed: continue with the new file (this process already loaded the old one).
    if (-not $SkipPull -and ($changed -contains "deploy/windows/update.ps1")) {
        Say "    update script changed - re-running it in its new version ..." "Yellow"
        $self = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $PSCommandPath, "-ProjectDir", $ProjectDir, "-Port", "$Port", "-SkipPull", "-FromCommit", $old)
        if ($Force) { $self += "-Force" }
        if ($NoRestart) { $self += "-NoRestart" }
        & powershell.exe @self
        exit $LASTEXITCODE
    }

    Say "[2/5] dependencies (requirements.txt) ..." "Cyan"
    $pip = Invoke-Native $py @("-m", "pip", "install", "-q", "--disable-pip-version-check", "-r", "requirements.txt")
    if ($pip.Code -ne 0) { throw "Could not install dependencies (see messages above). The site was not restarted." }

    Say "[3/5] TData import dependencies ..." "Cyan"
    $has = Invoke-Native $py @("-c", "import importlib.util,sys;sys.exit(0 if importlib.util.find_spec('opentele') else 1)") -Quiet
    if ($has.Code -ne 0) {
        $inst = Invoke-Native $py @("scripts\install_tdata_deps.py")
        if ($inst.Code -ne 0) { Say "    TData import is unavailable for now (the rest works). Retry: venv\Scripts\python.exe scripts\install_tdata_deps.py" "Yellow" }
    } else { Say "    ok" }

    if ($NoRestart) { Say "[4/5] restart skipped (-NoRestart). Restart the site yourself, then check 'version' in the left menu." "Yellow"; return }

    Say "[4/5] restarting site and worker ..." "Cyan"
    $v = Restart-Site $expected $changed

    Say "[5/5] verifying the running version ..." "Cyan"
    if ($v.Ok) {
        Say ""
        Say "DONE: the site and the worker are running the new code (version $expected)." "Green"
        Say "In the panel, the left menu shows 'version $expected' (press Ctrl+F5 once)."
    } else {
        Say ""
        Say "FAILED: the panel reports '$($v.Value)' but the code on disk is $expected." "Red"
        Say "The site is still running OLD code. Send the file $LogFile to the developer." "Red"
        foreach ($f in @("service-web.log")) {
            $log = Join-Path $ProjectDir "data\$f"
            if (Test-Path $log) { Say "--- tail of data\$f"; Get-Content $log -Tail 15 | ForEach-Object { Say "    $_" } }
        }
        throw "New version is not running."
    }
}

# Entry point. When dot-sourced (". update.ps1") only the functions are defined - used by tests.
if ($MyInvocation.InvocationName -ne ".") {
    try {
        Update-Project
    } catch {
        Say ""; Say "ERROR: $($_.Exception.Message)" "Red"
        exit 1
    }
}

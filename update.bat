@echo off
REM One-command update: git pull, dependencies, restart, verification of the running version.
REM Run from the project folder (as Administrator when the site runs as scheduled tasks).
REM Options: -Force (restart even if current), -NoRestart. Log: data\update.log
REM Everything is ASCII on purpose: no code page problems in any console.
cd /d "%~dp0"
REM Call and exit on ONE line: cmd reads a .bat in pieces and git pull may replace this very file.
REM The exit code of update.ps1 is passed on (0 = the new version is running, 1 = failed).
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\windows\update.ps1" %* && exit /b 0 || exit /b 1

@echo off
REM Emergency stop for the panel and the worker (works even if the site is unreachable).
REM Details and the -All flag: deploy\windows\stop_all.ps1
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\windows\stop_all.ps1" %*

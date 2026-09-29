@echo off
REM Full stop for a dedicated bot VPS: same as stop_all.bat, but always with -All, so
REM EVERY python.exe/pythonw.exe on this machine is killed, not just ones matched by path.
REM Use this on the server (nothing else here runs on Python) so a stuck dialog can never
REM keep going after the site is switched off. On a shared/dev machine use stop_all.bat instead.
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\windows\stop_all.ps1" -All

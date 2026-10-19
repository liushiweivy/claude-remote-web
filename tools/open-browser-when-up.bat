@echo off
rem Helper: wait until :8765 is LISTENING (max ~30s), then open the page.
rem Runs minimized, spawned by autostart-visible.bat. Pure ASCII only.
cd /d "%~dp0.."
for /l %%i in (1,1,30) do (
  netstat -ano -p tcp | findstr /R /C:":8765 " | findstr LISTENING >nul
  if not errorlevel 1 goto :up
  timeout /t 1 /nobreak >nul
)
exit /b 0
:up
set TOKEN=
if exist data\token.txt set /p TOKEN=<data\token.txt
start "" "http://127.0.0.1:8765/?token=%TOKEN%"

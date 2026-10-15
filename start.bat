@echo off
rem Claude Session Bridge launcher.
rem Keep this file pure ASCII: cmd.exe parses .bat bytes with the system ANSI
rem codepage; CJK in UTF-8 batch files gets mis-parsed even after chcp 65001.
chcp 65001 >nul
cd /d "%~dp0"
title Claude Session Bridge

if not exist ".venv\Scripts\python.exe" (
  echo [first run] creating venv and installing dependencies ...
  py -3.12 -m venv .venv
  if errorlevel 1 (
    echo py launcher unavailable, trying python ...
    python -m venv .venv
  )
  .venv\Scripts\python.exe -m pip install --upgrade pip
  .venv\Scripts\python.exe -m pip install -r requirements.txt
)

rem Import-time side effect: guarantees token file exists; print token (data\token.txt)
for /f %%i in ('.venv\Scripts\python.exe -c "from server.app import TOKEN;print(TOKEN)"') do set TOKEN=%%i

start "" "http://127.0.0.1:8765/?token=%TOKEN%"
.venv\Scripts\python.exe -m server.app
pause

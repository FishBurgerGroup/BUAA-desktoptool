@echo off
setlocal
cd /d "%~dp0"
start "" /b pythonw.exe "%~dp0app.py"
endlocal & exit /b 0

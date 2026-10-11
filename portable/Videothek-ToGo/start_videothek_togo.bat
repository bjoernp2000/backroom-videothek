@echo off
setlocal
cd /d "%~dp0"
if not exist data mkdir data
start "" /D "%~dp0" "%~dp0Videothek-ToGo.exe"
endlocal

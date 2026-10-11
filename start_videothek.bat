@echo off
rem Videothek App starten (eigenes Fenster, eingebauter Server)
cd /d "%~dp0"
if exist Videothek.exe (
  start "" /D "%~dp0" "%~dp0Videothek.exe"
  exit /b 0
)
rem Fallback: Developer-Modus (venv + Server + Browser)
if not exist .venv\Scripts\pythonw.exe (
  echo Erste Einrichtung: virtuelle Umgebung wird erstellt...
  python -m venv .venv || goto :err
  .venv\Scripts\python.exe -m pip install --quiet -r requirements.txt || goto :err
)
start "" /D "%~dp0" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0launcher\app.py"
exit /b 0
:err
echo Einrichtung fehlgeschlagen. Python 3.11+ muss installiert sein.
pause

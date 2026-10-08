@echo off
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python report.py summary
python report.py events --limit 30
pause

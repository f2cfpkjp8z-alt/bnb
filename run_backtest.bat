@echo off
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python backtest.py --days 60 --top 15
pause

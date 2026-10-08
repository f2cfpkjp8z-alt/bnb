@echo off
cd /d "%~dp0"
where python >nul 2>nul || (echo Python not found. Install it from python.org and tick Add to PATH, then run this again. & pause & exit /b 1)
if not exist .venv python -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install -q --upgrade pip
python -m pip install -q -r requirements.txt
python tests\test_basic.py
if not exist .env copy .env.example .env >nul
echo.
echo Setup done. Next: run_bot.bat   (and see FIREBASE_SETUP.md for the web app)
pause

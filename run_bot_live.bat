@echo off
cd /d "%~dp0"
call .venv\Scripts\activate.bat
echo This arms the REAL-MONEY account (it still starts with trading OFF until you switch it on in the web app).
set /p OK=Type YES to continue: 
if /i not "%OK%"=="YES" exit /b 1
python bot.py --live --i-understand-live-risk
pause

@echo off
cd /d "%~dp0"
echo Open http://localhost:8080 in your browser. Press Ctrl+C to stop.
python -m http.server 8080 --bind 127.0.0.1 --directory webapp

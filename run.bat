@echo off
cd /d "%~dp0"
python -m pip install -r requirements.txt
echo.
echo Dashboard: http://localhost:8000    API docs: http://localhost:8000/docs
python -m uvicorn app:app --host 0.0.0.0 --port 8000
pause

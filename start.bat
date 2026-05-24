@echo off
echo Starting Agentic Browser...
cd /d %~dp0backend
if exist venv\Scripts\python.exe (
  venv\Scripts\python.exe server.py
) else (
  python server.py
)

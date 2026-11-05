@echo off
title Session Manager
cd /d "%~dp0"

rem Check whether port 5690 is already in use
netstat -ano | findstr ":5690" | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 (
    echo Session Manager is already running at http://localhost:5690
    start http://localhost:5690
    goto :end
)

rem Try system Python
where python >nul 2>&1
if %errorlevel%==0 (
    python server.py
    goto :end
)

rem Try python3
where python3 >nul 2>&1
if %errorlevel%==0 (
    python3 server.py
    goto :end
)

rem Try py launcher
where py >nul 2>&1
if %errorlevel%==0 (
    py server.py
    goto :end
)

echo [Error] Python not found. Please install Python 3.10+
pause

:end

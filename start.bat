@echo off
title Session Manager
cd /d "%~dp0"

:: 检查端口是否已被占用
netstat -ano | findstr ":5690" | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 (
    echo Session Manager is already running at http://localhost:5690
    start http://localhost:5690
    goto :end
)

:: 尝试系统 Python
where python >nul 2>&1 && (
    python server.py
    goto :end
)

:: 尝试 python3
where python3 >nul 2>&1 && (
    python3 server.py
    goto :end
)

:: 尝试 py launcher
where py >nul 2>&1 && (
    py server.py
    goto :end
)

echo [Error] Python not found. Please install Python 3.10+
pause

:end

@echo off
REM Windows 一键启动；Linux / macOS 请使用同目录下的 start.sh
setlocal EnableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"

echo ================================
echo   Claude Web Server 启动脚本
echo ================================
echo.

:: 检查 Python 是否安装
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未检测到 Python，请先安装 Python 3.8+
    echo 下载地址: https://www.python.org/downloads/
    pause
    exit /b 1
)

:: 从 config.ini 读取监听端口（失败则回退 8080）
set "SERVER_PORT=8080"
for /f "delims=" %%p in ('python -c "import sys; sys.path.insert(0, '.'); from claude_web import config; print(config.SERVER_PORT)" 2^>nul') do set "SERVER_PORT=%%p"
if "!SERVER_PORT!"=="" set "SERVER_PORT=8080"

:: 检查依赖
echo [1/3] 检查依赖...
pip install -r requirements.txt -q
if errorlevel 1 (
    echo [安装] 正在安装 requirements.txt ...
    pip install -r requirements.txt
)

:: 设置 Token（可选）
set "TOKEN=%~1"
if "%TOKEN%"=="" (
    echo [Token] 未设置，局域网内无需认证
) else (
    echo [Token] %TOKEN%
)

:: 清理旧进程（占用当前配置端口的 LISTENING 进程）
echo [2/3] 清理旧进程...
echo   正在查找占用端口 !SERVER_PORT! 的进程...
set /a KILL_COUNT=0
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :!SERVER_PORT! ^| findstr LISTENING 2^>nul') do (
    echo   终止进程 PID: %%a
    taskkill /F /PID %%a >nul 2>&1
    set /a KILL_COUNT+=1
)
if !KILL_COUNT!==0 (
    echo   未发现占用端口 !SERVER_PORT! 的进程
) else (
    echo   已清理 !KILL_COUNT! 个旧进程
)
echo   等待端口释放...
timeout /t 1 /nobreak >nul

:: 启动服务
echo [3/3] 启动服务...
echo.
echo 局域网访问地址（端口 !SERVER_PORT!）:
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do (
    for /f "tokens=1" %%b in ("%%a") do (
        if "%TOKEN%"=="" (
            echo   http:%%b:!SERVER_PORT!
        ) else (
            echo   http:%%b:!SERVER_PORT!?token=%TOKEN%
        )
    )
)
echo.
echo 提示: 每个浏览器有独立的会话，互不干扰
echo.

if "%TOKEN%"=="" (
    python server.py
) else (
    python server.py %TOKEN%
)

pause

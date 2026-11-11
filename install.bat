@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem 缓存和虚拟环境全部落在本目录（D 盘），不写 C 盘
set "PIP_CACHE_DIR=%~dp0.pipcache"
set "PIP_NO_CACHE_DIR=off"

echo.
echo   文字转语音v1.0 · 安装依赖
echo   ------------------------------------

set "PYEXE="
call :try "C:\Python314\python.exe"
if not defined PYEXE call :try "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PYEXE call :try "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PYEXE call :try "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PYEXE call :try "python"

if not defined PYEXE (
    echo   [X] 没找到带 tkinter 的 Python
    echo       去 python.org 装一个 3.10+ 的正式版（安装时勾选 tcl/tk）
    pause
    exit /b 1
)

echo   [1/2] 使用 %PYEXE%
if not exist ".venv\Scripts\python.exe" (
    "%PYEXE%" -m venv .venv
    if errorlevel 1 (
        echo   [X] 创建虚拟环境失败
        pause
        exit /b 1
    )
)

echo   [2/2] 安装依赖（首次约 1-3 分钟）
".venv\Scripts\python.exe" -m pip install -q --upgrade pip
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
if errorlevel 1 (
    echo   [X] 依赖安装失败，检查网络后重试
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -c "import tkinter, edge_tts, sounddevice, miniaudio, numpy" >nul 2>&1
if errorlevel 1 (
    echo   [X] 依赖校验失败，请删除 .venv 目录后重新运行本脚本
    pause
    exit /b 1
)

echo.
echo   完成！双击 run.bat 启动
echo   所有文件都在 %~dp0
echo.
pause
exit /b 0

:try
set "CAND=%~1"
if "%CAND%"=="" exit /b
"%CAND%" -c "import tkinter" >nul 2>&1
if not errorlevel 1 set "PYEXE=%CAND%"
exit /b

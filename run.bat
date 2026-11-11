@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo 还没安装依赖，请先双击 install.bat
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" "app.py"

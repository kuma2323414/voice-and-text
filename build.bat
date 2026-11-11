@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "APP=文字转语音v1.0"

if not exist ".venv\Scripts\python.exe" (
    echo 请先运行 install.bat 安装依赖
    pause
    exit /b 1
)

echo 安装 / 更新 PyInstaller...
set "PIP_CACHE_DIR=%~dp0.pipcache"
".venv\Scripts\python.exe" -m pip install -q pyinstaller

echo 开始打包（约 30 秒）...
set "PYINSTALLER_CONFIG_DIR=%~dp0.pyinstaller"
set "TMP=%~dp0.tmp"
set "TEMP=%~dp0.tmp"
if not exist ".tmp" mkdir ".tmp"

rem 打包会重建 dist\%APP%，先保住用户配置、常用语和语音缓存
set "KEEP=%~dp0.distkeep"
if exist "%KEEP%" rd /s /q "%KEEP%"
if exist "dist\%APP%\config.json" (
    mkdir "%KEEP%" 2>nul
    copy /y "dist\%APP%\config.json" "%KEEP%\" >nul
    copy /y "dist\%APP%\phrases.json" "%KEEP%\" >nul 2>nul
    if exist "dist\%APP%\cache" robocopy "dist\%APP%\cache" "%KEEP%\cache" /E /NFL /NDL /NJH /NJS /NP >nul
)

".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --windowed --onedir ^
    --name TTSVoice --icon icon.ico --distpath dist --workpath build --specpath . ^
    --add-data "rvc\rvc_worker.py;rvc" ^
    --collect-all sounddevice --collect-all miniaudio --collect-all edge_tts app.py

if errorlevel 1 (
    echo 打包失败
    pause
    exit /b 1
)

if exist "dist\TTSVoice\TTSVoice.exe" (
    if exist "dist\%APP%" rd /s /q "dist\%APP%"
    ren "dist\TTSVoice" "%APP%"
    ren "dist\%APP%\TTSVoice.exe" "%APP%.exe"
)

rem 把配置、常用语、语音缓存放回去
if exist "%KEEP%\config.json" copy /y "%KEEP%\config.json" "dist\%APP%\" >nul
if exist "%KEEP%\phrases.json" copy /y "%KEEP%\phrases.json" "dist\%APP%\" >nul
if exist "%KEEP%\cache" robocopy "%KEEP%\cache" "dist\%APP%\cache" /E /NFL /NDL /NJH /NJS /NP >nul
if exist "%KEEP%" rd /s /q "%KEEP%"

rd /s /q build 2>nul
rd /s /q .tmp 2>nul

echo.
echo   完成：dist\%APP%\%APP%.exe
echo.
pause

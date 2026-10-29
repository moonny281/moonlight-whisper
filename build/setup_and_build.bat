@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

echo ============================================================
echo   Moonlight Whisper - Windows 一键环境配置与 EXE 打包脚本
echo ============================================================

cd /d "%~dp0.."

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [错误] 未检测到 Python，请先安装 Python 3.10 ~ 3.12 ^(勾选 Add python.exe to PATH^)。
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo [1/4] 正在创建虚拟环境 .venv ...
    python -m venv .venv
)

echo [2/4] 正在安装项目依赖 ^(使用清华镜像加速^)...
".venv\Scripts\python.exe" -m pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple
".venv\Scripts\python.exe" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
if %errorlevel% neq 0 (
    echo [错误] 依赖安装失败。
    pause
    exit /b 1
)

echo [3/4] 正在调用 build\build.ps1 执行测试与打包...
set "PATH=%CD%\.venv\Scripts;%PATH%"
powershell -ExecutionPolicy Bypass -File "build\build.ps1" -Version 1.1.0
if %errorlevel% neq 0 (
    echo [错误] 打包过程中出现错误。
    pause
    exit /b 1
)

echo.
echo [4/4] 打包成功！
echo  - 免安装目录版位于: dist\MoonlightWhisper\MoonlightWhisper.exe
echo  - 安装包位于 ^(若已装 Inno Setup^): dist\installer\MoonlightWhisper-Setup-1.1.0.exe
pause

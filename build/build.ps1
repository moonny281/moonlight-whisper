<#
.SYNOPSIS
    Moonlight Whisper 一键构建脚本：单元测试 -> PyInstaller onedir -> 便携版 ZIP -> Inno Setup 安装包编译
#>

param(
    [string]$Version = "1.0.0",
    [switch]$SkipTests = $false,
    [switch]$SkipInstaller = $false
)

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot
$env:APP_VERSION = $Version

Write-Host "====================================================" -ForegroundColor Cyan
Write-Host " Moonlight Whisper Build Pipeline v$Version" -ForegroundColor Cyan
Write-Host "====================================================" -ForegroundColor Cyan

if (-not (Test-Path "assets\app.ico")) {
    throw "未找到 assets\app.ico，请确认图标资源存在。"
}
if (-not (Test-Path "bin\ffmpeg.exe")) {
    Write-Warning "未检测到 bin\ffmpeg.exe！打包可以继续，但发布前请将 ffmpeg.exe 放入 bin\ 目录。"
}

# 1. 运行单元测试
if (-not $SkipTests) {
    Write-Host "[1/4] 正在运行单元测试..." -ForegroundColor Yellow
    python -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) {
        throw "单元测试未通过，构建中止。"
    }
}

# 2. 执行 PyInstaller onedir 打包
Write-Host "[2/4] 正在执行 PyInstaller (onedir) 打包..." -ForegroundColor Yellow
if (Test-Path "dist\MoonlightWhisper") {
    Remove-Item -Recurse -Force "dist\MoonlightWhisper"
}
python -m PyInstaller --noconfirm --clean "build\app.spec"
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller 打包失败。"
}

New-Item -ItemType Directory -Force -Path "dist\MoonlightWhisper\bin" | Out-Null
if (Test-Path "bin\*") {
    Copy-Item -Path "bin\*" -Destination "dist\MoonlightWhisper\bin\" -Recurse -Force
}

# 3. 校验打包产物并生成便携版 ZIP
Write-Host "[3/4] 正在校验打包产物..." -ForegroundColor Yellow
$ExePath = "dist\MoonlightWhisper\MoonlightWhisper.exe"
if (-not (Test-Path $ExePath)) {
    throw "未找到生成的 $ExePath"
}
New-Item -ItemType Directory -Force -Path "dist\installer" | Out-Null
$PortableZip = "dist\installer\MoonlightWhisper-Portable-$Version.zip"
if (Test-Path $PortableZip) { Remove-Item -Force $PortableZip }
Compress-Archive -Path "dist\MoonlightWhisper\*" -DestinationPath $PortableZip -Force
Write-Host "已生成便携版压缩包: $PortableZip" -ForegroundColor Green

# 4. 调用 Inno Setup 生成安装包
if (-not $SkipInstaller) {
    Write-Host "[4/4] 正在使用 Inno Setup 编译安装包..." -ForegroundColor Yellow
    $IsccExe = $null
    if (Get-Command "iscc.exe" -ErrorAction SilentlyContinue) {
        $IsccExe = (Get-Command "iscc.exe").Source
    } else {
        $PathsToCheck = @(
            "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
            "${env:ProgramFiles}\Inno Setup 6\ISCC.exe"
        )
        foreach ($p in $PathsToCheck) {
            if (Test-Path $p) {
                $IsccExe = $p
                break
            }
        }
    }

    if ($null -eq $IsccExe) {
        Write-Warning "未找到 Inno Setup (ISCC.exe)，已跳过安装包制作，可使用便携版 $PortableZip"
    } else {
        & $IsccExe "/DMyAppVersion=$Version" "build\installer.iss"
        if ($LASTEXITCODE -ne 0) {
            throw "Inno Setup 编译失败。"
        }
        Write-Host "安装包编译完成：dist\installer\MoonlightWhisper-Setup-$Version.exe" -ForegroundColor Green
    }
}

Write-Host "构建流程全部完成！" -ForegroundColor Cyan

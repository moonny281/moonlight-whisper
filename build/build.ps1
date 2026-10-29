<#
.SYNOPSIS
    Moonlight Whisper 一键构建脚本：单元测试 -> 生成版本资源 -> PyInstaller onedir -> 产物校验 -> Inno Setup 安装包编译
#>

param(
    [string]$Version = "1.0.0",
    [switch]$SkipTests = $false,
    [switch]$SkipInstaller = $false
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

Write-Host "====================================================" -ForegroundColor Cyan
Write-Host " Moonlight Whisper Build Pipeline v$Version" -ForegroundColor Cyan
Write-Host "====================================================" -ForegroundColor Cyan

# 1. 检查图标与外部二进制
if (-not (Test-Path "assets\app.ico")) {
    throw "未找到 assets\app.ico，请确认图标资源存在。"
}
if (-not (Test-Path "bin\ffmpeg.exe")) {
    Write-Warning "未检测到 bin\ffmpeg.exe！打包可以继续，但发布前请将 ffmpeg.exe 放入 bin\ 目录。"
}

# 生成 Windows EXE 版本信息资源文件 (§9.3)
$VerParts = $Version.Split(".")
while ($VerParts.Count -lt 4) { $VerParts += "0" }
$TupleStr = "($($VerParts[0]), $($VerParts[1]), $($VerParts[2]), $($VerParts[3]))"
$VersionFileContent = @"
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=$TupleStr,
    prodvers=$TupleStr,
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [
          StringStruct('CompanyName', 'Moonlight Whisper Team'),
          StringStruct('FileDescription', 'Moonlight Whisper Video Transcriber'),
          StringStruct('FileVersion', '$Version'),
          StringStruct('InternalName', 'MoonlightWhisper'),
          StringStruct('OriginalFilename', 'MoonlightWhisper.exe'),
          StringStruct('ProductName', 'Moonlight Whisper'),
          StringStruct('ProductVersion', '$Version')
        ]
      )
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"@
Set-Content -Path "build\file_version_info.txt" -Value $VersionFileContent -Encoding UTF8

# 2. 运行单元测试
if (-not $SkipTests) {
    Write-Host "[1/4] 正在运行单元测试..." -ForegroundColor Yellow
    python -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) {
        throw "单元测试未通过，构建中止。"
    }
}

# 3. 执行 PyInstaller onedir 打包
Write-Host "[2/4] 正在执行 PyInstaller (onedir) 打包..." -ForegroundColor Yellow
if (Test-Path "dist\MoonlightWhisper") {
    Remove-Item -Recurse -Force "dist\MoonlightWhisper"
}
pyinstaller --noconfirm --clean "build\app.spec"
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller 打包失败。"
}

# 复制 bin/ 到 dist/MoonlightWhisper/bin/
New-Item -ItemType Directory -Force -Path "dist\MoonlightWhisper\bin" | Out-Null
if (Test-Path "bin\*") {
    Copy-Item -Path "bin\*" -Destination "dist\MoonlightWhisper\bin\" -Recurse -Force
}

# 4. 校验打包产物
Write-Host "[3/4] 正在校验打包产物..." -ForegroundColor Yellow
$ExePath = "dist\MoonlightWhisper\MoonlightWhisper.exe"
if (-not (Test-Path $ExePath)) {
    throw "未找到生成的 $ExePath"
}
Write-Host "已生成可执行文件: $ExePath" -ForegroundColor Green

# 5. 调用 Inno Setup 生成安装包
if (-not $SkipInstaller) {
    Write-Host "[4/4] 正在使用 Inno Setup 编译安装包..." -ForegroundColor Yellow
    $IsccCandidates = @(
        "iscc.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles}\Inno Setup 6\ISCC.exe"
    )
    $IsccExe = $null
    foreach ($candidate in $IsccCandidates) {
        if (Get-Command $candidate -ErrorAction SilentlyContinue) {
            $IsccExe = $candidate
            break
        }
    }

    if ($null -eq $IsccExe) {
        Write-Warning "未找到 Inno Setup (ISCC.exe)，已跳过安装包制作。PyInstaller onedir 产物位于 dist\MoonlightWhisper"
    } else {
        & $IsccExe "/DMyAppVersion=$Version" "build\installer.iss"
        if ($LASTEXITCODE -ne 0) {
            throw "Inno Setup 编译失败。"
        }
        Write-Host "安装包编译完成：dist\installer\MoonlightWhisper-Setup-$Version.exe" -ForegroundColor Green
    }
}

Write-Host "构建流程全部完成！" -ForegroundColor Cyan

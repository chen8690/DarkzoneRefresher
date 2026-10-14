<#
.SYNOPSIS
  打包「暗区突围·武装派遣 自动刷新」为独立 Windows 桌面应用。
.DESCRIPTION
  1) 安装 PyInstaller
  2) 把 darkzone_app.py + darkzone 包 + 规则 Excel + 配置 + PowerShell 脚本
     打包成一个文件夹形式的发行版（dist\DarkzoneRefresher\）
  3) 可选再用 Inno Setup / NSIS 打成安装包
#>

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

# 打包用的 Python：可用环境变量 DARKZONE_PYTHON 覆盖；否则按当前用户家目录找 DSH 自带运行时，
# 再退到 PATH 里的 python（不写死某个用户名，换机器也能直接用）
$Python = $env:DARKZONE_PYTHON
if (-not $Python) {
    $cand = "$env:USERPROFILE\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
    if (Test-Path $cand) { $Python = $cand }
}
if (-not $Python) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $Python = $cmd.Source }
}
if (-not $Python) { throw "找不到 python；请设环境变量 DARKZONE_PYTHON 或把 python 加进 PATH" }
Write-Host "  打包用 Python: $Python" -ForegroundColor Gray

Write-Host "=== 1/4 安装 PyInstaller ===" -ForegroundColor Cyan
& $Python -m pip install pyinstaller --quiet 2>&1 | Out-Null
Write-Host "  PyInstaller 就绪" -ForegroundColor Green

Write-Host "`n=== 2/4 准备数据文件 ===" -ForegroundColor Cyan

# 检查并复制 Excel 规则文件：项目里已有 规则表.xlsx 就保留用户自己那份，
# 只有项目里没有时才从桌面 工作簿1.xlsx 复制一份进来
$ExcelSource = "$env:USERPROFILE\Desktop\工作簿1.xlsx"
$ExcelDest   = "$ScriptDir\规则表.xlsx"
if (Test-Path $ExcelDest) {
    Write-Host "  规则表: 用项目自带的 $ExcelDest" -ForegroundColor Green
} elseif (Test-Path $ExcelSource) {
    Copy-Item $ExcelSource $ExcelDest -Force
    Write-Host "  规则表: $ExcelSource -> 规则表.xlsx" -ForegroundColor Green
} else {
    Write-Host "  ⚠️ 规则表未找到: $ExcelSource (打包后需手动放置)" -ForegroundColor Yellow
}

# 确保 profiles 存在
if (-not (Test-Path "$ScriptDir\profiles")) {
    New-Item "$ScriptDir\profiles" -ItemType Directory -Force | Out-Null
}
if (-not (Test-Path "$ScriptDir\profiles\default.json")) {
    @'
{
  "_comment": "默认坐标配置，请参照 profiles/default.json 格式",
  "screen_list": { "refresh_x_frac": {}, "select_x_frac_hint": [] },
  "screen_detail": { "refresh_x_frac": 0.92, "right_panel_min_x_frac": 0.60, "dispatch_xy_frac": [0.808, 0.926] },
  "dialog_confirm_xy_frac": [0.4975, 0.7167],
  "tap_sweep_dx_frac": [0.0, -0.012, 0.012],
  "tap_sweep_dy_frac": [0.0, -0.012, 0.012]
}
'@ | Out-File "$ScriptDir\profiles\default.json" -Encoding utf8
}

Write-Host "`n=== 3/4 运行 PyInstaller ===" -ForegroundColor Cyan

# 构建数据文件参数
$DataFiles = @()

# darkzone 下的 PowerShell 脚本
foreach ($ps1 in (Get-ChildItem "$ScriptDir\darkzone\*.ps1" -ErrorAction SilentlyContinue)) {
    $DataFiles += "--add-data=$($ps1.FullName);darkzone"
}

# profiles
$DataFiles += "--add-data=$ScriptDir\profiles;profiles"

# Excel 规则表（如果复制成功）
if (Test-Path $ExcelDest) {
    $DataFiles += "--add-data=$ExcelDest;."
}
# 也尝试原始路径
if (Test-Path $ExcelSource) {
    $DataFiles += "--add-data=$ExcelSource;."
}

Write-Host "  数据文件参数:" -ForegroundColor Gray
$DataFiles | ForEach-Object { Write-Host "    $_" -ForegroundColor Gray }

$PyInstallerArgs = @(
    "-m", "PyInstaller",
    "--clean",
    "--noconfirm",
    "--name=DarkzoneRefresher",
    "--add-data=$ScriptDir\darkzone;darkzone"
) + $DataFiles + @(
    "--hidden-import=PIL",
    "--hidden-import=PIL.Image",
    "--hidden-import=openpyxl",
    "--hidden-import=numpy",
    "--hidden-import=tkinter",
    "--collect-all=openpyxl",
    "darkzone_app.py"
)

Write-Host "`n  执行 PyInstaller …" -ForegroundColor Gray
& $Python @PyInstallerArgs

Write-Host "`n=== 4/4 补充文件 ===" -ForegroundColor Cyan

$DistDir = "$ScriptDir\dist\DarkzoneRefresher"

# 复制 PowerShell OCR 脚本到 dist
# onedir 布局下，`--add-data=…\darkzone;darkzone` 实际落点是 `_internal\darkzone\`
# （win_ocr.py 里 HERE 会被解析到那里，运行时找得到）；这里再往 exe 同级的
# `darkzone\` 也放一份，作为老布局的兜底，两处都有才不挑 PyInstaller 版本。
New-Item -ItemType Directory -Force "$DistDir\_internal\darkzone" | Out-Null
New-Item -ItemType Directory -Force "$DistDir\darkzone" | Out-Null
foreach ($ps1 in (Get-ChildItem "$ScriptDir\darkzone\*.ps1" -ErrorAction SilentlyContinue)) {
    Copy-Item $ps1.FullName "$DistDir\_internal\darkzone\$($ps1.Name)" -Force
    Copy-Item $ps1.FullName "$DistDir\darkzone\$($ps1.Name)" -Force
}

# 复制 profiles
Copy-Item "$ScriptDir\profiles" "$DistDir\profiles" -Recurse -Force

# 复制规则 Excel
if (Test-Path $ExcelDest) {
    Copy-Item $ExcelDest "$DistDir\规则表.xlsx" -Force
}
if (Test-Path $ExcelSource) {
    Copy-Item $ExcelSource "$DistDir\$((Get-Item $ExcelSource).Name)" -Force
}

# 复制声明与许可证（正文文档，弹窗里是内嵌摘要，这里给一份可读的全文）
foreach ($doc in "DISCLAIMER.md", "LICENSE", "README.md") {
    if (Test-Path "$ScriptDir\$doc") {
        Copy-Item "$ScriptDir\$doc" "$DistDir\$doc" -Force
    }
}

# 创建便捷启动批处理
# 注意：.bat 内容保持纯 ASCII —— cmd.exe 按系统代码页（936）读批处理，
# 中文/制表符会被读错甚至拆成两条命令；中文提示交给 exe 自己输出。
@"
@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo ==============================================
echo   Darkzone Dispatch Auto-Refresher
echo ==============================================
echo.
start "" "DarkzoneRefresher.exe"
"@ | Out-File "$DistDir\启动.bat" -Encoding ascii

Write-Host "`n✅ 打包完成!" -ForegroundColor Green
Write-Host "   输出目录: $DistDir" -ForegroundColor Cyan
Write-Host "   启动方式: 双击 DarkzoneRefresher.exe 或 启动.bat" -ForegroundColor Cyan
Write-Host "`n   注意: 运行时需要 adb 在 PATH 中（或通过 GUI 设置路径）" -ForegroundColor Yellow
Write-Host "         首次运行建议先用「安全预演」测试" -ForegroundColor Yellow
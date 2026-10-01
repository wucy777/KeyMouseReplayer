# 构建脚本：编译 exe 并自检产物
#
#   .\tools\build.ps1              编译 + 自检
#   .\tools\build.ps1 -SkipCheck   只编译
#
# 依据项目规范：构建只在用户明确要求时执行，本脚本由用户手动调用。
param(
    [switch]$SkipCheck
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

$exe = Join-Path $root 'dist\KeyMouseReplayer.exe'

Write-Host '=== 生成图标 ===' -ForegroundColor Cyan
python tools\make_icon.py
if ($LASTEXITCODE -ne 0) {
    if (Test-Path (Join-Path $root 'assets\app.ico')) {
        Write-Host '  图标生成失败（缺 Pillow?），改用现有 assets\app.ico 继续' -ForegroundColor Yellow
    } else {
        throw "图标生成失败且 assets\app.ico 不存在：请先 pip install -r requirements-dev.txt"
    }
}

Write-Host ''
Write-Host '=== 编译 exe ===' -ForegroundColor Cyan
python -m PyInstaller --noconfirm --clean build.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败，退出码 $LASTEXITCODE" }

if (-not (Test-Path $exe)) { throw "未生成 $exe" }
$size = [math]::Round((Get-Item $exe).Length / 1MB, 2)
Write-Host "  已生成 $exe ($size MB)" -ForegroundColor Green

if (-not $SkipCheck) {
    Write-Host ''
    Write-Host '=== 自检产物 ===' -ForegroundColor Cyan
    $json = Join-Path $root '_cache\exe_check.json'
    $txt = "$json.txt"
    Remove-Item $json, $txt -ErrorAction SilentlyContinue
    # ArgumentList 必须整体预加引号：PowerShell 5.1 不会给含空格的数组元素
    # 自动加引号，路径里的空格会把 --out 的参数截断成 'D:\AAA' 这样的残路径
    $p = Start-Process -FilePath $exe -ArgumentList ('--check --out "{0}"' -f $json) -PassThru -Wait
    if (Test-Path $txt) { Get-Content $txt } else { Write-Host '  未生成自检结果' -ForegroundColor Yellow }
    if ($p.ExitCode -ne 0) {
        Write-Host ''
        Write-Host "自检未通过（退出码 $($p.ExitCode)）" -ForegroundColor Red
        Write-Host '提示：若报 "Failed to extract ... fopen: Permission denied"，' -ForegroundColor Yellow
        Write-Host '      是单文件 exe 无法向 %TEMP% 解压（安全软件/沙箱拦截），' -ForegroundColor Yellow
        Write-Host '      不是程序缺陷；在普通桌面环境下重新运行自检即可。' -ForegroundColor Yellow
        exit 1
    }
    Write-Host ''
    Write-Host '构建完成且自检通过' -ForegroundColor Green
} else {
    Write-Host ''
    Write-Host '构建完成（已跳过自检）' -ForegroundColor Green
}

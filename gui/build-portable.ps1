param(
    [string]$OutputDir = "",
    [switch]$SkipBackendBuild,
    [switch]$SkipTauriBuild
)

$ErrorActionPreference = "Stop"

$GuiRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $GuiRoot
$BackendDist = Join-Path $RepoRoot "dist\autovisor-backend"
$TauriConfPath = Join-Path $GuiRoot "src-tauri\tauri.conf.json"

$TauriConf = Get-Content -Raw -Encoding UTF8 $TauriConfPath | ConvertFrom-Json
$Version = [string]$TauriConf.version
if (-not $Version) {
    throw "无法从 $TauriConfPath 读取版本号"
}
$MainBinaryName = if ($TauriConf.mainBinaryName) { [string]$TauriConf.mainBinaryName } else { "Autovisor GUI" }
$PrimaryExeName = "$MainBinaryName.exe"

if (-not $OutputDir) {
    $OutputDir = Join-Path $RepoRoot "dist\Autovisor-GUI-$Version-windows-amd64"
}

if (-not $SkipBackendBuild -or -not (Test-Path (Join-Path $BackendDist "autovisor-backend.exe"))) {
    Write-Host "==> 1/4 构建内置无感后端 (autovisor-backend.exe)..." -ForegroundColor Cyan
    Push-Location $RepoRoot
    try {
        if (Get-Command uv -ErrorAction SilentlyContinue) {
            uv run --with pyinstaller==6.22.3 python build_backend.py
        } else {
            $VenvPy = Join-Path $RepoRoot ".venv\Scripts\python.exe"
            if (Test-Path $VenvPy) {
                & $VenvPy build_backend.py
            } else {
                python build_backend.py
            }
        }
        if ($LASTEXITCODE -ne 0) { throw "内置后端构建失败 (exit=$LASTEXITCODE)" }
    } finally {
        Pop-Location
    }
} else {
    Write-Host "==> 1/4 复用已构建的内置后端: $BackendDist" -ForegroundColor DarkCyan
}

if (-not (Test-Path (Join-Path $BackendDist "autovisor-backend.exe"))) {
    throw "未找到内置后端构建产物: $(Join-Path $BackendDist 'autovisor-backend.exe')"
}

$ReleaseDir = Join-Path $GuiRoot "src-tauri\target\release"
$CandidateExes = @(
    (Join-Path $ReleaseDir $PrimaryExeName),
    (Join-Path $ReleaseDir "autovisor-gui.exe")
) | Select-Object -Unique
$ExistingExe = $CandidateExes | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $SkipTauriBuild -or -not $ExistingExe) {
    Write-Host "==> 2/4 构建 React 前端..." -ForegroundColor Cyan
    Push-Location $GuiRoot
    try {
        if (-not (Test-Path (Join-Path $GuiRoot "node_modules"))) {
            npm install
            if ($LASTEXITCODE -ne 0) { throw "npm install 失败 (exit=$LASTEXITCODE)" }
        }
        npm run build
        if ($LASTEXITCODE -ne 0) { throw "前端构建失败 (exit=$LASTEXITCODE)" }
    } finally {
        Pop-Location
    }

    Write-Host "==> 3/4 编译 Tauri 桌面程序 (Release)..." -ForegroundColor Cyan
    Push-Location (Join-Path $GuiRoot "src-tauri")
    try {
        cargo build --release -j 4
        if ($LASTEXITCODE -ne 0) { throw "Tauri Release 编译失败 (exit=$LASTEXITCODE)" }
    } finally {
        Pop-Location
    }
} else {
    Write-Host "==> 2/4 & 3/4 复用已编译的 Tauri Release 宿主: $ExistingExe" -ForegroundColor DarkCyan
}

$BuiltExe = $CandidateExes | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $BuiltExe) {
    throw "未找到构建产物: $ReleaseDir\$PrimaryExeName 或 autovisor-gui.exe"
}

Write-Host "==> 4/4 组装便携成品目录: $OutputDir" -ForegroundColor Cyan
if (Test-Path $OutputDir) {
    Remove-Item -Recurse -Force $OutputDir
}
New-Item -ItemType Directory -Path $OutputDir | Out-Null

# 主程序本身即为 Autovisor GUI.exe，内置后端隐藏于 runtime/ 目录下由 Tauri 无感托管
Copy-Item $BuiltExe (Join-Path $OutputDir "Autovisor GUI.exe")
Copy-Item -Recurse -Force $BackendDist (Join-Path $OutputDir "runtime")
$RuntimeLogsDir = Join-Path $OutputDir "runtime\logs"
if (Test-Path $RuntimeLogsDir) {
    Remove-Item -Recurse -Force $RuntimeLogsDir
}
New-Item -ItemType File -Path (Join-Path $OutputDir ".portable") -Force | Out-Null

Write-Host ""
Write-Host "构建完成！直接双击运行桌面端：" -ForegroundColor Green
Write-Host "  $(Join-Path $OutputDir 'Autovisor GUI.exe')" -ForegroundColor Yellow

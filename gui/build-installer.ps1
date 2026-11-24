param(
    [switch]$SkipBackendBuild
)

$ErrorActionPreference = "Stop"

$GuiRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $GuiRoot
$BackendDist = Join-Path $RepoRoot "dist\autovisor-backend"
$BackendExe = Join-Path $BackendDist "autovisor-backend.exe"
$RuntimeResourceDir = Join-Path $GuiRoot "src-tauri\resources\runtime"
$TauriConfPath = Join-Path $GuiRoot "src-tauri\tauri.conf.json"

$TauriConf = Get-Content -Raw -Encoding UTF8 $TauriConfPath | ConvertFrom-Json
$Version = [string]$TauriConf.version
if (-not $Version) {
    throw "无法从 $TauriConfPath 读取版本号"
}

if (-not $SkipBackendBuild -or -not (Test-Path $BackendExe)) {
    Write-Host "==> 1/4 构建内置无感后端 (autovisor-backend.exe)..." -ForegroundColor Cyan
    Push-Location $RepoRoot
    try {
        $VenvPy = Join-Path $RepoRoot ".venv\Scripts\python.exe"
        if (Get-Command uv -ErrorAction SilentlyContinue) {
            uv run --with pyinstaller==6.13.0 python build_backend.py
        } elseif (Test-Path $VenvPy) {
            & $VenvPy build_backend.py
        } else {
            python build_backend.py
        }
        if ($LASTEXITCODE -ne 0) {
            throw "内置后端构建失败 (exit=$LASTEXITCODE)"
        }
    } finally {
        Pop-Location
    }
} else {
    Write-Host "==> 1/4 复用已构建的内置后端: $BackendDist" -ForegroundColor DarkCyan
}

if (-not (Test-Path $BackendExe)) {
    throw "未找到内置后端构建产物: $BackendExe"
}

Write-Host "==> 2/4 同步内置后端至 Tauri 资源目录: $RuntimeResourceDir" -ForegroundColor Cyan
if (Test-Path $RuntimeResourceDir) {
    Remove-Item -Recurse -Force $RuntimeResourceDir
}
New-Item -ItemType Directory -Path (Split-Path -Parent $RuntimeResourceDir) -Force | Out-Null
Copy-Item -Recurse -Force $BackendDist $RuntimeResourceDir
$RuntimeLogsDir = Join-Path $RuntimeResourceDir "logs"
if (Test-Path $RuntimeLogsDir) {
    Remove-Item -Recurse -Force $RuntimeLogsDir
}

if (-not (Test-Path (Join-Path $RuntimeResourceDir "autovisor-backend.exe"))) {
    throw "资源目录同步失败，缺少 autovisor-backend.exe"
}

Write-Host "==> 3/4 使用 Tauri NSIS Bundler 编译前端、Rust 宿主与安装向导..." -ForegroundColor Cyan
Push-Location $GuiRoot
try {
    $TauriCliBin = Join-Path $GuiRoot "node_modules\.bin\tauri.cmd"
    if (-not (Test-Path $TauriCliBin)) {
        Write-Host "    正在安装前端依赖 (@tauri-apps/cli)..." -ForegroundColor DarkCyan
        npm install
        if ($LASTEXITCODE -ne 0) {
            throw "npm install 失败 (exit=$LASTEXITCODE)"
        }
    }
    npx tauri build --bundles nsis
    if ($LASTEXITCODE -ne 0) {
        throw "Tauri NSIS 安装包构建失败 (exit=$LASTEXITCODE)"
    }
} finally {
    Pop-Location
}

Write-Host "==> 4/4 输出标准 Windows 安装程序..." -ForegroundColor Cyan
$NsisBundleDir = Join-Path $GuiRoot "src-tauri\target\release\bundle\nsis"
$SetupArtifact = Get-ChildItem -Path $NsisBundleDir -Filter "*-setup.exe" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
if (-not $SetupArtifact) {
    $SetupArtifact = Get-ChildItem -Path $NsisBundleDir -Filter "*.exe" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
}
if (-not $SetupArtifact) {
    throw "未在 $NsisBundleDir 找到生成的 NSIS 安装程序"
}

$DistRoot = Join-Path $RepoRoot "dist"
New-Item -ItemType Directory -Path $DistRoot -Force | Out-Null
$TargetInstaller = Join-Path $DistRoot "Autovisor-GUI-Setup-$Version-windows-x64.exe"
Copy-Item -Force $SetupArtifact.FullName $TargetInstaller

Write-Host ""
Write-Host "Windows 安装包构建完成！" -ForegroundColor Green
Write-Host "  $TargetInstaller" -ForegroundColor Yellow

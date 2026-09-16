<#
.SYNOPSIS
    安装/升级 llama.cpp（Windows + CUDA 预编译版）—— 自动查版本、下载、解压、规整、验证

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File D:\ai\mt\install-llama.ps1
    .\install-llama.ps1 -Root D:\ai\llama -CudaMajor 12
    .\install-llama.ps1 -Proxy "https://ghfast.top"        # 走第三方加速，注意风险
    .\install-llama.ps1 -Zip D:\Downloads\llama-cuda.zip   # 用本地已下好的包
    .\install-llama.ps1 -Clean                             # 装完删掉 zip
#>
[CmdletBinding()]
param(
    [string]$Root      = "D:\ai\llama",   # 安装目录
    [int]   $CudaMajor = 12,              # 你驱动支持的大版本：552.46 → 12
    [string]$Proxy     = "",              # 可选：GitHub 加速前缀，如 https://ghfast.top
    [string[]]$Zip     = @(),             # 可选：本地 zip 路径，跳过下载
    [switch]$Clean,                       # 装完删除 zip
    [switch]$SkipVerify
)

$ErrorActionPreference        = "Stop"
$ProgressPreference           = "SilentlyContinue"   # 大幅加速 Invoke-WebRequest
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

function Say  ($m, $c = "Gray") { Write-Host $m -ForegroundColor $c }
function Ok   ($m) { Write-Host "[OK] $m" -ForegroundColor Green }
function Warn ($m) { Write-Host "[!]  $m" -ForegroundColor Yellow }
function Fail ($m) { Write-Host "[X]  $m" -ForegroundColor Red }

function Get-CudaVer ($name) {
    $m = [regex]::Match($name, 'cuda[-_]?(?:cu)?([0-9]+(?:\.[0-9]+)?)')
    if ($m.Success) { try { return [version]$m.Groups[1].Value } catch {} }
    return [version]'0.0'
}

function Pick-Asset ($assets, $pattern, $major) {
    $c = @($assets | Where-Object { $_.name -match $pattern })
    $c = @($c | Where-Object { (Get-CudaVer $_.name).Major -eq $major })
    if ($c.Count -eq 0) { return $null }
    return ($c | Sort-Object { Get-CudaVer $_.name } | Select-Object -Last 1)
}

function Normalize-Layout ($root) {
    # llama.cpp 的 zip 有时是 build/bin/... 结构，把内容提到根目录
    $exe = Get-ChildItem -Path $root -Recurse -Filter llama-server.exe -File -ErrorAction SilentlyContinue |
           Select-Object -First 1
    if ($exe -and $exe.DirectoryName.TrimEnd('\') -ne $root.TrimEnd('\')) {
        Say "  规整: $($exe.DirectoryName) -> $root"
        Copy-Item -Path (Join-Path $exe.DirectoryName '*') -Destination $root -Recurse -Force
    }
    # cudart DLL 同理
    $dll = Get-ChildItem -Path $root -Recurse -Filter 'cudart64*.dll' -File -ErrorAction SilentlyContinue |
           Where-Object { $_.DirectoryName.TrimEnd('\') -ne $root.TrimEnd('\') } | Select-Object -First 1
    if ($dll) {
        Say "  规整: $($dll.DirectoryName) -> $root"
        Copy-Item -Path (Join-Path $dll.DirectoryName '*') -Destination $root -Recurse -Force
    }
}

# ============================ 开始 ============================
Say ""
Say "===== llama.cpp 安装脚本 =====" "Cyan"

New-Item -ItemType Directory -Force -Path $Root | Out-Null
$Root = (Resolve-Path $Root).Path
Say "安装目录: $Root" "Cyan"
Say "CUDA 大版本: $CudaMajor  (驱动 552.46 对应 CUDA 12.4)" "Cyan"

$targets = @()

if ($Zip.Count -gt 0) {
    # ---------- 本地包模式 ----------
    Say "`n[1/5] 使用本地压缩包" "Cyan"
    foreach ($z in $Zip) {
        if (-not (Test-Path $z)) { Fail "找不到: $z"; exit 1 }
        $dest = Join-Path $Root (Split-Path $z -Leaf)
        Copy-Item $z $dest -Force
        Ok "已复制 $(Split-Path $z -Leaf)"
    }
} else {
    # ---------- 在线模式 ----------
    Say "`n[1/5] 查询 GitHub 最新版本" "Cyan"
    try {
        $rel = Invoke-RestMethod -Uri "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest" `
               -Headers @{ "User-Agent" = "install-llama-ps" } -TimeoutSec 60
    } catch {
        Fail "无法访问 GitHub API: $($_.Exception.Message)"
        Warn "办法：1) 稍后重试  2) 加 -Proxy 前缀  3) 手动下载后用 -Zip 指定"
        exit 1
    }
    Ok "最新版本: $($rel.tag_name)"

    $mainRe   = 'llama-.*bin-win-cuda.*x64\.zip$'
    $cudartRe = 'cudart-llama-bin-win-cuda.*x64\.zip$'

    $aMain   = Pick-Asset $rel.assets $mainRe   $CudaMajor
    $aCudart = Pick-Asset $rel.assets $cudartRe $CudaMajor

    if (-not $aMain) {
        Fail "没找到 CUDA $CudaMajor 的 Windows 主程序包"
        Warn "本次 release 的候选文件："
        $rel.assets | Where-Object { $_.name -match 'win.*\.zip$' } |
            ForEach-Object { Say "   $($_.name)" }
        exit 1
    }
    $targets += $aMain
    if ($aCudart) { $targets += $aCudart }
    else { Warn "没找到 cudart 包，稍后若报缺 DLL 请手动补下" }

    Say "`n[2/5] 下载" "Cyan"
    foreach ($a in $targets) {
        $out = Join-Path $Root $a.name
        if ((Test-Path $out) -and ((Get-Item $out).Length -gt 1MB)) {
            Ok "已存在，跳过: $($a.name)"; continue
        }
        $url = $a.browser_download_url
        if ($Proxy) { $url = $Proxy.TrimEnd('/') + '/' + $url }
        Say "  $($a.name)  ($([math]::Round($a.size/1MB,1)) MB)"
        try { Invoke-WebRequest -Uri $url -OutFile $out -TimeoutSec 1800 }
        catch { Fail "下载失败: $($_.Exception.Message)"; exit 1 }
        if ((Get-Item $out).Length -lt 1MB) { Fail "文件过小，疑似被拦截: $($a.name)"; exit 1 }
        Ok "  完成 $([math]::Round((Get-Item $out).Length/1MB,1)) MB"
    }
}

# ---------- 解压 ----------
Say "`n[3/5] 解压" "Cyan"
$zips = @(Get-ChildItem -Path $Root -Filter *.zip -File)
if ($zips.Count -eq 0) { Fail "目录里没有 zip 可解压"; exit 1 }
foreach ($z in $zips) {
    Say "  $($z.Name)"
    Expand-Archive -Path $z.FullName -DestinationPath $Root -Force
}
Ok "解压完成"

# ---------- 规整 ----------
Say "`n[4/5] 规整目录结构" "Cyan"
Normalize-Layout $Root
$exePath = Join-Path $Root 'llama-server.exe'
if (-not (Test-Path $exePath)) { Fail "解压后仍找不到 llama-server.exe"; exit 1 }
Ok "llama-server.exe 就位"

# ---------- 验证 ----------
Say "`n[5/5] 验证" "Cyan"
$devOut = ""
if (-not $SkipVerify) {
    Say "  --- llama-server --version ---"
    try { Say ((& $exePath --version 2>&1 | Out-String).Trim()) } catch { Warn "输出 --version 失败" }

    Say "  --- llama-server --list-devices ---"
    try { $devOut = (& $exePath --list-devices 2>&1 | Out-String); Say $devOut.Trim() }
    catch { Warn "该版本可能不支持 --list-devices" }
}

$hasCudart = @(Get-ChildItem -Path $Root -Filter 'cudart64*.dll' -File -ErrorAction SilentlyContinue).Count -gt 0
$hasCudaDev = $devOut -match 'CUDA'

Say ""
Say "==================== 结果 ====================" "Cyan"
if ($hasCudart) { Ok "CUDA 运行时 DLL 已就位" } else { Warn "缺 cudart64*.dll（需 cudart-llama-bin-win-cuda-*.zip）" }
if ($SkipVerify)      { Warn "已跳过设备验证" }
elseif ($hasCudaDev)  { Ok "检测到 CUDA 设备 —— GPU 可用" }
else                  { Warn "未在设备列表里看到 CUDA。可能原因：cudart DLL 缺失 / 驱动过旧 / 该构建不含 CUDA" }

if ($Clean) {
    Get-ChildItem -Path $Root -Filter *.zip -File | Remove-Item -Force
    Ok "已清理 zip"
}

Say ""
Say "下一步：" "Cyan"
Say "  1) 编辑 D:\ai\mt\start-model.bat，把 -m 指向你的 Qwen3-8B-Q4_K_M.gguf"
Say "  2) 双击 start-model.bat，看到 'server is listening' 即成功"
Say "  3) 启动时留意日志里的 'offloaded XX/XX layers to GPU' —— 这才是 GPU 真正生效的证据"
Say "  4) 失败排查：cudaErrorInsufficientDriver → 更新显卡驱动"
Say ""

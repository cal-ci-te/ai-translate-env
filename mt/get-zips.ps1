<#
  get-zips.ps1 v3  (ASCII messages)
  - does NOT use /releases/latest
  - scans recent releases and picks the newest one that really has CUDA win assets
  - probes mirrors, downloads both zips via curl.exe
.EXAMPLE
  .\get-zips.ps1
  .\get-zips.ps1 -Tag b7123
  .\get-zips.ps1 -Scan 60
#>
[CmdletBinding()]
param(
    [string]  $Out       = "D:\ai\mt\downloads",
    [int]     $CudaMajor = 12,
    [string]  $Tag       = "",
    [int]     $Scan      = 30,
    [string[]]$Mirrors   = @("","https://ghfast.top/","https://gh-proxy.com/","https://ghproxy.net/","https://github.moeyy.xyz/")
)
$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force -Path $Out | Out-Null

function Get-CudaVer ($n) {
    $m = [regex]::Match($n, '(?:cuda|cu)[-_]?([0-9]+(?:\.[0-9]+)?)')
    if ($m.Success) { try { return [version]$m.Groups[1].Value } catch {} }
    return [version]'0.0'
}
function Pick ($list, $re) {
    $c = @($list | Where-Object { $_.name -match $re })
    if ($c.Count -eq 0) { return $null }
    $v = @($c | Where-Object { (Get-CudaVer $_.name).Major -eq $CudaMajor })
    if ($v.Count -gt 0) { $c = $v }          # 优先精确匹配大版本；没有就退而求其次
    return ($c | Sort-Object { Get-CudaVer $_.name } | Select-Object -Last 1)
}
function Get-Json ($url, $file) {
    curl.exe -m 25 -sL -H "User-Agent: get-zips" $url -o $file
    if ($LASTEXITCODE -ne 0) { return $null }
    try { return ([IO.File]::ReadAllText($file, [Text.Encoding]::UTF8) | ConvertFrom-Json) } catch { return $null }
}

$mainRe   = '^llama-.*bin-win-.*(cuda|cu1[0-9]).*x64\.zip$'
$cudartRe = 'cudart.*win.*x64\.zip$'

Write-Host "[1/3] query github releases ..." -ForegroundColor Cyan
$rel = $null
if ($Tag) {
    $rel = Get-Json "https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/$Tag" (Join-Path $Out "rel.json")
    if (-not $rel) { Write-Host "[X] cannot fetch tag $Tag" -ForegroundColor Red; exit 1 }
    Write-Host "      tag: $($rel.tag_name)"
} else {
    $rels = Get-Json "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=$Scan" (Join-Path $Out "rels.json")
    if (-not $rels) { Write-Host "[X] cannot list releases" -ForegroundColor Red; exit 1 }
    $rels = @($rels)
    Write-Host "      tag / asset-count / has_cuda_win :" -ForegroundColor Gray
    foreach ($r in $rels) {
        $a   = @($r.assets)
        $hit = (@($a | Where-Object { $_.name -match $mainRe }).Count -gt 0)
        Write-Host ("      {0,-18} {1,-4} {2}" -f $r.tag_name, $a.Count, $hit)
        if ($hit) { $rel = $r; break }
    }
    if (-not $rel) {
        Write-Host "[X] none of the last $Scan releases has a CUDA $CudaMajor windows asset" -ForegroundColor Red
        exit 1
    }
    Write-Host "[OK] selected release: $($rel.tag_name)" -ForegroundColor Green
}

$assets  = @($rel.assets)
$aMain   = Pick $assets $mainRe
$aCudart = Pick $assets $cudartRe
if (-not $aMain) {
    Write-Host "[X] no cuda win asset. win assets in this release:" -ForegroundColor Red
    $assets | Where-Object { $_.name -match 'win' } | ForEach-Object { Write-Host "      $($_.name)" }
    exit 1
}
$picks = @($aMain)
if ($aCudart) { $picks += $aCudart } else { Write-Host "[!] no cudart asset found" -ForegroundColor Yellow }
foreach ($p in $picks) { Write-Host ("      pick: {0}  ({1} MB)" -f $p.name, [math]::Round($p.size/1MB,1)) }

Write-Host "[2/3] probe mirrors (range 1KB) ..." -ForegroundColor Cyan
$probe  = $picks[0].browser_download_url
$chosen = $null
$ok     = $false
foreach ($m in $Mirrors) {
    $u     = if ($m) { $m.TrimEnd('/') + '/' + $probe } else { $probe }
    $label = if ($m) { $m } else { "(direct)" }
    $code  = ("" + (curl.exe -m 10 -sL -r 0-1023 -o NUL -w "%{http_code}" $u)).Trim()
    Write-Host ("      {0,-34} -> {1}" -f $label, $code)
    if ($code -match '^(200|206)$') { $chosen = $m; $ok = $true; break }
}
if (-not $ok) {
    Write-Host "[X] no working mirror. download these in a browser, then run:" -ForegroundColor Red
    Write-Host "    install-llama.ps1 -Zip `"<path1>`",`"<path2>`"" -ForegroundColor Yellow
    $picks | ForEach-Object { Write-Host "      $($_.name)" }
    exit 1
}
$label = if ($chosen) { $chosen } else { "(direct)" }
Write-Host "      using: $label" -ForegroundColor Green

Write-Host "[3/3] download ..." -ForegroundColor Cyan
$paths = @()
foreach ($p in $picks) {
    $dest = Join-Path $Out $p.name
    $paths += $dest
    if ((Test-Path $dest) -and ((Get-Item $dest).Length -gt 1MB)) { Write-Host "      exists, skip: $($p.name)"; continue }
    $u = if ($chosen) { $chosen.TrimEnd('/') + '/' + $p.browser_download_url } else { $p.browser_download_url }
    curl.exe -L --retry 2 -o $dest $u
    if ($LASTEXITCODE -ne 0) { Write-Host "[X] download failed: $($p.name)" -ForegroundColor Red; exit 1 }
    Write-Host ("      ok: {0}  ({1} MB)" -f $p.name, [math]::Round((Get-Item $dest).Length/1MB,1)) -ForegroundColor Green
}

$joined = (($paths | ForEach-Object { '"' + $_ + '"' }) -join ',')
Write-Host ""
Write-Host "DONE. Next, run:" -ForegroundColor Cyan
Write-Host "D:\ai\mt\install-llama.ps1 -Zip $joined" -ForegroundColor White

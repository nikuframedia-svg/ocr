$ErrorActionPreference = "Stop"

# R65 — derive root from script location (portable across machines).
# Script lives in <repo>/scripts/ops/start.ps1; root is 2 levels up.
$root = (Resolve-Path "$PSScriptRoot\..\..").Path
$logs = "$root\data\_logs"
New-Item -ItemType Directory -Force -Path $logs | Out-Null
Write-Host "REPO_ROOT=$root"

# R65 — load .env file from repo root if it exists. Parses KEY=VALUE
# lines (skipping comments and blank lines). Lets the operator point
# this runtime at a remote Ollama (OLLAMA_URL), local refs folder
# (KANBAN_DOC_DIR), etc. without editing this script.
$envFile = "$root\.env"
if (Test-Path $envFile) {
    Write-Host "Loading $envFile"
    Get-Content $envFile | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
            $idx = $line.IndexOf("=")
            $key = $line.Substring(0, $idx).Trim()
            $val = $line.Substring($idx + 1).Trim().Trim('"').Trim("'")
            Set-Item -Path "Env:$key" -Value $val
            Write-Host "  $key=$val"
        }
    }
} else {
    Write-Host "(no .env at $envFile - using defaults inline below)"
}

# Kill any stragglers
Get-Process | Where-Object { $_.Name -in @("python","cloudflared") } | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1

# Production OCR uses qwen3-vl:8b. Disable reasoning blocks via
# OCR_NO_THINK=1; older Qwen3 tests showed extra "thinking" tokens can hurt
# JSON stability and latency.
# R65: only set if not already loaded from .env.
if (-not $env:OCR_NO_THINK) { $env:OCR_NO_THINK = "1" }

# Round 44 - cross-check stub-accept variant. w13 builds on v13 with:
#   - lote 3-of-4 (drop esp gate; esp can be misread)
#   - modelo-aware dim downgrade (when modelo NO_MATCH, entry-selection is
#     unreliable - can't validate dim against wrong ref)
#   - cluster sanity (dim NA only when >=1 sibling dim MATCH)
# Achieves 95.50% match rate while preserving modelo NO_MATCH visibility
# (real divergences supervisor must see). See reports/round44_*.
if (-not $env:CC_STUB_VARIANT) { $env:CC_STUB_VARIANT = "w13" }

# Start uvicorn detached.
# DEV_RELOAD=1 (no .env do portátil de desenvolvimento) acrescenta --reload:
# o servidor recarrega sozinho a cada edição de .py. Em producao a variavel
# nao esta definida, por isso o comportamento mantem-se inalterado.
$uvArgs = @("-m","uvicorn","backend.app.web.main:app","--host","0.0.0.0","--port","8080","--log-level","info")
if ($env:DEV_RELOAD -eq "1") {
  $uvArgs += "--reload"
  Write-Host "DEV_RELOAD=1 - uvicorn com auto-reload (modo desenvolvimento)"
}
$uv = Start-Process -FilePath "$root\.venv\Scripts\python.exe" `
  -ArgumentList $uvArgs `
  -WorkingDirectory $root `
  -WindowStyle Hidden `
  -RedirectStandardOutput "$logs\uvicorn.log" `
  -RedirectStandardError "$logs\uvicorn.err" `
  -PassThru
$uv.Id | Out-File -Encoding ascii "$logs\uvicorn.pid"
Write-Host "UVICORN_PID=$($uv.Id)"
Start-Sleep -Seconds 4

# R65 - cloudflared auto-detect (PATH, then common install locations).
$cflar = (Get-Command cloudflared -ErrorAction SilentlyContinue).Source
if (-not $cflar) {
    $candidates = @(
        "C:\Users\$env:USERNAME\AppData\Local\Microsoft\WinGet\Packages\Cloudflare.cloudflared_Microsoft.Winget.Source_8wekyb3d8bbwe\cloudflared.exe",
        "C:\Program Files\cloudflared\cloudflared.exe",
        "C:\Program Files (x86)\cloudflared\cloudflared.exe"
    )
    foreach ($c in $candidates) { if (Test-Path $c) { $cflar = $c; break } }
}
if (-not $cflar) {
    Write-Host "ERROR: cloudflared not found. Install: winget install cloudflare.cloudflared"
    Write-Host "(uvicorn is up at http://127.0.0.1:8080 but no public tunnel)"
    exit 1
}
Write-Host "CLOUDFLARED=$cflar"

# Start cloudflared detached
$cf = Start-Process -FilePath $cflar `
  -ArgumentList "tunnel","--url","http://localhost:8080","--no-autoupdate" `
  -WorkingDirectory $root `
  -WindowStyle Hidden `
  -RedirectStandardOutput "$logs\cloudflared.log" `
  -RedirectStandardError "$logs\cloudflared.err" `
  -PassThru
$cf.Id | Out-File -Encoding ascii "$logs\cloudflared.pid"
Write-Host "CLOUDFLARED_PID=$($cf.Id)"
Write-Host "Waiting 8s for tunnel URL..."
Start-Sleep -Seconds 8

# Read tunnel URL from log (cloudflared writes to stderr by default)
$urlLine = Get-Content "$logs\cloudflared.err","$logs\cloudflared.log" -ErrorAction SilentlyContinue |
  Select-String -Pattern "https://[a-z0-9-]+\.trycloudflare\.com" |
  Select-Object -First 1
if ($urlLine) {
  $url = ($urlLine.Matches[0].Value)
  $url | Out-File -Encoding ascii "$logs\tunnel_url.txt"
  Write-Host "TUNNEL_URL=$url"
} else {
  Write-Host "TUNNEL_URL=NOT_FOUND_YET"
}

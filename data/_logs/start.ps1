$ErrorActionPreference = "Stop"
$root = "C:\Users\User\ocr"
$logs = "$root\data\_logs"
New-Item -ItemType Directory -Force -Path $logs | Out-Null

# Kill any stragglers
Get-Process | Where-Object { $_.Name -in @("python","cloudflared") } | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1

# Production OCR uses qwen3.5:9b (Round 20). Disable reasoning blocks via
# OCR_NO_THINK=1 — without it the model emits ~3× extra "thinking" tokens
# that cause 42% JSON parse failures and 4× latency. With it: 100% parse
# rate, ~22s/photo, +6.9pp field accuracy vs qwen2.5vl:7b baseline.
$env:OCR_NO_THINK = "1"

# Round 44 — cross-check stub-accept variant. w13 builds on v13 with:
#   - lote 3-of-4 (drop esp gate; esp can be misread)
#   - modelo-aware dim downgrade (when modelo NO_MATCH, entry-selection is
#     unreliable → can't validate dim against wrong ref)
#   - cluster sanity (dim NA only when ≥1 sibling dim MATCH)
# Achieves 95.50% match rate while preserving modelo NO_MATCH visibility
# (real divergences supervisor must see). See reports/round44_*.
$env:CC_STUB_VARIANT = "w13"

# Start uvicorn detached
$uv = Start-Process -FilePath "$root\.venv\Scripts\python.exe" `
  -ArgumentList "-m","uvicorn","backend.app.web.main:app","--host","0.0.0.0","--port","8080","--log-level","info" `
  -WorkingDirectory $root `
  -WindowStyle Hidden `
  -RedirectStandardOutput "$logs\uvicorn.log" `
  -RedirectStandardError "$logs\uvicorn.err" `
  -PassThru
$uv.Id | Out-File -Encoding ascii "$logs\uvicorn.pid"
Write-Host "UVICORN_PID=$($uv.Id)"
Start-Sleep -Seconds 4

# Start cloudflared detached
$cflar = "C:\Users\User\AppData\Local\Microsoft\WinGet\Packages\Cloudflare.cloudflared_Microsoft.Winget.Source_8wekyb3d8bbwe\cloudflared.exe"
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

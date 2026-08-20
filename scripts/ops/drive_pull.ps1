# Poller do Google Drive (scans + refs) - corre via tarefa agendada do
# Windows (ver register_drive_pull.ps1). Carrega o .env (mesmo padrao do
# start.ps1) e corre scripts/drive_pull.py no venv do repo.
# R227/R268: ASCII PURO neste ficheiro (PS5.1 le UTF-8 como ANSI).
$ErrorActionPreference = "Stop"

$root = (Resolve-Path "$PSScriptRoot\..\..").Path
$logs = "$root\data\_logs"
New-Item -ItemType Directory -Force -Path $logs | Out-Null

$envFile = "$root\.env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
            $idx = $line.IndexOf("=")
            $key = $line.Substring(0, $idx).Trim()
            $val = $line.Substring($idx + 1).Trim().Trim('"').Trim("'")
            Set-Item -Path "Env:$key" -Value $val
        }
    }
}

$py = "$root\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "ERROR: venv nao encontrado em $py"
    exit 1
}

& $py "$root\scripts\drive_pull.py" @args
exit $LASTEXITCODE

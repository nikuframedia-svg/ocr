# R105 — actualizacao do dia-a-dia no PC da Metalogalva.
# Busca o codigo novo do GitHub e reinicia o servidor. Um comando, e esta.
#
#   powershell -ExecutionPolicy Bypass -File scripts\ops\update.ps1
#
$ErrorActionPreference = "Stop"

# Raiz do repo: este script vive em <repo>\scripts\ops\update.ps1.
$root = (Resolve-Path "$PSScriptRoot\..\..").Path
Write-Host "REPO_ROOT=$root"

# --- Proteger os ficheiros que a aplicacao reescreve enquanto corre -------
# A base de dados e os ficheiros derivados sao DESTE PC. O 'git pull' nunca
# os deve sobrepor (nem rebentar por causa deles). 'assume-unchanged' diz ao
# git para nao esperar mudancas no ficheiro (R121: antes era 'skip-worktree'
# que abortava o pull quando o upstream tambem mexia no ficheiro). Em
# conjunto com o .gitignore (R121) e idempotente — correr varias vezes nao
# faz mal. Assim o codigo actualiza-se mas os dados de producao ficam
# intactos.
$protect = @(
  "data/app.db",
  "data/cross_sheet.json",
  "data/refs_cumulative.json",
  "kanban_refs/04_Documentacao/_refs_status.json",
  "lexicons/learned_overlay.json",
  "lexicons/sap_plan_mined.json"
)
foreach ($f in $protect) {
  if (Test-Path "$root\$f") {
    git -C $root update-index --assume-unchanged $f 2>$null
    if ($?) { Write-Host "  protegido: $f" }
  }
}

# --- Buscar o codigo novo -------------------------------------------------
# --ff-only: se por algum motivo nao for um avanco simples, para com erro
# em vez de criar um merge confuso. Nesse caso copia o ecra e mostra ao Claude.
Write-Host "A buscar codigo novo (git pull)..."
git -C $root pull --ff-only origin main
if (-not $?) {
  Write-Host ""
  Write-Host "ERRO: o 'git pull' falhou. Copia este ecra e mostra ao Claude."
  exit 1
}

# --- Reiniciar o servidor -------------------------------------------------
# O start.ps1 mata os processos antigos e arranca o uvicorn + cloudflared.
Write-Host "A reiniciar o servidor..."
& "$root\scripts\ops\start.ps1"

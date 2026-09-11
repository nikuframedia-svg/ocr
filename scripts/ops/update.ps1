# R105 - actualizacao do dia-a-dia no PC da Metalogalva.
# Busca o codigo novo do GitHub e reinicia o servidor. Um comando, e esta.
#
#   powershell -ExecutionPolicy Bypass -File scripts\ops\update.ps1
#
$ErrorActionPreference = "Stop"

# Raiz do repo: este script vive em <repo>\scripts\ops\update.ps1.
$root = (Resolve-Path "$PSScriptRoot\..\..").Path
Write-Host "REPO_ROOT=$root"

# Fail before backups or other actions when a copied installation lost its .git.
git -C $root rev-parse --show-toplevel 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
  Write-Host "ERRO: a instalacao nao tem metadados Git validos."
  Write-Host "Se .git estiver vazia/ausente, usar scripts/ops/recover_git.py conforme scripts/ops/README.md."
  exit 1
}

# --- Proteger os ficheiros que a aplicacao reescreve enquanto corre -------
# A base de dados e os ficheiros derivados sao DESTE PC. O 'git pull' nunca
# os deve sobrepor (nem rebentar por causa deles). 'assume-unchanged' diz ao
# git para nao esperar mudancas no ficheiro (R121: antes era 'skip-worktree'
# que abortava o pull quando o upstream tambem mexia no ficheiro). Em
# conjunto com o .gitignore (R121) e idempotente - correr varias vezes nao
# faz mal. Assim o codigo actualiza-se mas os dados de producao ficam
# intactos.
$protect = @(
  "data/app.db",
  "data/cross_sheet.json",
  "data/refs_cumulative.json",
  "kanban_refs/04_Documentacao/_refs_status.json",
  # R134 - os workbooks de refs sao actualizados pelo operador via /refs upload
  # (os.replace sobre o ficheiro tracked). Sem assume-unchanged, o 'git pull'
  # abaixo abortava/revertia o upload e o restart minerava o plano antigo.
  "kanban_refs/04_Documentacao/plan_colunas_cpis.xlsx",
  "kanban_refs/04_Documentacao/StockSAP.xlsx",
  "kanban_refs/04_Documentacao/maquinas.xlsx",
  "kanban_refs/04_Documentacao/ListaColaboradores.xlsx",
  "lexicons/learned_overlay.json",
  "lexicons/sap_plan_mined.json"
)
foreach ($f in $protect) {
  if (Test-Path "$root\$f") {
    # R232 - so protege ficheiros TRACKED. Os gitignored (data/app.db, etc.) nao
    # precisam e o 'update-index --assume-unchanged' rebentava neles com
    # 'fatal: Unable to mark file', abortando o update todo.
    if (git -C $root ls-files -- $f) {
      git -C $root update-index --assume-unchanged $f 2>$null
      if ($?) { Write-Host "  protegido: $f" }
    }
  }
}

# --- Snapshot pre-deploy da app.db (R267; best-effort, nunca aborta) ------
# Copia local com a API SQLite, incluindo WAL. Nao chamar o servidor antigo:
# ele ainda pode ter KANBAN_DB_BACKUP_DIR apontado para o Drive.
try {
  $backupCode = @'
import os, sqlite3, sys, tempfile
from contextlib import closing
from pathlib import Path
root = Path(sys.argv[1]).resolve()
dest = root / "data" / "backups"
dest.mkdir(parents=True, exist_ok=True)
fd, temp = tempfile.mkstemp(prefix="pre-update-", suffix=".db", dir=dest)
os.close(fd)
try:
    with closing(sqlite3.connect((root / "data" / "app.db").as_uri() + "?mode=ro", uri=True)) as src:
        with closing(sqlite3.connect(temp)) as dst:
            src.backup(dst)
            if dst.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("Backup SQLite invalido")
    os.replace(temp, dest / "app-pre-update.db")
finally:
    if os.path.exists(temp):
        os.unlink(temp)
'@
  $backupCode | & "$root\.venv\Scripts\python.exe" - $root
  if ($LASTEXITCODE -ne 0) { throw "snapshot local falhou" }
  Write-Host "  snapshot LOCAL da app.db: OK"
} catch {
  Write-Host "  (snapshot local pre-deploy nao feito: $_; segue)"
}

# --- Buscar o codigo novo -------------------------------------------------
# --ff-only: se por algum motivo nao for um avanco simples, para com erro
# em vez de criar um merge confuso. Nesse caso copia o ecra e mostra ao Claude.
#
# R223 - os dados runtime (data/images, data/events.jsonl, data/kernel_state.json,
# os JSON de cross-check, os aliases) DEIXARAM de ser tracked (passaram a
# gitignored). Antes, como a app os reescrevia e estavam tracked, o working tree
# estava sempre "sujo" e este pull abortava -> o codigo novo nunca chegava ao
# disco (a fabrica chegou a estar 22 commits atras). Agora o working tree fica
# limpo e o fast-forward passa sempre. (A transicao inicial dessa folha - passar
# os ficheiros para untracked sem perder dados - e' feita UMA vez, ver runbook.)
$before = (git -C $root rev-parse HEAD)
Write-Host "A buscar codigo novo (git pull)..."
git -C $root pull --ff-only origin main
if ($LASTEXITCODE -ne 0) {
  Write-Host ""
  Write-Host "ERRO: o 'git pull' falhou. Copia este ecra e mostra ao Claude."
  exit 1
}
$after = (git -C $root rev-parse HEAD)
Write-Host "HEAD: $before -> $after"
if ($before -eq $after) {
  Write-Host "(nada novo - ja estavas na versao mais recente do GitHub)"
} else {
  Write-Host "OK: codigo actualizado. A reiniciar para carregar a versao nova."
}

# --- Reiniciar o servidor -------------------------------------------------
# O start.ps1 mata os processos antigos e arranca o uvicorn + cloudflared.
Write-Host "A reiniciar o servidor..."
& "$root\scripts\ops\start.ps1"

# scripts/ops/

Runbook ops (R65, R105). Movidos de `data/_logs/` no R107.

| Ficheiro | Função |
|---|---|
| `start.ps1` | Mata processos antigos, carrega `.env`, arranca uvicorn :8080 + cloudflared tunnel (redundante desde o portal R268 — não remover ainda, não investir) |
| `update.ps1` | Snapshot pré-deploy da app.db + `git pull --ff-only` + reinicia o servidor; protege `data/app.db` e refs via `assume-unchanged` |
| `register_drive_pull.ps1` | (branch feature/drive-pull) Regista a tarefa Windows do poller drive_pull — correr UMA vez |

Uso típico no PC da Metalogalva:

```
cd C:\ocr
powershell -ExecutionPolicy Bypass -File scripts\ops\update.ps1
```

Ver `docs/MIGRATION.md` Parte D.

Nota R268: a app é servida em https://mtg2.nikufra.ai (portal
https://ocr.nikufra.ai) por um túnel SSH invertido — a ponte é a tarefa
Windows «OCR PC Bridge» em `C:\OCR-Suite\kit`, FORA deste repo. A app tem
de continuar em 127.0.0.1:8080 (é a porta que a ponte expõe).

## Google Drive — refs + PDFs + backup da app.db (R267/R268)

A via normal de entrada de dados passou a ser o poller `drive_pull`
(scripts/drive_pull.py, 2x/dia): descarrega a pasta partilhada do Drive
para o espelho local, pousa os refs no `KANBAN_REFS_IMPORT_DIR` (quem os
instala é o `ref_importer` nativo) e submete os PDFs da subpasta
«Kanban's MTG2» ao `POST /upload` (idempotência por sha256 no próprio
poller — `data/drive_pull_state.json`). O upload manual em `/refs`
continua a funcionar.

Setup no PC da fábrica (uma vez):

1. Merge da branch `feature/drive-pull` + `pip install -e .[drive]`.
2. No `.env` acrescentar:
   ```
   DRIVE_PULL_MIRROR_TO=C:\OCR-Suite\drive
   DRIVE_PULL_NOTIFY=http://127.0.0.1:8100/ingest/drive;http://127.0.0.1:8101/ingest/drive
   KANBAN_DB_BACKUP_DIR=C:\OCR-Suite\saida
   ```
   (`KANBAN_REFS_IMPORT_DIR` conforme a convenção do drive_pull; a pasta
   de backup é de SAÍDA dedicada — não usar o espelho de entrada
   `C:\OCR-Suite\drive`. A subida ao Drive da pasta de saída é do
   drive_pull, PUSH_FILES. O notify alimenta os dois kanban MES nas portas
   8100/8101 — repo separado, não tocar.)
3. `powershell -File scripts\ops\register_drive_pull.ps1` (uma vez).
4. `powershell -File scripts\ops\update.ps1` e verificar em
   `http://127.0.0.1:8080/admin/refs-status`:
   - `refs_importer.source_dir` aponta ao destino dos refs do drive_pull;
   - `db_backup.dest_dir = C:\OCR-Suite\saida`, `db_backup.last_ok = true`
     e o ficheiro `app.db` aparece lá (atualiza a cada hora e a cada
     update.ps1).
5. Restauro da base de dados: parar o servidor, copiar o `app.db` da pasta
   de saída (ou da versão no Drive) para `data\app.db`, arrancar.
6. Notas:
   - Para REVERTER de propósito para um plano antigo: upload manual em
     `/refs` e REMOVER o xlsx mais novo da pasta do Drive (senão o poller
     volta a trazê-lo). O importador recusa planos com max OF inferior ao
     ativo — aparece em `skipped` no `/admin/refs-status` com
     `guard: plan_recency`.
   - Os PDFs repetem nomes entre setores do Drive — o poller só trata a
     subpasta «Kanban's MTG2»; nunca assumir nome de PDF único.

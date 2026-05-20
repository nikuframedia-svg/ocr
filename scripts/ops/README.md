# scripts/ops/

Runbook ops (R65, R105). Movidos de `data/_logs/` no R107.

| Ficheiro | Função |
|---|---|
| `start.ps1` | Mata processos antigos, carrega `.env`, arranca uvicorn :8080 + cloudflared tunnel |
| `update.ps1` | `git pull --ff-only` + reinicia o servidor; protege `data/app.db` e refs via `--skip-worktree` |

Uso típico no PC da Metalogalva:

```
cd C:\ocr
powershell -ExecutionPolicy Bypass -File scripts\ops\update.ps1
```

Ver `docs/MIGRATION.md` Parte D.

# scripts/ops/

Runbook ops (R65, R105). Movidos de `data/_logs/` no R107.

| Ficheiro | Função |
|---|---|
| `start.ps1` | Mata processos antigos, carrega `.env`, arranca uvicorn :8080 + cloudflared tunnel (redundante desde o portal R268 — não remover ainda, não investir) |
| `update.ps1` | Snapshot pré-deploy da app.db + `git pull --ff-only` + reinicia o servidor; protege `data/app.db` e refs via `assume-unchanged` |
| `register_drive_pull.ps1` | (branch feature/drive-pull) Regista a tarefa Windows do poller drive_pull — correr UMA vez |

Uso típico no PC da Metalogalva:

```
cd F:\Apps\OCR-original
powershell -ExecutionPolicy Bypass -File scripts\ops\update.ps1
```

Ver `docs/MIGRATION.md` Parte D.

Nota R268: a app é servida em https://mtg2.nikufra.ai (portal
https://ocr.nikufra.ai) por um túnel SSH invertido — a ponte é a tarefa
Windows «OCR PC Bridge» em `C:\OCR-Suite\kit`, FORA deste repo. A app tem
de continuar em 127.0.0.1:8080 (é a porta que a ponte expõe).

## Referências do OCR original: pasta local

As referências entram exclusivamente por `F:\ocr\files` (ou pela pasta local
configurada em `KANBAN_REFS_IMPORT_DIR`). O importador vigia essa pasta a cada
15 minutos e instala os ficheiros em `kanban_refs\04_Documentacao`.
`Importar pasta agora` antecipa essa leitura local. A correção de antiguidade
usa a data do ficheiro e apresenta bloqueios explicitamente.

O OCR original não recebe referências/PDFs do Drive nem envia produção/backups
para o Drive. Captura e upload manual continuam disponíveis. Os ficheiros
`Kanbans_Producao_NOVO.xlsx` e CSV da produção continuam a ser gerados localmente.

## Backups locais do OCR

`KANBAN_DB_BACKUP_ENABLED=1` ativa backups em `<repo>\data\backups\app.db`.
Uma configuração antiga `KANBAN_DB_BACKUP_DIR` não vazia mantém os backups
ligados por compatibilidade, mas o destino antigo é ignorado. O valor explícito
`KANBAN_DB_BACKUP_ENABLED=0` desliga a função. Verificar `db_backup.dest_dir`
em `/admin/refs-status` depois de atualizar/reiniciar.

Para restaurar, parar o OCR e repor a cópia local de `app.db`. As cópias antigas
no Drive não são apagadas por esta alteração.

## Drive dos outros dois Kanbans MES

Não desativar a tarefa partilhada `OCR Drive Pull`: serve os outros dois
Kanbans, conforme o âmbito confirmado pelo Luís em 11/09/2026. O nome da tarefa
e os scripts existentes são mantidos. O poller passa a executar apenas:

- espelho da pasta Drive para `DRIVE_PULL_MIRROR_TO`, preservando subpastas;
- notificações para `http://127.0.0.1:8100/ingest/drive` e `:8101/ingest/drive`;
- exports `BaseDados_Cantoneiras_MTG3.xlsx` e `BaseDados_Perfis_MTG2.xlsx`;
- backups em `SAIDA/backups/kanban-mes/app-*.db` e
  `SAIDA/backups/kanban-mes-mtg2/app-*.db`, com a retenção existente de 14 dias.

A subida da pasta partilhada aceita apenas esses quatro padrões; o `app.db`
antigo do OCR e `Kanbans_Producao_NOVO.xlsx` ficam excluídos mesmo que ainda
constem da configuração antiga. O espelho recusa destinos que se sobreponham
à instalação do OCR ou à pasta local de referências. O parâmetro legado
`--app-url` deixa de produzir chamadas ao OCR original.

A rotina externa de IT que grava o plano/StockSAP em `F:\ocr\files` continua
necessária. Não é substituída pela tarefa Drive.

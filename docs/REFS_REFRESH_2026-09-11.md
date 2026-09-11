# Correção da atualização do plano no OCR original

O importador recusava o plano de 11/09/2026 em `F:\ocr\files` porque o maior número de OF era 999999 e o ativo continha 2502343. Esta última é uma linha fechada com OF igual à OV. A ordem numérica das OF não permite determinar a cronologia de um export.

A regra passa a comparar a data de modificação do ficheiro de origem com a do ativo, preservada por `copy2`. Um hash igual continua a ser ignorado. Um ficheiro diferente com data posterior pode entrar, mesmo com menos OF/linhas ou sem a maior OF antiga. Uma data anterior ou igual exige revisão explícita; não se tenta inferir a idade pelo número de OF. Esta regra depende da rotina externa preservar corretamente as datas dos exports: não é uma certificação da atualidade dos dados SAP dentro do Excel.

Os bloqueios têm agora resultado `ok=false`, motivo em `last_error` e lista `blocked`. Por compatibilidade, continuam presentes em `skipped`, mas a página só conta como iguais os que têm o motivo `igual ao ficheiro ativo`. A página e o botão de importação mostram o motivo dos bloqueios. A atualização das referências não chama a revalidação das folhas.

## Instalação no PC

Usar o script de atualização já existente no OCR original. Este obtém a versão publicada em `origin/main`, protege os dados locais e reinicia a aplicação.

No PowerShell do PC da fábrica:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File F:\Apps\OCR-original\scripts\ops\update.ps1
```

Não é necessário descarregar ou aplicar o ZIP anteriormente preparado. A atualização usa Git, seguindo o procedimento normal do projeto. O script pede um backup da base à aplicação (best effort), executa `git pull --ff-only origin main` e chama `start.ps1`. Se o Git recusar a atualização, conservar a mensagem de erro; não forçar nem apagar alterações locais. A atualização das referências não exige revalidar folhas.

Depois de reiniciar, o importador automático lê a pasta de origem. Em Administração → Referências, confirmar que o plano ativo tem o mesmo hash que o candidato da última execução e que não há bloqueios. À hora do diagnóstico, o candidato era `4477ece5…`, com 23 598 linhas; uma exportação posterior pode legitimamente mudar esses valores. O estado pode ser consultado em `/admin/refs-status`.

O acesso disponível nesta tarefa é HTTP às aplicações; a ponte SSH existente só encaminha portas e não dá uma consola de administração Windows. Publicar no GitHub não executa o script no PC. O sucesso local dos testes não confirma instalação na fábrica.

## Verificação

Testes específicos cobrem a regressão da OF 2502343, diminuição do número de linhas, datas antigas/iguais, simulação sem substituição, importação e leitura do novo plano, repetição idempotente e erros visíveis nas duas páginas e nos endpoints.

```bash
.venv/bin/python -m pytest -q --no-cov tests/unit/test_refs_uploads.py tests/unit/test_admin_pages.py
.venv/bin/python -m pytest -q -m 'not vllm'
```

Resultado da versão preparada em 11/09/2026, preparada sobre `origin/main` (`a953cd3`): **1 485 testes passaram**, 2 testes vLLM excluídos e cobertura de **70,85%**. A cópia isolada de testes foi inicializada com uma base SQLite vazia; antes dessa preparação, 10 testes falhavam por ausência da tabela `sheets`, comportamento também reproduzido na versão original sem a correção. A página foi renderizada em Chromium com o resultado do incidente: 1 igual e 1 bloqueado, com motivo visível.

## OCR original sem Drive — âmbito confirmado em 11/09/2026

O Luís confirmou que a desativação do Drive se aplica apenas ao OCR original.
A entrada de referências mantém-se em `F:\ocr\files`; o importador local e a
correção de antiguidade continuam ativos. O poller partilhado deixa de copiar
referências ou submeter PDFs ao OCR original. Também deixa de pedir ou enviar
exports do OCR. As configurações antigas de export/push do OCR são ignoradas.

A tarefa Windows existente continua a servir os dois Kanbans MES. As chamadas
são limitadas aos exports e à ingestão em 8100/8101. A subida aceita apenas os
dois BaseDados e os backups de `kanban-mes` e `kanban-mes-mtg2`. Assim, o
`app.db` antigo eventualmente presente na pasta partilhada não volta a subir.
O espelho não pode ter como destino a instalação do OCR ou a pasta local de
referências. As cópias antigas no Drive não foram apagadas.

Os backups do OCR, quando ligados, passam a `<repo>/data/backups/app.db`.
`KANBAN_DB_BACKUP_ENABLED` permite ligá-los/desligá-los; uma configuração
antiga `KANBAN_DB_BACKUP_DIR` mantém a função ligada, mas o destino antigo
é ignorado. Não se desativa a tarefa partilhada dos outros sistemas.

Verificação final: **1 495 testes passaram**, 2 testes vLLM excluídos,
cobertura **70,85%**. Os 100 testes específicos incluíram uma cópia local real
com rclone, confirmando que os artefactos do OCR ficam excluídos e os quatro
padrões de saída dos MES são preservados. Não foi feito acesso ao Drive por
este teste. As alterações estão preparadas localmente; a publicação GitHub
e a aplicação no PC continuam pendentes por falta de acesso autenticado.

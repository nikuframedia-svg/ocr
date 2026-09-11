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

Resultado da versão publicada em 11/09/2026, preparada sobre `origin/main` (`a953cd3`): **1 485 testes passaram**, 2 testes vLLM excluídos e cobertura de **70,85%**. A cópia isolada de testes foi inicializada com uma base SQLite vazia; antes dessa preparação, 10 testes falhavam por ausência da tabela `sheets`, comportamento também reproduzido na versão original sem a correção. A página foi renderizada em Chromium com o resultado do incidente: 1 igual e 1 bloqueado, com motivo visível.

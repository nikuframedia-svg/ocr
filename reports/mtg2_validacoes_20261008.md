# Validações e pesquisa das colunas MTG2 — 08/10/2026

Alterações locais na branch `codex/mtg2-validation-pending`, sobre a versão
`008a8b31e808af4610214f774d6391522df01cd8`, confirmada no `/health` da fábrica.
A implementação foi verificada localmente; a publicação no `main` do GitHub foi
autorizada pelo utilizador em 08/10/2026. A instalação na fábrica e alterações aos
registos reais continuam fora desta entrega.

## Diagnóstico confirmado na fábrica

- A instalação executa `008a8b3`, com o motor `v30_R259`.
- A `ListaColaboradores.xlsx` ativa tem 720 colaboradores e o mesmo conteúdo da
  cópia local: SHA-256 `2d29d51063a939e81f7ab27aac721792669858f7b43c81acd6220784bb793f36`.
  O número 4496 não existe nessa lista. O ficheiro ativo apresenta atualização de
  22/06/2026; o plano e o stock têm atualizações de 08/10/2026.
- O importador lê `F:\ocr\files`. Na consulta efetuada, encontrou apenas o plano
  e o StockSAP, ambos iguais às cópias ativas. Não encontrou uma lista nova de
  colaboradores. Foi consultado o estado; não foi acionada importação na fábrica.
- Na folha #7567, o OCR original apresenta número 496 e a vista final 4496.
  O nome continua sem correspondência na lista oficial. O asterisco é um
  indicador visual de alteração em relação ao OCR original.
- As folhas #7567 e #7545 já estão validadas. A #7545 é de Soldline e não declara
  FECHO. O valor/lote original que provocou o erro de gravação não foi recuperado;
  a divergência entre caixa desmarcada e valor enviado foi reproduzida em teste.

As leituras utilizaram `/health`, `/admin/refs-status`, `/mobile/qtds` e as vistas
OCR cru/final das folhas. Não foi obtida uma cópia integral da base da fábrica.

## Comportamento implementado

- O número e o PERNR conhecidos resolvem a identidade através da lista oficial.
  Números ambíguos e referências ausentes apresentam motivos de revisão.
  Aliases não criam colaboradores ausentes da lista. Nome e PERNR editados
  manualmente mantêm a proteção existente.
- Um número oficial em conflito com o nome não é trocado automaticamente pelo
  número de outra pessoa. A capitalização aprendida continua disponível e não
  confirma a identidade quando falta a lista oficial.
- O estado editável de FECHO é um booleano apenas no formulário. A gravação
  envia vazio ou `X`, conforme a caixa, e conserva o OCR original. A API continua
  a rejeitar outros valores e identifica folha e linha no erro.
- A pesquisa das colunas oferece OF/OV/referência, modelo, comprimento e páginas
  de 50 resultados. A interação foi adaptada da aplicação dos perfis
  `kanban-mes-mtg2` (referência `6069374c064cdb7a0426853baaa5c5a191012fa2`).
- A pendência utiliza a fase do setor e apenas a produção validada dessa fase,
  mantendo o corte temporal existente do plano. O consumo é repartido pelas
  linhas originais antes dos filtros e da paginação.
- O estado Aberta/Fechada vem do indicador explícito do plano. Uma fase concluída
  pode ter pendência zero e uma OF ainda aberta. Dados ausentes aparecem como
  indisponíveis. O texto adicional fica apenas na janela de pesquisa.
- A escolha mantém o índice original da linha do plano. A aplicação recusa
  escolhas feitas sobre uma revisão antiga da folha ou um plano entretanto alterado.
- A cache distingue fases e mudanças do plano/catálogo de máquinas. As
  invalidações existentes de edição, validação e importação também a limpam.

O motor local passa a `v31_R270`. A regeneração das cores/auditoria mantém a
proteção existente que impede alterações automáticas a `sheet_data` validado.
Não há migração da base de dados.

## Compatibilidade das interfaces

`GET /sheet/{id}/of-lookup` conserva `q`, `of` e `include_done` e acrescenta
`modelo`, `comp_mm` e `offset`. O comprimento é uma comparação numérica exata,
aceitando vírgula decimal. São rejeitados comprimentos inválidos e páginas negativas.

A resposta conserva os campos anteriores e acrescenta `setor`, `phase`,
`revision`, `plan_sha256`, `total`, `offset`, `limit` e `has_more`. Cada resultado
inclui `pending_valid` e `status_known`. `orig_idx` continua a identificar a
entrada original, incluindo referências repetidas.

`POST /sheet/{id}/apply-of-entry` aceita opcionalmente `expected_revision` e
`plan_sha256`; o novo formulário envia ambos. Clientes anteriores continuam
compatíveis. `/mobile/qtds` mantém a extração original de FECHO na resposta;
`fecho_checked` existe apenas no estado do navegador.

## Verificação

- Bateria completa: **1554 testes passaram**, 1 foi ignorado por ausência de
  `rclone` local. Cobertura **71,21%**, acima do mínimo de 70% do projeto.
- Comando: `.venv/bin/python -m pytest tests/unit -q --cov-report=term`.
- `git diff --check` passou. Ruff passou no módulo novo e nos três ficheiros
  novos de testes. O código existente mantém a dívida de lint anterior.
- A base local original continua com zero folhas; os testes utilizaram bases temporárias.

Foram executadas as funções reais do navegador em Node e verificada a interface
num servidor local com SQLite temporário. Confirmaram-se os filtros independentes,
a mensagem sobre o colaborador ausente e o texto
“Por fazer em ACABAMENTO MTG2: 10 · Aberta”. O servidor de demonstração foi encerrado.

Os testes cobrem zero/uma/várias marcas, desmarcação, preservação da extração,
referências repetidas em CSV e Excel, produção de outros setores, corte temporal,
dados indisponíveis, cache, seleção após filtros/paginação e escolhas desatualizadas.

## Dependência pendente

Para confirmar o colaborador 4496 é necessária uma lista oficial atualizada que
o inclua, disponibilizada ao importador existente ou carregada através de `/refs`.
A captura do papel não substitui essa referência oficial. Esta entrega explica
a ausência e conserva a revisão; não acrescenta uma identidade inventada.

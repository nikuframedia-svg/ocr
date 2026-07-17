# Proposta — conceitos cruzáveis declarados no registo de kanbans ("cross declarado")

**Data:** 2026-07-12 · **Rev. 2:** 2026-07-13 — estudo aprofundado: todos os
contratos internos verificados contra o código (secção 7); fase B redesenhada
para o padrão do repo; 2 fallbacks de UI encontrados e incorporados na fase A.
**Estado (2026-07-13): fases A e B IMPLEMENTADAS** — `declared_cross` no spec
(wizard: checkbox para campos custom + sub-linha coluna/tipo/tolerância),
braço `declared_plan` pós-winner no motor, `entry["extra"]` no watcher,
`scripts/diag/eval_declared.py` (fase B). Desvios face ao plano abaixo:
UI = "linha mínima" no passo campos (decisão do dono); campos ∈
`KNOWN_ROW_FIELDS` não-crossable também recusados no declarado (regra
endurecida — `coni`/`qtd` têm regras próprias). Fase C continua processo
com dev + gates. Semântica observada com o plano real: o declarado compara
com a ENTRY vencedora — numa OF com entries irmãs, se o winner cair numa
entry com a coluna vazia a célula fica NA (honesto; a taxa de NA da fase B
expõe-no por campo).
**Fase C-lite ("dv") também IMPLEMENTADA (2026-07-13):** opção por-campo
`vote=true` no registo ("contar para a escolha da linha") — termo one-sided
capado ≤2 bits no scorer do winner (molde extra_bias R242; peso log2(m/u)
com u medido do próprio plano — auto-protetor; m default 0.5 → medido pela
fase B via `eval_declared --write-params`), subtraído no posterior (padrão
id_infl R250 — calibração intacta), OFF no realinhamento, flip nunca sai
"strong" (cap 2.0 < margem decisiva 4.0 ⇒ revisão humana). Dupla porta:
toggle do admin no wizard × env `CROSS_DECLARED_VOTE` (off/shadow/on,
default off — "on" só com OK do Luís após soak `+dv`). Invariantes fixados
por 16 testes (TestDeclaredVote); harness estendido
(`--declared-spec`/`--dv-mode`/`--dv-synthetic`); soak com invariante do
cap + braço de utilidade no soak_sprt; trilho EN1090 (vote_decided_at +
evento kernel declared_vote_changed + declared_vote_mode por folha).
**Pergunta do dono:** ao registar um kanban de um setor novo com um campo que
nunca foi cruzado (ex.: "VLARG"), é possível dizer no próprio registo "isto é
cruzável contra o plano/StockSAP" sem intervenção de dev?

## TL;DR

- **Hoje: não.** O conjunto cruzável é fechado em código (12 conceitos) e é
  deliberado: o motor de cross é *calibrado por campo*, não genérico.
- **Proposto: sim, com um limite honesto.** O registo passa a poder declarar
  "o campo X cruza contra a coluna Y do plano, comparação texto/numérica±tol".
  Isso dá **sinalização verde/vermelho desde a primeira folha** (fase A,
  segura por construção: não toca no winner nem escreve por cima do OCR).
- **O voto no winner nunca é automático.** Um campo novo entra na decisão da
  encomenda apenas depois de calibrado com dados reais (fase C, processo com
  gates — o mesmo rigor de qualquer mudança do cross).
- **Rev. 2:** a viabilidade da fase A deixou de ser conjetura — os 6
  invariantes de que depende foram verificados um a um no código (secção 7):
  render da UI 100% compatível sem mudanças, escrita impossível por
  construção, ranking byte-idêntico, variante-agnóstico, sombra sem ruído,
  custo de memória ~1 escalar × 20,8k entries por coluna declarada.

---

## 1. Como funciona hoje (estado atual, verificado)

Um template registado no wizard só pode cruzar campos de `CROSSABLE_FIELDS`
(`backend/app/web/template_store.py:44-48`), o conjunto fechado:

```
cliente, ov, of, modelo, comp_mm, larg_mm, lote, esp, lbase, ltopo, dbase, dtopo
```

que é exatamente a união do que os builtins já cruzam. A cadeia completa:

| Peça | Onde | Papel |
|---|---|---|
| `CROSSABLE_FIELDS` / `KNOWN_ROW_FIELDS` | `template_store.py:44-58` | contrato do registo: o que o wizard aceita como cross / canónico |
| `validate_spec_payload` | `template_store.py:119-161` | rejeita cross fora de `row_fields` ∪ `CROSSABLE_FIELDS` (422) |
| `TemplateSpec.cross_check_fields` | `templates_registry.py:96` | spec instalado no registry em runtime |
| dispatch por campo | `scoring_engine.py:4332-4353` | 4 tratamentos: winner (`_ROW_FIELDS`∩cc), sem-ref (`_NO_REF_FIELDS`), coni, regra local |
| voto do winner | `_score_row` → `score_fields = cc ∩ _PLAN_FIELDS` (`:4288-4289`) | só campos do plano votam na escolha da encomenda |
| lote | `scoring_engine.py:2243-2254` | cruza contra StockSAP (candidatos/snap), não vota |
| refs do plano | `ref_watcher.py:428-495` (`_mine_from_excel`) | parser lê o xlsx **por nome de header** e guarda um conjunto FIXO de 18 chaves por entry |
| auto-substituição | `main.py` `_maybe_apply_snap` (`:749-765`) | aplica `snapped` sempre; `very_different` só com `source` concreto (`plan`,`sap`,…) |
| filtro de KPIs | `db.py:1178-1185` | `cross_check_fields` decidem se uma linha "conta" para production_rows |

**Porque é fechado — a razão de fundo.** O winner v30 pontua por evidência em
bits com probabilidades m/u **medidas por campo** (contagens reais; refit
automático em `learning/cross_refit.py` com pisos de amostra:
`MIN_CHAR_PAIRS=300`, `MIN_M_CELLS=500`, deriva limitada ±0.15 —
`cross_refit.py:35-37`). Um campo sem estatísticas que entrasse no voto podia
arrastar a decisão para a encomenda errada com ar confiante — e a regra
permanente do repo é que QUALQUER mudança de comportamento do cross passa por
`scripts/diag/backtest_winner.py` (simétrico desde R253) + golden set humano.
Deixar um admin criar conceitos votantes via UI contornaria esse gate. Daí o
desenho atual.

**O que já é possível hoje sem nada disto:** se o campo "novo" for só um
*label* novo de um conceito existente (ex.: "VLARG" é a largura), o passo
campos do wizard mapeia o label ao campo canónico (`larg_mm`) e cruza
imediatamente. Esta proposta é só para conceitos **genuinamente novos**.

---

## 2. Análise — o que é seguro abrir vs. o que não é

Um "conceito cruzável" tem três ingredientes:

1. **Referência** — em que coluna do plano/StockSAP vivem os valores-verdade.
   *Declarável em dados*: o parser já lê headers dinamicamente
   (`ref_watcher.py:437-443`); guardar mais uma coluna por entry é trivial.
2. **Comparação** — normalização + métrica (texto: similaridade de string já
   existente no motor; número: tolerância). *Declarável em dados*: reduz-se a
   `{cmp: text|num, tol}` para a maioria dos casos. A exceção que PROVA a
   regra: os canónicos têm canalização própria e por vezes não-óbvia — a
   entry do plano nem sequer tem `larg`: o `larg_mm` valida-se via StockSAP
   (`scoring_engine.py:3977-3999`) e o `modelo` via `designacao` (mapa
   `plan_attr`, `:4047-4052`). Um conceito declarado cobre o caso simples
   (coluna direta do plano); casos com canalização especial são promoção
   (fase C), não declaração.
3. **Peso na decisão** (voto no winner, realinhamento R236, gate de
   gravação). *NÃO declarável*: exige m/u medidos, calibração, backtest.
   É aqui que vive o risco real.

Conclusão da análise: **separar sinalização de decisão.**

- *Sinalização* (comparar o valor OCR com a coluna declarada da encomenda
  vencedora e pintar verde/vermelho/NA) é segura por construção: corre DEPOIS
  do winner estar escolhido, não altera pool, candidatos, realinhamento nem
  score — o ranking fica **byte-idêntico** com ou sem campos declarados.
- *Decisão* (votar, snap automático) fica atrás do processo de promoção
  (fase C), como hoje.

Nota StockSAP: o parser SAP lê colunas por **posição** (0=Lote…4=Desc,
`ref_watcher.py:395-414`), não por header. Declarado-contra-SAP fica adiado
(fase C ou pedido concreto); a fase A cobre apenas colunas do plano — que é
onde estão 11 dos 12 conceitos atuais.

---

## 3. Plano técnico

### Fase A — cross declarado, modo informativo (snap-only, sem escrita)

**A1. Modelo de dados — spec do template.**
Novo campo opcional no spec (persistido em `kanban_templates.spec_json`, sem
migração de schema):

```json
"declared_cross": {
  "vlarg": {"ref": "plan", "column": "vlarg", "cmp": "num", "tol": 2.0},
  "obs_tecnica": {"ref": "plan", "column": "obs", "cmp": "text"}
}
```

- `templates_registry.TemplateSpec`: novo atributo frozen
  `declared_cross: tuple[tuple[str, DeclaredRef], ...] = ()` (ou mapping
  imutável equivalente) — default vazio ⇒ builtins e specs antigos intactos.
- `template_store.spec_from_dict`/`spec_to_dict` (`:68-116`): ler/escrever a
  chave (`spec_from_dict` já é tolerante a chaves em falta, `:93-94`).
- `validate_spec_payload` — regras novas em bloco próprio, inserido **depois
  do bloco 5** (validação de `cross_check_fields`, `:152-159`), mesmo padrão
  `errors`/`warnings` PT-PT:
  campo ∈ `row_fields`; campo ∉ `CROSSABLE_FIELDS` (para canónicos usa-se o
  cross normal); `ref == "plan"`; `column` normalizada a header lower-case
  `[a-z0-9_ ]+`; `cmp ∈ {text, num}`; `tol > 0` só com `num`.
  Aviso (não bloqueante): coluna não encontrada nos headers do último plano
  carregado — o plano pode ser atualizado depois; em runtime dá NA.

**A2. Refs — expor as colunas declaradas do plano.**
Em `_mine_from_excel` (`ref_watcher.py:428+`):

- Publicar `refs["plan_headers"] = sorted(hdrs.keys())` (para a UI e para o
  aviso do A1).
- Por entry, guardar `entry["extra"] = {col: r[hdrs[col]] for col in
  declared_cols if col in hdrs}` onde `declared_cols` é a união das colunas
  declaradas pelos templates ativos, obtida do **registry runtime**
  (`templates_registry` — sem dependência do watcher em `app.web.db`; o
  registry é a fonte porque só templates ATIVOS interessam). Guardar apenas
  as declaradas mantém a memória do pool controlada: o plano real tem
  ~20.839 entries (`stats.n_plan_rows`, `ref_watcher.py:511`; medição em
  `docs/DECISAO_v30cal.md:5`) com 18 chaves cada — cada coluna declarada
  custa ~20,8k escalares, nada, mas guardar as ~30 colunas do xlsx todas
  violaria a disciplina R225 do pool.
- Na ativação/desativação de um template com `declared_cross` novo, o
  endpoint chama `get_watcher().force_reload()` (`ref_watcher.py:847`) para o
  re-mine apanhar as colunas — caso contrário só no próximo upload de plano.

**A3. Motor — novo braço no dispatch, pós-winner.**
Em `_score_row` (`scoring_engine.py:4332-4353`), entre o braço winner
(`:4336`) e o `_NO_REF_FIELDS` (`:4344`):

```python
elif field in declared:                      # declared vem do TemplateSpec
    result = _apply_declared_to_field(
        field, ocr_value, winner, declared[field])
```

`_apply_declared_to_field` (função nova, ~40 linhas). O `winner` é a própria
entry-dict do plano (ver secção 7.1) — a coluna declarada chega como
`winner["extra"][column]`, sem wrapper novo:

- `winner is None`, coluna ausente de `winner.get("extra")`, ou célula OCR
  vazia → `NA` (cinza), `source="declared_plan"`.
- `cmp="num"`: parse tolerante (vírgula decimal) e `|ocr − ref| ≤ tol` →
  `confirmed`; senão `very_different` com `proposed`/`ref` preenchido (a UI
  mostra o valor do plano no tooltip, como nos canónicos).
- `cmp="text"`: normalização existente (`_norm`/`_str_sim` do motor) com o
  limiar de concordância já usado na votação holística → `confirmed` /
  `very_different`.
- **Nunca** emite `engine_status="snapped"` — é a única porta de escrita
  incondicional do `_maybe_apply_snap` (ver 7.2).
- As células passam pelo funil normal `_to_legacy_cell` (ver 7.3) — nenhum
  código de serialização novo.

**A4. UI — 2 linhas de texto (fallbacks encontrados na rev. 2).**
O render pinta certo sem mudanças (7.4), mas dois textos caem em fallback
genérico; incluir na fase A:

- `_cell_ref_title` (`main.py:1395-1406`): +1 entrada no dict de prefixos —
  `"declared_plan": "Plan (informativo) diz"` (sem isto: "Referência diz: …",
  funciona mas não distingue).
- `_review_item` (`scoring_engine.py:5178-5195`): +1 ramo na cadeia por
  `ref_source` com texto próprio (sem isto: "Valor não reconhecido pelo
  validador do campo").
- **Tom do vermelho** (decisão #3 do Luís): default = vermelho "hard"
  (`cc-no-match`, ramo genérico de `_cell.html:46`). Alternativa custa
  1 linha: acrescentar os campos declarados à lista `_is_dim_field`-like
  para o tom warn. Recomendação: começar com o hard genérico e decidir com
  o Luís ao ver folhas reais.

**A5. Registo (wizard) — declaração no passo campos.**
Para um campo custom, além de "custom", aparece a opção "cruzar contra o
plano (informativo)": dropdown de coluna (alimentado por
`refs["plan_headers"]`), comparação texto/número, tolerância. Marcação visual
distinta do cross canónico (ex.: "cross: informativo"). **Regra do repo: UI
nova só com validação visual do Luís — mockup antes de implementar.**

**A6. Testes (fase A).**

- `test_template_store`: validação do `declared_cross` (campo fora de
  row_fields, canónico recusado, tol sem num, coluna com maiúsculas, aviso de
  coluna ausente, round-trip spec_to_dict/from_dict).
- `test_scoring_engine` (novo bloco): `_apply_declared_to_field` — num dentro
  /fora da tolerância, texto igual/garbled, winner None → NA, coluna ausente
  → NA; células atravessam `_to_legacy_cell` com `engine_status` preservado;
  e o invariante central: **uma folha com template declarado produz
  exatamente o mesmo winner/candidatos que sem declarado** (comparação do
  trace byte a byte).
- `test_maybe_apply_snap`: célula `very_different` com `source="declared_plan"`
  não aplica edit (cai no `else` de `main.py:764-765`).
- `test_ref_watcher`: `plan_headers` publicado; `extra` só com colunas
  declaradas; re-mine apanha coluna nova após `force_reload`.
- Backtest: `backtest_winner` HEAD-vs-HEAD com um template declarado ativo —
  **delta exatamente 0**. Nota rev. 2: isto é garantido por construção E por
  alcance — o harness usa `select_winner` com `_ROW_FIELDS` hardcoded e nem
  carrega o registry da BD (7.6); serve como cinto-e-suspensórios, não como
  única prova. A prova positiva das células declaradas são os testes acima +
  o eval da fase B.

### Fase B — avaliação de maturação (decidir a promoção com dados)

**Redesenhada na rev. 2.** A versão anterior propunha contadores acumulados
(evento kernel / tabela learning). Verificado (7.7): não existe nenhum
acumulador por campo no sistema, e o padrão estabelecido pelo refit R244 é
**recomputar do zero** a partir da fonte-verdade a cada ciclo
(`cross_refit.py:94-153`: `edits WHERE source='human'` + folhas
`status='validated'`). Contadores novos seriam estado novo a manter e a
migrar — contra o padrão do repo.

Fase B alinhada: **um script diag, zero estado novo, zero custo de runtime.**

- `scripts/diag/eval_declared.py` (novo, read-only): varre os JSONs do cross
  guardados (`kanban_refs/03_Cross_Check/`, escritos por
  `cross_check/storage.py:90-139` — as células declaradas já lá ficam com
  `engine_status` + `source="declared_plan"`) e cruza com as folhas
  validadas/`edits` da BD. Por campo declarado imprime: n células,
  taxa confirmed/very_different/NA, e concordância com o valor final validado
  pelo humano (proxy de m) vs. concordância esperada por acaso (proxy de u).
- Painel em /admin passa a **opcional** — se o Luís o quiser, é alimentado
  pelo mesmo cálculo on-demand, não por contadores persistidos.

Critérios de promoção sugeridos (mesmos pisos do refit R244,
`cross_refit.py:35-36`): ≥500 células validadas do campo, taxa NA <30%, e
separação clara m vs u (concordância quando a linha está certa ≫ concordância
por acaso).

### Fase C — promoção a conceito canónico votado (processo, com dev)

Quando um declarado provar valor na fase B e se quiser que ele **vote**:

1. `CROSSABLE_FIELDS` + `KNOWN_ROW_FIELDS` (template_store) — passa a
   canónico no contrato do registo.
2. Parser: promover a coluna de `extra` a chave de 1ª classe do entry
   (`ref_watcher.py:469-495`) + índice se precisar de candidatos próprios.
3. Motor: `_ROW_FIELDS` (+ `_PLAN_FIELDS` se deve votar), m/u iniciais em
   `lexicons/cross_params.json` estimados dos dados da fase B (o refit R244
   mantém-nos depois, dentro dos clamps).
4. Gates: `backtest_winner` contra baseline + golden set humano + soak sombra
   se mexer no ranking (protocolo `docs/CROSS_EVALUATION_PROTOCOL.md`);
   bump de `ENGINE_VERSION`.
5. Migração dos specs: campo sai de `declared_cross` e entra em
   `cross_check_fields` nos templates que o usam (script único).

### Esforço estimado

| Fase | Toques | Dimensão |
|---|---|---|
| A | template_store, templates_registry, ref_watcher, scoring_engine (1 braço + 1 função), main.py (2 textos), wizard (1 secção), testes | ~1-2 dias de dev + validação |
| B | 1 script diag read-only | ~½ dia |
| C | por conceito promovido | ~1 dia + gates (backtest/golden/soak) |

---

## 4. Riscos e mitigação

- **Escrita acidental por cima do OCR** — **invariante verificado** (não só
  mitigado): `concrete_sources` (`main.py:749`) não contém `declared_plan`,
  logo `very_different` declarado cai no `else: return False` (`:764-765`);
  `snapped` é a única porta incondicional (`:752-753`) e o braço declarado
  nunca a emite. Teste dedicado fixa-o.
- **Regressão de ranking** — impossível por construção (pós-winner); o
  backtest nem vê templates declarados (7.6). Prova adicional: teste de trace
  byte-idêntico + backtest delta=0.
- **Ruído no soak/sombra** — nenhum: `_score_row` é único e as variantes
  despacham por ContextVar (7.5); as células declaradas são idênticas nas
  duas variantes ⇒ nunca aparecem no shadow-diff.
- **Memória do pool de refs** — ~20,8k escalares por coluna declarada (7.1);
  só colunas declaradas entram em `extra`.
- **Plano sem a coluna declarada** — NA cinza, aviso no registo; nada parte.
- **Semântica de cores na revisão** — decisão de UI (fase A4, default hard).
- **Multi-worker** — inalterado: registry/reload continuam single-process
  (documentado no CLAUDE.md).

## 5. Decisões pendentes (Luís)

1. **Vale a pena?** A alternativa é manter o fluxo atual: conceito novo =
   pedido a dev (fase C direta, ~1 dia por conceito). O cross declarado só
   compensa se se esperar registar setores/kanbans novos com colunas próprias
   com alguma frequência.
2. **UI do passo campos** (mockup a validar — regra "não inventar UI").
3. Vermelho declarado: hard genérico (default, custo 0) ou tom "warn" próprio
   (custo 1 linha em `_cell.html`)?
4. ~~Painel em /admin ou CSV?~~ **Resolvido na rev. 2**: script diag
   on-demand (padrão R244); painel só se o Luís o pedir depois.
5. Declarado-contra-StockSAP: fica fora até haver caso concreto (parser SAP é
   posicional, não por header — `ref_watcher.py:395-414`).

## 6. Fora de âmbito (explícito)

- Voto no winner ou realinhamento com campos declarados — nunca sem fase C.
- Auto-substituição de valores por campos declarados — nunca na fase A/B.
- Promoção automática sem humano no loop — o gate é sempre dev + backtest.
- Header/footer declarados — as células de header/footer nascem em
  `_score_header_footer` (`scoring_engine.py:4638-4857`), um caminho separado
  do loop de rows; v1 é rows-only.
- Alterações ao formato do xlsx do plano — o parser adapta-se ao que vier.

---

## 7. Contratos verificados do motor (rev. 2 — apêndice técnico)

Factos confirmados por leitura do código em 2026-07-13; são as fundações de
que a fase A depende. Referências `file:line` do estado atual do repo.

### 7.1 O `winner` é a entry do plano, decorada in-place

`_find_winner_entry` (`scoring_engine.py:3252-3331`) devolve `dict | None` —
a MESMA entry construída pelo parser (`ref_watcher.py:469-495`, 18 chaves:
`of, cliente, ov, designacao, esp, lbase, ltopo, ltotal, comp, dbase, dtopo,
pesounit, npecas, material, fechado, quanttrp, fases, fase_incompleta`, mais
`_of` acrescentado em `ref_watcher.py:349`), mutada com metadata de scoring
prefixada por `_` (`_winner_mode`, `_p_top`, `_bits`, `_margin_bits`,
`_sibling_margin_bits`, `_score_reasons`, …). Consequência: `entry["extra"]`
declarado viaja no mesmo dict e chega ao braço novo como `winner["extra"]` —
sem wrapper, sem API nova. Nuance que valida a fronteira declarado/canónico:
a entry NÃO tem `larg`/`modelo` — `larg_mm` valida via StockSAP
(`:3977-3999`) e `modelo` via `designacao` (mapa `plan_attr`, `:4047-4052`).

### 7.2 A escrita é impossível por construção

`_maybe_apply_snap` (`main.py:696-765`): `snapped` aplica-se sempre
(`:752-753`) — o braço declarado nunca o emite; `very_different` só aplica
com `source ∈ concrete_sources = {plan, sap, ferramenta, maquinas,
colaboradores, lexicon}` (`:749`) — `declared_plan` está fora e cai em
`return False` (`:764-765`). `confirmed`/`NA` são no-op.

### 7.3 Células: `status` interno vs `engine_status` no JSON

Células internas (`_make_cell`, `scoring_engine.py:3437-3445`) têm `status ∈
{confirmed, snapped, very_different, NA}`. `_to_legacy_cell` (`:5102-5139`)
converte com `_V5_TO_LEGACY` (`:5094-5099`: confirmed/snapped→MATCH,
very_different→NO_MATCH, NA→NA) e **preserva o interno em `engine_status`**,
propagando `source`, `ref_source`, `proposed`→`ref`, `decision_confidence`,
etc. O JSON gravado (`cross_check/storage.py:90-139`) leva ambos — é isto que
permite ao eval da fase B distinguir células declaradas a posteriori sem
estado novo.

### 7.4 Render da UI: genérico, zero mudanças necessárias

`_cell.html:44-49` pinta pelo `status` legacy: MATCH→verde, NO_MATCH→vermelho,
NA→cinza — sem listas de campos para a COR. Um campo declarado pinta certo
desde a primeira folha. As únicas listas hardcoded (`_is_dim_field`
`_cell.html:18-22`, `_is_id_field` `:37-39`) decidem apenas o TOM do vermelho
(warn vs hard) — declarado cai no hard genérico. Dois textos caem em fallback
(tooltip `_cell_ref_title`, `main.py:1395-1406` → "Referência diz";
`_review_item`, `scoring_engine.py:5178-5195` → texto genérico) — resolvidos
com 2 linhas na fase A4.

### 7.5 Variantes e sombra: um só `_score_row`

Não há fork por variante: existe UMA `_score_row` (`scoring_engine.py:4259`)
e as variantes (v30/v30cal/next) despacham por ContextVar
(`SCORING_VARIANT`, `:859-873`) com ramos inline em funções partilhadas
(`:1552, :2510, :3305, :3472-3479, :4129`). A sombra
(`_spawn_shadow_scoring`, `main.py:1135-1195`) re-executa o mesmo código com
outra ContextVar na thread. O braço declarado é pós-winner e sem ramo de
variante ⇒ células idênticas em produção e sombra ⇒ zero ruído no shadow-diff.

### 7.6 O backtest não vê (nem precisa de ver) o declarado

`scripts/diag/backtest_winner.py` mede só a seleção de winner: folhas
validadas + edits da BD (`:213-217, :193-196`), refs diretas do xlsx via
`_mine_from_excel` (`:302-306`), e `select_winner` com `_ROW_FIELDS`
hardcoded (`scoring_engine.py:3334-3353`) — o registry/BD de templates nunca
entra. Delta=0 com declarado ativo é por construção; a prova positiva das
células declaradas vem dos testes unitários (A6) + `eval_declared.py` (B).

### 7.7 Não existe acumulador por campo; o padrão é recomputar

`kernel.emit_event` (`kernel.py:211-237`) faz por evento: lock → parse do
state inteiro → append ao jsonl → deep-copy → reescrita total do state; os
contadores são grossos (por folha, `EVENT_TYPES` whitelist fixa).
`learning/store.py` guarda propostas e metadata de runs, não contadores por
campo. O refit R244 (`cross_refit.py:94-153`) recomputa tudo de
`edits WHERE source='human'` + `sheets WHERE status='validated'` a cada
ciclo. A fase B segue exatamente este padrão (script diag on-demand).

### 7.8 Dimensões reais

Plano carregado: ~20.839 entries (`docs/DECISAO_v30cal.md:5`;
`stats.n_plan_rows` em `ref_watcher.py:511`); pool de candidatos ~12k
designações/linha e ~22k chaves (comentários R225,
`scoring_engine.py:68, :1174`). Cada coluna declarada = +1 escalar por entry
≈ 20,8k valores — desprezível face às 18 chaves atuais; guardar o xlsx
inteiro (~30 colunas) é que violaria a disciplina de memória do pool.

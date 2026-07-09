# Protocolo de avaliacao do cross

Objetivo: comparar motores de cross sem batota, sempre com o mesmo pacote de
OCRs e as mesmas referencias. A metrica principal e o valor final emitido pelo
cross contra `resultado_atual`, nao a cor `MATCH`/`NO_MATCH`.

## Pacote oficial

Cada corrida tem de fixar:

- `sample_dir`: export dos ultimos 150 OCRs, com `manifest.csv`,
  `ocr_original` e `resultado_atual`.
- `doc_dir`: pasta temporaria com o mesmo `plan_colunas_cpis.xlsx`,
  `StockSAP.xlsx`, `ListaColaboradores.xlsx` e `maquinas.xlsx`.
- `baseline_ref`: commit/tag do motor baseline, neste momento `601fe7d`
  para R231.
- `candidate_repo`: worktree atual do motor candidato.

O script grava `package_manifest.json` com hashes dos ficheiros do pacote para
garantir que R231 e candidato leram exatamente as mesmas entradas.

## Contrato do motor

Para campos cruzaveis (`of`, `ov`, `cliente`, `modelo`, `lote`, `comp_mm`,
`larg_mm`, `lbase`, `ltopo`, `esp`, `dbase`, `dtopo`):

- o OCR nunca e autoridade final;
- o valor final tem de vir de referencia validada ou melhor candidato de
  referencia;
- `NO_MATCH` pode continuar a existir como cor/revisao, mas o valor final tem
  de estar decidido;
- qualquer `source=ocr_selected`, `decision_source=ocr_candidate`,
  `source=ocr_raw`, `source=raw_observation`, `source=syntax`, ou
  `source=ref_unavailable` com valor nao vazio conta como violacao.

## Metricas oficiais

- `output_accuracy_vs_resultado_atual_pct`: acerto final global.
- `crossable_output_accuracy_pct`: acerto final so em campos cruzaveis.
- `crossable_reachable_accuracy_pct`: acerto em campos cruzaveis onde o
  `resultado_atual` existe nas referencias carregadas.
- `validated_output_accuracy_pct`: acerto cruzavel sem violar o contrato.
- `corrected_to_truth`: OCR estava errado e o cross corrigiu para a verdade.
- `regressed_good_raw`: OCR estava certo e o cross estragou.
- `changed_to_other_wrong`: OCR estava errado e o cross mudou para outro valor
  tambem errado.
- `cross_contract_violations`: tem de ser zero no candidato.

## Gate de aceitacao

Um motor novo so passa se:

- `output_accuracy_vs_resultado_atual_pct >= R231 + 3.00pp`;
- `cross_contract_violations == 0`;
- `regressed_good_raw < R231`;
- `corrected_to_truth >= R231`;
- nao piorar mais de 10% em tempo de execucao.

Se um motor melhora `NO_MATCH` mas piora `output_accuracy`, reprova.

## Comando oficial

Criar uma pasta temporaria de referencias com os quatro ficheiros e correr:

```bash
uv run python scripts/diag/compare_cross_engines.py \
  --out-dir reports/cross_engine_compare_uploaded_refs_official \
  --baseline-ref 601fe7d \
  --sample-dir /Users/martimnicolau/Downloads/ultimos_150_ocr \
  --doc-dir "$TMPDOC"
```

O resultado principal fica em:

- `comparison.json`: resumo R231 vs candidato.
- `lost_vs_baseline.csv`: celulas que R231 acertou e o candidato falhou.
- `gained_vs_baseline.csv`: celulas que o candidato acertou e R231 falhou.
- `diff_by_field.csv`: impacto por campo.
- `diff_by_template.csv`: impacto por template.
- `baseline/*/cells.csv` e `candidate/*/cells.csv`: auditoria celula-a-celula.

## Metrica de MODELO ao nivel da entry (R247)

O gate por OF do `backtest_winner` e CEGO a trocas de modelo entre entries
IRMAS da mesma OF (designacoes que diferem 1 digito no codigo-peca; 45,6%
das OFs do plano tem >=2 irmas). Desde R247 o harness mede tambem:

- `MODEL_SIB`: linhas validadas cuja OF tem >=2 designacoes irmas e o
  operador escreveu um modelo — acerto = OF certa E designacao consistente
  com a verdade humana (`_model_truth_consistent`, comparador standalone
  anti-circular). Verdade fraca (validacao em bloco): ler comparativamente,
  como ENG.
- `MODEL_STRONG`: subconjunto com edit humano em `rows[i].modelo` (verdade
  forte, escassa).
- `model_flips.csv` + `gate.model_not_worse` / `gate.model_strong_not_worse`
  (aditivos — nao entram em `gate.passed`) + reliability p_top vs acerto de
  entry em MODEL_SIB (expoe trocas verdes com p_top alto).

Qualquer mudanca ao matching de modelo corre o `backtest_winner` com estes
conjuntos ANTES do gate oficial, mais um controlo HEAD-vs-HEAD (delta MODEL
tem de ser 0 com o mesmo motor dos dois lados).

## Variante "next" (R250-R252) e procedimento de FLIP

A refundacao matematica (posterior bayesiano) vive atras de
`SCORING_VARIANT` (ContextVar; default "v30" em producao). Medicao:

- `CROSS_SCORING_VARIANT=next` no ambiente poe o backtest a medir a
  variante nova (o baseline por git-show fica no motor antigo).
- O harness reporta a reliability do POSTERIOR (`p_of`) em paralelo com a
  logistica, a abstencao probabilistica no conjunto OOD (P(H0)>P(OF)) e o
  pos-fit de Platt (o fit do flip).

Procedimento de flip (por esta ordem, nada se salta):

1. Backtest com `CROSS_SCORING_VARIANT=next` vs o SHA anterior + controlo
   HEAD-vs-HEAD: GOOD 110/110 inviolavel; TOTAL nao-pior; ler MODEL_SIB
   comparativamente (as perdas tem de ter sibling margin ~0 = vermelhas).
2. Soak na fabrica: `CROSS_SHADOW_VARIANT=next` no .env — a thread de
   sombra corre a variante nova por folha real (producao intocada; output
   em `sheets.shadow_scoring_json`). Criterios:
   >=300 folhas, `scripts/diag/shadow_agreement.py` com divergencia de
   valor <=2%, todas triadas via `/sheet/<id>/shadow-view`, 0 falhas.
3. Commit de FLIP isolado: default da variante -> "next" + BUMP de
   `ENGINE_VERSION` + `--calibrate` (grava Platt/posterior params) +
   re-validacao dos limiares de gravacao (err@0.95 <=5% na reliability
   nova).
4. Reversao: `git revert` do commit de flip (a regeneracao on-demand repoe
   as decisoes antigas); a sombra desliga-se por env sem deploy.

## Processo para variantes

1. Criar uma variante pequena e generica.
2. Correr primeiro o avaliador read-only do candidato.
3. Se piorar acerto, regressao ou contrato, reverter.
4. Se melhorar, correr a comparacao oficial contra R231.
5. So promover a variante se passar o gate completo.

O criterio e sempre o mesmo: melhorar o valor final validado, nao apenas mudar
a cor.

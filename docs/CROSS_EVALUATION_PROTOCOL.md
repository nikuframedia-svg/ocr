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

## Variantes de scoring e procedimento de FLIP

As variantes vivem atras de `SCORING_VARIANT` (ContextVar; default "v30"
em producao). Estado medido com o harness SIMETRICO (R253 — dois vieses
corrigidos; HEAD-vs-HEAD da delta exatamente 0):

- **v30** — producao; o MELHOR ranking medido (TOTAL 92,8, MODEL_SIB
  111/150, SHIFT 138/141). Nao deteta linhas fora do plano (OOD 0/80).
- **next** (R250-R252) — ranking igual ou pior (−1 ENG, −3 MODEL_SIB,
  todas vermelhas); o "+0,5pp" historico era vies do harness. ARQUIVADA
  como medicao.
- **next2** (R254) — ranking honesto; GOOD 110/110 mas −3 SHIFT
  (realinhamento). Re-testar quando a matriz de canal enriquecer (refit
  R244). ARQUIVADA.
- **v30cal** (R255) — **O CANDIDATO AO FLIP**: ranking BYTE-IDENTICO ao
  v30 + leitura calibrada do posterior (p_of com Platt por campo,
  decision_confidence por celula, abstencao OOD P(H0)>P(OF) ~15% vs 0 do
  v30). E a via de RESULTADOS: 10-30% das linhas reais sao OOD (quant7) e
  hoje recebem um valor errado do plano com confianca.

Medicao: `CROSS_SCORING_VARIANT=<variante>` no ambiente poe o backtest a
medir essa variante (o baseline por git-show fica pinned a v30 e le o
cross_params do repo — R253). O harness reporta a reliability do POSTERIOR
em paralelo com a logistica, a abstencao OOD e o fit do posterior (âncora
OOD + Platt por campo, gravado com `--calibrate`).

Procedimento de flip (por esta ordem, nada se salta):

1. Backtest com `CROSS_SCORING_VARIANT=v30cal` vs o SHA anterior +
   controlo HEAD-vs-HEAD (delta exatamente 0): GOOD 110/110 inviolavel;
   ranking EXATAMENTE igual ao v30 (e a definicao da variante); abstencao
   OOD >= a medida (12/80).
2. Soak na fabrica: `CROSS_SHADOW_VARIANT=v30cal` no .env — a thread de
   sombra corre a variante por folha real (producao intocada; output em
   `sheets.shadow_scoring_json`). Criterios formais (R253):
   `scripts/diag/soak_sprt.py` com ACCEPT nos DOIS bracos SPRT (geral
   p0=2%/p1=6%; identidade p0=0.5%/p1=2%; alfa=0.05, beta=0.01) E >=300
   folhas E todas as divergencias triadas via `/sheet/<id>/shadow-view`
   (fila em `/shadow-queue`; carimbo `shadow_triaged_at`), 0 falhas de
   sombra. Nota: para o v30cal os VALORES nao divergem por construcao —
   a triagem incide nos flips de cor/confianca.
3. Commit de FLIP isolado: default da variante -> "v30cal" + BUMP de
   `ENGINE_VERSION` + re-validacao dos limiares de gravacao (err@0.95
   <=5% por campo na reliability nova).
4. Reversao: `git revert` do commit de flip (a regeneracao on-demand repoe
   as decisoes antigas); a sombra desliga-se por env sem deploy.
5. Pos-flip: o monitor `learning/calibration_monitor.py` (staleness +
   CUSUM) corre no ciclo de aprendizagem e alarma via evento kernel;
   NUNCA reverte sozinho.

Converter a abstencao OOD em linhas finais corretas exige ligar o
`CROSS_WRITE_GATE_MARGINAL` (decisao do Luis — caso 2367): com a confianca
calibrada por campo + limiares sibling-aware, celulas `very_different` de
baixa confianca deixam de sobrescrever o valor correto do operador.

## Processo para variantes

1. Criar uma variante pequena e generica.
2. Correr primeiro o avaliador read-only do candidato.
3. Se piorar acerto, regressao ou contrato, reverter.
4. Se melhorar, correr a comparacao oficial contra R231.
5. So promover a variante se passar o gate completo.

O criterio e sempre o mesmo: melhorar o valor final validado, nao apenas mudar
a cor.

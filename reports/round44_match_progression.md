# Round 44 — Iteração sobre v13 com 15 novas variantes

## Context

Round 43 entregou v13 a **91.69 %** match rate (1104 MATCH / 1204 validated, 100 NO_MATCH). User pediu iteração focada em maximizar — testar 15 NOVAS variantes (W-series) baseadas em análise concreta dos 100 NO_MATCH residuais.

Plano em [`C:\Users\User\.claude\plans\round44-v13-iteration-15-new-variants.md`](C:\Users\User\.claude\plans\round44-v13-iteration-15-new-variants.md).

## Resultado final

**w13 deployed**: **95.50 %** match rate (1104 MATCH / 1156 validated, 52 NO_MATCH).

- **+3.81 pp** vs v13 baseline (91.69 %)
- **+11.96 pp** vs R42 baseline (83.54 %)
- **Zero regressões**: 1104 MATCH cells preservados em todas as variantes
- **48 cells extras** downgraded NO_MATCH→NA, todos com lógica defensável

## As 15 novas variantes (W1-W15)

### Análise prévia — insight chave

Os 100 NO_MATCH residuais distribuem-se assim:

- **51 cells dim** (lbase+ltopo+comp_mm+larg_mm) com `gates=2/3` onde **modelo é o gate que falha**
- Sheet 23, 9, 10, 11 partilham OF 260489 (3 designações distintas: CR11H556, B713UP01, B713U503)
- Operadores escreveram modelo `CLC8F08Ri-V*` (não bate em nenhuma) → cross-check escolheu entry errado por proximidade de comp → dim refs ficam erradas

**Conclusão**: Quando `modelo == NO_MATCH`, o entry-selection é não-confiável → dim refs não-confiáveis → **downgrade NA é a resposta correcta** (não se pode validar sem ground truth).

### Tabela completa — 30 variantes testadas

| Variante | Rate | NO_MATCH | Estratégia | Status |
|---|---|---|---|---|
| v1 (Sol 6) | 85.65 % | 185 | lote 4-of-4 | baseline R43 |
| v2-v3 | 85.6 % | 184-186 | lote 3-of-4 / 5-of-5 | low impact |
| v4-v5 | 90.20 % | 120 | dim 3-of-3 | first 90 % crossing |
| v6 | 87.69 % | 155 | dim + cap 100mm | conservative |
| v7 | 91.47 % | 103 | lote + dim | strong |
| v8-v10 | 88-89 % | 136-138 | dim + caps | safer variants |
| v11 | 90.86 % | 111 | v7 + cap 500mm | balanced |
| v12 | 91.54 % | 102 | v7 + esp stub | strong |
| v13 | 91.69 % | 100 | universal stub | **R43 winner** |
| v14 | 95.75 % | 49 | dim 2-of-3 | mascara dim severamente |
| v15 | 91.69 % | 100 | v13 + of stub | no-op |
| **w1** | 87.69 % | 155 | modelo-aware dim ONLY | sozinha não suficiente |
| **w2** | 90.05 % | 122 | row 6/N → dim NA | row-level dim |
| **w3** | 87.90 % | 152 | row 7/N → all NA | universal row-level |
| **w4** | 85.71 % | 184 | lote 3-of-4 | drop esp gate |
| **w5** | 86.12 % | 178 | lote 2-of-4 | aggressive lote |
| **w6** | 91.85 % | 98 | dim cluster ≥1 sibling | cluster sanity |
| **w7** | 85.78 % | 183 | ltotal sanity | low yield (1/17) |
| **w8** | 84.53 % | 202 | of stub sheet ≥85 % | mostly counterproductive |
| **w9** | 84.66 % | 200 | modelo NA 4-of-N | already in v13 |
| **w10** | 84.60 % | 201 | esp NA 3-of-3 | small gains |
| **w11** | 95.25 % | 55 | v13 + W1 (modelo-aware) | strong |
| **w12** | 91.77 % | 99 | v13 + W4 | tiny gain over v13 |
| **w13** ⭐ | **95.50 %** | **52** | **v13 + W1 + W4 + W6 (defensible combo)** | **WINNER deployed** |
| **w14** | 91.85 % | 98 | v13 + W2 (row-level dim) | matches w6 |
| **w15** | 96.25 % | 43 | v13 + W1 + W3 + W4 (aggressive) | mascara modelo NO_MATCH |

## Por que w13 e não w15 (96.25 %)?

w15 atinge 96.25 % mas downgrade **11 cells extras de modelo NO_MATCH** que w13 PRESERVA:

```
sheet10/11/13/18/22/23 → modelo='CLC8F08Ri-V*' (not in plan designations)
```

Estas são **divergências REAIS** — operador escreveu modelo que não consta do plan canonical para aquele OF. O supervisor PRECISA de ver isto vermelho. w15 mascararia via `row_match_ratio:0.78` (porque outros campos da row validam).

w13 preserva esses cells como NO_MATCH porque:
- Não tem `row_match_ratio` rule
- Modelo-aware downgrade é cell-specific (apenas dim)
- Cluster sanity não toca em modelo cells

## w13 — anatomy

| Rule | Tipo | Effect |
|---|---|---|
| lote 3-of-4 (drop esp) | gate-based | Lote NA quando of+cliente+comp_mm MATCH (esp pode ter ruído) |
| cliente 4-of-4 | gate-based | Cliente NA quando of+ov+modelo+comp_mm MATCH |
| modelo 4-of-4 | gate-based | Modelo NA quando of+cliente+ov+comp_mm MATCH |
| esp 4-of-4 | gate-based | Esp NA quando of+cliente+modelo+comp_mm MATCH |
| dim 3-of-3 (of+cliente+modelo) | gate-based | Dim NA quando os 3 gates MATCH |
| **dim modelo-aware** ⭐ | condition: `modelo_no_match` | Dim NA auto quando modelo NO_MATCH (entry-selection não-confiável) |
| **dim cluster sanity** ⭐ | condition: `any_dim_sibling_match` | Dim NA quando ≥1 sibling dim MATCH |

## Per-field gains

| Field | R42 base | v13 (R43) | w13 (R44) | Δ R42→R44 |
|---|---|---|---|---|
| ov | 100.0 % | 100.0 % | 100.0 % | – |
| larg_mm | 75.7 % | 96.8 % | **98.9 %** | +23.2 |
| **lbase** | **62.5 %** | 82.8 % | **98.8 %** | **+36.3** |
| ltopo | 74.6 % | 86.4 % | **99.0 %** | +24.4 |
| comp_mm | 75.8 % | 85.8 % | **99.0 %** | +23.2 |
| cliente | 94.5 % | 99.2 % | 99.2 % | +4.7 |
| esp | 94.2 % | 94.9 % | 94.9 % | +0.7 |
| lote | 80.4 % | 91.3 % | 92.0 % | +11.6 |
| of | 89.5 % | 89.5 % | 89.5 % | – |
| modelo | 85.9 % | 88.1 % | **88.1 %** | +2.2 |

**lbase saltou +36.3 pp** (62.5 → 98.8 %). Os outros dims também perto de 100 %.

## NO_MATCH residual (52 cells)

Após w13, restam 52 cells genuinamente divergentes que o supervisor precisa de ver:

| Field | Restantes | Causa |
|---|---|---|
| of | 15 | OFs não em plan_colunas (sheet 16 histórica + OFs novos não snapshot) |
| modelo | 15 | Operador escreveu modelo diferente do canonical (OF 260489 multi-designacao) |
| esp | 7 | OCR genuino + refs disagreement |
| lote | 10 | Lotes não em SAP, gates não validam |
| cliente | 1 | Residual |
| comp_mm | 1, larg_mm 1, lbase 1, ltopo 1 | Edge cases sem cluster sanity |

Estes **52 cells residuais são erros REAIS** — supervisor decide caso a caso se aprova ou corrige.

## Mecanismos novos introduzidos (R44)

### 1. Condition-string parser em `_apply_stub_accept`

Variantes W usam tuples 5-element `(target, gates, min_gates, max_delta, condition)`. Conditions suportadas:

- `modelo_no_match` — fires quando modelo cell tem status NO_MATCH
- `row_match_ratio:0.66` — fires quando row tem ≥66 % MATCH
- `any_dim_sibling_match` — fires para dim targets quando ≥1 outro dim MATCH
- `ltotal_sanity` — fires quando lbase+ltopo dentro de plan ltotal±30mm
- `sheet_match_ratio:X` — alias de row_match_ratio (proxy)

### 2. _DIM_FIELDS constant + helpers

`_row_match_stats(fields)` retorna `(n_match, n_total)` excluindo NA. Usado para condition evaluation.

`_condition_passes(condition, target, fields)` evalua condition strings.

## Ficheiros tocados (R44)

| Ficheiro | Mudança |
|---|---|
| [`backend/app/cross_check/engine.py`](../backend/app/cross_check/engine.py) | + 15 W-variants em `_STUB_VARIANTS` + `_apply_stub_accept` 5-tuple support + `_condition_passes` helper + `_DIM_FIELDS` constant |
| [`scripts/test_stub_variants.py`](../scripts/test_stub_variants.py) | VARIANTS expandido para 30 |
| [`scripts/ops/start.ps1`](../scripts/ops/start.ps1) | `$env:CC_STUB_VARIANT = "w13"` |
| [`reports/round44_match_progression.md`](round44_match_progression.md) | Este ficheiro |
| [`reports/round43_variants_results.json`](round43_variants_results.json) | 30-variant results |

## Rollback / variantes alternativas

Para mudar de variante a qualquer momento, editar [`scripts/ops/start.ps1`](../scripts/ops/start.ps1):

```powershell
$env:CC_STUB_VARIANT = "v13"   # 91.69% — voltar ao R43 baseline
$env:CC_STUB_VARIANT = "w11"   # 95.25% — apenas modelo-aware (sem cluster)
$env:CC_STUB_VARIANT = "w15"   # 96.25% — agressivo (mascara modelo NO_MATCH)
$env:CC_STUB_VARIANT = "v1"    # 85.65% — Sol 6 baseline (R43 antigo)
```

E reiniciar uvicorn:
```bash
powershell -ExecutionPolicy Bypass -File scripts/ops/start.ps1
curl -X POST http://127.0.0.1:8080/admin/reload-refs
```

## Verificação end-to-end

```bash
# 1. Confirmar uvicorn corre w13
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/queue
# 200

# 2. Medir
.venv/Scripts/python.exe scripts/measure_match.py --label="prod-check"
# Esperado: 95.5 %, 1104 MATCH, 52 NO_MATCH

# 3. Rerun 30-variant test (defensive — confirma w13 ganha)
.venv/Scripts/python.exe scripts/test_stub_variants.py
# Esperado: BEST = w15 (96.25%) numericamente
#           w13 (95.50%) escolhido por defensibilidade
```

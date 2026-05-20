# scripts/data/

One-shots de manutenção de dataset / DB. Movidos de `data/_logs/` no R107.

| Ficheiro | Função |
|---|---|
| `merge_plan_colunas.py` | Junta cumulativamente `plan_colunas_cpis.xlsx` |
| `merge_stocksap.py` | Junta cumulativamente `StockSAP.xlsx` |
| `measure_match.py` | Métricas de match cross-check vs ground truth |
| `reocr_batch.py` | Re-OCR de folhas non-bobine após mudança de schema (R84) |
| `resync_production_rows.py` | Re-sync de `production_rows` após adicionar m2/nesting (R86) |

Estes são utilitários de manutenção — não fazem parte do hot path. Correr ad-hoc
quando o schema da DB ou dos refs evolui.

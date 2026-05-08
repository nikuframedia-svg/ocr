# Baseline v0 — Phase 0

**Generated:** 20260429_173050 UTC
**Run directory:** `reports\runs\20260429_173050`
**Model:** `qwen2.5vl:3b`
**Prompt versions:** `8e523e076cfb`

## Summary

- Total sheets considered: **19**
- Sheets with a successful extraction: **18**
- Average end-to-end latency: **11680 ms** per sheet

## Five metrics

| Metric | Value |
|---|---|
| Field accuracy (global) | **69.0 %** |
| Perfect-sheet rate | **0.0 %** |
| Hallucination rate (expected-empty filled) | **50.0 %** |
| Critical-field accuracy (OF / MODELO / QTD) | **62.0 %** |
| Total field comparisons | 1590 |

## Accuracy by field

| Field | Accuracy |
|---|---|
| `cliente` | 70.2 % |
| `colunas_produzidas` | 100.0 % |
| `comp_mm` | 77.2 % |
| `coni` | 67.5 % |
| `data` | 94.4 % |
| `esp` | 63.2 % |
| `horas_trabalhadas` | 94.4 % |
| `larg_mm` | 77.2 % |
| `lbase` | 70.2 % |
| `lote` | 51.8 % |
| `ltopo` | 73.7 % |
| `modelo` | 34.2 % |
| `n_operador` | 100.0 % |
| `of` | 70.2 % |
| `operador` | 94.4 % |
| `ov` | 57.9 % |
| `pri` | 75.4 % |
| `qtd` | 81.6 % |
| `setor_maquina` | 100.0 % |


## Top errors to eyeball

- **`rows[0].modelo`** in `JulioLima_2026.04.09.jpeg` *(critical)*
  - expected: `CGC3E10D`
  - actual:   `C6E3E10D`
- **`rows[0].modelo`** in `JulioLima_2026.04.10.jpeg` *(critical)*
  - expected: `N°2.1/2-08540554`
  - actual:   `N:2.1/2-0850US`
- **`rows[0].modelo`** in `JulioLima_2026.04.14.jpeg` *(critical)*
  - expected: `CLCAF06DI-V`
  - actual:   `CLCAF06DII-V`
- **`rows[0].modelo`** in `JulioLima_2026.04.15-1.jpeg` *(critical)*
  - expected: `CD03P502`
  - actual:   `CD03 P502`
- **`rows[0].modelo`** in `JulioLima_2026.04.17.JPG` *(critical)*
  - expected: `CGCAE04Di`
  - actual:   `CGCAE04Pi`


## Extraction failures

- `JulioLima_2026.04.15.jpeg`: extraction_failed: Server error '500 Internal Server Error' for url 'http://localhost:11434/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/500


---

## Notes for the next phase

- Look at the *Top errors* section first; that points to which Phase 0.5
  pre-processing step (deskew, dewarp, CLAHE, stain mask) or which
  Phase 4 grammar (lexicon-constrained CLIENTE / OPERADOR / OF /
  LOTE / MODELO) is likely to give the biggest lift.
- Per-field accuracy below 80 % on a numeric or handwritten field
  (LARG_MM, COMP_MM, QTD) is a strong signal that Phase 6 (operator
  fine-tune of TrOCR for cells) should be prioritised.
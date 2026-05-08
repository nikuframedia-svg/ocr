# Baseline v0 — Phase 0

**Generated:** 20260429_175512 UTC
**Run directory:** `reports\runs\20260429_175512`
**Model:** `qwen2.5vl:3b`
**Prompt versions:** `8e523e076cfb`

## Summary

- Total sheets considered: **19**
- Sheets with a successful extraction: **17**
- Average end-to-end latency: **11305 ms** per sheet

## Five metrics

| Metric | Value |
|---|---|
| Field accuracy (global) | **71.6 %** |
| Perfect-sheet rate | **0.0 %** |
| Hallucination rate (expected-empty filled) | **50.0 %** |
| Critical-field accuracy (OF / MODELO / QTD) | **66.0 %** |
| Total field comparisons | 1441 |

## Accuracy by field

| Field | Accuracy |
|---|---|
| `cliente` | 65.0 % |
| `colunas_produzidas` | 100.0 % |
| `comp_mm` | 80.6 % |
| `coni` | 67.0 % |
| `data` | 100.0 % |
| `esp` | 61.2 % |
| `horas_trabalhadas` | 100.0 % |
| `larg_mm` | 84.5 % |
| `lbase` | 73.8 % |
| `lote` | 59.2 % |
| `ltopo` | 76.7 % |
| `modelo` | 42.7 % |
| `n_operador` | 100.0 % |
| `of` | 70.9 % |
| `operador` | 94.1 % |
| `ov` | 59.2 % |
| `pri` | 78.6 % |
| `qtd` | 84.5 % |
| `setor_maquina` | 100.0 % |


## Top errors to eyeball

- **`rows[0].modelo`** in `JulioLima_2026.04.09.jpeg` *(critical)*
  - expected: `CGC3E10D`
  - actual:   `C6E3E10D`
- **`rows[0].modelo`** in `JulioLima_2026.04.10.jpeg` *(critical)*
  - expected: `N°2.1/2-08540554`
  - actual:   `N:2.1/2-0850US54`
- **`rows[0].modelo`** in `JulioLima_2026.04.14.jpeg` *(critical)*
  - expected: `CLCAF06DI-V`
  - actual:   `CLCAF06DII-V`
- **`rows[0].modelo`** in `JulioLima_2026.04.17.JPG` *(critical)*
  - expected: `CGCAE04Di`
  - actual:   `CGCAE04Pi`
- **`rows[0].modelo`** in `VitorCarvalho_2026.04.10.jpeg` *(critical)*
  - expected: `CA06F18D-N°1 CT012A4500`
  - actual:   `CA06F18D-N`


## Extraction failures

- `JulioLima_2026.04.15.jpeg`: extraction_failed: Server error '500 Internal Server Error' for url 'http://localhost:11434/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/500
- `JulioLima_2026.04.18.JPG`: parse_error: ```json
{
  "header": {
    "operador": "JULIO LIMA",
    "n_operador": "0537",
    "setor_maquina": "BOBINE-FORMATO",
    "data": "18-04-2026"
  },
  "rows": [
    {"pri": "18", "cliente": "MTG", "ov": "250010", "of": "257504", "modelo": "CA08E10B", "qtd": "52", "comp_mm": "11020", "larg_mm": "1060


---

## Notes for the next phase

- Look at the *Top errors* section first; that points to which Phase 0.5
  pre-processing step (deskew, dewarp, CLAHE, stain mask) or which
  Phase 4 grammar (lexicon-constrained CLIENTE / OPERADOR / OF /
  LOTE / MODELO) is likely to give the biggest lift.
- Per-field accuracy below 80 % on a numeric or handwritten field
  (LARG_MM, COMP_MM, QTD) is a strong signal that Phase 6 (operator
  fine-tune of TrOCR for cells) should be prioritised.
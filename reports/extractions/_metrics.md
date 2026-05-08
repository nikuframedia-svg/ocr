# Extraction metrics

Generated: 2026-04-29T19:05:06.409783+00:00

## Summary

```
sheets evaluated:    17
field accuracy:      71.6%
perfect sheets:      0.0%
critical accuracy:   66.0%
hallucination rate:  60.0%
```

## Accuracy by field

| field | accuracy | CER (lex) |
|---|---|---|
| `cliente` | 65.0% | 0.193 |
| `colunas_produzidas` | 100.0% | — |
| `comp_mm` | 80.6% | — |
| `coni` | 67.0% | — |
| `data` | 100.0% | 0.000 |
| `esp` | 61.2% | — |
| `horas_trabalhadas` | 100.0% | — |
| `larg_mm` | 84.5% | — |
| `lbase` | 73.8% | — |
| `lote` | 59.2% | 0.160 |
| `ltopo` | 76.7% | — |
| `modelo` | 42.7% | 0.208 |
| `n_operador` | 100.0% | — |
| `of` | 70.9% | 0.166 |
| `operador` | 94.1% | 0.008 |
| `ov` | 59.2% | 0.207 |
| `pri` | 78.6% | — |
| `qtd` | 84.5% | — |
| `setor_maquina` | 100.0% | — |

## Top errors to eyeball

- **rows[0].modelo** in `JulioLima_2026.04.09.jpeg` *(critical)*
  - expected: `CGC3E10D`
  - actual:   `C6E3E10D`
- **rows[0].modelo** in `JulioLima_2026.04.10.jpeg` *(critical)*
  - expected: `N°2.1/2-08540554`
  - actual:   `N:2.1/2-0850US54`
- **rows[0].modelo** in `JulioLima_2026.04.14.jpeg` *(critical)*
  - expected: `CLCAF06DI-V`
  - actual:   `CLCAF06DII-V`
- **rows[0].modelo** in `JulioLima_2026.04.17.JPG` *(critical)*
  - expected: `CGCAE04Di`
  - actual:   `CGCAE04Pi`
- **rows[0].modelo** in `VitorCarvalho_2026.04.10.jpeg` *(critical)*
  - expected: `CA06F18D-N°1 CT012A4500`
  - actual:   `CA06F18D-N`

## Extraction failures

- `JulioLima_2026.04.15.jpeg`: failed: Server error '500 Internal Server Error' for url 'http://localhost:11434/v1/chat/completions'
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

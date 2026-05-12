# Refs a actualizar — relatório para factory team

Gerado: 106 cells em sheets actuais dependem de refs ausentes em plan_colunas/StockSAP.

## OFs ausentes em plan_colunas_cpis.xlsx

| OF | # cells | Sheets |
|---|---|---|
| `262489` | 2 | 9 |
| `262334` | 2 | 48 |
| `262498` | 1 | 43 |
| `262837` | 1 | 43 |
| `262478` | 1 | 43 |

## Lotes ausentes em StockSAP.xlsx

| Lote | # cells | Sheets |
|---|---|---|
| `M26B0355` | 13 | 8, 12, 14, 15, 17, 21, 22 |
| `M26B0343` | 9 | 39, 61 |
| `M26B0307` | 7 | 5, 19, 20, 24, 25, 27 |
| `M26B0294` | 7 | 40, 58 |
| `M26B0305` | 6 | 37, 42, 54 |
| `M26B0291` | 5 | 33, 36, 43 |
| `M26B0328` | 5 | 51 |
| `M25B1341` | 4 | 38 |
| `M26B0344` | 4 | 57 |
| `M26B0327` | 3 | 1, 6, 7 |
| `M26B0273` | 2 | 9, 28 |
| `14` | 2 | 9 |
| `M26B0226` | 2 | 11 |
| `M26B0325` | 2 | 29, 30 |
| `M26B0324` | 2 | 29, 30 |
| `M26B0380` | 2 | 39 |
| `M26B0345` | 2 | 39, 57 |
| `M26B0369` | 2 | 43, 46 |
| `M26B0329` | 2 | 44, 58 |
| `M26B0297` | 2 | 49 |
| `M26B0256` | 2 | 52, 57 |
| `M26B0284` | 1 | 3 |
| `M26B0292` | 1 | 7 |
| `M26B0326` | 1 | 30 |
| `M26B0354` | 1 | 37 |
| `M26B0367` | 1 | 37 |
| `M24B0895` | 1 | 43 |
| `M26B0243` | 1 | 44 |
| `M26B0378` | 1 | 45 |
| `M26B0374` | 1 | 45 |
| `M26B0260` | 1 | 47 |
| `M26B0350` | 1 | 50 |
| `M26B0249` | 1 | 62 |
| `M24B0031` | 1 | 63 |
| `M26B0351` | 1 | 65 |

## Acção

1. Verificar com a factory se estes OFs/lotes existem em sistema interno
2. Actualizar os XLSX em `C:\kanban\nifruka\04_Documentacao\`
3. `curl -X POST http://127.0.0.1:8080/admin/reload-refs` para recarregar
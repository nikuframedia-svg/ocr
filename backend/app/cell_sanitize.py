"""R257 — neutralização de injeção de fórmulas em exports (CSV/XLSX).

Valores de OCR / edições humanas iam crus para os exports. Ao abrir no
Excel, uma célula começada por ``=`` executa como fórmula (openpyxl infere
data_type='f'); na importação de CSV o Excel também trata ``+ - @`` e
tab/CR iniciais como gatilhos. Mitigação OWASP: prefixar com apóstrofo.

Âmbito deliberado (decisão do utilizador): downloads do utilizador
(XLSX + GET /sheet/{id}/csv). O CSV depositado para a FÁBRICA não é
alterado — é contrato do consumidor legado (kanban_csv2excel) e a sua
exposição está reportada como decisão de produto.
"""
from __future__ import annotations

_CSV_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def neutralize_xlsx(value: object) -> object:
    """Neutraliza strings que o openpyxl escreveria como fórmula (só ``=``
    dispara: '+', '-', '@' ficam data_type='s' em XLSX nativo)."""
    if isinstance(value, str) and value.startswith("="):
        return "'" + value
    return value


def neutralize_csv(value: object) -> object:
    """Neutraliza o conjunto completo de gatilhos da importação CSV→Excel."""
    if isinstance(value, str) and value.startswith(_CSV_TRIGGERS):
        return "'" + value
    return value

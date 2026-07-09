"""R257 — neutralização de injeção de fórmulas nos exports do UTILIZADOR.

Valores OCR/editados iam crus para os exports; uma célula '=cmd' executa
como fórmula ao abrir no Excel (openpyxl infere data_type='f' para strings
começadas por '='; a importação de CSV também dispara com + - @). Âmbito:
downloads do utilizador (XLSX + GET /sheet/{id}/csv). O CSV da FÁBRICA
fica byte-idêntico (contrato do consumidor legado — decisão do utilizador).
"""
from __future__ import annotations

from app.cell_sanitize import neutralize_csv, neutralize_xlsx
from app.web.main import _to_3block_csv


class TestHelpers:
    def test_xlsx_neutralizes_only_equals(self):
        assert neutralize_xlsx("=cmd|' /C calc'!A0") == "'=cmd|' /C calc'!A0"
        # Em XLSX nativo, + - @ ficam data_type='s' — não são gatilho.
        assert neutralize_xlsx("+351123") == "+351123"
        assert neutralize_xlsx("CP-1200") == "CP-1200"
        assert neutralize_xlsx(42) == 42
        assert neutralize_xlsx(None) is None

    def test_csv_neutralizes_full_trigger_set(self):
        assert neutralize_csv("=1+1") == "'=1+1"
        assert neutralize_csv("+SUM(A1)") == "'+SUM(A1)"
        assert neutralize_csv("-2+3") == "'-2+3"
        assert neutralize_csv("@here") == "'@here"
        assert neutralize_csv("\t=x") == "'\t=x"
        assert neutralize_csv("normal") == "normal"
        assert neutralize_csv(7) == 7


_SHEET_DATA = {
    "template_name": "bobine_formato",
    "header": {"operador": "=EVIL()", "n_operador": "537",
               "setor_maquina": "BOBINE-FORMATO", "data": "15-04-2026"},
    "rows": [{"pri": "", "cliente": "=HYPERLINK(\"http://x\")", "ov": "",
              "of": "123456", "modelo": "CP-1200", "qtd": "5"}],
    "footer": {},
}


class TestUserCsvDownload:
    def test_neutralized_download(self):
        out = _to_3block_csv("t.jpg", _SHEET_DATA, neutralize=True)
        assert "'=EVIL()" in out
        assert "'=HYPERLINK" in out
        assert ";=EVIL()" not in out

    def test_factory_deposit_byte_identical(self):
        # O caminho da fábrica (default neutralize=False) NÃO muda.
        out = _to_3block_csv("t.jpg", _SHEET_DATA)
        assert "=EVIL()" in out
        assert "'=EVIL()" not in out

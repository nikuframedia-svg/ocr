"""Task C E4 — parse tolerante da descoberta + mapeamento label→campo."""
from __future__ import annotations

import json

from app.web import template_store
from app.web.ocr_runner import parse_discovery_response


class TestParseDiscoveryResponse:
    def test_clean_json(self):
        raw = json.dumps({
            "title": "TPL999 CORTE",
            "header": ["Operador", "N°", "Setor/Maquina", "Data"],
            "columns": ["OF", "MODELO", "QTD"],
            "footer": ["Colunas Produzidas"],
        })
        out = parse_discovery_response(raw)
        assert out["parse_ok"] is True
        assert out["columns"] == ["OF", "MODELO", "QTD"]
        assert out["title"] == "TPL999 CORTE"

    def test_markdown_wrapped(self):
        raw = "```json\n{\"columns\": [\"OF\"], \"header\": [], \"footer\": []}\n```"
        out = parse_discovery_response(raw)
        assert out["parse_ok"] is True
        assert out["columns"] == ["OF"]

    def test_empty_response(self):
        out = parse_discovery_response("")
        assert out["parse_ok"] is False
        assert out["columns"] == []

    def test_garbage_never_raises(self):
        for raw in ["not json", "{broken", "[1,2,3]", "null", '{"columns": "OF"}']:
            out = parse_discovery_response(raw)
            assert out["parse_ok"] is False

    def test_no_columns_not_ok(self):
        out = parse_discovery_response(json.dumps(
            {"title": "X", "header": ["Operador"], "columns": [], "footer": []}))
        assert out["parse_ok"] is False

    def test_values_coerced_and_stripped(self):
        out = parse_discovery_response(json.dumps(
            {"columns": ["  OF  ", 12, ""], "header": None, "footer": {}}))
        assert out["columns"] == ["OF", "12"]
        assert out["header"] == []


class TestMapLabelToField:
    def test_exact_canonical_labels(self):
        assert template_store.map_label_to_field("MODELO") == ("modelo", True)
        assert template_store.map_label_to_field("COMP_MM") == ("comp_mm", True)
        assert template_store.map_label_to_field("FERRAMENTA / CONI") == ("coni", True)
        assert template_store.map_label_to_field("CESTA Nº") == ("cesta_n", True)
        assert template_store.map_label_to_field("QTD (METROS)") == ("qtd_metros", True)

    def test_accents_and_case(self):
        assert template_store.map_label_to_field("Modêlo") == ("modelo", True)
        assert template_store.map_label_to_field("INÍCIO") == ("inicio", True)

    def test_synonyms(self):
        assert template_store.map_label_to_field("QUANTIDADE") == ("qtd", True)
        assert template_store.map_label_to_field("Espessura") == ("esp", True)
        assert template_store.map_label_to_field("REFERÊNCIA / PEÇA") == ("modelo", True)

    def test_unknown_becomes_custom_slug(self):
        field, matched = template_store.map_label_to_field("Nº DE SÉRIE")
        assert matched is False
        assert field == "no_de_serie"  # NFKD: º → o

    def test_digit_start_prefixed(self):
        field, matched = template_store.map_label_to_field("2ª PASSAGEM")
        assert matched is False
        assert field.startswith("c_")


class TestSuggestSpec:
    DISC = {
        "parse_ok": True,
        "title": "TPL999 — CORTE ESPOSENDE",
        "header": ["Operador", "N°", "Setor/Maquina", "Data"],
        "columns": ["OF", "MODELO", "QTD", "CÓDIGO INTERNO"],
        "footer": ["Colunas Produzidas", "Horas Trabalhadas"],
    }

    def test_suggestion(self):
        out = template_store.suggest_spec_from_discovery(
            self.DISC, name="u2_corte", unidade_id=2)
        spec = out["spec"]
        assert spec["name"] == "u2_corte"
        assert spec["row_fields"] == ["of", "modelo", "qtd", "codigo_interno"]
        assert spec["cross_check_fields"] == ["of", "modelo"]
        assert spec["footer_fields"] == ["colunas_produzidas", "horas_trabalhadas"]
        assert spec["setor_aliases"] == []  # humano preenche
        assert any("codigo_interno" in w for w in out["warnings"])
        rows_map = [m for m in out["field_map"] if m["section"] == "rows"]
        assert [m["matched"] for m in rows_map] == [True, True, True, False]

    def test_duplicate_columns_suffixed(self):
        disc = dict(self.DISC, columns=["QTD", "QTD"])
        out = template_store.suggest_spec_from_discovery(
            disc, name="u2_x", unidade_id=2)
        assert out["spec"]["row_fields"] == ["qtd", "qtd_2"]

    def test_spec_validates(self):
        out = template_store.suggest_spec_from_discovery(
            self.DISC, name="u2_corte", unidade_id=2)
        spec = dict(out["spec"], setor_aliases=["CORTE ESPOSENDE"])
        errors, warnings = template_store.validate_spec_payload(spec)
        assert errors == []
        assert any("codigo_interno" in w for w in warnings)

    def test_empty_footer_gets_defaults(self):
        disc = dict(self.DISC, footer=[])
        out = template_store.suggest_spec_from_discovery(
            disc, name="u2_x", unidade_id=2)
        assert out["spec"]["footer_fields"] == [
            "colunas_produzidas", "horas_trabalhadas"]

    def test_field_labels_preserved(self):
        out = template_store.suggest_spec_from_discovery(
            self.DISC, name="u2_corte", unidade_id=2)
        assert out["spec"]["field_labels"]["codigo_interno"] == "CÓDIGO INTERNO"

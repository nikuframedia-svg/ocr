"""rev00 (13/04/2026) — testes do novo formato de kanbans.

Cobre: coluna SUCATA (registry/prompt/CSV/schema), TURNO no header por defeito,
novos aliases de máquina + endurecimento do MTG-token, template genérico
`paragens`, routing frente/verso por pista de página + flag de revisão, e o
filtro de paragens do downtime dirigido pelo registry.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from app.templates_registry import (
    TEMPLATES,
    detect_template,
    get_template,
)
from app.web import ocr_runner

# ---------------------------------------------------------------------------
# A — Registry: SUCATA + TURNO + aliases + paragens genérico
# ---------------------------------------------------------------------------

_PRODUCTION = [t for t in TEMPLATES.values() if t.has_production_rows]


@pytest.mark.parametrize("tpl", _PRODUCTION, ids=lambda t: t.name)
def test_production_templates_have_turno_in_header(tpl):
    assert "turno" in tpl.header_fields


@pytest.mark.parametrize(
    "tpl",
    [t for t in _PRODUCTION if t.name != "acabamento"],
    ids=lambda t: t.name,
)
def test_production_templates_end_with_sucata(tpl):
    # SUCATA é sempre a última coluna no papel rev00.
    assert tpl.row_fields[-1] == "sucata"


def test_acabamento_untouched_no_sucata():
    # TPL086 é família diferente; não entrou no lote rev00.
    assert "sucata" not in get_template("acabamento").row_fields


def test_generic_paragens_registered():
    p = get_template("paragens")
    assert p.name == "paragens"
    assert p.has_production_rows is False
    assert p.row_fields == ("motivo", "inicio", "fim", "duracao", "resolvido")
    assert p.cross_check_fields == ()
    assert p.footer_fields == ()
    assert "turno" in p.header_fields
    assert "setor_maquina" in p.header_fields  # mantém identidade da máquina


def test_paragens_never_auto_detected():
    # setor_aliases=() → nunca sai de detect_template; só via pista/side-detect.
    assert detect_template("MÁQUINA DE FUSTES").name != "paragens"
    assert detect_template("QUINADORA PAV.4").name != "paragens"


def test_maq_fustes_paragens_kept_for_legacy():
    # Folhas já persistidas continuam a resolver.
    assert get_template("maq_fustes_paragens").name == "maq_fustes_paragens"


# ---------------------------------------------------------------------------
# B — Detecção: novos rótulos rev00 + endurecimento do MTG-token
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "label, expected",
    [
        ("ROBOT MTG2", "robot"),
        ("LASER MTG2", "laser"),
        ("GUILHOTINA 6M", "guilhotina"),
        ("SOLDLINE 4", "soldline"),
        ("QUINADORA PAV.8", "quinadora_pav8"),
        ("MÁQUINA DE FUSTES", "maq_fustes"),
    ],
)
def test_rev00_labels_resolve(label, expected):
    assert detect_template(label).name == expected


def test_garbled_base_with_mtg_suffix_not_acabamento():
    # base mal lida + "MTG2" limpo NÃO deve cair em acabamento (regressão rev00).
    assert detect_template("R0B0T MTG2").name != "acabamento"
    assert detect_template("LAZER MTG2").name != "acabamento"


def test_bare_mtg_token_still_acabamento():
    # Um setor que é SÓ "MTG2" continua a ser acabamento (comportamento antigo).
    assert detect_template("MTG2").name == "acabamento"
    assert detect_template("ACABAMENTO MTG2").name == "acabamento"


# ---------------------------------------------------------------------------
# B — Prompt + schema + CSV: SUCATA
# ---------------------------------------------------------------------------

def test_prompt_columns_include_sucata():
    from app.pipeline.prompt_builder import build_prompt

    prompt = build_prompt(get_template("bobine_formato"))
    assert "SUCATA" in prompt
    assert '"sucata":""' in prompt  # skeleton da linha


def test_paragens_prompt_uses_paragens_rules():
    from app.pipeline.prompt_builder import build_prompt

    prompt = build_prompt(get_template("paragens"))
    assert "MOTIVO DA PARAGEM" in prompt
    # não deve pedir o schema de produção (QTD/OF de 6 dígitos)
    assert "OF is EXACTLY 6 digits" not in prompt


def test_row_schema_has_sucata():
    from app.pipeline.inference.schemas import Row

    assert "sucata" in Row.model_fields
    assert Row(sucata="3").sucata == "3"


def test_strict_grammar_accepts_sucata():
    from app.pipeline.inference.schemas_strict import KANBAN_JSON_SCHEMA

    props = KANBAN_JSON_SCHEMA["properties"]["rows"]["items"]["properties"]
    assert "sucata" in props
    # não em required — outros templates continuam válidos
    assert "sucata" not in KANBAN_JSON_SCHEMA["properties"]["rows"]["items"]["required"]


def test_factory_csv_includes_sucata_column():
    from app.web.main import _to_3block_csv

    data = {
        "template_name": "bobine_formato",
        "header": {"operador": "X", "data": "10-05-2026", "setor_maquina": "BOBINE-FORMATO"},
        "rows": [{"of": "262107", "modelo": "OMEGA", "qtd": "5", "sucata": "2"}],
        "footer": {"colunas_produzidas": "5", "horas_trabalhadas": "8"},
    }
    csv_text = _to_3block_csv("kanban.jpg", data)
    assert "SUCATA" in csv_text
    # o valor 2 sai na linha
    assert ";2" in csv_text or ",2" in csv_text or "2\r" in csv_text


# ---------------------------------------------------------------------------
# E — Downtime: filtro dirigido pelo registry
# ---------------------------------------------------------------------------

def test_downtime_paragens_names_registry_driven():
    from app.downtime import _paragens_template_names

    names = _paragens_template_names()
    assert "paragens" in names
    assert "maq_fustes_paragens" in names
    # nenhum template de produção
    assert "bobine_formato" not in names
    assert "quinadora_pav4_paragens" not in names  # nome morto, já não existe


# ---------------------------------------------------------------------------
# C — run_pipeline: routing frente/verso (pista autoritativa / fast-path / flag)
# ---------------------------------------------------------------------------

def _mock_pipeline(monkeypatch, *, setor="GUILHOTINA", rows=None):
    """Isola a lógica de lado: _run_ocr e helpers devolvem fakes."""
    pass1 = {"header": {"setor_maquina": setor}, "rows": rows or []}
    monkeypatch.setattr(ocr_runner, "_run_ocr", lambda *a, **k: (pass1, None))
    monkeypatch.setattr(ocr_runner, "_merge_pass2_into_pass1", lambda p1, p2: p1)
    monkeypatch.setattr(ocr_runner, "_build_current_and_dq", lambda raw, tpl: ({}, {}))


def _detect_must_not_run(*_a, **_k):
    raise AssertionError("_detect_side não devia correr aqui")


def test_hint_verso_routes_to_paragens_without_mini_ocr(monkeypatch):
    _mock_pipeline(monkeypatch, rows=[])
    monkeypatch.setattr(ocr_runner, "_detect_side", _detect_must_not_run)
    res = ocr_runner.run_pipeline(Path("x.jpg"), page_hint="V")
    assert res["template_name"] == "paragens"
    assert res["side"] == "V"
    assert res["side_source"] == "hint"
    assert res["needs_review"] is False


def test_hint_frente_keeps_production_without_mini_ocr(monkeypatch):
    _mock_pipeline(monkeypatch, rows=[])
    monkeypatch.setattr(ocr_runner, "_detect_side", _detect_must_not_run)
    res = ocr_runner.run_pipeline(Path("x.jpg"), page_hint="F")
    assert res["template_name"] == "guilhotina"
    assert res["side"] == "F"
    assert res["side_source"] == "hint"


def test_confident_frente_skips_mini_ocr(monkeypatch):
    _mock_pipeline(
        monkeypatch,
        rows=[{"of": "262107", "modelo": "OMEGA"}, {"of": "262559", "modelo": "CGC2"}],
    )
    monkeypatch.setattr(ocr_runner, "_detect_side", _detect_must_not_run)
    res = ocr_runner.run_pipeline(Path("x.jpg"))  # sem pista
    assert res["template_name"] == "guilhotina"
    assert res["side"] == "F"
    assert res["side_source"] == "structure"


def test_no_hint_detect_verso_routes_to_paragens(monkeypatch):
    _mock_pipeline(monkeypatch, rows=[])  # sem sinal de frente
    monkeypatch.setattr(ocr_runner, "_detect_side", lambda *_a, **_k: "V")
    res = ocr_runner.run_pipeline(Path("x.jpg"))
    assert res["template_name"] == "paragens"
    assert res["side"] == "V"
    assert res["side_source"] == "detect"
    assert res["needs_review"] is False


def test_no_hint_indeterminate_flags_needs_review(monkeypatch):
    _mock_pipeline(monkeypatch, rows=[])
    monkeypatch.setattr(ocr_runner, "_detect_side", lambda *_a, **_k: "?")
    res = ocr_runner.run_pipeline(Path("x.jpg"))
    # extrai como frente mas fica marcada p/ revisão (não deposita produção)
    assert res["side"] == "F"
    assert res["needs_review"] is True
    assert res["review_reason"] == "side_indeterminate"


# --- check estrutural INLINE da pista vs Pass-1 (substitui o cross-check async) ---

_PROD_ROWS = [{"of": "262107", "modelo": "OMEGA"}, {"of": "262559", "modelo": "CGC2"}]


def test_hint_v_but_frente_flags_review(monkeypatch):
    # pista=V mas o Pass-1 tem cara de frente → provável frente perdida → revisão.
    _mock_pipeline(monkeypatch, rows=_PROD_ROWS)
    monkeypatch.setattr(ocr_runner, "_detect_side", _detect_must_not_run)
    res = ocr_runner.run_pipeline(Path("x.jpg"), page_hint="V")
    assert res["template_name"] == "paragens"      # routing respeita a pista
    assert res["side"] == "V"
    assert res["needs_review"] is True
    assert res["review_reason"] == "side_hint_conflict"


def test_hint_f_but_nonproduction_flags_review(monkeypatch):
    # pista=F mas ≥2 linhas preenchidas sem identidade de produção → provável verso.
    _mock_pipeline(monkeypatch, rows=[{"a": "x"}, {"b": "y"}])
    monkeypatch.setattr(ocr_runner, "_detect_side", _detect_must_not_run)
    res = ocr_runner.run_pipeline(Path("x.jpg"), page_hint="F")
    assert res["template_name"] == "guilhotina"    # routing respeita a pista
    assert res["side"] == "F"
    assert res["needs_review"] is True
    assert res["review_reason"] == "side_hint_conflict"


def test_hint_f_with_production_not_flagged(monkeypatch):
    _mock_pipeline(monkeypatch, rows=_PROD_ROWS)
    monkeypatch.setattr(ocr_runner, "_detect_side", _detect_must_not_run)
    res = ocr_runner.run_pipeline(Path("x.jpg"), page_hint="F")
    assert res["needs_review"] is False


def test_hint_v_with_empty_pass1_not_flagged(monkeypatch):
    # pista=V com Pass-1 vazio (verso real) → sem conflito, sem revisão.
    _mock_pipeline(monkeypatch, rows=[])
    monkeypatch.setattr(ocr_runner, "_detect_side", _detect_must_not_run)
    res = ocr_runner.run_pipeline(Path("x.jpg"), page_hint="V")
    assert res["template_name"] == "paragens"
    assert res["needs_review"] is False

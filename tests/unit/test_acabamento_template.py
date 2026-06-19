from __future__ import annotations

from app.web import db


def _acabamento_sheet_data() -> dict:
    return {
        "template_name": "acabamento",
        "header": {
            "operador": "JÚLIO LIMA",
            "n_operador": "0537",
            "setor_maquina": "ACABAMENTO MTG4",
            "cod_maquina": "M061",
            "data": "25-05-2026",
            "turno": "M",
        },
        "rows": [
            {"of": "262892", "modelo": "CGC2E10D", "qtd": "4"},
        ],
        "footer": {"colunas_produzidas": "4"},
    }


def test_acabamento_syncs_minimal_rows_without_legacy_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "app.db")
    db.init_db()
    sheet_id = db.insert_sheet("acabamento.jpg")

    sheet_data = _acabamento_sheet_data()
    db.update_extraction(
        sheet_id=sheet_id,
        raw_extraction=sheet_data,
        dq_audit={},
        sheet_data=sheet_data,
    )

    with db.conn() as c:
        row = c.execute(
            """SELECT of, modelo, qtd, ov, cliente, coni, sheet_hours,
                      sheet_total_qty, sheet_iso_date
               FROM production_rows
               WHERE sheet_id = ?""",
            (sheet_id,),
        ).fetchone()

    assert row is not None
    assert row["of"] == "262892"
    assert row["modelo"] == "CGC2E10D"
    assert row["qtd"] == 4
    assert row["sheet_total_qty"] == 4
    assert row["sheet_iso_date"] == "2026-05-25"
    assert row["ov"] is None
    assert row["cliente"] is None
    assert row["coni"] is None
    assert row["sheet_hours"] is None


def test_acabamento_3block_csv_uses_tpl086_labels():
    from app.web.main import _to_3block_csv

    csv_text = _to_3block_csv("acabamento.jpg", _acabamento_sheet_data())

    assert "### TABELA DE ACABAMENTO" in csv_text
    assert "FICHEIRO;DATA;OPERADOR;OF;REFERÊNCIA / PEÇA;QTD" in csv_text
    assert "FICHEIRO;DATA;OPERADOR;TOTAL QTD" in csv_text
    assert "HORAS_TRABALHADAS" not in csv_text
    assert "COLUNAS_PRODUZIDAS" not in csv_text


def test_pass2_merge_preserves_acabamento_turno():
    from app.web.ocr_runner import _merge_pass2_into_pass1

    merged = _merge_pass2_into_pass1(
        {"header": {"setor_maquina": "ACABAMENTO MTG4", "data": "25-05-2026"}},
        {"header": {"turno": "XM"}, "rows": [], "footer": {}},
    )

    assert merged["header"]["turno"] == "XM"


def test_merge_preserves_real_setor_mtg2_over_pass2():
    """R139 — o setor real (Pass-1, leitura do papel) ganha ao Pass-1/Pass-2 merge:
    ACABAMENTO MTG2 não pode virar ACABAMENTO MTG4."""
    from app.web.ocr_runner import _merge_pass2_into_pass1

    merged = _merge_pass2_into_pass1(
        {"header": {"setor_maquina": "ACABAMENTO MTG2"}},
        {"header": {"setor_maquina": "ACABAMENTO MTG4"}, "rows": [], "footer": {}},
    )

    assert merged["header"]["setor_maquina"] == "ACABAMENTO MTG2"


def test_merge_preserves_real_setor_mtg4():
    """R139 — ACABAMENTO MTG4 continua ACABAMENTO MTG4."""
    from app.web.ocr_runner import _merge_pass2_into_pass1

    merged = _merge_pass2_into_pass1(
        {"header": {"setor_maquina": "ACABAMENTO MTG4"}},
        {"header": {"setor_maquina": "ACABAMENTO MTG4"}, "rows": [], "footer": {}},
    )

    assert merged["header"]["setor_maquina"] == "ACABAMENTO MTG4"


def test_acabamento_mtg2_alias_resolves_to_acabamento():
    """Compatibilidade: folhas persistidas como `acabamento_mtg2` resolvem para
    o template consolidado `acabamento`."""
    from app.templates_registry import get_template

    assert get_template("acabamento_mtg2").name == "acabamento"


def test_default_pass1_shifted_acabamento_infers_acabamento_template(monkeypatch):
    import importlib
    import sys
    from types import SimpleNamespace

    if "app.web.ocr_runner" in sys.modules:
        ocr_runner = sys.modules["app.web.ocr_runner"]
    else:
        monkeypatch.setitem(
            sys.modules,
            "ocr6",
            SimpleNamespace(
                PROMPT="",
                PROMPT_HASH="",
                load_prompt=lambda _path: ("", ""),
                process_image=lambda *_args, **_kwargs: None,
            ),
        )
        ocr_runner = importlib.import_module("app.web.ocr_runner")

    inferred = ocr_runner._infer_template_from_default_pass1({
        "header": {"setor_maquina": ""},
        "rows": [
            {
                "cliente": "",
                "ov": "Q6Q7F3",
                "of": "CGC2E0GDI",
                "modelo": "",
                "qtd": "7v",
            },
        ],
        "footer": {},
    })

    assert inferred is not None
    assert inferred.name == "acabamento"


def test_acabamento_prompt_columns_and_no_forced_mtg4():
    """Acabamento mantém colunas OF / REFERÊNCIA / PEÇA / QTD e o prompt não
    força ACABAMENTO MTG4 (R139)."""
    from app.pipeline.prompt_builder import build_prompt
    from app.templates_registry import get_template

    prompt = build_prompt(get_template("acabamento"))

    assert "OF | REFERÊNCIA / PEÇA | QTD" in prompt
    assert "ACABAMENTO MTG4" not in prompt

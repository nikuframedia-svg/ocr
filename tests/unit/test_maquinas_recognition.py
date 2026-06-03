"""R138 — reconhecimento das máquinas do maquinas.xlsx pelo detect_template.

As 7 quinadoras (QUINADORA P4/P8/ADIRA/PUMA/CÓNICA P8/MTG3) caíam no template
`bobine_formato` porque os templates só conheciam `PAV.4`/`PAV.8`. Agora os
labels P4 batem em quinadora_pav4 e o resto (P8 + sem-pavilhão) cai num
fallback genérico → quinadora_pav8 (mesmo schema). GUIFIL continua em guifil.
"""
from __future__ import annotations

import pytest

from app.dq.machines import resolve_machine_from_setor
from app.templates_registry import DEFAULT_TEMPLATE, detect_template, get_template

# Todos os labels de quinadora vindos do maquinas.xlsx (desigkanban + desmaq).
QUINADORA_LABELS = [
    "QUINADORA P4", "QUINADORA P8", "QUINADORA ADIRA", "QUINADORA ADIRA MTG4",
    "QUINADORA ADIRA 3M", "QUINADORA ADIRA 14M P4", "QUINADORA ADIRA 14M P8",
    "QUINADORA CÓNICA P8", "QUINADORA MTG3", "QUINADORA PUMA", "QUINADORA PUMA P4",
    "QUINADORA GUIFIL MTG2", "GUIFIL",
]


def _machine_refs():
    recs = {
        "M041": {"codmaq": "M041", "desmaq": "QUINADORA ADIRA MTG4", "desigkanban": "QUINADORA ADIRA", "colunaexcel": "Q"},
        "M044": {"codmaq": "M044", "desmaq": "QUINADORA ADIRA 14M P4", "desigkanban": "QUINADORA P4", "colunaexcel": "Q"},
        "M045": {"codmaq": "M045", "desmaq": "QUINADORA ADIRA 14M P8", "desigkanban": "QUINADORA P8", "colunaexcel": "Q"},
        "M067": {"codmaq": "M067", "desmaq": "QUINADORA GUIFIL MTG2", "desigkanban": "GUIFIL", "colunaexcel": "Q"},
        "M081": {"codmaq": "M081", "desmaq": "QUINADORA MTG3", "desigkanban": "", "colunaexcel": "Q"},
        "M040": {"codmaq": "M040", "desmaq": "QUINADORA CÓNICA P8", "desigkanban": "", "colunaexcel": "Q"},
    }
    return {
        "maquinas_by_codmaq": recs,
        "maquinas_by_kanban": {
            "QUINADORA ADIRA": recs["M041"],
            "QUINADORA P4": recs["M044"],
            "QUINADORA P8": recs["M045"],
            "GUIFIL": recs["M067"],
            "QUINADORA GUIFIL MTG2": recs["M067"],
            "QUINADORA MTG3": recs["M081"],
            "QUINADORA CÓNICA P8": recs["M040"],
        },
    }


@pytest.mark.parametrize("label", QUINADORA_LABELS)
def test_quinadora_never_bobine_formato(label):
    name = detect_template(label).name
    assert name in ("quinadora_pav4", "quinadora_pav8", "guifil"), (
        f"{label!r} → {name} (devia ser uma quinadora, não {DEFAULT_TEMPLATE.name})"
    )


@pytest.mark.parametrize("label,expected", [
    ("QUINADORA P4", "quinadora_pav4"),
    ("QUINADORA ADIRA 14M P4", "quinadora_pav4"),
    ("QUINADORA PUMA", "quinadora_pav4"),
    ("QUINADORA P8", "quinadora_pav8"),
    ("QUINADORA CÓNICA P8", "quinadora_pav8"),
    ("QUINADORA ADIRA MTG4", "quinadora_pav8"),  # sem pavilhão → fallback pav8
    ("QUINADORA MTG3", "quinadora_pav8"),
    ("GUIFIL", "guifil"),
    ("QUINADORA GUIFIL MTG2", "guifil"),
])
def test_quinadora_specific_mapping(label, expected):
    assert detect_template(label).name == expected


@pytest.mark.parametrize("label,expected", [
    # Aliases existentes inalterados (sem regressão).
    ("QUINADORA PAV.4", "quinadora_pav4"),
    ("QUINADORA PAV.8", "quinadora_pav8"),
    ("BOBINE-FORMATO", "bobine_formato"),
    ("GUILHOTINA 9M", "guilhotina"),
    ("LASER MTG2", "laser"),
    ("LINHA DE CORTE", "linha_corte"),
    ("ACABAMENTO MTG2", "acabamento"),
    ("ACABAMENTO MTG3", "acabamento"),
    ("ACABAMENTO MTG4", "acabamento"),
    ("ACABAMENTO MTG5", "acabamento"),
    ("ACAB. MTG4", "acabamento"),
    ("ACABAMENTO", "acabamento"),
    ("ROBOT MTG2", "robot"),
    ("SOLDLINE 4", "soldline"),
    # Soldadura sem template (confirmado: não digitalizadas) → fallback ok.
    ("ARCO SUBMERSO", "bobine_formato"),
    # Lixo / desconhecido → default.
    ("XPTO 123", "bobine_formato"),
])
def test_no_regression(label, expected):
    assert detect_template(label).name == expected


def test_quinadora_substring_in_longer_setor():
    # O operador pode escrever o cod junto: "QUINADORA P4 M044".
    assert detect_template("QUINADORA P4 M044").name == "quinadora_pav4"
    assert detect_template("QUINADORA MTG3 M081").name == "quinadora_pav8"


def test_acabamento_legacy_template_aliases_resolve_to_canonical():
    assert get_template("acabamento_mtg2").name == "acabamento"
    assert get_template("acabamento_mtg4").name == "acabamento"
    assert get_template("acabamento").name == "acabamento"


@pytest.mark.parametrize(
    ("setor", "expected_code"),
    [
        ("QUINADORA P8", "M045"),
        ("QUINADORA PAV.8", "M045"),
        ("QUINADORA P4", "M044"),
        ("QUINADORA PAV.4", "M044"),
        ("QUINADORA CÓNICA P8", "M040"),
        ("QUINADORA CONICA P8", "M040"),
        ("QUINADORA MTG3", "M081"),
        ("QUINADORA ADIRA", "M041"),
        ("GUIFIL", "M067"),
    ],
)
def test_resolve_machine_from_setor_handles_quinadora_aliases(setor, expected_code):
    rec = resolve_machine_from_setor(setor, _machine_refs())
    assert rec is not None
    assert rec["codmaq"] == expected_code


def test_apply_codmaq_fill_uses_machine_resolver(monkeypatch):
    from app.web import main

    calls = []
    monkeypatch.setattr(
        main.db,
        "apply_edit",
        lambda sid, path, value, source: calls.append((sid, path, value, source)),
    )
    sheet = {
        "sheet_data": {
            "header": {"setor_maquina": "QUINADORA PAV.8", "cod_maquina": ""}
        }
    }

    assert main._apply_codmaq_fill(42, sheet, _machine_refs()) == 1
    assert calls == [(42, "header.cod_maquina", "M045", "system")]


def test_kpi_machine_derivation_uses_refs_for_quinadora():
    from app.web.kpis import _derive_cod_maquina, _sector_machine_codes

    refs = _machine_refs()
    assert _derive_cod_maquina("QUINADORA CONICA P8", refs) == "M040"
    assert "M045" in _sector_machine_codes(refs)["Quinadora"]

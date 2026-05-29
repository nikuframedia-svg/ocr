"""R138 — reconhecimento das máquinas do maquinas.xlsx pelo detect_template.

As 7 quinadoras (QUINADORA P4/P8/ADIRA/PUMA/CÓNICA P8/MTG3) caíam no template
`bobine_formato` porque os templates só conheciam `PAV.4`/`PAV.8`. Agora os
labels P4 batem em quinadora_pav4 e o resto (P8 + sem-pavilhão) cai num
fallback genérico → quinadora_pav8 (mesmo schema). GUIFIL continua em guifil.
"""
from __future__ import annotations

import pytest

from app.templates_registry import detect_template, DEFAULT_TEMPLATE

# Todos os labels de quinadora vindos do maquinas.xlsx (desigkanban + desmaq).
QUINADORA_LABELS = [
    "QUINADORA P4", "QUINADORA P8", "QUINADORA ADIRA", "QUINADORA ADIRA MTG4",
    "QUINADORA ADIRA 3M", "QUINADORA ADIRA 14M P4", "QUINADORA ADIRA 14M P8",
    "QUINADORA CÓNICA P8", "QUINADORA MTG3", "QUINADORA PUMA", "QUINADORA PUMA P4",
    "QUINADORA GUIFIL MTG2", "GUIFIL",
]


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
    ("ACABAMENTO MTG2", "acabamento_mtg2"),
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

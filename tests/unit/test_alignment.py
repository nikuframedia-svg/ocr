"""R135 — post-OCR column-alignment guard (PRI<->OF wrong-key emission)."""
from app.dq.alignment import check_and_fix_alignment

_RF = (
    "pri", "cliente", "ov", "of", "modelo", "qtd", "comp_mm",
    "larg_mm", "lote", "coni", "esp", "lbase", "ltopo",
)


def test_valid_row_untouched():
    rows = [{"pri": "2", "cliente": "ENEDIS", "ov": "2603385", "of": "262559"}]
    fixed, flags = check_and_fix_alignment(rows, _RF)
    assert fixed[0]["pri"] == "2" and fixed[0]["of"] == "262559"
    assert flags == []


def test_priority_code_in_of_moves_to_pri():
    rows = [{"pri": "", "cliente": "ENEDIS", "ov": "2603385", "of": "C7"}]
    fixed, flags = check_and_fix_alignment(rows, _RF)
    assert fixed[0]["pri"] == "C7"
    assert fixed[0]["of"] == ""
    assert flags and flags[0]["from"] == "of" and flags[0]["to"] == "pri"


def test_six_digit_of_in_pri_moves_to_of():
    rows = [{"pri": "262559", "cliente": "X", "ov": "2603385", "of": ""}]
    fixed, flags = check_and_fix_alignment(rows, _RF)
    assert fixed[0]["of"] == "262559"
    assert fixed[0]["pri"] == ""
    assert flags and flags[0]["from"] == "pri" and flags[0]["to"] == "of"


def test_malformed_of_is_flagged_not_swapped():
    # PRI already filled → no empty target → flag only, value preserved.
    rows = [{"pri": "1", "cliente": "X", "ov": "2603385", "of": "12ABC"}]
    fixed, flags = check_and_fix_alignment(rows, _RF)
    assert fixed[0]["of"] == "12ABC" and fixed[0]["pri"] == "1"
    assert flags and flags[0].get("field") == "of"


def test_raw_rows_not_mutated():
    rows = [{"pri": "", "cliente": "X", "of": "C7"}]
    check_and_fix_alignment(rows, _RF)
    assert rows[0]["of"] == "C7" and rows[0]["pri"] == ""  # original untouched


def test_template_without_of_is_noop():
    rows = [{"motivo": "avaria", "inicio": "08:00"}]
    fixed, flags = check_and_fix_alignment(rows, ("motivo", "inicio", "fim"))
    assert fixed == rows and flags == []

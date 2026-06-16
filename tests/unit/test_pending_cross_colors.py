from __future__ import annotations

from app.web import main


def test_filled_pav8_cross_fields_with_na_are_marked_pending():
    sheet = {
        "sheet_data": {
            "template_name": "quinadora_pav8",
            "rows": [
                {
                    "cliente": "MTG DELUX",
                    "ov": "",
                    "of": "263301",
                    "modelo": "CB04C68D2",
                    "qtd": "44",
                    "esp": "5",
                    "lbase": "",
                    "ltopo": "",
                }
            ],
        }
    }
    status_map = {
        "rows[0].cliente": "NA",
        "rows[0].of": "NA",
        "rows[0].modelo": "NA",
        "rows[0].qtd": "NA",
        "rows[0].esp": "NA",
    }

    pending = main._build_pending_cross_map(sheet, status_map)

    assert pending == {
        "rows[0].cliente": True,
        "rows[0].of": True,
        "rows[0].modelo": True,
        "rows[0].esp": True,
    }


def test_pending_cross_map_ignores_non_cross_empty_and_matched_cells():
    sheet = {
        "sheet_data": {
            "template_name": "quinadora_pav8",
            "rows": [
                {
                    "cliente": "DRAKOS",
                    "ov": "-",
                    "of": "263006",
                    "modelo": "1598VP26",
                    "qtd": "6",
                    "esp": "",
                }
            ],
        }
    }
    status_map = {
        "rows[0].cliente": "MATCH",
        "rows[0].of": "NO_MATCH",
        "rows[0].modelo": "NA",
        "rows[0].qtd": "NA",
    }

    pending = main._build_pending_cross_map(sheet, status_map)

    assert pending == {"rows[0].modelo": True}


def test_missing_status_for_filled_cross_field_is_pending():
    sheet = {
        "sheet_data": {
            "template_name": "quinadora_pav8",
            "rows": [{"cliente": "TECPOL", "of": "254812", "modelo": ""}],
        }
    }

    pending = main._build_pending_cross_map(sheet, status_map={})

    assert pending == {
        "rows[0].cliente": True,
        "rows[0].of": True,
    }

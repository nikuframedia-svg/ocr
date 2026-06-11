import importlib.util
from pathlib import Path


_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "measure_match.py"
_SPEC = importlib.util.spec_from_file_location("measure_match_script", _SCRIPT)
assert _SPEC and _SPEC.loader
measure_match = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(measure_match)


def test_measure_match_counts_header_footer_and_rows(monkeypatch):
    sheet = {
        "sheet_id": 7,
        "rows": [{
            "row_index": 0,
            "fields": {
                "modelo": {"status": "MATCH", "value": "CGC2E10D", "ref": "CGC2E10D"},
                "of": {"status": "NO_MATCH", "value": "111111", "plan_value": "222222"},
            },
        }],
        "header": {
            "operador": {"status": "MATCH", "value": "ANA", "ref": "ANA"},
            "cod_maquina": {"status": "NA", "value": ""},
        },
        "footer": {
            "horas_trabalhadas": {"status": "NO_MATCH", "value": "ABC", "ref": ""},
        },
    }
    monkeypatch.setattr(
        measure_match.storage,
        "iter_sheet_cross_checks",
        lambda include_stale=False: [sheet],
    )
    monkeypatch.setattr(
        measure_match.storage,
        "load_summary",
        lambda: {"stale_sheets": 3},
    )

    result = measure_match.measure("unit")

    assert result["total"] == 5
    assert result["comparable"] == 4
    assert result["match"] == 2
    assert result["no_match"] == 2
    assert result["na"] == 1
    assert result["match_rate"] == 0.5
    assert result["by_section"] == {
        "footer": {"match": 0, "no_match": 1, "na": 0, "total": 1},
        "header": {"match": 1, "no_match": 0, "na": 1, "total": 2},
        "rows": {"match": 1, "no_match": 1, "na": 0, "total": 2},
    }
    assert result["by_field_total"] == {
        "footer.horas_trabalhadas": 1,
        "modelo": 1,
        "of": 1,
        "header.operador": 1,
    }
    assert result["by_field_no_match"] == {
        "footer.horas_trabalhadas": 1,
        "of": 1,
    }
    assert result["cells_by_id"]["sheet7::header::operador"] == "MATCH"
    assert result["cells_by_id"]["sheet7::footer::horas_trabalhadas"] == "NO_MATCH"
    assert result["no_match_examples"]["of"][0]["field_path"] == "rows[0].of"
    assert result["no_match_examples"]["of"][0]["ref"] == "222222"


def test_measure_match_passes_include_stale_to_storage(monkeypatch):
    seen = {}

    def _iter_sheet_cross_checks(include_stale=False):
        seen["include_stale"] = include_stale
        return []

    monkeypatch.setattr(
        measure_match.storage,
        "iter_sheet_cross_checks",
        _iter_sheet_cross_checks,
    )
    monkeypatch.setattr(
        measure_match.storage,
        "load_summary",
        lambda: {"stale_sheets": 3},
    )

    result = measure_match.measure("unit", include_stale=True)

    assert seen["include_stale"] is True
    assert result["stale_sheets"] == 0
    assert result["stale_available"] == 3

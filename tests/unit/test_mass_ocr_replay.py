from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "eval" / "mass_ocr_replay.py"
SPEC = importlib.util.spec_from_file_location("mass_ocr_replay", SCRIPT)
mass_ocr_replay = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(mass_ocr_replay)


def test_resolve_image_path_handles_dataset_relative_windows_paths(tmp_path):
    dataset = tmp_path / "kb"
    got = mass_ocr_replay.resolve_image_path(dataset, r"images\foo.jpeg")
    assert got == dataset / "images" / "foo.jpeg"


def test_cross_overwrite_applies_concrete_ref_but_not_syntax():
    sheet_data = {
        "header": {},
        "rows": [{"cliente": "STAEK MTG", "qtd": "x"}],
        "footer": {"horas_trabalhadas": "abc"},
    }
    cross = {
        "rows": [
            {
                "row_index": 0,
                "fields": {
                    "cliente": {
                        "status": "NO_MATCH",
                        "engine_status": "very_different",
                        "source": "plan",
                        "value": "ESTOQUE MTG",
                    },
                    "qtd": {
                        "status": "NO_MATCH",
                        "engine_status": "very_different",
                        "source": "syntax",
                        "value": "12",
                    },
                },
            }
        ],
        "footer": {
            "horas_trabalhadas": {
                "status": "NO_MATCH",
                "engine_status": "very_different",
                "source": "syntax",
                "value": "8H",
            }
        },
    }

    applied = mass_ocr_replay.apply_cross_overwrites_in_memory(sheet_data, cross)

    assert sheet_data["rows"][0]["cliente"] == "ESTOQUE MTG"
    assert sheet_data["rows"][0]["qtd"] == "x"
    assert sheet_data["footer"]["horas_trabalhadas"] == "abc"
    assert [a["field_path"] for a in applied] == ["rows[0].cliente"]


def test_header_derivations_apply_machine_code_without_db():
    def resolve_machine_from_setor(setor, refs):
        assert setor == "QUINADORA PUMA"
        assert refs
        return {"codmaq": "M090"}

    runtime = SimpleNamespace(
        refs={"available": True},
        resolve_machine_from_setor=resolve_machine_from_setor,
        snap_operador=lambda *_args, **_kwargs: SimpleNamespace(applied=False, pernr=""),
    )
    sheet_data = {"header": {"setor_maquina": "QUINADORA PUMA", "cod_maquina": ""}}

    applied = mass_ocr_replay.apply_header_derivations_in_memory(sheet_data, runtime)

    assert sheet_data["header"]["cod_maquina"] == "M090"
    assert applied == [
        {
            "field_path": "header.cod_maquina",
            "old": "",
            "new": "M090",
            "source": "maquinas",
        }
    ]


def test_cross_counts_splits_match_regra_from_plan_match():
    cross = {
        "header": {
            "operador": {"status": "MATCH"},
        },
        "rows": [
            {
                "row_index": 0,
                "fields": {
                    "cliente": {"status": "MATCH", "source": "plan"},
                    "qtd": {"status": "MATCH", "source": "syntax", "match_kind": "MATCH_REGRA"},
                    "modelo": {"status": "NO_MATCH"},
                    "lote": {"status": "NA"},
                },
            }
        ],
    }

    counts = mass_ocr_replay.cross_counts(cross)

    assert counts["MATCH"] == 2
    assert counts["MATCH_REGRA"] == 1
    assert counts["NO_MATCH"] == 1
    assert counts["NA"] == 1
    assert counts["TOTAL"] == 5


def test_cross_only_uses_historical_raw_and_reruns_after_overwrite(tmp_path):
    dataset = tmp_path / "kb"
    (dataset / "images").mkdir(parents=True)
    (dataset / "images" / "sheet.jpeg").write_bytes(b"fake")
    calls = []

    def cross_check_sheet(sheet_data, _dq, _refs):
        calls.append(sheet_data["rows"][0]["cliente"])
        if sheet_data["rows"][0]["cliente"] == "ESTOQUE MTG":
            return {
                "rows": [
                    {
                        "row_index": 0,
                        "fields": {"cliente": {"status": "MATCH", "engine_status": "confirmed"}},
                    }
                ]
            }
        return {
            "rows": [
                {
                    "row_index": 0,
                    "fields": {
                        "cliente": {
                            "status": "NO_MATCH",
                            "engine_status": "very_different",
                            "source": "plan",
                            "value": "ESTOQUE MTG",
                        }
                    },
                }
            ]
        }

    runtime = SimpleNamespace(
        refs={"available": True},
        cross_check_sheet=cross_check_sheet,
        resolve_machine_from_setor=lambda *_args, **_kwargs: None,
        snap_operador=lambda *_args, **_kwargs: SimpleNamespace(applied=False, pernr=""),
    )
    row = {
        "id": 1843,
        "image_path": r"images\sheet.jpeg",
        "status": "extracted",
        "historical_template": "guilhotina",
        "raw_extraction": '{"template_name":"guilhotina","rows":[{"cliente":"STAEK MTG"}]}',
        "sheet_data": '{"template_name":"guilhotina","rows":[{"cliente":"OLD FINAL"}]}',
        "dq_audit": "{}",
        "shadow_scoring_json": "{}",
    }

    rec = mass_ocr_replay.run_sheet_cross_only(row, dataset, runtime)

    assert rec["mode"] == "cross_only"
    assert calls == ["STAEK MTG", "ESTOQUE MTG"]
    assert rec["sheet_data_final"]["rows"][0]["cliente"] == "ESTOQUE MTG"
    assert rec["counts"]["MATCH"] == 1

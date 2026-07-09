"""R257 — o avaliador não pode pontuar células que só o motor emite.

Bug (auditoria externa, confirmado no run r249 oficial): _iter_cases pontuava
a união raw ∪ truth ∪ set(células emitidas pelo PRÓPRIO motor) — cada motor
tinha um denominador diferente, e um candidato que emitisse células extra
(vazias/auto-consistentes) inflacionava o headline: as 492 células
só-no-candidato do r249 contaram TODAS como corretas (+1,61pp anunciado;
−1,34pp na base comum). Corrigido: pontua-se apenas raw ∪ truth.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPT = (Path(__file__).resolve().parents[2]
           / "scripts" / "diag" / "evaluate_cross_outputs.py")


@pytest.fixture()
def evaluator(monkeypatch):
    spec = importlib.util.spec_from_file_location("evaluate_cross_outputs",
                                                  _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    # O dataclass CellCase resolve o namespace via sys.modules[__module__].
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    def _fake_cross_check_sheet(raw_sheet, _dq, _refs):
        # Motor "batoteiro": devolve as células do raw + 3 células INVENTADAS
        # (paths que não existem nem no raw nem no truth), todas
        # auto-consistentes — no código antigo entravam no denominador e
        # contavam como corretas.
        rows_fields = {}
        for f, v in (raw_sheet.get("rows") or [{}])[0].items():
            rows_fields[f] = {"value": v, "status": "MATCH", "source": "plan"}
        for extra in ("campo_fantasma_1", "campo_fantasma_2", "campo_fantasma_3"):
            rows_fields[extra] = {"value": "", "status": "NA", "source": "plan"}
        return {"header": {}, "footer": {},
                "rows": [{"row_index": 0, "fields": rows_fields}]}

    monkeypatch.setattr(mod, "cross_check_sheet", _fake_cross_check_sheet)
    monkeypatch.setattr(mod, "_get_indices", lambda _refs: {})
    return mod


def _mk_sample_dir(tmp_path: Path) -> Path:
    raw = {"header": {}, "rows": [{"of": "123456", "modelo": "CP-1200"}],
           "footer": {}}
    truth = {"header": {}, "rows": [{"of": "123456", "modelo": "CP-1200"}],
             "footer": {}}
    (tmp_path / "raw.json").write_text(json.dumps(raw), encoding="utf-8")
    (tmp_path / "truth.json").write_text(json.dumps(truth), encoding="utf-8")
    (tmp_path / "manifest.csv").write_text(
        "sheet_id;ocr_original;resultado_atual\n1;raw.json;truth.json\n",
        encoding="utf-8",
    )
    return tmp_path


def test_engine_only_cells_do_not_enter_the_denominator(evaluator, tmp_path):
    sample = _mk_sample_dir(tmp_path)
    cases = evaluator._iter_cases(sample, refs={}, sections={"rows"})
    paths = {c.path for c in cases}
    # As células reais (raw ∪ truth) estão lá…
    assert "rows[0].of" in paths and "rows[0].modelo" in paths
    # …mas as inventadas pelo motor NÃO entram na pontuação.
    assert not any("campo_fantasma" in p for p in paths)
    assert len(cases) == 2

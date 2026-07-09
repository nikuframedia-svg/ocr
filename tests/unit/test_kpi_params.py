"""Task C E3 — persistência das fórmulas de KPI (data/kpi_params.json)."""
from __future__ import annotations

import copy
import json

import pytest

from app.web import kpi_params


@pytest.fixture(autouse=True)
def tmp_params(tmp_path, monkeypatch):
    path = tmp_path / "kpi_params.json"
    monkeypatch.setattr(kpi_params, "_PARAMS_PATH", path)
    kpi_params.invalidate_cache()
    yield path
    kpi_params.invalidate_cache()


def _edit(kpis, kid, **changes):
    out = copy.deepcopy(kpis)
    for k in out:
        if k["id"] == kid:
            k.update(changes)
    return out


class TestDefaults:
    def test_no_file_returns_defaults(self, tmp_params):
        state = kpi_params.load_state()
        assert state["version"] == 0
        assert state["kpis"] == kpi_params.DEFAULT_KPIS
        assert state["history"] == []
        assert not tmp_params.exists()  # ler não cria ficheiro

    def test_corrupt_json_falls_back(self, tmp_params):
        tmp_params.write_text("{ not json !!", encoding="utf-8")
        kpi_params.invalidate_cache()
        state = kpi_params.load_state()
        assert state["kpis"] == kpi_params.DEFAULT_KPIS

    def test_wrong_structure_falls_back(self, tmp_params):
        tmp_params.write_text(json.dumps({"kpis": "nope"}), encoding="utf-8")
        kpi_params.invalidate_cache()
        assert kpi_params.load_state()["kpis"] == kpi_params.DEFAULT_KPIS


class TestSave:
    def test_save_and_reload(self, tmp_params):
        kpis = _edit(kpi_params.DEFAULT_KPIS, "col_per_h", expr="qtd / horas * 2")
        state = kpi_params.save_kpis(kpis, expected_version=0)
        assert state["version"] == 1
        got = {k["id"]: k for k in kpi_params.get_kpis()}
        assert got["col_per_h"]["expr"] == "qtd / horas * 2"
        # ficheiro no disco, JSON válido
        raw = json.loads(tmp_params.read_text(encoding="utf-8"))
        assert raw["version"] == 1

    def test_version_conflict(self, tmp_params):
        kpi_params.save_kpis(kpi_params.DEFAULT_KPIS, expected_version=0)
        with pytest.raises(kpi_params.KpiVersionConflict):
            kpi_params.save_kpis(kpi_params.DEFAULT_KPIS, expected_version=0)

    def test_invalid_formula_rejected(self, tmp_params):
        kpis = _edit(kpi_params.DEFAULT_KPIS, "col_per_h", expr="qtd / naoexiste")
        with pytest.raises(ValueError) as ei:
            kpi_params.save_kpis(kpis, expected_version=0)
        errors = json.loads(str(ei.value))
        assert "col_per_h" in errors
        assert not tmp_params.exists()  # nada gravado

    def test_duplicate_id_rejected(self, tmp_params):
        kpis = copy.deepcopy(kpi_params.DEFAULT_KPIS)
        kpis.append(copy.deepcopy(kpis[0]))
        with pytest.raises(ValueError):
            kpi_params.save_kpis(kpis, expected_version=0)

    def test_reserved_id_rejected(self, tmp_params):
        kpis = copy.deepcopy(kpi_params.DEFAULT_KPIS)
        kpis.append({"id": "colunas", "label": "x", "expr": "qtd", "unit": "",
                     "round": 1, "compat": None, "fmt": None, "target": None,
                     "direction": "higher", "scopes": ["totals"]})
        with pytest.raises(ValueError):
            kpi_params.save_kpis(kpis, expected_version=0)

    def test_custom_kpi_saved(self, tmp_params):
        kpis = copy.deepcopy(kpi_params.DEFAULT_KPIS)
        kpis.append({"id": "kg_por_col", "label": "Kg por coluna",
                     "expr": "kg_produzido / qtd", "unit": "kg/col",
                     "round": 1, "compat": None, "fmt": None, "target": 250.0,
                     "direction": "lower", "scopes": ["totals"]})
        kpi_params.save_kpis(kpis, expected_version=0)
        got = {k["id"]: k for k in kpi_params.get_kpis()}
        assert got["kg_por_col"]["target"] == 250.0

    def test_emit_event(self, tmp_params, monkeypatch):
        events = []
        from app import kernel
        monkeypatch.setattr(kernel, "emit_event",
                            lambda t, p=None: events.append((t, p)) or {})
        kpi_params.save_kpis(kpi_params.DEFAULT_KPIS, expected_version=0)
        assert events and events[0][0] == "kpi_params_changed"

    def test_kpi_params_changed_is_valid_event_type(self):
        from app import kernel
        assert "kpi_params_changed" in kernel.EVENT_TYPES


class TestHistoryAndRevert:
    def test_history_grows_and_caps(self, tmp_params):
        for i in range(23):
            kpis = _edit(kpi_params.DEFAULT_KPIS, "col_per_h",
                         expr=f"qtd / horas + {i}")
            kpi_params.save_kpis(kpis, expected_version=i)
        state = kpi_params.load_state()
        assert state["version"] == 23
        assert len(state["history"]) == 20  # cap

    def test_revert_to_defaults(self, tmp_params):
        kpis = _edit(kpi_params.DEFAULT_KPIS, "col_per_h", expr="qtd * 99")
        kpi_params.save_kpis(kpis, expected_version=0)
        state = kpi_params.revert_kpis("defaults")
        assert state["version"] == 2
        got = {k["id"]: k for k in state["kpis"]}
        assert got["col_per_h"]["expr"] == "qtd / horas"

    def test_revert_to_history_entry(self, tmp_params):
        v1 = _edit(kpi_params.DEFAULT_KPIS, "col_per_h", expr="qtd / horas + 1")
        kpi_params.save_kpis(v1, expected_version=0)
        v2 = _edit(kpi_params.DEFAULT_KPIS, "col_per_h", expr="qtd / horas + 2")
        kpi_params.save_kpis(v2, expected_version=1)
        state = kpi_params.load_state()
        # history[-1] guarda o estado ANTES da última gravação (ou seja, v1)
        idx = len(state["history"]) - 1
        reverted = kpi_params.revert_kpis(idx)
        got = {k["id"]: k for k in reverted["kpis"]}
        assert got["col_per_h"]["expr"] == "qtd / horas + 1"

    def test_revert_bad_index(self, tmp_params):
        with pytest.raises(ValueError):
            kpi_params.revert_kpis(99)


class TestMerge:
    def test_missing_default_reappears(self, tmp_params):
        # ficheiro gravado sem o KPI 'toneladas' (versão antiga) → reaparece
        kpis = [k for k in kpi_params.DEFAULT_KPIS if k["id"] != "toneladas"]
        tmp_params.write_text(json.dumps(
            {"version": 1, "kpis": kpis, "history": []}), encoding="utf-8")
        kpi_params.invalidate_cache()
        ids = [k["id"] for k in kpi_params.get_kpis()]
        assert "toneladas" in ids

    def test_hand_edited_partial_entry(self, tmp_params):
        # entrada só com id+expr (editada à mão) → completa com o default
        tmp_params.write_text(json.dumps({
            "version": 1, "history": [],
            "kpis": [{"id": "col_per_h", "expr": "qtd / horas * 3"}],
        }), encoding="utf-8")
        kpi_params.invalidate_cache()
        got = {k["id"]: k for k in kpi_params.get_kpis()}
        assert got["col_per_h"]["expr"] == "qtd / horas * 3"
        assert got["col_per_h"]["compat"] == "zero_fallback"
        assert got["col_per_h"]["scopes"] == ["totals", "sector", "machine"]


class TestComputeScopeKpis:
    VARS = {"qtd": 12, "horas": 4.0, "kg_consumido": 2000.0,
            "kg_produzido": 1500.0, "kg_desperdicio": 500.0, "chapas": 7,
            "n_folhas": 3, "n_operadores": 2}

    def test_totals_match_legacy_formulas(self):
        out = kpi_params.compute_scope_kpis("totals", self.VARS)
        assert out["col_per_h"] == 3.0
        assert out["min_per_col"] == 20.0
        assert out["col_per_operador"] == 6.0
        assert out["toneladas_consumido"] == 2.0
        assert out["toneladas_produzido"] == 1.5
        assert out["chapas_total"] == 7
        assert isinstance(out["chapas_total"], int)
        assert out["perc_desperdicio"] == 25.0
        assert out["toneladas"] == 1.5

    def test_zero_fallback(self):
        vars0 = dict(self.VARS, horas=0.0, qtd=0, n_operadores=0)
        out = kpi_params.compute_scope_kpis("totals", vars0)
        assert out["col_per_h"] == 0
        assert out["min_per_col"] == 0
        assert out["col_per_operador"] == 0

    def test_hide_if_zero(self):
        varsz = dict(self.VARS, kg_consumido=0.0, kg_produzido=0.0,
                     kg_desperdicio=0.0, chapas=0)
        out = kpi_params.compute_scope_kpis("totals", varsz)
        assert out["toneladas_consumido"] is None
        assert out["toneladas_produzido"] is None
        assert out["chapas_total"] is None
        assert out["perc_desperdicio"] is None  # div/0 → None (compat None)

    def test_small_positive_not_hidden(self):
        # 40 kg → 0.04 t → mostra "0.0", não oculta (gate antes do round)
        varsz = dict(self.VARS, kg_consumido=40.0)
        out = kpi_params.compute_scope_kpis("totals", varsz)
        assert out["toneladas_consumido"] == 0.0

    def test_broken_formula_falls_back_to_default(self):
        defs = kpi_params.normalize_kpis(
            [dict(k) for k in kpi_params.DEFAULT_KPIS])
        for k in defs:
            if k["id"] == "col_per_h":
                k["expr"] = "qtd / (("  # sintaxe inválida
        out = kpi_params.compute_scope_kpis("totals", self.VARS, defs)
        assert out["col_per_h"] == 3.0  # default aplicado

    def test_sector_scope(self):
        out = kpi_params.compute_scope_kpis(
            "sector", {"qtd": 10, "horas": 5.0, "n_folhas": 2, "n_linhas": 4})
        assert out["col_per_h"] == 2.0
        assert out["min_per_col"] == 30.0
        assert "toneladas" not in out  # scope totals-only

"""Task C E3 — production_overview com fórmulas editáveis.

REGRESSÃO central: sem data/kpi_params.json o output tem de ser idêntico
às fórmulas históricas (R29/R34/R72). Depois: override reflete-se; fórmula
partida em runtime cai na default.
"""
from __future__ import annotations

import json

import pytest
from app.web import db, kpi_params, kpis


@pytest.fixture(autouse=True)
def tmp_env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "app.db")
    db.init_db()
    monkeypatch.setattr(kpi_params, "_PARAMS_PATH", tmp_path / "kpi_params.json")
    kpi_params.invalidate_cache()

    # refs vazias — buckets estáticos + sem pesos diretos
    class _W:
        def get_refs(self):
            return {}

    import app.cross_check.ref_watcher as rw
    monkeypatch.setattr(rw, "get_watcher", _W)
    yield
    kpi_params.invalidate_cache()


def _seed(qtd=12, hours=4.0, day="2026-07-01", operador="ANA"):
    with db.conn() as c:
        c.execute("INSERT INTO sheets (image_path, status, sheet_data) VALUES (?,?,?)",
                  ("x.jpg", "validated",
                   json.dumps({"header": {"setor_maquina": "Corte"}})))
        sid = c.execute("SELECT MAX(id) AS i FROM sheets").fetchone()["i"]
        c.execute(
            "INSERT INTO production_rows (sheet_id, row_index, operador, "
            "sheet_iso_date, sheet_hours, qtd) VALUES (?,?,?,?,?,?)",
            (sid, 0, operador, day, hours, qtd))
    return sid


def _tecpoles_refs():
    entry = {
        "of": "251651",
        "ov": "2500854",
        "cliente": "TECPOLES GMBH",
        "designacao": "TSA20 16-20M 1234TJ23 - Nº2 1234T823 1/2",
        "comp": 5154,
        "lbase": 1170,
        "ltopo": 900,
        "esp": 5,
        "npecas": 1,
        "pesounit": 416,
    }
    return {"of_to_entries": {"251651": [entry]}}


class TestRegressionNoFile:
    def test_totals_legacy_values(self):
        _seed(qtd=12, hours=4.0)
        ov = kpis.production_overview("2026-07-01", "day")
        t = ov["totals"]
        # fórmulas históricas calculadas à mão
        assert t["colunas"] == 12
        assert t["col_per_h"] == 3.0
        assert t["min_per_col"] == 20.0
        assert t["hours"] == 4.0
        assert t["col_per_operador"] == 12.0
        assert t["n_operadores"] == 1
        assert t["n_sheets"] == 1
        # sem pesos diretos (refs vazias) → hide_if_zero
        assert t["toneladas_consumido"] is None
        assert t["toneladas_produzido"] is None
        assert t["chapas_total"] is None
        assert t["perc_desperdicio"] is None
        assert t["toneladas"] is None

    def test_totals_keys_complete(self):
        _seed()
        t = kpis.production_overview("2026-07-01", "day")["totals"]
        expected = {"colunas", "col_per_h", "min_per_col", "hours",
                    "toneladas_consumido", "toneladas_produzido",
                    "chapas_total", "perc_desperdicio", "toneladas",
                    "n_sheets", "n_operadores", "col_per_operador"}
        assert expected <= set(t.keys())

    def test_empty_day_zero_fallbacks(self):
        ov = kpis.production_overview("2026-07-02", "day")
        t = ov["totals"]
        assert t["colunas"] == 0
        assert t["col_per_h"] == 0
        assert t["min_per_col"] == 0
        assert t["col_per_operador"] == 0

    def test_sector_values(self):
        _seed(qtd=12, hours=4.0)
        ov = kpis.production_overview("2026-07-01", "day")
        corte = next(s for s in ov["sectors"] if s["name"] == "Corte")
        assert corte["has_data"] is True
        assert corte["col_per_h"] == 3.0
        assert corte["min_per_col"] == 20.0
        vazio = next(s for s in ov["sectors"] if s["name"] == "Soldadura")
        assert vazio["col_per_h"] is None  # gate has_data preservado

    def test_kpi_cards_present(self):
        _seed()
        ov = kpis.production_overview("2026-07-01", "day")
        cards = {c["id"]: c for c in ov["kpi_cards"]}
        assert cards["col_per_h"]["value"] == 3.0
        assert cards["col_per_h"]["meets_target"] is None  # sem meta default
        assert ov["kpi_variables"]["totals"]["qtd"] == 12

    def test_bobine_totals_use_geometric_produced_weight(self, monkeypatch):
        class _Watcher:
            def get_refs(self):
                return _tecpoles_refs()

        import app.cross_check.ref_watcher as rw
        monkeypatch.setattr(rw, "get_watcher", _Watcher)

        with db.conn() as c:
            c.execute(
                "INSERT INTO sheets (image_path, status, sheet_data) VALUES (?,?,?)",
                (
                    "tecpoles.jpg",
                    "validated",
                    json.dumps({"header": {"setor_maquina": "Bobine Formato"}}),
                ),
            )
            sid = c.execute("SELECT MAX(id) AS i FROM sheets").fetchone()["i"]
            c.execute(
                "INSERT INTO production_rows ("
                "sheet_id, row_index, operador, sheet_iso_date, sheet_hours, "
                "of, modelo, qtd, comp_mm, larg_mm, esp, lbase, ltopo"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    sid, 0, "LUÍS", "2026-08-02", 1.0,
                    "251651", "1234TJ23", 8, 5154, 1250, 5, 1170, 900,
                ),
            )

        overview = kpis.production_overview("2026-08-02", "day")
        variables = overview["kpi_variables"]["totals"]
        totals = overview["totals"]

        assert variables["kg_consumido"] == pytest.approx(2022.945)
        assert variables["kg_produzido"] == pytest.approx(1674.99846)
        assert variables["kg_desperdicio"] == pytest.approx(347.94654)
        assert variables["chapas"] == 8
        assert totals["toneladas_consumido"] == 2.0
        assert totals["toneladas_produzido"] == 1.7
        assert totals["perc_desperdicio"] == 17.2
        assert totals["chapas_total"] == 8


class TestOverrides:
    def test_override_reflected(self):
        _seed(qtd=12, hours=4.0)
        kpis_new = [dict(k) for k in kpi_params.DEFAULT_KPIS]
        for k in kpis_new:
            if k["id"] == "col_per_h":
                k["expr"] = "qtd / horas * 2"
                k["target"] = 5.0
        kpi_params.save_kpis(kpis_new, expected_version=0)
        ov = kpis.production_overview("2026-07-01", "day")
        assert ov["totals"]["col_per_h"] == 6.0
        card = next(c for c in ov["kpi_cards"] if c["id"] == "col_per_h")
        assert card["target"] == 5.0
        assert card["meets_target"] is True  # 6.0 >= 5.0, direction higher

    def test_custom_kpi_in_cards(self):
        _seed(qtd=12, hours=4.0)
        kpis_new = [dict(k) for k in kpi_params.DEFAULT_KPIS]
        kpis_new.append({
            "id": "folhas_por_op", "label": "Folhas por operador",
            "expr": "n_folhas / n_operadores", "unit": "un", "round": 1,
            "compat": None, "fmt": None, "target": None,
            "direction": "higher", "scopes": ["totals"]})
        kpi_params.save_kpis(kpis_new, expected_version=0)
        ov = kpis.production_overview("2026-07-01", "day")
        assert ov["totals"]["folhas_por_op"] == 1.0
        assert any(c["id"] == "folhas_por_op" for c in ov["kpi_cards"])

    def test_broken_file_formula_falls_back(self):
        # ficheiro escrito à mão com fórmula inválida (bypass ao save)
        _seed(qtd=12, hours=4.0)
        bad = [dict(k) for k in kpi_params.DEFAULT_KPIS]
        for k in bad:
            if k["id"] == "col_per_h":
                k["expr"] = "qtd / (("
        kpi_params._PARAMS_PATH.write_text(json.dumps(
            {"version": 1, "kpis": bad, "history": []}), encoding="utf-8")
        kpi_params.invalidate_cache()
        ov = kpis.production_overview("2026-07-01", "day")
        assert ov["totals"]["col_per_h"] == 3.0  # default de fábrica

    def test_kpi_defs_param_preview(self):
        # preview: defs candidatos SEM gravar
        _seed(qtd=12, hours=4.0)
        candidate = [dict(k) for k in kpi_params.DEFAULT_KPIS]
        for k in candidate:
            if k["id"] == "col_per_h":
                k["expr"] = "qtd / horas * 10"
        ov = kpis.production_overview("2026-07-01", "day", kpi_defs=candidate)
        assert ov["totals"]["col_per_h"] == 30.0
        assert not kpi_params._PARAMS_PATH.exists()

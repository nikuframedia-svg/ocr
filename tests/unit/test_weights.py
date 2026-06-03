from __future__ import annotations

import pytest
from app.dq.geometry import column_weight_kg
from app.production.weights import calculate_row_weights


def _refs(*, npecas=6, pesounit=None, sap_esp=2.6, sap_larg=1500):
    if pesounit is None:
        pesounit = column_weight_kg(200, 150, 5000, 2.6)
    entry = {
        "of": "261860",
        "ov": "100200",
        "cliente": "ENEDIS",
        "designacao": "CGC2E10D",
        "comp": 5000,
        "lbase": 200,
        "ltopo": 150,
        "esp": 2.6,
        "npecas": npecas,
        "pesounit": pesounit,
    }
    return {
        "of_to_entries": {
            "261860": [entry],
        },
        "plan_by_ov": {"100200": [{**entry, "_of": "261860"}]},
        "lotes_sap_full": {
            "L1": {"esp": sap_esp, "larg": sap_larg},
        },
    }


def _bobine_row(**extra):
    row = {
        "setor_maquina": "BOBINE-FORMATO",
        "of": "261860",
        "ov": "100200",
        "cliente": "ENEDIS",
        "modelo": "CGC2E10D",
        "qtd": 5,
        "comp_mm": 9999,
        "larg_mm": 999,
        "lote": "L1",
        "esp": 26,
        "lbase": 999,
        "ltopo": 999,
    }
    row.update(extra)
    return row


def test_bobine_uses_plan_and_sap_for_weight_inputs():
    out = calculate_row_weights(_bobine_row(), _refs())

    assert out.direct_consumption is True
    assert out.n_chapas == 1
    assert out.comp_mm == 5000
    assert out.larg_mm == 1500
    assert out.esp_mm == 2.6
    assert out.peso_consumido_kg == pytest.approx(153.075)
    assert out.peso_produzido_kg == pytest.approx(
        5 * column_weight_kg(200, 150, 5000, 2.6)
    )


def test_bad_ocr_esp_is_overridden_by_sap():
    out = calculate_row_weights(_bobine_row(esp=26), _refs(sap_esp=2.6))

    assert out.esp_mm == 2.6
    assert out.peso_consumido_kg == pytest.approx(153.075)


def test_plan_npecas_above_legacy_formula_is_respected():
    out = calculate_row_weights(_bobine_row(qtd=16), _refs(npecas=8))

    assert out.n_chapas == 2


def test_plan_pesounit_wins_for_produced_weight():
    out = calculate_row_weights(_bobine_row(qtd=3), _refs(pesounit=50))

    assert out.produced_source == "plan_pesounit"
    assert out.peso_produzido_kg == pytest.approx(150)


def test_waste_percent_is_over_consumed_weight():
    out = calculate_row_weights(_bobine_row(), _refs())

    assert out.desperdicio_kg == pytest.approx(
        out.peso_consumido_kg - out.peso_produzido_kg
    )
    assert out.desperdicio_pct == pytest.approx(
        out.desperdicio_kg / out.peso_consumido_kg * 100
    )


def test_acabamento_only_gets_produced_weight_from_plan():
    row = {
        "setor_maquina": "ACABAMENTO MTG4",
        "of": "261860",
        "modelo": "CGC2E10D",
        "qtd": 10,
    }
    out = calculate_row_weights(row, _refs(pesounit=50))

    assert out.direct_consumption is False
    assert out.peso_produzido_kg == pytest.approx(500)
    assert out.peso_consumido_kg is None
    assert out.n_chapas is None
    assert out.desperdicio_kg is None
    assert out.desperdicio_pct is None


def test_expedicao_gets_produced_weight_from_unique_ov_model_without_of():
    row = {
        "setor_maquina": "EXPEDIÇÃO",
        "of": "",
        "ov": "100200",
        "modelo": "CGC2E10D",
        "qtd": 10,
    }
    out = calculate_row_weights(row, _refs(pesounit=50))

    assert out.direct_consumption is False
    assert out.peso_produzido_kg == pytest.approx(500)
    assert out.peso_consumido_kg is None
    assert out.n_chapas is None
    assert out.desperdicio_kg is None


def test_expedicao_wrong_existing_of_uses_unique_ov_model_for_weight():
    refs = _refs(pesounit=50)
    refs["of_to_entries"]["999999"] = [{
        "of": "999999",
        "ov": "999000",
        "cliente": "OTHER",
        "designacao": "OUTRA PECA",
        "pesounit": 99,
    }]
    row = {
        "setor_maquina": "EXPEDIÇÃO",
        "of": "999999",
        "ov": "100200",
        "modelo": "CGC2E10D",
        "qtd": 10,
    }
    out = calculate_row_weights(row, refs)

    assert out.peso_produzido_kg == pytest.approx(500)
    assert out.peso_consumido_kg is None


def test_expedicao_ambiguous_ov_model_leaves_weights_empty():
    refs = _refs(pesounit=50)
    refs["plan_by_ov"]["100200"].append({
        "_of": "999999",
        "of": "999999",
        "ov": "100200",
        "cliente": "ENEDIS",
        "designacao": "CGC2E10D",
        "pesounit": 99,
    })
    row = {
        "setor_maquina": "EXPEDIÇÃO",
        "of": "",
        "ov": "100200",
        "modelo": "CGC2E10D",
        "qtd": 10,
    }
    out = calculate_row_weights(row, refs)

    assert out.peso_produzido_kg is None
    assert out.peso_consumido_kg is None


def test_missing_refs_and_geometry_returns_empty_weights():
    out = calculate_row_weights({
        "setor_maquina": "ACABAMENTO MTG4",
        "of": "999999",
        "qtd": 5,
    }, refs={})

    assert out.peso_produzido_kg is None
    assert out.peso_consumido_kg is None
    assert out.desperdicio_kg is None
    assert out.desperdicio_pct is None

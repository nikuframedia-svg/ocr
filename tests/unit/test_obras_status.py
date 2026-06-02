"""R115 — Tests do módulo obras_status."""
from __future__ import annotations

import pytest
from app.pipeline import obras_status, of_consumption


@pytest.fixture(autouse=True)
def _reset():
    obras_status.invalidate_cache()
    of_consumption.invalidate_cache()
    yield
    obras_status.invalidate_cache()
    of_consumption.invalidate_cache()


def _e(of, cliente, ov, desig, quanttrp, fases=None, fechado="0", **extra):
    return {
        "of": of,
        "cliente": cliente,
        "ov": ov,
        "designacao": desig,
        "quanttrp": quanttrp,
        "fases": fases or {},
        "fechado": fechado,
        **extra,
    }


# ----- build_ov_index ------------------------------------------------

class TestBuildOvIndex:
    def test_groups_entries_by_ov(self):
        refs = {
            "of_to_entries": {
                "100": [
                    _e("100", "ACME", "OV-A", "X", 5),
                    _e("100", "ACME", "OV-B", "Y", 3),
                ],
                "200": [_e("200", "BAR", "OV-A", "Z", 7)],
            },
        }
        idx = obras_status.build_ov_index(refs)
        assert set(idx.keys()) == {"OV-A", "OV-B"}
        assert len(idx["OV-A"]) == 2
        # _of stamped do dicionário top-level
        assert all(e.get("_of") for e in idx["OV-A"])

    def test_empty_ov_is_skipped(self):
        refs = {"of_to_entries": {"100": [_e("100", "ACME", "", "X", 5)]}}
        assert obras_status.build_ov_index(refs) == {}

    def test_no_refs(self):
        assert obras_status.build_ov_index({}) == {}


# ----- aggregate_ov --------------------------------------------------

class TestAggregateOv:
    def test_single_entry(self):
        e = _e("100", "ACME", "OV1", "MOD-A", 10,
               fases={"bf": 10, "c": 8, "q": 5},
               comp=1000, lbase=200)
        e["_of"] = "100"
        agg = obras_status.aggregate_ov("OV1", [e], kanbans=[], consumption={})
        assert agg["ov"] == "OV1"
        assert agg["cliente"] == "ACME"
        assert agg["planned"] == 10
        assert agg["produced"] == 5
        assert agg["remaining"] == 5
        assert agg["pct_complete"] == 50.0
        assert agg["all_closed"] is False
        assert agg["fase_totals"] == {"bf": 10.0, "c": 8.0, "q": 5.0}
        assert agg["last_kanban"] is None
        assert len(agg["entries"]) == 1

    def test_multiple_ofs_and_modelos(self):
        entries = [
            {**_e("100", "ACME", "OV1", "MOD-A", 5,
                  fases={"bf": 5, "c": 2}), "_of": "100"},
            {**_e("200", "ACME", "OV1", "MOD-B", 3,
                  fases={"bf": 1}), "_of": "200"},
        ]
        agg = obras_status.aggregate_ov("OV1", entries, kanbans=[], consumption={})
        assert agg["ofs"] == ["100", "200"]
        assert "MOD-A" in agg["modelos"] and "MOD-B" in agg["modelos"]
        assert agg["planned"] == 8
        assert agg["produced"] == 3
        assert agg["remaining"] == 5

    def test_downstream_phase_prevents_acabamento_from_closing_early(self):
        entry = {
            **_e(
                "300",
                "ACME",
                "OV1",
                "MOD-C",
                20,
                fases={"bf": 20, "c": 20, "q": 20, "s": 10, "r": 10, "a": 10},
            ),
            "_of": "300",
        }
        agg = obras_status.aggregate_ov("OV1", [entry], kanbans=[], consumption={})
        assert agg["produced"] == 10
        assert agg["remaining"] == 10
        assert agg["pct_complete"] == 50.0
        assert agg["all_closed"] is False
        assert agg["entries"][0]["done"] is False

    def test_last_kanban_picked_from_first(self):
        e = {**_e("100", "ACME", "OV1", "X", 10), "_of": "100"}
        kbs = [
            {"sheet_id": 99, "captured_at": "2026-05-20 14:00:00",
             "operador": "AUGUSTO", "setor_maquina": "Quinadora",
             "qtd": 3, "modelo": "X", "of": "100",
             "sheet_iso_date": "2026-05-20"},
            {"sheet_id": 50, "captured_at": "2026-05-19 10:00:00",
             "operador": "JÚLIO", "setor_maquina": "Soldadura",
             "qtd": 2, "modelo": "X", "of": "100",
             "sheet_iso_date": "2026-05-19"},
        ]
        agg = obras_status.aggregate_ov("OV1", [e], kanbans=kbs, consumption={})
        assert agg["last_kanban"]["sheet_id"] == 99
        assert agg["last_kanban"]["operador"] == "AUGUSTO"
        assert agg["n_kanbans"] == 2
        assert agg["recent_kanbans"] == kbs

    def test_entries_sorted_faltam_menos_primeiro(self):
        entries = [
            {**_e("100", "X", "OV1", "A", 10, fases={"bf": 2}), "_of": "100"},  # rem=8
            {**_e("100", "X", "OV1", "B", 10, fases={"bf": 8}), "_of": "100"},  # rem=2
            {**_e("100", "X", "OV1", "C", 10, fases={"bf": 5}), "_of": "100"},  # rem=5
        ]
        agg = obras_status.aggregate_ov("OV1", entries, [], consumption={})
        desigs = [r["designacao"] for r in agg["entries"]]
        assert desigs == ["B", "C", "A"]

    def test_done_entries_go_last(self):
        entries = [
            {**_e("100", "X", "OV1", "A", 10, fases={"bf": 3}), "_of": "100"},
            {**_e("100", "X", "OV1", "DONE",
                  10, fases={"bf": 10}), "_of": "100"},
        ]
        agg = obras_status.aggregate_ov("OV1", entries, [], consumption={})
        # DONE foi 10/10, vai para o fim
        assert agg["entries"][-1]["designacao"] == "DONE"
        assert agg["entries"][-1]["done"] is True


# ----- aggregate_cliente ---------------------------------------------

class TestAggregateCliente:
    def test_sums_across_ovs(self):
        ovs = [
            {"planned": 100, "produced": 80, "remaining": 20,
             "all_closed": False, "last_kanban": {"captured_at": "2026-05-20 10:00"}},
            {"planned": 50, "produced": 50, "remaining": 0,
             "all_closed": True, "last_kanban": None},
        ]
        c = obras_status.aggregate_cliente("ACME", ovs)
        assert c["n_ovs"] == 2
        assert c["n_open"] == 1
        assert c["n_closed"] == 1
        assert c["planned"] == 150
        assert c["produced"] == 130
        assert c["remaining"] == 20
        assert c["pct_complete"] == round(130 / 150 * 100, 1)
        assert c["last_kanban"]["captured_at"] == "2026-05-20 10:00"

    def test_picks_most_recent_last_kanban(self):
        ovs = [
            {"planned": 10, "produced": 5, "remaining": 5, "all_closed": False,
             "last_kanban": {"captured_at": "2026-05-10 09:00"}},
            {"planned": 10, "produced": 5, "remaining": 5, "all_closed": False,
             "last_kanban": {"captured_at": "2026-05-20 18:30"}},
        ]
        c = obras_status.aggregate_cliente("ACME", ovs)
        assert c["last_kanban"]["captured_at"] == "2026-05-20 18:30"


# ----- compute_obras_status (cache + filtros) ------------------------

class TestComputeObrasStatus:
    def test_filter_closed_off_by_default(self, monkeypatch):
        # Mock refs com 1 OV open + 1 OV fechada
        refs = {
            "of_to_entries": {
                "100": [_e("100", "ACME", "OPEN", "X", 10, fases={"bf": 3})],
                "200": [_e("200", "ACME", "DONE", "Y", 5, fases={"bf": 5})],
            },
        }
        monkeypatch.setattr(
            obras_status, "get_watcher",
            lambda: type("W", (), {"get_refs": lambda self: refs})(),
        )
        monkeypatch.setattr(obras_status, "_kanbans_by_ov", lambda: {})
        monkeypatch.setattr(obras_status, "get_consumption", lambda: {})

        result = obras_status.compute_obras_status(include_closed=False)
        all_ovs = [ov for c in result["clientes"] for ov in c["ovs"]]
        ov_names = [o["ov"] for o in all_ovs]
        assert "OPEN" in ov_names
        assert "DONE" not in ov_names

    def test_include_closed_keeps_them(self, monkeypatch):
        refs = {
            "of_to_entries": {
                "200": [_e("200", "ACME", "DONE", "Y", 5, fases={"bf": 5})],
            },
        }
        monkeypatch.setattr(
            obras_status, "get_watcher",
            lambda: type("W", (), {"get_refs": lambda self: refs})(),
        )
        monkeypatch.setattr(obras_status, "_kanbans_by_ov", lambda: {})
        monkeypatch.setattr(obras_status, "get_consumption", lambda: {})

        result = obras_status.compute_obras_status(include_closed=True)
        assert result["n_ovs"] == 1

    def test_cache_within_ttl(self, monkeypatch):
        calls = {"n": 0}

        def fake_build(include_closed):
            calls["n"] += 1
            return {"clientes": [], "n_clientes": 0, "n_ovs": 0,
                    "n_open_ovs": 0, "generated_at": 1.0,
                    "include_closed": include_closed}

        monkeypatch.setattr(obras_status, "_build_status", fake_build)
        obras_status.compute_obras_status(False)
        obras_status.compute_obras_status(False)
        assert calls["n"] == 1  # 2ª chamada usa cache

    def test_cache_key_distinguishes_include_closed(self, monkeypatch):
        calls = {"n": 0}

        def fake_build(include_closed):
            calls["n"] += 1
            return {"clientes": [], "n_clientes": 0, "n_ovs": 0,
                    "n_open_ovs": 0, "generated_at": 1.0,
                    "include_closed": include_closed}

        monkeypatch.setattr(obras_status, "_build_status", fake_build)
        obras_status.compute_obras_status(False)
        obras_status.compute_obras_status(True)
        # chaves diferentes → ambas executam
        assert calls["n"] == 2

    def test_invalidate_forces_refresh(self, monkeypatch):
        calls = {"n": 0}

        def fake_build(include_closed):
            calls["n"] += 1
            return {"clientes": [], "n_clientes": 0, "n_ovs": 0,
                    "n_open_ovs": 0, "generated_at": 1.0,
                    "include_closed": include_closed}

        monkeypatch.setattr(obras_status, "_build_status", fake_build)
        obras_status.compute_obras_status(False)
        obras_status.invalidate_cache()
        obras_status.compute_obras_status(False)
        assert calls["n"] == 2


# ----- get_ov_detail -------------------------------------------------

class TestGetOvDetail:
    def test_returns_none_for_unknown_ov(self, monkeypatch):
        monkeypatch.setattr(
            obras_status, "get_watcher",
            lambda: type("W", (), {"get_refs": lambda self: {"of_to_entries": {}}})(),
        )
        monkeypatch.setattr(obras_status, "_kanbans_by_ov", lambda: {})
        assert obras_status.get_ov_detail("UNKNOWN") is None

    def test_returns_detail_for_known_ov(self, monkeypatch):
        refs = {
            "of_to_entries": {
                "100": [_e("100", "ACME", "OV1", "X", 10, fases={"bf": 4})],
            },
        }
        monkeypatch.setattr(
            obras_status, "get_watcher",
            lambda: type("W", (), {"get_refs": lambda self: refs})(),
        )
        monkeypatch.setattr(obras_status, "_kanbans_by_ov", lambda: {})
        monkeypatch.setattr(obras_status, "get_consumption", lambda: {})
        det = obras_status.get_ov_detail("OV1")
        assert det is not None
        assert det["ov"] == "OV1"
        assert det["planned"] == 10
        assert det["produced"] == 4

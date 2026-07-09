"""R113 — Tests do módulo of_consumption.

Fix do double counting (wizard 🪄): as fases do plano são a produção JÁ
registada no ERP até ao snapshot, e este sistema alimenta o CPIS — o
consumo de kanbans passou a contar SÓ folhas com sheet_iso_date >= data
do plano, e a ser repartido por waterfall entre entries irmãs.
"""
from __future__ import annotations

import pytest

from app.pipeline import of_consumption


@pytest.fixture(autouse=True)
def reset_cache(monkeypatch):
    # Testes unitários não dependem do plano carregado: cutoff neutro.
    monkeypatch.setattr(of_consumption, "_plan_cutoff_iso", lambda: None)
    of_consumption.invalidate_cache()
    yield
    of_consumption.invalidate_cache()


# ----- remaining() ----------------------------------------------------

class TestRemaining:
    def test_no_quanttrp_returns_inf(self):
        entry = {"designacao": "X", "_of": "100"}
        # consumption vazio para isolar o teste
        rem = of_consumption.remaining(entry, consumption={})
        assert rem == float("inf")

    def test_quanttrp_zero_returns_inf(self):
        entry = {"quanttrp": 0, "designacao": "X", "_of": "100"}
        assert of_consumption.remaining(entry, consumption={}) == float("inf")

    def test_fechado_returns_zero(self):
        entry = {"quanttrp": 10, "fases": {"corte": 5}, "fechado": "1",
                 "designacao": "X", "_of": "100"}
        assert of_consumption.remaining(entry, consumption={}) == 0.0

    def test_fechado_true_string(self):
        entry = {"quanttrp": 10, "fechado": "True", "designacao": "X", "_of": "1"}
        assert of_consumption.remaining(entry, consumption={}) == 0.0

    def test_basic_remaining(self):
        # R138 — "produzido" = fase a JUSANTE (última na ordem das fases),
        # não o max. quanttrp=10, downstream (soldadura)=2, kanban=2 → faltam 6.
        # NOTA (fix double counting): a aritmética fase+kanban mantém-se,
        # mas o QUE entra no consumption dict mudou — só kanbans com
        # sheet_iso_date >= data do snapshot do plano (as anteriores JÁ
        # estão nas fases do ERP; ver TestKanbanConsumptionCutoff).
        entry = {
            "quanttrp": 10,
            "fases": {"corte": 3, "soldadura": 2},
            "fechado": "0",
            "designacao": "OMEGA",
            "_of": "262107",
        }
        consumption = {("262107", "OMEGA"): 2.0}  # kanbans PÓS-plano
        assert of_consumption.remaining(entry, consumption) == 6.0

    def test_remaining_can_be_negative(self):
        """Quando consumption + fases excede quanttrp — possível em práticas
        reais. Devolvemos número negativo (já feito, sort vai para o fim)."""
        entry = {
            "quanttrp": 10,
            "fases": {"corte": 8},
            "fechado": "0",
            "designacao": "X",
            "_of": "100",
        }
        consumption = {("100", "X"): 5.0}
        # 10 - 8 - 5 = -3
        assert of_consumption.remaining(entry, consumption) == -3.0

    def test_uses_downstream_phase_not_max_nor_sum(self):
        """R138 — usa a fase mais a JUSANTE (última), não max nem sum.

        As fases iniciais sobre-produzem (margem de sucata), por isso o max
        marcava ~92% das linhas como fechadas. A medida correcta é o estágio
        a jusante (ou a fase do setor, quando dada)."""
        entry = {
            "quanttrp": 100,
            "fases": {"corte": 50, "soldadura": 30, "acabamento": 20},
            "fechado": "0",
            "designacao": "X",
            "_of": "100",
        }
        # downstream (acabamento)=20 → 100 - 20 - 0 = 80
        # (o antigo max daria 50; sum daria 0)
        assert of_consumption.remaining(entry, consumption={}) == 80.0
        # fase do setor explícita: corte=50 → 100 - 50 = 50
        assert of_consumption.remaining(entry, consumption={}, phase="corte") == 50.0

    def test_fases_with_none_values(self):
        entry = {
            "quanttrp": 10,
            "fases": {"corte": None, "soldadura": 4, "acabamento": None},
            "fechado": "0",
            "designacao": "X",
            "_of": "100",
        }
        # max ignora None → 4
        assert of_consumption.remaining(entry, consumption={}) == 6.0

    def test_string_quanttrp_with_comma(self):
        entry = {
            "quanttrp": "10,5",
            "fases": {},
            "designacao": "X",
            "_of": "100",
        }
        assert of_consumption.remaining(entry, consumption={}) == 10.5


# ----- sort_entries_by_remaining() -----------------------------------

class TestSortEntriesByRemaining:
    def _make(self, of, design, qt, phase=0, fechado="0"):
        return {
            "_of": of, "of": of, "designacao": design,
            "quanttrp": qt, "fases": {"corte": phase},
            "fechado": fechado,
        }

    def test_sort_asc_by_remaining(self, monkeypatch):
        monkeypatch.setattr(of_consumption, "get_consumption", lambda: {})
        entries = [
            self._make("100", "A", 10, phase=2),   # remaining = 8
            self._make("100", "B", 10, phase=8),   # remaining = 2 — primeiro
            self._make("100", "C", 10, phase=5),   # remaining = 5
        ]
        sorted_entries = of_consumption.sort_entries_by_remaining(entries)
        names = [e["designacao"] for e in sorted_entries]
        assert names == ["B", "C", "A"]
        assert sorted_entries[0]["_remaining"] == 2
        assert sorted_entries[0]["_done"] is False

    def test_filter_done_by_default(self, monkeypatch):
        monkeypatch.setattr(of_consumption, "get_consumption", lambda: {})
        entries = [
            self._make("100", "A", 10, phase=2),
            self._make("100", "B", 10, fechado="1"),  # done
            self._make("100", "C", 10, phase=10),     # done (phase=quanttrp)
        ]
        sorted_entries = of_consumption.sort_entries_by_remaining(entries)
        names = [e["designacao"] for e in sorted_entries]
        assert names == ["A"]

    def test_include_done_keeps_them(self, monkeypatch):
        monkeypatch.setattr(of_consumption, "get_consumption", lambda: {})
        entries = [
            self._make("100", "A", 10, phase=2),
            self._make("100", "B", 10, fechado="1"),
        ]
        sorted_entries = of_consumption.sort_entries_by_remaining(
            entries, include_done=True,
        )
        assert len(sorted_entries) == 2
        # B (remaining=0) vem primeiro porque < A (remaining=8)
        assert sorted_entries[0]["designacao"] == "B"
        assert sorted_entries[0]["_done"] is True

    def test_inf_goes_last(self, monkeypatch):
        monkeypatch.setattr(of_consumption, "get_consumption", lambda: {})
        entries = [
            {"_of": "100", "designacao": "NoTotal"},  # remaining=inf
            self._make("100", "A", 10, phase=2),        # remaining=8
        ]
        sorted_entries = of_consumption.sort_entries_by_remaining(entries)
        names = [e["designacao"] for e in sorted_entries]
        assert names == ["A", "NoTotal"]


# ----- Waterfall entre irmãs (fix do smear) ---------------------------

class TestAnnotateRemaining:
    def _sister(self, qt=10, fase=0):
        return {"_of": "100", "designacao": "PEÇA X", "quanttrp": qt,
                "fases": {"corte": fase}, "fechado": "0"}

    def test_pool_waterfall_first_sister_absorbs(self):
        # 2 irmãs de 10; pool 10 → a 1ª fecha, a 2ª fica inteira.
        entries = [self._sister(), self._sister()]
        rems = of_consumption.annotate_remaining(
            entries, consumption={("100", "PEÇA X"): 10.0})
        assert rems == [0.0, 10.0]

    def test_pool_smaller_than_first_need(self):
        entries = [self._sister(), self._sister()]
        rems = of_consumption.annotate_remaining(
            entries, consumption={("100", "PEÇA X"): 5.0})
        assert rems == [5.0, 10.0]

    def test_pool_overflow_goes_negative_on_last(self):
        # pool 25 sobre 10+10: sobra -5 fica na ÚLTIMA irmã (semântica
        # "remaining pode ser negativo" preservada; total conservado).
        entries = [self._sister(), self._sister()]
        rems = of_consumption.annotate_remaining(
            entries, consumption={("100", "PEÇA X"): 25.0})
        assert rems == [0.0, -5.0]
        assert sum(rems) == 20.0 - 25.0

    def test_old_behaviour_would_close_all_sisters(self):
        # O bug: subtrair o agregado por inteiro a CADA irmã fechava as
        # duas com pool 12 (faltando 8). Com waterfall só a 1ª fecha.
        entries = [self._sister(), self._sister()]
        rems = of_consumption.annotate_remaining(
            entries, consumption={("100", "PEÇA X"): 12.0})
        assert rems == [0.0, 8.0]

    def test_single_entry_equals_remaining(self):
        entry = self._sister(qt=10, fase=3)
        cons = {("100", "PEÇA X"): 4.0}
        assert of_consumption.annotate_remaining([entry], cons) == [
            of_consumption.remaining(entry, cons)
        ]

    def test_distinct_keys_do_not_interact(self):
        a = {"_of": "100", "designacao": "A", "quanttrp": 10, "fases": {},
             "fechado": "0"}
        b = {"_of": "100", "designacao": "B", "quanttrp": 10, "fases": {},
             "fechado": "0"}
        rems = of_consumption.annotate_remaining(
            [a, b], consumption={("100", "A"): 10.0})
        assert rems == [0.0, 10.0]

    def test_phase_and_fechado_respected(self):
        done = dict(self._sister(), fechado="1")
        withphase = dict(self._sister(), fases={"corte": 4, "exp": 0})
        rems = of_consumption.annotate_remaining(
            [done, withphase], consumption={}, phase="corte")
        assert rems[0] == 0.0
        assert rems[1] == 6.0


# ----- Fases negativas (estornos ERP) ---------------------------------

class TestNegativePhases:
    def test_negative_downstream_clamped(self):
        # exp=-5 (estorno ERP) não pode dar remaining = quanttrp+5.
        entry = {"quanttrp": 10, "fases": {"corte": 3, "exp": -5},
                 "fechado": "0", "designacao": "X", "_of": "100"}
        assert of_consumption.remaining(entry, consumption={}) == 10.0

    def test_negative_sector_phase_clamped(self):
        entry = {"quanttrp": 10, "fases": {"a": -1},
                 "fechado": "0", "designacao": "X", "_of": "100"}
        assert of_consumption.remaining(entry, consumption={}, phase="a") == 10.0

    def test_positive_phase_unaffected(self):
        entry = {"quanttrp": 10, "fases": {"a": 4},
                 "fechado": "0", "designacao": "X", "_of": "100"}
        assert of_consumption.remaining(entry, consumption={}, phase="a") == 6.0


# ----- Corte temporal do consumo (fix double counting) ----------------

class TestKanbanConsumptionCutoff:
    @pytest.fixture()
    def tmp_db(self, tmp_path, monkeypatch):
        from app.web import db
        monkeypatch.setattr(db, "_DB_PATH", tmp_path / "app.db")
        db.init_db()
        with db.conn() as c:
            for sid, iso in ((1, "2026-07-01"), (2, "2026-07-03"),
                             (3, "2026-07-05")):
                c.execute(
                    "INSERT INTO sheets (id, status, image_path, sheet_data) "
                    "VALUES (?, 'validated', ?, '{}')",
                    (sid, f"img{sid}.jpg"))
                c.execute(
                    "INSERT INTO production_rows "
                    "(sheet_id, row_index, sheet_iso_date, sheet_status, "
                    " of, modelo, qtd) VALUES (?, 0, ?, 'validated', "
                    " '262107', 'OMEGA', 2)", (sid, iso))
            # folha extracted nunca conta
            c.execute("INSERT INTO sheets (id, status, image_path, sheet_data) "
                      "VALUES (9, 'extracted', 'img9.jpg', '{}')")
            c.execute(
                "INSERT INTO production_rows (sheet_id, row_index, "
                "sheet_iso_date, sheet_status, of, modelo, qtd) "
                "VALUES (9, 0, '2026-07-05', 'extracted', '262107', "
                "'OMEGA', 99)")
        return db

    def test_no_cutoff_counts_all_validated(self, tmp_db):
        out = of_consumption._kanban_consumption(None)
        assert out[("262107", "OMEGA")] == 6.0  # 2+2+2; extracted fora

    def test_cutoff_inclusive(self, tmp_db):
        # >= 2026-07-03 → conta 03 e 05 (o dia do snapshot conta: export
        # matinal, a produção desse dia ainda não está no ERP).
        out = of_consumption._kanban_consumption("2026-07-03")
        assert out[("262107", "OMEGA")] == 4.0

    def test_cutoff_after_all_returns_empty(self, tmp_db):
        out = of_consumption._kanban_consumption("2026-07-06")
        assert out.get(("262107", "OMEGA")) is None


# ----- Cache TTL + cutoff ---------------------------------------------

class TestCache:
    def test_cache_returns_same_dict_within_ttl(self, monkeypatch):
        calls = {"n": 0}
        def fake_consumption(cutoff_iso=None):
            calls["n"] += 1
            return {("999", "X"): 1.0}
        monkeypatch.setattr(of_consumption, "_kanban_consumption", fake_consumption)

        c1 = of_consumption.get_consumption()
        c2 = of_consumption.get_consumption()
        assert c1 == c2
        assert calls["n"] == 1  # 2ª chamada usa cache

    def test_invalidate_forces_refresh(self, monkeypatch):
        calls = {"n": 0}
        def fake_consumption(cutoff_iso=None):
            calls["n"] += 1
            return {}
        monkeypatch.setattr(of_consumption, "_kanban_consumption", fake_consumption)

        of_consumption.get_consumption()
        of_consumption.invalidate_cache()
        of_consumption.get_consumption()
        assert calls["n"] == 2

    def test_cutoff_change_forces_refresh_within_ttl(self, monkeypatch):
        # Plano novo (cutoff diferente) refresca já, mesmo dentro do TTL.
        calls = {"n": 0}
        def fake_consumption(cutoff_iso=None):
            calls["n"] += 1
            return {("CUT", str(cutoff_iso)): 1.0}
        monkeypatch.setattr(of_consumption, "_kanban_consumption", fake_consumption)
        cut = {"v": "2026-07-01"}
        monkeypatch.setattr(of_consumption, "_plan_cutoff_iso", lambda: cut["v"])

        c1 = of_consumption.get_consumption()
        assert ("CUT", "2026-07-01") in c1
        cut["v"] = "2026-07-08"
        c2 = of_consumption.get_consumption()
        assert ("CUT", "2026-07-08") in c2
        assert calls["n"] == 2

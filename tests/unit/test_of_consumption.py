"""R113 — Tests do módulo of_consumption."""
from __future__ import annotations

import pytest

from app.pipeline import of_consumption


@pytest.fixture(autouse=True)
def reset_cache():
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
        # quanttrp=10, max phase=3, kanban=2 → falta 5
        entry = {
            "quanttrp": 10,
            "fases": {"corte": 3, "soldadura": 2},
            "fechado": "0",
            "designacao": "OMEGA",
            "_of": "262107",
        }
        consumption = {("262107", "OMEGA"): 2.0}
        assert of_consumption.remaining(entry, consumption) == 5.0

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

    def test_max_phase_not_sum(self):
        """Verificar que usamos max(fases), não sum(fases)."""
        entry = {
            "quanttrp": 100,
            "fases": {"corte": 50, "soldadura": 30, "acabamento": 20},
            "fechado": "0",
            "designacao": "X",
            "_of": "100",
        }
        # max=50 (não sum=100)
        # 100 - 50 - 0 = 50
        assert of_consumption.remaining(entry, consumption={}) == 50.0

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


# ----- Cache TTL -----------------------------------------------------

class TestCache:
    def test_cache_returns_same_dict_within_ttl(self, monkeypatch):
        calls = {"n": 0}
        def fake_consumption():
            calls["n"] += 1
            return {("999", "X"): 1.0}
        monkeypatch.setattr(of_consumption, "_kanban_consumption", fake_consumption)

        c1 = of_consumption.get_consumption()
        c2 = of_consumption.get_consumption()
        assert c1 == c2
        assert calls["n"] == 1  # 2ª chamada usa cache

    def test_invalidate_forces_refresh(self, monkeypatch):
        calls = {"n": 0}
        def fake_consumption():
            calls["n"] += 1
            return {}
        monkeypatch.setattr(of_consumption, "_kanban_consumption", fake_consumption)

        of_consumption.get_consumption()
        of_consumption.invalidate_cache()
        of_consumption.get_consumption()
        assert calls["n"] == 2

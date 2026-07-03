"""R244 — refit automático dos parâmetros do cross (auto-gate)."""
from __future__ import annotations

from app.learning import cross_refit


class TestFitCharMatrix:
    def test_fit_learns_common_confusion(self):
        pairs = [("262107", "262407", "human")] * 40 + [
            ("111111", "111111", "validated")
        ] * 200
        m = cross_refit.fit_char_matrix(pairs)
        # 1→4 repetido 40× tem de ficar mais barato que uma troca nunca vista.
        assert m["sub_costs_bits"]["1>4"] < m["cost_default_bits"]
        assert m["sub_costs_bits"]["1>4"] <= 6.0
        # troca rara (só prior) fica cara
        assert m["sub_costs_bits"].get("8>B", 10.0) > m["sub_costs_bits"]["1>4"]
        assert 0.5 < m["p_match"] <= 1.0

    def test_costs_clamped(self):
        m = cross_refit.fit_char_matrix([("ABCDEF", "ABCDEF", "x")] * 50)
        for v in m["sub_costs_bits"].values():
            assert 3.0 <= v <= 10.0

    def test_nw_align_handles_indel(self):
        al = cross_refit.nw_align("262107", "26217")  # apagamento de 1 char
        assert sum(1 for a, b in al if b == "-") == 1
        assert sum(1 for a, b in al if a != "-" and b != "-" and a == b) == 5


class TestRefitAutoGate:
    def test_floor_blocks_refit_on_empty_db(self, tmp_path, monkeypatch):
        # DB local vazio → 0 pares < piso 300 → params NÃO são tocados.
        monkeypatch.setattr(cross_refit, "PARAMS_PATH",
                            tmp_path / "cross_params.json")
        assert cross_refit.refit_from_db() is None
        assert not (tmp_path / "cross_params.json").exists()

    def test_clamped_m_drift(self):
        assert cross_refit._clamped_m(0.9, 0.5) == 0.65   # +0.15 máx
        assert cross_refit._clamped_m(0.2, 0.5) == 0.35   # -0.15 máx
        assert cross_refit._clamped_m(0.55, 0.5) == 0.55  # dentro da deriva
        assert cross_refit._clamped_m(0.7, None) == 0.7   # sem anterior

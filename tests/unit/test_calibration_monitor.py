"""R253/F3 — monitor de calibração: staleness, CUSUM de Page e Murphy."""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone

from app.learning import calibration_monitor as cm


class TestStaleness:
    def test_fresh_params_not_stale(self, tmp_path, monkeypatch):
        p = tmp_path / "cross_params.json"
        now = datetime.now(timezone.utc)
        p.write_text(json.dumps(
            {"fitted_at": (now - timedelta(days=3)).isoformat()}),
            encoding="utf-8")
        monkeypatch.setattr(cm, "_PARAMS_PATH", p)
        st = cm.check_staleness(now)
        assert st["stale"] is False and abs(st["age_days"] - 3) < 0.1

    def test_old_params_stale_and_escalate(self, tmp_path, monkeypatch):
        p = tmp_path / "cross_params.json"
        now = datetime.now(timezone.utc)
        p.write_text(json.dumps(
            {"fitted_at": (now - timedelta(days=75)).isoformat()}),
            encoding="utf-8")
        monkeypatch.setattr(cm, "_PARAMS_PATH", p)
        st = cm.check_staleness(now)
        assert st["stale"] is True and st["escalate"] is True

    def test_missing_file_is_stale(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cm, "_PARAMS_PATH", tmp_path / "nada.json")
        assert cm.check_staleness()["stale"] is True


class TestCusum:
    def _daily(self, days: int, p: float, n_per_day: int,
               seed: int) -> list[tuple[str, float, int]]:
        rng = random.Random(seed)
        out = []
        for d in range(days):
            day = f"2026-07-{d + 1:02d}"
            for _ in range(n_per_day):
                out.append((day, float("nan"),
                            int(rng.random() < p)))
        return out

    def test_quiet_under_target(self):
        # sobrevivência real = alvo (95%) → sem alarme
        r = cm.cusum(self._daily(28, 0.95, 3, seed=1), p0=0.95)
        assert r["alarm"] is False

    def test_alarms_on_degradation(self):
        # sobrevivência cai para 70% → alarme dentro do mês
        r = cm.cusum(self._daily(28, 0.70, 3, seed=2), p0=0.95)
        assert r["alarm"] is True
        assert r["alarm_day"] is not None


class TestMurphy:
    def test_none_below_min_n(self):
        assert cm.murphy_decomposition([(0.9, 1.0)] * 100) is None

    def test_decomposition_identity(self):
        rng = random.Random(3)
        pairs = []
        for _ in range(1000):
            p = rng.random()
            pairs.append((p, 1.0 if rng.random() < p else 0.0))
        m = cm.murphy_decomposition(pairs)
        assert m is not None
        # identidade de Murphy: brier ≈ rel − res + unc (binning exato)
        assert abs(m["brier"] - (m["reliability"] - m["resolution"]
                                 + m["uncertainty"])) < 1e-6
        # amostra bem calibrada → reliability pequena
        assert m["reliability"] < 0.01

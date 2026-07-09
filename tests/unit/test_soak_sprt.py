"""R253/F2 — SPRT de Wald do soak: fronteiras fechadas + comportamento em
sequências Bernoulli sintéticas (seed fixa — determinístico, não flaky)."""
from __future__ import annotations

import importlib.util
import math
import random
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "soak_sprt",
    Path(__file__).resolve().parents[2] / "scripts" / "diag" / "soak_sprt.py",
)
soak_sprt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(soak_sprt)


def test_wald_bounds_closed_form():
    a, b = soak_sprt.wald_bounds(alpha=0.05, beta=0.01)
    assert abs(a - math.log(0.99 / 0.05)) < 1e-12
    assert abs(b - math.log(0.01 / 0.95)) < 1e-12
    assert abs(a - 2.986) < 0.01
    assert abs(b - (-4.554)) < 0.01


def _simulate(p_true: float, p0: float, p1: float, seed: int,
              max_n: int = 20_000) -> tuple[str, int]:
    rng = random.Random(seed)
    a, b = soak_sprt.wald_bounds()
    s = 0.0
    for n in range(1, max_n + 1):
        x = int(rng.random() < p_true)
        s += soak_sprt.llr(x, p0, p1)
        if s >= a:
            return "ABORT", n
        if s <= b:
            return "ACCEPT", n
    return "a decorrer", max_n


def test_accepts_under_h0():
    """p verdadeiro = p0 (2%) → aceita, tipicamente em ~200-400 células."""
    decisions = [_simulate(0.02, 0.02, 0.06, seed) for seed in range(20)]
    accepts = [n for d, n in decisions if d == "ACCEPT"]
    assert len(accepts) >= 18, f"esperava >=18/20 ACCEPT sob H0: {decisions}"
    import statistics
    assert statistics.median(accepts) < 600


def test_aborts_under_h1_quickly():
    """p verdadeiro = 8% (pior que H1) → aborta depressa (<300 células)."""
    decisions = [_simulate(0.08, 0.02, 0.06, seed) for seed in range(20)]
    aborts = [n for d, n in decisions if d == "ABORT"]
    assert len(aborts) >= 18, f"esperava >=18/20 ABORT sob p=8%: {decisions}"
    import statistics
    assert statistics.median(aborts) < 300


def test_identity_arm_stricter():
    """O braço de identidade (0.5%/2%) aborta com p=4% de divergência de
    identidade — o braço geral (2%/6%) ainda estaria confortável."""
    d_id = [_simulate(0.04, 0.005, 0.02, seed) for seed in range(10)]
    assert sum(d == "ABORT" for d, _ in d_id) >= 9
    d_geral = [_simulate(0.04, 0.02, 0.06, seed) for seed in range(10)]
    # 4% fica entre p0 e p1 do braço geral — sem abort maioritário rápido.
    assert sum(d == "ABORT" for d, n in d_geral if n < 200) <= 5

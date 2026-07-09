"""R253/F0 — os fallbacks hardcoded do motor não podem divergir em silêncio
dos valores fitted em lexicons/cross_params.json (caso real: _pi_h0 ficou em
0.107 — medição R243 — enquanto o quant7 re-medido no d1fa593 dava 0.059).

O fallback existe para o cenário "ficheiro ausente/corrupto"; se estiver a
mais de 20% do valor tracked, está a mentir sobre o comportamento normal.
"""
from __future__ import annotations

import json
from pathlib import Path

import app.pipeline.scoring_engine as se

_PARAMS = json.loads(
    (Path(__file__).resolve().parents[2] / "lexicons" / "cross_params.json")
    .read_text(encoding="utf-8")
)


def _pi_h0_fallback(monkeypatch, age_days):
    """_pi_h0 com cross_params vazio → só fallbacks."""
    monkeypatch.setattr(se, "_load_cross_params", lambda: {})
    return se._pi_h0(age_days)


def test_pi_h0_fallback_matches_tracked_fresh_bucket(monkeypatch):
    tracked = float(
        _PARAMS["quant7_ood_by_age"]["buckets"]["0-3"]["p_ood"])
    fallback = _pi_h0_fallback(monkeypatch, None)
    # clamp inferior é 0.05 — comparar contra o tracked clampado
    tracked_eff = min(max(tracked, 0.05), 0.50)
    assert abs(fallback - tracked_eff) / tracked_eff < 0.20, (
        f"fallback _pi_h0 fresco ({fallback}) divergiu >20% do fitted "
        f"({tracked_eff}) — sincronizar o literal no código"
    )


def test_pi_h0_fallback_age_zero_equals_none(monkeypatch):
    assert _pi_h0_fallback(monkeypatch, 0.0) == _pi_h0_fallback(
        monkeypatch, None)


def test_id_m_fallback_matches_tracked_quant8():
    mj = _PARAMS["quant8_identity_joint"]["m_joint"]
    for fields, fb in se._FS_ID_M_FALLBACK.items():
        tracked = float(mj["+".join(fields)]["m"])
        assert abs(fb - tracked) / tracked < 0.20, (
            f"_FS_ID_M_FALLBACK[{fields}]={fb} divergiu >20% do fitted "
            f"{tracked}"
        )


def test_id_u_floor_fallback_matches_tracked_quant8():
    uf = _PARAMS["quant8_identity_joint"]["u_floor"]
    for field, fb in se._FS_ID_U_FLOOR_FALLBACK.items():
        tracked = float(uf[field])
        assert abs(fb - tracked) / tracked < 0.20, (
            f"_FS_ID_U_FLOOR_FALLBACK[{field}]={fb} divergiu >20% do "
            f"fitted {tracked}"
        )

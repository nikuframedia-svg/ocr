"""R253/F2 — rota de triagem do soak (/sheet/<id>/shadow-view +
/shadow-queue + carimbo de triagem). O passo 2 do procedimento de flip
depende disto (docs/CROSS_EVALUATION_PROTOCOL.md)."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.web import db, main

_DESKTOP = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


def _cell(value: str, status: str = "snapped", conf: float | None = None,
          reason: str = "") -> dict:
    out: dict = {"value": value, "status": status}
    if conf is not None:
        out["decision_confidence"] = conf
    if reason:
        out["decision_reason"] = reason
    return out


_PROD = {"rows": [{"fields": {
    "of": _cell("262593"), "ov": _cell("2601149"),
    "modelo": _cell("5100T742"), "esp": _cell("12.0"),
}}]}
# diverge em OF (identidade) e modelo; esp igual; ov igual.
_SHADOW = {"rows": [{
    "fields": {
        "of": _cell("262594", conf=0.41, reason="posterior_marginal"),
        "ov": _cell("2601149"),
        "modelo": _cell("5100T743", conf=0.33,
                        reason="ambiguous_sibling_designacao"),
        "esp": _cell("12.0"),
    },
    "winner_p_of": 0.41, "winner_p_h0": 0.22,
    "winner_posterior_entropy_bits": 3.1,
    "winner_p_field": {"of": 0.41, "modelo": 0.33},
}]}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    test_db = tmp_path / "test_app.db"
    monkeypatch.setattr(db, "_DB_PATH", test_db)
    db.init_db()
    # A produção vive no storage por ficheiros — para o teste, devolve o
    # payload fixo para qualquer sheet_id.
    from app.cross_check import storage
    monkeypatch.setattr(
        storage, "load_sheet_cross_check",
        lambda sheet_id, include_stale=False: _PROD)
    return TestClient(main.app)


def _insert_sheet_with_shadow(*, shadow: dict | None = _SHADOW) -> int:
    sheet_id = db.insert_sheet("test.jpg")
    with db.conn() as c:
        c.execute(
            "UPDATE sheets SET status='extracted', "
            "sheet_data=?, raw_extraction=? WHERE id=?",
            (json.dumps({"rows": []}), json.dumps({"rows": []}), sheet_id),
        )
        if shadow is not None:
            c.execute(
                "UPDATE sheets SET shadow_scoring_json=?, "
                "shadow_scored_at=CURRENT_TIMESTAMP WHERE id=?",
                (json.dumps(shadow), sheet_id),
            )
    return sheet_id


def test_shadow_view_shows_only_diffs(client):
    sid = _insert_sheet_with_shadow()
    r = client.get(f"/sheet/{sid}/shadow-view", headers=_DESKTOP)
    assert r.status_code == 200
    # os campos divergentes aparecem, com os dois valores
    assert "262593" in r.text and "262594" in r.text
    assert "5100T742" in r.text and "5100T743" in r.text
    # um campo NÃO divergente não aparece na tabela de diffs
    assert "12.0" not in r.text
    # telemetria do posterior presente
    assert "0.41" in r.text


def test_shadow_view_404_without_shadow(client):
    sid = _insert_sheet_with_shadow(shadow=None)
    r = client.get(f"/sheet/{sid}/shadow-view", headers=_DESKTOP)
    assert r.status_code == 404


def test_triage_stamps_and_queue_empties(client):
    sid = _insert_sheet_with_shadow()
    q = client.get("/shadow-queue", headers=_DESKTOP)
    assert q.status_code == 200 and f"/sheet/{sid}/shadow-view" in q.text
    r = client.post(f"/sheet/{sid}/shadow-triage",
                    data={"note": "irmão ambíguo — vermelho, esperado"},
                    headers=_DESKTOP, follow_redirects=False)
    assert r.status_code == 303
    sheet = db.get_sheet(sid)
    assert sheet["shadow_triaged_at"] is not None
    assert "irmão ambíguo" in (sheet["shadow_triage_note"] or "")
    q2 = client.get("/shadow-queue", headers=_DESKTOP)
    assert f"/sheet/{sid}/shadow-view" not in q2.text

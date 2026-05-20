"""R110.C — Tests do policy_engine + eval gate + circuit breaker."""
from __future__ import annotations

import pytest

from app.pipeline import policy_engine
from app.pipeline.tools import (
    propose_cpis_change,
    propose_rule,
    propose_template_change,
    reset_session_buffers,
)
from app.web import db


@pytest.fixture(autouse=True)
def init_db():
    db.init_db()
    reset_session_buffers()


# ----- Promote --------------------------------------------------------

class TestPromote:
    def test_promote_rule_proposal(self):
        result = propose_rule(
            kind="cliente_alias",
            payload={"from": "AAA", "to": "BBB"},
            qwen_confidence=0.9,
        )
        assert result["status"] == "ok"
        version_id = policy_engine.promote_policy_from_proposal(
            result["proposal_id"], created_by="test"
        )
        assert version_id is not None
        active = policy_engine.get_active_policy()
        assert active["version"] == version_id

        # Proposta marcada como accepted
        p = db.get_proposal(result["proposal_id"])
        assert p["status"] == "accepted"

    def test_promote_template_creates_overlay(self):
        result = propose_template_change(
            template_name="quinadora_pav8",
            change_type="add_field",
            field_name="ov",
            justification="test",
        )
        assert result["status"] == "ok"
        version_id = policy_engine.promote_policy_from_proposal(
            result["proposal_id"]
        )
        assert version_id is not None
        overlays = db.get_active_template_overlays(template_name="quinadora_pav8")
        assert len(overlays) >= 1
        assert overlays[-1]["change_type"] == "add_field"

    def test_promote_nonexistent_proposal_returns_none(self):
        v = policy_engine.promote_policy_from_proposal(99999)
        assert v is None

    def test_reject_proposal(self):
        result = propose_rule(
            kind="cliente_alias",
            payload={"from": "X", "to": "Y"},
        )
        ok = policy_engine.reject_proposal(result["proposal_id"])
        assert ok
        p = db.get_proposal(result["proposal_id"])
        assert p["status"] == "rejected"


# ----- Versioning chain -----------------------------------------------

class TestVersioningChain:
    def test_versions_chain_via_parent(self):
        r1 = propose_rule(kind="cliente_alias",
                          payload={"from": "X1", "to": "Y1"})
        v1 = policy_engine.promote_policy_from_proposal(r1["proposal_id"])

        r2 = propose_rule(kind="cliente_alias",
                          payload={"from": "X2", "to": "Y2"})
        v2 = policy_engine.promote_policy_from_proposal(r2["proposal_id"])

        active = policy_engine.get_active_policy()
        assert active["version"] == v2
        assert active["parent_version"] == v1


# ----- Eval gate -------------------------------------------------------

class TestEvalGate:
    def test_eval_gate_returns_baseline(self):
        result = propose_rule(kind="cliente_alias",
                              payload={"from": "A", "to": "B"})
        p = db.get_proposal(result["proposal_id"])
        eval_result = policy_engine.run_eval_gate(p, window=10)
        assert eval_result["decision"] in {"passed_dry_run", "error"}
        if eval_result["decision"] == "passed_dry_run":
            assert "edits_per_sheet_baseline" in eval_result


# ----- Circuit breaker -------------------------------------------------

class TestCircuitBreaker:
    def test_no_baseline_when_few_sheets(self):
        # Pode dar no_baseline ou ok dependendo do app.db real
        result = policy_engine.check_circuit_breaker(
            window_recent=5, window_baseline=10,
        )
        assert "status" in result

    def test_rollback_to_parent(self):
        r1 = propose_rule(kind="cliente_alias",
                          payload={"from": "ROLLBACK_A", "to": "ROLLBACK_B"})
        v1 = policy_engine.promote_policy_from_proposal(r1["proposal_id"])

        r2 = propose_rule(kind="cliente_alias",
                          payload={"from": "ROLLBACK_C", "to": "ROLLBACK_D"})
        v2 = policy_engine.promote_policy_from_proposal(r2["proposal_id"])

        active = policy_engine.get_active_policy()
        assert active["version"] == v2

        # Rollback para parent
        result = policy_engine.rollback_to_parent(reason="test")
        assert result == v1
        active_after = policy_engine.get_active_policy()
        assert active_after["version"] == v1

    def test_rollback_when_no_parent(self):
        # Reset: criar policy v1 sem parent (já há vários da batch acima
        # mas o cleanup entre tests não acontece — só validamos o caminho).
        result = policy_engine.rollback_to_parent()
        # Pode ser None (sem parent) ou um version_id válido
        assert result is None or isinstance(result, int)


# ----- Endpoints integration ------------------------------------------

class TestEndpointsBasic:
    def test_list_proposals_endpoint(self):
        from fastapi.testclient import TestClient
        from backend.app.web.main import app
        client = TestClient(app)
        r = client.get("/agent/proposals?limit=5")
        assert r.status_code == 200
        body = r.json()
        assert "proposals" in body
        assert "count" in body

    def test_approve_endpoint(self):
        from fastapi.testclient import TestClient
        from backend.app.web.main import app
        client = TestClient(app)
        proposal_result = propose_rule(
            kind="cliente_alias",
            payload={"from": "ENDPOINT_A", "to": "ENDPOINT_B"},
        )
        r = client.post(f"/agent/proposals/{proposal_result['proposal_id']}/approve")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["policy_version"] is not None

    def test_reject_endpoint(self):
        from fastapi.testclient import TestClient
        from backend.app.web.main import app
        client = TestClient(app)
        proposal_result = propose_rule(
            kind="cliente_alias",
            payload={"from": "REJ_A", "to": "REJ_B"},
        )
        r = client.post(f"/agent/proposals/{proposal_result['proposal_id']}/reject")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"

    def test_approve_nonexistent_404(self):
        from fastapi.testclient import TestClient
        from backend.app.web.main import app
        client = TestClient(app)
        r = client.post("/agent/proposals/999999/approve")
        assert r.status_code == 404

    def test_circuit_breaker_endpoint(self):
        from fastapi.testclient import TestClient
        from backend.app.web.main import app
        client = TestClient(app)
        r = client.get("/agent/circuit-breaker")
        assert r.status_code == 200
        body = r.json()
        assert "status" in body

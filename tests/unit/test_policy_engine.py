"""R110.C — Tests do policy_engine + eval gate + circuit breaker."""
from __future__ import annotations

import json
import uuid

import pytest

from app.pipeline import policy_engine
from app.pipeline.tools import (
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
        fr = f"AAA_{uuid.uuid4().hex}"
        to = f"BBB_{uuid.uuid4().hex}"
        result = propose_rule(
            kind="cliente_alias",
            payload={"from": fr, "to": to},
            qwen_confidence=0.9,
        )
        assert result["status"] == "ok"
        version_id = policy_engine.promote_policy_from_proposal(
            result["proposal_id"], created_by="test"
        )
        assert version_id is not None
        active = policy_engine.get_active_policy()
        assert active["version"] == version_id
        policy = json.loads(active["yaml_blob"])
        assert policy["rules"][-1]["kind"] == "cliente_alias"
        assert policy["rules"][-1]["payload"]["from"] == fr
        assert policy["rules"][-1]["payload"]["to"] == to

        # Proposta marcada como accepted
        p = db.get_proposal(result["proposal_id"])
        assert p["status"] == "accepted"

    def test_promote_template_creates_overlay(self):
        field_name = f"zz_test_{uuid.uuid4().hex}"
        result = propose_template_change(
            template_name="quinadora_pav8",
            change_type="add_field",
            field_name=field_name,
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
        assert overlays[-1]["payload"]["field_name"] == field_name

    def test_promote_rejects_when_eval_gate_fails(self, monkeypatch):
        base_version = db.save_policy_version(
            parent_version=None,
            yaml_blob=json.dumps({}),
            diff_summary="baseline",
            created_by="test",
            eval_results={},
        )
        db.activate_policy_version(base_version)
        result = propose_rule(
            kind="cliente_alias",
            payload={
                "from": f"BAD_{uuid.uuid4().hex}",
                "to": f"WORSE_{uuid.uuid4().hex}",
            },
            qwen_confidence=0.9,
        )
        monkeypatch.setattr(
            policy_engine,
            "run_eval_gate",
            lambda proposal: {
                "decision": "failed",
                "edits_per_sheet_baseline": 2.0,
                "edits_per_sheet_with_proposal": 5.0,
                "note": "regression",
            },
        )

        version_id = policy_engine.promote_policy_from_proposal(
            result["proposal_id"], created_by="test"
        )

        assert version_id == base_version
        active = policy_engine.get_active_policy()
        assert active["version"] == base_version
        assert json.loads(active["yaml_blob"]) == {}
        proposal = db.get_proposal(result["proposal_id"])
        assert proposal["status"] == "rejected"
        assert proposal["shadow_eval_results"]["decision"] == "failed"

    def test_promote_nonexistent_proposal_returns_none(self):
        v = policy_engine.promote_policy_from_proposal(99999)
        assert v is None

    def test_duplicate_template_change_is_noop_in_policy(self):
        policy = {
            "template_overlays": {
                "quinadora_pav8": [
                    {
                        "change_type": "add_field",
                        "field_name": "ov",
                        "field_position": "end",
                        "from_proposal": 1,
                    }
                ]
            }
        }
        proposal = {
            "id": 2,
            "kind": "template",
            "payload": {
                "template_name": "quinadora_pav8",
                "change_type": "add_field",
                "field_name": "ov",
                "field_position": "end",
            },
        }

        new_policy = policy_engine._apply_proposal_to_policy(policy, proposal)

        assert new_policy == policy

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
        suffix = uuid.uuid4().hex
        r1 = propose_rule(kind="cliente_alias",
                          payload={"from": f"X1_{suffix}",
                                   "to": f"Y1_{suffix}"})
        v1 = policy_engine.promote_policy_from_proposal(r1["proposal_id"])

        r2 = propose_rule(kind="cliente_alias",
                          payload={"from": f"X2_{suffix}",
                                   "to": f"Y2_{suffix}"})
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

    def test_cliente_alias_simulation_does_not_create_fake_plan_ref(self):
        """Alias simulation cannot invent a cliente plan pool with no entries."""
        from app.pipeline.scoring_engine import shadow_score

        refs = {
            "available": True,
            "loaded_at": "base",
            "of_to_entries": {},
            "clientes_plan": frozenset(),
            "lotes_sap_full": {},
        }
        proposal = {
            "id": 11,
            "kind": "rule",
            "payload": {"kind": "cliente_alias", "from": "AAA", "to": "ACME"},
        }

        simulated = policy_engine._apply_proposal_to_refs(proposal, refs)

        assert simulated is not None
        assert simulated.get("cliente_aliases") == {"AAA": "ACME"}
        assert "ACME" not in set(simulated.get("clientes_plan") or frozenset())

        sheet_data = {
            "template_name": "bobine_formato",
            "header": {},
            "footer": {},
            "rows": [{"cliente": "ACME"}],
        }
        scoring, *_ = shadow_score(sheet_data, None, simulated)

        assert scoring["rows"][0]["fields"]["cliente"]["status"] == "very_different"

    def test_stored_rule_shape_is_simulable(self):
        refs = {
            "available": True,
            "loaded_at": "base",
            "of_to_entries": {},
            "clientes_plan": frozenset(),
            "lotes_sap_full": {},
        }
        proposal = {
            "id": 15,
            "kind": "cliente_alias",
            "payload": {"from": "AAA", "to": "ACME"},
        }

        simulated_refs = policy_engine._apply_proposal_to_refs(proposal, refs)
        simulated_sheet, changed = policy_engine._apply_proposal_to_sheet_data(
            proposal,
            {"rows": [{"cliente": "AAA"}]},
        )

        assert simulated_refs is not None
        assert simulated_refs["cliente_aliases"] == {"AAA": "ACME"}
        assert changed is True
        assert simulated_sheet["rows"][0]["cliente"] == "ACME"

    def test_cliente_alias_sheet_simulation_rewrites_matching_rows(self):
        sheet_data = {
            "template_name": "bobine_formato",
            "header": {},
            "footer": {},
            "rows": [
                {"cliente": "aaa"},
                {"cliente": "OTHER"},
            ],
        }
        proposal = {
            "id": 12,
            "kind": "rule",
            "payload": {"kind": "cliente_alias", "from": "AAA", "to": "ACME"},
        }

        simulated, changed = policy_engine._apply_proposal_to_sheet_data(
            proposal,
            sheet_data,
        )

        assert changed is True
        assert simulated["rows"][0]["cliente"] == "ACME"
        assert simulated["rows"][1]["cliente"] == "OTHER"
        assert sheet_data["rows"][0]["cliente"] == "aaa"

    def test_modelo_alias_sheet_simulation_rewrites_matching_rows(self):
        sheet_data = {
            "template_name": "bobine_formato",
            "header": {},
            "footer": {},
            "rows": [
                {"modelo": "CGC2E1OD"},
                {"modelo": "OTHER"},
            ],
        }
        proposal = {
            "id": 13,
            "kind": "rule",
            "payload": {
                "kind": "modelo_alias",
                "from": "CGC2E1OD",
                "to": "CGC2E10D",
            },
        }

        simulated, changed = policy_engine._apply_proposal_to_sheet_data(
            proposal,
            sheet_data,
        )

        assert changed is True
        assert simulated["rows"][0]["modelo"] == "CGC2E10D"
        assert simulated["rows"][1]["modelo"] == "OTHER"
        assert sheet_data["rows"][0]["modelo"] == "CGC2E1OD"


# ----- Eval gate real (R117) ------------------------------------------

class TestEvalGateShadow:
    """R117 — shadow A/B do eval gate.

    Cada teste mocka:
      * `_load_validated_sheets_for_eval`  → controla N + payloads.
      * `get_watcher`/`shadow_score`        → controla métricas A/B sem
        precisar de Excel refs no disco.
    """

    @staticmethod
    def _fake_sheets(n: int) -> list[dict]:
        # Estrutura mínima aceite por shadow_score real, mas como o motor
        # está mockado, basta um dict sentinel.
        return [
            {
                "id": i,
                "sheet_data": {"rows": [], "header": {}, "footer": {}},
                "dq_audit": {},
            }
            for i in range(n)
        ]

    def test_passed_when_proposal_does_not_worsen(self, monkeypatch):
        """R117 — proposta cliente_alias com with_proposal ≤ baseline → passed."""
        sheets = self._fake_sheets(10)
        monkeypatch.setattr(
            policy_engine, "_load_validated_sheets_for_eval",
            lambda window: sheets,
        )

        class _FakeWatcher:
            def get_refs(self):
                return {"loaded_at": "2026-05-21T00:00:00", "clientes_plan": frozenset()}

        # baseline: 4 atenções; with_proposal: 3 atenções (melhora)
        def _fake_shadow_score(_sd, _da, refs):
            # Alterna baseline/proposal pela presença do sufixo "+prop" em loaded_at
            attn = 3 if "+prop" in str(refs.get("loaded_at", "")) else 4
            scoring = {"summary": {"snapped": attn, "very_different": 0, "confirmed": 0, "na": 0, "total": attn}}
            return scoring, attn, attn, 0, 0, 1

        import app.pipeline.scoring_engine as _se
        import app.cross_check.ref_watcher as _rw
        monkeypatch.setattr(_se, "shadow_score", _fake_shadow_score)
        monkeypatch.setattr(_rw, "get_watcher", lambda: _FakeWatcher())

        proposal = {
            "id": 1, "kind": "rule",
            "payload": {"kind": "modelo_alias", "from": "AAA", "to": "BBB"},
        }
        out = policy_engine.run_eval_gate(proposal, window=10)
        assert out["decision"] == "passed", out
        assert out["n_sheets_evaluated"] == 10
        assert out["edits_per_sheet_baseline"] == 4.0
        assert out["edits_per_sheet_with_proposal"] == 3.0

    def test_failed_when_proposal_worsens_above_threshold(self, monkeypatch):
        """R117 — proposta piora cells_need_attention > 5% → failed."""
        sheets = self._fake_sheets(10)
        monkeypatch.setattr(
            policy_engine, "_load_validated_sheets_for_eval",
            lambda window: sheets,
        )

        class _FakeWatcher:
            def get_refs(self):
                return {"loaded_at": "x", "clientes_plan": frozenset()}

        def _fake_shadow_score(_sd, _da, refs):
            # baseline=2.0, with_proposal=5.0 → piora 150% → failed
            attn = 5 if "+prop" in str(refs.get("loaded_at", "")) else 2
            scoring = {"summary": {"snapped": attn, "very_different": 0, "confirmed": 0, "na": 0, "total": attn}}
            return scoring, attn, attn, 0, 0, 1

        import app.pipeline.scoring_engine as _se
        import app.cross_check.ref_watcher as _rw
        monkeypatch.setattr(_se, "shadow_score", _fake_shadow_score)
        monkeypatch.setattr(_rw, "get_watcher", lambda: _FakeWatcher())

        proposal = {
            "id": 2, "kind": "rule",
            "payload": {"kind": "modelo_alias", "from": "C", "to": "D"},
        }
        out = policy_engine.run_eval_gate(proposal, window=10)
        assert out["decision"] == "failed", out
        assert out["edits_per_sheet_baseline"] == 2.0
        assert out["edits_per_sheet_with_proposal"] == 5.0

    def test_passed_dry_run_when_not_simulable(self, monkeypatch):
        """R117 — confusion_pair não é simulável → passed_dry_run com nota."""
        sheets = self._fake_sheets(8)
        monkeypatch.setattr(
            policy_engine, "_load_validated_sheets_for_eval",
            lambda window: sheets,
        )

        class _FakeWatcher:
            def get_refs(self):
                return {"loaded_at": "y", "clientes_plan": frozenset()}

        def _fake_shadow_score(_sd, _da, _refs):
            scoring = {"summary": {"snapped": 2, "very_different": 1, "confirmed": 0, "na": 0, "total": 3}}
            return scoring, 3, 3, 0, 0, 1

        import app.pipeline.scoring_engine as _se
        import app.cross_check.ref_watcher as _rw
        monkeypatch.setattr(_se, "shadow_score", _fake_shadow_score)
        monkeypatch.setattr(_rw, "get_watcher", lambda: _FakeWatcher())

        proposal = {
            "id": 3, "kind": "rule",
            "payload": {"kind": "confusion_pair", "gold_char": "O", "ocr_char": "0"},
        }
        out = policy_engine.run_eval_gate(proposal, window=10)
        assert out["decision"] == "passed_dry_run", out
        assert out["edits_per_sheet_with_proposal"] is None
        assert "not simulable" in out["note"]

    def test_passed_dry_run_when_insufficient_sheets(self, monkeypatch):
        """R117 — < 5 sheets validadas → passed_dry_run com nota explícita."""
        monkeypatch.setattr(
            policy_engine, "_load_validated_sheets_for_eval",
            lambda window: self._fake_sheets(2),
        )
        proposal = {
            "id": 4, "kind": "rule",
            "payload": {"kind": "cliente_alias", "from": "E", "to": "F"},
        }
        out = policy_engine.run_eval_gate(proposal, window=10)
        assert out["decision"] == "passed_dry_run"
        assert "insufficient validated sheets" in out["note"]
        assert out["n_sheets_evaluated"] == 2

    def test_cliente_alias_eval_gate_rejects_cross_alias_improvement(self, monkeypatch):
        """Cliente aliases no longer count as cross evidence."""
        sheet = {
            "template_name": "bobine_formato",
            "header": {},
            "footer": {},
            "rows": [{"cliente": "AAA"}],
        }
        sheets = [
            {"id": i, "sheet_data": sheet, "dq_audit": {}}
            for i in range(5)
        ]
        monkeypatch.setattr(
            policy_engine, "_load_validated_sheets_for_eval",
            lambda window: sheets,
        )

        refs = {
            "available": True,
            "loaded_at": "base",
            "of_to_entries": {
                "262300": [
                    {
                        "ov": "2410300",
                        "cliente": "ACME",
                        "designacao": "MOD-A",
                    }
                ]
            },
            "clientes_plan": frozenset({"ACME"}),
            "lotes_sap_full": {},
        }

        class _FakeWatcher:
            def get_refs(self):
                return refs

        import app.cross_check.ref_watcher as _rw
        monkeypatch.setattr(_rw, "get_watcher", lambda: _FakeWatcher())

        proposal = {
            "id": 14,
            "kind": "cliente_alias",
            "payload": {"from": "AAA", "to": "ACME"},
        }
        out = policy_engine.run_eval_gate(proposal, window=10)

        assert out["decision"] == "failed", out
        assert out["edits_per_sheet_baseline"] >= 1.0
        assert out["edits_per_sheet_with_proposal"] == out["edits_per_sheet_baseline"]
        assert "cliente_alias ignored by row cross" in out["note"]


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
        from app.web.main import app
        client = TestClient(app)
        r = client.get("/agent/proposals?limit=5")
        assert r.status_code == 200
        body = r.json()
        assert "proposals" in body
        assert "count" in body

    def test_approve_endpoint(self):
        from fastapi.testclient import TestClient
        from app.web.main import app
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
        from app.web.main import app
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
        from app.web.main import app
        client = TestClient(app)
        r = client.post("/agent/proposals/999999/approve")
        assert r.status_code == 404

    def test_circuit_breaker_endpoint(self):
        from fastapi.testclient import TestClient
        from app.web.main import app
        client = TestClient(app)
        r = client.get("/agent/circuit-breaker")
        assert r.status_code == 200
        body = r.json()
        assert "status" in body

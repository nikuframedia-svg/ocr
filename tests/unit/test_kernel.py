"""R110.E — Tests do kernel event log + state + replay."""
from __future__ import annotations


import pytest

from app import kernel


@pytest.fixture(autouse=True)
def reset_kernel():
    kernel.reset_state("test setup")
    yield
    kernel.reset_state("test teardown")


# ----- State básico ---------------------------------------------------

class TestStateBasic:
    def test_empty_initial_state(self):
        state = kernel.get_state()
        assert state["version"] == 0
        assert state["counts"]["sheets_uploaded"] == 0

    def test_emit_increments_version(self):
        ev = kernel.emit_event("sheet_uploaded", {"sheet_id": 1})
        assert ev["version"] == 1
        state = kernel.get_state()
        assert state["version"] == 1
        assert state["counts"]["sheets_uploaded"] == 1

    def test_unknown_event_type_logs_warning(self):
        # Não rejeita — só avisa
        ev = kernel.emit_event("custom_made_up", {})
        assert ev["type"] == "custom_made_up"
        # Counts não mudam para tipos desconhecidos
        state = kernel.get_state()
        assert state["counts"]["sheets_uploaded"] == 0


# ----- Counters por tipo ----------------------------------------------

class TestCounters:
    def test_sheet_validated(self):
        kernel.emit_event("sheet_validated", {"sheet_id": 1})
        assert kernel.get_state()["counts"]["sheets_validated"] == 1

    def test_proposal_accepted(self):
        kernel.emit_event("proposal_decided",
                          {"proposal_id": 1, "decision": "accepted"})
        assert kernel.get_state()["counts"]["proposals_accepted"] == 1

    def test_proposal_rejected(self):
        kernel.emit_event("proposal_decided",
                          {"proposal_id": 1, "decision": "rejected"})
        assert kernel.get_state()["counts"]["proposals_rejected"] == 1

    def test_policy_promoted_sets_active(self):
        kernel.emit_event("policy_promoted", {"version": 42})
        state = kernel.get_state()
        assert state["active_policy_version"] == 42
        assert state["counts"]["policies_promoted"] == 1

    def test_rollback_updates_active(self):
        kernel.emit_event("policy_promoted", {"version": 5})
        kernel.emit_event("policy_rolled_back", {"version": 4})
        state = kernel.get_state()
        assert state["active_policy_version"] == 4
        assert state["counts"]["rollbacks"] == 1


# ----- Replay determinístico ------------------------------------------

class TestReplay:
    def test_replay_matches_live_state(self):
        kernel.emit_event("sheet_uploaded", {"sheet_id": 1})
        kernel.emit_event("sheet_validated", {"sheet_id": 1})
        kernel.emit_event("proposal_created", {"proposal_id": 5})
        kernel.emit_event("proposal_decided",
                          {"proposal_id": 5, "decision": "accepted"})
        kernel.emit_event("policy_promoted", {"version": 1})

        live = kernel.get_state()
        replayed = kernel.replay()
        assert live == replayed

    def test_replay_from_offset(self):
        kernel.emit_event("sheet_uploaded", {"sheet_id": 1})
        kernel.emit_event("sheet_uploaded", {"sheet_id": 2})
        kernel.emit_event("sheet_uploaded", {"sheet_id": 3})

        # Skipping first 2 events leaves only 1 in the replay
        partial = kernel.replay(from_event=2)
        assert partial["counts"]["sheets_uploaded"] == 1


# ----- Event log ------------------------------------------------------

class TestEventLog:
    def test_list_recent(self):
        for i in range(5):
            kernel.emit_event("sheet_uploaded", {"sheet_id": i})
        recent = kernel.list_recent_events(limit=3)
        assert len(recent) == 3
        assert recent[-1]["payload"]["sheet_id"] == 4

    def test_count_events(self):
        for _ in range(7):
            kernel.emit_event("sheet_uploaded")
        assert kernel.count_events() == 7


# ----- Endpoint integration -------------------------------------------

class TestKernelEndpoints:
    def test_state_endpoint(self):
        from fastapi.testclient import TestClient
        from app.web.main import app
        client = TestClient(app)
        kernel.emit_event("sheet_uploaded", {"sheet_id": 99})
        r = client.get("/agent/kernel/state")
        assert r.status_code == 200
        body = r.json()
        assert body["state"]["counts"]["sheets_uploaded"] >= 1
        assert body["total_events"] >= 1

    def test_events_endpoint(self):
        from fastapi.testclient import TestClient
        from app.web.main import app
        client = TestClient(app)
        kernel.emit_event("sheet_uploaded", {"sheet_id": 100})
        r = client.get("/agent/kernel/events?limit=5")
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body["events"], list)
        assert len(body["events"]) >= 1

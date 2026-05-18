"""CRUD for the ``learnings`` / ``learning_runs`` tables.

This is the single API the web layer uses to read, approve, reject,
edit and delete learnings — no raw SQL in routes.
"""
from __future__ import annotations

import json
import sqlite3

from app.learning.models import Proposal
from app.web import db


def _row_to_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    for f, empty in (("payload", {}), ("evidence_refs", [])):
        raw = d.get(f)
        if raw:
            try:
                d[f] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                d[f] = empty
        else:
            d[f] = empty
    return d


# ---- proposals (the ``learnings`` table) -----------------------------------

def upsert_proposal(proposal: Proposal, risk: str) -> dict:
    """Insert a proposal, or merge it into an existing row by ``dedup_key``.

    New rows: ``status='approved'`` when ``risk=='low'`` else ``'quarantine'``.
    Existing rows: a human decision (approved/rejected) is never overturned —
    only the recomputed evidence + payload are refreshed.

    Returns ``{'id', 'created', 'status'}``.
    """
    payload_j = json.dumps(proposal.payload, ensure_ascii=False)
    refs_j = json.dumps(sorted(set(proposal.evidence_refs)))
    with db.conn() as c:
        existing = c.execute(
            "SELECT id, status FROM learnings WHERE dedup_key = ?",
            (proposal.dedup_key,),
        ).fetchone()
        if existing is not None:
            c.execute(
                "UPDATE learnings SET evidence_count = ?, evidence_refs = ?, "
                "payload = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (proposal.evidence_count, refs_j, payload_j, existing["id"]),
            )
            return {"id": existing["id"], "created": False,
                    "status": existing["status"]}
        status = "approved" if risk == "low" else "quarantine"
        cur = c.execute(
            "INSERT INTO learnings (kind, field, template_name, payload, risk, "
            "status, origin, evidence_count, evidence_refs, dedup_key) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (proposal.kind, proposal.field, proposal.template_name, payload_j,
             risk, status, proposal.origin, proposal.evidence_count, refs_j,
             proposal.dedup_key),
        )
        return {"id": cur.lastrowid, "created": True, "status": status}


def list_proposals(status: str | None = None, kind: str | None = None,
                    limit: int = 500) -> list[dict]:
    """List learnings — quarantine first, then by evidence strength."""
    sql = "SELECT * FROM learnings"
    conds: list[str] = []
    params: list = []
    if status:
        conds.append("status = ?")
        params.append(status)
    if kind:
        conds.append("kind = ?")
        params.append(kind)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += (" ORDER BY (status = 'quarantine') DESC, evidence_count DESC, "
            "updated_at DESC LIMIT ?")
    params.append(limit)
    with db.conn() as c:
        return [_row_to_dict(r) for r in c.execute(sql, params).fetchall()]


def get_proposal(learning_id: int) -> dict | None:
    with db.conn() as c:
        r = c.execute(
            "SELECT * FROM learnings WHERE id = ?", (learning_id,)
        ).fetchone()
    return _row_to_dict(r) if r is not None else None


def _set_status(learning_id: int, status: str, decided_by: str,
                note: str = "") -> None:
    with db.conn() as c:
        c.execute(
            "UPDATE learnings SET status = ?, decided_by = ?, note = ?, "
            "decided_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (status, decided_by, note, learning_id),
        )


def approve_proposal(learning_id: int, decided_by: str, note: str = "") -> None:
    _set_status(learning_id, "approved", decided_by, note)


def reject_proposal(learning_id: int, decided_by: str, note: str = "") -> None:
    _set_status(learning_id, "rejected", decided_by, note)


def update_proposal_payload(learning_id: int, payload: dict, decided_by: str,
                            note: str | None = None) -> None:
    with db.conn() as c:
        if note is not None:
            c.execute(
                "UPDATE learnings SET payload = ?, decided_by = ?, note = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (json.dumps(payload, ensure_ascii=False), decided_by, note,
                 learning_id),
            )
        else:
            c.execute(
                "UPDATE learnings SET payload = ?, decided_by = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (json.dumps(payload, ensure_ascii=False), decided_by,
                 learning_id),
            )


def delete_proposal(learning_id: int) -> None:
    with db.conn() as c:
        c.execute("DELETE FROM learnings WHERE id = ?", (learning_id,))


def create_manual_proposal(kind: str, payload: dict, *, field: str | None = None,
                           template_name: str | None = None,
                           origin: str = "manual", risk: str = "behavior",
                           status: str = "quarantine",
                           dedup_key: str | None = None,
                           decided_by: str | None = None) -> dict:
    """Create a learning by hand (UI 'create rule' / LLM-proposed rule).

    Idempotent on ``dedup_key`` — re-submitting the same rule is a no-op.
    """
    if not dedup_key:
        body = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        dedup_key = f"{kind}|{origin}|{body}"
    with db.conn() as c:
        existing = c.execute(
            "SELECT id FROM learnings WHERE dedup_key = ?", (dedup_key,)
        ).fetchone()
        if existing is not None:
            return {"id": existing["id"], "created": False}
        cur = c.execute(
            "INSERT INTO learnings (kind, field, template_name, payload, risk, "
            "status, origin, evidence_count, evidence_refs, dedup_key, decided_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (kind, field, template_name,
             json.dumps(payload, ensure_ascii=False), risk, status, origin,
             0, "[]", dedup_key, decided_by),
        )
        return {"id": cur.lastrowid, "created": True}


def count_by_status() -> dict[str, int]:
    with db.conn() as c:
        rows = c.execute(
            "SELECT status, COUNT(*) n FROM learnings GROUP BY status"
        ).fetchall()
    return {r["status"]: r["n"] for r in rows}


# ---- runs (the ``learning_runs`` table) ------------------------------------

def record_run(validated_count: int) -> int:
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO learning_runs (validated_count_at_run, status) "
            "VALUES (?, 'running')",
            (validated_count,),
        )
        return cur.lastrowid


def finish_run(run_id: int, *, status: str = "done", edits_scanned: int = 0,
               proposals_new: int = 0, proposals_auto_applied: int = 0,
               proposals_quarantined: int = 0,
               corrections_per_sheet: float | None = None,
               error_message: str | None = None) -> None:
    with db.conn() as c:
        c.execute(
            "UPDATE learning_runs SET finished_at = CURRENT_TIMESTAMP, "
            "status = ?, edits_scanned = ?, proposals_new = ?, "
            "proposals_auto_applied = ?, proposals_quarantined = ?, "
            "corrections_per_sheet = ?, error_message = ? WHERE id = ?",
            (status, edits_scanned, proposals_new, proposals_auto_applied,
             proposals_quarantined, corrections_per_sheet, error_message,
             run_id),
        )


def last_run() -> dict | None:
    with db.conn() as c:
        r = c.execute(
            "SELECT * FROM learning_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return dict(r) if r is not None else None


def list_runs(limit: int = 200) -> list[dict]:
    """Completed runs that recorded a metric — the success-metric series."""
    with db.conn() as c:
        rows = c.execute(
            "SELECT * FROM learning_runs WHERE corrections_per_sheet IS NOT NULL "
            "ORDER BY id ASC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]

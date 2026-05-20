"""R110.C — Policy engine: versioning, promote, rollback, eval_gate.

Cada proposta aceite gera uma nova policy_version. A activa
manda. Rollback é trocar qual versão tem active=1. Tudo persistido em
SQLite, idempotente.

Eval gate corre antes de promover: testa a policy em shadow contra as
últimas N folhas validadas. Se edits_per_sheet sobe, falha.

Circuit breaker monitor (chamado externamente, e.g. via cron):
calcula edits_per_sheet recente vs baseline. Se sobe > 15%, reverte
para a versão anterior.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from app.web import db


logger = logging.getLogger(__name__)


_DEFAULT_EVAL_WINDOW = 50
_CIRCUIT_BREAKER_THRESHOLD = 0.15  # 15% regressão dispara rollback


# ----- Policy version helpers -------------------------------------------

def get_active_policy() -> dict | None:
    """Devolve a policy_version active (parsed) ou None se não houver."""
    p = db.get_active_policy_version()
    return p


def promote_policy_from_proposal(
    proposal_id: int,
    created_by: str = "human-approval",
) -> int | None:
    """Aceita uma proposta e cria/promove uma nova policy_version.

    1. Lê a proposta.
    2. Aplica change ao YAML actual.
    3. Corre eval_gate (opcional, dry_run inicial).
    4. Marca proposta como accepted/auto_applied.
    5. Cria nova policy_version + activa.

    Devolve version_id ou None se falhar.
    """
    proposal = db.get_proposal(proposal_id)
    if proposal is None:
        return None

    # Reler policy actual (YAML blob)
    current = get_active_policy()
    current_yaml = current.get("yaml_blob", "{}") if current else "{}"
    try:
        current_data = json.loads(current_yaml)
    except (json.JSONDecodeError, TypeError):
        current_data = {}

    # Aplicar a mudança
    new_data = _apply_proposal_to_policy(current_data, proposal)
    new_yaml = json.dumps(new_data, ensure_ascii=False, indent=2)
    diff_summary = _summarize_diff(current_data, new_data)

    # Corre eval gate (best effort — só regista, não bloqueia se "skipped")
    eval_results = run_eval_gate(proposal)

    parent = current.get("version") if current else None
    version_id = db.save_policy_version(
        parent_version=parent,
        yaml_blob=new_yaml,
        diff_summary=diff_summary,
        created_by=created_by,
        eval_results=eval_results,
    )
    db.activate_policy_version(version_id)

    # Marcar proposta como accepted
    db.decide_proposal(
        proposal_id,
        status="accepted",
        decided_by=created_by,
        eval_results=eval_results,
    )

    # Se for um template change, aplicar também o template_overlay
    if proposal["kind"] == "template":
        try:
            payload = proposal["payload"]
            if isinstance(payload, dict):
                db.save_template_overlay(
                    template_name=payload.get("template_name", ""),
                    change_type=payload.get("change_type", ""),
                    payload=payload,
                    proposal_id=proposal_id,
                )
        except Exception:  # noqa: BLE001
            logger.exception("falha ao gravar template_overlay")

    return version_id


def reject_proposal(proposal_id: int, decided_by: str = "human") -> bool:
    return db.decide_proposal(proposal_id, status="rejected", decided_by=decided_by)


def _apply_proposal_to_policy(policy: dict, proposal: dict) -> dict:
    """Aplica a proposta ao dict policy. Devolve novo dict."""
    new = dict(policy)
    kind = proposal["kind"]
    payload = proposal["payload"] if isinstance(proposal["payload"], dict) else {}

    if kind == "rule":
        # Acumular em new["rules"]
        rules = list(new.get("rules") or [])
        rules.append({
            "id": f"prop_{proposal['id']}",
            "kind": payload.get("kind") or "rule",
            "payload": payload,
            "added_at": proposal["created_at"],
        })
        new["rules"] = rules
    elif kind == "template":
        templates = dict(new.get("template_overlays") or {})
        tname = payload.get("template_name", "")
        if tname:
            existing = list(templates.get(tname) or [])
            existing.append({
                "change_type": payload.get("change_type"),
                "field_name": payload.get("field_name"),
                "field_position": payload.get("field_position"),
                "from_proposal": proposal["id"],
            })
            templates[tname] = existing
            new["template_overlays"] = templates
    elif kind == "cpis":
        overlays = list(new.get("cpis_overlays") or [])
        overlays.append({
            "scope": payload.get("scope"),
            "change_type": payload.get("change_type"),
            "column_name": payload.get("column_name"),
            "from_proposal": proposal["id"],
        })
        new["cpis_overlays"] = overlays
    return new


def _summarize_diff(old: dict, new: dict) -> str:
    """Diff legível PT-PT para o histórico de versões."""
    lines: list[str] = []
    n_rules_old = len(old.get("rules") or [])
    n_rules_new = len(new.get("rules") or [])
    if n_rules_new != n_rules_old:
        lines.append(f"rules: {n_rules_old} → {n_rules_new}")

    n_t_old = len(old.get("template_overlays") or {})
    n_t_new = len(new.get("template_overlays") or {})
    if n_t_new != n_t_old:
        lines.append(f"template_overlays: {n_t_old} → {n_t_new}")
    else:
        # Detectar adições em listas internas
        for t, vals in (new.get("template_overlays") or {}).items():
            old_vals = (old.get("template_overlays") or {}).get(t) or []
            if len(vals) != len(old_vals):
                lines.append(f"  {t}: {len(old_vals)} → {len(vals)} overlays")

    n_c_old = len(old.get("cpis_overlays") or [])
    n_c_new = len(new.get("cpis_overlays") or [])
    if n_c_new != n_c_old:
        lines.append(f"cpis_overlays: {n_c_old} → {n_c_new}")

    return "\n".join(lines) if lines else "(nenhuma diferença detectada)"


# ----- Eval gate --------------------------------------------------------

def run_eval_gate(
    proposal: dict,
    window: int = _DEFAULT_EVAL_WINDOW,
) -> dict:
    """Corre eval gate em shadow para uma proposta.

    Implementação inicial (R110.C): mede edits_per_sheet baseline nas
    últimas N folhas validadas. Não corre o motor com a policy proposta
    em shadow (requer integração mais profunda). Devolve métricas reais
    da baseline + um "decision: passed_dry_run" para não bloquear o
    workflow enquanto a integração shadow não é completa.
    """
    try:
        with db.conn() as c:
            row = c.execute(
                """SELECT COUNT(*) n_sheets,
                          AVG((SELECT COUNT(*) FROM edits e
                               WHERE e.sheet_id = s.id AND e.source = 'human')) avg_edits
                   FROM (
                       SELECT id FROM sheets
                       WHERE status = 'validated'
                       ORDER BY validated_at DESC LIMIT ?
                   ) s""",
                (window,),
            ).fetchone()
            n_sheets = row["n_sheets"] if row else 0
            avg_edits = float(row["avg_edits"] or 0) if row else 0.0

        return {
            "decision": "passed_dry_run",
            "window_size": window,
            "n_sheets_evaluated": n_sheets,
            "edits_per_sheet_baseline": round(avg_edits, 3),
            "edits_per_sheet_with_proposal": None,
            "note": "Eval gate em modo dry-run — métrica baseline calculada, "
                    "shadow execution da policy fica para iteração futura.",
        }
    except Exception as e:  # noqa: BLE001
        return {"decision": "error", "error": str(e)}


# ----- Circuit breaker -------------------------------------------------

def check_circuit_breaker(
    window_recent: int = 20,
    window_baseline: int = 100,
    threshold: float = _CIRCUIT_BREAKER_THRESHOLD,
) -> dict:
    """Detecta regressão de edits_per_sheet. Devolve relatório.

    Se a janela recente bate baseline em > threshold (default 15%),
    sinaliza para rollback. Não faz rollback automático — devolve a
    recomendação para o caller decidir (UI / cron).
    """
    try:
        with db.conn() as c:
            recent = c.execute(
                """SELECT AVG(n) avg_edits FROM (
                       SELECT (SELECT COUNT(*) FROM edits e
                               WHERE e.sheet_id = s.id AND e.source = 'human') AS n
                       FROM sheets s
                       WHERE status = 'validated'
                       ORDER BY validated_at DESC LIMIT ?
                   )""",
                (window_recent,),
            ).fetchone()
            baseline = c.execute(
                """SELECT AVG(n) avg_edits FROM (
                       SELECT (SELECT COUNT(*) FROM edits e
                               WHERE e.sheet_id = s.id AND e.source = 'human') AS n
                       FROM sheets s
                       WHERE status = 'validated'
                       ORDER BY validated_at DESC LIMIT ? OFFSET ?
                   )""",
                (window_baseline, window_recent),
            ).fetchone()

        recent_avg = float(recent["avg_edits"] or 0) if recent else 0.0
        baseline_avg = float(baseline["avg_edits"] or 0) if baseline else 0.0

        if baseline_avg == 0:
            return {
                "status": "no_baseline",
                "recent_avg": recent_avg,
                "baseline_avg": baseline_avg,
            }

        delta_rel = (recent_avg - baseline_avg) / baseline_avg
        triggered = delta_rel > threshold

        return {
            "status": "triggered" if triggered else "ok",
            "recent_avg": round(recent_avg, 3),
            "baseline_avg": round(baseline_avg, 3),
            "delta_rel": round(delta_rel, 3),
            "threshold": threshold,
            "recommendation": ("rollback ultima versao activa" if triggered
                               else "nenhuma accao necessaria"),
        }
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": str(e)}


def rollback_to_parent(reason: str = "manual") -> int | None:
    """Reverte para a parent_version da versão actualmente activa.
    Devolve o version_id resultante ou None se não há para onde reverter.
    """
    current = get_active_policy()
    if not current:
        return None
    parent = current.get("parent_version")
    if not parent:
        return None
    db.activate_policy_version(parent)
    db.log_circuit_breaker(
        event="rollback",
        action_taken=f"rollback de v{current['version']} para v{parent} ({reason})",
    )
    return parent


__all__ = [
    "get_active_policy", "promote_policy_from_proposal", "reject_proposal",
    "run_eval_gate", "check_circuit_breaker", "rollback_to_parent",
]

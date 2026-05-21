"""R110.C — Tools de propostas: rules, templates, CPIS.

O Qwen invoca estas tools quando vê padrões nos dados e quer sugerir
mudanças ao sistema. Cada proposta entra em `proposals` com status
'pending' (ou 'auto_applied' se passar o classificador AUTO + eval_gate).

Salvaguardas determinísticas validam o payload antes de aceitar — o
Qwen não pode contornar limits do schema.
"""
from __future__ import annotations

from typing import Any

from app.web import db


# Buffer por sessão para o wrapper qwen_agent recolher no final.
_current_session_proposals: list[dict] = []


def reset_session_proposals() -> None:
    _current_session_proposals.clear()


def get_session_proposals() -> list[dict]:
    return list(_current_session_proposals)


_VALID_RULE_KINDS = {
    "cliente_alias", "modelo_alias", "operador_alias",
    "confusion_pair", "snap_rule",
}
_VALID_TEMPLATE_CHANGES = {"add_field", "remove_field", "reorder"}
_VALID_CPIS_CHANGES = {"add_column", "remove_column", "reorder"}


def _classify_risk_rule(
    kind: str,
    payload: dict,
    evidence_count: int,
    qwen_confidence: float,
) -> str:
    """Risk classifier rule-based (Fase C).

    AUTO: alias com target conhecido + evidência >= 5 + confidence >= 0.9
    REVIEW: tudo o resto
    APPROVAL: confusion_pair com peso alto, snap_rule.
    """
    if kind == "snap_rule":
        return "approval"
    if kind == "confusion_pair":
        return "review"
    if kind in {"cliente_alias", "modelo_alias", "operador_alias"}:
        if evidence_count >= 5 and qwen_confidence >= 0.9:
            return "auto"
        return "review"
    return "review"


def _validate_rule_payload(kind: str, payload: dict) -> str:
    """Devolve mensagem de erro ou '' se válido."""
    if kind in {"cliente_alias", "modelo_alias", "operador_alias"}:
        fr = str(payload.get("from") or "").strip()
        to = str(payload.get("to") or "").strip()
        if not fr or not to:
            return "Alias precisa de 'from' e 'to' não vazios."
        if fr.upper() == to.upper():
            return "Alias 'from' e 'to' não podem ser iguais."
    elif kind == "confusion_pair":
        gc = str(payload.get("gold_char") or "")
        oc = str(payload.get("ocr_char") or "")
        if len(gc) != 1 or len(oc) != 1:
            return ("confusion_pair: 'gold_char' e 'ocr_char' têm de ter "
                    "EXACTAMENTE 1 carácter cada.")
    elif kind == "snap_rule":
        if not payload.get("field"):
            return "snap_rule precisa de 'field'."
    return ""


def propose_rule(
    kind: str,
    payload: dict,
    field: str = "",
    justification: str = "",
    evidence_sheet_ids: list[int] | None = None,
    qwen_confidence: float = 0.5,
) -> dict:
    """Propõe uma regra ao sistema.

    Tipos válidos: cliente_alias, modelo_alias, operador_alias,
    confusion_pair, snap_rule.

    A proposta é validada (schema + sanidade), classificada por risco
    (AUTO/REVIEW/APPROVAL), e gravada em `proposals` com status 'pending'.
    O AUTO só dispara se passar o eval_gate (Fase C completa).

    Args:
        kind: tipo de regra.
        payload: dict com os campos específicos do tipo.
        field: campo que a regra afecta (cliente, modelo, ...).
        justification: porquê esta regra (1-2 frases PT-PT).
        evidence_sheet_ids: IDs das folhas que suportam.
        qwen_confidence: 0..1 — quão certo estás.

    Returns:
        dict {status, proposal_id, risk_class, message}.
    """
    if kind not in _VALID_RULE_KINDS:
        return {
            "status": "error",
            "error": f"Tipo inválido: {kind}. Válidos: "
                     f"{', '.join(sorted(_VALID_RULE_KINDS))}.",
        }
    if not isinstance(payload, dict):
        return {"status": "error", "error": "payload tem de ser objecto."}

    err = _validate_rule_payload(kind, payload)
    if err:
        return {"status": "error", "error": err}

    evidence_ids = evidence_sheet_ids or []
    evidence_count = len(evidence_ids)
    risk = _classify_risk_rule(kind, payload, evidence_count,
                                float(qwen_confidence))
    evidence = {"sheet_ids": evidence_ids[:50], "n": evidence_count}
    if field:
        payload = dict(payload)
        payload.setdefault("field", field)

    try:
        pid = db.save_proposal(
            kind=kind,
            payload=payload,
            justification=justification or "",
            evidence=evidence,
            risk_class=risk,
            qwen_confidence=float(qwen_confidence),
        )
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": f"Falhou ao gravar: {e}"}

    # R117 — emit kernel event para a UI / cron / kernel reagirem
    try:
        from app import kernel
        kernel.emit_event("proposal_created", {
            "proposal_id": pid, "kind": "rule", "risk_class": risk,
        })
    except Exception:
        pass

    _current_session_proposals.append({
        "id": pid, "kind": kind, "field": field or payload.get("field"),
        "payload": payload, "reason": justification, "risk_class": risk,
    })
    return {
        "status": "ok",
        "proposal_id": pid,
        "risk_class": risk,
        "message": ("Proposta criada. Vai aparecer no envelope como "
                    "proposed_rule. O operador pode aceitar/rejeitar em "
                    "/learnings."),
    }


def propose_template_change(
    template_name: str,
    change_type: str,
    field_name: str = "",
    field_position: str = "",
    justification: str = "",
    evidence_sheet_ids: list[int] | None = None,
    qwen_confidence: float = 0.5,
) -> dict:
    """Propõe mudança a um template (e.g. adicionar coluna OV ao Quinadora).

    change_type: add_field | remove_field | reorder.

    Esta proposta entra como REVIEW sempre (operador valida com 1 clique).
    """
    if change_type not in _VALID_TEMPLATE_CHANGES:
        return {
            "status": "error",
            "error": f"change_type inválido. Válidos: "
                     f"{', '.join(_VALID_TEMPLATE_CHANGES)}.",
        }
    if not template_name.strip():
        return {"status": "error", "error": "template_name vazio."}
    if change_type in {"add_field", "remove_field"} and not field_name.strip():
        return {"status": "error",
                "error": f"{change_type} precisa de field_name."}

    # Validar que o template existe
    try:
        from app.templates_registry import TEMPLATES
        if template_name not in TEMPLATES:
            return {
                "status": "error",
                "error": f"Template '{template_name}' não existe. "
                         f"Disponíveis: {', '.join(sorted(TEMPLATES.keys()))}.",
            }
    except Exception:
        pass

    payload = {
        "template_name": template_name,
        "change_type": change_type,
        "field_name": field_name,
        "field_position": field_position or "end",
    }
    evidence_ids = evidence_sheet_ids or []
    evidence = {"sheet_ids": evidence_ids[:50], "n": len(evidence_ids)}

    try:
        pid = db.save_proposal(
            kind="template",
            payload=payload,
            justification=justification,
            evidence=evidence,
            risk_class="review",
            qwen_confidence=float(qwen_confidence),
        )
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": f"Falhou ao gravar: {e}"}

    # R117 — emit kernel event
    try:
        from app import kernel
        kernel.emit_event("proposal_created", {
            "proposal_id": pid, "kind": "template", "risk_class": "review",
        })
    except Exception:
        pass

    _current_session_proposals.append({
        "id": pid, "kind": "template",
        "payload": payload, "reason": justification, "risk_class": "review",
    })
    return {
        "status": "ok", "proposal_id": pid, "risk_class": "review",
        "message": "Proposta de template criada.",
    }


def propose_cpis_change(
    scope: str,
    change_type: str,
    column_name: str = "",
    justification: str = "",
    qwen_confidence: float = 0.5,
) -> dict:
    """Propõe mudança ao formato de exportação CPIS.

    scope: "all" ou nome de cliente/template a filtrar.
    change_type: add_column | remove_column | reorder.
    """
    if change_type not in _VALID_CPIS_CHANGES:
        return {
            "status": "error",
            "error": f"change_type inválido. Válidos: "
                     f"{', '.join(_VALID_CPIS_CHANGES)}.",
        }
    if change_type in {"add_column", "remove_column"} and not column_name:
        return {"status": "error",
                "error": f"{change_type} precisa de column_name."}

    payload = {
        "scope": scope or "all",
        "change_type": change_type,
        "column_name": column_name,
    }
    try:
        pid = db.save_proposal(
            kind="cpis",
            payload=payload,
            justification=justification,
            evidence={},
            risk_class="review",
            qwen_confidence=float(qwen_confidence),
        )
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": f"Falhou ao gravar: {e}"}

    # R117 — emit kernel event
    try:
        from app import kernel
        kernel.emit_event("proposal_created", {
            "proposal_id": pid, "kind": "cpis", "risk_class": "review",
        })
    except Exception:
        pass

    _current_session_proposals.append({
        "id": pid, "kind": "cpis",
        "payload": payload, "reason": justification, "risk_class": "review",
    })
    return {
        "status": "ok", "proposal_id": pid, "risk_class": "review",
        "message": "Proposta CPIS criada.",
    }


PROPOSE_TOOLS: dict[str, dict[str, Any]] = {
    "propose_rule": {
        "fn": propose_rule,
        "description": "Propõe regra ao sistema: cliente_alias, "
                       "modelo_alias, operador_alias, confusion_pair, "
                       "snap_rule. Entra em quarentena para aprovação humana "
                       "(ou auto se for alias com evidência forte).",
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {"type": "string",
                         "enum": sorted(_VALID_RULE_KINDS)},
                "payload": {"type": "object",
                            "description": "Para aliases: {from, to}. Para "
                                           "confusion_pair: {gold_char, "
                                           "ocr_char}. Para snap_rule: "
                                           "{field, pattern, target}."},
                "field": {"type": "string",
                          "description": "Campo afectado (cliente, modelo, "
                                         "lote, ...)."},
                "justification": {"type": "string",
                                  "description": "1-2 frases PT-PT a "
                                                 "explicar."},
                "evidence_sheet_ids": {"type": "array",
                                       "items": {"type": "integer"}},
                "qwen_confidence": {"type": "number",
                                    "description": "0..1 — quão certo estás."},
            },
            "required": ["kind", "payload"],
        },
    },
    "propose_template_change": {
        "fn": propose_template_change,
        "description": "Propõe alteração a um template (ex: adicionar coluna "
                       "OV ao template 'quinadora_pav8'). Tipos: add_field, "
                       "remove_field, reorder.",
        "parameters": {
            "type": "object",
            "properties": {
                "template_name": {"type": "string"},
                "change_type": {"type": "string",
                                "enum": sorted(_VALID_TEMPLATE_CHANGES)},
                "field_name": {"type": "string"},
                "field_position": {"type": "string",
                                   "description": "'end' ou nome de outro "
                                                  "campo para inserir depois."},
                "justification": {"type": "string"},
                "evidence_sheet_ids": {"type": "array",
                                       "items": {"type": "integer"}},
                "qwen_confidence": {"type": "number"},
            },
            "required": ["template_name", "change_type"],
        },
    },
    "propose_cpis_change": {
        "fn": propose_cpis_change,
        "description": "Propõe alteração ao formato de exportação CPIS "
                       "(coluna nova/removida/reordenada). Aplica-se a 'all' "
                       "ou a cliente/template específico.",
        "parameters": {
            "type": "object",
            "properties": {
                "scope": {"type": "string",
                          "description": "'all' ou nome cliente/template."},
                "change_type": {"type": "string",
                                "enum": sorted(_VALID_CPIS_CHANGES)},
                "column_name": {"type": "string"},
                "justification": {"type": "string"},
                "qwen_confidence": {"type": "number"},
            },
            "required": ["scope", "change_type"],
        },
    },
}


__all__ = [
    "PROPOSE_TOOLS", "propose_rule", "propose_template_change",
    "propose_cpis_change", "reset_session_proposals", "get_session_proposals",
]

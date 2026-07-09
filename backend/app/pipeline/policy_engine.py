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

import copy
import json
import logging

from app.web import db


logger = logging.getLogger(__name__)


_DEFAULT_EVAL_WINDOW = 50
_CIRCUIT_BREAKER_THRESHOLD = 0.15  # 15% regressão dispara rollback
_RULE_PROPOSAL_KINDS = {
    "cliente_alias",
    "modelo_alias",
    "operador_alias",
    "confusion_pair",
    "snap_rule",
}
_SIMULABLE_KINDS = {"cliente_alias", "modelo_alias"}


# ----- Policy version helpers -------------------------------------------

def get_active_policy() -> dict | None:
    """Devolve a policy_version active (parsed) ou None se não houver."""
    p = db.get_active_policy_version()
    return p


def _rule_kind_and_payload(proposal: dict) -> tuple[str, dict]:
    """Return the rule subtype for both stored and canonical proposal shapes."""
    raw_payload = proposal.get("payload") or {}
    payload = raw_payload if isinstance(raw_payload, dict) else {}
    kind = (proposal.get("kind") or "").strip()

    if kind == "rule":
        sub_kind = (payload.get("kind") or "").strip()
    elif kind in _RULE_PROPOSAL_KINDS:
        sub_kind = kind
    else:
        sub_kind = ""

    if sub_kind:
        payload = dict(payload)
        payload.setdefault("kind", sub_kind)
    return sub_kind, payload


def _rule_key(rule: dict) -> tuple[str, str]:
    payload = rule.get("payload") if isinstance(rule.get("payload"), dict) else {}
    return (
        str(rule.get("kind") or "").strip(),
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
    )


def _template_overlay_key(overlay: dict) -> tuple[str, str, str]:
    return (
        str(overlay.get("change_type") or "").strip(),
        str(overlay.get("field_name") or "").strip(),
        str(overlay.get("field_position") or "").strip(),
    )


def _cpis_overlay_key(overlay: dict) -> tuple[str, str, str]:
    return (
        str(overlay.get("scope") or "").strip(),
        str(overlay.get("change_type") or "").strip(),
        str(overlay.get("column_name") or "").strip(),
    )


def _no_effect_eval_result(proposal: dict) -> dict:
    return {
        "decision": "passed_dry_run",
        "window_size": 0,
        "n_sheets_evaluated": 0,
        "edits_per_sheet_baseline": 0.0,
        "edits_per_sheet_with_proposal": None,
        "note": (
            "proposal has no effect on the active policy; "
            f"not promoted (kind={proposal.get('kind')})"
        ),
    }


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

    if new_data == current_data:
        eval_results = _no_effect_eval_result(proposal)
        db.decide_proposal(
            proposal_id,
            status="rejected",
            decided_by=created_by,
            eval_results=eval_results,
        )
        return current.get("version") if current else None

    # Corre eval gate. `passed_dry_run` continua permissivo porque pode
    # significar refs/sheets insuficientes, mas um `failed` explícito bloqueia
    # a promoção.
    eval_results = run_eval_gate(proposal)
    if (eval_results or {}).get("decision") == "failed":
        db.decide_proposal(
            proposal_id,
            status="rejected",
            decided_by=created_by,
            eval_results=eval_results,
        )
        return current.get("version") if current else None

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
        except Exception:
            logger.exception("falha ao gravar template_overlay")

    return version_id


def reject_proposal(proposal_id: int, decided_by: str = "human") -> bool:
    return db.decide_proposal(proposal_id, status="rejected", decided_by=decided_by)


def _apply_proposal_to_policy(policy: dict, proposal: dict) -> dict:
    """Aplica a proposta ao dict policy. Devolve novo dict."""
    new = dict(policy)
    kind = proposal["kind"]
    payload = proposal["payload"] if isinstance(proposal["payload"], dict) else {}
    rule_kind, rule_payload = _rule_kind_and_payload(proposal)

    if rule_kind:
        # Acumular em new["rules"]
        rules = list(new.get("rules") or [])
        new_rule = {
            "id": f"prop_{proposal['id']}",
            "kind": rule_kind,
            "payload": rule_payload,
            "added_at": str(proposal.get("created_at") or ""),
        }
        existing_keys = {_rule_key(r) for r in rules}
        if _rule_key(new_rule) not in existing_keys:
            rules.append(new_rule)
        new["rules"] = rules
    elif kind == "template":
        templates = dict(new.get("template_overlays") or {})
        tname = payload.get("template_name", "")
        if tname:
            existing = list(templates.get(tname) or [])
            new_overlay = {
                "change_type": payload.get("change_type"),
                "field_name": payload.get("field_name"),
                "field_position": payload.get("field_position"),
                "from_proposal": proposal["id"],
            }
            existing_keys = {_template_overlay_key(o) for o in existing}
            if _template_overlay_key(new_overlay) not in existing_keys:
                existing.append(new_overlay)
            templates[tname] = existing
            new["template_overlays"] = templates
    elif kind == "cpis":
        overlays = list(new.get("cpis_overlays") or [])
        new_overlay = {
            "scope": payload.get("scope"),
            "change_type": payload.get("change_type"),
            "column_name": payload.get("column_name"),
            "from_proposal": proposal["id"],
        }
        existing_keys = {_cpis_overlay_key(o) for o in overlays}
        if _cpis_overlay_key(new_overlay) not in existing_keys:
            overlays.append(new_overlay)
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

# R117 — eval gate real: A/B em shadow com últimas N folhas validadas.
_EVAL_SHADOW_WINDOW = 20          # folhas usadas para o A/B
_EVAL_MIN_SHEETS = 5              # abaixo disto → fallback dry-run
_EVAL_REGRESSION_THRESHOLD = 0.05 # piora > 5% → failed


def _cells_need_attention(scoring: dict) -> int:
    """R117 — Conta `snapped + very_different` no output do shadow_score.

    São as células que o operador tem de rever; baixar este número é
    sinal de que a proposta melhora o motor."""
    summary = scoring.get("summary", {}) if scoring else {}
    return int(summary.get("snapped", 0)) + int(summary.get("very_different", 0))


def _apply_proposal_to_refs(proposal: dict, base_refs: dict) -> dict | None:
    """R117 — Aplica a proposta a uma cópia (shallow) das refs.

    Devolve refs modificadas se simulável, ou None se a proposta não tem
    forma de ser simulada com um shadow_score leve (ex.: confusion_pair,
    snap_rule, template, cpis).
    """
    sub_kind, payload = _rule_kind_and_payload(proposal)
    if sub_kind not in _SIMULABLE_KINDS:
        return None

    fr = str(payload.get("from") or "").strip().upper()
    to = str(payload.get("to") or "").strip().upper()
    if not fr or not to:
        return None

    # Cópia shallow — as estruturas internas são tratadas como read-only
    # pelo shadow_score, exceptuando os dicts de aliases que aqui
    # (re)injectamos. Aliases não podem inventar pools de plan: sem uma
    # entry real em of_to_entries/plan_by_* o cross-check deve continuar NA.
    new_refs = dict(base_refs)

    if sub_kind == "cliente_alias":
        aliases = {
            str(k).strip().upper(): str(v).strip().upper()
            for k, v in (new_refs.get("cliente_aliases") or {}).items()
            if str(k).strip() and str(v).strip()
        }
        aliases[fr] = to
        new_refs["cliente_aliases"] = aliases
    elif sub_kind == "modelo_alias":
        aliases = {
            str(k).strip().upper(): str(v).strip().upper()
            for k, v in (new_refs.get("modelo_aliases") or {}).items()
            if str(k).strip() and str(v).strip()
        }
        aliases[fr] = to
        new_refs["modelo_aliases"] = aliases

    # Forçar nova entrada no INDEX_CACHE com loaded_at distinto, para o
    # shadow_score reindexar com as refs alteradas.
    new_refs["loaded_at"] = f"{base_refs.get('loaded_at', '')}+prop{proposal.get('id', 0)}"
    return new_refs


def _apply_proposal_to_sheet_data(proposal: dict, sheet_data: dict) -> tuple[dict, bool]:
    """Return sheet_data as it would look after an OCR alias correction.

    Eval-gate aliases must be tested on the captured rows, not by inventing
    plan refs. We only replace exact case-insensitive values in the field the
    proposal targets; fuzzy matching belongs to the OCR/snap layer.
    """
    sub_kind, payload = _rule_kind_and_payload(proposal)
    field_by_kind = {
        "cliente_alias": "cliente",
        "modelo_alias": "modelo",
    }
    field = field_by_kind.get(sub_kind)
    if field is None:
        return sheet_data, False

    fr = str(payload.get("from") or "").strip().upper()
    to = str(payload.get("to") or "").strip()
    if not fr or not to:
        return sheet_data, False

    rows = sheet_data.get("rows")
    if not isinstance(rows, list):
        return sheet_data, False

    out = copy.deepcopy(sheet_data)
    changed = False
    for row in out.get("rows") or []:
        if not isinstance(row, dict):
            continue
        current = str(row.get(field) or "").strip()
        if current.upper() == fr:
            row[field] = to
            changed = True

    return out, changed


def _load_validated_sheets_for_eval(window: int) -> list[dict]:
    """R117 — Lê últimas N folhas validadas com sheet_data + dq_audit
    parseados. Devolve [] se falhar (defensive)."""
    try:
        with db.conn() as c:
            rows = c.execute(
                """SELECT id, sheet_data, dq_audit
                   FROM sheets
                   WHERE status = 'validated'
                     AND sheet_data IS NOT NULL
                   ORDER BY validated_at DESC LIMIT ?""",
                (window,),
            ).fetchall()
    except Exception:
        logger.exception("R117 eval gate: falha a ler sheets validadas")
        return []

    out: list[dict] = []
    for r in rows:
        try:
            sd = json.loads(r["sheet_data"]) if r["sheet_data"] else {}
            da = json.loads(r["dq_audit"]) if r["dq_audit"] else {}
        except (json.JSONDecodeError, TypeError):
            continue
        if not sd:
            continue
        out.append({"id": r["id"], "sheet_data": sd, "dq_audit": da})
    return out


def run_eval_gate(
    proposal: dict,
    window: int = _EVAL_SHADOW_WINDOW,
) -> dict:
    """R117 — Eval gate real com shadow A/B.

    Algoritmo:
      1. Carrega últimas `window` sheets validadas (mín. _EVAL_MIN_SHEETS).
      2. Para cada uma corre `shadow_score(refs_atuais)` (baseline) e, se a
         proposta for simulável, corre também com refs modificadas.
      3. Métrica: avg(snapped + very_different) — células que o operador
         tem de rever. Baixar = melhora.
      4. Decisão:
         - `passed`        se with_proposal ≤ baseline (não piora)
         - `failed`        se with_proposal piora > _EVAL_REGRESSION_THRESHOLD
         - `passed_dry_run` se proposta não simulável ou dados insuficientes.

    Mantém as chaves do retorno legacy para compat com `db.save_policy_version`
    e UI: decision, edits_per_sheet_baseline, edits_per_sheet_with_proposal,
    note, n_sheets_evaluated.
    """
    sheets = _load_validated_sheets_for_eval(window)
    n = len(sheets)

    if n < _EVAL_MIN_SHEETS:
        return {
            "decision": "passed_dry_run",
            "window_size": window,
            "n_sheets_evaluated": n,
            "edits_per_sheet_baseline": 0.0,
            "edits_per_sheet_with_proposal": None,
            "note": "insufficient validated sheets for shadow eval "
                    f"(have {n}, need {_EVAL_MIN_SHEETS})",
        }

    # Lazy imports — só puxar scoring_engine / ref_watcher quando o gate
    # corre de facto (mantém startup leve para CLIs sem refs).
    try:
        from app.pipeline.scoring_engine import shadow_score
        from app.cross_check.ref_watcher import get_watcher
        refs = get_watcher().get_refs()
    except Exception as e:
        logger.exception("R117 eval gate: falha a carregar shadow_score/refs")
        return {
            "decision": "passed_dry_run",
            "window_size": window,
            "n_sheets_evaluated": n,
            "edits_per_sheet_baseline": 0.0,
            "edits_per_sheet_with_proposal": None,
            "note": f"shadow_score/refs unavailable: {e}",
        }

    sub_kind, _payload = _rule_kind_and_payload(proposal)
    proposal_refs = _apply_proposal_to_refs(proposal, refs)
    cliente_alias_disabled_for_cross = sub_kind == "cliente_alias"
    simulable = proposal_refs is not None and not cliente_alias_disabled_for_cross

    # Baseline pass — sempre corre, mesmo quando não simulável (dá número
    # informativo no histórico de versões).
    baseline_attn: list[int] = []
    with_attn: list[int] = []
    failed_sheets = 0
    for s in sheets:
        try:
            scoring_b, *_ = shadow_score(s["sheet_data"], s["dq_audit"], refs)
            baseline_attn.append(_cells_need_attention(scoring_b))
        except Exception:
            failed_sheets += 1
            continue
        if simulable:
            try:
                proposal_sheet_data, _changed = _apply_proposal_to_sheet_data(
                    proposal,
                    s["sheet_data"],
                )
                scoring_w, *_ = shadow_score(
                    proposal_sheet_data,
                    s["dq_audit"],
                    proposal_refs,
                )
                with_attn.append(_cells_need_attention(scoring_w))
            except Exception:
                # Se a proposta rebenta o motor numa folha, conta como piora
                # severa — pre-warning para o reviewer humano.
                with_attn.append(_cells_need_attention(scoring_b) + 1)

    if not baseline_attn:
        return {
            "decision": "passed_dry_run",
            "window_size": window,
            "n_sheets_evaluated": n,
            "edits_per_sheet_baseline": 0.0,
            "edits_per_sheet_with_proposal": None,
            "note": f"shadow_score failed on every sheet (failed={failed_sheets})",
        }

    baseline_avg = sum(baseline_attn) / len(baseline_attn)

    if cliente_alias_disabled_for_cross:
        return {
            "decision": "failed",
            "window_size": window,
            "n_sheets_evaluated": n,
            "edits_per_sheet_baseline": round(baseline_avg, 3),
            "edits_per_sheet_with_proposal": round(baseline_avg, 3),
            "note": "cliente_alias ignored by row cross; aliases cannot pass as cross evidence",
        }

    if not simulable:
        return {
            "decision": "passed_dry_run",
            "window_size": window,
            "n_sheets_evaluated": n,
            "edits_per_sheet_baseline": round(baseline_avg, 3),
            "edits_per_sheet_with_proposal": None,
            "note": ("proposal not simulable in shadow A/B "
                     "(kind != cliente_alias|modelo_alias); baseline-only"),
        }

    with_avg = sum(with_attn) / len(with_attn) if with_attn else baseline_avg

    # Regressão relativa — protege casos baseline ≈ 0 com guarda absoluta.
    if baseline_avg <= 0:
        regressed = with_avg > _EVAL_REGRESSION_THRESHOLD
    else:
        regressed = (with_avg - baseline_avg) / baseline_avg > _EVAL_REGRESSION_THRESHOLD

    if regressed:
        decision = "failed"
        note = (f"shadow A/B: proposta piora cells_need_attention "
                f"{baseline_avg:.2f} → {with_avg:.2f} (> {_EVAL_REGRESSION_THRESHOLD:.0%})")
    else:
        decision = "passed"
        note = (f"shadow A/B: proposta não piora "
                f"({baseline_avg:.2f} → {with_avg:.2f}, "
                f"n={n}, failed={failed_sheets})")

    return {
        "decision": decision,
        "window_size": window,
        "n_sheets_evaluated": n,
        "edits_per_sheet_baseline": round(baseline_avg, 3),
        "edits_per_sheet_with_proposal": round(with_avg, 3),
        "note": note,
    }


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
    except Exception as e:
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

"""OCR pipeline runner — R109 lean.

Corre OCR via ocr6 + detecção de template + Pass-2 se aplicável.
DQ / snap antigos foram descartados — a normalização e validação acontecem
no motor unificado (``app.pipeline.scoring_engine.cross_check_sheet``),
chamado por ``main._run_and_store_cross_check`` após o upload.

Public API:
    run_pipeline(image_path: Path) -> dict
        raw            — OCR output (header/rows/footer)
        dq             — stub vazio (compat)
        current        — igual a raw (cross_check aplica edits depois)
        template_name  — template detectado

    rerun_pipeline_for_template(image_path, template_name) -> dict
        Mesma forma; template forçado.
"""
from __future__ import annotations

import sys
import time  # R224 — timing por passagem (profiling)
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "backend"))

import json  # R132 — parse Pass-1.5 side-detect response
import sys  # R132 — side-detect failure log
from typing import Final  # R132

import ocr6  # type: ignore

from app.dq.alignment import check_and_fix_alignment
from app.pipeline.prompt_builder import (
    build_discovery_prompt,
    build_prompt,
    build_side_detect_prompt,
)
from app.templates_registry import (
    DEFAULT_TEMPLATE,
    TEMPLATES,
    detect_template_with_reason,
    get_template,
)

# rev00 (13/04/2026) — TODAS as folhas passaram a ter frente (produção) + verso
# (paragens) partilhando o `setor_maquina`. Qualquer template de produção pode
# aparecer como verso → mapeia-se para o template genérico `paragens`. O
# `run_pipeline` usa a pista de página da captura guiada (autoritativa) e/ou o
# mini-OCR side-detect para escolher o lado.
_GENERIC_PARAGENS: Final[str] = "paragens"


def __getattr__(name: str) -> object:
    # Task C E4 — era um snapshot de import-time; com templates registados
    # em runtime (set_runtime_templates) ficaria desatualizado. PEP 562:
    # calculado a cada acesso, sempre em sincronia com TEMPLATES.
    if name == "TWO_SIDED_TEMPLATES":
        return {
            tname: _GENERIC_PARAGENS
            for tname, tpl in TEMPLATES.items()
            if tpl.has_production_rows
        }
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

_PROMPT_PATH = _REPO / "prompts" / "ocr6_v3.txt"
_V3_PROMPT, _V3_PROMPT_HASH = ocr6.load_prompt(_PROMPT_PATH)
ocr6.PROMPT, ocr6.PROMPT_HASH = _V3_PROMPT, _V3_PROMPT_HASH

# R256 — o prompt passou a ser parâmetro de ocr6.process_image; o padrão
# swap-global de ocr6.PROMPT (R117, _swap_prompt + _PROMPT_LOCK) foi removido.
# O R117 serializava o bloco swap→OCR→restore, mas o Pass-1 lia o global SEM
# o lock — em paralelização futura o lock não protegeria nada. Com o prompt
# por parâmetro a classe de bug desaparece. O install v3 acima mantém-se: é o
# prompt default do Pass-1 (prompt=None) e do CLI.


def _detect_side(image_path: Path) -> str:
    """R132 / rev00 — Pass-1.5 mini OCR para kanbans 2-lados. Devolve
    'F' (frente, produção), 'V' (verso, paragens) ou '?' (indeterminado).

    Strategy: corre process_image com o side-detect prompt (~3-5s output)
    e parse o `raw_response` JSON em busca da chave `side`.

    rev00: deixou de haver fallback silencioso a 'F'. Em qualquer falha
    (resposta vazia, JSON impossível de parsear, valor inesperado) devolve
    '?' para o chamador poder marcar a folha para revisão humana em vez de a
    tratar cegamente como frente — agora que ~metade das fotos são versos, um
    verso lido como produção corromperia o CSV da fábrica.

    Custo: 1 chamada extra ao Ollama (~5s) quando corre inline.
    """
    result = ocr6.process_image(
        image_path, idx=1, total=1, prompt=build_side_detect_prompt(),
    )
    raw = getattr(result, "raw_response", "") or ""
    if not raw:
        return "?"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # Modelo devolveu texto com markdown ou prefix — tentar extrair {…}
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return "?"
        try:
            parsed = json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            return "?"
    side = str(parsed.get("side", "")).strip().upper()
    return side if side in ("F", "V") else "?"


def parse_discovery_response(raw: str) -> dict:
    """Task C E4 — parse tolerante da resposta de descoberta de template.

    NUNCA levanta. Devolve sempre o mesmo shape:
    {"parse_ok", "title", "header", "columns", "footer", "raw"} — com
    parse_ok=False o wizard mostra o aviso e o humano preenche à mão.
    """
    out: dict = {
        "parse_ok": False, "title": "",
        "header": [], "columns": [], "footer": [],
        "raw": raw or "",
    }
    if not raw:
        return out
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # markdown/prefixo à volta do JSON — tenta extrair {…} (padrão
        # _detect_side).
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return out
        try:
            parsed = json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            return out
    if not isinstance(parsed, dict):
        return out

    def _str_list(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(v).strip() for v in value if str(v).strip()]

    out["title"] = str(parsed.get("title") or "").strip()
    out["header"] = _str_list(parsed.get("header"))
    out["columns"] = _str_list(parsed.get("columns"))
    out["footer"] = _str_list(parsed.get("footer"))
    # Sem colunas não há template utilizável — parse_ok exige-as.
    out["parse_ok"] = bool(out["columns"])
    return out


def run_discovery(image_path: Path) -> dict:
    """Task C E4 — mini-OCR de descoberta do layout de um template novo
    (wizard /admin). Padrão _detect_side: process_image com o discovery
    prompt por parâmetro. Nunca levanta — falhas devolvem parse_ok=False.
    """
    try:
        result = ocr6.process_image(
            image_path, idx=1, total=1, prompt=build_discovery_prompt(),
        )
        raw = getattr(result, "raw_response", "") or ""
    except Exception as exc:
        print(f"[discovery] {image_path.name}: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return parse_discovery_response("")
    return parse_discovery_response(raw)


def _looks_confidently_frente(pass1_raw: dict) -> bool:
    """Structural fast-path — decide, sem custo, que a foto é frente.

    O Pass-1 já correu o prompt de produção; se devolveu ≥2 linhas com
    identidade de produção (OF de 6 dígitos ou um MODELO não-trivial), a folha
    é inequivocamente uma frente e pode saltar-se o mini-OCR de ~5s. Um verso
    de paragens não tem colunas OF/MODELO, por isso nunca dispara isto — a
    estrutura só é confiada na direcção SEGURA (frente-confiante → saltar),
    nunca para declarar um verso. Não depende de QTD (preenchida no mobile
    depois do upload), logo apanha frentes ainda sem quantidades.
    """
    good = 0
    for r in pass1_raw.get("rows") or []:
        if not isinstance(r, dict):
            continue
        of = str(r.get("of", "") or "").strip()
        modelo = str(r.get("modelo", "") or "").strip()
        if (of.isdigit() and len(of) == 6) or len(modelo) >= 4:
            good += 1
            if good >= 2:
                return True
    return False


def _pass1_looks_nonproduction(pass1_raw: dict) -> bool:
    """rev00 — Pass-1 (prompt de produção) com ≥2 linhas PREENCHIDAS mas NENHUMA
    com identidade de produção (OF de 6 díg / MODELO) → provável verso (paragens)
    lido como produção. Usado só para o check estrutural de pista=F: conservador
    (exige conteúdo mas sem produção), para não marcar frentes esparsas/mal-lidas.
    """
    filled = 0
    ident = 0
    for r in pass1_raw.get("rows") or []:
        if not isinstance(r, dict):
            continue
        if not any(str(v or "").strip() for v in r.values()):
            continue
        filled += 1
        of = str(r.get("of", "") or "").strip()
        modelo = str(r.get("modelo", "") or "").strip()
        if (of.isdigit() and len(of) == 6) or len(modelo) >= 4:
            ident += 1
    return filled >= 2 and ident == 0


def _run_ocr(image_path: Path, template: Any = None) -> tuple[dict, Any]:
    """R224 — devolve (dict, metrics): o `ExtractionMetrics` (eval_count,
    retries, model, duration_sec) deixa de ser descartado, para o profiling.

    R256 — com template, o prompt do template segue por parâmetro (era o
    swap-global do R117); sem template usa o global (v3, instalado acima).
    """
    if template is not None:
        result = ocr6.process_image(
            image_path, idx=1, total=1,
            row_fields=template.row_fields,
            header_fields=template.header_fields,
            footer_fields=template.footer_fields,
            prompt=build_prompt(template),
        )
    else:
        result = ocr6.process_image(image_path, idx=1, total=1)
    if not result.metrics or result.metrics.status != "ok":
        err = result.metrics.error if result.metrics else "unknown"
        raise RuntimeError(f"OCR failed: {err}")
    return {
        "header": result.header,
        "rows": result.rows,
        "footer": result.footer,
    }, result.metrics


def _ocr_metrics_dict(m: Any) -> dict | None:
    """R224 — `ExtractionMetrics` → dict leve para o profiling."""
    if m is None:
        return None
    return {
        "eval_count": getattr(m, "eval_count", 0),
        "retries": getattr(m, "retries", 0),
        "duration_sec": round(getattr(m, "duration_sec", 0.0), 1),
        "status": getattr(m, "status", None),
        "rows_count": getattr(m, "rows_count", 0),
    }


def _merge_pass2_into_pass1(pass1: dict, pass2: dict) -> dict:
    h1 = pass1.get("header", {}) or {}
    h2 = pass2.get("header", {}) or {}

    base_order = ("operador", "n_operador", "setor_maquina", "cod_maquina", "data", "turno")
    extra_keys = list(dict.fromkeys(
        k for k in (*h2.keys(), *h1.keys())
        if k not in base_order
    ))
    header_keys = [k for k in base_order if k in h1 or k in h2] + extra_keys

    merged_header = {}
    for k in header_keys:
        v2 = (h2.get(k) or "").strip()
        v1 = (h1.get(k) or "").strip()
        if k == "setor_maquina":
            # R139 — Pass-1 é a leitura real do papel (OCR genérico, sem o prompt
            # do template a fixar um setor canónico); preserva ACABAMENTO MTG2/MTG4
            # em vez de deixar o Pass-2 enviesado ganhar.
            merged_header[k] = v1 or v2
        else:
            merged_header[k] = v2 or v1
    return {
        "header": merged_header,
        "rows": pass2.get("rows", []) or [],
        "footer": pass2.get("footer", {}) or {},
    }


def _empty_dq_stub() -> dict:
    """R109 — DQ legacy foi descartado. Stub vazio para compat com a UI."""
    return {
        "cells": {},
        "violations": [],
        "score": 1.0,
        "stp_eligible": True,
        "summary": {"n_review": 0, "n_violations": 0},
    }


def _compact_alnum(value: object) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def _looks_like_model_code(value: object) -> bool:
    compact = _compact_alnum(value)
    if len(compact) < 5:
        return False
    return any(ch.isalpha() for ch in compact) and any(ch.isdigit() for ch in compact)


def _digits_only(value: object) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _looks_like_of(value: object) -> bool:
    digits = _digits_only(value)
    return len(digits) == 6


def _looks_like_short_qty(value: object) -> bool:
    compact = _compact_alnum(value)
    digits = _digits_only(value)
    return bool(digits) and len(compact) <= 4


def _looks_like_reference_code(value: object) -> bool:
    compact = _compact_alnum(value)
    if len(compact) < 5:
        return False
    prefixes = (
        "CA", "CB", "CBC", "CBO", "CD", "CF", "CFC", "CFH", "CG", "CGC",
        "CL", "CLC", "CR", "CS", "CSC", "CO", "GC",
    )
    if compact.startswith(prefixes):
        return True
    return _looks_like_model_code(value)


def _present(value: object) -> bool:
    return bool(str(value or "").strip())


def _bobine_dims_present(row: dict) -> int:
    return sum(
        1 for field in ("comp_mm", "larg_mm", "esp", "lbase", "ltopo", "lote")
        if _present(row.get(field))
    )


def _score_acabamento_like_row(row: dict) -> tuple[int, list[str]]:
    """Score a default-template row for the 3-column Acabamento shape."""
    score = 0
    reasons: list[str] = []
    cliente = row.get("cliente")
    ov = row.get("ov")
    of = row.get("of")
    modelo = row.get("modelo")
    qtd = row.get("qtd")

    if _looks_like_of(of):
        score += 2
        reasons.append("of_6_digits")
    if not _present(ov):
        score += 2
        reasons.append("ov_empty")
    elif _looks_like_reference_code(ov):
        score += 1
        reasons.append("ov_reference_like")
    else:
        score -= 4
        reasons.append("ov_present")

    if not _present(cliente):
        score += 2
        reasons.append("cliente_empty")
    elif _looks_like_reference_code(cliente):
        score += 2
        reasons.append("cliente_reference_like")
    else:
        score -= 2
        reasons.append("cliente_text")

    if not _present(modelo):
        score += 1
        reasons.append("modelo_empty")
    elif _looks_like_reference_code(modelo):
        score += 2
        reasons.append("modelo_reference_like")

    if _looks_like_short_qty(qtd):
        score += 1
        reasons.append("qtd_short")

    dims = _bobine_dims_present(row)
    if dims == 0:
        score += 1
        reasons.append("no_bobine_dims")
    elif dims >= 2:
        score -= 3
        reasons.append("bobine_dims_present")
    if _present(row.get("lote")):
        score -= 3
        reasons.append("lote_present")
    return score, reasons


def _acabamento_structure_analysis(pass1_raw: dict) -> dict[str, Any]:
    rows = [r for r in (pass1_raw.get("rows") or []) if isinstance(r, dict)]
    best_score = 0
    acabamento_like = 0
    bobine_like = 0
    best_reasons: list[str] = []
    for row in rows:
        score, reasons = _score_acabamento_like_row(row)
        if score > best_score:
            best_score = score
            best_reasons = reasons
        if score >= 5 and any(
            r in reasons
            for r in ("cliente_reference_like", "modelo_reference_like", "ov_reference_like")
        ):
            acabamento_like += 1
        if _present(row.get("ov")) and _bobine_dims_present(row) >= 2:
            bobine_like += 1
    return {
        "score": best_score,
        "rows": len(rows),
        "acabamento_like_rows": acabamento_like,
        "bobine_like_rows": bobine_like,
        "reasons": best_reasons,
    }


def _infer_template_from_default_pass1(pass1_raw: dict) -> Any | None:
    analysis = _acabamento_structure_analysis(pass1_raw)
    if (
        analysis["acabamento_like_rows"] > 0
        and analysis["bobine_like_rows"] == 0
        and analysis["score"] >= 5
    ):
        return get_template("acabamento")
    return None


def _build_current_and_dq(raw_extraction: dict, template: Any) -> tuple[dict, dict]:
    """R135 — corre o guard de alinhamento de colunas sobre a cópia editável
    (`current`), mantendo `raw_extraction` como snapshot OCR intacto (audit).

    Move valores trocados entre PRI<->OF nos casos não-ambíguos e regista cada
    movimento / violação em `dq['violations']`.
    """
    dq = _empty_dq_stub()
    rows = raw_extraction.get("rows", []) or []
    fixed_rows, flags = check_and_fix_alignment(rows, tuple(template.row_fields))
    current = dict(raw_extraction)
    current["rows"] = fixed_rows
    if flags:
        dq["violations"] = flags
        dq["summary"]["n_violations"] = len(flags)
    return current, dq


def run_pipeline(image_path: Path, page_hint: str | None = None) -> dict:
    """R109 — corre OCR + detecção de template. Sem DQ.

    A normalização (snap_cliente, snap_modelo, etc.) acontece no motor
    unificado via cross_check_sheet (chamado a seguir pelo main).

    rev00 (13/04/2026) — TODAS as folhas têm 2 lados (frente=produção,
    verso=paragens) partilhando o `setor_maquina`. Escolha do lado:
      • `page_hint` ∈ {"F","V"} da captura guiada é **autoritativo** para o
        routing. Um check estrutural INLINE (custo zero) compara a pista com o
        Pass-1 e marca `needs_review` quando contradizem (pista=V mas parece
        frente / pista=F mas sem produção) — o depósito fica suspenso.
      • Sem pista → corre o `_detect_side` mini-OCR inline como decisor. Se
        devolver '?' (indeterminado), extrai como frente MAS marca
        `needs_review=True` para o depósito ficar suspenso até revisão humana.

    Devolve, além do habitual, chaves de lado no dict: `side` ('F'/'V'),
    `side_source` ('hint'/'detect'/'na') e `needs_review` (bool) + `review_reason`.
    """
    timing: dict[str, int] = {}  # R224 — ms por etapa (profiling)

    t = time.perf_counter()
    pass1_raw, m1 = _run_ocr(image_path)
    timing["pass1_ms"] = int((time.perf_counter() - t) * 1000)
    header = pass1_raw.get("header", {}) or {}
    setor = header.get("setor_maquina", "")
    cod_maquina = header.get("cod_maquina", "")
    template, detection_source = detect_template_with_reason(
        setor, cod_maquina=cod_maquina,
    )
    structure = _acabamento_structure_analysis(pass1_raw)
    if template.name == DEFAULT_TEMPLATE.name:
        inferred = _infer_template_from_default_pass1(pass1_raw)
        if inferred is not None:
            template = inferred
            detection_source = "row_structure"

    template_detection = {
        "source": detection_source,
        "raw_setor": str(setor or ""),
        "raw_cod_maquina": str(cod_maquina or ""),
        "structural_score": structure.get("score", 0),
        "structural_rows": structure.get("rows", 0),
        "acabamento_like_rows": structure.get("acabamento_like_rows", 0),
        "bobine_like_rows": structure.get("bobine_like_rows", 0),
        "structural_reasons": structure.get("reasons", []),
    }

    # rev00 — escolha do lado (frente/verso) para kanbans 2-lados.
    side = "F"
    side_source = "na"
    needs_review = False
    review_reason = ""
    hint = (page_hint or "").strip().upper()
    # Task C E4 — has_production_rows em vez do dict snapshot: cobre também
    # templates registados em runtime (unidades novas).
    if template.has_production_rows:
        if hint in ("F", "V"):
            # Pista da captura guiada é autoritativa para o routing.
            side = hint
            side_source = "hint"
            # rev00 — check estrutural INLINE (custo zero, corre ANTES do
            # depósito): se a pista contradiz o que o Pass-1 mostra, marca a
            # folha para revisão (o gate de depósito trava-a). Substitui o
            # cross-check assíncrono (removido: era assimétrico e corria tarde
            # demais). Conservador — só as direcções de baixo falso-positivo.
            if hint == "V" and _looks_confidently_frente(pass1_raw):
                needs_review = True          # verso marcado, mas parece frente
                review_reason = "side_hint_conflict"
            elif hint == "F" and _pass1_looks_nonproduction(pass1_raw):
                needs_review = True          # frente marcada, mas sem produção
                review_reason = "side_hint_conflict"
        elif _looks_confidently_frente(pass1_raw):
            # Fast-path estrutural: Pass-1 já mostra produção → é frente, salta
            # o mini-OCR (custo zero). Nunca declara verso por estrutura.
            side = "F"
            side_source = "structure"
            timing["side_detect_ms"] = 0
        else:
            # Sem pista e sem sinal claro de frente: mini-OCR inline decide.
            # '?' → extrai como frente mas marca para revisão (não deposita
            # produção de folha duvidosa).
            t = time.perf_counter()
            try:
                detected = _detect_side(image_path)
            except Exception as exc:
                print(
                    f"[side_detect] {image_path.name}: {type(exc).__name__}: {exc}"
                    " — indeterminado, marcado para revisão",
                    file=sys.stderr,
                )
                detected = "?"
            timing["side_detect_ms"] = int((time.perf_counter() - t) * 1000)
            side_source = "detect"
            if detected in ("F", "V"):
                side = detected
            else:
                side = "F"  # extrai como frente, mas sinaliza
                needs_review = True
                review_reason = "side_indeterminate"
        if side == "V":
            template = get_template(_GENERIC_PARAGENS)

    m2 = None
    if template.name == DEFAULT_TEMPLATE.name:
        raw_extraction = pass1_raw
    else:
        # R256 — o prompt do template segue por parâmetro dentro de _run_ocr;
        # o bloco atómico swap→OCR→restore do R117 deixou de ser necessário.
        t = time.perf_counter()
        pass2_raw, m2 = _run_ocr(image_path, template=template)
        timing["pass2_ms"] = int((time.perf_counter() - t) * 1000)
        raw_extraction = _merge_pass2_into_pass1(pass1_raw, pass2_raw)

    raw_extraction["template_name"] = template.name
    raw_extraction["template_detection"] = template_detection

    t = time.perf_counter()
    current, dq = _build_current_and_dq(raw_extraction, template)
    timing["build_ms"] = int((time.perf_counter() - t) * 1000)
    return {
        "raw": raw_extraction,
        "dq": dq,
        "current": current,
        "template_name": template.name,
        "template_detection": template_detection,
        # rev00 — info de lado (frente/verso) para o cross-check + gate de depósito.
        "side": side,
        "side_source": side_source,
        "needs_review": needs_review,
        "review_reason": review_reason,
        "timing": timing,
        "metrics": {
            "model": getattr(m1, "model", None),
            "pass1": _ocr_metrics_dict(m1),
            "pass2": _ocr_metrics_dict(m2),
            "eval_count_total": (getattr(m1, "eval_count", 0) or 0)
            + (getattr(m2, "eval_count", 0) or 0),
        },
    }


def rerun_pipeline_for_template(image_path: Path, template_name: str) -> dict:
    """Forçar um template específico (operador corrigiu o setor)."""
    template = get_template(template_name)
    if template.name == DEFAULT_TEMPLATE.name:
        raw_extraction, _ = _run_ocr(image_path)
    else:
        raw_extraction, _ = _run_ocr(image_path, template=template)

    raw_extraction["template_name"] = template.name
    raw_extraction["template_detection"] = {
        "source": "forced",
        "raw_setor": str((raw_extraction.get("header") or {}).get("setor_maquina") or ""),
        "raw_cod_maquina": str((raw_extraction.get("header") or {}).get("cod_maquina") or ""),
        "structural_score": 0,
        "structural_rows": len(raw_extraction.get("rows") or []),
        "acabamento_like_rows": 0,
        "bobine_like_rows": 0,
        "structural_reasons": [],
    }

    current, dq = _build_current_and_dq(raw_extraction, template)
    return {
        "raw": raw_extraction,
        "dq": dq,
        "current": current,
        "template_name": template.name,
        "template_detection": raw_extraction.get("template_detection", {}),
    }


__all__ = ["run_pipeline", "rerun_pipeline_for_template"]

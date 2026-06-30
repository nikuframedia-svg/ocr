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

import hashlib
import sys
import threading  # R117 — protege swap global de ocr6.PROMPT
import time  # R224 — timing por passagem (profiling)
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "backend"))

import json  # R132 — parse Pass-1.5 side-detect response
import sys  # R132 — side-detect failure log
from typing import Final  # R132

import ocr6  # type: ignore  # noqa: E402

from app.dq.alignment import check_and_fix_alignment  # noqa: E402
from app.pipeline.prompt_builder import (  # noqa: E402
    build_prompt,
    build_side_detect_prompt,
)
from app.templates_registry import (  # noqa: E402
    DEFAULT_TEMPLATE,
    detect_template_with_reason,
    get_template,
)

# R132 — kanbans com 2 lados (frente/verso) que partilham `setor_maquina`.
# Map: nome do template default (frente, devolvido por detect_template) →
# nome do template do verso. `run_pipeline` corre um Pass-1.5 mini-OCR
# (side-detect) para escolher qual usar.
TWO_SIDED_TEMPLATES: Final[dict[str, str]] = {
    "maq_fustes": "maq_fustes_paragens",  # TPL103 MÁQUINA DE FUSTES
}

_PROMPT_PATH = _REPO / "prompts" / "ocr6_v3.txt"
_V3_PROMPT, _V3_PROMPT_HASH = ocr6.load_prompt(_PROMPT_PATH)
ocr6.PROMPT, ocr6.PROMPT_HASH = _V3_PROMPT, _V3_PROMPT_HASH

# R117 — `ocr6.PROMPT` é estado global no módulo ocr6. _swap_prompt muta-o
# e a API actual de ocr6.process_image não aceita o prompt como
# argumento, pelo que temos de o trocar antes da chamada e restaurar
# depois. Para tornar isto seguro em paralelização futura, serializamos
# o bloco swap→OCR→restore. Custo: throughput limitado a 1 OCR
# simultâneo enquanto o lock for segurado (~25 s/folha). Aceitável; a
# alternativa correcta é refactor ao ocr6 para aceitar o prompt
# directamente, mas isso está fora do scope deste R117.
_PROMPT_LOCK = threading.Lock()


def _swap_prompt(prompt_text: str) -> tuple[str, str]:
    prev = (ocr6.PROMPT, ocr6.PROMPT_HASH)
    ocr6.PROMPT = prompt_text
    ocr6.PROMPT_HASH = hashlib.sha256(prompt_text.encode()).hexdigest()[:12]
    return prev


def _detect_side(image_path: Path) -> str:
    """R132 — Pass-1.5 mini OCR para kanbans 2-lados (TPL103 MÁQUINA DE
    FUSTES). Devolve 'F' (frente, produção) ou 'V' (verso, paragens).

    Strategy: swap o `ocr6.PROMPT` para o side-detect prompt (~3-5s output),
    corre process_image, parse o `raw_response` JSON em busca da chave
    `side`. Fallback silencioso a 'F' em qualquer erro (default frente é o
    caso mais comum).

    Custo: 1 chamada extra ao Ollama por upload MAQ_FUSTES (~5s).
    """
    prompt = build_side_detect_prompt()
    with _PROMPT_LOCK:
        prev = _swap_prompt(prompt)
        try:
            result = ocr6.process_image(image_path, idx=1, total=1)
        finally:
            ocr6.PROMPT, ocr6.PROMPT_HASH = prev
    raw = getattr(result, "raw_response", "") or ""
    if not raw:
        return "F"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # Modelo devolveu texto com markdown ou prefix — tentar extrair {…}
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return "F"
        try:
            parsed = json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            return "F"
    side = str(parsed.get("side", "F")).strip().upper()
    return side if side in ("F", "V") else "F"


def _run_ocr(image_path: Path, template: Any = None) -> tuple[dict, Any]:
    """R224 — devolve (dict, metrics): o `ExtractionMetrics` (eval_count,
    retries, model, duration_sec) deixa de ser descartado, para o profiling."""
    if template is not None:
        result = ocr6.process_image(
            image_path, idx=1, total=1,
            row_fields=template.row_fields,
            header_fields=template.header_fields,
            footer_fields=template.footer_fields,
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


def run_pipeline(image_path: Path) -> dict:
    """R109 — corre OCR + detecção de template. Sem DQ.

    A normalização (snap_cliente, snap_modelo, etc.) acontece no motor
    unificado via cross_check_sheet (chamado a seguir pelo main).

    R132 — kanbans com 2 lados (TPL103 MÁQUINA DE FUSTES) partilham o
    `setor_maquina` mas têm tabelas diferentes (frente=produção vs
    verso=paragens). Para esses templates corre-se um Pass-1.5 mini-OCR
    (`_detect_side`, ~5s) que inspecciona o cabeçalho da tabela na foto
    e escolhe o template apropriado antes do Pass-2 completo.
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

    # R132 — side-detect Pass-1.5 para kanbans 2-lados. Falha do detector
    # é silenciosa (assume frente — caso mais comum); operador pode usar
    # `rerun_pipeline_for_template` se o template wrongly detected.
    if template.name in TWO_SIDED_TEMPLATES:
        t = time.perf_counter()
        try:
            side = _detect_side(image_path)
            if side == "V":
                template = get_template(TWO_SIDED_TEMPLATES[template.name])
        except Exception as exc:  # noqa: BLE001
            print(
                f"[side_detect] {image_path.name}: {type(exc).__name__}: {exc} "
                "— fallback to frente",
                file=sys.stderr,
            )
        timing["side_detect_ms"] = int((time.perf_counter() - t) * 1000)

    m2 = None
    if template.name == DEFAULT_TEMPLATE.name:
        raw_extraction = pass1_raw
    else:
        # R117 — swap→OCR→restore tem de ser atómico para evitar race em
        # paralelização futura (Pass-1 da próxima folha veria o prompt errado).
        with _PROMPT_LOCK:
            prev = _swap_prompt(build_prompt(template))
            try:
                t = time.perf_counter()
                pass2_raw, m2 = _run_ocr(image_path, template=template)
                timing["pass2_ms"] = int((time.perf_counter() - t) * 1000)
            finally:
                ocr6.PROMPT, ocr6.PROMPT_HASH = prev
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
        # R117 — ver comentário em run_pipeline; mesmo motivo.
        with _PROMPT_LOCK:
            prev = _swap_prompt(build_prompt(template))
            try:
                raw_extraction, _ = _run_ocr(image_path, template=template)
            finally:
                ocr6.PROMPT, ocr6.PROMPT_HASH = prev

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

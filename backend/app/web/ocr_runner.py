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
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "backend"))

import json  # R132 — parse Pass-1.5 side-detect response
import sys  # R132 — side-detect failure log
from typing import Final  # R132

import ocr6  # type: ignore  # noqa: E402

from app.pipeline.prompt_builder import (  # noqa: E402
    build_prompt,
    build_side_detect_prompt,
)
from app.templates_registry import (  # noqa: E402
    DEFAULT_TEMPLATE,
    detect_template,
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


def _run_ocr(image_path: Path, template: Any = None) -> dict:
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
    }


def _merge_pass2_into_pass1(pass1: dict, pass2: dict) -> dict:
    h1 = pass1.get("header", {}) or {}
    h2 = pass2.get("header", {}) or {}
    merged_header = {}
    for k in ("operador", "n_operador", "setor_maquina", "cod_maquina", "data"):
        v2 = (h2.get(k) or "").strip()
        v1 = (h1.get(k) or "").strip()
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
    pass1_raw = _run_ocr(image_path)
    setor = (pass1_raw.get("header", {}) or {}).get("setor_maquina", "")
    template = detect_template(setor)

    # R132 — side-detect Pass-1.5 para kanbans 2-lados. Falha do detector
    # é silenciosa (assume frente — caso mais comum); operador pode usar
    # `rerun_pipeline_for_template` se o template wrongly detected.
    if template.name in TWO_SIDED_TEMPLATES:
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

    if template.name == DEFAULT_TEMPLATE.name:
        raw_extraction = pass1_raw
    else:
        # R117 — swap→OCR→restore tem de ser atómico para evitar race em
        # paralelização futura (Pass-1 da próxima folha veria o prompt errado).
        with _PROMPT_LOCK:
            prev = _swap_prompt(build_prompt(template))
            try:
                pass2_raw = _run_ocr(image_path, template=template)
            finally:
                ocr6.PROMPT, ocr6.PROMPT_HASH = prev
        raw_extraction = _merge_pass2_into_pass1(pass1_raw, pass2_raw)

    raw_extraction["template_name"] = template.name

    return {
        "raw": raw_extraction,
        "dq": _empty_dq_stub(),
        "current": raw_extraction,
        "template_name": template.name,
    }


def rerun_pipeline_for_template(image_path: Path, template_name: str) -> dict:
    """Forçar um template específico (operador corrigiu o setor)."""
    template = get_template(template_name)
    if template.name == DEFAULT_TEMPLATE.name:
        raw_extraction = _run_ocr(image_path)
    else:
        # R117 — ver comentário em run_pipeline; mesmo motivo.
        with _PROMPT_LOCK:
            prev = _swap_prompt(build_prompt(template))
            try:
                raw_extraction = _run_ocr(image_path, template=template)
            finally:
                ocr6.PROMPT, ocr6.PROMPT_HASH = prev

    raw_extraction["template_name"] = template.name

    return {
        "raw": raw_extraction,
        "dq": _empty_dq_stub(),
        "current": raw_extraction,
        "template_name": template.name,
    }


__all__ = ["run_pipeline", "rerun_pipeline_for_template"]

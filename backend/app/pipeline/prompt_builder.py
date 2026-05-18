"""Round 54 — Template-aware prompt builder.

The original `prompts/ocr6_v3.txt` is hardcoded to BOBINE-FORMATO's 13
columns. With 11 templates, we need a way to produce a prompt tuned to
each template's schema without writing 11 separate prompt files.

This module builds the prompt programmatically from a `TemplateSpec`:
  - Lists the template's row_fields in the right order
  - Generates a row skeleton with the correct field keys
  - Mentions the template's `setor_maquina` in HEADER guidance
  - Preserves the v3 "watch-out" rules verbatim (proven 87.9% field accuracy)

Public API:
    build_prompt(template: TemplateSpec) -> str  # full OCR prompt
    build_setor_only_prompt() -> str             # cheap pass-1 for setor detection

Notes:
- For BOBINE-FORMATO this should produce a prompt EQUIVALENT to v3 so
  there's no accuracy regression on the 65 legacy sheets.
- Gemini (TPL102) templates get a slightly different schema explainer
  because their domain (nesting de chapa, M², NESTING code) is alien
  to the v3 instructions.
- Paragens (QUINADORA PAV.4) is a special schema with no production
  rows — handled separately.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from app.templates_registry import TemplateSpec

logger = logging.getLogger(__name__)

_OVERLAY_PATH = (
    Path(__file__).resolve().parents[3] / "lexicons" / "learned_overlay.json"
)


# Field display names for the column header line in the prompt.
# These mirror what the operator sees on the kanban paper.
_FIELD_LABELS: dict[str, str] = {
    "pri": "PRI",
    "cliente": "CLIENTE",
    "ov": "OV",
    "of": "OF",
    "modelo": "MODELO",
    "qtd": "QTD",
    "qtd_metros": "QTD (METROS)",
    "comp_mm": "COMP_MM",
    "larg_mm": "LARG_MM",
    "lote": "LOTE",
    "coni": "CONI",
    "esp": "ESP",
    "lbase": "LBASE",
    "ltopo": "LTOPO",
    "sobras": "SOBRAS",
    "cesta_n": "CESTA Nº",
    "motivo": "MOTIVO DA PARAGEM",
    "inicio": "INÍCIO",
    "fim": "FIM",
    "duracao": "DURAÇÃO",
    "resolvido": "RESOLVIDO",
    # TPL102 Gemini
    "pf": "PF",
    "cf": "C/F",
    "m2": "M²",
    "nesting": "NESTING",
    "np": "NP",
}


def _columns_line(template: TemplateSpec) -> str:
    """Build the `COL1 | COL2 | COL3` line for the prompt."""
    return " | ".join(_FIELD_LABELS.get(f, f.upper()) for f in template.row_fields)


def _row_skeleton(template: TemplateSpec) -> str:
    """Build the JSON skeleton for one row, e.g. `{"pri":"", "cliente":"", ...}`."""
    pairs = ",".join(f'"{f}":""' for f in template.row_fields)
    return "{" + pairs + "}"


def _footer_skeleton(template: TemplateSpec) -> str:
    """Build the JSON skeleton for the footer."""
    if not template.footer_fields:
        return ""
    pairs = ",\n    ".join(f'"{f}": ""' for f in template.footer_fields)
    return pairs


def _canonical_setor(template: TemplateSpec) -> str:
    """First (canonical) alias for the template's setor field."""
    return template.setor_aliases[0] if template.setor_aliases else ""


# ============================================================================
#  Shared rules (verbatim from v3 — proven 87.9% field accuracy)
# ============================================================================

_RULES_PRODUCTION = """IMPORTANT RULES:
- Extract EVERY row that has data — do not skip any row.
- Some rows may have the MODELO field written across 2 lines — join them with a space.
- Some MODELO values include suffixes like "1ª PRIORIDADE" or "2ª PRIORIDADE" — preserve them.
- PRI values can be: numbers (1, 2, 5), codes (c7, C16, C24, P2, P4), or combinations (REP. C9).
- CLIENTE may have no spaces (MTGBELUX) or have spaces (MTG GMBH, DAV NORDIC, LE HAVRE).
- CONI can be a number (10, 12, 14) or text (T, OCT, TORRES).
- LOTE follows pattern like M25B0746, M26B0307, H24B1003 — copy exactly what you see.
- If a field is empty or not visible, use empty string "".
- Do NOT invent values. If a digit is unclear, copy your best reading; do not make up plausible alternatives.
- Normalize date to DD-MM-YYYY format.

EXAMPLES of MODELO (copy verbatim, do not normalize):
  CGC2E10D, CLC8F07Ri-V, CFC5F45Riv, CD03P502, CB04E63D, CA08E10B,
  OMEGA60-6M, PTJ157578, 1383VF01, 8615F00, BSBCA0066, LMF1882T,
  BRAÇOS, CGC2E06Di-2ªPRIORIDADE.

WATCH OUT FOR HANDWRITING CONFUSIONS:
- 0 (zero) vs O (letter) — MODELO codes almost always have digits where they look like 0.
- 1 (one) vs I (letter) vs L (letter) — usually 1 in MODELO codes.
- 5 vs S, 6 vs G, 8 vs B — handwriting can blur these.
- Ç in modelos like BRAÇOS — keep the cedilla, do not drop it."""

_RULES_PARAGENS = """IMPORTANT RULES:
- Extract EVERY paragem (downtime entry) that has data — do not skip any row.
- MOTIVO is free-text Portuguese describing why production stopped.
- INÍCIO and FIM are times in HH:MM format (24h clock).
- DURAÇÃO is HH:MM (total minutes paused).
- RESOLVIDO is a yes/no marker (sim/não, S/N, ✓/✗).
- If a field is empty, use empty string "".
- Do NOT invent values."""

_RULES_GEMINI = """IMPORTANT RULES:
- Extract EVERY nesting entry (row) that has data — do not skip any row.
- PF is a priority/production-flow code, often a single letter (P, F) or number.
- C/F is a code combining cliente / fabrica info — copy verbatim.
- M² is the area in square metres, normally a decimal (e.g. 12.5, 8.34).
- QTD is the number of pieces.
- NESTING is the nesting program code (alphanumeric).
- INÍCIO and FIM are times in HH:MM.
- NP is a numbering field — copy verbatim.
- If a field is empty, use empty string "".
- Do NOT invent values."""


# ============================================================================
#  Few-shot examples (Round 98 — learning-engine feedback loop)
# ============================================================================

def _fewshot_block(template_name: str) -> str:
    """Build a few-shot block of confirmed extractions for this template.

    The learning engine selects the best validated sheets per template and
    records their ids in ``lexicons/learned_overlay.json``. We render up to
    3 of them as worked JSON examples so the model imitates the structure,
    field formatting and vocabulary of human-validated sheets.

    Returns ``""`` when there are no examples — the prompt is then
    byte-identical to the pre-few-shot version (no regression).
    """
    try:
        if not _OVERLAY_PATH.exists():
            return ""
        overlay = json.loads(_OVERLAY_PATH.read_text(encoding="utf-8"))
        sheet_ids = overlay.get("fewshot_by_template", {}).get(template_name, [])
        if not sheet_ids:
            return ""
        from app.web import db

        examples: list[str] = []
        for sid in sheet_ids[:3]:
            sheet = db.get_sheet(int(sid))
            if not sheet or not sheet.get("sheet_data"):
                continue
            sd = sheet["sheet_data"]
            slim = {
                "header": sd.get("header", {}),
                "rows": sd.get("rows", []),
                "footer": sd.get("footer", {}),
            }
            examples.append(json.dumps(slim, ensure_ascii=False, indent=2))
        if not examples:
            return ""
        worked = "\n\n".join(
            f"VALIDATED EXAMPLE {i}:\n{ex}" for i, ex in enumerate(examples, 1)
        )
        return (
            "\nHere are confirmed, human-validated extractions of this exact "
            "kanban type. Match their JSON structure, field formatting and "
            "vocabulary — but read THIS image, do not copy their values:\n"
            f"{worked}\n"
        )
    except Exception:  # noqa: BLE001 — few-shot is best-effort, never break OCR
        logger.exception("few-shot block build failed for %s", template_name)
        return ""


# ============================================================================
#  Builders
# ============================================================================

def build_prompt(template: TemplateSpec) -> str:
    """Return the full OCR prompt customized for ``template``.

    Drop-in replacement for the global ``ocr6.PROMPT``. Sets the
    row schema, header guidance, and rules block according to the
    template's family (production / paragens / gemini).
    """
    setor = _canonical_setor(template)
    cols_line = _columns_line(template)
    row_skel = _row_skeleton(template)
    footer_skel = _footer_skeleton(template)
    has_footer = bool(template.footer_fields)

    # Choose rules block by family
    if not template.has_production_rows:
        rules = _RULES_PARAGENS
        domain_hint = "DOWNTIME (paragens — registo de paragens de máquina)"
    elif template.is_gemini:
        rules = _RULES_GEMINI
        domain_hint = "NESTING DE CHAPA (Gemini — produção de cortes em chapa)"
    else:
        rules = _RULES_PRODUCTION
        domain_hint = "PRODUÇÃO DE COLUNAS"

    # Round 98 — few-shot block from the learning engine. Empty string when
    # the template has no learned examples, keeping the prompt unchanged.
    fewshot = _fewshot_block(template.name)

    # Header skeleton (identical for all templates)
    # cod_maquina is the printed machine code (e.g. M032 for BOBINE-FORMATO).
    # Empty string when not visible — derivation table fills it later.
    header_skel = (
        '    "operador": "FULL NAME",\n'
        '    "n_operador": "e.g. 0537",\n'
        f'    "setor_maquina": "{setor}",\n'
        '    "cod_maquina": "e.g. M032 (machine code printed near sector name; empty if absent)",\n'
        '    "data": "DD-MM-YYYY"'
    )

    # Footer block: paragens has no footer
    if has_footer:
        footer_block = (
            ',\n  "footer": {\n'
            f'    {footer_skel}\n'
            '  }'
        )
    else:
        footer_block = ""

    # Footer line in the prompt's structure description
    footer_desc = (
        f"3. FOOTER fields: {', '.join(_FIELD_LABELS.get(f, f.upper()) for f in template.footer_fields)}"
        if has_footer
        else "3. (This template has no footer — only header + rows.)"
    )

    return f"""You are an OCR and data extraction expert. Analyze this industrial Kanban production sheet image carefully.

This kanban is for {domain_hint}. Setor/Máquina = {setor}.

Extract ALL data and return ONLY a valid JSON object. No markdown, no <think> blocks, no explanation.

The sheet has:
1. HEADER fields: Operador (full name), N° (operator number), Setor/Maquina, Cód. Máquina (M0xx code printed near the sector — leave "" if absent), Data
2. A TABLE with exactly these columns in order:
   {cols_line}
{footer_desc}

{rules}
{fewshot}
Return this exact JSON structure:
{{
  "header": {{
{header_skel}
  }},
  "rows": [
    {row_skel}
  ]{footer_block}
}}
"""


def build_setor_only_prompt() -> str:
    """Lightweight prompt that asks ONLY for the setor/máquina label.

    Used in 2-pass extraction when we want to detect the template
    before committing to a full extraction. Much shorter prompt
    means faster Ollama response (~5s vs ~22s).
    """
    return """You are reading an industrial Kanban production sheet.

In the HEADER area, find the field labelled "Setor/Maquina" (or "Setor / Máquina", or "Máquina").
Extract ONLY that value — the machine or sector identifier.

Return a JSON object with exactly one key:
{
  "setor_maquina": "EXACT VALUE AS WRITTEN"
}

Common values include: BOBINE-FORMATO, GUILHOTINA, LINHA DE CORTE,
QUINADORA PAV.4, QUINADORA PAV.8, GUIFIL, SOLDLINE 4, LASER, MANUAL,
ROBOT, EXPEDIÇÃO, GASPARINI, HPE32, HD36.

No markdown. No explanation. No <think> blocks."""


__all__ = ["build_prompt", "build_setor_only_prompt"]

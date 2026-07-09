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
    "coni": "FERRAMENTA / CONI",
    "esp": "ESP",
    "lbase": "LBASE",
    "ltopo": "LTOPO",
    # R128 — kanban LASER (dimensões diâmetro base/topo)
    "dbase": "DBASE",
    "dtopo": "DTOPO",
    "sobras": "SOBRAS",
    "cesta_n": "CESTA Nº",
    # rev00 (13/04/2026) — coluna SUCATA (nº de peças sucatadas) em todas as
    # tabelas de produção.
    "sucata": "SUCATA",
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
    """Build the `COL1 | COL2 | COL3` line for the prompt.

    R129+ — per-template overrides via `template.field_labels` (e.g.
    Acabamento stores "REFERÊNCIA / PEÇA" internally as `modelo`).
    """
    labels = {**_FIELD_LABELS, **(template.field_labels or {})}
    return " | ".join(labels.get(f, f.upper()) for f in template.row_fields)


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


def _field_label(template: TemplateSpec, field: str) -> str:
    labels = {**_FIELD_LABELS, **(template.field_labels or {})}
    return labels.get(field, field.upper())


_HEADER_LABELS: dict[str, str] = {
    "operador": "Operador (full name)",
    "n_operador": "N° (operator number)",
    "setor_maquina": "Setor/Maquina",
    "cod_maquina": (
        'Cód. Máquina (M0xx code printed near the sector — leave "" if absent)'
    ),
    "data": "Data",
    "turno": "Turno",
}


def _header_desc(template: TemplateSpec) -> str:
    return ", ".join(
        _HEADER_LABELS.get(field, field.upper())
        for field in template.header_fields
        if field != "pernr"
    )


def _canonical_setor(template: TemplateSpec) -> str:
    """First (canonical) alias for the template's setor field."""
    return template.setor_aliases[0] if template.setor_aliases else ""


# R132 — Templates JSON-skeleton for each known header field. Used by
# `_header_skel` to render the JSON shape dynamically from
# `template.header_fields`, so per-template extras (turno, etc.) work.
_HEADER_FIELD_TEMPLATES: dict[str, str] = {
    "operador": '"operador": "FULL NAME"',
    "n_operador": '"n_operador": "e.g. 0537"',
    "setor_maquina": '"setor_maquina": "{setor}"',
    "cod_maquina": '"cod_maquina": "e.g. M032 (machine code printed near sector name; empty if absent)"',
    "data": '"data": "DD-MM-YYYY"',
    "turno": '"turno": "M | R | XM | T (which checkbox is marked; empty if none)"',
}


def _header_skel(template: TemplateSpec, setor: str) -> str:
    """R132 — Render the JSON header skeleton from `template.header_fields`.

    Pre-R132 this was hardcoded to 5 fields, which silently dropped the
    `turno` field defined in acabamento and maq_fustes. Now we
    iterate the template's actual header_fields and emit the right line
    for each. `pernr` is skipped (derived by snap_operador, not OCR).
    """
    lines: list[str] = []
    for field in template.header_fields:
        if field == "pernr":
            continue
        tpl = _HEADER_FIELD_TEMPLATES.get(field, f'"{field}": ""')
        lines.append("    " + tpl.format(setor=setor))
    return ",\n".join(lines)


# ============================================================================
#  Shared rules (verbatim from v3 — proven 87.9% field accuracy)
# ============================================================================

_RULES_PRODUCTION = """IMPORTANT RULES:
- Extract EVERY row that has data — do not skip any row.
- COLUMN ALIGNMENT (critical): the table has a FIXED set of columns. For EACH
  row emit one value PER column, in order, using the exact JSON keys shown —
  ALWAYS include every key, even when the cell is blank (then use ""). A blank
  or unreadable cell is "" for THAT key only: never shift the next column's
  value left to fill the gap, and never borrow a value from the row above/below.
- Each column is independent — a value belongs to the column it physically sits
  under. OF is EXACTLY 6 digits; OV is digits; PRI is a short priority code or
  number (1, 2, C7, P4, REP. C9) and is NEVER a 6-digit number. If a value does
  not fit its column's shape, you are probably reading the wrong column — re-check
  the physical alignment before placing it.
- Some rows may have the MODELO field written across 2 lines — join them with a space.
- Some MODELO values include suffixes like "1ª PRIORIDADE" or "2ª PRIORIDADE" — preserve them.
- PRI values can be: numbers (1, 2, 5), codes (c7, C16, C24, P2, P4), or combinations (REP. C9).
- CLIENTE may have no spaces (MTGBELUX) or have spaces (MTG GMBH, DAV NORDIC, LE HAVRE).
- FERRAMENTA / CONI can only be CONI, TORRES, OCT, CIL, CIO, CIB, or a number.
  Normalize obvious punctuation/case variants in your JSON (for example OCT. -> OCT).
- TURNO is the shift checkbox in the header: one of M, R, XM, T. Report the
  marked one; use empty string "" if no checkbox is marked or none is present.
- LOTE follows pattern like M25B0746, M26B0307, H24B1003 — copy exactly what you see.
- SUCATA is the number of scrapped/rejected pieces for that row (a small integer).
  It is usually blank — use "" when the cell is empty; never invent a value.
- If a field is empty or not visible, use empty string "".
- Do NOT invent values. If a digit is unclear, copy your best reading; do not make up plausible alternatives.
- Normalize date to DD-MM-YYYY format.

EXAMPLES of MODELO (copy verbatim, do not normalize):
  CGC2E10D, CLC8F07Ri-V, CFC5F45Riv, CD03P502, CB04E63D, CA08E10B,
  OMEGA60-6M, PTJ157578, 1383VF01, 8615F00, BSBCA0066, LMF1882T,
  BRAÇOS, CGC2E06Di-2ªPRIORIDADE.

COLUMN ALIGNMENT EXAMPLE — a row whose PRI cell is blank keeps every key; the
empty cell is "" and nothing shifts left:
  CORRECT: {"pri":"", "cliente":"ENEDIS", "ov":"2603385", "of":"262559", "modelo":"CGC2E10D", ...}
  WRONG  : {"pri":"ENEDIS", "cliente":"2603385", "of":"262559", "modelo":"CGC2E10D", ...}  (values shifted left)

WATCH OUT FOR HANDWRITING CONFUSIONS:
- 0 (zero) vs O (letter) — MODELO codes almost always have digits where they look like 0.
- 1 (one) vs I (letter) vs L (letter) — usually 1 in MODELO codes.
- 5 vs S, 6 vs G, 8 vs B — handwriting can blur these.
- Ç in modelos like BRAÇOS — keep the cedilla, do not drop it."""

_RULES_ACABAMENTO = """IMPORTANT RULES:
- Extract EVERY row that has OF, REFERÊNCIA / PEÇA, or QTD — do not skip any row.
- COLUMN ALIGNMENT (critical): the table has exactly 3 columns: OF,
  REFERÊNCIA / PEÇA, QTD. Emit one value per column using the JSON keys
  `of`, `modelo`, `qtd`. Store REFERÊNCIA / PEÇA internally as `modelo`.
- OF is EXACTLY 6 digits. If a value is not 6 digits, re-check the physical
  alignment before placing it in OF.
- REFERÊNCIA / PEÇA may be written across 2 lines — join them with a space.
- TURNO is one of: M, R, XM, T (the marked checkbox). Empty string if none.
- TOTAL QTD is the footer total; store it as `footer.colunas_produzidas`.
- If a field is empty or not visible, use empty string "".
- Do NOT invent values. If a digit is unclear, copy your best reading.
- Normalize date to DD-MM-YYYY format.

WATCH OUT FOR HANDWRITING CONFUSIONS:
- 0 (zero) vs O (letter) — piece references often use digits where they look like 0.
- 1 (one) vs I (letter) vs L (letter) — usually 1 in piece references.
- 5 vs S, 6 vs G, 8 vs B — handwriting can blur these.
- Ç in references like BRAÇOS — keep the cedilla, do not drop it."""

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
- SUCATA is the number of scrapped/rejected pieces (a small integer); usually
  blank — use "" when empty.
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
    if template.name == "acabamento":
        # R139 — o template `acabamento` cobre MTG2/3/4/5: não fixar um sub-setor
        # no prompt (a regressão do Codex fixava "ACABAMENTO MTG4"). O setor real
        # é a leitura do Pass-1 (ver _merge_pass2_into_pass1).
        setor = "ACABAMENTO"
    cols_line = _columns_line(template)
    row_skel = _row_skeleton(template)
    footer_skel = _footer_skeleton(template)
    has_footer = bool(template.footer_fields)

    # Choose rules block by family
    if not template.has_production_rows:
        rules = _RULES_PARAGENS
        domain_hint = "DOWNTIME (paragens — registo de paragens de máquina)"
    elif template.name == "acabamento":
        rules = _RULES_ACABAMENTO
        domain_hint = "ACABAMENTO DE COLUNAS"
    elif template.is_gemini:
        rules = _RULES_GEMINI
        domain_hint = "NESTING DE CHAPA (Gemini — produção de cortes em chapa)"
    else:
        rules = _RULES_PRODUCTION
        domain_hint = "PRODUÇÃO DE COLUNAS"

    # Round 98 — few-shot block from the learning engine. Empty string when
    # the template has no learned examples, keeping the prompt unchanged.
    fewshot = _fewshot_block(template.name)

    # R132 — header_skel agora é gerado dinamicamente de
    # `template.header_fields` (pré-R132 estava hardcoded a 5 campos e o
    # `turno` definido em acabamento/maq_fustes nunca era pedido ao
    # Qwen). Ver `_header_skel` abaixo.
    header_skel = _header_skel(template, setor)

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
        f"3. FOOTER fields: {', '.join(_field_label(template, f) for f in template.footer_fields)}"
        if has_footer
        else "3. (This template has no footer — only header + rows.)"
    )

    return f"""You are an OCR and data extraction expert. Analyze this industrial Kanban production sheet image carefully.

This kanban is for {domain_hint}. Setor/Máquina = {setor}.

Extract ALL data and return ONLY a valid JSON object. No markdown, no <think> blocks, no explanation.

The sheet has:
1. HEADER fields: {_header_desc(template)}
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
ROBOT, EXPEDIÇÃO, ACABAMENTO MTG2, MÁQUINA DE FUSTES, GASPARINI, HPE32, HD36.

No markdown. No explanation. No <think> blocks."""


def build_side_detect_prompt() -> str:
    """R132 — Pass-1.5 mini OCR para kanbans 2-lados (TPL103 MÁQUINA DE
    FUSTES). Olha SÓ ao cabeçalho da tabela e decide se é frente (PRI/
    CLIENTE/OV/OF/MODELO) ou verso (MOTIVO DA PARAGEM/INÍCIO/FIM/...).

    Output minimal {"side": "F"} ou {"side": "V"} → latência ~3-5s vs
    ~22s de um full extraction. Usado por `_detect_side` em ocr_runner.
    """
    return """Look at the table header row of this industrial Kanban sheet.

Is the leftmost column header "PRI" (with CLIENTE, OV, OF, MODELO following)
OR "MOTIVO DA PARAGEM" (with INÍCIO, FIM, DURAÇÃO, RESOLVIDO following)?

Return ONLY a JSON object with one key:
{"side": "F"}   ← if the table is PRI/CLIENTE/OV/OF/MODELO (front, production)
{"side": "V"}   ← if the table is MOTIVO DA PARAGEM (back, downtimes)

No markdown, no <think>, no explanation."""


def canonical_field_labels() -> dict[str, str]:
    """Task C E4 — cópia dos labels impressos por campo canónico de linha.
    Usado pelo índice invertido label→campo no registo de templates."""
    return dict(_FIELD_LABELS)


def canonical_header_labels() -> dict[str, str]:
    """Task C E4 — cópia dos labels de cabeçalho canónicos."""
    return dict(_HEADER_LABELS)


def build_discovery_prompt() -> str:
    """Task C E4 — descoberta de campos de um template de kanban NOVO.

    Corre sobre a fotografia do template em branco (ou exemplar) carregada
    no wizard de registo (/admin, tab Kanbans). Pede APENAS os labels
    IMPRESSOS, organizados por secção — sem inventar campos e sem tentar
    ler valores manuscritos. O resultado alimenta o mapeamento
    label→campo canónico (parse_discovery_response em ocr_runner) e é
    sempre validado por um humano antes de o template poder ser ativado.
    """
    return """You are analysing the LAYOUT of an industrial Kanban production sheet template.

Do NOT read handwritten values. Only report the PRINTED field labels of the form itself.

Identify:
1. header: labelled boxes at the top (e.g. Operador, N°, Setor/Maquina, Data, Turno)
2. columns: the table's printed column headers, LEFT TO RIGHT, exactly as printed
3. footer: labelled totals below the table (e.g. Colunas Produzidas, Horas Trabalhadas)
4. title: the sheet's printed title/code if any (e.g. "TPL103 - PRODUÇÃO COLUNAS")

Return ONLY a JSON object with exactly these keys:
{
  "title": "printed title or empty string",
  "header": ["label 1", "label 2"],
  "columns": ["COL 1", "COL 2"],
  "footer": ["label 1"]
}

Rules:
- Copy labels EXACTLY as printed (accents included). Do not translate.
- Do not invent fields that are not printed on the sheet.
- If a section does not exist, return an empty list for it.
- No markdown, no <think>, no explanation."""


__all__ = [
    "build_prompt",
    "build_setor_only_prompt",
    "build_side_detect_prompt",
    "build_discovery_prompt",
    "canonical_field_labels",
    "canonical_header_labels",
]

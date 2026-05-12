"""Round 54 — Multi-template registry (11 kanban schemas).

The factory uses 11 different kanban layouts across 7 production phases.
This module is the single source of truth for which fields each template
has, how to detect a template from the OCR'd setor/máquina, and which
fields participate in DQ / cross-check.

Templates fall into 3 families:

| Family | Templates | Common schema |
|---|---|---|
| TPL103 Produção Colunas | 10 variants | `pri, cliente, ov, of, modelo` + variable tail |
| TPL103 Paragens | quinadora_pav4_paragens | `motivo, inicio, fim, duracao, resolvido` |
| TPL102 Produção Gemini | gasparini, hpe32, hd36 | `pf, cliente, of, modelo, cf, m2, qtd, nesting, inicio, fim, np` |

Phase mapping (matches dashboard R29 sector flow):

| Phase           | Templates                                  |
|-----------------|--------------------------------------------|
| Bobine Formato  | bobine_formato                             |
| Corte           | guilhotina, linha_corte                    |
| Corte térmico   | gasparini, hpe32, hd36                     |
| Quinagem        | quinadora_pav4_paragens, quinadora_pav8,   |
|                 | guifil                                     |
| Soldadura       | soldline, laser                            |
| Manual / Robot  | manual, robot                              |
| Expedição       | expedicao                                  |

Public API:
    TEMPLATES: dict[str, TemplateSpec]   # 11 entries keyed by canonical name
    detect_template(setor, *, tpl_code=None) -> TemplateSpec
    DEFAULT_TEMPLATE: TemplateSpec       # bobine_formato (used as fallback)

Field semantics:
    row_fields           — ordered tuple of column names (matches kanban left-to-right)
    cross_check_fields   — subset that can be validated vs plan_colunas / SAP
    has_production_rows  — False for paragens (downtime sheets)
    is_gemini            — True for TPL102 (different domain: chapa nesting)
    csv_block_label      — heading for the "tabela" block in the deposit CSV

Backward compat:
    Legacy sheets (no template_name in sheet_data) infer 'bobine_formato'
    via detect_template() when header.setor_maquina matches BOBINE-FORMATO
    aliases. Anything that doesn't match falls back to bobine_formato so
    the 65 existing sheets continue to render unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


@dataclass(frozen=True)
class TemplateSpec:
    """Schema for one kanban template.

    Frozen so it can be safely shared across requests / threads. Use
    `TEMPLATES[name]` to fetch; never mutate.
    """

    # Canonical identifier (snake_case). Persisted in sheet_data.template_name.
    name: str

    # Family code: "TPL102" (Gemini nesting) or "TPL103" (Produção Colunas).
    tpl_code: str

    # Production phase from dashboard R29 sector flow.
    phase: str

    # All known operator-written variants of the setor/máquina label.
    # First entry is the canonical one shown in the UI.
    setor_aliases: tuple[str, ...]

    # Column names for each row in the kanban table, in display order.
    row_fields: tuple[str, ...]

    # Footer field names. Most templates use ("colunas_produzidas",
    # "horas_trabalhadas"); paragens has none.
    footer_fields: tuple[str, ...] = (
        "colunas_produzidas",
        "horas_trabalhadas",
    )

    # False for paragens (PAV.4) — no rows, just downtime entries.
    has_production_rows: bool = True

    # True for TPL102 Gemini (chapa nesting, not column production).
    is_gemini: bool = False

    # Subset of row_fields that can be cross-checked against refs.
    # Empty for paragens (no plan to compare against).
    cross_check_fields: tuple[str, ...] = ()

    # Header fields specific to this template. All 11 share the same
    # 5-field header (operador, n_operador, setor_maquina, cod_maquina,
    # data) so this is constant; kept as a field for future flexibility.
    # cod_maquina (e.g. "M032") was added for the CPIS migration export —
    # legacy sheets without it still parse (Pydantic Header default "").
    header_fields: tuple[str, ...] = (
        "operador",
        "n_operador",
        "setor_maquina",
        "cod_maquina",
        "data",
    )

    # Label for the production block in the deposit CSV. Different
    # templates may want different labels (e.g. "TABELA DE PARAGENS"
    # for downtime, "TABELA DE NESTING" for Gemini).
    csv_block_label: str = "TABELA DE PRODUÇÃO"

    # Free-form description shown in UI tooltips / queue badges.
    description: str = ""


# ============================================================================
#  TPL103 — Produção Colunas (10 variants)
# ============================================================================

# Common header / footer for all TPL103 production templates
_TPL103_HEADER = ("operador", "n_operador", "setor_maquina", "cod_maquina", "data")
_TPL103_FOOTER = ("colunas_produzidas", "horas_trabalhadas")

# Bobine-Formato — the original supported template (13 row fields)
_BOBINE_FORMATO = TemplateSpec(
    name="bobine_formato",
    tpl_code="TPL103",
    phase="Bobine Formato",
    setor_aliases=("BOBINE-FORMATO", "BOBINE FORMATO", "BOBINE/FORMATO"),
    row_fields=(
        "pri", "cliente", "ov", "of", "modelo",
        "qtd", "comp_mm", "larg_mm", "lote", "coni",
        "esp", "lbase", "ltopo",
    ),
    cross_check_fields=(
        "cliente", "ov", "of", "modelo",
        "comp_mm", "larg_mm", "lote", "esp",
        "lbase", "ltopo",
    ),
    description="Corte / formato de bobine. Schema canónico com 13 colunas.",
)

# Guilhotina — Máquina de corte (no LARG_MM, no LOTE)
_GUILHOTINA = TemplateSpec(
    name="guilhotina",
    tpl_code="TPL103",
    phase="Corte",
    setor_aliases=("GUILHOTINA",),
    row_fields=(
        "pri", "cliente", "ov", "of", "modelo",
        "qtd", "comp_mm", "coni", "esp", "lbase", "ltopo",
    ),
    cross_check_fields=(
        "cliente", "ov", "of", "modelo",
        "comp_mm", "esp", "lbase", "ltopo",
    ),
    description="Máquina de corte (guilhotina). Sem LARG nem LOTE.",
)

# Linha de Corte — same fields as Guilhotina but no COMP
_LINHA_CORTE = TemplateSpec(
    name="linha_corte",
    tpl_code="TPL103",
    phase="Corte",
    setor_aliases=("LINHA DE CORTE", "LINHA CORTE"),
    row_fields=(
        "pri", "cliente", "ov", "of", "modelo",
        "qtd", "coni", "esp", "lbase", "ltopo",
    ),
    cross_check_fields=(
        "cliente", "ov", "of", "modelo",
        "esp", "lbase", "ltopo",
    ),
    description="Máquina de corte (linha). Sem COMP nem LARG.",
)

# Quinadora PAV.8 — produção, tail é apenas QTD
_QUINADORA_PAV8 = TemplateSpec(
    name="quinadora_pav8",
    tpl_code="TPL103",
    phase="Quinagem",
    setor_aliases=("QUINADORA PAV.8", "QUINADORA PAV 8", "QUINADORA PAV8"),
    row_fields=(
        "pri", "cliente", "ov", "of", "modelo", "qtd",
    ),
    cross_check_fields=(
        "cliente", "ov", "of", "modelo",
    ),
    csv_block_label="TABELA DE EXPEDIÇÃO",
    description="Quinadora PAV.8 (colunas expedidas).",
)

# Guifil (Quinadora) — same as Bobine minus QTD/COMP/LARG/LOTE
_GUIFIL = TemplateSpec(
    name="guifil",
    tpl_code="TPL103",
    phase="Quinagem",
    setor_aliases=("GUIFIL", "QUINADORA GUIFIL"),
    row_fields=(
        "pri", "cliente", "ov", "of", "modelo",
        "qtd", "coni", "esp", "lbase", "ltopo",
    ),
    cross_check_fields=(
        "cliente", "ov", "of", "modelo",
        "esp", "lbase", "ltopo",
    ),
    description="Quinadora Guifil. CONI/ESP/LBASE/LTOPO sem COMP/LARG.",
)

# Soldline 4 — Máquina de soldadura (QTD colunas + QTD metros)
_SOLDLINE = TemplateSpec(
    name="soldline",
    tpl_code="TPL103",
    phase="Soldadura",
    setor_aliases=("SOLDLINE 4", "SOLDLINE", "SOLDLINE4"),
    row_fields=(
        "pri", "cliente", "ov", "of", "modelo",
        "qtd", "qtd_metros",
    ),
    cross_check_fields=(
        "cliente", "ov", "of", "modelo",
    ),
    description="Máquina de soldadura Soldline 4. QTD em colunas e em metros.",
)

# Laser — Soldadura laser (QTD col + QTD metros + LBASE/LTOPO)
_LASER = TemplateSpec(
    name="laser",
    tpl_code="TPL103",
    phase="Soldadura",
    setor_aliases=("LASER", "SOLDADURA LASER"),
    row_fields=(
        "pri", "cliente", "ov", "of", "modelo",
        "qtd", "qtd_metros", "lbase", "ltopo",
    ),
    cross_check_fields=(
        "cliente", "ov", "of", "modelo",
        "lbase", "ltopo",
    ),
    description="Soldadura laser. QTD colunas/metros + LBASE/LTOPO.",
)

# Manual — Produção manual (QTD + SOBRAS)
_MANUAL = TemplateSpec(
    name="manual",
    tpl_code="TPL103",
    phase="Manual",
    setor_aliases=("MANUAL",),
    row_fields=(
        "pri", "cliente", "ov", "of", "modelo",
        "qtd", "sobras",
    ),
    cross_check_fields=(
        "cliente", "ov", "of", "modelo",
    ),
    description="Produção manual. QTD colunas + SOBRAS.",
)

# Robot — Robot Colunas (mesmo schema que Manual)
_ROBOT = TemplateSpec(
    name="robot",
    tpl_code="TPL103",
    phase="Robot",
    setor_aliases=("ROBOT", "ROBOT COLUNAS"),
    row_fields=(
        "pri", "cliente", "ov", "of", "modelo",
        "qtd", "sobras",
    ),
    cross_check_fields=(
        "cliente", "ov", "of", "modelo",
    ),
    description="Robot de produção de colunas. QTD + SOBRAS.",
)

# Expedição — QTD + CESTA Nº
_EXPEDICAO = TemplateSpec(
    name="expedicao",
    tpl_code="TPL103",
    phase="Expedição",
    setor_aliases=("EXPEDIÇÃO", "EXPEDICAO", "EXPEDIÇAO"),
    row_fields=(
        "pri", "cliente", "ov", "of", "modelo",
        "qtd", "cesta_n",
    ),
    cross_check_fields=(
        "cliente", "ov", "of", "modelo",
    ),
    csv_block_label="TABELA DE EXPEDIÇÃO",
    description="Expedição. QTD colunas + CESTA Nº.",
)

# ============================================================================
#  TPL103 — Paragens (não-produção)
# ============================================================================

# Quinadora PAV.4 — Paragens (motivo / início / fim / duração / resolvido)
_QUINADORA_PAV4_PARAGENS = TemplateSpec(
    name="quinadora_pav4_paragens",
    tpl_code="TPL103",
    phase="Quinagem",
    setor_aliases=("QUINADORA PAV.4", "QUINADORA PAV 4", "QUINADORA PAV4"),
    row_fields=(
        "motivo", "inicio", "fim", "duracao", "resolvido",
    ),
    footer_fields=(),
    has_production_rows=False,
    cross_check_fields=(),
    csv_block_label="TABELA DE PARAGENS",
    description="Quinadora PAV.4 paragens (downtime tracking). Sem produção.",
)

# ============================================================================
#  TPL102 — Produção Gemini (corte térmico, nesting de chapa)
# ============================================================================

# Schema partilhado pelas 3 máquinas de corte térmico
_GEMINI_ROW_FIELDS = (
    "pf", "cliente", "of", "modelo", "cf",
    "m2", "qtd", "nesting", "inicio", "fim", "np",
)
_GEMINI_FOOTER = ("horas_trabalhadas",)
_GEMINI_CROSS_CHECK = ("cliente", "of", "modelo")  # nesting domain — limited refs

_GASPARINI = TemplateSpec(
    name="gasparini",
    tpl_code="TPL102",
    phase="Corte térmico",
    setor_aliases=("GASPARINI",),
    row_fields=_GEMINI_ROW_FIELDS,
    footer_fields=_GEMINI_FOOTER,
    is_gemini=True,
    cross_check_fields=_GEMINI_CROSS_CHECK,
    csv_block_label="TABELA DE NESTING",
    description="Corte térmico Gasparini (nesting de chapa).",
)

_HPE32 = TemplateSpec(
    name="hpe32",
    tpl_code="TPL102",
    phase="Corte térmico",
    setor_aliases=("HPE32", "HPE 32"),
    row_fields=_GEMINI_ROW_FIELDS,
    footer_fields=_GEMINI_FOOTER,
    is_gemini=True,
    cross_check_fields=_GEMINI_CROSS_CHECK,
    csv_block_label="TABELA DE NESTING",
    description="Corte térmico HPE32 (nesting de chapa).",
)

_HD36 = TemplateSpec(
    name="hd36",
    tpl_code="TPL102",
    phase="Corte térmico",
    setor_aliases=("HD36", "HD 36"),
    row_fields=_GEMINI_ROW_FIELDS,
    footer_fields=_GEMINI_FOOTER,
    is_gemini=True,
    cross_check_fields=_GEMINI_CROSS_CHECK,
    csv_block_label="TABELA DE NESTING",
    description="Corte térmico HD36 (nesting de chapa).",
)


# ============================================================================
#  Registry (keyed by canonical name)
# ============================================================================

TEMPLATES: dict[str, TemplateSpec] = {
    t.name: t
    for t in (
        _BOBINE_FORMATO,
        _GUILHOTINA,
        _LINHA_CORTE,
        _QUINADORA_PAV8,
        _QUINADORA_PAV4_PARAGENS,
        _GUIFIL,
        _SOLDLINE,
        _LASER,
        _MANUAL,
        _ROBOT,
        _EXPEDICAO,
        _GASPARINI,
        _HPE32,
        _HD36,
    )
}

# Default template — used for legacy sheets that don't have template_name
# in sheet_data, and as fallback when detect_template() can't match.
DEFAULT_TEMPLATE: TemplateSpec = _BOBINE_FORMATO


# ============================================================================
#  Detection
# ============================================================================

# Pre-built reverse index: setor alias (uppercased, stripped) → template name.
# Order matters: more specific aliases must be checked first when there's
# overlap (e.g. "QUINADORA PAV.4" vs "QUINADORA PAV.8" — both contain
# "QUINADORA"). The dict iteration order matches insertion in TEMPLATES.
_ALIAS_INDEX: dict[str, str] = {}
for _t in TEMPLATES.values():
    for _alias in _t.setor_aliases:
        _ALIAS_INDEX[_alias.upper().strip()] = _t.name


def _normalize_setor(raw: str) -> str:
    """Normalize a setor/máquina string for alias lookup.

    Uppercases, strips whitespace, collapses internal whitespace runs.
    Does NOT change punctuation (PAV.4 / PAV 4 are both valid aliases).
    """
    if not raw:
        return ""
    s = " ".join(raw.split())  # collapse whitespace
    return s.upper().strip()


def detect_template(
    setor_maquina: str | None,
    *,
    tpl_code: str | None = None,
) -> TemplateSpec:
    """Match a setor/máquina label to a TemplateSpec.

    Strategy (in order):
    1. Exact match against all known aliases (case-insensitive, whitespace-normalized)
    2. Substring match — alias appears as a token inside the input
       (handles operator notation like "BOBINE-FORMATO M032")
    3. If `tpl_code` is "TPL102" and nothing matched, return GASPARINI
       (3 Gemini machines share the same schema)
    4. Fallback to `DEFAULT_TEMPLATE` (bobine_formato)

    Backward compat: legacy sheets where setor_maquina is "BOBINE-FORMATO"
    return DEFAULT_TEMPLATE directly via step 1.

    Args:
        setor_maquina: raw operator-written setor string from OCR
        tpl_code: optional hint ("TPL102" or "TPL103") — used only when
            setor lookup fails

    Returns:
        TemplateSpec — never None, fallback to DEFAULT_TEMPLATE
    """
    if not setor_maquina:
        return DEFAULT_TEMPLATE

    norm = _normalize_setor(setor_maquina)
    if not norm:
        return DEFAULT_TEMPLATE

    # Step 1 — exact alias match
    name = _ALIAS_INDEX.get(norm)
    if name is not None:
        return TEMPLATES[name]

    # Step 2 — substring match (alias is a contiguous token in input)
    # Sorted by alias length DESC so "QUINADORA PAV.4" matches before "QUINADORA".
    for alias_up, tname in sorted(
        _ALIAS_INDEX.items(), key=lambda kv: -len(kv[0])
    ):
        if alias_up in norm:
            return TEMPLATES[tname]

    # Step 3 — TPL102 hint fallback (3 Gemini share schema)
    if tpl_code and tpl_code.upper() == "TPL102":
        return TEMPLATES["gasparini"]

    # Step 4 — fallback (legacy / unknown sheet)
    return DEFAULT_TEMPLATE


def get_template(name: str | None) -> TemplateSpec:
    """Fetch a template by canonical name, fallback to DEFAULT_TEMPLATE.

    Use this in sheet rendering where `sheet_data.template_name` was
    persisted and you need the spec back. Unknown names (typos, deleted
    templates) silently fall back so the UI never breaks.
    """
    if not name:
        return DEFAULT_TEMPLATE
    return TEMPLATES.get(name, DEFAULT_TEMPLATE)


def all_phases() -> tuple[str, ...]:
    """Ordered unique phases across the registry (for dashboard headers)."""
    seen: list[str] = []
    for t in TEMPLATES.values():
        if t.phase not in seen:
            seen.append(t.phase)
    return tuple(seen)


def templates_in_phase(phase: str) -> tuple[TemplateSpec, ...]:
    """Return all TemplateSpec entries belonging to a production phase."""
    return tuple(t for t in TEMPLATES.values() if t.phase == phase)


__all__ = [
    "TemplateSpec",
    "TEMPLATES",
    "DEFAULT_TEMPLATE",
    "detect_template",
    "get_template",
    "all_phases",
    "templates_in_phase",
]

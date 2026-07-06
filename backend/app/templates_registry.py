"""Round 54 — Multi-template registry (11 kanban schemas).

The factory uses 11 different kanban layouts across 7 production phases.
This module is the single source of truth for which fields each template
has, how to detect a template from the OCR'd setor/máquina, and which
fields participate in DQ / cross-check.

Templates fall into 2 families:

| Family | Templates | Common schema |
|---|---|---|
| TPL103 Produção Colunas | 11 variants | `pri, cliente, ov, of, modelo` + variable tail |
| TPL102 Produção Gemini | gasparini, hpe32, hd36 | `pf, cliente, of, modelo, cf, m2, qtd, nesting, inicio, fim, np` |

Phase mapping (matches dashboard R29 sector flow). R68: phases
re-aligned with user's verbatim distribution — Gemini machines merged
into "Corte"; "Quinagem" renamed to "Quinadora"; "Manual" + "Robot"
merged into "Abertura Portinholas".

| Phase                  | Templates                                  |
|------------------------|--------------------------------------------|
| Bobine Formato         | bobine_formato                             |
| Corte                  | guilhotina, linha_corte, gasparini,        |
|                        | hpe32, hd36                                |
| Quinadora              | quinadora_pav4, quinadora_pav8, guifil     |
| Soldadura              | soldline, laser                            |
| Abertura Portinholas   | manual, robot                              |
| Acabamento             | acabamento                                 |
| Expedição              | expedicao                                  |

Public API:
    TEMPLATES: dict[str, TemplateSpec]   # 11 entries keyed by canonical name
    detect_template(setor, *, tpl_code=None, cod_maquina=None) -> TemplateSpec
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

import re
import unicodedata
from dataclasses import dataclass, field as dc_field
from difflib import SequenceMatcher


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

    # Header fields shared by every template. rev00 (13/04/2026) added the
    # TURNO checkbox (M/R/XM/T) to ALL kanban headers, so `turno` is now part
    # of the default header. Templates that predate this (acabamento,
    # maq_fustes) already listed it explicitly — harmless overlap.
    # cod_maquina (e.g. "M032") was added for the CPIS migration export —
    # legacy sheets without it still parse (Pydantic Header default "").
    # NOTE: putting `turno` here changes the OCR prompt for every template —
    # gated behind a golden-set A/B before deploy (see plan / CLAUDE.md).
    header_fields: tuple[str, ...] = (
        "operador",
        "n_operador",
        "setor_maquina",
        "cod_maquina",
        "data",
        "turno",
    )

    # Label for the production block in the deposit CSV. Different
    # templates may want different labels (e.g. "TABELA DE PARAGENS"
    # for downtime, "TABELA DE NESTING" for Gemini).
    csv_block_label: str = "TABELA DE PRODUÇÃO"

    # Free-form description shown in UI tooltips / queue badges.
    description: str = ""

    # Per-template override do label de uma coluna (no prompt, CSV, UI).
    # Default `{}` mantém o comportamento global (_FIELD_LABELS em
    # prompt_builder.py). Acabamento TPL086 usa para mostrar
    # "REFERÊNCIA / PEÇA" sobre o campo interno `modelo`.
    field_labels: dict[str, str] = dc_field(default_factory=dict)


# ============================================================================
#  TPL103 — Produção Colunas (11 variants)
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
        "esp", "lbase", "ltopo", "sucata",
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
    setor_aliases=("GUILHOTINA 6M", "GUILHOTINA"),
    row_fields=(
        "pri", "cliente", "ov", "of", "modelo",
        "qtd", "comp_mm", "coni", "esp", "lbase", "ltopo", "sucata",
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
        "qtd", "coni", "esp", "lbase", "ltopo", "sucata",
    ),
    cross_check_fields=(
        "cliente", "ov", "of", "modelo",
        "esp", "lbase", "ltopo",
    ),
    description="Máquina de corte (linha). Sem COMP nem LARG.",
)

# Quinadora PAV.8 — produção, tail é apenas QTD
# R126: alargado para alinhar com guifil — pav4/pav8 e guifil processam o
# mesmo tipo de peça e o plano tem coni/esp/lbase/ltopo na linha. Antes só
# tinha cliente/ov/of/modelo validáveis → cobertura de cross-check fraca.
_QUINADORA_PAV8 = TemplateSpec(
    name="quinadora_pav8",
    tpl_code="TPL103",
    phase="Quinadora",
    setor_aliases=("QUINADORA PAV.8", "QUINADORA PAV 8", "QUINADORA PAV8"),
    # Coexistência: o papel rev00 da PAV só tem QTD+SUCATA, mas mantemos as
    # dimensões (coni/esp/lbase/ltopo) para as folhas do formato antigo ainda
    # em circulação lerem; num papel novo essas colunas voltam "".
    row_fields=(
        "pri", "cliente", "ov", "of", "modelo",
        "qtd", "coni", "esp", "lbase", "ltopo", "sucata",
    ),
    cross_check_fields=(
        "cliente", "ov", "of", "modelo",
        "esp", "lbase", "ltopo",
    ),
    csv_block_label="TABELA DE EXPEDIÇÃO",
    description="Quinadora PAV.8 (colunas expedidas).",
)

# Guifil (Quinadora) — same as Bobine minus QTD/COMP/LARG/LOTE
_GUIFIL = TemplateSpec(
    name="guifil",
    tpl_code="TPL103",
    phase="Quinadora",
    setor_aliases=("GUIFIL", "QUINADORA GUIFIL"),
    row_fields=(
        "pri", "cliente", "ov", "of", "modelo",
        "qtd", "coni", "esp", "lbase", "ltopo", "sucata",
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
        "qtd", "qtd_metros", "sucata",
    ),
    cross_check_fields=(
        "cliente", "ov", "of", "modelo",
    ),
    description="Máquina de soldadura Soldline 4. QTD em colunas e em metros.",
)

# Laser — Soldadura laser (QTD col + QTD metros + DBASE/DTOPO).
# R128: campos DBASE/DTOPO são distintos de LBASE/LTOPO (são as dimensões
# próprias do laser — colunas dbase/dtopo no plan_colunas_cpis.xlsx).
_LASER = TemplateSpec(
    name="laser",
    tpl_code="TPL103",
    phase="Soldadura",
    setor_aliases=("LASER MTG2", "LASER", "SOLDADURA LASER"),
    row_fields=(
        "pri", "cliente", "ov", "of", "modelo",
        "qtd", "qtd_metros", "dbase", "dtopo", "sucata",
    ),
    cross_check_fields=(
        "cliente", "ov", "of", "modelo",
        "dbase", "dtopo",
    ),
    description="Soldadura laser. QTD colunas/metros + DBASE/DTOPO.",
)

# Manual — Produção manual (QTD + SOBRAS)
_MANUAL = TemplateSpec(
    name="manual",
    tpl_code="TPL103",
    phase="Abertura Portinholas",
    setor_aliases=("MANUAL",),
    row_fields=(
        "pri", "cliente", "ov", "of", "modelo",
        "qtd", "sobras", "sucata",
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
    phase="Abertura Portinholas",
    setor_aliases=("ROBOT MTG2", "ROBOT", "ROBOT COLUNAS"),
    row_fields=(
        "pri", "cliente", "ov", "of", "modelo",
        "qtd", "sobras", "sucata",
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
        "qtd", "cesta_n", "sucata",
    ),
    cross_check_fields=(
        "cliente", "ov", "of", "modelo",
    ),
    csv_block_label="TABELA DE EXPEDIÇÃO",
    description="Expedição. QTD colunas + CESTA Nº.",
)

# Quinadora PAV.4 — produção de colunas. R123: estava erradamente definido
# como tabela de paragens (downtime); a folha real é produção normal de
# colunas, com o mesmo schema do PAV.8.
# R126: alargado para coni/esp/lbase/ltopo, idêntico a pav8 e guifil.
_QUINADORA_PAV4 = TemplateSpec(
    name="quinadora_pav4",
    tpl_code="TPL103",
    phase="Quinadora",
    # R138 — labels do maquinas.xlsx que carregam "P4" (Pavilhão 4). As que
    # não têm Pavilhão explícito caem no fallback genérico de detect_template
    # (→ pav8). pav4/pav8 partilham schema, por isso a escolha só afecta o badge.
    setor_aliases=(
        "QUINADORA PAV.4", "QUINADORA PAV 4", "QUINADORA PAV4",
        "QUINADORA P4", "QUINADORA ADIRA 14M P4",
        "QUINADORA PUMA", "QUINADORA PUMA P4",
    ),
    row_fields=(
        "pri", "cliente", "ov", "of", "modelo",
        "qtd", "coni", "esp", "lbase", "ltopo", "sucata",
    ),
    cross_check_fields=(
        "cliente", "ov", "of", "modelo",
        "esp", "lbase", "ltopo",
    ),
    csv_block_label="TABELA DE EXPEDIÇÃO",
    description="Quinadora PAV.4 (produção de colunas).",
)

# Acabamento — kanban TPL086, formato MTG4 rev01. O mesmo layout passa a
# aplicar-se a todos os acabamentos (MTG2/3/4/5): cabeçalho com TURNO,
# tabela OF / REFERÊNCIA-PEÇA / QTD e rodapé TOTAL QTD.
_ACABAMENTO = TemplateSpec(
    name="acabamento",
    tpl_code="TPL086",
    phase="Acabamento",
    setor_aliases=(
        "ACABAMENTO MTG4", "ACABAMENTO MTG 4", "ACAB. MTG4", "ACAB MTG4",
        "ACABAMENTO MTG2", "ACABAMENTO MTG 2", "ACAB. MTG2", "ACAB MTG2",
        "ACABAMENTO MTG3", "ACABAMENTO MTG 3", "ACAB. MTG3", "ACAB MTG3",
        "ACABAMENTO MTG5", "ACABAMENTO MTG 5", "ACAB. MTG5", "ACAB MTG5",
        "ACABAMENTO",
    ),
    row_fields=(
        "of", "modelo", "qtd",
    ),
    cross_check_fields=(
        "of", "modelo",
    ),
    header_fields=(
        "operador", "n_operador", "setor_maquina", "cod_maquina",
        "data", "turno",
    ),
    footer_fields=("colunas_produzidas",),
    csv_block_label="TABELA DE ACABAMENTO",
    field_labels={
        "modelo": "REFERÊNCIA / PEÇA",
        "colunas_produzidas": "TOTAL QTD",
    },
    description="Acabamento. OF/REFERÊNCIA-PEÇA/QTD; TURNO M/R/XM/T.",
)


# R132 — TPL103 MÁQUINA DE FUSTES (frente: produção colunas). Header com
# TURNO (M/R/XM/T), como Acabamento. Tabela com QTD Colunas + QTD
# Metros (qtd_metros já tem coluna em production_rows desde R97). Setor
# já mapeia para cod M031 via maquinas.xlsx (codsec S004).
_MAQUINA_FUSTES = TemplateSpec(
    name="maq_fustes",
    tpl_code="TPL103",
    phase="Produção Fustes",
    setor_aliases=("MÁQUINA DE FUSTES", "MAQUINA DE FUSTES",
                   "MAQ DE FUSTES", "MAQ FUSTES"),
    row_fields=(
        "pri", "cliente", "ov", "of", "modelo", "qtd", "qtd_metros", "sucata",
    ),
    cross_check_fields=("cliente", "ov", "of", "modelo"),
    header_fields=(
        "operador", "n_operador", "setor_maquina", "cod_maquina",
        "data", "turno",
    ),
    footer_fields=("colunas_produzidas", "horas_trabalhadas"),
    csv_block_label="TABELA DE PRODUÇÃO",
    description="TPL103 frente — Máquina de Fustes, PRI/CLI/OV/OF/MOD/QTD/QTDm + TURNO.",
)


# R132 — TPL103 MÁQUINA DE FUSTES verso (paragens). Mesmo header da frente,
# tabela motivo/inicio/fim/duracao/resolvido. Sem footer. `has_production_rows
# = False` activa `_RULES_PARAGENS` no prompt_builder. `setor_aliases=()`
# para garantir que `detect_template("MÁQUINA DE FUSTES")` devolve sempre
# a FRENTE; o verso é escolhido apenas via side-detect Pass-1.5 em ocr_runner.
_MAQUINA_FUSTES_PARAGENS = TemplateSpec(
    name="maq_fustes_paragens",
    tpl_code="TPL103",
    phase="Paragens Fustes",
    setor_aliases=(),
    row_fields=(
        "motivo", "inicio", "fim", "duracao", "resolvido",
    ),
    cross_check_fields=(),
    header_fields=(
        "operador", "n_operador", "setor_maquina", "cod_maquina",
        "data", "turno",
    ),
    footer_fields=(),
    has_production_rows=False,
    csv_block_label="TABELA DE PARAGENS",
    description="TPL103 verso — Máquina de Fustes paragens (motivo/início/fim/duração/resolvido).",
)


# rev00 (13/04/2026) — TODAS as folhas passaram a ter um verso de paragens
# idêntico. Este template genérico serve o verso de QUALQUER máquina. Como o
# `setor_maquina` é lido em ambas as páginas (preservado pelo merge Pass-2), a
# identidade da máquina fica no header — não é preciso um paragens por máquina.
# `setor_aliases=()` → nunca é auto-detectado; só é escolhido pela pista de
# página (captura guiada) ou pelo side-detect (ver ocr_runner.TWO_SIDED_TEMPLATES).
# `_MAQUINA_FUSTES_PARAGENS` mantém-se registado para folhas já persistidas.
_PARAGENS = TemplateSpec(
    name="paragens",
    tpl_code="TPL103",
    phase="Paragens",
    setor_aliases=(),
    row_fields=(
        "motivo", "inicio", "fim", "duracao", "resolvido",
    ),
    cross_check_fields=(),
    header_fields=(
        "operador", "n_operador", "setor_maquina", "cod_maquina",
        "data", "turno",
    ),
    footer_fields=(),
    has_production_rows=False,
    csv_block_label="TABELA DE PARAGENS",
    description="Verso genérico rev00 — paragens (motivo/início/fim/duração/resolvido).",
)


# ============================================================================
#  TPL102 — Produção Gemini (corte térmico, nesting de chapa)
# ============================================================================

# Schema partilhado pelas 3 máquinas de corte térmico
_GEMINI_ROW_FIELDS = (
    "pf", "cliente", "of", "modelo", "cf",
    "m2", "qtd", "nesting", "inicio", "fim", "np", "sucata",
)
_GEMINI_FOOTER = ("horas_trabalhadas",)
_GEMINI_CROSS_CHECK = ("cliente", "of", "modelo")  # nesting domain — limited refs

_GASPARINI = TemplateSpec(
    name="gasparini",
    tpl_code="TPL102",
    phase="Corte",
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
    phase="Corte",
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
    phase="Corte",
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
        _QUINADORA_PAV4,
        _GUIFIL,
        _SOLDLINE,
        _LASER,
        _MANUAL,
        _ROBOT,
        _EXPEDICAO,
        _ACABAMENTO,
        _MAQUINA_FUSTES,
        _MAQUINA_FUSTES_PARAGENS,
        _PARAGENS,
        _GASPARINI,
        _HPE32,
        _HD36,
    )
}

# Default template — used for legacy sheets that don't have template_name
# in sheet_data, and as fallback when detect_template() can't match.
DEFAULT_TEMPLATE: TemplateSpec = _BOBINE_FORMATO

_TEMPLATE_ALIASES = {
    # Compatibilidade com folhas já persistidas antes do novo formato MTG4.
    "acabamento_mtg2": "acabamento",
    "acabamento_mtg3": "acabamento",
    "acabamento_mtg4": "acabamento",
    "acabamento_mtg5": "acabamento",
}


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

    Uppercases, removes accents, turns punctuation into separators, and
    collapses internal whitespace runs.
    """
    if not raw:
        return ""
    decomposed = unicodedata.normalize("NFKD", str(raw))
    no_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    s = re.sub(r"[^A-Za-z0-9]+", " ", no_marks.upper())
    return " ".join(s.split()).strip()


_ALIAS_NORM_INDEX: dict[str, str] = {}
for _t in TEMPLATES.values():
    for _alias in _t.setor_aliases:
        _ALIAS_NORM_INDEX[_normalize_setor(_alias)] = _t.name


def _compact_setor(raw: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "", _normalize_setor(raw))


def _is_acabamento_token(token: str) -> bool:
    if len(token) < 5:
        return False
    cleaned = token.replace("0", "O")
    if cleaned.startswith(("ACAB", "ACAM")):
        return True
    return SequenceMatcher(None, cleaned, "ACABAMENTO").ratio() >= 0.72


def _looks_like_acabamento_setor(norm: str) -> bool:
    if not norm:
        return False
    tokens = norm.split()
    compact = "".join(tokens)
    if any(_is_acabamento_token(t) for t in tokens):
        return True
    return _is_acabamento_token(compact)


def _has_mtg_token(norm: str) -> bool:
    """True only when the setor is essentially a BARE MTG token (e.g. "MTG2",
    "MIG 3") with no machine base-name.

    rev00 added MTG suffixes to real machines (ROBOT MTG2, LASER MTG2). A
    substring MTG match would mis-route those to `acabamento` whenever the
    base token OCRs badly (e.g. "R0B0T MTG2") — the exact/fuzzy alias then
    fails and this fallback would fire. So we require the WHOLE compact setor
    to BE the token, not merely contain it; a garbled base falls through to
    cod_maquina / default instead of silently becoming acabamento.
    """
    if not norm:
        return False
    return _compact_setor(norm) in {
        "MTG2", "MTG3", "MTG4", "MTG5",
        "MIG2", "MIG3", "MIG4", "MIG5",
    }


_ACABAMENTO_COD_MAQUINAS = frozenset({
    "M001", "M054", "M055", "M061", "M106", "M107",
})


def _normalize_cod_maquina(value: str | None) -> str:
    compact = re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())
    m = re.fullmatch(r"M(\d{1,3})", compact)
    if not m:
        return compact
    return "M" + m.group(1).zfill(3)


def detect_template_with_reason(
    setor_maquina: str | None,
    *,
    tpl_code: str | None = None,
    cod_maquina: str | None = None,
) -> tuple[TemplateSpec, str]:
    """Return the detected template plus the rule that selected it."""
    norm = _normalize_setor(setor_maquina)
    selected: tuple[TemplateSpec, str] | None = None
    if norm:
        name = _ALIAS_NORM_INDEX.get(norm)
        if name is not None:
            selected = (TEMPLATES[name], "exact_alias")

        if selected is None:
            for alias_up, tname in sorted(
                _ALIAS_NORM_INDEX.items(), key=lambda kv: -len(kv[0])
            ):
                if alias_up and alias_up in norm:
                    selected = (TEMPLATES[tname], "fuzzy_alias")
                    break

    if selected is None and tpl_code and tpl_code.upper() == "TPL102":
        selected = (TEMPLATES["gasparini"], "fuzzy_alias")

    if selected is None and norm:
        if "QUINADORA" in norm:
            selected = (TEMPLATES["quinadora_pav8"], "fuzzy_alias")
        elif _looks_like_acabamento_setor(norm):
            selected = (TEMPLATES["acabamento"], "fuzzy_alias")
        elif _has_mtg_token(norm):
            selected = (TEMPLATES["acabamento"], "mtg_token")

    if selected is None:
        cod = _normalize_cod_maquina(cod_maquina)
        if cod in _ACABAMENTO_COD_MAQUINAS:
            selected = (TEMPLATES["acabamento"], "cod_maquina")

    return selected or (DEFAULT_TEMPLATE, "default")


def detect_template(
    setor_maquina: str | None,
    *,
    tpl_code: str | None = None,
    cod_maquina: str | None = None,
) -> TemplateSpec:
    """Match a setor/máquina label to a TemplateSpec.

    Strategy (in order):
    1. Exact match against all known aliases (case-insensitive, whitespace-normalized)
    2. Substring match — alias appears as a token inside the input
       (handles operator notation like "BOBINE-FORMATO M032")
    3. If `tpl_code` is "TPL102" and nothing matched, return GASPARINI
       (3 Gemini machines share the same schema)
    4. Use an unambiguous acabamento `cod_maquina` before default fallback
    5. Fallback to `DEFAULT_TEMPLATE` (bobine_formato)

    Backward compat: legacy sheets where setor_maquina is "BOBINE-FORMATO"
    return DEFAULT_TEMPLATE directly via step 1.

    Args:
        setor_maquina: raw operator-written setor string from OCR
        tpl_code: optional hint ("TPL102" or "TPL103") — used only when
            setor lookup fails
        cod_maquina: optional machine code from the kanban header. Only
            unambiguous Acabamento codes are used, and only before fallback.

    Returns:
        TemplateSpec — never None, fallback to DEFAULT_TEMPLATE
    """
    template, _reason = detect_template_with_reason(
        setor_maquina, tpl_code=tpl_code, cod_maquina=cod_maquina,
    )
    return template


def get_template(name: str | None) -> TemplateSpec:
    """Fetch a template by canonical name, fallback to DEFAULT_TEMPLATE.

    Use this in sheet rendering where `sheet_data.template_name` was
    persisted and you need the spec back. Unknown names (typos, deleted
    templates) silently fall back so the UI never breaks.
    """
    if not name:
        return DEFAULT_TEMPLATE
    canonical = _TEMPLATE_ALIASES.get(name, name)
    return TEMPLATES.get(canonical, DEFAULT_TEMPLATE)


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
    "detect_template_with_reason",
    "get_template",
    "all_phases",
    "templates_in_phase",
]

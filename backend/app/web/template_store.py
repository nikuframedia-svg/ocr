"""Task C E4 — ponte entre kanban_templates (DB) e o registry em memória.

Os templates registados no /admin (wizard de kanbans) vivem na tabela
``kanban_templates`` como spec_json (espelho de TemplateSpec). Este módulo
converte spec_json ↔ TemplateSpec e instala os ATIVOS no registry via
templates_registry.set_runtime_templates (mutação in-place — o hot path do
ocr_runner vê os templates novos sem restart).

Limite conhecido: o reload afeta apenas ESTE processo. O deploy da fábrica
corre uvicorn single-process (start.ps1), por isso chega; com multi-worker
seria preciso um sinal entre processos.

Chamar reload_registry() no startup (main.py) e após activate/deactivate/
delete de um template.
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata

from app.pipeline.prompt_builder import canonical_field_labels
from app.templates_registry import (
    TEMPLATES,
    DeclaredRef,
    TemplateSpec,
    set_runtime_templates,
)
from app.web import db

__all__ = [
    "CROSSABLE_FIELDS",
    "KNOWN_ROW_FIELDS",
    "map_label_to_field",
    "reload_registry",
    "spec_from_dict",
    "spec_to_dict",
    "suggest_spec_from_discovery",
    "unidade_for_template",
    "validate_spec_payload",
]

# Campos de linha validáveis contra o plano/StockSAP (subconjunto canónico
# — ver cross_check_fields dos builtins). Um spec DB só pode cruzar estes.
CROSSABLE_FIELDS = frozenset({
    "cliente", "ov", "of", "modelo", "comp_mm", "larg_mm", "lote",
    "esp", "lbase", "ltopo", "dbase", "dtopo",
})

# Campos canónicos conhecidos do pipeline (união dos row_fields builtin).
# Um campo fora desta lista é aceite como "custom" — extraído pelo OCR mas
# sem KPIs nem cross-check (aviso no wizard).
KNOWN_ROW_FIELDS = frozenset({
    "pri", "cliente", "ov", "of", "modelo", "qtd", "comp_mm", "larg_mm",
    "lote", "coni", "esp", "lbase", "ltopo", "sucata", "fecho", "qtd_metros",
    "sobras", "cesta_n", "dbase", "dtopo", "m2", "nesting", "inicio",
    "fim", "np", "pf", "cf", "motivo", "duracao", "resolvido",
})

_SNAKE_RE = re.compile(r"^[a-z][a-z0-9_]*$")

_DEFAULT_HEADER = (
    "operador", "n_operador", "setor_maquina", "cod_maquina", "data", "turno",
)
_DEFAULT_FOOTER = ("colunas_produzidas", "horas_trabalhadas")


def spec_to_dict(spec: TemplateSpec) -> dict:
    """TemplateSpec → dict JSON-serializável (para kanban_templates.spec_json)."""
    return {
        "name": spec.name,
        "tpl_code": spec.tpl_code,
        "phase": spec.phase,
        "setor_aliases": list(spec.setor_aliases),
        "row_fields": list(spec.row_fields),
        "footer_fields": list(spec.footer_fields),
        "has_production_rows": spec.has_production_rows,
        "is_gemini": spec.is_gemini,
        "cross_check_fields": list(spec.cross_check_fields),
        "header_fields": list(spec.header_fields),
        "csv_block_label": spec.csv_block_label,
        "description": spec.description,
        "field_labels": dict(spec.field_labels),
        "declared_cross": {
            f: {"ref": "plan", "column": r.column, "cmp": r.cmp,
                **({"tol": r.tol} if r.tol is not None else {}),
                **({"vote": True} if r.vote else {})}
            for f, r in spec.declared_cross.items()
        },
    }


def _declared_from_dict(raw) -> dict[str, DeclaredRef]:
    """spec_json["declared_cross"] → dict campo→DeclaredRef. TOLERANTE:
    entradas malformadas são descartadas (reload_registry nunca pode perder
    um template inteiro por uma chave acessória); a validação dura vive em
    validate_spec_payload, que guarda a porta de entrada do wizard."""
    out: dict[str, DeclaredRef] = {}
    if not isinstance(raw, dict):
        return out
    for field, cfg in raw.items():
        if not isinstance(cfg, dict):
            continue
        column = str(cfg.get("column") or "").strip().lower()
        cmp = str(cfg.get("cmp") or "text").strip()
        if not field or not column or cmp not in ("text", "num"):
            continue
        tol = None
        if cmp == "num" and cfg.get("tol") is not None:
            try:
                tol = float(cfg["tol"])
            except (TypeError, ValueError):
                continue
            if tol <= 0:
                continue
        # vote: parse ESTRITO-para-True — um spec_json malformado ("sim",
        # 1, "true") nunca liga um voto por engano; a direção segura do
        # descarte é "inerte". A validação dura vive em validate_spec_payload.
        out[str(field)] = DeclaredRef(
            column=column, cmp=cmp, tol=tol,
            vote=cfg.get("vote") is True,
        )
    return out


def spec_from_dict(
    d: dict,
    *,
    unidade_id: int | None = None,
    db_id: int | None = None,
) -> TemplateSpec:
    """dict (spec_json) → TemplateSpec runtime. Tolerante a chaves em falta
    (defaults do dataclass); levanta ValueError se faltar o essencial."""
    name = str(d.get("name") or "").strip()
    row_fields = tuple(str(f) for f in (d.get("row_fields") or []))
    if not name or not row_fields:
        raise ValueError("spec sem name ou row_fields")
    return TemplateSpec(
        name=name,
        tpl_code=str(d.get("tpl_code") or "TPL103"),
        phase=str(d.get("phase") or "Produção"),
        setor_aliases=tuple(str(a) for a in (d.get("setor_aliases") or [])),
        row_fields=row_fields,
        footer_fields=tuple(str(f) for f in d.get("footer_fields", _DEFAULT_FOOTER)),
        has_production_rows=bool(d.get("has_production_rows", True)),
        is_gemini=bool(d.get("is_gemini", False)),
        cross_check_fields=tuple(str(f) for f in (d.get("cross_check_fields") or [])),
        header_fields=tuple(str(f) for f in d.get("header_fields", _DEFAULT_HEADER)),
        csv_block_label=str(d.get("csv_block_label") or "TABELA DE PRODUÇÃO"),
        description=str(d.get("description") or ""),
        field_labels={str(k): str(v) for k, v in (d.get("field_labels") or {}).items()},
        declared_cross=_declared_from_dict(d.get("declared_cross")),
        source="db",
        unidade_id=unidade_id,
        db_id=db_id,
    )


def validate_spec_payload(
    d: dict,
    *,
    plan_headers=None,
    declared_vote_env: str | None = None,
) -> tuple[list[str], list[str]]:
    """Valida o spec vindo do wizard. Devolve (erros, avisos) em PT-PT.

    Erros bloqueiam a gravação; avisos pedem confirmação (campos custom
    fora do pipeline canónico). `plan_headers` (opcional) são os headers do
    último plano carregado — usados só para AVISAR quando uma coluna
    declarada não existe (None = sem plano conhecido, sem avisos falsos).
    `declared_vote_env` é o modo CROSS_DECLARED_VOTE do servidor (off/
    shadow/on; None = desconhecido) — vote=true com env off é ACEITE mas
    avisado (o admin regista hoje, o dono liga o env na fábrica amanhã)."""
    errors: list[str] = []
    warnings: list[str] = []

    name = str(d.get("name") or "").strip()
    if not _SNAKE_RE.fullmatch(name or ""):
        errors.append("nome do template inválido — usar snake_case (a-z, 0-9, _)")

    row_fields = [str(f).strip() for f in (d.get("row_fields") or []) if str(f).strip()]
    if not row_fields:
        errors.append("o template precisa de pelo menos 1 campo de tabela")
    seen: set[str] = set()
    for f in row_fields:
        if not _SNAKE_RE.fullmatch(f):
            errors.append(f"campo inválido: '{f}' — usar snake_case")
        elif f in seen:
            errors.append(f"campo duplicado: '{f}'")
        elif f not in KNOWN_ROW_FIELDS:
            warnings.append(
                f"campo '{f}' não é canónico — será extraído mas fica sem "
                "KPIs nem cross-check")
        seen.add(f)

    aliases = [str(a).strip() for a in (d.get("setor_aliases") or []) if str(a).strip()]
    if not aliases:
        errors.append(
            "é obrigatório pelo menos 1 alias de setor/máquina — é assim "
            "que o OCR reconhece o template")

    cross = [str(f) for f in (d.get("cross_check_fields") or [])]
    for f in cross:
        if f not in row_fields:
            errors.append(f"cross-check '{f}' não está nos campos da tabela")
        elif f not in CROSSABLE_FIELDS:
            errors.append(
                f"cross-check '{f}' não é validável contra o plano/StockSAP "
                f"(permitidos: {', '.join(sorted(CROSSABLE_FIELDS))})")

    # Cross declarado (informativo) — só para campos custom; canónicos usam
    # o cross normal ou têm regra local própria (coni/qtd/pri…).
    declared = d.get("declared_cross")
    if declared is not None and not isinstance(declared, dict):
        errors.append("declared_cross inválido — esperado objeto campo→config")
        declared = None
    header_set = set(plan_headers) if plan_headers else None
    for fld, cfg in (declared or {}).items():
        fld = str(fld)  # noqa: PLW2901 — normalização do próprio item
        if not isinstance(cfg, dict):
            errors.append(f"cross declarado '{fld}': config inválida")
            continue
        if fld not in row_fields:
            errors.append(f"cross declarado '{fld}' não está nos campos da tabela")
        elif fld in CROSSABLE_FIELDS:
            errors.append(
                f"cross declarado '{fld}': é campo canónico — usa o cross-check "
                "normal")
        elif fld in KNOWN_ROW_FIELDS:
            errors.append(
                f"cross declarado '{fld}': campo canónico com regra própria — "
                "o cross declarado é só para campos custom")
        ref = str(cfg.get("ref") or "plan")
        if ref != "plan":
            errors.append(
                f"cross declarado '{fld}': ref '{ref}' não suportada (só 'plan')")
        column = str(cfg.get("column") or "").strip()
        if not column or not re.fullmatch(r"[a-z0-9_ ]+", column):
            errors.append(
                f"cross declarado '{fld}': coluna do plano inválida — usar o "
                "header em minúsculas (a-z, 0-9, _, espaço)")
        elif header_set is not None and column not in header_set:
            warnings.append(
                f"cross declarado '{fld}': coluna '{column}' não existe no "
                "último plano carregado — fica NA até um plano com essa coluna")
        cmp = str(cfg.get("cmp") or "text")
        if cmp not in ("text", "num"):
            errors.append(
                f"cross declarado '{fld}': comparação '{cmp}' inválida "
                "(text ou num)")
        tol = cfg.get("tol")
        if tol is not None:
            if cmp != "num":
                errors.append(
                    f"cross declarado '{fld}': tolerância só faz sentido com "
                    "comparação numérica")
            else:
                try:
                    tol_f = float(tol)
                except (TypeError, ValueError):
                    tol_f = None
                if tol_f is None or tol_f <= 0:
                    errors.append(
                        f"cross declarado '{fld}': tolerância inválida — "
                        "número > 0")
        vote = cfg.get("vote")
        if vote is not None and not isinstance(vote, bool):
            errors.append(
                f"cross declarado '{fld}': vote inválido — true ou false")
        elif vote is True and declared_vote_env == "off":
            warnings.append(
                f"cross declarado '{fld}': voto gravado mas o "
                "CROSS_DECLARED_VOTE está desligado no servidor — fica "
                "inerte até o dono o ligar")

    return errors, warnings


def reload_registry() -> dict:
    """(Re)instala os templates DB ativos no registry. Nunca crasha — um
    spec_json partido é ignorado com aviso; com DB vazia o registry fica
    byte-idêntico aos builtins. Devolve um resumo para logging/UI."""
    specs: list[TemplateSpec] = []
    bad: list[str] = []
    try:
        rows = db.list_kanban_templates(status="ativo")
    except Exception as exc:
        print(f"[template_store] lista de templates falhou: {exc}",
              file=sys.stderr)
        rows = []
    for row in rows:
        try:
            d = json.loads(row["spec_json"])
            specs.append(spec_from_dict(
                d, unidade_id=row["unidade_id"], db_id=row["id"]))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            bad.append(f"{row.get('name')}: {exc}")
            print(f"[template_store] spec inválido ignorado — "
                  f"{row.get('name')}: {exc}", file=sys.stderr)
    skipped = set_runtime_templates(specs)
    return {
        "loaded": [s.name for s in specs if s.name not in skipped],
        "skipped_builtin_collision": skipped,
        "invalid": bad,
    }


def unidade_for_template(template_name: str | None) -> int | None:
    """Unidade fabril do template (None = Trofa/sede, inclui builtins)."""
    if not template_name:
        return None
    spec = TEMPLATES.get(template_name)
    return getattr(spec, "unidade_id", None) if spec else None


# ---------------------------------------------------------------------------
# Mapeamento label impresso → campo canónico (descoberta do wizard)
# ---------------------------------------------------------------------------

def _norm_label(raw: str) -> str:
    """Normaliza um label impresso para matching: maiúsculas, sem acentos,
    só A-Z0-9 (espaços/pontuação fora)."""
    decomposed = unicodedata.normalize("NFKD", str(raw or ""))
    no_marks = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"[^A-Z0-9]+", "", no_marks.upper())


# Sinónimos frequentes nos papéis além dos labels canónicos exatos.
_LABEL_SYNONYMS: dict[str, str] = {
    "QUANTIDADE": "qtd",
    "QTDCOLUNAS": "qtd",
    "REFERENCIA": "modelo",
    "REFERENCIAPECA": "modelo",
    "NLOTE": "lote",
    "ESPESSURA": "esp",
    "COMP": "comp_mm",
    "COMPRIMENTO": "comp_mm",
    "LARG": "larg_mm",
    "LARGURA": "larg_mm",
    "FERRAMENTA": "coni",
    "CESTA": "cesta_n",
    "MOTIVODAPARAGEM": "motivo",
}

_FOOTER_SYNONYMS: dict[str, str] = {
    "COLUNASPRODUZIDAS": "colunas_produzidas",
    "TOTALQTD": "colunas_produzidas",
    "TOTAL": "colunas_produzidas",
    "HORASTRABALHADAS": "horas_trabalhadas",
    "HORAS": "horas_trabalhadas",
}


def _label_index() -> dict[str, str]:
    """Índice invertido label normalizado → campo canónico. Inclui os
    labels de _FIELD_LABELS, os nomes dos próprios campos e sinónimos."""
    index: dict[str, str] = {}
    for field, label in canonical_field_labels().items():
        index[_norm_label(label)] = field
    for field in KNOWN_ROW_FIELDS:
        index.setdefault(_norm_label(field), field)
    for k, v in _LABEL_SYNONYMS.items():
        index.setdefault(k, v)
    return index


def _slug_field(label: str) -> str:
    """Label sem match → nome de campo custom snake_case."""
    decomposed = unicodedata.normalize("NFKD", str(label or ""))
    no_marks = "".join(c for c in decomposed if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9]+", "_", no_marks.lower()).strip("_")
    if not s:
        return "campo"
    if s[0].isdigit():
        s = "c_" + s
    return s[:40]


def map_label_to_field(label: str) -> tuple[str, bool]:
    """Label impresso → (campo, matched). matched=False → campo custom
    derivado do label (extraído mas sem KPIs nem cross-check)."""
    norm = _norm_label(label)
    field = _label_index().get(norm)
    if field:
        return field, True
    return _slug_field(label), False


def suggest_spec_from_discovery(
    discovery: dict,
    *,
    name: str,
    unidade_id: int,
) -> dict:
    """Resultado da descoberta → proposta de spec para o passo 'campos' do
    wizard. A ativação NUNCA é automática — isto é só a sugestão inicial.

    Devolve {"spec": dict, "field_map": [...], "warnings": [...]}, onde
    field_map = [{"label", "field", "section", "matched"}] alimenta a
    tabela editável do wizard.
    """
    field_map: list[dict] = []
    warnings: list[str] = []
    row_fields: list[str] = []
    seen: set[str] = set()
    for label in discovery.get("columns") or []:
        field, matched = map_label_to_field(label)
        base = field
        n = 2
        while field in seen:  # colunas repetidas → sufixo
            field = f"{base}_{n}"
            n += 1
        seen.add(field)
        row_fields.append(field)
        field_map.append({"label": label, "field": field,
                          "section": "rows", "matched": matched})
        if not matched:
            warnings.append(
                f"coluna '{label}' sem campo canónico — proposta como "
                f"'{field}' (sem KPIs nem cross-check)")

    footer_fields: list[str] = []
    for label in discovery.get("footer") or []:
        canonical = _FOOTER_SYNONYMS.get(_norm_label(label))
        if canonical and canonical not in footer_fields:
            footer_fields.append(canonical)
            field_map.append({"label": label, "field": canonical,
                              "section": "footer", "matched": True})
        else:
            field_map.append({"label": label, "field": "",
                              "section": "footer", "matched": False})
            warnings.append(f"rodapé '{label}' ignorado (sem equivalente)")

    for label in discovery.get("header") or []:
        field_map.append({"label": label, "field": "",
                          "section": "header", "matched": True})

    cross = [f for f in row_fields if f in CROSSABLE_FIELDS]
    spec = {
        "name": name,
        "tpl_code": "TPL103",
        "phase": "Produção",
        "setor_aliases": [],  # obrigatório — o humano preenche no wizard
        "row_fields": row_fields,
        "footer_fields": footer_fields or list(_DEFAULT_FOOTER),
        "has_production_rows": True,
        "is_gemini": False,
        "cross_check_fields": cross,
        "header_fields": list(_DEFAULT_HEADER),
        "csv_block_label": "TABELA DE PRODUÇÃO",
        "description": (discovery.get("title") or "").strip(),
        "field_labels": {
            m["field"]: m["label"] for m in field_map
            if m["section"] == "rows" and m["field"]
        },
    }
    return {"spec": spec, "field_map": field_map, "warnings": warnings,
            "unidade_id": unidade_id}

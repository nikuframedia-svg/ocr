"""Task C E3 — parâmetros dos KPIs de produção (fórmulas editáveis).

Os defaults vivem em CÓDIGO (DEFAULT_KPIS) e reproduzem byte-a-byte as
fórmulas históricas de kpis.production_overview. Sem ficheiro no disco o
comportamento é idêntico ao de sempre.

O ficheiro ``data/kpi_params.json`` é escrito pela app (gitignored — lição
R121/R223: dados runtime tracked sujam o working tree e abortam o git pull
da fábrica). Estrutura::

    {"version": 3, "kpis": [...], "history": [{"saved_at", "version", "kpis"}]}

Histórico com cap de 20 gravações; versão otimista (gravar exige a versão
atual — corrida entre dois browsers dá 409 em vez de silently clobber).

Semântica de compat (preserva as nuances do output atual):
  - ``zero_fallback``: fórmula deu None (div/0, var None) → 0
  - ``hide_if_zero``: valor cru ausente ou <= 0 → None (a UI oculta)
Arredondamento DEPOIS do gate hide_if_zero (0.04 t > 0 mostra "0.0").
"""
from __future__ import annotations

import copy
import json
import os
import threading
from datetime import datetime
from pathlib import Path

from app.web.kpi_expr import KpiExprError, eval_expr, validate_expr

__all__ = [
    "DEFAULT_KPIS",
    "SCOPE_VARIABLES",
    "KpiVersionConflict",
    "compute_scope_kpis",
    "get_kpis",
    "invalidate_cache",
    "load_state",
    "params_path",
    "revert_kpis",
    "save_kpis",
    "validate_kpi_def",
]

_PARAMS_PATH = Path(__file__).resolve().parents[3] / "data" / "kpi_params.json"
_HISTORY_CAP = 20
_LOCK = threading.Lock()
_cache: dict | None = None
_cache_mtime: float | None = None

# Variáveis disponíveis por scope — com unidade para a UI. Os valores vêm
# dos agregados de production_overview.
SCOPE_VARIABLES: dict[str, dict[str, dict]] = {
    "totals": {
        "qtd": {"label": "Colunas produzidas", "unit": "col"},
        "horas": {"label": "Horas registadas", "unit": "h"},
        "kg_consumido": {"label": "Aço consumido", "unit": "kg"},
        "kg_produzido": {"label": "Aço produzido", "unit": "kg"},
        "kg_desperdicio": {"label": "Desperdício", "unit": "kg"},
        "chapas": {"label": "Chapas usadas", "unit": "un"},
        "n_folhas": {"label": "Folhas kanban", "unit": "un"},
        "n_operadores": {"label": "Operadores distintos", "unit": "un"},
    },
    "sector": {
        "qtd": {"label": "Colunas do setor", "unit": "col"},
        "horas": {"label": "Horas do setor", "unit": "h"},
        "n_folhas": {"label": "Folhas do setor", "unit": "un"},
        "n_linhas": {"label": "Linhas kanban", "unit": "un"},
    },
    "machine": {
        "qtd": {"label": "Colunas da máquina", "unit": "col"},
        "horas": {"label": "Horas da máquina", "unit": "h"},
        "n_folhas": {"label": "Folhas da máquina", "unit": "un"},
        "n_linhas": {"label": "Linhas kanban", "unit": "un"},
    },
}

# Fórmulas de fábrica — REPRODUZEM as expressões históricas (R29/R34/R72).
# `scopes`: onde o KPI é calculado; `compat`: ver docstring; `fmt`: "int"
# trunca (chapas); `target`/`direction`: metas (capacidade nova, sem default).
DEFAULT_KPIS: list[dict] = [
    {
        "id": "col_per_h", "label": "Colunas por hora",
        "expr": "qtd / horas", "unit": "col/h", "round": 1,
        "compat": "zero_fallback", "fmt": None,
        "target": None, "direction": "higher",
        "scopes": ["totals", "sector", "machine"],
    },
    {
        "id": "min_per_col", "label": "Minutos por coluna",
        "expr": "horas * 60 / qtd", "unit": "min/col", "round": 1,
        "compat": "zero_fallback", "fmt": None,
        "target": None, "direction": "lower",
        "scopes": ["totals", "sector", "machine"],
    },
    {
        "id": "col_per_operador", "label": "Colunas por operador",
        "expr": "qtd / n_operadores", "unit": "col/op", "round": 1,
        "compat": "zero_fallback", "fmt": None,
        "target": None, "direction": "higher",
        "scopes": ["totals"],
    },
    {
        "id": "toneladas_consumido", "label": "Toneladas consumidas",
        "expr": "kg_consumido / 1000", "unit": "t", "round": 1,
        "compat": "hide_if_zero", "fmt": None,
        "target": None, "direction": "lower",
        "scopes": ["totals"],
    },
    {
        "id": "toneladas_produzido", "label": "Toneladas produzidas",
        "expr": "kg_produzido / 1000", "unit": "t", "round": 1,
        "compat": "hide_if_zero", "fmt": None,
        "target": None, "direction": "higher",
        "scopes": ["totals"],
    },
    {
        "id": "chapas_total", "label": "Chapas usadas",
        "expr": "chapas", "unit": "un", "round": 0,
        "compat": "hide_if_zero", "fmt": "int",
        "target": None, "direction": "lower",
        "scopes": ["totals"],
    },
    {
        "id": "perc_desperdicio", "label": "% desperdício",
        "expr": "kg_desperdicio / kg_consumido * 100", "unit": "%", "round": 1,
        "compat": None, "fmt": None,
        "target": None, "direction": "lower",
        "scopes": ["totals"],
    },
    {
        # Back-compat alias pré-R72 — templates/scripts legados leem
        # `toneladas` = peso produzido.
        "id": "toneladas", "label": "Toneladas (alias produzido)",
        "expr": "kg_produzido / 1000", "unit": "t", "round": 1,
        "compat": "hide_if_zero", "fmt": None,
        "target": None, "direction": "higher",
        "scopes": ["totals"],
    },
]

_DEFAULTS_BY_ID = {k["id"]: k for k in DEFAULT_KPIS}
_VALID_SCOPES = tuple(SCOPE_VARIABLES.keys())
_VALID_DIRECTIONS = ("higher", "lower")

# Chaves fixas dos dicts de output de production_overview — um KPI custom
# com um destes ids sobreporia agregados primitivos no template.
_RESERVED_IDS = frozenset({
    "colunas", "hours", "n_sheets", "n_operadores", "n_rows", "qtd",
    "name", "has_data", "machines", "code", "date", "period",
})


class KpiVersionConflict(Exception):
    """Gravação com versão desatualizada — outro browser gravou primeiro."""


def params_path() -> Path:
    return _PARAMS_PATH


def invalidate_cache() -> None:
    global _cache, _cache_mtime
    with _LOCK:
        _cache = None
        _cache_mtime = None


def _read_file() -> dict | None:
    """Lê o ficheiro; JSON corrupto/estrutura errada → None (defaults)."""
    try:
        raw = json.loads(_PARAMS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("kpis"), list):
        return None
    return raw


def _safe_version(raw: dict | None) -> int:
    try:
        return int(raw.get("version") or 0) if raw else 0
    except (TypeError, ValueError):
        return 0


def load_state() -> dict:
    """Estado atual: {"version", "kpis", "history"}. Cache por mtime."""
    global _cache, _cache_mtime
    try:
        mtime = _PARAMS_PATH.stat().st_mtime
    except OSError:
        mtime = None
    with _LOCK:
        if _cache is not None and _cache_mtime == mtime:
            return copy.deepcopy(_cache)
    state = None
    if mtime is not None:
        raw = _read_file()
        if raw is not None:
            state = {
                "version": _safe_version(raw),
                "kpis": _merge_with_defaults(raw["kpis"]),
                "history": raw.get("history") or [],
            }
    if state is None:
        state = {
            "version": 0,
            "kpis": copy.deepcopy(DEFAULT_KPIS),
            "history": [],
        }
    with _LOCK:
        _cache = copy.deepcopy(state)
        _cache_mtime = mtime
    return state


def _merge_with_defaults(stored: list) -> list[dict]:
    """Overlay do ficheiro sobre os defaults: um KPI de fábrica em falta no
    ficheiro reaparece com a fórmula default (upgrade-safe); KPIs novos do
    utilizador ficam no fim, pela ordem gravada."""
    by_id = {k.get("id"): k for k in stored if isinstance(k, dict) and k.get("id")}
    merged: list[dict] = []
    for default in DEFAULT_KPIS:
        got = by_id.pop(default["id"], None)
        if got is None:
            merged.append(copy.deepcopy(default))
        else:
            # Overlay sobre o default + normalização (aguenta ficheiro
            # editado à mão com chaves em falta ou tipos errados).
            merged.append(_normalize_custom({**default, **got}))
    for k in stored:
        kid = k.get("id") if isinstance(k, dict) else None
        if kid and kid in by_id:
            merged.append(_normalize_custom(k))
            by_id.pop(kid)
    return merged


def _normalize_custom(k: dict) -> dict:
    return {
        "id": str(k.get("id")),
        "label": str(k.get("label") or k.get("id")),
        "expr": str(k.get("expr") or ""),
        "unit": str(k.get("unit") or ""),
        "round": int(k.get("round") if k.get("round") is not None else 1),
        "compat": k.get("compat") if k.get("compat") in ("zero_fallback", "hide_if_zero") else None,
        "fmt": "int" if k.get("fmt") == "int" else None,
        "target": float(k["target"]) if isinstance(k.get("target"), (int, float)) else None,
        "direction": k.get("direction") if k.get("direction") in _VALID_DIRECTIONS else "higher",
        "scopes": [s for s in (k.get("scopes") or ["totals"]) if s in _VALID_SCOPES] or ["totals"],
    }


def get_kpis() -> list[dict]:
    return load_state()["kpis"]


def normalize_kpis(kpis: list[dict]) -> list[dict]:
    """Normaliza um conjunto candidato (tipos/valores) sem o gravar —
    usado pelo preview do /admin/kpis."""
    return [_normalize_custom(k) for k in kpis if isinstance(k, dict)]


def validate_kpi_def(k: dict) -> str | None:
    """Valida um KPI (id, scopes, fórmula vs variáveis de CADA scope).
    Devolve mensagem de erro PT-PT ou None se OK."""
    kid = k.get("id") or ""
    if not kid or not all(c.isascii() and (c.islower() or c.isdigit() or c == "_") for c in kid):
        return "id inválido — usar snake_case (a-z, 0-9, _)"
    if kid in _RESERVED_IDS:
        return f"id reservado ({kid}) — escolhe outro nome"
    scopes = k.get("scopes") or []
    if not scopes or any(s not in _VALID_SCOPES for s in scopes):
        return f"scope inválido — permitidos: {', '.join(_VALID_SCOPES)}"
    if k.get("direction") not in _VALID_DIRECTIONS:
        return "direção inválida — 'higher' ou 'lower'"
    try:
        rnd = int(k.get("round", 1))
    except (TypeError, ValueError):
        return "arredondamento inválido"
    if not 0 <= rnd <= 4:
        return "arredondamento inválido — 0 a 4 casas"
    for scope in scopes:
        try:
            validate_expr(k.get("expr") or "", SCOPE_VARIABLES[scope].keys())
        except KpiExprError as e:
            return f"[{scope}] {e}"
    return None


def _atomic_write(payload: dict) -> None:
    _PARAMS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PARAMS_PATH.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, _PARAMS_PATH)


def _emit_changed(action: str, version: int) -> None:
    try:
        from app import kernel
        kernel.emit_event("kpi_params_changed", {"action": action, "version": version})
    except Exception:
        pass


def save_kpis(kpis: list[dict], expected_version: int) -> dict:
    """Valida e grava o conjunto completo. Devolve o estado novo.

    Levanta KpiVersionConflict se expected_version != versão atual, e
    ValueError com a lista de erros se alguma fórmula for inválida.
    """
    errors = {}
    seen: set[str] = set()
    for k in kpis:
        kid = k.get("id") or "?"
        if kid in seen:
            errors[kid] = "id duplicado"
            continue
        seen.add(kid)
        err = validate_kpi_def(k)
        if err:
            errors[kid] = err
    if errors:
        raise ValueError(json.dumps(errors, ensure_ascii=False))

    with _LOCK:
        raw = _read_file()
        current_version = _safe_version(raw)
        if expected_version != current_version:
            raise KpiVersionConflict(
                f"versão desatualizada (atual: {current_version})")
        history = (raw.get("history") if raw else None) or []
        prev_kpis = raw["kpis"] if raw else copy.deepcopy(DEFAULT_KPIS)
        history.append({
            "saved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "version": current_version,
            "kpis": prev_kpis,
        })
        history = history[-_HISTORY_CAP:]
        new_state = {
            "version": current_version + 1,
            "kpis": [_normalize_custom(k) for k in kpis],
            "history": history,
        }
        _atomic_write(new_state)
        global _cache, _cache_mtime
        _cache = None
        _cache_mtime = None
    _emit_changed("save", new_state["version"])
    return load_state()


def revert_kpis(to: str | int = "defaults") -> dict:
    """Reverte para os defaults de fábrica ou para uma entrada do histórico
    (índice na lista history). Devolve o estado novo."""
    with _LOCK:
        raw = _read_file()
        current_version = _safe_version(raw)
        history = (raw.get("history") if raw else None) or []
        if to == "defaults":
            restored = copy.deepcopy(DEFAULT_KPIS)
        else:
            idx = int(to)
            if not 0 <= idx < len(history):
                raise ValueError("entrada de histórico inexistente")
            restored = history[idx]["kpis"]
        prev_kpis = raw["kpis"] if raw else copy.deepcopy(DEFAULT_KPIS)
        history.append({
            "saved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "version": current_version,
            "kpis": prev_kpis,
        })
        history = history[-_HISTORY_CAP:]
        new_state = {
            "version": current_version + 1,
            "kpis": restored,
            "history": history,
        }
        _atomic_write(new_state)
        global _cache, _cache_mtime
        _cache = None
        _cache_mtime = None
    _emit_changed("revert", new_state["version"])
    return load_state()


# ---------------------------------------------------------------------------
# Runtime — usado por kpis.production_overview
# ---------------------------------------------------------------------------

def compute_scope_kpis(
    scope: str,
    values: dict[str, float | None],
    kpi_defs: list[dict] | None = None,
) -> dict[str, float | int | None]:
    """Calcula os KPIs de um scope a partir das variáveis agregadas.

    Fórmula inválida em runtime → fallback à default do mesmo id (se
    existir); um KPI nunca crasha o dashboard.
    """
    defs = kpi_defs if kpi_defs is not None else get_kpis()
    out: dict[str, float | int | None] = {}
    for k in defs:
        if scope not in (k.get("scopes") or []):
            continue
        try:
            raw = eval_expr(k.get("expr") or "", values)
        except KpiExprError:
            default = _DEFAULTS_BY_ID.get(k.get("id"))
            if default is None or scope not in default["scopes"]:
                out[k["id"]] = None
                continue
            raw = eval_expr(default["expr"], values)
        out[k["id"]] = _finalize(k, raw)
    return out


def _finalize(k: dict, raw: float | None) -> float | int | None:
    compat = k.get("compat")
    if compat == "hide_if_zero":
        if raw is None or raw <= 0:
            return None
    elif compat == "zero_fallback":
        if raw is None:
            return 0
    elif raw is None:
        return None
    rnd = int(k.get("round", 1))
    value = round(raw, rnd) if not isinstance(raw, int) else raw
    if k.get("fmt") == "int":
        return int(value)
    return value

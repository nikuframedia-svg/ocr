"""R265 — calendário de dias úteis da fábrica.

A data de um kanban processado deixa de vir do campo `Data` manuscrito e passa
a ser SEMPRE o dia útil anterior ao dia em que a foto entrou no sistema (o
kanban é preenchido durante a produção e fotografado na manhã seguinte). Este
módulo é a única fonte de verdade sobre o que é "dia útil".

Defaults em CÓDIGO (``DEFAULT_CALENDARIO``): segunda a SÁBADO são dias úteis —
a fábrica trabalha sábado de manhã, e carimbar essa produção com sexta
inflacionaria a sexta e faria o sábado desaparecer dos KPIs. Os sábados que
NÃO forem trabalhados entram na mesma lista dos feriados (`nao_uteis`), pelo
que a regra se adapta sem código novo.

O ficheiro ``data/calendario_util.json`` é escrito pela app (gitignored —
lição R121/R223: dados runtime tracked sujam o working tree e abortam o git
pull da fábrica). Sem ficheiro ⇒ defaults. Estrutura::

    {"version": 3,
     "calendario": {"dias_semana": [0,1,2,3,4,5],
                    "nao_uteis":   [{"data": "2026-08-15", "nota": "Assunção"}],
                    "uteis_extra": [{"data": "2026-08-16", "nota": "domingo extra"}]},
     "history": [{"saved_at", "version", "calendario"}]}

Precedência: ``uteis_extra`` > ``nao_uteis`` > ``dias_semana``.

Mesmo padrão do ``kpi_params`` (cache por mtime, lock, escrita atómica, versão
otimista com histórico) — deliberadamente, para não haver duas maneiras de
guardar configuração runtime nesta app.
"""
from __future__ import annotations

import copy
import json
import os
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

__all__ = [
    "DEFAULT_CALENDARIO",
    "DIAS_SEMANA_LABELS",
    "CalendarioVersionConflict",
    "calendario_path",
    "dia_util_anterior",
    "get_calendario",
    "invalidate_cache",
    "is_dia_util",
    "load_state",
    "revert_calendario",
    "save_calendario",
    "validate_calendario",
]

_CAL_PATH = Path(__file__).resolve().parents[3] / "data" / "calendario_util.json"
_HISTORY_CAP = 20
_LOCK = threading.Lock()
_cache: dict | None = None
_cache_mtime: float | None = None

# 0 = segunda … 6 = domingo (convenção de datetime.date.weekday()).
DIAS_SEMANA_LABELS: tuple[str, ...] = (
    "Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo",
)

DEFAULT_CALENDARIO: dict = {
    "dias_semana": [0, 1, 2, 3, 4, 5],  # seg-sáb (a fábrica trabalha sábado de manhã)
    "nao_uteis": [],                    # feriados + sábados/dias não trabalhados
    "uteis_extra": [],                  # dias trabalhados fora do normal
}

# Trava contra config degenerada (ex.: `dias_semana` vazio): sem cap, o recuo
# dia-a-dia seria um loop infinito DENTRO do worker do OCR.
_MAX_RECUO_DIAS = 14


class CalendarioVersionConflict(RuntimeError):
    """Gravação com versão desatualizada (dois browsers em /admin)."""


def calendario_path() -> Path:
    return _CAL_PATH


def invalidate_cache() -> None:
    global _cache, _cache_mtime
    with _LOCK:
        _cache = None
        _cache_mtime = None


def _parse_iso(s: object) -> date | None:
    try:
        return date.fromisoformat(str(s).strip())
    except (TypeError, ValueError):
        return None


def _normalize_dias(entries: object) -> list[dict]:
    """Normaliza uma lista de exceções: só datas ISO reais, sem duplicados,
    ordenadas. Um ficheiro editado à mão com lixo degrada para menos entradas,
    nunca para uma exceção no worker."""
    out: dict[str, str] = {}
    for e in entries if isinstance(entries, list) else []:
        if isinstance(e, str):
            d, nota = _parse_iso(e), ""
        elif isinstance(e, dict):
            d, nota = _parse_iso(e.get("data")), str(e.get("nota") or "")[:200]
        else:
            continue
        if d is not None:
            out[d.isoformat()] = nota
    return [{"data": k, "nota": out[k]} for k in sorted(out)]


def _normalize_calendario(cal: object) -> dict:
    cal = cal if isinstance(cal, dict) else {}
    dias = cal.get("dias_semana")
    if not isinstance(dias, list):
        dias = DEFAULT_CALENDARIO["dias_semana"]
    clean_dias = sorted({int(d) for d in dias
                         if isinstance(d, (int, float)) and not isinstance(d, bool)
                         and 0 <= int(d) <= 6})
    return {
        "dias_semana": clean_dias,
        "nao_uteis": _normalize_dias(cal.get("nao_uteis")),
        "uteis_extra": _normalize_dias(cal.get("uteis_extra")),
    }


def validate_calendario(cal: dict) -> str | None:
    """Valida um calendário candidato. Devolve erro PT-PT ou None."""
    if not isinstance(cal, dict):
        return "calendário inválido"
    dias = cal.get("dias_semana")
    if not isinstance(dias, list) or not dias:
        return "escolhe pelo menos um dia útil da semana"
    for d in dias:
        if isinstance(d, bool) or not isinstance(d, (int, float)) or not 0 <= int(d) <= 6:
            return "dia da semana inválido — usar 0 (segunda) a 6 (domingo)"
    for key, label in (("nao_uteis", "não úteis"), ("uteis_extra", "úteis extra")):
        entries = cal.get(key) or []
        if not isinstance(entries, list):
            return f"lista de dias {label} inválida"
        for e in entries:
            raw = e.get("data") if isinstance(e, dict) else e
            if _parse_iso(raw) is None:
                return f"data inválida em dias {label}: {raw!r} (usar AAAA-MM-DD)"
    nao = {e["data"] for e in _normalize_dias(cal.get("nao_uteis"))}
    extra = {e["data"] for e in _normalize_dias(cal.get("uteis_extra"))}
    both = sorted(nao & extra)
    if both:
        return (f"dia em ambas as listas: {', '.join(both)} — "
                "um dia não pode ser feriado e trabalhado ao mesmo tempo")
    return None


def _read_file() -> dict | None:
    """Lê o ficheiro; JSON corrupto/estrutura errada → None (defaults)."""
    try:
        raw = json.loads(_CAL_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("calendario"), dict):
        return None
    return raw


def _safe_version(raw: dict | None) -> int:
    try:
        return int(raw.get("version") or 0) if raw else 0
    except (TypeError, ValueError):
        return 0


def load_state() -> dict:
    """Estado atual: {"version", "calendario", "history"}. Cache por mtime."""
    global _cache, _cache_mtime
    try:
        mtime = _CAL_PATH.stat().st_mtime
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
                "calendario": _normalize_calendario(raw["calendario"]),
                "history": raw.get("history") or [],
            }
    if state is None:
        state = {
            "version": 0,
            "calendario": copy.deepcopy(DEFAULT_CALENDARIO),
            "history": [],
        }
    with _LOCK:
        _cache = copy.deepcopy(state)
        _cache_mtime = mtime
    return state


def get_calendario() -> dict:
    return load_state()["calendario"]


def is_dia_util(d: date, cal: dict | None = None) -> bool:
    """Precedência: trabalhado à força > feriado/paragem > dia da semana."""
    cal = cal if cal is not None else get_calendario()
    iso = d.isoformat()
    if any(e["data"] == iso for e in cal.get("uteis_extra") or []):
        return True
    if any(e["data"] == iso for e in cal.get("nao_uteis") or []):
        return False
    return d.weekday() in (cal.get("dias_semana") or [])


def dia_util_anterior(ref: date, cal: dict | None = None) -> date:
    """Primeiro dia útil ESTRITAMENTE antes de ``ref``.

    Recua dia a dia (é assim que se encadeiam fim de semana + feriados sem
    casos especiais). Se em ``_MAX_RECUO_DIAS`` não houver nenhum dia útil
    (config degenerada — ninguém trabalha nunca), devolve ``ref - 1`` em vez
    de rebentar: uma data plausível é melhor do que uma folha em erro.
    """
    cal = cal if cal is not None else get_calendario()
    for back in range(1, _MAX_RECUO_DIAS + 1):
        cand = ref - timedelta(days=back)
        if is_dia_util(cand, cal):
            return cand
    return ref - timedelta(days=1)


def _atomic_write(payload: dict) -> None:
    _CAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _CAL_PATH.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, _CAL_PATH)


def _emit_changed(action: str, version: int) -> None:
    try:
        from app import kernel
        kernel.emit_event("calendario_changed",
                          {"action": action, "version": version})
    except Exception:
        pass


def _commit(new_cal: dict, *, expected_version: int | None, action: str) -> dict:
    global _cache, _cache_mtime
    with _LOCK:
        raw = _read_file()
        current_version = _safe_version(raw)
        if expected_version is not None and int(expected_version) != current_version:
            raise CalendarioVersionConflict(
                f"versão desatualizada (atual: {current_version})")
        history = (raw.get("history") if raw else None) or []
        prev = (_normalize_calendario(raw["calendario"]) if raw
                else copy.deepcopy(DEFAULT_CALENDARIO))
        history.append({
            "saved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "version": current_version,
            "calendario": prev,
        })
        new_state = {
            "version": current_version + 1,
            "calendario": new_cal,
            "history": history[-_HISTORY_CAP:],
        }
        _atomic_write(new_state)
        _cache = None
        _cache_mtime = None
    _emit_changed(action, new_state["version"])
    return load_state()


def save_calendario(cal: dict, expected_version: int) -> dict:
    """Valida e grava. Levanta ValueError (erro PT-PT) ou
    CalendarioVersionConflict."""
    err = validate_calendario(cal)
    if err:
        raise ValueError(err)
    return _commit(_normalize_calendario(cal),
                   expected_version=expected_version, action="save")


def revert_calendario(to: str | int = "defaults") -> dict:
    """Reverte para os defaults de fábrica ou para uma entrada do histórico."""
    if to == "defaults":
        restored = copy.deepcopy(DEFAULT_CALENDARIO)
    else:
        idx = int(to)
        history = load_state()["history"]
        if not 0 <= idx < len(history):
            raise ValueError("entrada de histórico inexistente")
        restored = _normalize_calendario(history[idx].get("calendario"))
    return _commit(restored, expected_version=None, action="revert")

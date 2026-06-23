"""R110.E — Kernel arquitectural.

Núcleo pequeno (~500 LOC) que orquestra o sistema via event log. Os
componentes (motor v5, R98 scheduler, agente Qwen, policy_engine)
continuam a viver onde estão; o kernel é uma camada de coordenação
agnóstica que regista TODOS os eventos importantes e expõe replay.

Decisões de design (Karpathy-style):
- Limite duro: este módulo nunca cresce além de ~500 LOC.
- Estado global: `data/kernel_state.json` versionado por evento.
- Event log: `data/events.jsonl` append-only (replayable).
- Sem singletons globais — tudo passa por funções puras + ficheiros.

Eventos suportados (extensíveis):
- sheet_uploaded: nova folha entrou no sistema
- sheet_validated: operador validou uma folha
- proposal_created: agente criou uma proposta
- proposal_decided: humano aceitou/rejeitou
- policy_promoted: nova versão de policy activa
- policy_rolled_back: voltámos para versão anterior
- circuit_breaker_triggered: regressão detectada
- r98_cycle_completed: motor R98 acabou um ciclo

Refactor minimal: cada componente que já existe ganha uma linha que
chama `kernel.emit_event(...)`. Nada se rearquitecta.
"""
from __future__ import annotations

import gzip  # R117 — rotação do events.jsonl
import json
import logging
import os
import re  # R117 — parse de events.jsonl.N.gz
import shutil  # R117 — copia para .gz em chunks
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


_REPO = Path(__file__).resolve().parents[2]
_DATA_DIR = _REPO / "data"
_EVENT_LOG = _DATA_DIR / "events.jsonl"
_STATE_FILE = _DATA_DIR / "kernel_state.json"

# R117 — limite de rotação. 50 MB é um meio-termo razoável: a ~300 B
# por evento dá ~170k eventos por ficheiro (meses de uso normal) e
# mantém-se barato comprimir num único shot quando rodamos.
_ROTATE_MAX_BYTES = 50_000_000
_ROTATED_RE = re.compile(r"^events\.jsonl\.(\d+)\.gz$")

_LOCK = threading.Lock()


# Tipos de evento aceites (centraliza para validação).
EVENT_TYPES = frozenset({
    "sheet_uploaded",
    "sheet_validated",
    "proposal_created",
    "proposal_decided",
    "policy_promoted",
    "policy_rolled_back",
    "circuit_breaker_triggered",
    "r98_cycle_completed",
    "qwen_session",
    "shadow_run_completed",
    "sheet_profiled",  # R224 — timing por etapa + resumo do profiling por folha
})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_data_dir() -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)


# ----- State management ----------------------------------------------------

def _empty_state() -> dict:
    return {
        "version": 0,
        "last_event_at": None,
        "counts": {
            "sheets_uploaded": 0,
            "sheets_validated": 0,
            "proposals_created": 0,
            "proposals_accepted": 0,
            "proposals_rejected": 0,
            "policies_promoted": 0,
            "rollbacks": 0,
            "circuit_breaker_triggers": 0,
            "r98_cycles": 0,
            "qwen_sessions": 0,
            "shadow_runs": 0,
        },
        "active_policy_version": None,
        "last_r98_run_at": None,
        "last_qwen_session_at": None,
        "last_circuit_breaker_at": None,
    }


def get_state() -> dict:
    """Lê o estado actual do kernel a partir do ficheiro JSON.

    Se o ficheiro não existe, devolve estado vazio.
    """
    _ensure_data_dir()
    if not _STATE_FILE.exists():
        return _empty_state()
    try:
        return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("kernel_state.json corrompido (%s) — devolvendo vazio", e)
        return _empty_state()


def _write_state(state: dict) -> None:
    """Escrita atómica via tmp+rename."""
    _ensure_data_dir()
    tmp = _STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(_STATE_FILE)


def _apply_event_to_state(state: dict, event: dict) -> dict:
    """State machine pura: dado um estado + um evento, devolve novo estado.

    Esta função é o coração do replay — tem de ser determinística.
    """
    new = json.loads(json.dumps(state))  # deep copy
    etype = event.get("type", "")
    counts = new.setdefault("counts", _empty_state()["counts"])

    if etype == "sheet_uploaded":
        counts["sheets_uploaded"] += 1
    elif etype == "sheet_validated":
        counts["sheets_validated"] += 1
    elif etype == "proposal_created":
        counts["proposals_created"] += 1
    elif etype == "proposal_decided":
        decision = (event.get("payload") or {}).get("decision", "")
        if decision == "accepted":
            counts["proposals_accepted"] += 1
        elif decision == "rejected":
            counts["proposals_rejected"] += 1
    elif etype == "policy_promoted":
        counts["policies_promoted"] += 1
        new["active_policy_version"] = (event.get("payload") or {}).get("version")
    elif etype == "policy_rolled_back":
        counts["rollbacks"] += 1
        new["active_policy_version"] = (event.get("payload") or {}).get("version")
    elif etype == "circuit_breaker_triggered":
        counts["circuit_breaker_triggers"] += 1
        new["last_circuit_breaker_at"] = event.get("at")
    elif etype == "r98_cycle_completed":
        counts["r98_cycles"] += 1
        new["last_r98_run_at"] = event.get("at")
    elif etype == "qwen_session":
        counts["qwen_sessions"] += 1
        new["last_qwen_session_at"] = event.get("at")
    elif etype == "shadow_run_completed":
        counts["shadow_runs"] += 1

    new["version"] = (state.get("version") or 0) + 1
    new["last_event_at"] = event.get("at")
    return new


# ----- Event log -----------------------------------------------------------

def _rotate_event_log_if_needed() -> None:
    """R117 — se events.jsonl > 50 MB, comprime para events.jsonl.{N+1}.gz.

    Encontra o N mais alto dos ficheiros rotados existentes e usa N+1.
    Falhas de rotação são silenciadas — auditoria recente continua a
    funcionar mesmo que o disco esteja cheio.

    Assume que o caller já segura _LOCK.
    """
    try:
        if not _EVENT_LOG.exists():
            return
        if _EVENT_LOG.stat().st_size <= _ROTATE_MAX_BYTES:
            return
        max_n = 0
        for entry in _DATA_DIR.iterdir():
            m = _ROTATED_RE.match(entry.name)
            if m:
                n = int(m.group(1))
                if n > max_n:
                    max_n = n
        next_n = max_n + 1
        rotated = _DATA_DIR / f"events.jsonl.{next_n}.gz"
        # Comprime em streaming (não carrega tudo em memória).
        with open(_EVENT_LOG, "rb") as src, gzip.open(rotated, "wb") as dst:
            shutil.copyfileobj(src, dst)
        os.remove(_EVENT_LOG)
        logger.info("kernel: events.jsonl rodado para %s", rotated.name)
    except Exception as e:  # noqa: BLE001
        # Rotação é best-effort — nunca pode bloquear o emit.
        logger.warning("kernel: falha a rodar events.jsonl: %s", e)


def emit_event(event_type: str, payload: dict | None = None) -> dict:
    """Regista um evento no log + actualiza estado. Thread-safe.

    Devolve o evento gravado (com timestamp e id de versão).
    """
    if event_type not in EVENT_TYPES:
        logger.warning("Tipo de evento desconhecido: %s", event_type)

    _ensure_data_dir()
    with _LOCK:
        # R117 — rodar ANTES do append. Se o ficheiro foi rodado, o
        # open(..., "a") a seguir cria um novo events.jsonl vazio.
        _rotate_event_log_if_needed()
        state = get_state()
        event = {
            "version": (state.get("version") or 0) + 1,
            "type": event_type,
            "at": _now_iso(),
            "payload": payload or {},
        }
        # Append ao log
        with open(_EVENT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        # Actualizar estado
        new_state = _apply_event_to_state(state, event)
        _write_state(new_state)
    return event


# ----- Replay --------------------------------------------------------------

def replay(from_event: int = 0) -> dict:
    """Re-aplica eventos do log para reconstruir o estado.

    Útil para auditoria ("qual era o estado depois do evento 1247?")
    e para validação de determinismo do state machine.

    Se from_event > 0, salta os primeiros N eventos.
    """
    # R117 — lock contra leitura parcial enquanto emit_event escreve
    # (Windows: ficheiro aberto duas vezes pode devolver bytes truncados).
    # _LOCK é threading.Lock (não RLock): seguro porque _apply_event_to_state
    # é puro e replay nunca chama emit_event.
    state = _empty_state()
    with _LOCK:
        if not _EVENT_LOG.exists():
            return state
        skipped = 0
        with open(_EVENT_LOG, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                if skipped < from_event:
                    skipped += 1
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("evento corrompido no log, a saltar")
                    continue
                state = _apply_event_to_state(state, event)
    return state


def list_recent_events(limit: int = 50) -> list[dict]:
    """Devolve as últimas N entradas do log."""
    # R117 — lock contra leitura parcial concorrente com emit_event.
    with _LOCK:
        if not _EVENT_LOG.exists():
            return []
        # Strategy: ler o ficheiro inteiro até linha N do fim. Para volumes
        # grandes seria preciso indexar. Para já, OK.
        with open(_EVENT_LOG, "r", encoding="utf-8") as f:
            lines = f.readlines()
    out: list[dict] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def count_events() -> int:
    """Total de eventos no log."""
    # R117 — lock contra leitura parcial concorrente com emit_event.
    with _LOCK:
        if not _EVENT_LOG.exists():
            return 0
        with open(_EVENT_LOG, "r", encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())


# ----- Reset (para tests / re-bootstrap) ----------------------------------

def reset_state(reason: str = "manual") -> None:
    """Reset do estado e do event log. USAR COM CUIDADO."""
    logger.warning("kernel.reset_state called (%s)", reason)
    with _LOCK:
        if _STATE_FILE.exists():
            os.remove(_STATE_FILE)
        if _EVENT_LOG.exists():
            os.remove(_EVENT_LOG)


__all__ = [
    "EVENT_TYPES",
    "emit_event",
    "get_state",
    "replay",
    "list_recent_events",
    "count_events",
    "reset_state",
]

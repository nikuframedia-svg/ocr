"""R253/F3 — monitorização contínua da calibração do cross em produção.

Três vigias, todos ADITIVOS (padrão circuit-breaker do policy_engine:
detetam e recomendam, NUNCA revertem sozinhos — reverter o flip é decisão
humana via git revert, documentada no protocolo):

1. STALENESS: cross_params.json com fitted_at > 30 dias → alarme (o refit
   R244 devia estar a correr; >60 dias = escalar ao Luís).
2. CUSUM (Page) sobre a taxa de SOBREVIVÊNCIA das células auto-escritas:
   por dia, células escritas pelo cross em folhas validadas que NÃO foram
   corrigidas por um humano depois. Padronização por dia (n_i pequeno e
   variável — regime real da fábrica, 1-3 folhas/dia):
     Z_i = (p0 − p̂_i) / sqrt(p0(1−p0)/n_i)   (degradação → Z_i > 0)
     S_i = max(0, S_{i-1} + Z_i − k)          k=0.5 (convenção Page)
     ALARME quando S_i > h                    h=5.0 (ARL longo sob H0)
3. MURPHY (Brier = REL − RES + UNC) em janelas de >=500 células com
   decision_confidence: REL (miscalibração) acima do dobro do baseline do
   flip → alarme de prioridade alta.

Fonte de dados: edits com source='human' cruzadas com as células
auto-escritas (source='system') — a única verdade contínua disponível sem
rotulagem extra.
"""
from __future__ import annotations

import json
import logging
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_REPO = Path(__file__).resolve().parents[3]
_PARAMS_PATH = _REPO / "lexicons" / "cross_params.json"

STALE_DAYS_WARN = 30
STALE_DAYS_ESCALATE = 60
CUSUM_K = 0.5
CUSUM_H = 5.0
MURPHY_MIN_N = 500


def check_staleness(now: datetime | None = None) -> dict:
    """Idade dos parâmetros fitted; alarma acima dos limiares."""
    now = now or datetime.now(timezone.utc)
    try:
        params = json.loads(_PARAMS_PATH.read_text(encoding="utf-8"))
        fitted_at = datetime.fromisoformat(str(params.get("fitted_at")))
    except (OSError, ValueError, TypeError):
        return {"stale": True, "age_days": None,
                "reason": "cross_params.json ilegível ou sem fitted_at"}
    age_days = (now - fitted_at).total_seconds() / 86400.0
    return {
        "stale": age_days > STALE_DAYS_WARN,
        "escalate": age_days > STALE_DAYS_ESCALATE,
        "age_days": round(age_days, 1),
        "source_db_sha256_16": params.get("source_db_sha256_16"),
    }


def collect_writeback_outcomes(since_days: int = 90) -> list[tuple[str, float, int]]:
    """(dia, decision_confidence|nan, sobreviveu) por célula AUTO-ESCRITA
    (edits source='system' em campos cruzáveis de folhas validadas),
    onde sobreviveu = nenhum edit humano posterior no mesmo field_path."""
    from app.web import db

    out: list[tuple[str, float, int]] = []
    with db.conn() as c:
        rows = c.execute(
            """
            SELECT e.sheet_id, e.field_path, e.edited_at,
                   s.validated_at
            FROM edits e
            JOIN sheets s ON s.id = e.sheet_id
            WHERE e.source = 'system'
              AND s.status = 'validated'
              AND e.field_path LIKE 'rows[%'
              AND e.edited_at >= datetime('now', ?)
            """,
            (f"-{int(since_days)} days",),
        ).fetchall()
        human = set()
        for r in c.execute(
            """
            SELECT sheet_id, field_path FROM edits
            WHERE source = 'human'
              AND edited_at >= datetime('now', ?)
            """,
            (f"-{int(since_days)} days",),
        ):
            human.add((r["sheet_id"], r["field_path"]))
    for r in rows:
        day = str(r["edited_at"] or "")[:10]
        if not day:
            continue
        survived = 0 if (r["sheet_id"], r["field_path"]) in human else 1
        out.append((day, float("nan"), survived))
    return out


def cusum(daily: list[tuple[str, float, int]], p0: float,
          k: float = CUSUM_K, h: float = CUSUM_H) -> dict:
    """CUSUM de Page sobre a taxa diária de sobrevivência vs alvo p0."""
    by_day: dict[str, list[int]] = defaultdict(list)
    for day, _conf, ok in daily:
        by_day[day].append(ok)
    s = 0.0
    trajectory: list[dict] = []
    alarm_day: str | None = None
    for day in sorted(by_day):
        obs = by_day[day]
        n_i = len(obs)
        p_hat = sum(obs) / n_i
        z = (p0 - p_hat) / math.sqrt(max(p0 * (1 - p0) / n_i, 1e-12))
        s = max(0.0, s + z - k)
        trajectory.append({"day": day, "n": n_i,
                           "p_hat": round(p_hat, 3), "s": round(s, 3)})
        if s > h and alarm_day is None:
            alarm_day = day
    return {"p0": p0, "k": k, "h": h, "s_final": round(s, 3),
            "alarm": alarm_day is not None, "alarm_day": alarm_day,
            "trajectory": trajectory}


def murphy_decomposition(pairs: list[tuple[float, float]],
                         n_bins: int = 10) -> dict | None:
    """Brier = Reliability − Resolution + Uncertainty (Murphy 1973).
    None com amostra < MURPHY_MIN_N (sem poder estatístico)."""
    if len(pairs) < MURPHY_MIN_N:
        return None
    n = len(pairs)
    ybar = sum(y for _p, y in pairs) / n
    unc = ybar * (1 - ybar)
    bins: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for p, y in pairs:
        bins[min(n_bins - 1, int(p * n_bins))].append((p, y))
    rel = res = 0.0
    for v in bins.values():
        nk = len(v)
        pk = sum(p for p, _y in v) / nk
        yk = sum(y for _p, y in v) / nk
        rel += nk * (pk - yk) ** 2
        res += nk * (yk - ybar) ** 2
    rel /= n
    res /= n
    return {"n": n, "brier": round(rel - res + unc, 4),
            "reliability": round(rel, 4), "resolution": round(res, 4),
            "uncertainty": round(unc, 4)}


def run_monitor(p0: float = 0.95) -> dict:
    """Ciclo completo (chamado do learning scheduler): staleness + CUSUM.
    p0 = taxa-alvo de sobrevivência das auto-escritas (err@0.95 ≤5% do
    protocolo). Emite eventos kernel; NUNCA altera o motor."""
    report: dict = {"generated_at": datetime.now(timezone.utc).isoformat()}
    report["staleness"] = check_staleness()
    try:
        daily = collect_writeback_outcomes()
        report["n_outcomes"] = len(daily)
        report["cusum"] = cusum(daily, p0) if daily else None
    except Exception:
        logger.exception("calibration monitor: recolha falhou")
        report["cusum"] = None
    alarms: list[str] = []
    if report["staleness"].get("stale"):
        alarms.append(
            f"stale_params ({report['staleness'].get('age_days')}d)")
    if (report.get("cusum") or {}).get("alarm"):
        alarms.append(
            f"cusum_calibration_drift (dia {report['cusum']['alarm_day']})")
    report["alarms"] = alarms
    if alarms:
        try:
            from app import kernel
            kernel.emit_event("cross_calibration_alarm", {
                "alarms": alarms,
                "staleness": report["staleness"],
                "cusum_s": (report.get("cusum") or {}).get("s_final"),
            })
        except Exception:
            pass
    return report

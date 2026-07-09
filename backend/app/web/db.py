"""SQLite helpers — stdlib sqlite3, no ORM.

Single-file DB at ``data/app.db``. Two tables:

- ``sheets``: one row per uploaded photo, with current state (post-edits)
- ``edits``: append-only audit trail of every cell edit

JSON fields (``sheet_data``, ``raw_extraction``, ``dq_audit``) are
stored as TEXT and serialised via :mod:`json`.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from app.dq.ferramenta import canonical_ferramenta_or_raw

# R117 — logger para auto-promote de propostas em save_proposal
logger = logging.getLogger(__name__)

_DB_PATH = Path(__file__).resolve().parents[3] / "data" / "app.db"


# R256 — o Python 3.12 deprecou os adapters/converters DEFAULT do sqlite3
# (usados via detect_types=PARSE_DECLTYPES nas colunas TIMESTAMP). Sem isto,
# cada fetch emitia um DeprecationWarning (~6.4k por corrida da suite) e a
# remoção futura dos defaults mudaria o tipo devolvido para str. Registamos
# réplicas byte-a-byte dos defaults do CPython: comportamento IDÊNTICO
# (datetime nas colunas TIMESTAMP), zero warnings, à prova de remoção.
def _adapt_date(val: _dt.date) -> str:
    return val.isoformat()


def _adapt_datetime(val: _dt.datetime) -> str:
    return val.isoformat(" ")


def _convert_date(val: bytes) -> _dt.date:
    return _dt.date(*map(int, val.split(b"-")))


def _convert_timestamp(val: bytes) -> _dt.datetime:
    datepart, timepart = val.split(b" ")
    year, month, day = map(int, datepart.split(b"-"))
    timepart_full = timepart.split(b".")
    hours, minutes, seconds = map(int, timepart_full[0].split(b":"))
    if len(timepart_full) == 2:
        microseconds = int(f"{timepart_full[1].decode():0<6.6}")
    else:
        microseconds = 0
    return _dt.datetime(year, month, day, hours, minutes, seconds, microseconds)


sqlite3.register_adapter(_dt.date, _adapt_date)
sqlite3.register_adapter(_dt.datetime, _adapt_datetime)
sqlite3.register_converter("date", _convert_date)
sqlite3.register_converter("timestamp", _convert_timestamp)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sheets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    image_path TEXT NOT NULL,
    captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status TEXT NOT NULL DEFAULT 'pending',
    operador TEXT,
    sheet_data TEXT,
    raw_extraction TEXT,
    dq_audit TEXT,
    extracted_at TIMESTAMP,
    validated_at TIMESTAMP,
    error_message TEXT,
    image_rotation INTEGER NOT NULL DEFAULT 0,  -- user-set quarter-turns CW (0/90/180/270)
    -- rev00 (captura guiada frente/verso): pista de página do operador ('F'/'V'),
    -- token que liga as 2 fotos do mesmo kanban, e flag de revisão quando o
    -- cross-check do lado discorda / fica indeterminado.
    page_hint TEXT,
    capture_group TEXT,
    needs_review INTEGER NOT NULL DEFAULT 0,
    review_reason TEXT
);

CREATE TABLE IF NOT EXISTS edits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sheet_id INTEGER NOT NULL REFERENCES sheets(id),
    field_path TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT NOT NULL,
    edited_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Denormalised production rows: one row per data row across all sheets.
-- Auto-populated on update_extraction() and re-synced on apply_edit().
-- Drives all KPI aggregations (instead of json_each scans on sheet_data).
CREATE TABLE IF NOT EXISTS production_rows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sheet_id INTEGER NOT NULL REFERENCES sheets(id) ON DELETE CASCADE,
    row_index INTEGER NOT NULL,
    -- denormalised from sheet header / footer
    operador TEXT,
    sheet_date TEXT,                  -- DD-MM-YYYY as captured (kept as text; query via substr/julianday)
    sheet_iso_date TEXT,              -- YYYY-MM-DD for SQL date() comparisons
    captured_at TIMESTAMP,
    validated_at TIMESTAMP,
    sheet_status TEXT,
    sheet_hours REAL,                 -- footer.horas_trabalhadas as decimal hours
    sheet_total_qty INTEGER,          -- sum(qtd) of the sheet (for ratios)
    -- row data
    pri TEXT,
    cliente TEXT,
    ov TEXT,
    of TEXT,
    modelo TEXT,
    qtd INTEGER,
    comp_mm INTEGER,
    larg_mm INTEGER,
    lote TEXT,
    coni TEXT,
    esp REAL,
    lbase INTEGER,
    ltopo INTEGER,
    -- R128 — kanban LASER: dimensões próprias (dbase/dtopo). NULL para
    -- todos os outros templates.
    dbase INTEGER,
    dtopo INTEGER,
    -- R86 — Gemini (TPL102) fields: m² (area) + nesting code.
    -- Populated only for HPE32/GASPARINI/HD36; NULL for other templates.
    m2 REAL,
    nesting TEXT,
    -- R97 — extra fields per template family:
    --   qtd_metros: linear metres (Soldline, Laser)
    --   cesta_n:    basket number (Expedição)
    --   inicio/fim: start/end times HH:MM (Gemini + Paragens)
    qtd_metros REAL,
    cesta_n TEXT,
    inicio TEXT,
    fim TEXT,
    -- rev00 — SUCATA (nº de peças sucatadas). NULL para folhas do formato antigo.
    sucata INTEGER,
    UNIQUE (sheet_id, row_index)
);

-- Learning engine — canonical store of learned proposals.
-- Mined from `edits` (human corrections) + validated sheets every 50
-- validated sheets. Low-risk proposals land as 'approved', behaviour-
-- changing ones as 'quarantine' awaiting human review on /learnings.
CREATE TABLE IF NOT EXISTS learnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,            -- cliente_alias|operador_alias|modelo_alias
                                   -- |confusion_pair|fewshot_example|snap_rule
    field TEXT,                    -- affected field (cliente/modelo/operador/...)
    template_name TEXT,            -- scope; NULL = global
    payload TEXT NOT NULL,         -- JSON: the learning content
    risk TEXT NOT NULL DEFAULT 'behavior',   -- 'low' | 'behavior'
    status TEXT NOT NULL DEFAULT 'quarantine',  -- approved|quarantine|rejected|superseded
    origin TEXT NOT NULL DEFAULT 'edits_mining', -- edits_mining|validated_sheets|llm|manual
    evidence_count INTEGER NOT NULL DEFAULT 1,
    evidence_refs TEXT,            -- JSON: list of edit_id / sheet_id
    dedup_key TEXT NOT NULL UNIQUE,  -- stable key for idempotent upsert
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    decided_at TIMESTAMP,
    decided_by TEXT,
    note TEXT
);

-- Learning engine — one row per engine run. Doubles as the time series
-- for the "corrections per sheet" success metric.
CREATE TABLE IF NOT EXISTS learning_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMP,
    validated_count_at_run INTEGER,
    edits_scanned INTEGER,
    proposals_new INTEGER,
    proposals_auto_applied INTEGER,
    proposals_quarantined INTEGER,
    corrections_per_sheet REAL,
    status TEXT NOT NULL DEFAULT 'running',  -- running|done|error
    error_message TEXT
);

-- R108 — shadow scoring engine audit. One row per shadow_score() run.
-- Counts come from the scoring_engine.shadow_score result; agreement_rate
-- is left NULL here (computed later by the /shadow dashboard against the
-- production cross-check JSON).
CREATE TABLE IF NOT EXISTS shadow_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sheet_id INTEGER NOT NULL REFERENCES sheets(id) ON DELETE CASCADE,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMP,
    cells_total INTEGER,
    cells_snapped INTEGER,        -- correção suave/autofill aplicada pelo motor
    cells_very_different INTEGER, -- proposta longe do OCR; revisão humana
    cells_confirmed INTEGER,      -- motor escolheu candidato == OCR raw
    cells_na INTEGER,             -- campo sem pool de referência
    duration_ms INTEGER,
    status TEXT NOT NULL DEFAULT 'running',  -- running|done|error
    error_message TEXT
);

-- R110 — Qwen agent sessions (audit completo das invocações).
CREATE TABLE IF NOT EXISTS qwen_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger TEXT NOT NULL DEFAULT 'manual',  -- manual|cron|after_validation
    user_message TEXT NOT NULL,
    reply TEXT,
    tools_used TEXT,                          -- JSON list de tool names
    tool_calls_count INTEGER DEFAULT 0,
    rounds INTEGER DEFAULT 0,
    charts_count INTEGER DEFAULT 0,
    proposed_rules_count INTEGER DEFAULT 0,
    duration_ms INTEGER,
    status TEXT NOT NULL DEFAULT 'ok',        -- ok|error|max_rounds_reached
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- R110.B — Gráficos gerados pelo agente Qwen.
CREATE TABLE IF NOT EXISTS qwen_charts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    chart_type TEXT,                     -- bar|line|scatter|heatmap|histogram|pie
    spec TEXT NOT NULL,                  -- JSON ChartSpec
    narrative TEXT,                      -- explanação PT-PT
    session_id INTEGER REFERENCES qwen_sessions(id) ON DELETE SET NULL,
    pinned BOOLEAN NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- R110.C — Propostas do agente que mudam o sistema.
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,                  -- rule|template|cpis
    payload TEXT NOT NULL,               -- JSON
    justification TEXT,                  -- texto PT-PT do Qwen
    evidence TEXT,                       -- JSON: sheet_ids etc.
    risk_class TEXT NOT NULL,            -- auto|review|approval
    qwen_confidence REAL,                -- 0..1
    estimated_impact TEXT,               -- JSON
    status TEXT NOT NULL DEFAULT 'pending',  -- pending|accepted|rejected|auto_applied
    shadow_eval_results TEXT,            -- JSON
    session_id INTEGER REFERENCES qwen_sessions(id) ON DELETE SET NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    decided_at TIMESTAMP,
    decided_by TEXT
);

-- R110.C — Versões de policy (rules + template_overlay aggregadas).
CREATE TABLE IF NOT EXISTS policy_versions (
    version INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_version INTEGER,
    yaml_blob TEXT NOT NULL,
    diff_summary TEXT,
    created_by TEXT,                     -- qwen-agent|manual|circuit_breaker_rollback
    promoted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    active BOOLEAN NOT NULL DEFAULT 0,
    eval_results TEXT
);

-- R110.C — Overlay de templates aceites (alterações em runtime).
CREATE TABLE IF NOT EXISTS template_overlay (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    template_name TEXT NOT NULL,
    change_type TEXT NOT NULL,           -- add_field|remove_field|reorder
    payload TEXT NOT NULL,               -- JSON
    proposal_id INTEGER REFERENCES proposals(id) ON DELETE SET NULL,
    active BOOLEAN NOT NULL DEFAULT 1,
    promoted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- R110.C — Log do circuit breaker (apenas eventos).
CREATE TABLE IF NOT EXISTS circuit_breaker_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event TEXT NOT NULL,                 -- triggered|rollback|reset
    metric_name TEXT,
    metric_value REAL,
    baseline_value REAL,
    action_taken TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Unidades fabris (fábricas/instalações Metalogalva). 'Trofa' é a sede
-- (seed no init_db); folhas com unidade_id NULL pertencem-lhe (legacy —
-- o conceito nasceu com o registo de kanbans por unidade).
CREATE TABLE IF NOT EXISTS unidades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nome TEXT NOT NULL UNIQUE COLLATE NOCASE,   -- escrito pelo utilizador
    ativo INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Templates de kanban registados em runtime (por unidade fabril).
-- spec_json espelha TemplateSpec (templates_registry.py); os 18 builtin
-- NÃO migram — a DB só guarda templates novos. discovery_json = output
-- cru do OCR de descoberta (audit EN 1090).
CREATE TABLE IF NOT EXISTS kanban_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,           -- canónico, prefixo u{unidade_id}_
    unidade_id INTEGER NOT NULL REFERENCES unidades(id),
    spec_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',  -- draft|a_analisar|analisado|ativo|inativo
    image_path TEXT,
    discovery_json TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    activated_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_kanban_templates_status ON kanban_templates(status);
CREATE INDEX IF NOT EXISTS idx_kanban_templates_unidade ON kanban_templates(unidade_id);

CREATE INDEX IF NOT EXISTS idx_sheets_status ON sheets(status);
CREATE INDEX IF NOT EXISTS idx_sheets_captured ON sheets(captured_at);
CREATE INDEX IF NOT EXISTS idx_edits_sheet ON edits(sheet_id);
CREATE INDEX IF NOT EXISTS idx_rows_of ON production_rows(of);
CREATE INDEX IF NOT EXISTS idx_rows_cliente ON production_rows(cliente);
CREATE INDEX IF NOT EXISTS idx_rows_operador ON production_rows(operador);
CREATE INDEX IF NOT EXISTS idx_rows_iso_date ON production_rows(sheet_iso_date);
CREATE INDEX IF NOT EXISTS idx_rows_modelo ON production_rows(modelo);
CREATE INDEX IF NOT EXISTS idx_learnings_status ON learnings(status);
CREATE INDEX IF NOT EXISTS idx_learnings_kind ON learnings(kind);
CREATE INDEX IF NOT EXISTS idx_shadow_runs_sheet ON shadow_runs(sheet_id);
CREATE INDEX IF NOT EXISTS idx_shadow_runs_started ON shadow_runs(started_at);
CREATE INDEX IF NOT EXISTS idx_qwen_sessions_created ON qwen_sessions(created_at);
CREATE INDEX IF NOT EXISTS idx_qwen_sessions_trigger ON qwen_sessions(trigger);
CREATE INDEX IF NOT EXISTS idx_qwen_charts_created ON qwen_charts(created_at);
CREATE INDEX IF NOT EXISTS idx_qwen_charts_pinned ON qwen_charts(pinned);
CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status);
CREATE INDEX IF NOT EXISTS idx_proposals_kind ON proposals(kind);
CREATE INDEX IF NOT EXISTS idx_proposals_risk ON proposals(risk_class);
CREATE INDEX IF NOT EXISTS idx_policy_versions_active ON policy_versions(active);
CREATE INDEX IF NOT EXISTS idx_template_overlay_active ON template_overlay(active, template_name);
"""


def db_path() -> Path:
    return _DB_PATH


@contextmanager
def conn() -> Iterator[sqlite3.Connection]:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(_DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES, timeout=10.0)
    c.row_factory = sqlite3.Row
    # Foreign keys are off by default in SQLite; we use ON DELETE CASCADE
    # on production_rows so this matters.
    c.execute("PRAGMA foreign_keys = ON")
    # Wait up to 5s on lock conflicts before raising "database is locked".
    # Default is 0 (immediate raise), too aggressive for concurrent writes.
    c.execute("PRAGMA busy_timeout = 5000")
    try:
        yield c
        c.commit()
    finally:
        c.close()


@contextmanager
def conn_at(path: Path | str) -> Iterator[sqlite3.Connection]:
    """R256 — como :func:`conn` mas para um ``db_path`` arbitrário (dashboards
    downtime/gemini recebem o path por parâmetro). Substitui os
    ``sqlite3.connect`` diretos que não tinham busy_timeout nem close
    garantido em exceção. Leituras não fazem commit — é inócuo."""
    c = sqlite3.connect(path, detect_types=sqlite3.PARSE_DECLTYPES, timeout=10.0)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA busy_timeout = 5000")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db() -> None:
    with conn() as c:
        # WAL mode: writers don't block readers (H1). Persists across
        # connections — set once on init.
        c.execute("PRAGMA journal_mode = WAL")
        c.execute("PRAGMA synchronous = NORMAL")
        c.executescript(_SCHEMA)
        # Idempotent migrations for columns added after initial schema.
        cols = {r["name"] for r in c.execute("PRAGMA table_info(sheets)").fetchall()}
        if "image_rotation" not in cols:
            c.execute("ALTER TABLE sheets ADD COLUMN image_rotation INTEGER NOT NULL DEFAULT 0")
        # rev00 — captura guiada frente/verso + flag de revisão do lado.
        if "page_hint" not in cols:
            c.execute("ALTER TABLE sheets ADD COLUMN page_hint TEXT")
        if "capture_group" not in cols:
            c.execute("ALTER TABLE sheets ADD COLUMN capture_group TEXT")
        if "needs_review" not in cols:
            c.execute("ALTER TABLE sheets ADD COLUMN needs_review INTEGER NOT NULL DEFAULT 0")
        if "review_reason" not in cols:
            c.execute("ALTER TABLE sheets ADD COLUMN review_reason TEXT")
        # Unidades fabris — folha carimbada com a unidade do template
        # detetado; NULL = Trofa (folhas anteriores ao conceito).
        if "unidade_id" not in cols:
            c.execute("ALTER TABLE sheets ADD COLUMN unidade_id INTEGER REFERENCES unidades(id)")
        # Seed idempotente da sede.
        c.execute(
            "INSERT INTO unidades (nome) SELECT 'Trofa' WHERE NOT EXISTS "
            "(SELECT 1 FROM unidades WHERE nome = 'Trofa' COLLATE NOCASE)"
        )
        # R86 — production_rows ganhou m2 + nesting (Gemini fields).
        # R97 — production_rows ganhou qtd_metros (Soldline/Laser),
        # cesta_n (Expedição), inicio/fim (Gemini/Paragens).
        pr_cols = {r["name"] for r in c.execute("PRAGMA table_info(production_rows)").fetchall()}
        if "m2" not in pr_cols:
            c.execute("ALTER TABLE production_rows ADD COLUMN m2 REAL")
        if "nesting" not in pr_cols:
            c.execute("ALTER TABLE production_rows ADD COLUMN nesting TEXT")
        # R128 — kanban LASER: dbase/dtopo (diâmetro base/topo).
        if "dbase" not in pr_cols:
            c.execute("ALTER TABLE production_rows ADD COLUMN dbase INTEGER")
        if "dtopo" not in pr_cols:
            c.execute("ALTER TABLE production_rows ADD COLUMN dtopo INTEGER")
        for col, type_ in (
            ("qtd_metros", "REAL"),
            ("cesta_n", "TEXT"),
            ("inicio", "TEXT"),
            ("fim", "TEXT"),
            # rev00 — SUCATA (nº de peças sucatadas).
            ("sucata", "INTEGER"),
        ):
            if col not in pr_cols:
                c.execute(f"ALTER TABLE production_rows ADD COLUMN {col} {type_}")
        # Learning engine — `edits.source` distinguishes human corrections
        # ('human') from auto-snaps written by cross-check / operador_snap
        # ('system'). The success metric only counts 'human'.
        edit_cols = {r["name"] for r in c.execute("PRAGMA table_info(edits)").fetchall()}
        if "source" not in edit_cols:
            c.execute("ALTER TABLE edits ADD COLUMN source TEXT NOT NULL DEFAULT 'human'")
        # One-time backfill: edits on header.pernr / header.cod_maquina are
        # ALWAYS written by the system (operador_snap / codmaq fill), never
        # typed by a human. The 'source' column defaults to 'human', so pre-
        # existing rows were mislabelled — reclassify them so they don't
        # pollute the learning attractors / metric. Idempotent: after the
        # first pass these rows are 'system' and the WHERE no longer matches.
        c.execute(
            "UPDATE edits SET source = 'system' "
            "WHERE source = 'human' "
            "AND field_path IN ('header.pernr', 'header.cod_maquina')"
        )
        # R108 — shadow scoring engine output on the sheets row itself,
        # so /sheet/<id>/shadow-view can render side-by-side without an
        # extra join. Idempotent.
        if "shadow_scoring_json" not in cols:
            c.execute("ALTER TABLE sheets ADD COLUMN shadow_scoring_json TEXT")
        if "shadow_scored_at" not in cols:
            c.execute("ALTER TABLE sheets ADD COLUMN shadow_scored_at TIMESTAMP")
        # R257 — nome do CSV depositado na fábrica. Fonte de verdade para:
        # overwrite idempotente no re-depósito do validate, resolução de
        # colisões Operador_data (duas folhas do mesmo operador no mesmo dia
        # clobber-avam-se) e delete EXATO (o glob antigo *stem* apagava o CSV
        # da folha irmã "-1" sobrevivente).
        if "factory_csv_name" not in cols:
            c.execute("ALTER TABLE sheets ADD COLUMN factory_csv_name TEXT")
        # R253/F2 — triagem do soak: o critério "todas as divergências
        # triadas" do procedimento de flip só é auditável com carimbo.
        if "shadow_triaged_at" not in cols:
            c.execute("ALTER TABLE sheets ADD COLUMN shadow_triaged_at TIMESTAMP")
        if "shadow_triage_note" not in cols:
            c.execute("ALTER TABLE sheets ADD COLUMN shadow_triage_note TEXT")
        shadow_cols = {r["name"] for r in c.execute("PRAGMA table_info(shadow_runs)").fetchall()}
        if "cells_very_different" not in shadow_cols:
            c.execute("ALTER TABLE shadow_runs ADD COLUMN cells_very_different INTEGER")


def insert_sheet(
    image_path: str,
    *,
    page_hint: str | None = None,
    capture_group: str | None = None,
) -> int:
    """Cria a folha. rev00 — a captura guiada pode passar a pista de página
    ('F'/'V') e o token que liga as 2 fotos do mesmo kanban (`capture_group`).
    Ambos são opcionais (uploads desktop/legacy não os enviam)."""
    ph = (page_hint or "").strip().upper() or None
    if ph not in ("F", "V", None):
        ph = None
    cg = (capture_group or "").strip() or None
    with conn() as c:
        cur = c.execute(
            "INSERT INTO sheets (image_path, status, page_hint, capture_group) "
            "VALUES (?, 'pending', ?, ?)",
            (image_path, ph, cg),
        )
        return cur.lastrowid


def set_needs_review(sheet_id: int, reason: str) -> None:
    """rev00 — marca a folha para revisão humana (lado indeterminado ou o
    cross-check do mini-OCR discordou da pista de página). O depósito de CSV
    fica suspenso até o humano resolver no desktop."""
    with conn() as c:
        c.execute(
            "UPDATE sheets SET needs_review = 1, review_reason = ? WHERE id = ?",
            ((reason or "").strip() or "side_review", sheet_id),
        )


def clear_needs_review(sheet_id: int) -> None:
    """Limpa a flag de revisão (humano confirmou o lado no desktop)."""
    with conn() as c:
        c.execute(
            "UPDATE sheets SET needs_review = 0, review_reason = NULL WHERE id = ?",
            (sheet_id,),
        )


def set_page_hint(sheet_id: int, side: str) -> None:
    """rev00 — força a pista de página ('F'/'V') numa folha. Usado pela
    resolução humana de `needs_review` (banner "é frente ou verso?"): a seguir
    faz-se reprocess e o `run_pipeline` honra a pista como autoritativa."""
    s = (side or "").strip().upper()
    if s not in ("F", "V"):
        raise ValueError(f"side must be 'F' or 'V', got {side!r}")
    with conn() as c:
        c.execute("UPDATE sheets SET page_hint = ? WHERE id = ?", (s, sheet_id))


def update_extraction(
    sheet_id: int,
    raw_extraction: dict,
    dq_audit: dict,
    sheet_data: dict,
) -> None:
    with conn() as c:
        c.execute(
            """UPDATE sheets
               SET status = 'extracted',
                   raw_extraction = ?,
                   dq_audit = ?,
                   sheet_data = ?,
                   extracted_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (
                json.dumps(raw_extraction, ensure_ascii=False),
                json.dumps(dq_audit, ensure_ascii=False),
                json.dumps(sheet_data, ensure_ascii=False),
                sheet_id,
            ),
        )
        _sync_production_rows(c, sheet_id, sheet_data)


# R108 — shadow scoring engine persistence ---------------------------------

def start_shadow_run(sheet_id: int) -> int:
    """Open a shadow_runs row in 'running' state. Returns the run id."""
    with conn() as c:
        cur = c.execute(
            "INSERT INTO shadow_runs (sheet_id, status) VALUES (?, 'running')",
            (sheet_id,),
        )
        return cur.lastrowid


def finish_shadow_run(
    run_id: int,
    sheet_id: int,
    scoring: dict,
    cells_total: int,
    cells_snapped: int,
    cells_confirmed: int,
    cells_na: int,
    duration_ms: int,
) -> None:
    """Mark a shadow_runs row as done and persist the scoring on the sheet."""
    payload = json.dumps(scoring, ensure_ascii=False)
    try:
        cells_very_different = int(
            (scoring.get("summary") or {}).get("very_different", 0) or 0
        )
    except (TypeError, ValueError):
        cells_very_different = 0
    with conn() as c:
        c.execute(
            """UPDATE shadow_runs
               SET finished_at = CURRENT_TIMESTAMP,
                   cells_total = ?,
                   cells_snapped = ?,
                   cells_very_different = ?,
                   cells_confirmed = ?,
                   cells_na = ?,
                   duration_ms = ?,
                   status = 'done'
               WHERE id = ?""",
            (cells_total, cells_snapped, cells_very_different,
             cells_confirmed, cells_na, duration_ms, run_id),
        )
        c.execute(
            """UPDATE sheets
               SET shadow_scoring_json = ?,
                   shadow_scored_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (payload, sheet_id),
        )


def fail_shadow_run(run_id: int, error_message: str) -> None:
    """Mark a shadow_runs row as error; sheet shadow_scoring_json stays null."""
    with conn() as c:
        c.execute(
            """UPDATE shadow_runs
               SET finished_at = CURRENT_TIMESTAMP,
                   status = 'error',
                   error_message = ?
               WHERE id = ?""",
            (error_message[:2000], run_id),
        )


def mark_shadow_triaged(sheet_id: int, note: str | None = None) -> None:
    """R253/F2 — carimbo de triagem humana do soak (auditável)."""
    with conn() as c:
        c.execute(
            """UPDATE sheets
               SET shadow_triaged_at = CURRENT_TIMESTAMP,
                   shadow_triage_note = ?
               WHERE id = ?""",
            ((note or "").strip()[:2000] or None, sheet_id),
        )


def shadow_queue(limit: int = 500) -> list[dict]:
    """R253/F2 — folhas com sombra por triar (para /shadow-queue)."""
    with conn() as c:
        return [dict(r) for r in c.execute(
            """SELECT id, captured_at, status,
                      COALESCE(json_extract(sheet_data, '$.template_name'),
                               json_extract(raw_extraction,
                                            '$.template_name'))
                          AS template_name,
                      shadow_scored_at, shadow_triaged_at
               FROM sheets
               WHERE shadow_scoring_json IS NOT NULL
                 AND shadow_triaged_at IS NULL
               ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()]


# R110 — Qwen agent session persistence ------------------------------------

def save_qwen_session(
    user_message: str,
    envelope: dict,
    trigger: str = "manual",
) -> int:
    """Persiste uma sessão do agente Qwen. Devolve session_id.

    envelope é o output de qwen_agent.chat() — inclui reply, charts,
    proposed_rules e _meta com tools_used/duration_ms/status.
    """
    meta = envelope.get("_meta") or {}
    with conn() as c:
        cur = c.execute(
            """INSERT INTO qwen_sessions
               (trigger, user_message, reply, tools_used, tool_calls_count,
                rounds, charts_count, proposed_rules_count, duration_ms,
                status, error_message)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                trigger,
                user_message[:8000],
                (envelope.get("reply") or "")[:16000],
                json.dumps(meta.get("tools_used") or [], ensure_ascii=False),
                int(meta.get("tool_calls_count") or 0),
                int(meta.get("rounds") or 0),
                len(envelope.get("charts") or []),
                len(envelope.get("proposed_rules") or []),
                int(meta.get("duration_ms") or 0),
                meta.get("status") or "ok",
                (meta.get("error") or "")[:2000] or None,
            ),
        )
        return cur.lastrowid


def list_qwen_sessions(limit: int = 50) -> list[dict]:
    """Lista as últimas N sessões do agente para o painel de audit."""
    limit = max(1, min(limit, 200))
    with conn() as c:
        rows = c.execute(
            """SELECT id, trigger, user_message, tool_calls_count, rounds,
                      charts_count, proposed_rules_count, duration_ms,
                      status, created_at
               FROM qwen_sessions
               ORDER BY created_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


# R110.B — Charts -------------------------------------------------

def save_qwen_chart(
    spec: dict,
    title: str = "",
    narrative: str = "",
    session_id: int | None = None,
) -> int:
    with conn() as c:
        cur = c.execute(
            """INSERT INTO qwen_charts
               (title, chart_type, spec, narrative, session_id)
               VALUES (?, ?, ?, ?, ?)""",
            (
                title[:500] if title else "",
                spec.get("type") if isinstance(spec, dict) else None,
                json.dumps(spec, ensure_ascii=False),
                (narrative or "")[:4000],
                session_id,
            ),
        )
        return cur.lastrowid


def list_qwen_charts(limit: int = 30, pinned_only: bool = False) -> list[dict]:
    limit = max(1, min(limit, 200))
    where = "WHERE pinned = 1" if pinned_only else ""
    with conn() as c:
        rows = c.execute(
            f"""SELECT id, title, chart_type, spec, narrative, pinned, created_at
                FROM qwen_charts {where}
                ORDER BY pinned DESC, created_at DESC
                LIMIT ?""",
            (limit,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["spec"] = json.loads(d["spec"]) if d["spec"] else None
            except (json.JSONDecodeError, TypeError):
                pass
            out.append(d)
        return out


def set_chart_pinned(chart_id: int, pinned: bool) -> bool:
    with conn() as c:
        c.execute(
            "UPDATE qwen_charts SET pinned = ? WHERE id = ?",
            (1 if pinned else 0, chart_id),
        )
        return c.total_changes > 0


# R110.C — Proposals + policies -----------------------------------

def save_proposal(
    kind: str,
    payload: dict,
    justification: str,
    evidence: dict,
    risk_class: str,
    qwen_confidence: float = 0.0,
    estimated_impact: dict | None = None,
    session_id: int | None = None,
) -> int:
    with conn() as c:
        cur = c.execute(
            """INSERT INTO proposals
               (kind, payload, justification, evidence, risk_class,
                qwen_confidence, estimated_impact, session_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                kind,
                json.dumps(payload, ensure_ascii=False),
                (justification or "")[:8000],
                json.dumps(evidence or {}, ensure_ascii=False),
                risk_class,
                float(qwen_confidence),
                json.dumps(estimated_impact or {}, ensure_ascii=False),
                session_id,
            ),
        )
        proposal_id = cur.lastrowid

    # R117 — M2: auto-promover proposta se risk_class == "auto".
    # Lazy import para evitar risco de import circular (policy_engine
    # importa db). Try/except: se a promoção falhar, a proposta fica
    # 'pending' e alguém revê depois — não fazemos rollback do save.
    if risk_class == "auto" and proposal_id is not None:
        try:
            from app.pipeline import policy_engine
            policy_engine.promote_policy_from_proposal(proposal_id)
            logger.info(
                "Proposal %d auto-promoted (risk_class=auto)", proposal_id
            )
        except Exception:
            logger.exception(
                "auto-promote falhou para proposal_id=%d (fica pending)",
                proposal_id,
            )

    return proposal_id


def list_proposals(
    status: str = "",
    kind: str = "",
    limit: int = 50,
) -> list[dict]:
    limit = max(1, min(limit, 200))
    where: list[str] = []
    params: list = []
    if status:
        where.append("status = ?")
        params.append(status)
    if kind:
        where.append("kind = ?")
        params.append(kind)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)
    with conn() as c:
        rows = c.execute(
            f"""SELECT id, kind, payload, justification, evidence, risk_class,
                       qwen_confidence, estimated_impact, status,
                       shadow_eval_results, created_at, decided_at, decided_by
                FROM proposals
                {where_sql}
                ORDER BY created_at DESC, id DESC
                LIMIT ?""",
            params,
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for k in ("payload", "evidence", "estimated_impact", "shadow_eval_results"):
                v = d.get(k)
                if isinstance(v, str) and v:
                    try:
                        d[k] = json.loads(v)
                    except (json.JSONDecodeError, TypeError):
                        pass
            out.append(d)
        return out


def get_proposal(proposal_id: int) -> dict | None:
    rows = list_proposals(limit=200)
    for p in rows:
        if p["id"] == proposal_id:
            return p
    return None


def decide_proposal(
    proposal_id: int,
    status: str,
    decided_by: str = "human",
    eval_results: dict | None = None,
) -> bool:
    """status ∈ {accepted, rejected, auto_applied}."""
    with conn() as c:
        c.execute(
            """UPDATE proposals
               SET status = ?, decided_at = CURRENT_TIMESTAMP,
                   decided_by = ?,
                   shadow_eval_results = COALESCE(?, shadow_eval_results)
               WHERE id = ?""",
            (
                status, decided_by,
                json.dumps(eval_results, ensure_ascii=False) if eval_results else None,
                proposal_id,
            ),
        )
        return c.total_changes > 0


def save_policy_version(
    parent_version: int | None,
    yaml_blob: str,
    diff_summary: str = "",
    created_by: str = "manual",
    eval_results: dict | None = None,
) -> int:
    with conn() as c:
        cur = c.execute(
            """INSERT INTO policy_versions
               (parent_version, yaml_blob, diff_summary, created_by, eval_results)
               VALUES (?, ?, ?, ?, ?)""",
            (
                parent_version,
                yaml_blob,
                diff_summary[:4000] if diff_summary else "",
                created_by,
                json.dumps(eval_results, ensure_ascii=False) if eval_results else None,
            ),
        )
        return cur.lastrowid


def activate_policy_version(version: int) -> bool:
    with conn() as c:
        c.execute("UPDATE policy_versions SET active = 0")
        c.execute(
            "UPDATE policy_versions SET active = 1 WHERE version = ?",
            (version,),
        )
        return c.total_changes > 0


def get_active_policy_version() -> dict | None:
    with conn() as c:
        row = c.execute(
            "SELECT * FROM policy_versions WHERE active = 1 LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def list_policy_versions(limit: int = 30) -> list[dict]:
    limit = max(1, min(limit, 200))
    with conn() as c:
        rows = c.execute(
            """SELECT version, parent_version, diff_summary, created_by,
                      promoted_at, active
               FROM policy_versions
               ORDER BY version DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def save_template_overlay(
    template_name: str,
    change_type: str,
    payload: dict,
    proposal_id: int | None = None,
) -> int:
    with conn() as c:
        cur = c.execute(
            """INSERT INTO template_overlay
               (template_name, change_type, payload, proposal_id)
               VALUES (?, ?, ?, ?)""",
            (
                template_name,
                change_type,
                json.dumps(payload, ensure_ascii=False),
                proposal_id,
            ),
        )
        return cur.lastrowid


def get_active_template_overlays(template_name: str | None = None) -> list[dict]:
    where = ["active = 1"]
    params: list = []
    if template_name:
        where.append("template_name = ?")
        params.append(template_name)
    where_sql = "WHERE " + " AND ".join(where)
    with conn() as c:
        rows = c.execute(
            f"""SELECT id, template_name, change_type, payload, proposal_id,
                       promoted_at
                FROM template_overlay {where_sql}
                ORDER BY promoted_at""",
            params,
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if isinstance(d.get("payload"), str):
                try:
                    d["payload"] = json.loads(d["payload"])
                except (json.JSONDecodeError, TypeError):
                    pass
            out.append(d)
        return out


def log_circuit_breaker(
    event: str,
    metric_name: str = "",
    metric_value: float | None = None,
    baseline_value: float | None = None,
    action_taken: str = "",
) -> int:
    with conn() as c:
        cur = c.execute(
            """INSERT INTO circuit_breaker_log
               (event, metric_name, metric_value, baseline_value, action_taken)
               VALUES (?, ?, ?, ?, ?)""",
            (event, metric_name, metric_value, baseline_value, action_taken),
        )
        return cur.lastrowid


def get_sheet_template_name(sheet: dict | int) -> str:
    """Round 54 — return the canonical template name for a sheet.

    Backward-compatible with legacy sheets (pre-R54) that have no
    ``template_name`` in their sheet_data: falls back to inference
    via the setor_maquina header field. Final fallback is
    ``"bobine_formato"`` (the default template).

    Accepts either a sheet dict (output of get_sheet) or a sheet_id.
    """
    if isinstance(sheet, int):
        s = get_sheet(sheet)
        if s is None:
            return "bobine_formato"
    else:
        s = sheet

    sd_raw = s.get("sheet_data")
    if not sd_raw:
        return "bobine_formato"
    try:
        sd = json.loads(sd_raw) if isinstance(sd_raw, str) else sd_raw
    except (json.JSONDecodeError, TypeError):
        return "bobine_formato"

    # Explicit template_name persisted by R54+ uploads
    tname = sd.get("template_name")
    if tname:
        return str(tname)

    # Legacy sheets — infer from header.setor_maquina, with cod_maquina as a
    # last-resort discriminator for unambiguous Acabamento machines.
    header = sd.get("header") or {}
    setor = (header.get("setor_maquina") or "").strip()
    cod_maquina = (header.get("cod_maquina") or "").strip()
    if setor or cod_maquina:
        # Lazy import to avoid circular dependency
        from app.templates_registry import detect_template
        return detect_template(setor, cod_maquina=cod_maquina).name

    return "bobine_formato"


# ---- production_rows sync ----

def _parse_int(s: Any) -> int | None:
    if s is None or s == "":
        return None
    try:
        return int(re.sub(r"[^\d-]", "", str(s)))
    except (ValueError, TypeError):
        return None


def _parse_float(s: Any) -> float | None:
    if s is None or s == "":
        return None
    try:
        return float(str(s).replace(",", ".").strip())
    except (ValueError, TypeError):
        return None


def _parse_hours(s: Any) -> float | None:
    """`8:30` -> 8.5; `5,5` -> 5.5; `6` -> 6.0; `6 H` -> 6.0.
    Returns None for values <= 0 or > 24 (sanity guard against OCR garbage
    like '10100' from misreading '10:00')."""
    if not s:
        return None
    raw = str(s).strip().rstrip("Hh").strip()
    if ":" in raw:
        m = re.match(r"^(\d{1,2}):(\d{1,2})$", raw)
        if m:
            val = int(m.group(1)) + int(m.group(2)) / 60
        else:
            return None
    else:
        val = _parse_float(raw)
    if val is None or val <= 0 or val > 24:
        return None
    return val


def _parse_date_iso(s: Any) -> str | None:
    """`09-04-2026` or `9-4-2026` -> `2026-04-09`.

    R117 — B5: valida ranges (1≤m≤12, 1≤d≤31, 2000≤y≤2099) para alinhar
    com ``_normalize_data_pt_to_iso`` e evitar strings mal formadas
    (e.g. ``2026-13-45``) a chegar à camada de KPIs.
    """
    if not s:
        return None
    parts = re.split(r"[-/.\s]+", str(s).strip())
    if len(parts) != 3:
        return None
    try:
        d, m, y = (int(p) for p in parts)
        if y < 100:
            y += 2000
        # R117 — validar ranges (consistente com _normalize_data_pt_to_iso)
        if not (1 <= d <= 31 and 1 <= m <= 12 and 2000 <= y <= 2099):
            return None
        return f"{y:04d}-{m:02d}-{d:02d}"
    except (ValueError, TypeError):
        return None


def _sync_production_rows(c: sqlite3.Connection, sheet_id: int, sheet_data: dict) -> None:
    """Replace production_rows for this sheet. Called on every
    update_extraction() and apply_edit() to keep aggregates in sync.

    R117 — M3: agora é template-aware. Para templates sem produção
    (paragens), sai imediatamente após o DELETE — não há rows a
    alimentar. Para os restantes, o filter de "row vazio" usa o
    ``cross_check_fields`` do template em vez do hardcoded
    ``(pri, cliente, ov, of, modelo, qtd, lote)`` que excluía
    paragens e quase-todas as variantes Gemini.
    """
    c.execute("DELETE FROM production_rows WHERE sheet_id = ?", (sheet_id,))

    # R117 — M3: resolver TemplateSpec (lazy import para evitar potencial
    # circular). Preferimos ``sheet_data.template_name`` quando presente
    # (R54+); para folhas legadas, ``detect_template`` infere via
    # ``header.setor_maquina``.
    from app.templates_registry import (
        detect_template,
        get_template,
    )
    header = sheet_data.get("header", {}) or {}
    footer = sheet_data.get("footer", {}) or {}
    rows = sheet_data.get("rows", []) or []

    tname = sheet_data.get("template_name")
    if tname:
        tspec = get_template(tname)
    else:
        tspec = detect_template(
            (header.get("setor_maquina") or "").strip(),
            cod_maquina=(header.get("cod_maquina") or "").strip(),
        )

    # R117 — M3: paragens não têm production_rows; sair já.
    if not tspec.has_production_rows:
        return

    operador = (header.get("operador") or "").strip() or None
    sheet_date_raw = (header.get("data") or "").strip() or None
    sheet_iso = _parse_date_iso(sheet_date_raw)
    hours = _parse_hours(footer.get("horas_trabalhadas", ""))

    sheet_meta = c.execute(
        "SELECT captured_at, validated_at, status, operador FROM sheets WHERE id = ?",
        (sheet_id,),
    ).fetchone()
    if sheet_meta is None:
        return
    captured_at = sheet_meta["captured_at"]
    validated_at = sheet_meta["validated_at"]
    status = sheet_meta["status"]
    # If sheet was validated, sheets.operador is the dropdown choice and
    # is authoritative for KPIs. Otherwise fall back to OCR header.
    sheets_operador = (sheet_meta["operador"] or "").strip()
    if sheets_operador:
        operador = sheets_operador

    # Phase E fallback: if OCR couldn't parse header.data, use the
    # photo's captured_at date so the sheet still appears in date-grouped
    # dashboards instead of becoming a "ghost" row. captured_at is
    # SQLite TIMESTAMP "YYYY-MM-DD HH:MM:SS" — slice the date portion.
    if sheet_iso is None and captured_at:
        try:
            sheet_iso = str(captured_at)[:10]
            # Sanity: must look like YYYY-MM-DD
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", sheet_iso):
                sheet_iso = None
        except Exception:
            sheet_iso = None

    total_qty = sum((_parse_int(r.get("qtd")) or 0) for r in rows if isinstance(r, dict))

    # R117 — M3: filter template-aware. Se o template tem
    # ``cross_check_fields`` definidos (caso geral), usamos esses para
    # decidir se a linha é "vazia". Caso contrário, fallback ao
    # critério legado (pri/cliente/ov/of/modelo/qtd/lote).
    empty_check_fields: tuple[str, ...] = (
        tspec.cross_check_fields
        or ("pri", "cliente", "ov", "of", "modelo", "qtd", "lote")
    )

    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        # Skip totally empty rows (R117 — usa fields do template)
        if not any((row.get(k) or "").strip() for k in empty_check_fields):
            continue
        c.execute(
            """INSERT INTO production_rows
               (sheet_id, row_index,
                operador, sheet_date, sheet_iso_date, captured_at, validated_at,
                sheet_status, sheet_hours, sheet_total_qty,
                pri, cliente, ov, of, modelo, qtd,
                comp_mm, larg_mm, lote, coni, esp, lbase, ltopo,
                dbase, dtopo,
                m2, nesting,
                qtd_metros, cesta_n, inicio, fim,
                sucata)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                sheet_id, i,
                operador, sheet_date_raw, sheet_iso, captured_at, validated_at,
                status, hours, total_qty,
                (row.get("pri") or "").strip() or None,
                (row.get("cliente") or "").strip() or None,
                (row.get("ov") or "").strip() or None,
                (row.get("of") or "").strip() or None,
                (row.get("modelo") or "").strip() or None,
                _parse_int(row.get("qtd")),
                _parse_int(row.get("comp_mm")),
                _parse_int(row.get("larg_mm")),
                (row.get("lote") or "").strip() or None,
                canonical_ferramenta_or_raw(row.get("coni")) or None,
                _parse_float(row.get("esp")),
                _parse_int(row.get("lbase")),
                _parse_int(row.get("ltopo")),
                # R128 — kanban LASER (dbase/dtopo). NULL para outros templates.
                _parse_int(row.get("dbase")),
                _parse_int(row.get("dtopo")),
                # R86 — Gemini fields (m2 + nesting). Optional; bobine/laser
                # rows leave these NULL.
                _parse_float(row.get("m2")),
                (row.get("nesting") or "").strip() or None,
                # R97 — qtd_metros (Soldline/Laser), cesta_n (Expedição),
                # inicio/fim (Gemini/Paragens). All optional.
                _parse_float(row.get("qtd_metros")),
                (row.get("cesta_n") or "").strip() or None,
                (row.get("inicio") or "").strip() or None,
                (row.get("fim") or "").strip() or None,
                # rev00 — SUCATA (nº de peças sucatadas). NULL se vazio.
                _parse_int(row.get("sucata")),
            ),
        )


def update_error(sheet_id: int, message: str) -> None:
    with conn() as c:
        c.execute(
            "UPDATE sheets SET status = 'error', error_message = ? WHERE id = ?",
            (message, sheet_id),
        )


# R71 — helpers for the background OCR queue (status transitions + recovery)

def update_status(sheet_id: int, status: str) -> None:
    """Set the sheet's status without touching other fields. Used by the
    background queue to flip sheets back to 'pending' on reprocess."""
    with conn() as c:
        c.execute(
            "UPDATE sheets SET status = ? WHERE id = ?",
            (status, sheet_id),
        )


def clear_error(sheet_id: int) -> None:
    """Wipe the error_message column. Pair with update_status('pending')
    when re-enqueueing a previously-errored sheet."""
    with conn() as c:
        c.execute(
            "UPDATE sheets SET error_message = NULL WHERE id = ?",
            (sheet_id,),
        )


def list_stuck_pending(older_than_seconds: int = 10) -> list[int]:
    """Return sheet ids that have been in status='pending' for longer
    than ``older_than_seconds``. Used at startup to re-enqueue uploads
    that were in-flight when the previous process crashed/restarted.

    Excludes very-recent rows (might be racing with this very startup).
    """
    with conn() as c:
        rows = c.execute(
            """
            SELECT id FROM sheets
             WHERE status = 'pending'
               AND captured_at < datetime('now', ?)
             ORDER BY id ASC
            """,
            (f"-{int(older_than_seconds)} seconds",),
        ).fetchall()
    return [r["id"] for r in rows]


def get_sheet(sheet_id: int) -> dict | None:
    with conn() as c:
        row = c.execute("SELECT * FROM sheets WHERE id = ?", (sheet_id,)).fetchone()
    if row is None:
        return None
    out = dict(row)
    for json_field in ("sheet_data", "raw_extraction", "dq_audit"):
        if out.get(json_field):
            out[json_field] = json.loads(out[json_field])
    return out


def list_sheets(status: str | None = None, limit: int = 100) -> list[dict]:
    # Round 54 — also pull template_name + setor_maquina for badge rendering.
    # R89 — pull `sheet_operador` (header.operador extracted by OCR) as
    # fallback for the queue UI when `sheets.operador` is still NULL
    # (sheets.operador only filled on validation).
    # json_extract is sqlite native + indexed via the sheet_data BLOB so no
    # extra cost vs the legacy SELECT.
    sql = """
        SELECT id, captured_at, status, operador, validated_at, image_path,
               needs_review, review_reason, page_hint, capture_group,
               unidade_id,
               json_extract(sheet_data, '$.header.operador') AS sheet_operador,
               COALESCE(json_extract(sheet_data, '$.template_name'),
                        json_extract(raw_extraction, '$.template_name')) AS template_name,
               json_extract(sheet_data, '$.header.setor_maquina') AS setor_maquina
        FROM sheets
    """
    params: tuple = ()
    if status:
        sql += " WHERE status = ?"
        params = (status,)
    sql += " ORDER BY captured_at DESC LIMIT ?"
    params = (*params, limit)
    with conn() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def _normalize_data_pt_to_iso(s: str | None) -> str | None:
    """R90 — Convert various PT/EU date formats to ISO YYYY-MM-DD.

    Handles formats commonly written by operators on kanbans:
      - DD-MM-YYYY  (canonical)
      - DD-MM-YY    (2-digit year -> assume 20YY)
      - DD.MM.YYYY  (dot separator)
      - DD/MM/YYYY  (slash separator)
      - D-M-YYYY    (without zero-pad)
      - DD-MM-YYYY. (trailing punctuation)

    Returns None when unrecognised so callers can treat as no-match.
    """
    if not s:
        return None
    import re as _re
    cleaned = str(s).strip().rstrip(".").rstrip("/").rstrip("-")
    m = _re.match(r"^(\d{1,2})[-./](\d{1,2})[-./](\d{2,4})$", cleaned)
    if not m:
        return None
    d, mo, y = m.group(1), m.group(2), m.group(3)
    if len(y) == 2:
        y = "20" + y  # assume 21st century (2000-2099)
    try:
        d_i, m_i, y_i = int(d), int(mo), int(y)
        if not (1 <= d_i <= 31 and 1 <= m_i <= 12 and 2000 <= y_i <= 2099):
            return None
        return f"{y_i:04d}-{m_i:02d}-{d_i:02d}"
    except ValueError:
        return None


def _normalize_operador(s: str | None) -> str:
    """Normalize operador for case+accent-insensitive comparison.
    NFKD strip combining marks + uppercase + trim.
    'JúLIO LIMA' / 'Júlio Lima' / 'JULIO LIMA' all → 'JULIO LIMA'.
    """
    if not s:
        return ""
    import unicodedata
    decomposed = unicodedata.normalize("NFKD", str(s))
    no_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return no_marks.upper().strip()


def list_distinct_operadores() -> list[str]:
    """Return distinct operador values across extracted/validated sheets,
    deduped by case+accent-normalized form. The displayed string is the
    most common case found (so 'JÚLIO LIMA' beats 'júlio lima').
    """
    sql = """
        SELECT json_extract(sheet_data, '$.header.operador') AS op,
               COUNT(*) AS n
        FROM sheets
        WHERE sheet_data IS NOT NULL
          AND status IN ('extracted', 'validated')
          AND json_extract(sheet_data, '$.header.operador') IS NOT NULL
          AND TRIM(json_extract(sheet_data, '$.header.operador')) <> ''
        GROUP BY op
        ORDER BY n DESC
    """
    with conn() as c:
        rows = c.execute(sql).fetchall()
    # Group by normalized key, keep the most common literal form
    by_norm: dict[str, tuple[str, int]] = {}
    for r in rows:
        op = (r["op"] or "").strip()
        if not op:
            continue
        norm = _normalize_operador(op)
        if not norm:
            continue
        if norm not in by_norm or r["n"] > by_norm[norm][1]:
            by_norm[norm] = (op, r["n"])
    return sorted({v[0] for v in by_norm.values()}, key=_normalize_operador)


def apply_edit(
    sheet_id: int,
    field_path: str,
    new_value: str,
    source: str = "human",
) -> tuple[str, str]:
    """Apply edit + insert audit row. Returns (old_value, new_value).

    Mutates ``sheet_data`` JSON in place; preserves ``raw_extraction``.

    ``source`` is ``'human'`` for operator corrections (the default) and
    ``'system'`` for auto-snaps written by cross-check / operador_snap.
    The learning engine's success metric only counts ``'human'`` edits.
    """
    with conn() as c:
        row = c.execute(
            "SELECT sheet_data FROM sheets WHERE id = ?", (sheet_id,)
        ).fetchone()
        if row is None or not row["sheet_data"]:
            raise ValueError(f"Sheet {sheet_id} has no extraction yet")
        data = json.loads(row["sheet_data"])
        old = _get_by_path(data, field_path) or ""
        _set_by_path(data, field_path, new_value)
        c.execute(
            "UPDATE sheets SET sheet_data = ? WHERE id = ?",
            (json.dumps(data, ensure_ascii=False), sheet_id),
        )
        c.execute(
            "INSERT INTO edits (sheet_id, field_path, old_value, new_value, source) "
            "VALUES (?, ?, ?, ?, ?)",
            (sheet_id, field_path, old, new_value, source),
        )
        # Re-sync production_rows so aggregations reflect the edit
        _sync_production_rows(c, sheet_id, data)
    return old, new_value


def apply_edits_batch(
    sheet_id: int,
    edits: list[tuple[str, str]],
    source: str = "human",
) -> list[tuple[str, str, str]]:
    """Apply multiple edits to one sheet in a single transaction.

    Preserves the same audit semantics as ``apply_edit`` while paying the
    JSON update + production_rows sync cost once per sheet.
    """
    if not edits:
        return []
    applied: list[tuple[str, str, str]] = []
    with conn() as c:
        row = c.execute(
            "SELECT sheet_data FROM sheets WHERE id = ?", (sheet_id,)
        ).fetchone()
        if row is None or not row["sheet_data"]:
            raise ValueError(f"Sheet {sheet_id} has no extraction yet")
        data = json.loads(row["sheet_data"])
        for field_path, new_value in edits:
            old = _get_by_path(data, field_path) or ""
            _set_by_path(data, field_path, new_value)
            applied.append((field_path, old, new_value))
        c.execute(
            "UPDATE sheets SET sheet_data = ? WHERE id = ?",
            (json.dumps(data, ensure_ascii=False), sheet_id),
        )
        c.executemany(
            "INSERT INTO edits (sheet_id, field_path, old_value, new_value, source) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (sheet_id, field_path, old, new_value, source)
                for field_path, old, new_value in applied
            ],
        )
        _sync_production_rows(c, sheet_id, data)
    return applied


def add_row(sheet_id: int, *, source: str = "human") -> int:
    """R136 — append an empty row to ``sheet_data["rows"]``. Returns the new
    row index.

    Mirrors ``apply_edit``: one transaction that persists ``sheet_data``,
    logs an audit row (EN 1090 / ISO 9001) and re-syncs ``production_rows``
    (the blank row is filtered out of aggregates until the operator fills it).

    Appends at the END deliberately — that never shifts the index of any
    existing ``rows[i].field`` edit path.
    """
    with conn() as c:
        row = c.execute(
            "SELECT sheet_data FROM sheets WHERE id = ?", (sheet_id,)
        ).fetchone()
        if row is None or not row["sheet_data"]:
            raise ValueError(f"Sheet {sheet_id} has no extraction yet")
        data = json.loads(row["sheet_data"])
        rows = data.setdefault("rows", [])
        if len(rows) > _MAX_ROW_INDEX:
            raise ValueError(f"sheet already has the maximum {_MAX_ROW_INDEX} rows")
        rows.append({})
        new_idx = len(rows) - 1
        c.execute(
            "UPDATE sheets SET sheet_data = ? WHERE id = ?",
            (json.dumps(data, ensure_ascii=False), sheet_id),
        )
        c.execute(
            "INSERT INTO edits (sheet_id, field_path, old_value, new_value, source) "
            "VALUES (?, ?, ?, ?, ?)",
            (sheet_id, f"rows[{new_idx}]", "", "(linha adicionada)", source),
        )
        _sync_production_rows(c, sheet_id, data)
    return new_idx


def delete_row(sheet_id: int, row_index: int, *, source: str = "human") -> dict:
    """R136 — remove ``sheet_data["rows"][row_index]``. Returns the removed
    row dict.

    Shifts the indices of subsequent rows. The removed content is stored
    verbatim in the ``edits`` audit row so it stays recoverable. Re-syncs
    ``production_rows``.
    """
    with conn() as c:
        row = c.execute(
            "SELECT sheet_data FROM sheets WHERE id = ?", (sheet_id,)
        ).fetchone()
        if row is None or not row["sheet_data"]:
            raise ValueError(f"Sheet {sheet_id} has no extraction yet")
        data = json.loads(row["sheet_data"])
        rows = data.get("rows", []) or []
        if row_index < 0 or row_index >= len(rows):
            raise ValueError(
                f"row index {row_index} out of range [0, {len(rows) - 1}]"
            )
        removed = rows.pop(row_index)
        data["rows"] = rows
        c.execute(
            "UPDATE sheets SET sheet_data = ? WHERE id = ?",
            (json.dumps(data, ensure_ascii=False), sheet_id),
        )
        c.execute(
            "INSERT INTO edits (sheet_id, field_path, old_value, new_value, source) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                sheet_id,
                f"rows[{row_index}]",
                json.dumps(removed, ensure_ascii=False),
                "(linha removida)",
                source,
            ),
        )
        _sync_production_rows(c, sheet_id, data)
    return removed


def validate_sheet(sheet_id: int, operador: str) -> None:
    """Mark sheet validated. Dropdown ``operador`` overrides whatever
    OCR / manual edits put in production_rows — the dropdown is the
    KPI source of truth (``sheets.operador``).

    R131: o dropdown passou a ser dinâmico (todos os snames da
    ListaColaboradores.xlsx via `_get_operadores()`) e vem pré-seleccionado
    com o resultado do `snap_operador` (header.operador canónico). O
    "always overwrite" continua correcto — o operador pode SEMPRE corrigir
    o nome pré-seleccionado se o snap errou, ficando essa escolha
    autoritativa para KPI groupings.
    """
    with conn() as c:
        c.execute(
            """UPDATE sheets
               SET status = 'validated',
                   operador = ?,
                   validated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (operador, sheet_id),
        )
        # Always overwrite — dropdown is authoritative for KPI groupings.
        # Without this, a mis-OCR of operador name silently splits one
        # operator into two in dashboard aggregations.
        c.execute(
            """UPDATE production_rows
               SET sheet_status = 'validated',
                   validated_at = CURRENT_TIMESTAMP,
                   operador = ?
               WHERE sheet_id = ?""",
            (operador, sheet_id),
        )


def set_image_rotation(sheet_id: int, rotation: int) -> int:
    """Round 34c — store user's manual rotation override (CW quarter-turns).

    rotation is normalised to {0, 90, 180, 270}. Returns the stored value.
    """
    norm = rotation % 360
    norm = (norm // 90) * 90
    with conn() as c:
        c.execute(
            "UPDATE sheets SET image_rotation = ? WHERE id = ?",
            (norm, sheet_id),
        )
    return norm


def factory_csv_filename(sheet: dict) -> str:
    """R257 — nome canónico base do CSV da fábrica para uma folha.

    Prefere operador+data do sheet_data (``JulioLima_2026.04.15.csv``) para
    as linhas de log do validador ficarem legíveis; cai no stem da imagem.
    (Era ``main._factory_csv_filename``; vive aqui para o delete_sheet poder
    calcular o MESMO nome sem import circular.)
    """
    data = sheet.get("sheet_data") or {}
    h = data.get("header", {}) or {}
    operador = (h.get("operador") or "").strip().title().replace(" ", "")
    data_str = (h.get("data") or "").strip()
    if operador and data_str:
        # 15-04-2026 → 2026.04.15
        parts = data_str.split("-")
        if len(parts) == 3:
            iso = f"{parts[2]}.{parts[1]}.{parts[0]}"
            return f"{operador}_{iso}.csv"
    return f"{Path(sheet['image_path']).stem}.csv"


def set_factory_csv_name(sheet_id: int, name: str) -> None:
    """R257 — regista o nome do CSV depositado (ver migração em init_db)."""
    with conn() as c:
        c.execute("UPDATE sheets SET factory_csv_name = ? WHERE id = ?",
                  (name, sheet_id))


def get_factory_csv_claim(name: str) -> int | None:
    """R257 — devolve o sheet_id que reclamou este nome de CSV, ou None."""
    with conn() as c:
        row = c.execute(
            "SELECT id FROM sheets WHERE factory_csv_name = ? LIMIT 1",
            (name,),
        ).fetchone()
        return row["id"] if row else None


def delete_sheet(sheet_id: int) -> dict:
    """Round 34 — hard delete a sheet + cascade clean.

    Removes:
    - sheets row
    - edits rows (FK)
    - production_rows rows (FK CASCADE in schema)
    - image file in data/images/
    - cross-check JSON in C:\\kanban\\nifruka\\03_Cross_Check\\
    - factory CSV in 02_Dados_Extraidos/csv/ or imported/

    Returns dict with what was removed for the caller's confirmation.
    """
    removed: dict = {"sheet_id": sheet_id, "image": None, "cross_check": None,
                     "factory_csv": None, "edits": 0, "production_rows": 0}

    # 1. Get image_path BEFORE deleting (otherwise we lose the link)
    sheet = get_sheet(sheet_id)
    if sheet is None:
        raise ValueError(f"Sheet {sheet_id} not found")
    image_rel = sheet.get("image_path")

    # 2. DB cascade — explicit deletes (production_rows has ON DELETE CASCADE
    #    but edits doesn't, so do both manually for clarity)
    with conn() as c:
        n_edits = c.execute("SELECT COUNT(*) AS n FROM edits WHERE sheet_id = ?",
                            (sheet_id,)).fetchone()["n"]
        n_pr = c.execute("SELECT COUNT(*) AS n FROM production_rows WHERE sheet_id = ?",
                         (sheet_id,)).fetchone()["n"]
        c.execute("DELETE FROM edits WHERE sheet_id = ?", (sheet_id,))
        c.execute("DELETE FROM production_rows WHERE sheet_id = ?", (sheet_id,))
        c.execute("DELETE FROM sheets WHERE id = ?", (sheet_id,))
    removed["edits"] = n_edits
    removed["production_rows"] = n_pr

    # 3. Delete image file
    if image_rel:
        repo_root = Path(__file__).resolve().parents[3]
        img_path = repo_root / "data" / image_rel
        if img_path.exists():
            try:
                img_path.unlink()
                removed["image"] = str(img_path)
            except OSError:
                pass

    # 4. Delete cross-check JSON + update aggregates
    try:
        from app.cross_check.storage import remove_sheet_cross_check
        remove_sheet_cross_check(sheet_id)
        removed["cross_check"] = "ok"
    except Exception:
        pass

    # 5. Delete factory CSV
    # R118 — usar resolve_kanban_path para cair em repo-local quando o disco
    # C:\kanban\ não existe (laptop dev sem .env). Lazy import para evitar
    # ciclo se config.py vier a importar de db.
    from app.config import resolve_kanban_path
    factory_csv_dir = resolve_kanban_path(
        "FACTORY_CSV_DIR",
        r"C:\kanban\nifruka\02_Dados_Extraidos\csv",
        "kanban_refs/02_Dados_Extraidos/csv",
    )
    if factory_csv_dir.exists():
        # R257 — apagar pelo nome EXATO (registado no depósito; fallback ao
        # nome calculado + stem para folhas pré-R257 sem registo). O glob
        # antigo rglob(f"*{stem}*.csv") apanhava por substring o CSV da folha
        # irmã sobrevivente (ex.: apagar ...04.15 levava o ...04.15-1.csv).
        # O importador da fábrica move CSVs consumidos para imported/ —
        # procurar lá também.
        names = []
        if sheet.get("factory_csv_name"):
            names.append(sheet["factory_csv_name"])
        else:
            names.append(factory_csv_filename(sheet))
            if image_rel:
                names.append(f"{Path(image_rel).stem}.csv")
        for name in dict.fromkeys(names):
            for csv_path in (factory_csv_dir / name,
                             factory_csv_dir / "imported" / name):
                if csv_path.exists():
                    try:
                        csv_path.unlink()
                        removed["factory_csv"] = str(csv_path)
                    except OSError:
                        pass

    return removed


# ---------------------------------------------------------------------------
# Unidades fabris + templates de kanban registados (registo por unidade)
# ---------------------------------------------------------------------------

def list_unidades(only_ativo: bool = True) -> list[dict]:
    sql = "SELECT id, nome, ativo, created_at FROM unidades"
    if only_ativo:
        sql += " WHERE ativo = 1"
    sql += " ORDER BY nome COLLATE NOCASE"
    with conn() as c:
        return [dict(r) for r in c.execute(sql).fetchall()]


def create_unidade(nome: str) -> int:
    nome = (nome or "").strip()
    if not nome:
        raise ValueError("nome da unidade vazio")
    with conn() as c:
        cur = c.execute("INSERT INTO unidades (nome) VALUES (?)", (nome,))
        return int(cur.lastrowid or 0)


def set_unidade_ativo(unidade_id: int, ativo: bool) -> None:
    with conn() as c:
        c.execute("UPDATE unidades SET ativo = ? WHERE id = ?",
                  (1 if ativo else 0, unidade_id))


def trofa_unidade_id() -> int:
    """Id da sede (seed do init_db). As folhas com unidade_id NULL são dela."""
    with conn() as c:
        r = c.execute(
            "SELECT id FROM unidades WHERE nome = 'Trofa' COLLATE NOCASE"
        ).fetchone()
        return int(r["id"]) if r else 1


def set_sheet_unidade(sheet_id: int, unidade_id: int | None) -> None:
    with conn() as c:
        c.execute("UPDATE sheets SET unidade_id = ? WHERE id = ?",
                  (unidade_id, sheet_id))


def insert_kanban_template(
    name: str, unidade_id: int, spec_json: str,
    image_path: str | None = None, status: str = "draft",
) -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO kanban_templates (name, unidade_id, spec_json, "
            "image_path, status) VALUES (?,?,?,?,?)",
            (name, unidade_id, spec_json, image_path, status))
        return int(cur.lastrowid or 0)


def get_kanban_template(template_id: int) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM kanban_templates WHERE id = ?",
                      (template_id,)).fetchone()
        return dict(r) if r else None


def list_kanban_templates(status: str | None = None) -> list[dict]:
    sql = ("SELECT kt.*, u.nome AS unidade_nome FROM kanban_templates kt "
           "JOIN unidades u ON u.id = kt.unidade_id")
    args: tuple = ()
    if status:
        sql += " WHERE kt.status = ?"
        args = (status,)
    sql += " ORDER BY kt.created_at DESC"
    with conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def update_kanban_template_spec(template_id: int, spec_json: str) -> None:
    with conn() as c:
        c.execute("UPDATE kanban_templates SET spec_json = ? WHERE id = ?",
                  (spec_json, template_id))


def set_kanban_template_status(
    template_id: int, status: str, discovery_json: str | None = None,
) -> None:
    with conn() as c:
        if discovery_json is not None:
            c.execute(
                "UPDATE kanban_templates SET status = ?, discovery_json = ?, "
                "activated_at = CASE WHEN ? = 'ativo' THEN CURRENT_TIMESTAMP "
                "ELSE activated_at END WHERE id = ?",
                (status, discovery_json, status, template_id))
        else:
            c.execute(
                "UPDATE kanban_templates SET status = ?, activated_at = "
                "CASE WHEN ? = 'ativo' THEN CURRENT_TIMESTAMP ELSE "
                "activated_at END WHERE id = ?",
                (status, status, template_id))


def delete_kanban_template(template_id: int) -> None:
    with conn() as c:
        c.execute("DELETE FROM kanban_templates WHERE id = ?", (template_id,))


def count_sheets_for_template(template_name: str) -> int:
    """Folhas processadas com este template — bloqueia o delete (audit
    trail EN1090: um template com folhas só pode ser desativado)."""
    with conn() as c:
        r = c.execute(
            "SELECT COUNT(*) AS n FROM sheets "
            "WHERE json_extract(sheet_data, '$.template_name') = ?",
            (template_name,)).fetchone()
        return int(r["n"] or 0)


def list_distinct_setores() -> list[str]:
    """Return distinct setor_maquina values across extracted/validated sheets."""
    sql = """
        SELECT DISTINCT TRIM(json_extract(sheet_data, '$.header.setor_maquina')) AS setor
          FROM sheets
         WHERE sheet_data IS NOT NULL
           AND status IN ('extracted', 'validated')
           AND json_extract(sheet_data, '$.header.setor_maquina') IS NOT NULL
           AND TRIM(json_extract(sheet_data, '$.header.setor_maquina')) <> ''
         ORDER BY setor
    """
    with conn() as c:
        return [r["setor"] for r in c.execute(sql).fetchall() if r["setor"]]


def list_sheets_filtered(
    operador: str | None = None,
    data_iso: str | None = None,
    captured_iso: str | None = None,
    setor: str | None = None,
    of: str | None = None,
    statuses: tuple[str, ...] = ("extracted", "validated"),
    unidade: int | None = None,
) -> list[dict]:
    """Round 34/36 — multi-filter: operador AND data AND setor AND of (all optional).

    `data_iso` is YYYY-MM-DD; matches against header.data after format
    normalisation (header.data is DD-MM-YYYY in our schema).

    R128 — `captured_iso` is YYYY-MM-DD; matches against the date portion
    of `sheets.captured_at` (TIMESTAMP set by the backend on upload).
    Distinct semantically from `data_iso`: the latter is what the operator
    wrote on the sheet; this is when the upload happened.

    `of` filters sheets that have at least one production row with that OF
    (exact match against `production_rows.of`).

    Task C E4 — `unidade` filtra pela unidade fabril da folha: usa
    `sheets.unidade_id` quando carimbado; folhas antigas (NULL) resolvem
    pelo template registado (kanban_templates) ou caem na sede (Trofa).

    Filters in Python after the SQL pull because operador uses
    case+accent-insensitive normalisation that SQLite UPPER doesn't handle.
    """
    placeholders = ",".join("?" * len(statuses))
    sql = f"""
        SELECT id, captured_at, status, operador, validated_at, image_path,
               needs_review, review_reason, page_hint, capture_group,
               unidade_id,
               json_extract(sheet_data, '$.header.operador') AS sheet_operador,
               json_extract(sheet_data, '$.header.data') AS sheet_data_str,
               json_extract(sheet_data, '$.header.setor_maquina') AS sheet_setor,
               COALESCE(json_extract(sheet_data, '$.template_name'),
                        json_extract(raw_extraction, '$.template_name')) AS template_name,
               json_extract(sheet_data, '$.header.setor_maquina') AS setor_maquina
          FROM sheets
         WHERE status IN ({placeholders})
           AND sheet_data IS NOT NULL
         ORDER BY captured_at ASC
    """
    target_op = _normalize_operador(operador) if operador else None
    target_setor = setor.upper().strip() if setor else None
    target_of = of.strip() if of else None

    tpl_unidade: dict[str, int] = {}
    trofa_id: int | None = None
    with conn() as c:
        rows = c.execute(sql, statuses).fetchall()
        if unidade is not None:
            tpl_unidade = {
                r["name"]: r["unidade_id"]
                for r in c.execute(
                    "SELECT name, unidade_id FROM kanban_templates"
                ).fetchall()
            }
            t = c.execute(
                "SELECT id FROM unidades WHERE nome = 'Trofa' COLLATE NOCASE"
            ).fetchone()
            trofa_id = int(t["id"]) if t else 1
        of_sheet_ids: set[int] | None = None
        if target_of:
            of_sheet_ids = {
                r["sheet_id"]
                for r in c.execute(
                    "SELECT DISTINCT sheet_id FROM production_rows WHERE of = ?",
                    (target_of,),
                ).fetchall()
            }

    out = []
    for r in rows:
        d = dict(r)
        # operador filter (case+accent)
        if target_op and _normalize_operador(d.get("sheet_operador")) != target_op:
            continue
        # setor filter (uppercase exact)
        if target_setor:
            setor_v = (d.get("sheet_setor") or "").upper().strip()
            if setor_v != target_setor:
                continue
        # data filter — header.data has many operator-written formats
        # (DD-MM-YYYY, DD-MM-YY, DD.MM.YYYY, DD/MM/YYYY, D-M-YYYY, ...).
        # R90: use the robust normalizer so all variants of the same date
        # match the requested ISO filter.
        if data_iso:
            iso = _normalize_data_pt_to_iso(d.get("sheet_data_str"))
            if iso != data_iso:
                continue
        # R128 — captured filter: dia da TIMESTAMP do upload. captured_at
        # vem como datetime ou string consoante o driver SQLite/configuração;
        # tratamos os dois.
        if captured_iso:
            cap_raw = d.get("captured_at")
            cap = (cap_raw.isoformat() if hasattr(cap_raw, "isoformat")
                   else str(cap_raw or ""))[:10]
            if cap != captured_iso:
                continue
        # of filter — sheet must have ≥ 1 row with this OF
        if of_sheet_ids is not None and d["id"] not in of_sheet_ids:
            continue
        # unidade filter — carimbo ∥ template registado ∥ Trofa (NULL)
        if unidade is not None:
            uid = d.get("unidade_id")
            if uid is None:
                uid = tpl_unidade.get(d.get("template_name") or "") or trofa_id
            if uid != unidade:
                continue
        out.append(d)
    return out


# ---- field_path resolution ----

def _get_by_path(data: dict, path: str) -> Any:
    """``rows[3].modelo`` -> data['rows'][3]['modelo']"""
    section, _, rest = path.partition(".")
    if section in ("header", "footer"):
        return data.get(section, {}).get(rest, "")
    if section.startswith("rows["):
        idx = int(section[5:-1])
        rows = data.get("rows", [])
        if idx >= len(rows):
            return ""
        return rows[idx].get(rest, "")
    raise ValueError(f"unrecognised path: {path}")


_MAX_ROW_INDEX = 100  # caps malicious / buggy field_path payloads (B5)


def _set_by_path(data: dict, path: str, value: str) -> None:
    section, _, rest = path.partition(".")
    if section in ("header", "footer"):
        data.setdefault(section, {})[rest] = value
        return
    if section.startswith("rows["):
        try:
            idx = int(section[5:-1])
        except ValueError as e:
            raise ValueError(f"invalid row index in path: {path}") from e
        if idx < 0 or idx > _MAX_ROW_INDEX:
            raise ValueError(
                f"row index {idx} out of range [0, {_MAX_ROW_INDEX}]"
            )
        rows = data.setdefault("rows", [])
        while len(rows) <= idx:
            rows.append({})
        rows[idx][rest] = value
        return
    raise ValueError(f"unrecognised path: {path}")

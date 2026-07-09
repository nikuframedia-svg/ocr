"""FastAPI MVP — capture → review → dashboard.

Run:
    cd <repo-root>
    .venv/Scripts/python.exe -m uvicorn backend.app.web.main:app \\
        --reload --host 0.0.0.0 --port 8080
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import re
import secrets
import shutil
import sqlite3  # Task C — erros de unicidade nas unidades fabris
import sys
import threading
import time  # R224 — timing por etapa (profiling)
import traceback
from pathlib import Path
from urllib.parse import quote_plus

import httpx  # R120 — endpoint /admin/qwen-tools-test

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

_REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO))

# Ensure backend app importable
sys.path.insert(0, str(_REPO / "backend"))

from app import kernel
from app.config import get_settings
from app.web import attractors, db, export, kpi_params, kpis, llm_assistant, ocr_queue, ocr_runner, pdf_ingest, template_store
from app.cross_check import (
    cross_check_sheet,
    get_watcher,
    load_summary,
    load_to_analisar,
    store_cross_check,
)
from app.cross_check import ref_importer
from app.dq.machines import resolve_machine_from_setor
from app.dq.operador_snap import snap_operador
from app.learning import (
    materialize as learning_materialize,
    metrics as learning_metrics,
    scheduler as learning_scheduler,
    store as learning_store,
)

# ----- App + paths -----
_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"
_DATA_DIR = _REPO / "data"
_IMAGES_DIR = _DATA_DIR / "images"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_IMAGES_DIR.mkdir(parents=True, exist_ok=True)

# R70 — Operadores conhecidos para fallback defensivo. R131: a fonte de
# verdade passou a ser ListaColaboradores.xlsx via `_get_operadores()`;
# este tuplo só é usado quando refs ainda não foi carregado (boot frio
# ou ficheiro ausente).
_OPERADORES_FALLBACK = ("AUGUSTO MONTEIRO", "JÚLIO LIMA", "VITOR CARVALHO")


def _get_operadores() -> tuple[str, ...]:
    """R131 — Lista dinâmica de operadores conhecidos vinda da
    ListaColaboradores.xlsx (`refs["colaboradores"]`). Ordenada A→Z.

    Fonte: `_mine_colaboradores` em [ref_watcher.py](backend/app/cross_check/ref_watcher.py)
    devolve `{cod: {sname (UPPER ASCII), pernr}}`. Aqui extraímos os
    snames únicos para drive dos dropdowns de validação e filtros.

    Fallback aos 3 hardcoded R70 (`_OPERADORES_FALLBACK`) quando refs
    ainda não foi carregado ou está vazio — defensivo para boot frio.
    """
    try:
        refs = get_watcher().get_refs()
        colabs = refs.get("colaboradores") or {}
        snames = sorted({
            (c.get("sname") or "").strip()
            for c in colabs.values()
            if c.get("sname")
        })
        snames = tuple(s for s in snames if s)
        if snames:
            return snames
    except Exception:
        pass
    return _OPERADORES_FALLBACK

ROW_FIELDS = (
    "pri", "cliente", "ov", "of", "modelo", "qtd",
    "comp_mm", "larg_mm", "lote", "coni", "esp", "lbase", "ltopo",
)
HEADER_FIELDS = ("operador", "n_operador", "setor_maquina", "cod_maquina", "data")
FOOTER_FIELDS = ("colunas_produzidas", "horas_trabalhadas")


# Round 54 — per-sheet template context. Looks up the TemplateSpec for
# a sheet so the UI iterates the right row_fields / footer_fields.
# Legacy sheets (no template_name) fall back to bobine_formato via
# db.get_sheet_template_name's inference rules.
def _template_ctx_for_sheet(sheet: dict | None) -> dict:
    """Build template-aware context vars for rendering a sheet.

    Always returns keys: ``template`` (TemplateSpec or None),
    ``template_name``, ``row_fields``, ``footer_fields``, ``header_fields``.
    Backward-compat: if sheet is None, returns bobine_formato fields.
    """
    from app.templates_registry import DEFAULT_TEMPLATE, get_template
    if sheet is None:
        tpl = DEFAULT_TEMPLATE
    else:
        tname = db.get_sheet_template_name(sheet)
        tpl = get_template(tname)
    return {
        "template": tpl,
        "template_name": tpl.name,
        "row_fields": tpl.row_fields,
        "footer_fields": tpl.footer_fields,
        "header_fields": tpl.header_fields,
        "field_labels": tpl.field_labels or {},
    }

app = FastAPI(title="Metalogalva OCR — MVP")
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# R69 — production sectors available as a Jinja global so the export
# modal can iterate them without importing kpis in templates.
from app.web.kpis import PRODUCTION_SECTORS
templates.env.globals["production_sectors"] = PRODUCTION_SECTORS  # type: ignore[assignment]

# R136 — filtro Jinja: o date-picker do cabeçalho (célula header.data) é a
# ÚNICA forma de editar a data (a barra "Validar" deixou de ter input próprio).
# header.data guarda-se em DD-MM-YYYY; o <input type="date"> precisa de ISO.
templates.env.filters["pt_to_iso"] = db._normalize_data_pt_to_iso  # type: ignore[assignment]


def _process_sheet_ocr(sheet_id: int) -> None:
    """R71 — worker callback. Runs OCR + DQ + cross-check + CSV deposit
    for a single sheet. Identical pipeline to the old inline /upload
    logic but invoked from the background thread instead of the request
    handler.

    Idempotent: skips if sheet is no longer 'pending' (e.g. concurrent
    edit or already processed). Catches every Exception and persists
    error to the DB so the worker loop never crashes.
    """
    try:
        sheet = db.get_sheet(sheet_id)
        if sheet is None:
            return
        if sheet.get("status") != "pending":
            return  # idempotency — already processed by another invocation
        img_path = _DATA_DIR / sheet["image_path"]
        if not img_path.exists():
            db.update_error(sheet_id, "image file missing")
            return
        # R224 — distingue 1º processamento de reprocesso (já tinha OCR cru).
        was_reprocess = bool(sheet.get("raw_extraction"))
        # rev00 — pista de página da captura guiada (autoritativa p/ o routing).
        page_hint = (sheet.get("page_hint") or "").strip().upper() or None
        result = ocr_runner.run_pipeline(img_path, page_hint=page_hint)
        db.update_extraction(
            sheet_id=sheet_id,
            raw_extraction=result["raw"],
            dq_audit=result["dq"],
            sheet_data=result["current"],
        )
        # rev00 — flag de revisão do lado: o `run_pipeline` decide inline (pista
        # vs estrutura do Pass-1, ou mini-OCR '?' sem pista). Marca OU limpa —
        # um reprocess que agora resolve limpo desmarca a folha (senão o badge/
        # banner "rever lado" ficaria preso).
        try:
            if result.get("needs_review"):
                db.set_needs_review(sheet_id, result.get("review_reason") or "side_review")
            else:
                db.clear_needs_review(sheet_id)
        except Exception as nr_err:
            # R256 — falha aqui deixa o flag de revisão STALE (badge/banner e
            # gate de depósito errados); não pode ser silenciosa.
            print(f"[worker needs_review] sheet {sheet_id}: {nr_err}", file=sys.stderr)
            traceback.print_exc()
        try:
            _run_and_store_cross_check(
                sheet_id,
                profile_trigger="ocr_reprocess" if was_reprocess else "ocr_process",
                ocr_timing=result.get("timing"),
                ocr_metrics=result.get("metrics"),
            )
        except Exception as cc_err:
            print(f"[worker cross-check] sheet {sheet_id}: {cc_err}", file=sys.stderr)
            traceback.print_exc()
        # rev00 — só deposita CSV se a folha NÃO estiver marcada p/ revisão
        # (não corrompe o CSV da fábrica com um lado duvidoso). O check de lado
        # corre inline no run_pipeline, portanto o result é autoritativo aqui.
        try:
            if not result.get("needs_review"):
                _deposit_csv_to_factory(sheet_id)
        except Exception as dep_err:
            print(f"[worker deposit] sheet {sheet_id}: {dep_err}", file=sys.stderr)
        # Task C E4 — carimba a unidade fabril da folha a partir do template
        # detetado (builtins → NULL = Trofa). Best-effort: nunca falha o OCR.
        try:
            tpl_name = (result.get("raw") or {}).get("template_name")
            db.set_sheet_unidade(
                sheet_id, template_store.unidade_for_template(tpl_name))
        except Exception:
            pass
    except Exception as e:
        try:
            db.update_error(sheet_id, f"{type(e).__name__}: {e}")
        except Exception:
            pass
        traceback.print_exc()


def _process_discovery(template_id: int) -> None:
    """Task C E4 — worker callback da descoberta de campos de um template
    novo (wizard /admin). Partilha a fila FIFO do OCR de produção.

    Nunca crasha o worker: qualquer falha fica registada no discovery_json
    (parse_ok=False) e o template passa a 'analisado' — o humano corrige
    os campos à mão no wizard em vez de o job ficar preso na fila.
    """
    try:
        tpl = db.get_kanban_template(template_id)
        if tpl is None or tpl.get("status") != "a_analisar":
            return  # idempotência — já processado ou apagado
        img_rel = tpl.get("image_path") or ""
        img_path = _DATA_DIR / img_rel if img_rel else None
        if img_path is None or not img_path.exists():
            db.set_kanban_template_status(
                template_id, "analisado",
                discovery_json=json.dumps(
                    {"parse_ok": False, "error": "imagem do template em falta"},
                    ensure_ascii=False))
            return
        discovery = ocr_runner.run_discovery(img_path)
        suggestion = template_store.suggest_spec_from_discovery(
            discovery, name=tpl["name"], unidade_id=tpl["unidade_id"])
        spec = suggestion["spec"]
        # Preserva o que o humano já tiver posto no stub (ex.: aliases).
        try:
            stub = json.loads(tpl.get("spec_json") or "{}")
        except json.JSONDecodeError:
            stub = {}
        if stub.get("setor_aliases"):
            spec["setor_aliases"] = stub["setor_aliases"]
        if not discovery.get("parse_ok") and stub.get("row_fields"):
            spec["row_fields"] = stub["row_fields"]
        db.update_kanban_template_spec(
            template_id, json.dumps(spec, ensure_ascii=False))
        db.set_kanban_template_status(
            template_id, "analisado",
            discovery_json=json.dumps({
                "parse_ok": discovery.get("parse_ok", False),
                "discovery": {k: v for k, v in discovery.items() if k != "raw"},
                "raw": (discovery.get("raw") or "")[:4000],  # audit EN1090
                "field_map": suggestion["field_map"],
                "warnings": suggestion["warnings"],
            }, ensure_ascii=False))
    except Exception as e:
        try:
            db.set_kanban_template_status(
                template_id, "analisado",
                discovery_json=json.dumps(
                    {"parse_ok": False, "error": f"{type(e).__name__}: {e}"},
                    ensure_ascii=False))
        except Exception:
            pass
        traceback.print_exc()


@app.on_event("startup")
def _startup() -> None:
    db.init_db()
    # Task C E4 — instala os templates registados (DB) no registry antes de
    # o worker arrancar; com DB vazia é identidade (18 builtins).
    try:
        loaded = template_store.reload_registry()
        if loaded.get("loaded"):
            print(f"[templates] runtime: {', '.join(loaded['loaded'])}",
                  file=sys.stderr)
    except Exception as e:
        print(f"[templates startup] {e}", file=sys.stderr)
    # R71 — boot background OCR worker + recover any sheets stuck in
    # status='pending' from a previous process. The 10s window skips
    # sheets that are about to be enqueued by /upload in flight right now.
    ocr_queue.start_worker(_process_sheet_ocr, _process_discovery)
    n_recovered = ocr_queue.recover_pending(
        older_than_seconds=10,
        list_pending_fn=db.list_stuck_pending,
    )
    if n_recovered:
        print(f"[R71 startup] re-enqueued {n_recovered} pending sheet(s)", file=sys.stderr)
    # Task C E4 — re-enfileira descobertas órfãs (processo morreu a meio).
    try:
        for tpl in db.list_kanban_templates(status="a_analisar"):
            ocr_queue.enqueue_discovery(tpl["id"])
            print(f"[templates startup] re-enqueued discovery #{tpl['id']}",
                  file=sys.stderr)
    except Exception as e:
        print(f"[templates discovery startup] {e}", file=sys.stderr)
    try:
        if ref_importer.start_background_importer():
            st = ref_importer.status()
            print(
                "[refs-import] watching "
                f"{st.get('source_dir')} every {st.get('interval_seconds')}s",
                file=sys.stderr,
            )
    except Exception as e:
        print(f"[refs-import startup] {e}", file=sys.stderr)


_MOBILE_UA_PATTERNS = ("mobile", "iphone", "android", "ipad", "ipod")


def _is_mobile_request(request: Request) -> bool:
    """Detect mobile clients by User-Agent. Used to:
    - hide sidebar (mobile sees full-screen capture only)
    - drive flow decisions (mobile = capture-only, no review/edit)
    """
    ua = (request.headers.get("user-agent") or "").lower()
    return any(p in ua for p in _MOBILE_UA_PATTERNS)


@app.middleware("http")
async def _admin_token_gate(request: Request, call_next):
    """Task C F7 — gate OPCIONAL das páginas de administração.

    Sem ADMIN_TOKEN no ambiente (default) não muda NADA — a app continua
    aberta na LAN como sempre (não parte a fábrica). Com ADMIN_TOKEN
    definido, /admin* e /refs* exigem o token via cookie, header
    X-Admin-Token ou ?token= (o query param fixa o cookie para a
    navegação seguinte).
    """
    token = os.environ.get("ADMIN_TOKEN", "").strip()
    path = request.url.path
    if token and (path.startswith("/admin") or path.startswith("/refs")):
        supplied = (
            request.headers.get("x-admin-token")
            or request.query_params.get("token")
            or request.cookies.get("admin_token")
            or ""
        )
        if not secrets.compare_digest(supplied, token):
            return JSONResponse(
                {"detail": "token de administração inválido"}, status_code=401)
        response = await call_next(request)
        if request.query_params.get("token"):
            response.set_cookie(
                "admin_token", token, httponly=True, samesite="lax")
        return response
    return await call_next(request)


@app.middleware("http")
async def _attach_watermark(request: Request, call_next):
    """Inject a monotonic sheet watermark into every HTML response so
    the client banner ("X new sheets") can detect new uploads across
    HTMX polls. Adds a request-scoped attribute too so templates can
    embed it server-side on first paint. Also attaches a `mobile` flag
    used by templates to render mobile-only / desktop-only sections.
    """
    request.state.watermark = kpis.latest_sheet_id()
    request.state.mobile = _is_mobile_request(request)
    response = await call_next(request)
    # Header is always present; client uses it to detect increments.
    response.headers["X-Sheet-Watermark"] = str(request.state.watermark)
    return response




# ----- Pages -----

@app.get("/", response_class=HTMLResponse)
def home(request: Request) -> RedirectResponse:
    # R66 — mobile lands straight on the camera; desktop on the queue.
    target = "/capture" if _is_mobile_request(request) else "/queue"
    return RedirectResponse(target, status_code=302)


_GIT_SHA_CACHE: str | None = None


def _git_short_sha() -> str:
    """R223 — git short-SHA do código a correr, para o /health.

    Calculado uma vez (cache). Permite confirmar, de fora, QUE versão está
    mesmo viva na fábrica — o sintoma "atualizei e ficou igual" era, na raiz,
    um processo a servir código antigo sem forma de o ver."""
    global _GIT_SHA_CACHE
    if _GIT_SHA_CACHE is not None:
        return _GIT_SHA_CACHE
    sha = "unknown"
    try:
        import subprocess
        repo_root = Path(__file__).resolve().parents[3]
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(repo_root), capture_output=True, text=True, timeout=3,
        )
        if out.returncode == 0 and out.stdout.strip():
            sha = out.stdout.strip()
    except Exception:
        pass
    _GIT_SHA_CACHE = sha
    return sha


@app.get("/health")
def health() -> JSONResponse:
    """R223 — versão viva VISÍVEL (diagnóstico de deploy).

    O `start.ps1` faz health-check a este endpoint após reiniciar; o conteúdo
    mostra o ENGINE_VERSION + git SHA realmente carregados, para nunca mais
    haver dúvida sobre o que está a correr."""
    from app.pipeline.scoring_engine import ENGINE_VERSION
    return JSONResponse({
        "status": "ok",
        "engine_version": ENGINE_VERSION,
        "git_sha": _git_short_sha(),
        "python": sys.version.split()[0],
        "pid": os.getpid(),
    })


@app.get("/capture", response_class=HTMLResponse)
def capture_page(request: Request) -> Response:
    # R114 — operadores para dropdown de "Quem está a validar?" em
    # folhas com cesta (Expedição).
    return templates.TemplateResponse(
        request, "capture.html",
        {"operadores": _get_operadores()},
    )


_MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB — phone photos are typically 3-10 MB
# rev01 — scans de MFP multipágina em PDF podem ser maiores que uma foto.
_MAX_PDF_UPLOAD_BYTES = int(os.environ.get("PDF_MAX_UPLOAD_MB", "50")) * 1024 * 1024
_IMAGE_SUFFIXES = (".jpeg", ".jpg", ".png", ".bmp", ".tiff", ".webp")


def _register_image_sheet(
    page_path: Path,
    *,
    page_hint: str | None,
    capture_group: str | None,
    run_autocrop: bool,
) -> dict[str, object]:
    """rev01 — regista UMA imagem-folha e enfileira-a para OCR.

    Extraído do corpo do /upload para ser reutilizado 1x por foto/imagem e Nx
    por página de um PDF. Faz: insert_sheet → kernel event → (opcional) auto-crop
    → ocr_queue.enqueue. Devolve ``{sheet_id, status, queue_pos}``.

    ``run_autocrop`` só é True para FOTOS (perspetiva do papel). Páginas de PDF
    são scans já limpos/axis-aligned → saltam o auto-crop (o _warp_to_a4 rodaria
    /distorceria páginas portrait, e o OCR usa o original de qualquer forma).
    """
    rel_path = str(page_path.relative_to(_DATA_DIR))
    sheet_id = db.insert_sheet(
        rel_path, page_hint=page_hint, capture_group=capture_group,
    )
    # R117 — kernel event: folha aceite pelo servidor (pré-OCR).
    try:
        kernel.emit_event("sheet_uploaded", {"sheet_id": sheet_id, "image_path": rel_path})
    except Exception:
        pass
    if run_autocrop:
        # Round 46 — auto-crop kanban paper from photo (background removed,
        # perspective corrected). Silent no-op if detection fails.
        try:
            from .image_crop import auto_crop
            auto_crop(page_path)
        except Exception as crop_err:
            print(f"[auto-crop] sheet upload {page_path.name}: {crop_err}", file=sys.stderr)
    # R71 — enqueue for background OCR (worker drains FIFO serially, matching
    # Ollama's single-inference GPU constraint).
    queue_pos = ocr_queue.enqueue(sheet_id)
    return {"sheet_id": sheet_id, "status": "pending", "queue_pos": queue_pos}


@app.post("/upload")
async def upload(
    image: UploadFile = File(...),
    return_mode: str | None = Query(default=None, alias="return"),
    # rev00 — captura guiada frente/verso: pista de página ('F'/'V') +
    # token que liga as 2 fotos do mesmo kanban. Ambos opcionais.
    page: str | None = Form(default=None),
    capture_group: str | None = Form(default=None),
) -> Response:
    if not image.filename:
        raise HTTPException(400, "no filename")
    suffix = Path(image.filename).suffix.lower() or ".jpeg"

    # rev01 — decidir imagem vs PDF por MAGIC BYTES (%PDF-), autoritativo sobre a
    # extensão do filename. Lê o 1º chunk antes de escolher o destino/limite.
    first = await image.read(1024 * 1024)
    is_pdf = pdf_ingest.is_pdf_bytes(first) or suffix == ".pdf"
    if suffix == ".pdf" and not pdf_ingest.is_pdf_bytes(first):
        raise HTTPException(400, "ficheiro .pdf inválido (sem assinatura %PDF-)")
    if not is_pdf and suffix not in _IMAGE_SUFFIXES:
        raise HTTPException(400, f"unsupported extension {suffix}")

    # Size check via Content-Length quando disponível (limite maior p/ PDF).
    max_bytes = _MAX_PDF_UPLOAD_BYTES if is_pdf else _MAX_UPLOAD_BYTES
    if image.size is not None and image.size > max_bytes:
        raise HTTPException(
            413, f"file too large ({image.size} bytes > {max_bytes})"
        )

    store_suffix = ".pdf" if is_pdf else suffix
    token = secrets.token_hex(8)
    target = _IMAGES_DIR / f"{dt.datetime.now().astimezone():%Y%m%d_%H%M%S}_{token}{store_suffix}"
    # Grava o 1º chunk (já lido) + o resto do stream, com cap incremental.
    bytes_written = 0
    with target.open("wb") as f:
        chunk = first
        while chunk:
            bytes_written += len(chunk)
            if bytes_written > max_bytes:
                f.close()
                target.unlink(missing_ok=True)
                raise HTTPException(413, "file exceeded size limit")
            f.write(chunk)
            chunk = await image.read(1024 * 1024)

    if is_pdf:
        # rev01 — explode o PDF em N folhas. Cada página é um kanban
        # independente; page_hint=None → auto-deteção F/V por página (cobre
        # "vários kanbans por PDF" e "1 documento por PDF"). O .pdf-fonte é
        # PRESERVADO em data/images/ (trilho de auditoria EN 1090 / ISO 9001).
        group = (capture_group or "").strip() or token
        try:
            page_paths = await asyncio.to_thread(
                pdf_ingest.rasterize_pdf, target, _IMAGES_DIR, stem=target.stem
            )
        except pdf_ingest.PdfIngestError as e:
            target.unlink(missing_ok=True)  # PDF ilegível/indisponível → não deixa lixo
            # missing_dep = servidor sem pypdfium2 (503); resto = ficheiro inválido (422).
            status_code = 503 if e.reason == "missing_dep" else 422
            raise HTTPException(status_code, str(e)) from e
        sheets = [
            _register_image_sheet(
                p, page_hint=None, capture_group=group, run_autocrop=False,
            )
            for p in page_paths
        ]
    else:
        sheets = [
            _register_image_sheet(
                target, page_hint=page, capture_group=capture_group, run_autocrop=True,
            )
        ]

    # rev01 — resposta UNIFORME: `sheets` é uma lista (1 elem p/ imagem, N p/ PDF).
    # Mantém `sheet_id`/`status`/`queue_pos` no topo (retrocompat: clientes antigos
    # lêem body.sheet_id e processam só a 1ª página).
    if return_mode == "json":
        first_sheet = sheets[0]
        return JSONResponse(
            {
                "sheets": sheets,
                "sheet_id": first_sheet["sheet_id"],
                "status": first_sheet["status"],
                "queue_pos": first_sheet["queue_pos"],
                "count": len(sheets),
                "error": None,
            },
            status_code=200,
        )
    # Não-JSON (desktop): 1 folha → revisão dessa folha; multipágina → a fila.
    if len(sheets) == 1:
        return RedirectResponse(f"/sheet/{sheets[0]['sheet_id']}", status_code=303)
    return RedirectResponse("/queue", status_code=303)


# --- Cross-check helper (Round 33: pure verification) ---
# R124: política de substituição vive em `_apply_auto_overwrites` e
# `_maybe_apply_snap` — cada célula decide pelo seu `engine_status`,
# `source` e `ref_source`. As constantes R61/R66 anteriores saíram
# por dead code; R109 substituiu-as pela flag `snapped` por célula.

# R215 — restaurado auto-overwrite R134/R135: `snapped` e `very_different`
# com proposta concreta de referência são aplicados. `ocr_raw`/`syntax`
# continuam a ficar para revisão humana. Travões adicionais: edições humanas
# (R133) e obra_concluida (R125).


def _human_edited_paths(sheet_id: int) -> frozenset[str]:
    """R133 — field_paths cuja ÚLTIMA edição foi feita por um humano
    (`edits.source='human'`). Estes campos são AUTORITATIVOS: o
    auto-overwrite do cross-check (snap/operador/codmaq, source='system')
    NÃO os deve reverter.

    Sem isto, o operador edita uma célula e o re-cross-check disparado pelo
    `sheet_edit` reescreve logo o valor canónico do plan → o sintoma
    "escrevo mas não guarda". Última edição por path = MAX(id) (itera por
    id ASC, o último source ganha).
    """
    try:
        with db.conn() as c:
            rows = c.execute(
                "SELECT field_path, source FROM edits "
                "WHERE sheet_id = ? ORDER BY id ASC",
                (sheet_id,),
            ).fetchall()
    except Exception:
        return frozenset()
    last: dict[str, str] = {}
    for r in rows:
        last[r["field_path"]] = r["source"]
    return frozenset(fp for fp, src in last.items() if src == "human")


def _last_human_field_edits(sheet_id: int) -> dict[str, str]:
    """Último valor humano por célula, excluindo edits estruturais de linha."""
    try:
        with db.conn() as c:
            rows = c.execute(
                "SELECT field_path, new_value, source FROM edits "
                "WHERE sheet_id = ? ORDER BY id ASC",
                (sheet_id,),
            ).fetchall()
    except Exception:
        return {}
    last: dict[str, tuple[str, str]] = {}
    for r in rows:
        path = str(r["field_path"] or "")
        if not path or "." not in path:
            continue
        last[path] = (str(r["source"] or ""), str(r["new_value"] or ""))
    return {
        path: value
        for path, (source, value) in last.items()
        if source == "human"
    }


def _has_human_row_structure_edits(sheet_id: int) -> bool:
    """True quando houve add/remove row humano; aí não reconstruímos rows."""
    try:
        with db.conn() as c:
            rows = c.execute(
                "SELECT field_path, source FROM edits WHERE sheet_id = ?",
                (sheet_id,),
            ).fetchall()
    except Exception:
        return False
    for r in rows:
        path = str(r["field_path"] or "")
        if str(r["source"] or "") == "human" and path.startswith("rows[") and "." not in path:
            return True
    return False


def _rebuild_sheet_data_from_raw(sheet_id: int, sheet: dict) -> bool:
    """Limpa snaps antigos: raw OCR + últimas edições humanas por campo."""
    if sheet.get("status") == "validated":
        return False
    raw = sheet.get("raw_extraction") or {}
    current = sheet.get("sheet_data") or {}
    if not raw or _has_human_row_structure_edits(sheet_id):
        return False
    rebuilt = json.loads(json.dumps(raw, ensure_ascii=False))
    for path, value in _last_human_field_edits(sheet_id).items():
        try:
            db._set_by_path(rebuilt, path, value)
        except Exception:
            continue
    if rebuilt == current:
        return False
    with db.conn() as c:
        c.execute(
            "UPDATE sheets SET sheet_data = ? WHERE id = ?",
            (json.dumps(rebuilt, ensure_ascii=False), sheet_id),
        )
        db._sync_production_rows(c, sheet_id, rebuilt)
    return True


def _maybe_apply_snap(
    sheet_id: int,
    field_path: str,
    cell: dict,
    protected: frozenset[str] = frozenset(),
) -> bool:
    """Aplica correções propostas pelo cross-check.

    Política:
      - `snapped` (delta suave / autofill) → aplica.
      - `very_different` vindo de ref concreta → aplica.
      - `confirmed` / `NA` → no-op.
      - `very_different` sem ref concreta → no-op; é revisão humana.

    Travões (apenas estes):
      - R133: campo com última edição humana (`protected`) — autoritativo,
        nunca revertido (senão "escrevo mas não guarda").
      - R125: `source == "obra_concluida"` — operador tem de investigar.

    Retorna True quando aplicou um edit.
    """
    # R133 — edição humana é autoritativa: nunca auto-substituir.
    if field_path in protected:
        return False
    # R125 — obra concluída no plan: operador investiga, não auto-substituir.
    if cell.get("source") == "obra_concluida":
        return False
    engine_status = cell.get("engine_status")
    # R236/R243 — gate de gravação (flag, default OFF = R219 substitui-sempre):
    # quando ON, a decisão é por PERDA ESPERADA — grava sse P(winner certo) ≥
    # limiar do campo (1 − C_rev/C_erro: esp/comp 0.98, identidade 0.95,
    # resto 0.90). A proposta continua visível a vermelho; o OCR do operador
    # fica intacto até revisão. Sem confiança calibrada na célula (cross
    # antigo), cai no critério R236 (winner_mode weak_guess). Caso provado:
    # folha 2367. Ligar com CROSS_WRITE_GATE_MARGINAL=1 depois do OK do Luís.
    if (
        engine_status == "very_different"
        and get_settings().cross_write_gate_marginal
    ):
        conf = cell.get("decision_confidence")
        if conf is not None:
            from app.pipeline.scoring_engine import write_confidence_threshold

            field_name = field_path.rsplit(".", 1)[-1]
            # R253/F3 — com irmão plausível (<2 bits) o limiar sobe um tier
            # para TODOS os campos da decisão (Sadinle/reject-option).
            if float(conf) < write_confidence_threshold(
                    field_name, cell.get("sibling_margin_bits")):
                return False
        elif cell.get("winner_mode") == "weak_guess":
            return False
    source = cell.get("source")
    ref_source = cell.get("ref_source") or source
    concrete_sources = {"plan", "sap", "ferramenta", "maquinas", "colaboradores", "lexicon"}
    concrete_ref_sources = {"plan", "sap", "maquinas", "colaboradores"}
    canonical = ""
    if engine_status == "snapped":
        canonical = (cell.get("value") or "").strip()
    elif engine_status == "very_different":
        if source in concrete_sources:
            canonical = (
                cell.get("value")
                or cell.get("ref")
                or cell.get("proposed")
                or ""
            ).strip()
        elif ref_source in concrete_ref_sources:
            canonical = (cell.get("ref") or cell.get("proposed") or "").strip()
        else:
            return False
    else:
        return False
    if not canonical:
        return False
    try:
        db.apply_edit(sheet_id, field_path, canonical, source="system")
        return True
    except Exception:
        return False


def _apply_auto_overwrites(
    sheet_id: int, result: dict, protected: frozenset[str] = frozenset()
) -> int:
    """R109/R124 — aplica snaps em rows + header + footer.

    R109 introduziu o ciclo sobre `result["rows"]` e `snapped=True`.
    R124 estende:
      - cobre também `result["header"]` e `result["footer"]` (motor já
        produz cells nessas secções desde R123 Fase 4 B9);

    R215: `very_different` com ref concreta volta a ser aplicado
    automaticamente. Vermelho sem ref concreta continua a significar revisão.

    R133 — `protected` (field_paths com última edição humana) salta o
    auto-overwrite desses campos. Ver `_maybe_apply_snap`.
    """
    n_applied = 0
    for row_r in result.get("rows", []):
        i = row_r.get("row_index")
        if i is None:
            continue
        for fn, cell in row_r.get("fields", {}).items():
            if _maybe_apply_snap(sheet_id, f"rows[{i}].{fn}", cell, protected):
                n_applied += 1
    for section in ("header", "footer"):
        for fn, cell in (result.get(section) or {}).items():
            if _maybe_apply_snap(sheet_id, f"{section}.{fn}", cell, protected):
                n_applied += 1
    return n_applied


_LAST_OPERADOR_SNAP_WARN: str | None = None


def _apply_operador_snap(
    sheet_id: int, sheet: dict, refs: dict, protected: frozenset[str] = frozenset()
) -> int:
    """R70 — resolve operator identity against ListaColaboradores.

    Reads ``header.operador`` + ``header.n_operador`` from sheet_data,
    runs ``snap_operador`` against ``refs["colaboradores"]``, and persists
    canonical values via ``db.apply_edit`` when:
      - ``snapped_name`` differs from current operador, OR
      - ``snapped_cod`` differs from current n_operador, OR
      - ``pernr`` is set and differs from current header.pernr

    Returns count of fields edited (0-3). Suspended cells (yellow flag)
    do not trigger edits; engine handles the visual flag separately.

    R133 — `protected` (paths com última edição humana) salta o snap de
    header.operador / header.n_operador editados manualmente.
    """
    global _LAST_OPERADOR_SNAP_WARN
    colabs = refs.get("colaboradores") or {}
    if not colabs:
        # R131 — log único por reload de refs (chave = loaded_at) para o
        # utilizador perceber que o snap não está a correr por falta de
        # ListaColaboradores. Causa frequente: ficheiro em path errado no
        # PC da Metalogalva ou ainda não copiado para `kanban_refs/`.
        loaded_at = str(refs.get("loaded_at") or "<no-refs>")
        if loaded_at != _LAST_OPERADOR_SNAP_WARN:
            print(
                f"[operador_snap] colaboradores vazio (refs.loaded_at={loaded_at}) "
                "— snap operador DESACTIVADO. Verificar "
                "kanban_refs/ListaColaboradores.xlsx ou KANBAN_REFS_DIR no .env.",
                file=sys.stderr,
            )
            _LAST_OPERADOR_SNAP_WARN = loaded_at
        return 0
    header = (sheet.get("sheet_data") or {}).get("header") or {}
    raw_name = (header.get("operador") or "").strip()
    raw_cod = (header.get("n_operador") or "").strip()
    cur_pernr = (header.get("pernr") or "").strip()

    if not raw_name and not raw_cod:
        return 0

    # R91: pass aliases lexicon for memorized OCR-corrupt → canonical
    # mappings. Empty dict if no aliases registered yet.
    aliases = refs.get("operador_aliases") or {}
    sr = snap_operador(raw_name, raw_cod, colabs, aliases=aliases)

    n_applied = 0

    # Persist pernr whenever we have a confident match (HIGH levels A/B/C),
    # even if no-op on name/cod (Condition A still has pernr to record).
    if sr.pernr and sr.pernr != cur_pernr:
        try:
            db.apply_edit(sheet_id, "header.pernr", sr.pernr, source="system")
            n_applied += 1
        except Exception:
            pass

    # Snap name when changed (R133 — salta se o operador editou à mão)
    if (sr.applied and sr.snapped_name and sr.snapped_name != raw_name
            and "header.operador" not in protected):
        try:
            db.apply_edit(sheet_id, "header.operador", sr.snapped_name, source="system")
            n_applied += 1
        except Exception:
            pass

    # Snap cod when changed (Condition C — Lev-1; R133 — salta se editado à mão)
    if (sr.applied and sr.snapped_cod and sr.snapped_cod != raw_cod
            and "header.n_operador" not in protected):
        try:
            db.apply_edit(sheet_id, "header.n_operador", sr.snapped_cod, source="system")
            n_applied += 1
        except Exception:
            pass

    return n_applied


def _apply_codmaq_fill(
    sheet_id: int, sheet: dict, refs: dict, protected: frozenset[str] = frozenset()
) -> int:
    """R85/R124 — fill OR correct header.cod_maquina from setor_maquina.

    Looks up ``header.setor_maquina`` (e.g. "HPE32", "GUIFIL", "LASER")
    in ``refs["maquinas_by_kanban"]`` and writes the canonical
    ``codmaq`` (M024 / M067 / M030) to ``header.cod_maquina``.

    R124: substitui também quando o operador escreveu um cod errado —
    antes só preenchia se estivesse vazio. O setor é a chave fiável (vem
    do template da folha); o cod é derivável e portanto sobrescrevível.
    Skipped quando o setor não mapeia unambiguamente (ex: "GUILHOTINA"
    sem largura — registry tem GUILHOTINA 3M/6M/9M/10M).

    R133 — salta se header.cod_maquina foi editado à mão (protected).

    Returns 1 if applied, 0 otherwise.
    """
    if "header.cod_maquina" in protected:
        return 0
    header = (sheet.get("sheet_data") or {}).get("header") or {}
    setor = (header.get("setor_maquina") or "").strip()
    if not setor:
        return 0
    maq = resolve_machine_from_setor(setor, refs)
    if not maq or not maq.get("codmaq"):
        return 0
    canonical = maq["codmaq"]
    current = (header.get("cod_maquina") or "").strip()
    if current == canonical:
        return 0  # já igual — nada a fazer
    try:
        db.apply_edit(sheet_id, "header.cod_maquina", canonical, source="system")
        return 1
    except Exception:
        return 0


# ── R224 — profiling: tempo por etapa + traço de match por folha ──────────
_PROFILE_LOG = _DATA_DIR / "_logs" / "profile.jsonl"
_PROFILE_MAX_BYTES = 50_000_000


def _profile_on() -> bool:
    """Profiling ligado por defeito; kill-switch `PROFILE_DISABLED=1`."""
    return os.environ.get("PROFILE_DISABLED", "").lower() not in ("1", "true", "yes")


def _write_profile(record: dict) -> None:
    """Append de um registo de profiling ao `data/_logs/profile.jsonl` +
    evento kernel `sheet_profiled`. Nunca rebenta o pipeline (tudo try/except)."""
    if not _profile_on():
        return
    try:
        _PROFILE_LOG.parent.mkdir(parents=True, exist_ok=True)
        try:
            if _PROFILE_LOG.exists() and _PROFILE_LOG.stat().st_size > _PROFILE_MAX_BYTES:
                _PROFILE_LOG.replace(_PROFILE_LOG.with_suffix(".jsonl.1"))
        except Exception:
            pass
        with open(_PROFILE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        try:
            kernel.emit_event("sheet_profiled", {
                "sheet_id": record.get("sheet_id"),
                "trigger": record.get("trigger"),
                "timing": record.get("timing"),
            })
        except Exception:
            pass
    except Exception:
        pass


def _run_and_store_cross_check(
    sheet_id: int,
    *,
    rebuild_from_raw: bool = False,
    profile_trigger: str | None = None,
    ocr_timing: dict | None = None,
    ocr_metrics: dict | None = None,
) -> dict | None:
    """Round 33 — invisible verification inline in /upload pipeline.

    R215 — para células ``snapped`` e ``very_different`` com referência
    concreta, ``_apply_auto_overwrites`` escreve o valor canónico do plano
    na sheet_data. ``very_different`` sem ref concreta continua a ficar para
    revisão humana.

    Steps:
      1. Run cross_check_sheet → per-cell status against refs
      2. apply_auto_overwrites (snapped + very_different concreto) +
         operador snap + cod_maquina fill
      3. If any edits were applied, re-run cross_check_sheet on the updated
         sheet_data so the persisted JSON reflects the final state
      4. Persist JSON to ``C:\\kanban\\nifruka\\03_Cross_Check\\``
    """
    _prof = profile_trigger is not None and _profile_on()
    _ct: dict[str, int] = {}  # R224 — ms por sub-etapa do cross
    _scoring_trace: list | None = None
    sheet = db.get_sheet(sheet_id)
    if sheet is None or not sheet.get("sheet_data"):
        return None
    _t0 = time.perf_counter()
    watcher = get_watcher()
    refs = watcher.get_refs()
    _ct["refs_ms"] = int((time.perf_counter() - _t0) * 1000)
    if not refs.get("available"):
        return None

    # R212 — quando o JSON antigo fica stale por bump de ENGINE_VERSION,
    # snaps de versões anteriores podem já ter poluído `sheet_data`.
    # Recomeçamos do OCR cru e reaplicamos só edições humanas por célula.
    if rebuild_from_raw and sheet.get("status") != "validated":
        _t0 = time.perf_counter()
        if _rebuild_sheet_data_from_raw(sheet_id, sheet):
            refreshed = db.get_sheet(sheet_id)
            if refreshed is not None:
                sheet = refreshed
        _ct["rebuild_ms"] = int((time.perf_counter() - _t0) * 1000)

    _t0 = time.perf_counter()
    result = cross_check_sheet(
        sheet["sheet_data"], sheet.get("dq_audit"), refs, collect_trace=_prof,
    )
    _ct["scoring1_ms"] = int((time.perf_counter() - _t0) * 1000)
    # R224 — guarda o traço da 1ª pontuação (sobre os valores do OCR, antes das
    # substituições) e RETIRA-o do `result` para não inchar o JSON do cross.
    _scoring_trace = result.pop("trace", None)
    from app.cross_check.ref_watcher import refs_snapshot
    plan_path = getattr(watcher, "plan_path", None)
    result["refs_snapshot"] = refs_snapshot(refs, plan_path)

    # R133 — campos com última edição humana são autoritativos: o
    # auto-overwrite abaixo salta-os, senão o re-cross-check disparado por
    # `sheet_edit` reverte a correcção do operador ("escrevo mas não guarda").
    protected = _human_edited_paths(sheet_id)
    # R134 — folhas validadas são IMUTÁVEIS (audit trail EN 1090 / ISO 9001):
    # nunca auto-substituir nada. O bump do ENGINE_VERSION faz `_build_cc_maps`
    # regenerar o cross-check de folhas antigas ao abrir — para validated isso
    # só pode refrescar o JSON de display, nunca tocar no sheet_data.
    if sheet.get("status") == "validated":
        n_overwritten = n_op_snapped = n_codmaq_filled = 0
    else:
        _t0 = time.perf_counter()
        # Cross-check auto-overwrite: `snapped` e `very_different` concreto.
        n_overwritten = _apply_auto_overwrites(sheet_id, result, protected)
        # R70 — operator snap against ListaColaboradores (SAP employee list).
        # Resolves OCR name/cod against canonical sname/cod/pernr and applies
        # auto-substitution when there's strong identity signal (cod + token
        # overlap). See backend/app/dq/operador_snap.py for the 5 rules.
        n_op_snapped = _apply_operador_snap(sheet_id, sheet, refs, protected)
        # R85 — auto-fill cod_maquina from setor_maquina via maquinas.xlsx
        # lookup. Fills empty cod_maquina when setor maps to a known machine.
        n_codmaq_filled = _apply_codmaq_fill(sheet_id, sheet, refs, protected)
        _ct["overwrites_ms"] = int((time.perf_counter() - _t0) * 1000)
    if n_overwritten > 0 or n_op_snapped > 0 or n_codmaq_filled > 0:
        # Re-fetch sheet (sheet_data was modified by apply_edit) and
        # re-run cross-check to refresh statuses against new values.
        # R80 note: the `*` indicator is derived in _build_snapped_map_from_raw
        # by comparing current sheet_data vs raw_extraction, so it survives
        # this re-run automatically (no need to preserve `snapped` flags
        # on the cross-check JSON).
        refreshed = db.get_sheet(sheet_id)
        if refreshed is not None and refreshed.get("sheet_data"):
            sheet = refreshed
            _t0 = time.perf_counter()
            result = cross_check_sheet(sheet["sheet_data"], sheet.get("dq_audit"), refs)
            _ct["scoring2_ms"] = int((time.perf_counter() - _t0) * 1000)
            result["refs_snapshot"] = refs_snapshot(refs, plan_path)

    if sheet is None or not sheet.get("sheet_data"):
        return result  # defensive — should not happen

    # R246 — descodificação ATIVA (flag, default OFF): para células na zona
    # cinzenta com duas hipóteses concretas, re-lê o crop com pergunta
    # discriminativa. Resultados em result['active_rereads'] (shadow — não
    # mudam valores até a fiabilidade do re-read estar calibrada na fábrica).
    try:
        from app.config import get_settings as _gs
        if _gs().cross_active_reread:
            from app.pipeline import active_reread

            rereads = []
            for cand_rr in active_reread.candidates_for_reread(result):
                rr = active_reread.discriminative_reread(
                    sheet["image_path"], int(cand_rr["row_index"] or 0),
                    str(cand_rr["field"]), cand_rr["options"],
                )
                if rr is not None:
                    rereads.append({
                        "row_index": rr.row_index, "field": rr.field,
                        "options": list(rr.options), "answer": rr.answer,
                        "duration_ms": rr.duration_ms,
                    })
            if rereads:
                result["active_rereads"] = rereads
    except Exception:
        pass

    header = sheet["sheet_data"].get("header", {}) or {}
    operador = header.get("operador") or sheet.get("operador") or "?"
    date_pt = (header.get("data") or "").strip()
    date_iso = date_pt
    if len(date_pt) == 10 and date_pt[2] == "-":
        date_iso = f"{date_pt[6:10]}-{date_pt[3:5]}-{date_pt[0:2]}"
    _t0 = time.perf_counter()
    store_cross_check(
        sheet_id=sheet_id,
        image_path=sheet["image_path"],
        operador=operador,
        date_iso=date_iso,
        sheet_status=sheet["status"],
        cross_check_result=result,
    )
    _ct["store_ms"] = int((time.perf_counter() - _t0) * 1000)
    # R108 — shadow scoring engine corre em background, escreve em coluna
    # própria. Não bloqueia, não interfere com `result`. Try/except wrap
    # garante que qualquer falha no shadow não toca em produção.
    _spawn_shadow_scoring(sheet_id, sheet["sheet_data"], sheet.get("dq_audit"), refs)

    # R224 — profiling: junta o timing do OCR (se veio do worker) + o timing do
    # cross + o traço de match e escreve um registo por processamento.
    if _prof:
        timing = dict(ocr_timing or {})
        timing.update(_ct)
        timing["cross_total_ms"] = sum(_ct.values())
        _write_profile({
            "sheet_id": sheet_id,
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "trigger": profile_trigger,
            "template": result.get("template_name"),
            "n_rows": len(result.get("rows", []) or []),
            "timing": timing,
            "ocr": ocr_metrics or {},
            "template_detection": (
                (sheet.get("sheet_data") or {}).get("template_detection") or {}
            ),
            "scoring": _scoring_trace or [],
        })
    return result


def _spawn_shadow_scoring(
    sheet_id: int,
    sheet_data: dict,
    dq_audit: dict | None,
    refs: dict,
) -> None:
    """R108 — dispara `scoring_engine.shadow_score` em thread daemon.

    Devolve imediatamente. Erros silenciados (logged) — shadow nunca
    deve afectar o output de produção.
    """
    def _run() -> None:
        try:
            from app.pipeline.scoring_engine import shadow_score, set_scoring_variant
            # R250 — A/B real na fábrica: a sombra corre a variante configurada
            # (só nesta thread — ContextVar); produção intocada, output em
            # sheets.shadow_scoring_json.
            # R257 — o dispatch comparava com o literal "next" (era R250), pelo
            # que CROSS_SHADOW_VARIANT=v30cal caía silenciosamente no default
            # v30 — e como o ranking v30cal é byte-idêntico ao v30, o soak do
            # R255 compararia v30-vs-v30 e daria luz verde FALSA ao flip.
            # Agora qualquer valor != "current" é despachado.
            shadow_variant = (get_settings().cross_shadow_variant or "").strip()
            if shadow_variant and shadow_variant != "current":
                set_scoring_variant(shadow_variant)
            run_id = db.start_shadow_run(sheet_id)
            try:
                scoring, total, snapped, confirmed, na, dur_ms = shadow_score(
                    sheet_data, dq_audit, refs
                )
                db.finish_shadow_run(
                    run_id, sheet_id, scoring,
                    total, snapped, confirmed, na, dur_ms,
                )
                # R117 — kernel event: shadow scoring concluído (thread daemon,
                # kernel.emit_event é thread-safe via _LOCK).
                try:
                    very_different = int(
                        (scoring.get("summary") or {}).get("very_different", 0) or 0
                    )
                    kernel.emit_event("shadow_run_completed", {
                        "sheet_id": sheet_id,
                        "total": total,
                        "snapped": snapped,
                        "very_different": very_different,
                        "confirmed": confirmed,
                        "na": na,
                        "duration_ms": dur_ms,
                    })
                except Exception:
                    pass
            except Exception as exc:
                db.fail_shadow_run(run_id, f"{type(exc).__name__}: {exc}")
                traceback.print_exc()
        except Exception:
            # Falha a abrir o run sequer — não devia acontecer, mas guarda
            traceback.print_exc()

    threading.Thread(
        target=_run, daemon=True, name=f"shadow-score-{sheet_id}"
    ).start()


@app.get("/sheet/{sheet_id}", response_class=HTMLResponse)
def sheet_page(
    request: Request,
    sheet_id: int,
    view: str | None = None,
    back: str | None = None,
) -> Response:
    """Round 41d: ``view`` query param toggles between 'final' (default,
    post-snap + manual edits) and 'raw' (original OCR before any auto-fix).
    Lets supervisor see what the OCR actually extracted vs what the system
    auto-corrected via DQ snap + cross-check.

    R88: ``back`` is an optional URL where the "← Voltar à lista" button
    should point (typically /queue?... or /kanbans?... with active
    filters). Only relative paths are accepted (open-redirect guard).
    """
    sheet = db.get_sheet(sheet_id)
    if sheet is None:
        raise HTTPException(404, f"sheet {sheet_id} not found")

    # R88 — validate back URL: relative path only, no scheme/host
    back_url: str | None = None
    if back:
        candidate = back.strip()
        if candidate.startswith("/") and not candidate.startswith("//"):
            back_url = candidate

    cells_by_path: dict[str, dict] = {}
    if sheet.get("dq_audit"):
        cells_by_path = sheet["dq_audit"].get("cells", {})

    view_mode = "raw" if view == "raw" else "final"
    if view_mode == "raw" and sheet.get("raw_extraction"):
        # Show original OCR — disable cross-check colors (they validate
        # the post-snap values, not raw)
        src = sheet["raw_extraction"]
        (cc_status_by_path, cc_ref_by_path, cc_ref_title_by_path,
         cc_suspended_by_path, cc_snapped_by_path,
         cc_obra_concluida_by_path) = ({}, {}, {}, {}, {}, {})
    else:
        src = sheet.get("sheet_data") or {}
        (cc_status_by_path, cc_ref_by_path, cc_ref_title_by_path,
         cc_suspended_by_path, cc_snapped_by_path,
         cc_obra_concluida_by_path) = _build_cc_maps(sheet_id)

    rows = src.get("rows", []) or []
    header = src.get("header", {}) or {}
    footer = src.get("footer", {}) or {}

    flagged = sum(1 for c in cells_by_path.values() if c.get("requires_review"))

    tpl_ctx = _template_ctx_for_sheet(sheet)

    # R136 — a barra "Validar" deixou de ter date-picker próprio; a data
    # edita-se na célula header.data (date-picker via filtro `pt_to_iso`).
    # Já não é preciso pré-calcular `data_iso_for_validate` aqui.

    # R111 — flag para a UI saber se a imagem servida em /image/<id> é cropped
    # (paper detectado) ou raw (fallback silencioso). Quando False, sheet.html
    # mostra badge + botão "Tentar recortar agora".
    from .image_crop import has_cropped as _has_cropped
    sheet_has_cropped = _has_cropped(_DATA_DIR / sheet["image_path"])

    return templates.TemplateResponse(
        request,
        "sheet.html",
        {
            "sheet": sheet,
            "header": header,
            "rows": rows,
            "footer": footer,
            "cells_by_path": cells_by_path,
            "cc_status_by_path": cc_status_by_path,
            "cc_ref_by_path": cc_ref_by_path,
            "cc_ref_title_by_path": cc_ref_title_by_path,
            "cc_suspended_by_path": cc_suspended_by_path,
            "cc_snapped_by_path": cc_snapped_by_path,
            "cc_obra_concluida_by_path": cc_obra_concluida_by_path,
            "flagged_count": flagged,
            "view_mode": view_mode,
            "back_url": back_url,
            "has_cropped": sheet_has_cropped,
            **tpl_ctx,  # template, template_name, row/footer/header_fields
        },
    )


def _build_snapped_map_from_raw(sheet: dict) -> dict[str, bool]:
    """R80 — compute {field_path: True} for cells whose current value
    differs from the raw OCR extraction. Captures every cell that was
    modified after upload (auto-correction via DQ snap, cross-check
    overwrite, or operator manual edit). Used to render the `*` indicator
    on cells that aren't the OCR original.

    Compares ``sheet_data`` (current) against ``raw_extraction`` (snapshot
    at upload time). Fields covered: rows[*].*, header.*, footer.*.

    Returns {} if no raw_extraction available.
    """
    raw = sheet.get("raw_extraction") or {}
    cur = sheet.get("sheet_data") or {}
    if not raw or not cur:
        return {}

    def _norm(v: object) -> str:
        return str(v).strip() if v is not None else ""

    out: dict[str, bool] = {}

    raw_rows = raw.get("rows") or []
    cur_rows = cur.get("rows") or []
    for i, cur_r in enumerate(cur_rows):
        if i >= len(raw_rows):
            break
        raw_r = raw_rows[i] or {}
        for fn in (cur_r or {}).keys():
            if _norm(cur_r.get(fn)) != _norm(raw_r.get(fn)):
                out[f"rows[{i}].{fn}"] = True

    for section in ("header", "footer"):
        raw_sec = raw.get(section) or {}
        cur_sec = cur.get(section) or {}
        for fn in cur_sec.keys():
            if _norm(cur_sec.get(fn)) != _norm(raw_sec.get(fn)):
                out[f"{section}.{fn}"] = True

    return out


def _build_cc_maps(sheet_id: int, *, allow_regen: bool = True) -> tuple[
    dict[str, str], dict[str, str], dict[str, str], dict[str, bool],
    dict[str, bool], dict[str, bool],
]:
    """Round 33: load cross-check JSON for sheet, build {field_path: status}
    + {field_path: ref} maps for template rendering of green/red cell colors.
    Ref titles include the real source (Plan/SAP/machines/collaborators).

    R52 F4: also returns {field_path: suspended_by_stub} for distinguishing
    NA from stub-accept (amarelo soft) vs NA from no-ref (cinza).

    R80: also returns {field_path: snapped} derived from comparing current
    ``sheet_data`` against ``raw_extraction``. Captures every cell that was
    modified after upload (auto-correction or manual edit). Used for the
    `*` indicator showing operator which values aren't the OCR original.

    Returns empty maps plus the snapped map if no cross-check data is available."""
    from app.cross_check.storage import load_sheet_cross_check
    from app.pipeline.scoring_engine import ENGINE_VERSION
    cc = load_sheet_cross_check(sheet_id)
    stale_cc = None
    if not cc:
        stale_cc = load_sheet_cross_check(sheet_id, include_stale=True)
    # R123 (D1) — fallback on-demand. A folha nunca teve cross-check
    # (processada antes do R118) ou o JSON é de um motor anterior ao R123:
    # regenera-o agora e relê, para nenhuma folha abrir toda cinza nem com
    # cores de um motor antigo.
    if allow_regen and (not cc or cc.get("engine_version") != ENGINE_VERSION):
        try:
            rebuild_from_raw = bool(
                (cc and cc.get("engine_version") != ENGINE_VERSION)
                or (stale_cc and stale_cc.get("engine_version") != ENGINE_VERSION)
            )
            _run_and_store_cross_check(
                sheet_id, rebuild_from_raw=rebuild_from_raw,
                profile_trigger="view_regen",
            )
            cc = load_sheet_cross_check(sheet_id)
        except Exception:
            # R223 — NÃO engolir em silêncio. Esta regeneração on-demand é o que
            # aplica um motor novo às folhas antigas ao abri-las; se falhava
            # (refs indisponíveis, db, cross_check_sheet a rebentar), a folha
            # ficava a mostrar o motor ANTIGO (ou cinza) sem qualquer aviso —
            # mascarando um deploy que não pegou. Logar para stderr/uvicorn.err.
            print(
                f"[cross-check] regeneração on-demand falhou para sheet "
                f"{sheet_id}: {traceback.format_exc()}",
                file=sys.stderr, flush=True,
            )
    sheet = db.get_sheet(sheet_id) or {}
    snapped_map = _build_snapped_map_from_raw(sheet)
    if not cc:
        return {}, {}, {}, {}, snapped_map, {}
    status_map: dict[str, str] = {}
    ref_map: dict[str, str] = {}
    ref_title_map: dict[str, str] = {}
    suspended_map: dict[str, bool] = {}
    # R125/R163 — paths de linhas com obra_concluida=True. Antes isto vinha
    # como source="obra_concluida" em todas as células; agora é metadata da
    # linha para evitar transformar células certas em NO_MATCH.
    obra_concluida_map: dict[str, bool] = {}

    def _cell_ref(info: dict) -> object:
        ref = info.get("ref")
        if ref not in (None, ""):
            return ref
        return info.get("plan_value")

    def _cell_ref_title(info: dict, ref: object) -> str:
        ref_text = str(ref).strip()
        ref_source = str(info.get("ref_source") or info.get("source") or "").strip()
        prefix = {
            "plan": "Plan diz",
            "sap": "SAP diz",
            "maquinas": "Máquina esperada",
            "colaboradores": "Colaborador esperado",
            "ferramenta": "Ferramenta esperada",
            "syntax": "Formato esperado",
        }.get(ref_source, "Referência diz")
        return f"{prefix}: {ref_text}" if ref_text else ""

    for r in cc.get("rows", []):
        i = r.get("row_index")
        row_obra_concluida = bool(r.get("obra_concluida"))
        for f, info in (r.get("fields") or {}).items():
            path = f"rows[{i}].{f}"
            status_map[path] = info.get("status", "NA")
            ref = _cell_ref(info)
            if ref is not None:
                ref_map[path] = str(ref)
                title = _cell_ref_title(info, ref)
                if title:
                    ref_title_map[path] = title
            if info.get("suspended_by_stub"):
                suspended_map[path] = True
            if row_obra_concluida or info.get("source") == "obra_concluida":
                obra_concluida_map[path] = True
    # R123 (B9) — header/footer também coloridos (operador, data, máquina,
    # colunas_produzidas, ...). Cross-checks gravados antes do R123 não os
    # têm — `cc.get(section)` devolve {} e o cabeçalho fica neutro.
    for section in ("header", "footer"):
        for f, info in (cc.get(section) or {}).items():
            path = f"{section}.{f}"
            status_map[path] = info.get("status", "NA")
            ref = _cell_ref(info)
            if ref is not None:
                ref_map[path] = str(ref)
                title = _cell_ref_title(info, ref)
                if title:
                    ref_title_map[path] = title
    return status_map, ref_map, ref_title_map, suspended_map, snapped_map, obra_concluida_map


def _apply_lightweight_edit_snaps(sheet_id: int, field_path: str, sheet: dict) -> dict:
    """Apply cheap dependent header updates after a single-cell edit.

    Full cross-check now runs in background, but these snaps update visible
    header companions immediately (operator name/number and machine code).
    """
    if field_path not in ("header.operador", "header.n_operador", "header.setor_maquina"):
        return sheet
    try:
        refs = get_watcher().get_refs()
    except Exception:
        return sheet
    if not refs.get("available"):
        return sheet
    protected = _human_edited_paths(sheet_id)
    n_applied = 0
    if field_path in ("header.operador", "header.n_operador"):
        n_applied += _apply_operador_snap(sheet_id, sheet, refs, protected)
    if field_path == "header.setor_maquina":
        n_applied += _apply_codmaq_fill(sheet_id, sheet, refs, protected)
    if n_applied:
        return db.get_sheet(sheet_id) or sheet
    return sheet


def _maybe_record_operador_alias(sheet_id: int) -> None:
    """R91 — Persist OCR-corrupt → canonical mapping in operator aliases.

    Triggered after the operator manually edits header.operador or
    header.n_operador in the UI. If the new (name, cod) combination
    resolves to a known colaborador AND the original OCR-captured name
    was different, save it so future OCRs of the same corrupt string
    resolve directly via the lexicon (skip confusion-map).

    Idempotent + safe — silently bails when:
      - sheet has no raw_extraction OR no header
      - edit doesn't yield a numeric cod
      - cod not in colaboradores
      - canonical sname doesn't match the operator-typed name
    """
    import datetime as _dt
    sheet = db.get_sheet(sheet_id)
    if not sheet or not sheet.get("sheet_data"):
        return
    header = (sheet.get("sheet_data") or {}).get("header") or {}
    raw_header = (sheet.get("raw_extraction") or {}).get("header") or {}
    raw_ocr_name = str(raw_header.get("operador") or "").strip()
    new_name = str(header.get("operador") or "").strip()
    new_cod_str = str(header.get("n_operador") or "").strip()
    if not raw_ocr_name or not new_name:
        return
    if raw_ocr_name.upper() == new_name.upper():
        return  # nothing to memorize — operator's edit didn't change the name
    try:
        cod_int = int(new_cod_str.lstrip("0") or "0")
    except ValueError:
        return
    if cod_int <= 0:
        return
    refs = get_watcher().get_refs()
    colabs = refs.get("colaboradores") or {}
    entry = colabs.get(cod_int)
    if not entry or entry.get("sname", "").upper() != new_name.upper():
        return  # edit doesn't resolve to a known colaborador
    # Persist alias — keyed by normalized OCR name (UPPER + ASCII-stripped)
    import unicodedata
    nfd = unicodedata.normalize("NFD", raw_ocr_name)
    key = " ".join("".join(ch for ch in nfd if not unicodedata.combining(ch)).upper().split())
    if not key:
        return
    aliases_path = _REPO / "lexicons" / "operador_aliases.json"
    aliases: dict = {}
    if aliases_path.exists():
        try:
            aliases = json.loads(aliases_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            aliases = {}
    aliases[key] = {
        "cod": cod_int,
        "pernr": entry.get("pernr", ""),
        "sname": entry.get("sname", ""),
        "source": "manual",
        "added_at": _dt.datetime.now(tz=_dt.timezone.utc).isoformat(timespec="seconds"),
    }
    aliases_path.parent.mkdir(parents=True, exist_ok=True)
    aliases_path.write_text(
        json.dumps(aliases, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


@app.post("/sheet/{sheet_id}/edit", response_class=HTMLResponse)
async def sheet_edit(
    request: Request,
    sheet_id: int,
    field_path: str = Form(...),
    new_value: str = Form(""),
) -> Response:
    """HTMX endpoint — applies an edit + returns the cell HTML fragment."""
    # Round 34 — mobile cannot do full edits (only via /mobile/qtds-batch
    # which has whitelist of qty-related fields)
    if _is_mobile_request(request):
        raise HTTPException(403, "Edição só pode ser feita em desktop")
    # Round 50 — folha validada é read-only; sem edits posteriores.
    sheet_pre = db.get_sheet(sheet_id)
    if sheet_pre is None:
        raise HTTPException(404, f"sheet {sheet_id} not found")
    if sheet_pre.get("status") == "validated":
        raise HTTPException(409, "Folha já validada — edits bloqueados")
    # R136 — snapshot do cabeçalho ANTES do edit. Comparado com o estado
    # pós cross-check para devolver swaps out-of-band das células que mudaram
    # em consequência (ex: header.operador quando se edita header.n_operador).
    header_before = dict((sheet_pre.get("sheet_data") or {}).get("header") or {})
    # R136 — a célula header.data edita-se com <input type="date"> (ISO). O
    # armazenamento canónico continua DD-MM-YYYY (CSV, date_iso, filenames),
    # por isso converte-se aqui antes de persistir.
    if field_path == "header.data":
        _v = new_value.strip()
        if _ISO_DATE_RE.match(_v):
            new_value = f"{_v[8:10]}-{_v[5:7]}-{_v[0:4]}"
    try:
        old, new = db.apply_edit(sheet_id, field_path, new_value)
    except ValueError as e:
        raise HTTPException(400, str(e))
    sheet = db.get_sheet(sheet_id)
    if sheet is None:
        raise HTTPException(404, f"sheet {sheet_id} not found")
    # R91 — after editing header.operador or header.n_operador, try to
    # memorize the OCR→canonical mapping in the aliases lexicon so future
    # OCRs of the same corrupt name resolve directly.
    if field_path in ("header.operador", "header.n_operador"):
        try:
            _maybe_record_operador_alias(sheet_id)
        except Exception as e:
            print(f"[alias] sheet {sheet_id}: {e}", file=sys.stderr)
    sheet = _apply_lightweight_edit_snaps(sheet_id, field_path, sheet)
    # R123 — devolver o valor REAL persistido. O cross-check pesado corre em
    # background, mas snaps leves de cabeçalho podem já ter ajustado sheet_data.
    sheet = db.get_sheet(sheet_id) or sheet
    try:
        real_value = db._get_by_path(sheet.get("sheet_data") or {}, field_path)
    except Exception:
        real_value = new
    if real_value is None:
        real_value = new
    cells_by_path = (sheet.get("dq_audit") or {}).get("cells", {})
    (cc_status_by_path, cc_ref_by_path, cc_ref_title_by_path,
     cc_suspended_by_path, cc_snapped_by_path,
     cc_obra_concluida_by_path) = _build_cc_maps(sheet_id, allow_regen=False)

    def _render_cell(fp: str, val: object, *, edited: bool, oob: bool) -> str:
        """Render a single _cell.html fragment. ``oob=True`` adds
        hx-swap-oob so HTMX updates that cell by id without it being the
        request's swap target."""
        return templates.env.get_template("_cell.html").render(
            sheet_id=sheet_id,
            field_path=fp,
            value=val,
            audit=cells_by_path.get(fp, {}),
            edited=edited,
            cc_status_by_path=cc_status_by_path,
            cc_ref_by_path=cc_ref_by_path,
            cc_ref_title_by_path=cc_ref_title_by_path,
            cc_suspended_by_path=cc_suspended_by_path,
            cc_snapped_by_path=cc_snapped_by_path,
            cc_obra_concluida_by_path=cc_obra_concluida_by_path,
            sheet_status=sheet.get("status"),
            oob=oob,
        )

    # Primary cell — swapped into the edit target (hx-target=#cell-...).
    parts = [_render_cell(field_path, real_value, edited=(old != real_value), oob=False)]

    # R136 — out-of-band swaps. Qualquer célula do CABEÇALHO que tenha mudado
    # como efeito secundário do cross-check (ex: header.operador resolvido a
    # partir do novo header.n_operador via _apply_operador_snap; header.cod_maquina
    # via _apply_codmaq_fill) é re-renderada com hx-swap-oob para actualizar no
    # ecrã sem reload. Resolve o "mudo o nº do operador mas o nome não muda".
    header_after = (sheet.get("sheet_data") or {}).get("header") or {}
    header_fields = _template_ctx_for_sheet(sheet).get("header_fields", ()) or ()
    for f in header_fields:
        fp = f"header.{f}"
        if fp == field_path:
            continue  # já é a célula primária
        if str(header_before.get(f) or "") != str(header_after.get(f) or ""):
            parts.append(_render_cell(fp, header_after.get(f, ""), edited=True, oob=True))

    _start_sheet_cross_check({sheet_id}, profile_trigger="sheet_edit")
    return HTMLResponse("".join(parts))


# Factory deposit: CSVs go here automatically when a sheet is validated.
# Defaults to the local factory clone in C:\kanban\nifruka\... (set up
# by the user in this workspace). Set FACTORY_CSV_DIR env var to override
# or to "" to disable auto-deposit.
# R118 — usar resolve_kanban_path para cair em repo-local quando o disco
# C:\kanban\ não existe (laptop dev sem .env).
if os.environ.get("FACTORY_CSV_DIR", "_DEFAULT_") != "":
    from app.config import resolve_kanban_path
    _FACTORY_CSV_DIR = resolve_kanban_path(
        "FACTORY_CSV_DIR",
        r"C:\kanban\nifruka\02_Dados_Extraidos\csv",
        "kanban_refs/02_Dados_Extraidos/csv",
    )
else:
    _FACTORY_CSV_DIR = None


def _factory_csv_filename(sheet: dict) -> str:
    """Return the canonical filename to use when depositing a sheet's CSV
    in the factory dir. Prefer the operador+date encoded in sheet_data
    (e.g. ``JulioLima_2026.04.15-1.csv``) so the validator log lines stay
    human-readable; fall back to the raw image stem.
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


def _deposit_csv_to_factory(sheet_id: int) -> Path | None:
    """Write the sheet's 3-block CSV to FACTORY_CSV_DIR. Returns the path
    written, or None if the deposit dir isn't configured / doesn't exist /
    the sheet has no data. Idempotent: overwrites existing file."""
    if _FACTORY_CSV_DIR is None or not _FACTORY_CSV_DIR.exists():
        return None
    sheet = db.get_sheet(sheet_id)
    if sheet is None or not sheet.get("sheet_data"):
        return None
    filename = _factory_csv_filename(sheet)
    target = _FACTORY_CSV_DIR / filename
    csv_text = _to_3block_csv(Path(sheet["image_path"]).name, sheet["sheet_data"])
    target.write_text(csv_text, encoding="utf-8")
    return target


@app.post("/sheet/{sheet_id}/validate")
async def sheet_validate(
    sheet_id: int,
    request: Request,
    operador: str | None = Form(None),
    data: str | None = Form(None),
    n_operador: str | None = Form(None),
) -> RedirectResponse:
    # Round 34 — mobile cannot validate (server-side enforcement)
    if _is_mobile_request(request):
        raise HTTPException(403, "Validação só pode ser feita em desktop")
    # Round 50 — re-validate bloqueada; folha validada é final.
    sheet_pre = db.get_sheet(sheet_id)
    if sheet_pre is None:
        raise HTTPException(404, f"sheet {sheet_id} not found")
    if sheet_pre.get("status") == "validated":
        raise HTTPException(409, "Folha já validada — não é possível re-validar")

    header = (sheet_pre.get("sheet_data") or {}).get("header") or {}
    # R136 — dois fluxos partilham este endpoint:
    #   • sheet.html (Folha): a barra "Validar" deixou de ter inputs próprios
    #     (eram duplicados das células do cabeçalho e, por ficarem stale,
    #     revertiam a edição ao validar). NÃO envia campos → lê-se do CABEÇALHO,
    #     a única fonte de verdade. Não reescreve nada.
    #   • kanban_viewer.html: continua a confirmar data + nº + operador (dropdown)
    #     no próprio form → caminho clássico (R94): valida e aplica os valores.
    from_form = operador is not None or data is not None or n_operador is not None
    if from_form:
        operador_final = (operador or "").strip()
        if not operador_final:
            raise HTTPException(400, "operador em falta — corrige o cabeçalho antes de validar")
        data_iso = (data or "").strip()
        if not _ISO_DATE_RE.match(data_iso):
            raise HTTPException(400, f"data must be YYYY-MM-DD, got {data!r}")
        n_op_clean = (n_operador or "").strip()
        if not n_op_clean.isdigit() or len(n_op_clean) > 5:
            raise HTTPException(400, f"n_operador must be 1-5 digits, got {n_operador!r}")
        # Convert ISO → DD-MM-YYYY for storage compatibility. Apply edits before
        # the validation lock — standard apply_edit path keeps production_rows +
        # cross-check in sync.
        data_pt = f"{data_iso[8:10]}-{data_iso[5:7]}-{data_iso[0:4]}"
        if (header.get("data") or "").strip() != data_pt:
            try:
                db.apply_edit(sheet_id, "header.data", data_pt)
            except Exception:
                pass
        if (header.get("n_operador") or "").strip() != n_op_clean:
            try:
                db.apply_edit(sheet_id, "header.n_operador", n_op_clean)
            except Exception:
                pass
    else:
        operador_final = (header.get("operador") or "").strip()
        if not operador_final:
            raise HTTPException(
                400, "Operador em falta — corrige o cabeçalho antes de validar."
            )
        n_op_clean = (header.get("n_operador") or "").strip()
        if not n_op_clean.isdigit() or len(n_op_clean) > 5:
            raise HTTPException(
                400, "Nº operador inválido — corrige o cabeçalho (1-5 dígitos) antes de validar."
            )
        if db._normalize_data_pt_to_iso(header.get("data")) is None:
            raise HTTPException(
                400, "Data inválida — corrige a data no cabeçalho (dd-mm-aaaa) antes de validar."
            )

    # R126 — edição de cesta_n foi removida do sheet.html (validate desktop).
    # A cesta entra exclusivamente pelo fluxo mobile (capture.html → /mobile/qtds-batch).

    db.validate_sheet(sheet_id, operador_final)
    # R117 — kernel event: folha validada (lock confirmado pelo operador).
    try:
        kernel.emit_event("sheet_validated", {"sheet_id": sheet_id, "operador": operador_final})
    except Exception:
        pass
    # R113 — folha acabada de validar entra no cálculo de consumption.
    # Invalida cache para o /of-lookup seguinte ver os números actualizados.
    # R115 — também invalida o agregado /obras (qtd produzida muda).
    try:
        from app.pipeline.of_consumption import invalidate_cache
        invalidate_cache()
        from app.pipeline.obras_status import invalidate_cache as obras_inv
        obras_inv()
    except Exception:
        pass
    # Closed loop: drop CSV in the factory CSV dir so the next run of
    # ``kanban_csv2excel_novo_layout.py`` picks it up. Failure is silent —
    # the user can still pull the CSV via the /sheet/{id}/csv endpoint.
    try:
        _deposit_csv_to_factory(sheet_id)
    except Exception as e:
        traceback.print_exc()
        print(f"[factory deposit] sheet {sheet_id}: {e}", file=sys.stderr)
    # Update cross-check status in background (sheet just got validated).
    _start_sheet_cross_check({sheet_id}, profile_trigger="sheet_validate")
    # Learning loop — every 50 validated sheets, mine corrections + gold
    # into learnings. Runs in a background thread; failure is silent.
    try:
        from app.learning.scheduler import maybe_trigger_learning
        maybe_trigger_learning()
    except Exception as le:
        print(f"[learning] sheet {sheet_id} trigger: {le}", file=sys.stderr)
    return RedirectResponse("/queue", status_code=303)


# --- Round 30 + Round 32: cross-check admin endpoints ---
# Note: Round 32 removed the /dashboard/cross-check page from the UI —
# cross-check runs invisibly inline in /upload (see _run_and_store_cross_check).
# Admin APIs below remain for debug + ref-management.


# ============================================================================
# Mobile flow: capture-only + QTD edit (Round 31)
# Mobile users can ONLY take photos and edit qty/colunas_produzidas. Every
# other interaction with the system happens on desktop.
# ============================================================================

@app.get("/mobile/qtds")
def mobile_qtds(ids: str) -> JSONResponse:
    """Return minimal data needed for the mobile QTD-confirm screen.

    R114 — Para folhas com `cesta_n` no template (Expedição), inclui
    também o número da cesta e `row_fields_extra=['cesta_n']` para a UI
    mostrar a coluna apropriada.
    """
    try:
        sheet_ids = [int(s) for s in ids.split(",") if s.strip()]
    except ValueError:
        raise HTTPException(400, "ids must be comma-separated integers")
    from app.templates_registry import get_template
    out = []
    for sid in sheet_ids:
        sheet = db.get_sheet(sid)
        if sheet is None:
            continue
        sd = sheet.get("sheet_data") or {}
        h = sd.get("header", {}) or {}
        f = sd.get("footer", {}) or {}
        rows = sd.get("rows", []) or []
        # R114 — descobrir o template + campos extra editáveis em mobile
        template_name = db.get_sheet_template_name(sheet)
        try:
            tpl = get_template(template_name)
        except Exception:
            tpl = None
        # rev00 — o ecrã de QTDs é só para folhas de produção; um verso de
        # paragens não tem qtd → saltá-lo (senão mostrava uma tabela partida).
        if tpl is not None and not tpl.has_production_rows:
            continue
        # rev00 — SUCATA por linha entra como campo extra (espelha cesta_n).
        extra_fields = (
            [fname for fname in ("cesta_n", "sucata") if fname in tpl.row_fields]
            if tpl is not None else []
        )
        out.append({
            "sheet_id": sid,
            "status": sheet["status"],
            "operador": h.get("operador") or "",
            "data": h.get("data") or "",
            "template_name": template_name,
            "row_fields_extra": extra_fields,
            "rows": [
                {
                    "row_index": i,
                    "modelo": r.get("modelo", ""),
                    "cliente": r.get("cliente", ""),
                    "of": r.get("of", ""),
                    "qtd": r.get("qtd", ""),
                    # R114 — campo extra (vazio se template não usa cesta)
                    "cesta_n": r.get("cesta_n", ""),
                    # rev00 — SUCATA por linha (vazio se template não a usa)
                    "sucata": r.get("sucata", ""),
                }
                for i, r in enumerate(rows)
            ],
            "colunas_produzidas": f.get("colunas_produzidas", ""),
            "horas_trabalhadas": f.get("horas_trabalhadas", ""),
        })
    return JSONResponse({"sheets": out})


def _run_sheet_cross_checks(
    sheet_ids: tuple[int, ...],
    *,
    profile_trigger: str,
    rebuild_from_raw: bool = False,
) -> None:
    for sid in sheet_ids:
        try:
            kwargs = {"profile_trigger": profile_trigger}
            if rebuild_from_raw:
                kwargs["rebuild_from_raw"] = True
            _run_and_store_cross_check(sid, **kwargs)
        except Exception:
            traceback.print_exc()


def _start_sheet_cross_check(
    sheet_ids: set[int] | tuple[int, ...] | list[int],
    *,
    profile_trigger: str,
    rebuild_from_raw: bool = False,
) -> None:
    ordered_ids = tuple(sorted({int(sid) for sid in sheet_ids}))
    if not ordered_ids:
        return
    threading.Thread(
        target=_run_sheet_cross_checks,
        args=(ordered_ids,),
        kwargs={
            "profile_trigger": profile_trigger,
            "rebuild_from_raw": rebuild_from_raw,
        },
        name=f"{profile_trigger}-cross-check",
        daemon=True,
    ).start()


@app.post("/mobile/qtds-batch")
async def mobile_qtds_batch(request: Request) -> JSONResponse:
    """Apply a batch of qty edits at once. Body is JSON:
        { "edits": [ {sheet_id, field_path, value}, ... ] }

    Restricts field_path to qty/cesta_n/colunas_produzidas only —
    anything else is rejected. Re-cross-checks affected sheets in background.

    R123 — já NÃO valida folhas: o auto-validate mobile do R114/R122 foi
    revertido. Validar é um acto humano deliberado, feito no desktop.
    """
    body = await request.json()
    edits = body.get("edits", [])
    if not isinstance(edits, list):
        raise HTTPException(400, "edits must be a list")

    # Whitelist: qty + cesta_n (R114) + sucata (rev00) + footer counters
    row_field_re = re.compile(r"^rows\[(\d{1,3})\]\.(qtd|cesta_n|sucata)$")
    allowed_footer = {"footer.colunas_produzidas", "footer.horas_trabalhadas"}
    errors: list[dict] = []
    valid_edits: list[tuple[int, str, str]] = []
    sheet_cache: dict[int, dict] = {}

    for e in edits:
        if not isinstance(e, dict):
            errors.append({"edit": e, "error": "bad shape"})
            continue
        try:
            sid = int(e.get("sheet_id"))
            field_path = str(e.get("field_path") or "").strip()
            value = str(e.get("value") if e.get("value") is not None else "")
        except (TypeError, ValueError):
            errors.append({"edit": e, "error": "bad shape"})
            continue

        sheet = sheet_cache.get(sid)
        if sheet is None:
            sheet = db.get_sheet(sid)
            if sheet is not None:
                sheet_cache[sid] = sheet
        if sheet is None:
            errors.append({"edit": e, "error": f"sheet {sid} not found"})
            continue
        if sheet.get("status") == "validated":
            errors.append({"edit": e, "error": f"sheet {sid} is already validated"})
            continue
        data = sheet.get("sheet_data") or {}
        if not data:
            errors.append({"edit": e, "error": f"sheet {sid} has no extraction yet"})
            continue

        row_match = row_field_re.match(field_path)
        if row_match:
            row_index = int(row_match.group(1))
            row_field = row_match.group(2)
            rows = data.get("rows") or []
            if row_index >= len(rows):
                errors.append({
                    "edit": e,
                    "error": f"row index {row_index} out of range",
                })
                continue
            # cesta_n (R114) e sucata (rev00) só são aceites se o template os
            # declarar; qtd é universal.
            if row_field in ("cesta_n", "sucata"):
                from app.templates_registry import get_template
                tpl = get_template(db.get_sheet_template_name(sheet))
                if row_field not in tpl.row_fields:
                    errors.append({
                        "edit": e,
                        "error": f"{row_field} not allowed for this template",
                    })
                    continue
        elif field_path not in allowed_footer:
            errors.append({"edit": e, "error": f"field {field_path} not allowed on mobile"})
            continue
        valid_edits.append((sid, field_path, value))

    if errors:
        return JSONResponse({
            "ok": False,
            "applied": 0,
            "errors": errors,
            "sheets_updated": [],
        }, status_code=400)

    edits_by_sheet: dict[int, list[tuple[str, str]]] = {}
    for sid, field_path, value in valid_edits:
        edits_by_sheet.setdefault(sid, []).append((field_path, value))

    applied = 0
    affected_sheets: set[int] = set()
    for sid, sheet_edits in edits_by_sheet.items():
        try:
            db.apply_edits_batch(sid, sheet_edits)
            applied += len(sheet_edits)
            affected_sheets.add(sid)
        except ValueError as ex:
            errors.append({"sheet_id": sid, "error": str(ex)})

    if errors:
        return JSONResponse({
            "ok": False,
            "applied": applied,
            "errors": errors,
            "sheets_updated": sorted(affected_sheets),
        }, status_code=400)

    _start_sheet_cross_check(affected_sheets, profile_trigger="mobile_qtd")

    return JSONResponse({
        "ok": True,
        "applied": applied,
        "errors": errors,
        "sheets_updated": sorted(affected_sheets),
    })


# ============================================================================
# Mobile guard — desktop-only pages redirect mobile clients to /capture
# ============================================================================

_DESKTOP_ONLY_PREFIXES = (
    "/sheet/",       # editing/review page (full table edit)
    "/kanbans",      # kanban viewer (operator selector + nav)
    "/dashboard",    # all dashboards (production, workers, jobs, etc.)
    "/queue",        # full sheets list with edit/validate
    "/pair",         # QR code page (only useful on desktop showing the QR)
    "/export",       # bulk Excel export (covers /export and /export/cpis)
    "/excel",        # R66 — continuous data page (desktop-only)
)


@app.middleware("http")
async def _mobile_guard(request: Request, call_next):
    """Redirect mobile clients away from desktop-only pages.

    Allowed on mobile: /capture, /mobile/*, /upload, /image/*, static, admin
    APIs (used by JS only — not human-navigated). Everything else → /capture.

    HTMX/AJAX requests pass through (they need to keep working when mobile JS
    triggers them on /capture itself, e.g. /upload).
    """
    path = request.url.path
    is_mobile = (
        any(p in (request.headers.get("user-agent") or "").lower()
            for p in ("mobile", "iphone", "android", "ipad", "ipod"))
    )
    # Only redirect human navigations — not API/HTMX hits
    is_html_request = (
        request.method == "GET"
        and "text/html" in (request.headers.get("accept") or "")
    )
    if is_mobile and is_html_request:
        for prefix in _DESKTOP_ONLY_PREFIXES:
            if path.startswith(prefix):
                return RedirectResponse("/capture", status_code=303)
    return await call_next(request)


@app.get("/admin/refs-status")
def admin_refs_status() -> JSONResponse:
    """Show current state of the SAP/plan refs (which Excel files loaded,
    when, n_lotes/n_ofs counts). Plus aggregate cross-check summary."""
    return JSONResponse({
        "refs": get_watcher().status(),
        "refs_importer": ref_importer.status(),
        "summary": load_summary(),
    })


@app.post("/admin/import-refs")
def admin_import_refs() -> JSONResponse:
    """Manually import refs from the configured shared folder."""
    result = ref_importer.import_refs_from_config()
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@app.get("/admin/queue-status")
def admin_queue_status() -> JSONResponse:
    """R71 — health + depth of the background OCR queue. Used for debug
    and the /admin pages to show worker liveness."""
    return JSONResponse({
        "queue_size": ocr_queue.queue_size(),
        "worker_alive": ocr_queue.worker_alive(),
    })


@app.get("/admin/qwen-tools-test")
def qwen_tools_test() -> JSONResponse:
    """R120 — testa se o modelo Ollama actual invoca function calling.

    Faz um pedido mínimo com uma tool fake ``get_now``. Se a resposta tem
    ``tool_calls[]`` populado, o modelo suporta tools. Se não, sabemos que
    o fallback é a solução correcta.
    """
    from app.config import get_settings
    s = get_settings()
    payload = {
        "model": s.ollama_text_model,
        "messages": [
            {"role": "user", "content": "Qual a hora actual? Usa a tool get_now."}
        ],
        "tools": [{
            "type": "function",
            "function": {
                "name": "get_now",
                "description": "Devolve a data e hora actual.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }],
        "stream": False,
        "think": False,
        "options": {"temperature": 0.1},
    }
    try:
        with httpx.Client(timeout=60.0) as c:
            r = c.post(f"{str(s.ollama_url).rstrip('/')}/api/chat", json=payload)
            r.raise_for_status()
            data = r.json()
        msg = data.get("message") or {}
        tool_calls = msg.get("tool_calls") or []
        return JSONResponse({
            "model": s.ollama_text_model,
            "supports_tools": len(tool_calls) > 0,
            "tool_calls_in_response": tool_calls,
            "content_preview": (msg.get("content") or "")[:200],
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/sheet/{sheet_id}/shadow-view", response_class=HTMLResponse)
def sheet_shadow_view(request: Request, sheet_id: int) -> Response:
    """R253/F2 — triagem do soak (passo 2 do procedimento de flip,
    docs/CROSS_EVALUATION_PROTOCOL.md): SÓ as divergências produção-vs-
    sombra (a mesma régua do shadow_agreement.py), identidade primeiro,
    com a telemetria do posterior da sombra e o carimbo de triagem."""
    from app.cross_check.shadow_diff import diff_prod_vs_shadow
    from app.cross_check.storage import load_sheet_cross_check

    sheet = db.get_sheet(sheet_id)
    if sheet is None:
        raise HTTPException(404, f"sheet {sheet_id} not found")
    if not sheet.get("shadow_scoring_json"):
        raise HTTPException(404, f"sheet {sheet_id} não tem sombra")
    try:
        shadow = json.loads(sheet["shadow_scoring_json"])
    except (TypeError, ValueError):
        shadow = {}
    # A produção NÃO vive em sheets.cross_check (coluna não existe) — vive
    # no storage por ficheiros (kanban_refs/03_Cross_Check + índice).
    prod = load_sheet_cross_check(sheet_id, include_stale=True) or {}
    diff = diff_prod_vs_shadow(prod, shadow)
    telemetry = [
        {"row": i,
         "p_of": (r or {}).get("winner_p_of"),
         "p_h0": (r or {}).get("winner_p_h0"),
         "entropy": (r or {}).get("winner_posterior_entropy_bits"),
         "p_field": (r or {}).get("winner_p_field")}
        for i, r in enumerate(shadow.get("rows") or [])
        if (r or {}).get("winner_p_of") is not None
    ]
    run = None
    with db.conn() as c:
        row = c.execute(
            "SELECT status, error_message, duration_ms FROM shadow_runs "
            "WHERE sheet_id = ? ORDER BY id DESC LIMIT 1", (sheet_id,),
        ).fetchone()
        if row is not None:
            run = dict(row)
    return templates.TemplateResponse(request, "shadow_view.html", {
        "sheet_id": sheet_id,
        "diff": diff,
        "telemetry": telemetry,
        "run": run,
        "shadow_scored_at": sheet.get("shadow_scored_at"),
        "shadow_triaged_at": sheet.get("shadow_triaged_at"),
        "shadow_triage_note": sheet.get("shadow_triage_note"),
    })


@app.post("/sheet/{sheet_id}/shadow-triage")
def sheet_shadow_triage(sheet_id: int, note: str = Form("")) -> Response:
    """R253/F2 — carimbo de triagem humana (auditável no soak)."""
    sheet = db.get_sheet(sheet_id)
    if sheet is None or not sheet.get("shadow_scoring_json"):
        raise HTTPException(404)
    db.mark_shadow_triaged(sheet_id, note)
    return RedirectResponse(f"/sheet/{sheet_id}/shadow-view", status_code=303)


@app.get("/shadow-queue", response_class=HTMLResponse)
def shadow_queue_page(request: Request) -> Response:
    """R253/F2 — folhas com sombra por triar (ordenadas por mais recente)."""
    return templates.TemplateResponse(request, "shadow_queue.html", {
        "sheets": db.shadow_queue(),
    })


@app.get("/sheet/{sheet_id}/status")
def sheet_status(sheet_id: int) -> JSONResponse:
    """R71 — JSON polling endpoint for the mobile capture flow + desktop
    auto-refresh. Returns minimal fields so the JS can drive its state
    machine without re-rendering the full sheet view.
    """
    sheet = db.get_sheet(sheet_id)
    if sheet is None:
        raise HTTPException(404)
    return JSONResponse({
        "id": sheet_id,
        "status": sheet.get("status"),
        "queue_size": ocr_queue.queue_size(),
        "error_message": sheet.get("error_message"),
    })


@app.get("/sheet/{sheet_id}/status-fragment", response_class=HTMLResponse)
def sheet_status_fragment(sheet_id: int) -> Response:
    """R71 — HTMX-friendly partial for the desktop /sheet/{id} pending
    banner. While status is 'pending', returns the banner so HTMX swaps
    it back in (preserving the every-2s trigger). When status transitions
    out of 'pending', returns an empty body with ``HX-Refresh: true``,
    causing the browser to refresh and render the now-extracted view.
    """
    sheet = db.get_sheet(sheet_id)
    if sheet is None:
        raise HTTPException(404)
    if sheet.get("status") != "pending":
        return Response(
            content="",
            status_code=200,
            headers={"HX-Refresh": "true"},
        )
    queue_size = ocr_queue.queue_size()
    html = (
        '<div class="alert info" '
        f'hx-get="/sheet/{sheet_id}/status-fragment" '
        'hx-trigger="every 2s" hx-swap="outerHTML">'
        '<b>⏳ A processar OCR...</b><br>'
        '<span class="muted tiny">'
        f'{queue_size} folha(s) na fila — esta página actualiza-se sozinha'
        '</span></div>'
    )
    return HTMLResponse(html)


@app.post("/admin/reload-refs")
def admin_reload_refs() -> JSONResponse:
    """Force-reload SAP + plan_colunas Excel files (skip mtime check).
    Re-cross-checks sheets in background so the request stays quick."""
    refs = get_watcher().force_reload()
    # R115 — refs novas invalidam o agregado /obras
    try:
        from app.pipeline.obras_status import invalidate_cache as obras_inv
        obras_inv()
    except Exception:
        pass
    # Fix double counting — plano novo muda o cutoff dos kanbans: o cache
    # do consumo tem de refrescar já (o cutoff também é chave do cache,
    # isto é cinto-e-suspensórios para o TTL).
    try:
        from app.pipeline.of_consumption import invalidate_cache as cons_inv
        cons_inv()
    except Exception:
        pass
    revalidation_started = _start_revalidation()
    return JSONResponse({
        "ok": True,
        "refs_loaded_at": refs.get("loaded_at"),
        "n_lotes": refs.get("stats", {}).get("n_lotes", 0),
        "n_ofs": refs.get("stats", {}).get("n_ofs", 0),
        "sheets_revalidated": 0,
        "revalidation_started": revalidation_started,
    })


# ===================== R104 — página de refs SAP/plan =====================
# Upload dos workbooks de referência usados por OCR/cross-check.

_REFS_FILENAMES = {
    "plan": "plan_colunas_cpis.xlsx",
    "stocksap": "StockSAP.xlsx",
    "maquinas": "maquinas.xlsx",
    "colaboradores": "ListaColaboradores.xlsx",
}
_REFS_LABELS = {
    "plan": "Plano",
    "stocksap": "StockSAP",
    "maquinas": "Máquinas",
    "colaboradores": "Colaboradores",
}
_REFS_WATCHER_ATTRS = {
    "plan": "plan_path",
    "stocksap": "sap_path",
    "maquinas": "maq_path",
    "colaboradores": "colab_path",
}
_REFS_STATUS_KEYS = {
    "plan": "plan",
    "stocksap": "sap",
    "maquinas": "maquinas",
    "colaboradores": "colaboradores",
}
_REFS_SHA_KEYS = {
    "plan": "plan_sha256",
    "stocksap": "sap_sha256",
    "maquinas": "maquinas_sha256",
    "colaboradores": "colab_sha256",
}
_REFS_COUNT_STAT_KEYS = {
    "plan": "n_plan_rows",
    "stocksap": "n_lotes",
    "maquinas": "n_maquinas",
    "colaboradores": "n_colaboradores",
}

# Progresso da re-validação cross-check em background (1 corrida de cada vez).
_revalidation_state: dict = {
    "running": False, "done": 0, "total": 0,
    "started_at": None, "finished_at": None,
}
_revalidation_lock = threading.Lock()


def _revalidate_all_sheets_bg() -> None:
    """Re-cross-check every non-pending/-error sheet against the freshly
    loaded refs. Runs in a daemon thread so the upload response is instant."""
    try:
        sheets = [
            s for s in db.list_sheets(limit=10000)
            if s["status"] not in ("error", "pending")
        ]
        with _revalidation_lock:
            _revalidation_state.update(
                running=True, done=0, total=len(sheets), finished_at=None,
                started_at=dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            )
        for s in sheets:
            try:
                _run_and_store_cross_check(s["id"], rebuild_from_raw=True)
            except Exception:
                traceback.print_exc()
            with _revalidation_lock:
                _revalidation_state["done"] += 1
    finally:
        with _revalidation_lock:
            _revalidation_state["running"] = False
            _revalidation_state["finished_at"] = (
                dt.datetime.now().astimezone().isoformat(timespec="seconds"))


# Destinos permitidos para o `back` dos POSTs de refs — o corpo da página
# vive em /refs (URL histórico) e /admin/referencias (Task C).
_REFS_BACK_PATHS = ("/refs", "/admin/referencias")


def _refs_redirect(
    param: str, message: str, back: str = "/refs",
) -> RedirectResponse:
    if back not in _REFS_BACK_PATHS:
        back = "/refs"
    return RedirectResponse(
        f"{back}?{param}={quote_plus(message)}", status_code=303,
    )


def _inspect_refs_xlsx(path: Path, kind: str) -> tuple[str | None, dict]:
    """Validate refs workbook and return lightweight file stats.

    Defensive: a bad upload must never replace a good live refs file.
    """
    return ref_importer.inspect_refs_xlsx(path, kind)


def _validate_refs_xlsx(path: Path, kind: str) -> str | None:
    """Back-compat wrapper used by older tests/callers."""
    err, _info = _inspect_refs_xlsx(path, kind)
    return err


def _fmt_mtime(ts: float | None) -> str:
    """Epoch float → readable local datetime (for the refs status card)."""
    if not ts:
        return "—"
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _refs_status_card(
    status: dict,
    stats: dict,
    kind: str,
    *,
    big: int,
    label: str,
    sub: str,
) -> dict:
    st = status.get(_REFS_STATUS_KEYS[kind], {}) or {}
    sha = st.get("sha256") or ""
    return {
        "kind": kind,
        "filename": _REFS_FILENAMES[kind],
        "big": big,
        "label": label,
        "sub": sub,
        "date": _fmt_mtime(st.get("mtime")),
        "hash_short": sha[:8] if sha else "—",
        "path": st.get("path") or "—",
        "size": st.get("size") or 0,
    }


def _refs_upload_count(kind: str, refs: dict, upload_info: dict) -> int:
    stats = refs.get("stats", {}) or {}
    stat_key = _REFS_COUNT_STAT_KEYS[kind]
    return int(stats.get(stat_key) or upload_info.get(stat_key) or upload_info.get("n_rows") or 0)


def _start_revalidation() -> bool:
    """Arranca a re-validação cross-check em background (1 corrida de cada
    vez). Devolve True se arrancou, False se já estava a correr."""
    with _revalidation_lock:
        if _revalidation_state["running"]:
            return False
        _revalidation_state.update(
            running=True, done=0, total=0, finished_at=None,
            started_at=dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        )
    threading.Thread(target=_revalidate_all_sheets_bg, daemon=True).start()
    return True


def _refs_context(request: Request) -> dict:
    """Contexto do corpo de refs (_refs_content.html) — partilhado entre
    /refs (URL histórico) e /admin/referencias (Task C)."""
    from app.cross_check import refs_uploads
    refs = get_watcher().get_refs()
    status = get_watcher().status()
    stats = refs.get("stats", {}) or {}
    plan_status = status.get("plan", {}) or {}
    plan_sha = plan_status.get("sha256") or ""
    sap_status = status.get("sap", {}) or {}
    sap_sha = sap_status.get("sha256") or ""
    maq_status = status.get("maquinas", {}) or {}
    maq_sha = maq_status.get("sha256") or ""
    colab_status = status.get("colaboradores", {}) or {}
    colab_sha = colab_status.get("sha256") or ""
    refs_cards = [
        _refs_status_card(
            status, stats, "plan",
            big=stats.get("n_ofs", 0),
            label="OFs no plano",
            sub=(
                f"{stats.get('n_clientes', 0)} clientes · "
                f"{stats.get('n_plan_rows', 0)} linhas · "
                f"{stats.get('n_ovs', 0)} OVs"
            ),
        ),
        _refs_status_card(
            status, stats, "stocksap",
            big=stats.get("n_lotes", 0),
            label="lotes no StockSAP",
            sub="espessura e largura por lote",
        ),
        _refs_status_card(
            status, stats, "maquinas",
            big=stats.get("n_maquinas", 0),
            label="máquinas mapeadas",
            sub="código, setor e coluna Excel",
        ),
        _refs_status_card(
            status, stats, "colaboradores",
            big=stats.get("n_colaboradores", 0),
            label="colaboradores",
            sub="operadores e códigos SAP",
        ),
    ]
    upload_cards = [
        {
            "kind": "plan",
            "title": "Carregar plano",
            "subtitle": "plan_colunas_cpis.xlsx — OFs, OVs, clientes, fases",
            "button": "Carregar plano",
            "date": _fmt_mtime(plan_status.get("mtime")),
            "hash_short": plan_sha[:8] if plan_sha else "—",
        },
        {
            "kind": "stocksap",
            "title": "Carregar StockSAP",
            "subtitle": "StockSAP.xlsx — lotes, espessura, largura",
            "button": "Carregar StockSAP",
            "date": _fmt_mtime(sap_status.get("mtime")),
            "hash_short": sap_sha[:8] if sap_sha else "—",
        },
        {
            "kind": "maquinas",
            "title": "Carregar máquinas",
            "subtitle": "maquinas.xlsx — códigos, setores, fases",
            "button": "Carregar máquinas",
            "date": _fmt_mtime(maq_status.get("mtime")),
            "hash_short": maq_sha[:8] if maq_sha else "—",
        },
        {
            "kind": "colaboradores",
            "title": "Carregar colaboradores",
            "subtitle": "ListaColaboradores.xlsx — operadores e códigos",
            "button": "Carregar colaboradores",
            "date": _fmt_mtime(colab_status.get("mtime")),
            "hash_short": colab_sha[:8] if colab_sha else "—",
        },
    ]
    return {
        "refs_status": status,
        "stats": stats,
        "uploads": refs_uploads.recent(),
        "refs_cards": refs_cards,
        "upload_cards": upload_cards,
        "refs_importer": ref_importer.status(),
        "refs_kind_labels": _REFS_LABELS,
        "revalidation": dict(_revalidation_state),
        "sap_file_date": _fmt_mtime(sap_status.get("mtime")),
        "sap_hash_short": sap_sha[:8] if sap_sha else "—",
        "sap_path": sap_status.get("path") or "—",
        "plan_file_date": _fmt_mtime(plan_status.get("mtime")),
        "plan_hash_short": plan_sha[:8] if plan_sha else "—",
        "plan_path": plan_status.get("path") or "—",
        "plan_size": plan_status.get("size") or 0,
        "maquinas_file_date": _fmt_mtime(maq_status.get("mtime")),
        "maquinas_hash_short": maq_sha[:8] if maq_sha else "—",
        "maquinas_path": maq_status.get("path") or "—",
        "colaboradores_file_date": _fmt_mtime(colab_status.get("mtime")),
        "colaboradores_hash_short": colab_sha[:8] if colab_sha else "—",
        "colaboradores_path": colab_status.get("path") or "—",
        "flash_ok": request.query_params.get("ok"),
        "flash_err": request.query_params.get("err"),
    }


@app.get("/refs", response_class=HTMLResponse)
def refs_page(request: Request) -> Response:
    """Página para carregar refs e ver o estado dos workbooks ativos."""
    ctx = _refs_context(request)
    ctx["active_tab"] = "refs"
    ctx["refs_back"] = "/refs"
    return templates.TemplateResponse(request, "refs.html", ctx)


@app.post("/refs/upload")
async def refs_upload(
    kind: str = Form(...),
    file: UploadFile = File(...),
    back: str = Form("/refs"),
) -> Response:
    """Recebe um workbook de refs, valida-o e substitui
    o ficheiro vivo. Recarrega as refs DIRETO do ficheiro (sem acumulação
    histórica). NÃO re-cross-checka folhas — isso é o botão 'Re-validar'."""
    from app.cross_check import refs_uploads
    from app.cross_check.ref_watcher import file_sha256
    if kind not in _REFS_FILENAMES:
        raise HTTPException(400, "kind inválido")
    if not file.filename:
        return _refs_redirect("err", "sem ficheiro", back=back)
    if Path(file.filename).suffix.lower() not in (".xlsx", ".xlsm"):
        return _refs_redirect("err", "o ficheiro tem de ser .xlsx", back=back)

    # R118 — rede de segurança global: qualquer exceção (PermissionError no
    # mkdir, falha do watcher, etc.) é silenciosa hoje e dá página em branco
    # ao operador. Captura e devolve mensagem útil em ?err=...
    try:
        watcher = get_watcher()
        target = getattr(watcher, _REFS_WATCHER_ATTRS[kind])
        target.parent.mkdir(parents=True, exist_ok=True)
        # Temp file keeps the .xlsx suffix — openpyxl validates by extension.
        tmp = target.with_name(f"{target.stem}.upload-tmp{target.suffix}")

        bytes_written = 0
        with tmp.open("wb") as f:
            while chunk := await file.read(1024 * 1024):
                bytes_written += len(chunk)
                if bytes_written > _MAX_UPLOAD_BYTES:
                    f.close()
                    tmp.unlink(missing_ok=True)
                    return _refs_redirect("err", "ficheiro demasiado grande", back=back)
                f.write(chunk)

        err, upload_info = _inspect_refs_xlsx(tmp, kind)
        if err:
            tmp.unlink(missing_ok=True)
            return _refs_redirect("err", f"ficheiro rejeitado: {err}", back=back)
        upload_sha = file_sha256(tmp)

        # R134 — backup do ficheiro vivo ANTES de o substituir, para poder
        # restaurar se a validação/reload falhar (um ficheiro mau nunca fica
        # ativo com refs inconsistentes nem entala scans). Best-effort: se não
        # der para copiar, segue sem rollback em vez de bloquear o upload.
        backup: Path | None = None
        if target.exists():
            backup = target.with_name(f"{target.stem}.prevbak{target.suffix}")
            try:
                shutil.copy2(target, backup)
            except Exception:
                backup = None

        # os.replace falha se o ficheiro vivo estiver aberto (ex.: Excel) — tenta
        # algumas vezes antes de desistir com um erro claro.
        replaced = False
        for _ in range(5):
            try:
                os.replace(tmp, target)
                replaced = True
                break
            except PermissionError:
                await asyncio.sleep(0.3)
        if not replaced:
            tmp.unlink(missing_ok=True)
            if backup is not None:
                backup.unlink(missing_ok=True)
            return _refs_redirect("err", "ficheiro em uso - fecha o Excel e tenta outra vez", back=back)

        def _rollback(msg: str) -> Response:
            # R134 — restaura o ficheiro anterior e recarrega refs a partir
            # dele, deixando ficheiro vivo e refs em memória consistentes.
            if backup is not None and backup.exists():
                try:
                    os.replace(backup, target)
                    watcher.force_reload()
                except Exception:
                    traceback.print_exc()
            return _refs_redirect("err", msg, back=back)

        active_sha = file_sha256(target)
        if active_sha != upload_sha:
            return _rollback(
                "upload falhou verificação: o ficheiro ativo não tem o mesmo hash",
            )

        refs = watcher.force_reload()  # recarrega direto do ficheiro
        if refs.get(_REFS_SHA_KEYS[kind]) != active_sha:
            return _rollback(
                "upload falhou verificação: refs carregadas não batem o ficheiro ativo",
            )

        # Sucesso — descartar o backup transitório.
        if backup is not None:
            backup.unlink(missing_ok=True)

        # R115 + upload hardening — refs novas invalidam agregados/caches.
        try:
            from app.pipeline.obras_status import invalidate_cache as obras_inv
            obras_inv()
        except Exception:
            pass
        try:
            from app.pipeline.of_consumption import invalidate_cache as of_inv
            of_inv()
        except Exception:
            pass
        stats = refs.get("stats", {})
        n_rows = _refs_upload_count(kind, refs, upload_info)
        # R134 — `stats` tem sempre as chaves (default 0), por isso o fallback
        # ao upload_info só dispara com `or` (e não com get(key, default)).
        n_ofs = stats.get("n_ofs") or upload_info.get("n_ofs", 0)
        n_ovs = stats.get("n_ovs") or upload_info.get("n_ovs", 0)
        # R118 — record() é best-effort; nunca falhar o ?ok=
        try:
            refs_uploads.record(
                kind, target.name, n_rows,
                sha256=active_sha,
                n_ofs=n_ofs if kind == "plan" else None,
                n_ovs=n_ovs if kind == "plan" else None,
                size=target.stat().st_size,
            )
        except Exception:
            traceback.print_exc()
        if kind == "plan":
            ok_msg = (
                f"Plano atualizado: {n_rows} linhas, {n_ofs} OFs, "
                f"{n_ovs} OVs, hash {active_sha[:8]}"
            )
        elif kind == "stocksap":
            ok_msg = f"StockSAP atualizado: {n_rows} lotes, hash {active_sha[:8]}"
        elif kind == "maquinas":
            ok_msg = f"Máquinas atualizadas: {n_rows} máquinas, hash {active_sha[:8]}"
        else:
            ok_msg = (
                f"Colaboradores atualizados: {n_rows} colaboradores, "
                f"hash {active_sha[:8]}"
            )
        return _refs_redirect("ok", ok_msg, back=back)
    except Exception as e:
        # R118 — captura qualquer exceção não tratada e devolve mensagem
        # útil ao operador (antes: silêncio / página em branco).
        traceback.print_exc()
        msg = str(e)[:80].replace("\n", " ")
        return _refs_redirect("err", f"erro inesperado: {msg}", back=back)


@app.post("/refs/import-folder")
def refs_import_folder(back: str = Form("/refs")) -> Response:
    """Importa refs da pasta partilhada configurada."""
    result = ref_importer.import_refs_from_config()
    if result.get("ok"):
        imported = result.get("imported") or []
        skipped = result.get("skipped") or []
        if imported:
            kinds = ", ".join(i.get("kind", "?") for i in imported)
            return _refs_redirect(
                "ok",
                f"importação da pasta: {len(imported)} ficheiro(s) atualizado(s) ({kinds})",
                back=back,
            )
        return _refs_redirect(
            "ok",
            f"importação da pasta: sem alterações ({len(skipped)} já iguais)",
            back=back,
        )
    errors = result.get("error") or "; ".join(
        str(e.get("error")) for e in result.get("errors", []) if e.get("error")
    )
    return _refs_redirect(
        "err",
        f"importação da pasta falhou: {errors or 'erro desconhecido'}",
        back=back,
    )


@app.post("/refs/revalidate")
def refs_revalidate(back: str = Form("/refs")) -> Response:
    """Botão 'Re-validar folhas' — re-corre o cross-check de TODAS as folhas
    (extracted + validated) contra as refs atuais, em background."""
    if _start_revalidation():
        return _refs_redirect("ok", "re-validacao iniciada", back=back)
    return _refs_redirect("err", "re-validacao ja esta a correr", back=back)


@app.get("/refs/revalidation-status", response_class=HTMLResponse)
def refs_revalidation_status(request: Request) -> Response:
    """Fragmento HTMX com o progresso da re-validação cross-check."""
    return templates.TemplateResponse(request, "_refs_revalidation.html", {
        "revalidation": dict(_revalidation_state),
    })


@app.get("/admin/to-analisar")
def admin_to_analisar(limit: int | None = None) -> JSONResponse:
    """Inbox of cells flagged ANALISAR — for the supervisor's review queue."""
    return JSONResponse(load_to_analisar(limit=limit))


@app.get("/admin/refs-lookup")
def admin_refs_lookup(q: str = "", include_done: int = 0) -> JSONResponse:
    """Passo 'validação' do wizard — pesquisa OF/OV/modelo no plano (via
    _refs_lookup, sem folha/fase) + lote no StockSAP."""
    query = (q or "").strip()
    if not query:
        return JSONResponse({
            "found": False, "mode": "none", "q": "", "of": "", "entries": [],
        })
    result = _refs_lookup(query, include_done=bool(include_done), phase=None)
    if result.get("found"):
        return JSONResponse(result)
    # Tier 4 — lote (StockSAP). R35: lotes indexados com e sem prefixo M.
    refs = get_watcher().get_refs() or {}
    lotes = refs.get("lotes_sap_full") or {}
    key = query.upper()
    rec = lotes.get(key) or lotes.get(f"M{key}")
    if rec:
        return JSONResponse({
            "found": True, "mode": "lote", "q": query, "of": "",
            "entries": [], "n_entries": 0, "n_total": 1,
            "lote": {**rec, "lote": key},
        })
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# Task C — página /admin com separadores (Referências | Kanbans | Unidades |
# KPIs). NOTA: as rotas fixas /admin/* (refs-status, to-analisar, …) estão
# registadas ACIMA, por isso ganham ao wildcard /admin/{tab}.
# ---------------------------------------------------------------------------

_ADMIN_TABS = ("referencias", "kanbans", "unidades", "kpis")


def _admin_redirect(tab: str, param: str, message: str) -> RedirectResponse:
    if tab not in _ADMIN_TABS:
        tab = "referencias"
    return RedirectResponse(
        f"/admin/{tab}?{param}={quote_plus(message)}", status_code=303,
    )


@app.get("/admin")
def admin_root() -> RedirectResponse:
    return RedirectResponse("/admin/referencias", status_code=303)


@app.get("/admin/{tab}", response_class=HTMLResponse)
def admin_page(request: Request, tab: str) -> Response:
    """Página de administração — cada separador carrega só o seu contexto."""
    if tab not in _ADMIN_TABS:
        raise HTTPException(404, "separador desconhecido")
    ctx: dict = {
        "admin_tab": tab,
        "flash_ok": request.query_params.get("ok"),
        "flash_err": request.query_params.get("err"),
    }
    if tab == "referencias":
        ctx.update(_refs_context(request))
        ctx["refs_back"] = "/admin/referencias"
    elif tab == "unidades":
        ctx["unidades"] = db.list_unidades(only_ativo=False)
        ctx["trofa_id"] = db.trofa_unidade_id()
    elif tab == "kanbans":
        from app.templates_registry import TEMPLATES
        ctx["kanban_templates"] = db.list_kanban_templates()
        ctx["n_builtin_templates"] = sum(
            1 for t in TEMPLATES.values() if t.source == "builtin")
        ctx["unidades_ativas"] = db.list_unidades()
        ctx["crossable_fields"] = sorted(template_store.CROSSABLE_FIELDS)
        ctx["known_row_fields"] = sorted(template_store.KNOWN_ROW_FIELDS)
    elif tab == "kpis":
        state = kpi_params.load_state()
        ctx["kpi_state"] = state
        ctx["kpi_default_ids"] = [k["id"] for k in kpi_params.DEFAULT_KPIS]
        ctx["kpi_scope_variables"] = kpi_params.SCOPE_VARIABLES
        ctx["kpi_preview_date"] = _kpi_last_production_day()
    ctx["active_tab"] = "admin"
    return templates.TemplateResponse(request, "admin.html", ctx)


def _kpi_last_production_day() -> str | None:
    """Último dia com produção — alvo do preview de fórmulas de KPI."""
    with db.conn() as c:
        r = c.execute(
            "SELECT MAX(sheet_iso_date) AS d FROM production_rows "
            "WHERE sheet_iso_date IS NOT NULL"
        ).fetchone()
        return r["d"] if r and r["d"] else None


@app.post("/admin/kpis/validate")
async def admin_kpis_validate(request: Request) -> JSONResponse:
    """Valida um conjunto candidato de KPIs e devolve o preview calculado
    contra o último dia com produção (sem gravar nada)."""
    if _is_mobile_request(request):
        raise HTTPException(403, "Edição de KPIs só em desktop")
    body = await request.json()
    kpis_in = body.get("kpis") or []
    errors: dict[str, str] = {}
    seen: set[str] = set()
    for k in kpis_in:
        kid = str(k.get("id") or "?")
        if kid in seen:
            errors[kid] = "id duplicado"
            continue
        seen.add(kid)
        err = kpi_params.validate_kpi_def(k)
        if err:
            errors[kid] = err
    preview_cards = None
    variables = None
    preview_date = _kpi_last_production_day()
    if not errors and preview_date:
        candidate = kpi_params.normalize_kpis(kpis_in)
        ov = kpis.production_overview(preview_date, "day", kpi_defs=candidate)
        preview_cards = ov["kpi_cards"]
        variables = ov["kpi_variables"]["totals"]
    return JSONResponse({
        "ok": not errors,
        "errors": errors,
        "preview_date": preview_date,
        "preview": preview_cards,
        "variables": variables,
    })


@app.post("/admin/kpis/save")
async def admin_kpis_save(request: Request) -> JSONResponse:
    """Grava o conjunto completo de KPIs (versão otimista: 409 se outro
    browser gravou entretanto; 422 com erros por-KPI)."""
    if _is_mobile_request(request):
        raise HTTPException(403, "Edição de KPIs só em desktop")
    body = await request.json()
    try:
        version = int(body.get("version"))
    except (TypeError, ValueError):
        raise HTTPException(422, "versão em falta")
    try:
        state = kpi_params.save_kpis(body.get("kpis") or [], expected_version=version)
    except kpi_params.KpiVersionConflict as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        try:
            errors = json.loads(str(e))
        except json.JSONDecodeError:
            errors = {"_": str(e)}
        return JSONResponse({"ok": False, "errors": errors}, status_code=422)
    return JSONResponse({"ok": True, "version": state["version"]})


@app.post("/admin/kpis/revert")
async def admin_kpis_revert(request: Request) -> JSONResponse:
    """Reverte para os defaults de fábrica ou para uma entrada do histórico."""
    if _is_mobile_request(request):
        raise HTTPException(403, "Edição de KPIs só em desktop")
    try:
        body = await request.json()
    except Exception:
        body = {}
    to = body.get("to", "defaults")
    try:
        state = kpi_params.revert_kpis(to)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return JSONResponse({"ok": True, "version": state["version"]})


@app.post("/admin/unidades")
def admin_create_unidade(nome: str = Form(...)) -> Response:
    """Cria uma unidade fabril nova (tab Unidades)."""
    nome = (nome or "").strip()
    try:
        db.create_unidade(nome)
    except ValueError:
        return _admin_redirect("unidades", "err", "o nome da unidade não pode ser vazio")
    except sqlite3.IntegrityError:
        return _admin_redirect("unidades", "err", f"já existe uma unidade chamada '{nome}'")
    return _admin_redirect("unidades", "ok", f"unidade '{nome}' criada")


@app.post("/admin/unidades/{unidade_id}/toggle")
def admin_toggle_unidade(unidade_id: int) -> Response:
    """Ativa/desativa uma unidade. A sede (Trofa) não pode ser desativada —
    as folhas sem unidade pertencem-lhe."""
    if unidade_id == db.trofa_unidade_id():
        return _admin_redirect("unidades", "err", "a sede não pode ser desativada")
    alvo = next(
        (u for u in db.list_unidades(only_ativo=False) if u["id"] == unidade_id),
        None,
    )
    if alvo is None:
        return _admin_redirect("unidades", "err", "unidade não encontrada")
    novo = not bool(alvo["ativo"])
    db.set_unidade_ativo(unidade_id, novo)
    estado = "reativada" if novo else "desativada"
    return _admin_redirect("unidades", "ok", f"unidade '{alvo['nome']}' {estado}")


# ---------------------------------------------------------------------------
# Task C E4 — registo de kanbans novos (wizard da tab Kanbans)
# ---------------------------------------------------------------------------

_TEMPLATE_IMAGES_DIR = "template_images"  # sob _DATA_DIR (gitignored)
_TEMPLATE_IMG_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")


def _slug_template_name(nome: str) -> str:
    from app.web.template_store import _slug_field
    return _slug_field(nome)


@app.post("/admin/kanban-templates")
async def admin_create_kanban_template(
    request: Request,
    nome: str = Form(...),
    unidade_id: int = Form(...),
    file: UploadFile = File(...),
) -> JSONResponse:
    """Passo 'upload' do wizard: guarda a fotografia do template, cria o
    registo (status a_analisar) e mete a descoberta na fila FIFO do OCR
    (partilhada com a produção — sem prioridades)."""
    if _is_mobile_request(request):
        raise HTTPException(403, "Registo de kanbans só em desktop")
    unidades = {u["id"]: u for u in db.list_unidades()}
    if unidade_id not in unidades:
        raise HTTPException(422, "unidade inexistente ou inativa")
    slug = _slug_template_name(nome)
    if not slug or slug == "campo":
        raise HTTPException(422, "nome do kanban inválido")
    # Prefixo u{unidade_id}_ — proteção estrutural contra colisão com os
    # builtins (nenhum builtin começa por u\\d).
    canonical = f"u{unidade_id}_{slug}"
    from app.templates_registry import TEMPLATES
    if canonical in TEMPLATES:
        raise HTTPException(409, f"já existe um template '{canonical}'")
    if not file.filename or Path(file.filename).suffix.lower() not in _TEMPLATE_IMG_SUFFIXES:
        raise HTTPException(422, "a imagem tem de ser .jpg/.png/.webp")

    img_dir = _DATA_DIR / _TEMPLATE_IMAGES_DIR
    img_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(file.filename).suffix.lower()
    rel_path = f"{_TEMPLATE_IMAGES_DIR}/{canonical}{suffix}"
    target = _DATA_DIR / rel_path
    bytes_written = 0
    with target.open("wb") as f:
        while chunk := await file.read(1024 * 1024):
            bytes_written += len(chunk)
            if bytes_written > _MAX_UPLOAD_BYTES:
                f.close()
                target.unlink(missing_ok=True)
                raise HTTPException(413, "imagem demasiado grande")
            f.write(chunk)

    stub = {"name": canonical, "row_fields": [], "setor_aliases": []}
    try:
        tid = db.insert_kanban_template(
            canonical, unidade_id, json.dumps(stub, ensure_ascii=False),
            image_path=rel_path, status="a_analisar")
    except sqlite3.IntegrityError:
        target.unlink(missing_ok=True)
        raise HTTPException(409, f"já existe um template '{canonical}'")
    pos = ocr_queue.enqueue_discovery(tid)
    return JSONResponse({
        "ok": True, "id": tid, "name": canonical,
        "queue_position": pos, "queue_size": ocr_queue.queue_size(),
    })


@app.get("/admin/kanban-templates/{template_id}/status")
def admin_kanban_template_status(template_id: int) -> JSONResponse:
    """Polling do wizard (passo 'análise') + retoma de um registo."""
    tpl = db.get_kanban_template(template_id)
    if tpl is None:
        raise HTTPException(404, "template não encontrado")
    try:
        spec = json.loads(tpl.get("spec_json") or "{}")
    except json.JSONDecodeError:
        spec = {}
    try:
        discovery = json.loads(tpl.get("discovery_json") or "null")
    except json.JSONDecodeError:
        discovery = None
    return JSONResponse({
        "id": tpl["id"], "name": tpl["name"], "status": tpl["status"],
        "unidade_id": tpl["unidade_id"], "spec": spec,
        "discovery": discovery, "queue_size": ocr_queue.queue_size(),
    })


@app.post("/admin/kanban-templates/{template_id}/spec")
async def admin_kanban_template_spec(
    template_id: int, request: Request,
) -> JSONResponse:
    """Passo 'campos' do wizard — grava o spec corrigido pelo humano.
    422 com erros/conflitos bloqueantes; avisos seguem no 200."""
    if _is_mobile_request(request):
        raise HTTPException(403, "Registo de kanbans só em desktop")
    tpl = db.get_kanban_template(template_id)
    if tpl is None:
        raise HTTPException(404, "template não encontrado")
    body = await request.json()
    spec = dict(body.get("spec") or {})
    spec["name"] = tpl["name"]  # o nome canónico não muda por aqui
    errors, warnings = template_store.validate_spec_payload(spec)
    from app.templates_registry import alias_conflicts
    conflicts = alias_conflicts(
        spec.get("setor_aliases") or [], exclude_template=tpl["name"])
    # Alias EXATO contra qualquer template existente rouba folhas — bloqueia.
    blocking = [c for c in conflicts if c["kind"] == "exact"]
    if errors or blocking:
        return JSONResponse(
            {"ok": False, "errors": errors, "conflicts": conflicts},
            status_code=422)
    db.update_kanban_template_spec(
        template_id, json.dumps(spec, ensure_ascii=False))
    if tpl["status"] == "ativo":
        template_store.reload_registry()
    return JSONResponse({"ok": True, "warnings": warnings,
                         "conflicts": conflicts})


@app.post("/admin/kanban-templates/{template_id}/activate")
def admin_kanban_template_activate(
    template_id: int, request: Request,
) -> JSONResponse:
    """Ativação FINAL — só depois da validação humana (nunca automática).
    Revalida o spec e os conflitos de alias antes de instalar no registry."""
    if _is_mobile_request(request):
        raise HTTPException(403, "Registo de kanbans só em desktop")
    tpl = db.get_kanban_template(template_id)
    if tpl is None:
        raise HTTPException(404, "template não encontrado")
    if tpl["status"] not in ("analisado", "inativo"):
        raise HTTPException(409, f"não ativável a partir de '{tpl['status']}'")
    try:
        spec = json.loads(tpl.get("spec_json") or "{}")
    except json.JSONDecodeError:
        raise HTTPException(422, "spec inválido — volta ao passo dos campos")
    errors, _warnings = template_store.validate_spec_payload(spec)
    from app.templates_registry import alias_conflicts
    conflicts = alias_conflicts(
        spec.get("setor_aliases") or [], exclude_template=tpl["name"])
    blocking = [c for c in conflicts if c["kind"] == "exact"]
    if errors or blocking:
        return JSONResponse(
            {"ok": False, "errors": errors, "conflicts": conflicts},
            status_code=422)
    db.set_kanban_template_status(template_id, "ativo")
    template_store.reload_registry()
    return JSONResponse({"ok": True})


@app.post("/admin/kanban-templates/{template_id}/deactivate")
def admin_kanban_template_deactivate(
    template_id: int, request: Request,
) -> JSONResponse:
    """Kill-switch — tira o template do registry imediatamente."""
    if _is_mobile_request(request):
        raise HTTPException(403, "Registo de kanbans só em desktop")
    tpl = db.get_kanban_template(template_id)
    if tpl is None:
        raise HTTPException(404, "template não encontrado")
    db.set_kanban_template_status(template_id, "inativo")
    template_store.reload_registry()
    return JSONResponse({"ok": True})


@app.post("/admin/kanban-templates/{template_id}/delete")
def admin_kanban_template_delete(
    template_id: int, request: Request,
) -> JSONResponse:
    """Apaga um registo SEM folhas processadas (audit EN1090: com folhas
    só desativar)."""
    if _is_mobile_request(request):
        raise HTTPException(403, "Registo de kanbans só em desktop")
    tpl = db.get_kanban_template(template_id)
    if tpl is None:
        raise HTTPException(404, "template não encontrado")
    n = db.count_sheets_for_template(tpl["name"])
    if n > 0:
        raise HTTPException(
            409, f"{n} folha(s) processadas com este template — desativa em vez de apagar")
    if tpl.get("image_path"):
        (_DATA_DIR / tpl["image_path"]).unlink(missing_ok=True)
    db.delete_kanban_template(template_id)
    template_store.reload_registry()
    return JSONResponse({"ok": True})


@app.get("/admin/kanban-templates/{template_id}/image")
def admin_kanban_template_image(template_id: int) -> Response:
    tpl = db.get_kanban_template(template_id)
    if tpl is None or not tpl.get("image_path"):
        raise HTTPException(404, "imagem não encontrada")
    path = _DATA_DIR / tpl["image_path"]
    if not path.exists():
        raise HTTPException(404, "imagem não encontrada")
    return FileResponse(path)


@app.post("/sheet/{sheet_id}/delete")
def sheet_delete(sheet_id: int, request: Request) -> JSONResponse:
    """Round 34 — hard delete sheet + cascade. Desktop only.

    Cascade:
    - sheets row, edits, production_rows
    - image file in data/images/
    - cross-check JSON in C:\\kanban\\nifruka\\03_Cross_Check\\
    - factory CSV in C:\\kanban\\nifruka\\02_Dados_Extraidos\\csv\\
    """
    if _is_mobile_request(request):
        raise HTTPException(403, "Apagar só pode ser feito em desktop")
    try:
        result = db.delete_sheet(sheet_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return JSONResponse({"ok": True, "removed": result})


@app.post("/sheet/{sheet_id}/reprocess")
def sheet_reprocess(sheet_id: int) -> RedirectResponse:
    """Round 59/71 — Re-run OCR on a sheet that previously errored.

    Reuses the original uploaded image; no need to re-take photo. Triggered
    by the "↻ Re-processar OCR" button on the error banner in /sheet/{id}.

    R71: flips status back to 'pending' and enqueues to the background
    worker (instead of running OCR synchronously here). /sheet/{id} then
    shows the "A processar..." banner until the worker finishes.
    """
    sheet = db.get_sheet(sheet_id)
    if sheet is None:
        raise HTTPException(404, f"sheet {sheet_id} not found")
    img_path = _DATA_DIR / sheet["image_path"]
    if not img_path.exists():
        raise HTTPException(404, "image file missing")
    db.update_status(sheet_id, "pending")
    db.clear_error(sheet_id)
    ocr_queue.enqueue(sheet_id)
    return RedirectResponse(f"/sheet/{sheet_id}", status_code=303)


@app.post("/sheet/{sheet_id}/resolve-side")
def sheet_resolve_side(sheet_id: int, side: str = Form(...)) -> RedirectResponse:
    """rev00 — resolve uma folha marcada `needs_review`: o humano diz se é
    frente ('F') ou verso ('V'). Força a pista, limpa a flag e reprocessa —
    o worker relê `page_hint` (autoritativo) e encaminha para o template de
    produção ou para `paragens`. O cross-check não corre num reprocess
    (`was_reprocess`), portanto a escolha humana não é re-questionada."""
    s = (side or "").strip().upper()
    if s not in ("F", "V"):
        raise HTTPException(400, f"side must be F or V, got {side!r}")
    sheet = db.get_sheet(sheet_id)
    if sheet is None:
        raise HTTPException(404, f"sheet {sheet_id} not found")
    img_path = _DATA_DIR / sheet["image_path"]
    if not img_path.exists():
        raise HTTPException(404, "image file missing")
    db.set_page_hint(sheet_id, s)
    db.clear_needs_review(sheet_id)
    # Reusa a cauda do reprocess (R71): reenfileira para o worker.
    db.update_status(sheet_id, "pending")
    db.clear_error(sheet_id)
    ocr_queue.enqueue(sheet_id)
    return RedirectResponse(f"/sheet/{sheet_id}", status_code=303)


@app.post("/sheet/{sheet_id}/rotate")
def sheet_rotate(sheet_id: int, request: Request) -> JSONResponse:
    """Round 34c — rotate the served image 90° CW per click. Desktop only.

    Phones don't write EXIF orientation, so the auto-rotation guess can be
    wrong (sometimes upside-down). User clicks "rodar" to cycle through
    0/90/180/270 until kanban is readable. Stored per-sheet.
    """
    if _is_mobile_request(request):
        raise HTTPException(403, "Rodar só pode ser feito em desktop")
    sheet = db.get_sheet(sheet_id)
    if sheet is None:
        raise HTTPException(404, f"sheet {sheet_id} not found")
    current = int(sheet.get("image_rotation") or 0)
    new = db.set_image_rotation(sheet_id, current + 90)
    return JSONResponse({"ok": True, "rotation": new})


# R112 — Wizard "Corrigir via OF" -----------------------------------------

def _refs_lookup(
    query_raw: str,
    *,
    include_done: bool = False,
    phase: str | None = None,
) -> dict:
    """R112/R128 — núcleo do lookup OF/OV/modelo contra o plano, sem
    depender de folha. Task C E4: extraído de sheet_of_lookup (o endpoint
    delega aqui, comportamento intacto) para o wizard de registo de
    kanbans reutilizar na etapa de validação (/admin/refs-lookup)."""
    from app.pipeline.scoring_engine import normalize_of
    from app.pipeline.of_consumption import sort_entries_by_remaining, _plan_cutoff_iso

    refs = get_watcher().get_refs() or {}
    of_to_entries = refs.get("of_to_entries") or {}
    plan_by_ov = refs.get("plan_by_ov") or {}
    plan_by_modelo_ft = refs.get("plan_by_modelo_ft") or {}

    LIMIT = 50
    mode = "none"
    matched_of = ""
    pooled: list[dict] = []
    n_total_pre_filter = 0
    truncated = False

    q_upper = query_raw.upper()
    is_numeric = query_raw.isdigit()

    # Tier 1 — OF (numérico)
    if is_numeric:
        of_norm = normalize_of(query_raw)
        entries = of_to_entries.get(of_norm) or []
        if entries:
            mode = "of"
            matched_of = of_norm
            n_total_pre_filter = len(entries)
            pooled = [
                {**e, "_of": of_norm, "_orig_idx": i}
                for i, e in enumerate(entries)
            ]

    # Tier 2 — OV (numérico, exact match)
    if mode == "none" and is_numeric:
        ov_entries = plan_by_ov.get(query_raw) or []
        if ov_entries:
            mode = "ov"
            n_total_pre_filter = len(ov_entries)
            # plan_by_ov entries já têm "_of" anotado (ver
            # ref_watcher._derive_plan_indexes). _orig_idx é por OF para
            # apply-of-entry; rebuild aqui contra of_to_entries.
            for e in ov_entries:
                of_of_entry = str(e.get("_of") or "")
                source = of_to_entries.get(of_of_entry) or []
                orig_idx = next(
                    (i for i, se in enumerate(source) if se is e
                     or (se.get("ov") == e.get("ov")
                         and se.get("designacao") == e.get("designacao"))),
                    -1,
                )
                pooled.append({**e, "_orig_idx": orig_idx})

    # Tier 3 — modelo: prefix no first-token (fast path) + substring na
    # designação completa. R128 fez o campo modelo no cross-check guardar
    # a designação completa; o operador escreve naturalmente parte longa
    # ("Tronco-Cónica" ou "CGC2E10D - Coluna") e antes não batia nada.
    if mode == "none":
        seen: set[tuple] = set()

        def _add_to_pool(e: dict) -> None:
            of_of_entry = str(e.get("_of") or "")
            key = (of_of_entry, str(e.get("ov") or ""),
                   str(e.get("designacao") or ""))
            if key in seen:
                return
            seen.add(key)
            source = of_to_entries.get(of_of_entry) or []
            orig_idx = next(
                (i for i, se in enumerate(source) if se is e
                 or (se.get("ov") == e.get("ov")
                     and se.get("designacao") == e.get("designacao"))),
                -1,
            )
            pooled.append({**e, "_orig_idx": orig_idx})

        # Pass A — prefix no first-token (caso típico, queries curtas)
        for k in plan_by_modelo_ft.keys():
            if not k.startswith(q_upper):
                continue
            for e in plan_by_modelo_ft.get(k) or []:
                _add_to_pool(e)
        # Pass B — substring na designação completa (queries longas)
        if len(q_upper) >= 3:
            for of_key, entries in of_to_entries.items():
                for e in entries:
                    des = (e.get("designacao") or "").upper()
                    if q_upper not in des:
                        continue
                    _add_to_pool({**e, "_of": of_key})
        if pooled:
            mode = "modelo"
            n_total_pre_filter = len(pooled)

    if mode == "none":
        return {
            "found": False, "mode": "none", "q": query_raw, "of": "",
            "entries": [], "n_entries": 0, "n_total": 0,
        }

    sorted_entries = sort_entries_by_remaining(
        pooled, include_done=include_done, phase=phase,
    )

    if len(sorted_entries) > LIMIT:
        sorted_entries = sorted_entries[:LIMIT]
        truncated = True

    out_entries = []
    for i, e in enumerate(sorted_entries):
        out_entries.append({
            "idx": i,
            "orig_idx": e.get("_orig_idx"),  # R116 — usar este no apply
            "of": str(e.get("_of") or ""),    # R128 — OF por entry (modo modelo/OV)
            "cliente": e.get("cliente", ""),
            "ov": str(e.get("ov", "")),
            "modelo": e.get("designacao", ""),
            "comp_mm": e.get("comp"),
            "lbase": e.get("lbase"),
            "ltopo": e.get("ltopo"),
            "esp": e.get("esp"),
            "material": e.get("material", ""),
            "fechado": bool(e.get("fechado")),
            "remaining": e.get("_remaining"),
            "quanttrp": e.get("_quanttrp"),
            "done": e.get("_done", False),
            # Decomposição do remaining (fix do double counting): o
            # operador vê de onde vêm os números e ganha confiança.
            "produced_erp": e.get("_produced_erp"),
            "kanban_qty": e.get("_kanban_qty"),
        })

    return {
        "found": True,
        "mode": mode,
        "q": query_raw,
        "of": matched_of,    # back-compat: vazio nos modos ov/modelo
        "entries": out_entries,
        "n_entries": len(out_entries),
        "n_total": n_total_pre_filter,
        "truncated": truncated,
        "plan_date": _plan_cutoff_iso(),  # corte dos kanbans pós-plano
    }


@app.get("/sheet/{sheet_id}/of-lookup")
def sheet_of_lookup(
    sheet_id: int,
    of: str = "",
    q: str = "",
    include_done: int = 0,
) -> JSONResponse:
    """R112 — devolve entries do plan_colunas para um OF/OV/modelo.

    R113 — entries são ordenadas por "faltam menos primeiro" e entries
    já fechadas (remaining ≤ 0) são filtradas por defeito.
    `include_done=1` para mostrar todas (recuperação de folhas antigas).

    R128 — auto-detect multi-modo. O parâmetro `q` aceita OF (6 dígitos),
    OV (≥6 dígitos) ou prefixo de modelo (alfanumérico). Ordem de
    tentativa: OF → OV → modelo prefix. `of=` mantido para back-compat
    com clients antigos.
    """
    sheet = db.get_sheet(sheet_id)
    if sheet is None:
        raise HTTPException(404, f"sheet {sheet_id} not found")
    query_raw = (q or of or "").strip()
    if not query_raw:
        return JSONResponse({
            "found": False, "mode": "none", "q": "", "of": "", "entries": [],
        })

    from app.pipeline.scoring_engine import _current_phase

    refs = get_watcher().get_refs() or {}
    # R138 — etapa do kanban (setor→colunaexcel) para o "done"/remaining ser
    # consciente do setor: uma linha só está fechada quando ESTA fase atingiu
    # quanttrp. Sem este phase, o remaining usava max(fases) e a fase inicial
    # sobre-produzida marcava ~92% das linhas como fechadas.
    phase = _current_phase(sheet.get("sheet_data") or {}, refs)
    return JSONResponse(_refs_lookup(
        query_raw, include_done=bool(include_done), phase=phase,
    ))


@app.post("/sheet/{sheet_id}/apply-of-entry")
async def sheet_apply_of_entry(sheet_id: int, request: Request) -> JSONResponse:
    """R112 — aplica os campos de uma entry do plan a uma linha do kanban.

    Body: {row_index, of, entry_idx}. Escreve cliente, modelo, OV,
    comp_mm, lbase, ltopo, esp via apply_edit(source='system'). Não
    toca em qtd, lote, pri, coni, larg_mm (não estão no plan ou são
    por bobine). Re-corre cross-check para refrescar cores.
    """
    sheet = db.get_sheet(sheet_id)
    if sheet is None:
        raise HTTPException(404, f"sheet {sheet_id} not found")
    if sheet.get("status") == "validated":
        raise HTTPException(409, "Folha já validada — edits bloqueados")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Body JSON inválido")
    try:
        row_index = int(body.get("row_index", -1))
        entry_idx = int(body.get("entry_idx", -1))
    except (ValueError, TypeError):
        raise HTTPException(400, "row_index e entry_idx têm de ser inteiros")
    of_raw = str(body.get("of", "")).strip()
    if row_index < 0 or entry_idx < 0 or not of_raw:
        raise HTTPException(400, "row_index, of, entry_idx obrigatórios")

    from app.pipeline.scoring_engine import normalize_of
    of_norm = normalize_of(of_raw)
    refs = get_watcher().get_refs() or {}
    entries = (refs.get("of_to_entries") or {}).get(of_norm) or []
    if not entries or entry_idx >= len(entries):
        raise HTTPException(404, "OF ou entry não encontrados no plan")

    e = entries[entry_idx]
    fields_to_set = {
        "of": of_norm,
        "cliente": e.get("cliente", ""),
        "ov": str(e.get("ov", "")),
        "modelo": e.get("designacao", ""),
        "comp_mm": e.get("comp"),
        "lbase": e.get("lbase"),
        "ltopo": e.get("ltopo"),
        "esp": e.get("esp"),
    }
    edits_to_apply = []
    skipped = []
    for field, value in fields_to_set.items():
        if value is None or value == "":
            skipped.append(field)
            continue
        path = f"rows[{row_index}].{field}"
        edits_to_apply.append((field, path, str(value)))
    applied = []
    if edits_to_apply:
        try:
            batch = [(path, value) for _field, path, value in edits_to_apply]
            db.apply_edits_batch(sheet_id, batch, source="system")
            applied = [
                {"field": field, "value": value}
                for field, _path, value in edits_to_apply
            ]
        except ValueError:
            skipped.extend(field for field, _path, _value in edits_to_apply)
        except Exception:
            skipped.extend(field for field, _path, _value in edits_to_apply)
    if applied:
        _start_sheet_cross_check({sheet_id}, profile_trigger="apply_of_entry")
    # R113 — após aplicar, refresca a cache de consumption (a próxima
    # chamada a /of-lookup vai recomputar baseado neste novo estado).
    try:
        from app.pipeline.of_consumption import invalidate_cache
        invalidate_cache()
    except Exception:
        pass
    return JSONResponse({
        "ok": True,
        "n_applied": len(applied),
        "applied": applied,
        "skipped": skipped,
        "of_used": of_norm,
    })


@app.post("/sheet/{sheet_id}/add-row")
async def sheet_add_row(sheet_id: int, request: Request) -> JSONResponse:
    """R136 — adiciona uma linha em branco à tabela de produção (no fim).

    O operador pode então preenchê-la à mão ou via o wizard "Corrigir via OF".
    Só antes da validação. Re-corre o cross-check para a nova linha (apenas
    células vazias ficam NA/cinza) e devolve o índice da nova linha.
    """
    if _is_mobile_request(request):
        raise HTTPException(403, "Edição só pode ser feita em desktop")
    sheet = db.get_sheet(sheet_id)
    if sheet is None:
        raise HTTPException(404, f"sheet {sheet_id} not found")
    if sheet.get("status") == "validated":
        raise HTTPException(409, "Folha já validada — edits bloqueados")
    try:
        new_idx = db.add_row(sheet_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _start_sheet_cross_check({sheet_id}, profile_trigger="add_row")
    return JSONResponse({"ok": True, "row_index": new_idx})


@app.post("/sheet/{sheet_id}/remove-row")
async def sheet_remove_row(sheet_id: int, request: Request) -> JSONResponse:
    """R136 — remove uma linha (errada/inventada pelo OCR) da tabela de
    produção. Body JSON: {row_index}. Só antes da validação.

    O conteúdo removido fica no audit trail (edits). Re-corre o cross-check
    (re-indexado) e invalida a cache de consumption — remover uma linha muda
    as quantidades produzidas.
    """
    if _is_mobile_request(request):
        raise HTTPException(403, "Edição só pode ser feita em desktop")
    sheet = db.get_sheet(sheet_id)
    if sheet is None:
        raise HTTPException(404, f"sheet {sheet_id} not found")
    if sheet.get("status") == "validated":
        raise HTTPException(409, "Folha já validada — edits bloqueados")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Body JSON inválido")
    try:
        row_index = int(body.get("row_index", -1))
    except (ValueError, TypeError):
        raise HTTPException(400, "row_index tem de ser inteiro")
    try:
        db.delete_row(sheet_id, row_index)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _start_sheet_cross_check({sheet_id}, profile_trigger="remove_row")
    # R113 — remover uma linha muda a qtd produzida → invalida a cache.
    try:
        from app.pipeline.of_consumption import invalidate_cache
        invalidate_cache()
    except Exception:
        pass
    return JSONResponse({"ok": True})


@app.post("/sheet/{sheet_id}/recrop")
def sheet_recrop(sheet_id: int) -> JSONResponse:
    """R111 — tenta correr auto-crop outra vez nesta folha.

    Útil quando a primeira tentativa falhou (paper não detectado) e o
    supervisor quer pedir nova tentativa via UI. Idempotente: se já
    existe cropped, sobrepõe; se a detecção falhar agora, devolve
    {ok: False, has_cropped: False} para a UI mostrar mensagem.
    """
    sheet = db.get_sheet(sheet_id)
    if sheet is None:
        raise HTTPException(404, f"sheet {sheet_id} not found")
    img_path = _DATA_DIR / sheet["image_path"]
    if not img_path.exists():
        return JSONResponse(
            {"ok": False, "has_cropped": False,
             "error": "Imagem original não encontrada."},
            status_code=404,
        )
    from .image_crop import auto_crop, has_cropped
    result = auto_crop(img_path)
    success = result is not None
    return JSONResponse({
        "ok": success,
        "has_cropped": has_cropped(img_path),
        "message": ("Kanban recortado com sucesso." if success
                    else "Não consegui detectar o kanban na foto. "
                         "Talvez a folha esteja com pouco contraste, "
                         "muito inclinada ou cortada."),
    })


@app.get("/sheet/{sheet_id}/csv")
def sheet_csv(sheet_id: int) -> Response:
    sheet = db.get_sheet(sheet_id)
    if sheet is None:
        raise HTTPException(404, f"sheet {sheet_id} not found")
    data = sheet.get("sheet_data") or {}
    csv_text = _to_3block_csv(Path(sheet["image_path"]).name, data)
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="sheet_{sheet_id}.csv"'
        },
    )


@app.get("/image/{sheet_id}/original")
def sheet_image_original(sheet_id: int) -> FileResponse:
    """Serve the raw uploaded photo (no auto-crop applied).

    Useful for review when the auto-crop misfires or the user wants to
    see the full photo with background.
    """
    sheet = db.get_sheet(sheet_id)
    if sheet is None:
        raise HTTPException(404, f"sheet {sheet_id} not found")
    img_path = _DATA_DIR / sheet["image_path"]
    if not img_path.exists():
        raise HTTPException(404, "image file missing")
    return _serve_image_with_rotation(sheet, img_path)


@app.get("/image/{sheet_id}")
def sheet_image(sheet_id: int) -> FileResponse:
    """Serve the kanban photo. Round 46: prefer auto-cropped version (paper
    only, perspective-corrected) when available; falls back to raw photo.

    Phones take portrait photos of landscape kanbans. Rotating server-side
    (with a disk cache next to the original) avoids CSS transform hacks
    that overflow the layout. EXIF orientation also applied via PIL.
    """
    sheet = db.get_sheet(sheet_id)
    if sheet is None:
        raise HTTPException(404, f"sheet {sheet_id} not found")
    img_path = _DATA_DIR / sheet["image_path"]
    if not img_path.exists():
        raise HTTPException(404, "image file missing")
    # Round 46: prefer auto-cropped version (paper only, no background).
    # Cropped image is ALREADY rotated/oriented correctly by the warp,
    # so we skip _serve_image_with_rotation for it (no extra rotation pass).
    from .image_crop import cropped_path_for
    cropped = cropped_path_for(img_path)
    if cropped.exists():
        # Honour user rotation override on top of the cropped image
        rot_override = int(sheet.get("image_rotation") or 0) % 360
        if rot_override == 0:
            return FileResponse(cropped, media_type="image/jpeg")
        # Apply rotation override to cropped (cache result)
        rot_cache = cropped.with_name(f"{cropped.stem}_r{rot_override}.jpg")
        src_mtime = cropped.stat().st_mtime
        if not rot_cache.exists() or rot_cache.stat().st_mtime < src_mtime:
            from PIL import Image
            with Image.open(cropped) as im:
                if im.mode != "RGB":
                    im = im.convert("RGB")
                im.rotate(-rot_override, expand=True).save(
                    rot_cache, "JPEG", quality=90, optimize=True)
            os.utime(rot_cache, (src_mtime, src_mtime))
        return FileResponse(rot_cache, media_type="image/jpeg")
    # Fallback: raw photo with auto-rotation pass (legacy path).
    return _serve_image_with_rotation(sheet, img_path)


def _serve_image_with_rotation(sheet: dict, img_path: Path) -> FileResponse:
    """Serve raw photo with EXIF transpose + portrait→landscape rotation +
    user rotation override. Used when no auto-cropped version exists."""
    # Cache key includes rotation override (per-sheet, stored in DB).
    # Phones don't write EXIF orientation, so we guess: portrait → rotate
    # 90° CCW. If wrong (upside-down), user clicks "rodar" → override
    # cycles through 90/180/270 and re-renders.
    rot_override = int(sheet.get("image_rotation") or 0) % 360
    suffix = f"_r{rot_override}" if rot_override else "_landscape"
    cache_path = img_path.with_name(img_path.stem + suffix + ".jpg")
    src_mtime = img_path.stat().st_mtime
    if not cache_path.exists() or cache_path.stat().st_mtime < src_mtime:
        from PIL import Image, ImageOps
        with Image.open(img_path) as im:
            im = ImageOps.exif_transpose(im)
            # Auto step: portrait → CCW 90° (most phones in this fleet
            # produce kanban with top-edge towards the LEFT of the sensor)
            if im.height > im.width:
                im = im.rotate(90, expand=True)
            # User override (clockwise quarter-turns)
            if rot_override:
                im = im.rotate(-rot_override, expand=True)
            if im.mode != "RGB":
                im = im.convert("RGB")
            im.save(cache_path, "JPEG", quality=85, optimize=True)
        os.utime(cache_path, (src_mtime, src_mtime))
    return FileResponse(cache_path, media_type="image/jpeg")


def _parse_unidade_param(unidade: str | None) -> int | None:
    """Task C E4 — query param 'unidade' (id) tolerante a lixo."""
    try:
        return int(unidade) if unidade not in (None, "", "all") else None
    except (TypeError, ValueError):
        return None


@app.get("/kanbans", response_class=HTMLResponse)
def kanban_viewer(
    request: Request,
    operador: str | None = None,
    data: str | None = None,
    setor: str | None = None,
    of: str | None = None,
    status: str | None = None,
    sheet_id: int | None = None,
    unidade: str | None = None,
) -> Response:
    """Desktop kanban viewer with multi-filter (operador + data + setor + of + status).

    Round 34/36: filters combinable via URL params. ``data`` is YYYY-MM-DD.
    ``of`` matches sheets that have at least one row with that OF.
    ``status`` accepts 'extracted' (não validadas) or 'validated'; empty = both.
    Task C E4 — ``unidade`` (id) filtra pela unidade fabril da folha.
    Empty filters = all matching sheets.
    """
    operadores = db.list_distinct_operadores()
    setores = db.list_distinct_setores()
    current_of = (of or "").strip() or None
    current_status = status if status in ("extracted", "validated") else None
    statuses = (current_status,) if current_status else ("extracted", "validated")
    current_unidade = _parse_unidade_param(unidade)
    unidades = db.list_unidades(only_ativo=False)

    if not operadores:
        return templates.TemplateResponse(
            request, "kanban_viewer.html",
            {
                "operadores": [],
                "setores": [],
                "current_operador": None,
                "current_data": data,
                "current_setor": setor,
                "current_of": current_of,
                "current_status": current_status,
                "current_unidade": current_unidade,
                "unidades": unidades,
                "sheets": [],
                "sheet": None,
                "neighbors": {"position": 0, "total": 0, "prev_id": None, "next_id": None},
                "header": {},
                "rows": [],
                "footer": {},
                "cells_by_path": {},
                "cc_status_by_path": {},
                "cc_ref_by_path": {},
                "cc_ref_title_by_path": {},
                "cc_obra_concluida_by_path": {},
                "valid_operadores": _get_operadores(),
                **_template_ctx_for_sheet(None),  # bobine_formato defaults
            },
        )

    # Resolve operador URL param via case+accent-insensitive match
    current_operador = None
    if operador:
        target_norm = db._normalize_operador(operador)
        for op in operadores:
            if db._normalize_operador(op) == target_norm:
                current_operador = op
                break

    # Apply multi-filter
    sheets = db.list_sheets_filtered(
        operador=current_operador,
        data_iso=data if data and _ISO_DATE_RE.match(data) else None,
        setor=setor,
        of=current_of,
        statuses=statuses,
        unidade=current_unidade,
    )
    if not sheets:
        sheet = None
        neighbors = {"position": 0, "total": 0, "prev_id": None, "next_id": None}
    else:
        # Pick sheet by ID if provided, else first in filtered list
        target_id = sheet_id if sheet_id and any(s["id"] == sheet_id for s in sheets) else sheets[0]["id"]
        sheet = db.get_sheet(target_id)
        # Round 34: neighbors computed within the filtered set so prev/next
        # respects the user's multi-filter (operador + data + setor).
        ids = [s["id"] for s in sheets]
        try:
            idx = ids.index(target_id)
            neighbors = {
                "position": idx + 1,
                "total": len(ids),
                "prev_id": ids[idx - 1] if idx > 0 else None,
                "next_id": ids[idx + 1] if idx + 1 < len(ids) else None,
            }
        except ValueError:
            neighbors = {"position": 0, "total": len(ids), "prev_id": None, "next_id": None}

    cells_by_path = {}
    if sheet and sheet.get("dq_audit"):
        cells_by_path = sheet["dq_audit"].get("cells", {})

    header = (sheet.get("sheet_data") or {}).get("header", {}) if sheet else {}
    rows = (sheet.get("sheet_data") or {}).get("rows", []) if sheet else []
    footer = (sheet.get("sheet_data") or {}).get("footer", {}) if sheet else {}

    # Round 33: cross-check colors per cell
    (cc_status_by_path, cc_ref_by_path, cc_ref_title_by_path,
     cc_suspended_by_path, cc_snapped_by_path,
     cc_obra_concluida_by_path) = ({}, {}, {}, {}, {}, {})
    if sheet:
        (cc_status_by_path, cc_ref_by_path, cc_ref_title_by_path,
         cc_suspended_by_path, cc_snapped_by_path,
         cc_obra_concluida_by_path) = _build_cc_maps(sheet["id"])

    # R94 — ISO date pre-fill for validation form (same as sheet_page)
    data_iso_for_validate = db._normalize_data_pt_to_iso(header.get("data")) if header else None

    # R111 — has_cropped flag (mesma lógica do sheet_page)
    from .image_crop import has_cropped as _has_cropped
    sheet_has_cropped = False
    if sheet and sheet.get("image_path"):
        sheet_has_cropped = _has_cropped(_DATA_DIR / sheet["image_path"])

    return templates.TemplateResponse(
        request, "kanban_viewer.html",
        {
            "operadores": operadores,
            "setores": setores,
            "current_operador": current_operador,
            "current_data": data,
            "current_setor": setor,
            "current_of": current_of,
            "current_status": current_status,
            "current_unidade": current_unidade,
            "unidades": unidades,
            "sheets": sheets,
            "sheet": sheet,
            "neighbors": neighbors,
            "header": header,
            "rows": rows,
            "footer": footer,
            "cells_by_path": cells_by_path,
            "cc_status_by_path": cc_status_by_path,
            "cc_ref_by_path": cc_ref_by_path,
            "cc_ref_title_by_path": cc_ref_title_by_path,
            "cc_suspended_by_path": cc_suspended_by_path,
            "cc_snapped_by_path": cc_snapped_by_path,
            "cc_obra_concluida_by_path": cc_obra_concluida_by_path,
            "valid_operadores": _get_operadores(),
            "data_iso_for_validate": data_iso_for_validate,
            "has_cropped": sheet_has_cropped,
            **_template_ctx_for_sheet(sheet),  # per-current-sheet template
        },
    )


@app.get("/queue", response_class=HTMLResponse)
def queue_page(
    request: Request,
    status: str | None = None,
    of: str | None = None,
    operador: str | None = None,
    data: str | None = None,
    captured: str | None = None,
    setor: str | None = None,
    unidade: str | None = None,
) -> Response:
    """Round 36 — OF filter; R81 — operador/data/setor filters (combinable).
    R128 — captured (data de captura, distinta de header.data).
    Task C E4 — unidade (id da unidade fabril)."""
    of_filter = (of or "").strip() or None
    operador_filter = (operador or "").strip() or None
    data_filter = (data or "").strip() or None
    captured_filter = (captured or "").strip() or None
    setor_filter = (setor or "").strip() or None
    unidade_filter = _parse_unidade_param(unidade)

    use_filtered = any([of_filter, operador_filter, data_filter,
                        captured_filter, setor_filter,
                        unidade_filter is not None])
    if use_filtered:
        statuses_all = ("pending", "extracted", "validated", "error")
        statuses = (status,) if status and status != "all" else statuses_all
        sheets = db.list_sheets_filtered(
            operador=operador_filter,
            data_iso=data_filter,
            captured_iso=captured_filter,
            setor=setor_filter,
            of=of_filter,
            statuses=statuses,
            unidade=unidade_filter,
        )
        # list_sheets_filtered returns oldest first; flip to newest first
        sheets = sorted(sheets, key=lambda s: s.get("captured_at") or "", reverse=True)
    else:
        sheets = db.list_sheets(status=status)

    unidades = db.list_unidades(only_ativo=False)
    unidade_nome_by_id = {u["id"]: u["nome"] for u in unidades}
    return templates.TemplateResponse(
        request,
        "queue.html",
        {
            "sheets": sheets,
            "status_filter": status or "all",
            "of_filter": of_filter,
            "operador_filter": operador_filter,
            "data_filter": data_filter,
            "captured_filter": captured_filter,
            "setor_filter": setor_filter,
            "unidade_filter": unidade_filter,
            "unidades": unidades,
            "unidade_nome_by_id": unidade_nome_by_id,
            "operadores": db.list_distinct_operadores(),
            "setores": db.list_distinct_setores(),
        },
    )


_ISO_DATE_RE = __import__("re").compile(r"^\d{4}-\d{2}-\d{2}$")


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_today(request: Request, date: str | None = None) -> Response:
    """Default dashboard view: Today (or any date via ?date=YYYY-MM-DD).

    Bad date strings are silently dropped (treated as no-filter) rather
    than passed to SQL — prevents user-visible errors from typos.
    """
    if date and not _ISO_DATE_RE.match(date):
        date = None
    summary = kpis.today_summary(reference_date=date)
    # Timeline window centers on the filter date (or today if no filter)
    daily = kpis.daily_trend(days=7, end_date=date)
    daily_op = kpis.daily_per_operador(days=7, end_date=date)
    return templates.TemplateResponse(
        request,
        "dashboard_today.html",
        {
            "summary": summary,
            "daily": daily,
            "daily_op": daily_op,
            "active_tab": "today",
            "date_filter": date,
        },
    )


@app.get("/export")
def export_excel(
    date_from: str | None = None,
    date_to: str | None = None,
    operador: str | None = None,
    sector: str | None = None,
    validated_only: bool = False,
) -> Response:
    """Round 29 Phase D — Excel multi-sheet bulk export.

    Query params (R69):
    - ``date_from``, ``date_to``: ISO YYYY-MM-DD inclusive range. Both
      omitted = "sempre" (no date filter).
    - ``operador``: optional filter (case-insensitive)
    - ``sector``: optional filter against one of ``PRODUCTION_SECTORS``
    - ``validated_only`` (R130): se True, só inclui folhas com
      ``sheets.status='validated'``. Default False = inclui rascunhos.

    Returns .xlsx with Resumo sheet + 1 sheet per day with sub-tables per
    operador. See export.py for structure.
    """
    df = (date_from or "").strip() or None
    dt_ = (date_to or "").strip() or None
    sec = (sector or "").strip() or None
    # Date validation: both or neither (XOR rejected for UX clarity)
    if bool(df) != bool(dt_):
        raise HTTPException(400, "provide both date_from and date_to, or neither (= sempre)")
    if df and dt_:
        if not _ISO_DATE_RE.match(df) or not _ISO_DATE_RE.match(dt_):
            raise HTTPException(400, "date_from and date_to must be YYYY-MM-DD")
        if dt_ < df:
            raise HTTPException(400, "date_to must be >= date_from")
    if sec and sec not in PRODUCTION_SECTORS:
        raise HTTPException(400, f"sector must be one of {list(PRODUCTION_SECTORS)}")
    xlsx_bytes = export.export_excel(df, dt_, operador, sec, validated_only)
    filename = export.filename_for(df, dt_, operador, sec, validated_only)
    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/excel", response_class=HTMLResponse)
def excel_page(
    request: Request,
    operador: str | None = None,
    of_filter: str | None = Query(None, alias="of"),
    limit: int = Query(500, ge=1, le=5000),
) -> Response:
    """Continuous Excel-style view of every registered production row.

    Mirrors the current CPIS schema (the same shape as the
    `MigracaoNikufraCPIS.xlsx` export). Read-only; clicking "Exportar"
    in the top bar produces the downloadable .xlsx with a period
    selector (1 dia / 1 semana / 1 mês / 3 / 6 meses / 1 ano).
    """
    from app.web.db import conn

    where = ["pr.sheet_iso_date IS NOT NULL"]
    params: list = []
    if operador:
        where.append(
            "(UPPER(pr.operador) = UPPER(?) OR UPPER(s.operador) = UPPER(?))"
        )
        params.extend([operador, operador])
    if of_filter:
        where.append("pr.of = ?")
        params.append(of_filter.strip())

    sql = f"""
        SELECT pr.*,
               s.operador AS validated_operador,
               json_extract(s.sheet_data, '$.header.setor_maquina') AS setor_maquina,
               json_extract(s.sheet_data, '$.header.n_operador') AS n_operador,
               json_extract(s.sheet_data, '$.header.cod_maquina') AS header_cod_maquina,
               json_extract(s.sheet_data, '$.header.pernr') AS header_pernr
          FROM production_rows pr
          JOIN sheets s ON s.id = pr.sheet_id
         WHERE {' AND '.join(where)}
         ORDER BY pr.sheet_iso_date DESC, pr.sheet_id DESC, pr.row_index ASC
         LIMIT ?
    """
    params.append(limit)
    with conn() as c:
        raw_rows = [dict(r) for r in c.execute(sql, params).fetchall()]

    try:
        refs = get_watcher().get_refs()
    except Exception:
        refs = None
    cpis_rows = [export._build_cpis_row(r, refs=refs) for r in raw_rows]

    # Distinct operators for the filter dropdown (reuse db helper)
    operadores = db.list_distinct_operadores()

    # Total count (separate cheap query, no limit) for the row counter
    with conn() as c:
        total_rows = c.execute(
            "SELECT COUNT(*) FROM production_rows WHERE sheet_iso_date IS NOT NULL"
        ).fetchone()[0]

    return templates.TemplateResponse(
        request,
        "excel.html",
        {
            "rows": cpis_rows,
            "columns": export.CPIS_COLUMNS,
            "operadores": operadores,
            "operador_filter": operador or "",
            "of_filter": of_filter or "",
            "limit": limit,
            "shown": len(cpis_rows),
            "total_rows": total_rows,
            "active_tab": "excel",
        },
    )


@app.get("/export/cpis")
def export_cpis(
    date_from: str | None = None,
    date_to: str | None = None,
    operador: str | None = None,
    sector: str | None = None,
    validated_only: bool = False,
) -> Response:
    """CPIS migration export — single-sheet .xlsx matching
    `MigracaoNikufraCPIS.xlsx` (`Folha1`).

    One row per kanban production row in the period. Weight metrics are
    resolved via app.production.weights using plan/SAP before OCR.
    `Cód. Máquina` derived from setor_maquina (BOBINE-FORMATO → M032, etc.).
    Query params identical to `/export` (R69: same date / sector semantics).
    R130: ``validated_only`` exclui rascunhos quando True.
    """
    df = (date_from or "").strip() or None
    dt_ = (date_to or "").strip() or None
    sec = (sector or "").strip() or None
    if bool(df) != bool(dt_):
        raise HTTPException(400, "provide both date_from and date_to, or neither (= sempre)")
    if df and dt_:
        if not _ISO_DATE_RE.match(df) or not _ISO_DATE_RE.match(dt_):
            raise HTTPException(400, "date_from and date_to must be YYYY-MM-DD")
        if dt_ < df:
            raise HTTPException(400, "date_to must be >= date_from")
    if sec and sec not in PRODUCTION_SECTORS:
        raise HTTPException(400, f"sector must be one of {list(PRODUCTION_SECTORS)}")
    xlsx_bytes = export.build_cpis_workbook(df, dt_, operador, sec, validated_only)
    filename = export.cpis_filename_for(df, dt_, operador, sec, validated_only)
    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/dashboard/production", response_class=HTMLResponse)
def dashboard_production(
    request: Request,
    date: str | None = None,
    period: str = "day",
) -> Response:
    """Round 29 Phase C — Production sector flow.

    Round 34: + period selector (day/week/month/year).
    When no date specified AND no data for today, fall back to the
    most-recent-non-future date with data.
    """
    if date and not _ISO_DATE_RE.match(date):
        date = None
    if period not in ("day", "week", "month", "year"):
        period = "day"
    if not date:
        # Try today first; if empty, fall back to most recent NON-FUTURE date.
        from app.web.db import conn
        with conn() as c:
            today_count = c.execute(
                "SELECT COUNT(*) FROM production_rows WHERE sheet_iso_date = DATE('now', 'localtime')"
            ).fetchone()[0]
            if today_count == 0:
                row = c.execute(
                    "SELECT sheet_iso_date FROM production_rows "
                    "WHERE sheet_iso_date IS NOT NULL "
                    "  AND sheet_iso_date <= DATE('now', 'localtime') "
                    "ORDER BY sheet_iso_date DESC LIMIT 1"
                ).fetchone()
                if row and row[0]:
                    date = row[0]
    overview = kpis.production_overview(date=date, period=period)
    return templates.TemplateResponse(
        request,
        "dashboard_production.html",
        {
            "overview": overview,
            "date_filter": date,
            "period": period,
            "active_tab": "production",
        },
    )


@app.get("/dashboard/nesting", response_class=HTMLResponse)
def dashboard_nesting(request: Request) -> Response:
    """Round 54 Fase 7 — Gemini (TPL102) nesting dashboard.

    Aggregates m²/qty across all 3 Gemini machines (GASPARINI / HPE32 /
    HD36). Domain is chapa nesting, distinct from column production —
    deserves its own KPI page.
    """
    from app.gemini import list_gemini_sheets, aggregate_gemini
    db_path = _DATA_DIR / "app.db"
    sheets = list_gemini_sheets(db_path)
    agg = aggregate_gemini(sheets)
    return templates.TemplateResponse(
        request,
        "dashboard_nesting.html",
        {
            "sheets": sheets,
            "agg": agg,
            "active_tab": "nesting",
        },
    )


@app.get("/dashboard/downtime", response_class=HTMLResponse)
def dashboard_downtime(request: Request) -> Response:
    """Round 54 Fase 6 — Paragens (downtime) dashboard.

    rev00 — puxa todas as folhas de paragens (qualquer template com
    `has_production_rows=False`: `paragens` genérico, `maq_fustes_paragens`, …
    via `downtime.list_downtime_sheets`) + legacy por setor QUINADORA PAV.4, e
    agrega minutos por operador / motivo / estado resolvido.

    Empty state when no paragens sheets exist yet.
    """
    from app.downtime import list_downtime_sheets, aggregate_downtime
    db_path = _DATA_DIR / "app.db"
    sheets = list_downtime_sheets(db_path)
    agg = aggregate_downtime(sheets)
    return templates.TemplateResponse(
        request,
        "dashboard_downtime.html",
        {
            "sheets": sheets,
            "agg": agg,
            "active_tab": "downtime",
        },
    )


@app.get("/dashboard/workers", response_class=HTMLResponse)
def dashboard_workers(request: Request) -> Response:
    workers = kpis.workers_summary()
    matrix = kpis.operador_cliente_matrix()
    return templates.TemplateResponse(
        request,
        "dashboard_workers.html",
        {"workers": workers, "matrix": matrix, "active_tab": "workers"},
    )


@app.get("/dashboard/workers/{operador}", response_class=HTMLResponse)
def dashboard_worker_detail(request: Request, operador: str) -> Response:
    detail = kpis.worker_detail(operador)
    return templates.TemplateResponse(
        request,
        "dashboard_worker_detail.html",
        {"detail": detail, "active_tab": "workers"},
    )


@app.get("/dashboard/jobs", response_class=HTMLResponse)
def dashboard_jobs(request: Request) -> Response:
    jobs = kpis.jobs_summary()
    leads = kpis.lead_time_per_of()
    return templates.TemplateResponse(
        request,
        "dashboard_jobs.html",
        {"jobs": jobs, "leads": leads, "active_tab": "jobs"},
    )


@app.get("/dashboard/jobs/{of_number:path}", response_class=HTMLResponse)
def dashboard_job_detail(request: Request, of_number: str) -> Response:
    """Use ``:path`` converter so OFs like ``OF262107/70`` (Dossier
    notation) don't get split by the router. Kanban OFs are 6 digits
    and unaffected."""
    detail = kpis.job_detail(of_number)
    return templates.TemplateResponse(
        request,
        "dashboard_job_detail.html",
        {"detail": detail, "active_tab": "jobs"},
    )


@app.get("/dashboard/trends", response_class=HTMLResponse)
def dashboard_trends(request: Request) -> Response:
    daily = kpis.daily_trend(30)
    weekly = kpis.weekly_trend(12)
    monthly = kpis.monthly_trend(12)
    top_clientes = kpis.top_clientes(12)
    top_modelos = kpis.top_modelos(12)
    matrix_modelo = kpis.operador_modelo_matrix(12, top_n=15)
    return templates.TemplateResponse(
        request,
        "dashboard_trends.html",
        {
            "daily": daily, "weekly": weekly, "monthly": monthly,
            "top_clientes": top_clientes, "top_modelos": top_modelos,
            "matrix_modelo": matrix_modelo,
            "active_tab": "trends",
        },
    )


@app.get("/dashboard/alerts", response_class=HTMLResponse)
def dashboard_alerts(request: Request) -> Response:
    data = kpis.alerts()
    return templates.TemplateResponse(
        request,
        "dashboard_alerts.html",
        {"data": data, "active_tab": "alerts"},
    )


@app.get("/pair", response_class=HTMLResponse)
def pair_page(request: Request) -> Response:
    """QR-code pairing: shows a QR pointing at /capture so a phone on the
    same LAN can scan and open the camera page directly without typing
    the IP. The QR is generated client-side from the page URL."""
    return templates.TemplateResponse(request, "pair.html", {})


# ============================================================================
#  Aprendizagens & Regras  (/learnings)  — Round 98 learning engine
# ============================================================================

def _learning_user(request: Request) -> str:
    """Best-effort reviewer identity for the learnings audit trail."""
    return request.headers.get("X-Forwarded-User") or "web"


def _refresh_overlay() -> None:
    """Re-materialise the learned overlay and reload it into the running
    pipeline. Called after any approve / reject / edit / delete so the
    OCR snap sees the change without a restart."""
    try:
        learning_materialize.materialize_overlay()
        learning_scheduler.reload_pipeline()
    except Exception as e:
        print(f"[learning] overlay refresh failed: {e}", file=sys.stderr)


def _rule_item_response(request: Request, learning_id: int) -> Response:
    rule = learning_store.get_proposal(learning_id)
    if rule is None:
        return HTMLResponse("")
    return templates.TemplateResponse(request, "_rule_item.html", {"r": rule})


@app.get("/learnings", response_class=HTMLResponse)
def learnings_page(
    request: Request,
    status: str = Query(""),
    kind: str = Query(""),
) -> Response:
    """The Aprendizagens & Regras page — rules tab + LLM analyst tab."""
    rules = learning_store.list_proposals(status=status or None, kind=kind or None)
    return templates.TemplateResponse(request, "learnings.html", {
        "rules": rules,
        "filter_status": status,
        "filter_kind": kind,
        "counts": learning_store.count_by_status(),
        "metric_latest": learning_metrics.corrections_per_sheet(),
        "metric_trend": learning_metrics.corrections_trend(),
        "attractors": attractors.compute_attractors(top_n=12),
        "production_sectors": db.list_distinct_setores(),
    })


@app.get("/learnings/rules", response_class=HTMLResponse)
def learnings_rules(
    request: Request,
    status: str = Query(""),
    kind: str = Query(""),
) -> Response:
    """Rules list fragment (HTMX target for filters)."""
    rules = learning_store.list_proposals(status=status or None, kind=kind or None)
    return templates.TemplateResponse(
        request, "_learnings_rules.html", {"rules": rules}
    )


@app.post("/learnings/rules/{learning_id}/approve", response_class=HTMLResponse)
def learnings_rule_approve(request: Request, learning_id: int) -> Response:
    learning_store.approve_proposal(learning_id, _learning_user(request))
    _refresh_overlay()
    return _rule_item_response(request, learning_id)


@app.post("/learnings/rules/{learning_id}/reject", response_class=HTMLResponse)
def learnings_rule_reject(request: Request, learning_id: int) -> Response:
    learning_store.reject_proposal(learning_id, _learning_user(request))
    _refresh_overlay()
    return _rule_item_response(request, learning_id)


@app.post("/learnings/rules/{learning_id}/edit", response_class=HTMLResponse)
def learnings_rule_edit(
    request: Request,
    learning_id: int,
    to: str = Form(""),
    value: str = Form(""),
    weight: str = Form(""),
    note: str = Form(""),
) -> Response:
    rule = learning_store.get_proposal(learning_id)
    if rule is None:
        return HTMLResponse("")
    payload = dict(rule["payload"]) if isinstance(rule["payload"], dict) else {}
    if to.strip():
        payload["to"] = to.strip()
    if value.strip():
        payload["value"] = value.strip()
    if weight.strip():
        try:
            payload["weight"] = float(weight.replace(",", "."))
        except ValueError:
            pass
    learning_store.update_proposal_payload(
        learning_id, payload, _learning_user(request),
        note=note.strip() or None,
    )
    _refresh_overlay()
    return _rule_item_response(request, learning_id)


@app.post("/learnings/rules/{learning_id}/delete", response_class=HTMLResponse)
def learnings_rule_delete(request: Request, learning_id: int) -> Response:
    learning_store.delete_proposal(learning_id)
    _refresh_overlay()
    return HTMLResponse("")


@app.post("/learnings/rules/create")
async def learnings_rule_create(request: Request) -> JSONResponse:
    """Create a learning by hand — used by the LLM tab's 'Adicionar ao
    Separador 1' button. Lands in quarantine awaiting human approval."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "JSON inválido"}, status_code=400)
    kind = (body.get("kind") or "").strip()
    payload = body.get("payload")
    if not kind or not isinstance(payload, dict):
        return JSONResponse(
            {"ok": False, "error": "kind/payload em falta"}, status_code=400
        )
    res = learning_store.create_manual_proposal(
        kind, payload,
        field=body.get("field"),
        template_name=body.get("template_name"),
        origin=body.get("origin") or "llm",
        decided_by=_learning_user(request),
    )
    return JSONResponse({"ok": True, "id": res["id"], "created": res["created"]})


@app.post("/learning/run", response_class=HTMLResponse)
def learning_run(
    request: Request,
    status: str = Query(""),
    kind: str = Query(""),
) -> Response:
    """Force a learning cycle now (the 'Minerar agora' button)."""
    try:
        learning_scheduler.run_learning_cycle()
    except Exception as e:
        print(f"[learning] manual run failed: {e}", file=sys.stderr)
    rules = learning_store.list_proposals(status=status or None, kind=kind or None)
    return templates.TemplateResponse(
        request, "_learnings_rules.html", {"rules": rules}
    )


@app.get("/learning/metrics")
def learning_metrics_json() -> JSONResponse:
    return JSONResponse({
        "latest": learning_metrics.corrections_per_sheet(),
        "trend": learning_metrics.corrections_trend(),
    })


@app.get("/learnings/llm/atratores")
def learnings_atratores() -> JSONResponse:
    return JSONResponse({"attractors": attractors.compute_attractors(top_n=15)})


@app.post("/learnings/llm/chat")
async def learnings_llm_chat(request: Request) -> JSONResponse:
    """One chat turn with the local Ollama analyst. Returns the JSON
    envelope {reply, charts, proposed_rules}."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"reply": "Pedido inválido.", "charts": [], "proposed_rules": []},
            status_code=400,
        )
    message = (body.get("message") or "").strip()
    history = body.get("history") if isinstance(body.get("history"), list) else []
    if not message:
        return JSONResponse(
            {"reply": "Escreve uma pergunta.", "charts": [], "proposed_rules": []}
        )
    # llm_assistant.chat() does a blocking HTTP call to Ollama — run it in a
    # worker thread so it never freezes the async event loop.
    envelope = await asyncio.to_thread(llm_assistant.chat, message, history)
    return JSONResponse(envelope)


# ----- R110.C — Agent proposals + policies (endpoints REST) -----
from fastapi.encoders import jsonable_encoder

# R117: `kernel` import foi promovido para o topo do ficheiro.


def _json_ok(data: dict) -> JSONResponse:
    """JSONResponse com datetime → ISO automático."""
    return JSONResponse(jsonable_encoder(data))


# ----- R110.E — Kernel state visibility -----

@app.get("/agent/kernel/state")
def kernel_state_endpoint() -> JSONResponse:
    return _json_ok({"state": kernel.get_state(),
                     "total_events": kernel.count_events()})


@app.get("/agent/kernel/events")
def kernel_events_endpoint(limit: int = 50) -> JSONResponse:
    return _json_ok({"events": kernel.list_recent_events(limit=limit)})


@app.get("/agent/proposals")
def agent_proposals_list(
    status: str = "",
    kind: str = "",
    limit: int = 50,
) -> JSONResponse:
    """Lista propostas do agente Qwen. Filtros opcionais status/kind."""
    rows = db.list_proposals(status=status, kind=kind, limit=limit)
    return _json_ok({"proposals": rows, "count": len(rows)})


@app.post("/agent/proposals/{proposal_id}/approve")
def agent_proposal_approve(proposal_id: int) -> JSONResponse:
    """Aceita uma proposta: corre eval gate, promove nova policy_version,
    aplica template_overlay se for o caso."""
    from app.pipeline import policy_engine
    version_id = policy_engine.promote_policy_from_proposal(
        proposal_id, created_by="human-approval"
    )
    if version_id is None:
        return JSONResponse(
            {"status": "error", "error": "Proposta não encontrada."},
            status_code=404,
        )
    proposal = db.get_proposal(proposal_id)
    proposal_status = (proposal or {}).get("status") or "accepted"
    kernel.emit_event("proposal_decided",
                      {"proposal_id": proposal_id, "decision": proposal_status})
    if proposal_status == "accepted":
        kernel.emit_event("policy_promoted", {"version": version_id})
    return _json_ok({
        "status": "ok",
        "proposal_id": proposal_id,
        "policy_version": version_id,
        "proposal": proposal,
    })


@app.post("/agent/proposals/{proposal_id}/reject")
def agent_proposal_reject(proposal_id: int) -> JSONResponse:
    from app.pipeline import policy_engine
    ok = policy_engine.reject_proposal(proposal_id, decided_by="human")
    if not ok:
        return JSONResponse(
            {"status": "error", "error": "Proposta não encontrada."},
            status_code=404,
        )
    kernel.emit_event("proposal_decided",
                      {"proposal_id": proposal_id, "decision": "rejected"})
    return JSONResponse({"status": "ok", "proposal_id": proposal_id})


@app.get("/agent/policies")
def agent_policies_list(limit: int = 30) -> JSONResponse:
    return _json_ok({"versions": db.list_policy_versions(limit=limit)})


@app.get("/agent/policies/active")
def agent_policies_active() -> JSONResponse:
    return _json_ok({"policy": db.get_active_policy_version()})


@app.post("/agent/policies/rollback")
def agent_policies_rollback() -> JSONResponse:
    """Reverte para a parent_version da activa actual."""
    from app.pipeline import policy_engine
    new_version = policy_engine.rollback_to_parent(reason="manual-rollback")
    if new_version is None:
        return JSONResponse(
            {"status": "error",
             "error": "Sem versão anterior para reverter."},
            status_code=400,
        )
    kernel.emit_event("policy_rolled_back", {"version": new_version})
    return JSONResponse({"status": "ok", "active_version": new_version})


@app.get("/agent/circuit-breaker")
def agent_circuit_breaker() -> JSONResponse:
    """Verifica estado do circuit breaker (recommended action). Não age sozinho."""
    from app.pipeline import policy_engine
    return JSONResponse(policy_engine.check_circuit_breaker())


@app.get("/agent/sessions")
def agent_sessions_list(limit: int = 50) -> JSONResponse:
    return _json_ok({"sessions": db.list_qwen_sessions(limit=limit)})


@app.get("/agent/charts")
def agent_charts_list(pinned_only: bool = False, limit: int = 30) -> JSONResponse:
    return _json_ok({
        "charts": db.list_qwen_charts(limit=limit, pinned_only=pinned_only)
    })


@app.post("/agent/charts/{chart_id}/pin")
def agent_chart_pin(chart_id: int) -> JSONResponse:
    ok = db.set_chart_pinned(chart_id, True)
    if not ok:
        return JSONResponse({"status": "error",
                             "error": "Chart não encontrado."},
                            status_code=404)
    return JSONResponse({"status": "ok"})


@app.post("/agent/charts/{chart_id}/unpin")
def agent_chart_unpin(chart_id: int) -> JSONResponse:
    ok = db.set_chart_pinned(chart_id, False)
    if not ok:
        return JSONResponse({"status": "error",
                             "error": "Chart não encontrado."},
                            status_code=404)
    return JSONResponse({"status": "ok"})


# ----- R115 — /obras (ponto de situação por cliente e OV) -----

@app.get("/obras", response_class=HTMLResponse)
def obras_page(
    request: Request,
    closed: int = 0,
    q: str = "",
    tab: str = "cliente",
    ov: str = "",
) -> Response:
    """Página de ponto-de-situação. Junta o plan (refs) com production_rows."""
    from app.pipeline import obras_status
    data = obras_status.compute_obras_status(include_closed=bool(closed))
    return templates.TemplateResponse(request, "obras.html", {
        "data": data,
        "include_closed": bool(closed),
        "initial_search": q,
        "initial_tab": "ov" if tab == "ov" else "cliente",
        "initial_ov": ov,
    })


@app.get("/api/obras/summary")
def api_obras_summary(closed: int = 0) -> JSONResponse:
    from app.pipeline import obras_status
    return _json_ok(obras_status.compute_obras_status(include_closed=bool(closed)))


@app.get("/api/obras/ov/{ov_str}")
def api_obras_ov(ov_str: str) -> JSONResponse:
    from app.pipeline import obras_status
    det = obras_status.get_ov_detail(ov_str)
    if det is None:
        return JSONResponse({"status": "error",
                             "error": f"OV {ov_str} não encontrada no plan"},
                            status_code=404)
    return _json_ok(det)


@app.post("/api/obras/refresh")
def api_obras_refresh() -> JSONResponse:
    from app.pipeline import obras_status
    obras_status.invalidate_cache()
    return JSONResponse({"status": "ok"})


# ----- CSV builder (mirrors ocr6.write_csv but to string) -----

def _to_3block_csv(filename: str, data: dict) -> str:
    """Produce CSV string in the Metalogalva 3-block format.

    Round 54 — template-aware. The block headers + column lists are
    derived from the TemplateSpec for this sheet:
      - block 2 heading: ``template.csv_block_label`` (e.g. "TABELA DE
        PRODUÇÃO" for bobine, "TABELA DE PARAGENS" for paragens,
        "TABELA DE NESTING" for Gemini).
      - block 2 columns: ``template.row_fields`` (uppercased).
      - block 3 (footer) columns: ``template.footer_fields``. Templates
        with no footer (paragens) omit the block entirely.

    For BOBINE-FORMATO this produces byte-identical output to the prior
    R53 implementation so the factory validator (which knows only this
    schema) keeps parsing legacy + new bobine sheets identically.
    """
    import csv
    import io

    # Lazy import — avoids circular if templates_registry ever imports main
    from app.templates_registry import (
        DEFAULT_TEMPLATE, detect_template, get_template,
    )

    # Resolve template — explicit > inferred > default
    tname = (data or {}).get("template_name")
    if tname:
        template = get_template(tname)
    else:
        header = (data or {}).get("header") or {}
        setor = header.get("setor_maquina", "")
        cod_maquina = header.get("cod_maquina", "")
        template = (
            detect_template(setor, cod_maquina=cod_maquina)
            if setor or cod_maquina
            else DEFAULT_TEMPLATE
        )

    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    h = data.get("header", {}) or {}
    f_ = data.get("footer", {}) or {}
    field_labels = template.field_labels or {}

    def _csv_label(field: str) -> str:
        return field_labels.get(field, field.upper())

    # --- Block 1: CABEÇALHO (identical for all templates) ---
    w.writerow(["### CABEÇALHO"])
    w.writerow(["FICHEIRO", "DATA", "OPERADOR", "N_OPERADOR", "SETOR_MAQUINA"])
    w.writerow([
        filename, h.get("data", ""), h.get("operador", ""),
        h.get("n_operador", ""), h.get("setor_maquina", ""),
    ])
    w.writerow([])

    # --- Block 2: TABELA (per-template label + columns) ---
    w.writerow([f"### {template.csv_block_label}"])
    # Column header — common prefix (FICHEIRO/DATA/OPERADOR) + template fields
    column_header = ["FICHEIRO", "DATA", "OPERADOR"] + [
        _csv_label(f) for f in template.row_fields
    ]
    w.writerow(column_header)
    for row in data.get("rows", []) or []:
        row_out = [filename, h.get("data", ""), h.get("operador", "")]
        row_out.extend(str(row.get(f, "") or "") for f in template.row_fields)
        w.writerow(row_out)
    w.writerow([])

    # --- Block 3: RODAPÉ (skipped when template has no footer fields) ---
    if template.footer_fields:
        w.writerow(["### RODAPÉ"])
        footer_header = ["FICHEIRO", "DATA", "OPERADOR"] + [
            _csv_label(f) for f in template.footer_fields
        ]
        w.writerow(footer_header)
        footer_row = [filename, h.get("data", ""), h.get("operador", "")]
        footer_row.extend(str(f_.get(f, "") or "") for f in template.footer_fields)
        w.writerow(footer_row)

    # UTF-8 BOM prefix (Excel friendly) — preserved from R53.
    return "﻿" + buf.getvalue()

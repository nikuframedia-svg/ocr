# Metalogalva OCR

OCR pipeline for Metalogalva industrial Kanban production sheets.

**Status:** Production baseline (R117) — Ollama nativo Windows
serving `qwen3-vl:8b` com `OCR_NO_THINK=1`. Roadmap de 8 fases em
`docs/MIGRATION.md` (R105 runbook) e `CLAUDE.md` (resumo).

## Stack

- Python 3.11 + uv + Pydantic v2 + structlog
- **Ollama** (native Windows) serving `qwen3-vl:8b`. The web OCR path uses
  Ollama's native API; scripts can use the OpenAI-compatible endpoint at
  `http://localhost:11434/v1`. `OCR_NO_THINK=1` desliga
  reasoning blocks for more stable JSON and lower latency.
- Client communicates via `httpx`

> `docker-compose.yml`, `infra/install_docker_wsl.sh`, and
> `backend/app/pipeline/inference/schemas_strict.py` are ready for the
> vLLM + llguidance path (closed-vocabulary guided decoding). At time
> of writing vLLM 0.20.x in WSL2 + Blackwell (RTX 50-series) hits a
> silent SIGINT 30-50 s after init that we couldn't trace. Switch to
> vLLM by setting `VLLM_URL=http://localhost:8000/v1`,
> `VLLM_MODEL=Qwen/Qwen2.5-VL-7B-Instruct-AWQ`, and
> `GUIDED_DECODING_ENABLED=true` in `.env`, then `docker compose up
> -d vllm`.

## Prerequisites

- NVIDIA GPU (any modern card with ≥8 GB VRAM works; project target is
  RTX 5060 Ti 16GB)
- Recent NVIDIA driver
- Python 3.11 + [uv](https://docs.astral.sh/uv/) on the host
- Enough free disk for `qwen3-vl:8b`; keep extra space for legacy/test
  models if benchmarking alternatives

## Quick start

```bash
# 1. Install Python deps
uv sync

# 2. (Optional) configure environment — defaults already point at Ollama
cp .env.example .env

# 3. Install Ollama and pull the model (once)
#    Windows: install OllamaSetup.exe, then in any shell:
ollama pull qwen3-vl:8b   # production OCR model

# 4. Sanity-check the endpoint
uv run python scripts/check_vllm.py
# Expected: {"status": "ok", "model": "qwen3-vl:8b", ...}

# 5. Extract one (or many) Kanbans → CSV + side-by-side HTML
uv run python scripts/extract.py inputs/originais/AugustoMonteiro_2026.04.16.JPG --html
uv run python scripts/extract.py inputs/originais/ --html
# Output: reports/extractions/<stem>.csv + .html + index.html

# 6. (Optional) Build gold standard with draft pre-fill
uv run python scripts/annotate_cli.py --from-draft ground_truth_draft/

# 7. (Optional) Re-run extraction with metrics against gold
uv run python scripts/extract.py inputs/originais/ --html \
    --ground-truth ground_truth/ --out reports/extractions_v1/
# Output: reports/extractions_v1/_metrics.md (field accuracy + CER)
```

## MVP web app — capture (mobile) → review/edit → dashboard

```bash
# Once: pull deps (already in pyproject)
uv sync

# Run the server (binds to all interfaces so phones on the same wifi reach it)
uv run python -m uvicorn backend.app.web.main:app --host 0.0.0.0 --port 8080
```

Then on the phone (same wifi as the server box) open
`http://<server-ip>:8080/capture` and:

1. Tap "Foto" → camera nativa abre, captura a folha Kanban.
2. Tap "Enviar e extrair" → spinner ~25 s (OCR + DQ síncrono).
3. Vê o resultado em `/sheet/<id>` — image lado-a-lado com a tabela,
   células coloridas por confidence (verde/amarelo/vermelho).
4. Toca numa célula para editar; submit (blur ou Enter) guarda via
   HTMX e regista em audit trail.
5. Selecciona o operador → "Validar" → vai para `/queue`.
6. Em `/dashboard` vê KPIs: STP rate, top campos editados, sheets/dia.

### Endpoints

O servidor expõe ~100 endpoints distribuídos por capture, sheets, refs,
learnings, agent (kernel/proposals/policies/charts), obras, exports.
Ver `backend/app/web/main.py` para detalhe. Rotas core: `/capture`,
`/upload`, `/sheet/<id>`, `/sheet/<id>/edit`, `/sheet/<id>/validate`,
`/sheet/<id>/csv`, `/queue`, `/dashboard`.

### Storage

- `data/app.db` — SQLite single-file, 12 tables (`sheets`, `edits`,
  `production_rows`, `learnings`, `learning_runs`, `shadow_runs`,
  `qwen_sessions`, `qwen_charts`, `proposals`, `policy_versions`,
  `template_overlay`, `circuit_breaker_log`). Schema em
  `backend/app/web/db.py`.
- `data/images/` — fotos uploaded (gitignored)
- `data/cross_sheet.json` — DQ Module persistent index

## Tests

```bash
uv run pytest                  # unit + smoke
uv run pytest -m vllm          # integration, requires vLLM up
```

Coverage threshold is enforced at 70% on `backend/app/`.

## Repository layout

```
backend/app/                 — library code (importable as `app.*`)
backend/app/pipeline/        — extraction pipeline (vllm_client, schemas, csv_writer)
scripts/                     — operator CLIs (annotate_cli, benchmark, check_vllm)
inputs/originais/            — raw Kanban photos as received (gitignored: customer data)
ground_truth/                — manually-annotated JSON (committed, no images)
reports/                     — baseline_v0.md and per-run diffs
infra/docker/                — vLLM Dockerfile (only if customisation is needed)
tests/                       — pytest suite (unit + integration)
```

## Project briefing

- `CLAUDE.md` — context essencial para futuros agentes (8-phase summary,
  hot path, anti-patterns)
- `docs/MIGRATION.md` — runbook PC→Portátil + fine-tuning na 5090 (R105)
- `scripts/ops/start.ps1`, `scripts/ops/update.ps1` — ops do dia-a-dia

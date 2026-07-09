# Metalogalva OCR — Claude Code context

## What this is
Standalone OCR para folhas Kanban A4 manuscritas da Metalogalva (cliente final,
fabricante de colunas metálicas em Trofa, exporta para a Europa). **NÃO** integra
com o PP1 (APS interno da NIKUFRA.AI). Audit trail certificável EN 1090 / ISO 9001.

## Stack (canonical)
- Python 3.11 + uv, Pydantic v2, FastAPI, Jinja2 + HTMX (sem SPA)
- **Ollama nativo Windows** :11434 com `qwen3.5:9b` + `OCR_NO_THINK=1`
  (sem o flag, ~42% dos OCRs falham JSON parse e a latência é 4x; ver Round 19/20)
- vLLM dorme até à Fase 4 — RTX 50xx Blackwell SIGINT silencioso aos 30-50s.
  Código preparado em `pipeline/inference/vllm_client.py` + `config.py`
- SQLite single-file em `data/app.db` (sem ORM)

## Hot path (R117 — confirmado pós-R109)
```
POST /upload → ocr_queue → _process_sheet_ocr (main.py) →
  ocr_runner.run_pipeline → ocr6.process_image (raiz, urllib → Ollama) →
  template detect (templates_registry) → eventual Pass-2 com swap_prompt →
  db.update_extraction (DQ stub vazio) →
  _run_and_store_cross_check → scoring_engine.cross_check_sheet (motor v5) →
    _apply_auto_overwrites + _apply_operador_snap + _apply_codmaq_fill →
    store_cross_check JSON + _spawn_shadow_scoring (thread daemon)
  _deposit_csv_to_factory
```
~25 s/folha em produção. Nota R117: `pipeline/inference/vllm_client.py`
permanece em `scripts/` (não em produção — vLLM dorme até à Fase 4).
Audit trail do agente Qwen em `backend/app/pipeline/qwen_agent.py` +
`backend/app/kernel.py` (sessions/proposals/policies).

## Estrutura
- `backend/app/` — código de produção (`web/`, `pipeline/`, `dq/`, `cross_check/`, `learning/`, `evaluation/`)
- `scripts/` — CLIs operator (`extract`, `annotate_cli`, `benchmark`, `dq_run`);
  subdirs `finetune/`, `data/`, `ops/` para utilitários R105+
- `kanban_refs/` — SAP refs factory (StockSAP, plan_colunas_cpis, ListaColaboradores)
- `inputs/originais/`, `ground_truth/`, `lexicons/` — dataset histórico (TRACKED em R64)
- `data/`, `kanban_refs/03_Cross_Check/`, `lexicons/{operador,cliente}_aliases.json`
  — estado runtime, **gitignored** (R121+R223). Eram tracked, mas como a app os
  reescreve, mantinham o working tree sujo e faziam o `git pull` da fábrica
  abortar (deploy não pegava). Migração destes dados PC↔Portátil é por cópia
  explícita, não por git.
- `docs/MIGRATION.md` — runbook PC→Portátil (R105)
- `prompts/ocr6_v3.txt` — prompt canónico

## Roadmap (8 fases — briefing original)
0 baseline · 0.5 pré-processamento OpenCV (default OFF — regrediu em VLMs) ·
1 PWA captura · 2 ingestor Dossiers de Fabrico (PostgreSQL) · 3 crops +
validação cruzada · 4 vLLM grammars (llguidance) · 5 HITL · 6 TrOCR
fine-tune por operador · 7 LiLT (opcional). Hoje: Fase 0.5 + R104-R106
(refs uploads + fine-tuning scaffolding R105).

## Administração (Task C — unidades, registo de kanbans, KPIs)
- `/admin` com separadores Referências | Kanbans | Unidades | KPIs;
  o corpo do `/refs` vive em `_refs_content.html` (URL /refs mantém-se)
- **Unidades fabris**: tabela `unidades` (seed 'Trofa' = sede);
  `sheets.unidade_id` NULL = Trofa; filtro por unidade em /queue e /kanbans
- **Registo de kanbans**: wizard na tab Kanbans → `kanban_templates`
  (spec_json espelha TemplateSpec; prefixo `u{unidade_id}_`); descoberta de
  campos partilha a fila FIFO do OCR; ativação SEMPRE humana;
  `template_store.reload_registry()` instala os ativos no registry em
  runtime (processo único uvicorn — multi-worker precisaria de sinal)
- **KPIs editáveis**: fórmulas em `data/kpi_params.json` (gitignored, como
  todos os dados runtime); defaults em código (`kpi_params.DEFAULT_KPIS`)
  byte-idênticos às fórmulas históricas; avaliador `kpi_expr.py` é um
  walker AST com whitelist — NUNCA trocar por eval
- `ADMIN_TOKEN` (env, opcional): protege /admin* e /refs*; sem env → aberto
  como sempre

## Variantes do cross engine (R250–R255)
- `SCORING_VARIANT` (ContextVar; env `CROSS_SCORING_VARIANT`, default **v30**):
  "v30" = produção (melhor ranking medido); "next" (R250-R252) e "next2"
  (R254) = arquivadas, medidas iguais/piores no ranking; **"v30cal" (R255) =
  candidata ao flip** — ranking byte-idêntico ao v30 + confiança calibrada
  (Platt por campo) + abstenção OOD P(H0)>P(OF). Evidência e sequência
  soak→flip→gate em `docs/DECISAO_v30cal.md`; procedimento formal em
  `docs/CROSS_EVALUATION_PROTOCOL.md`.
- `CROSS_SHADOW_VARIANT` (default "current"): variante da thread de sombra;
  "=v30cal" inicia o soak (triagem em /shadow-queue + scripts/diag/soak_sprt.py).
- `CROSS_POSTERIOR_TELEMETRY=0`: desliga a telemetria do posterior em v30
  (recuperação de runtime); na variante ativa é decisão e corre sempre.
- `CROSS_WRITE_GATE_MARGINAL` (default OFF; ligar só com OK do Luís): gate de
  gravação por confiança calibrada, limiares sibling-aware.
- Harness: `scripts/diag/backtest_winner.py` é SIMÉTRICO desde R253 (controlo
  HEAD-vs-HEAD tem de dar delta exatamente 0); validar variantes SEMPRE por lá.

## Convenções
- Comentários `R##` referenciam o round/commit que introduziu a regra — manter
- Prompts versionados em `prompts/ocr6_v*.txt`; v3 é canónico
- Production-ready desde o dia 1 (regra Luís)
- Sem dependências exóticas (>1k stars + manutenção recente)
- PT-PT em comunicação com o utilizador; código e identificadores em EN

## Anti-patterns (lista negra explícita)
- **Não** introduzir SPA frontend — HTMX server-side é suficiente
- **Não** inverter `preprocess_enabled` sem A/B fresco (Fase 0.5 regrediu
  −2.6 pp field acc em Qwen2.5-VL: o modelo viu phone-camera photos no treino)
- **Não** reactivar vLLM sem confirmar Blackwell graph-capture stable
- **Não** inventar UI sem screenshot/input visual do Luís
- **Não** tocar em dados tracked: `data/app.db`, `data/images/`,
  `ground_truth*/`, `kanban_refs/`, `inputs/_factory_test/photos*/`
- **Não** sugerir integração com PP1 (sistema separado)

## Comandos comuns
```
uv run python -m uvicorn backend.app.web.main:app --host 0.0.0.0 --port 8080
uv run python scripts/extract.py inputs/originais/<file> --html
uv run python scripts/check_vllm.py                  # sanity Ollama/vLLM
uv run pytest                                        # cobertura 70% em backend/app/
uv run pytest -m vllm                                # skip se Ollama-only
powershell -File scripts/ops/start.ps1               # produção: uvicorn + cloudflared
powershell -File scripts/ops/update.ps1              # PC da Metalogalva: pull + restart
```

## Verificação (Boris: "ensure Claude can verify its work")
- Mudanças no pipeline OCR → `scripts/extract.py` contra `ground_truth/`,
  comparar `reports/extractions/_metrics.md`
- Mudanças no DQ → `scripts/dq_run.py` em batch
- Mudanças na UI → abrir `/sheet/<id>` no browser e validar render
- Antes de commit → `uv run pytest -q` (excluindo `-m vllm`)

## Mais contexto
- `docs/MIGRATION.md` — runbook PC→Portátil + fine-tuning na 5090
- `README.md` — quick start
- `inputs/_factory_test/` — dataset legacy de referência (NÃO apagar fotos/CSVs)

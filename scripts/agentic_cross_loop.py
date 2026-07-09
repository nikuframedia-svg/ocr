#!/usr/bin/env python3
"""Agentic loop Claude (cientista) + Codex (engenheiro) para melhorar o motor de
cross-check contra o baseline R231 — sem batota, com prova auditável.

Contrato (resumo): correr investigação -> implementação -> validação em loop.
- Baseline fixo: R231 (git ``601fe7d``).
- Um candidato só passa se melhorar >= ``--target-delta-pp`` (default 3.0) sobre R231,
  com ``cross_contract_violations == 0``, sem regredir ``regressed_good_raw`` nem
  ``corrected_to_truth`` vs baseline, e sem overfit ao subconjunto cego (holdout).
- Métrica principal: ``final_value == resultado_atual`` (output_accuracy_vs_resultado_atual_pct).
- Proibido decorar a amostra: nenhuma estratégia específica de cliente/modelo/OF/OV/lote/folha.

Este script NÃO reescreve o benchmark. Reutiliza o harness provado
``scripts/diag/compare_cross_engines.py`` (que já corre R231 vs candidato sobre o
mesmo pacote, calcula hashes SHA256 e o gate +3pp) e acrescenta por cima:
    * preflight com hard-stop de modelos obrigatórios (opus-4.8 + gpt-5.5);
    * isolamento numa branch git dedicada (commit dos aceites, revert dos rejeitados);
    * gate estendido do contrato + gate anti-overfit por holdout cego;
    * gates anti-batota (allowlist de ficheiros + deteção de literais que coincidem
      com valores da amostra + auto-teste de generalização do Claude);
    * o loop Claude->Codex e o trilho de auditoria por iteração.

Uso:
    uv run python scripts/agentic_cross_loop.py \
      --sample-dir "/Users/martimnicolau/Downloads/ultimos_150_ocr" \
      --refs-dir   "kanban_refs/04_Documentacao" \
      --baseline-ref 601fe7d \
      --target-delta-pp 3.0 --max-iterations 20

    # preflight só (não edita nada, imprime baseline R231 vs candidato-HEAD):
    uv run python scripts/agentic_cross_loop.py --max-iterations 0
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]           # .../ocr
_COMPARE = _REPO / "scripts" / "diag" / "compare_cross_engines.py"
_EVAL = _REPO / "scripts" / "diag" / "evaluate_cross_outputs.py"

_DEFAULT_SAMPLE_DIR = Path("/Users/martimnicolau/Downloads/ultimos_150_ocr")
_DEFAULT_REFS_DIR = _REPO / "kanban_refs" / "04_Documentacao"
_DEFAULT_REPORTS_DIR = _REPO / "reports" / "agentic_cross_loop"

# Codex só pode tocar nestes caminhos (relativos ao repo). Fora disto = rejeição.
_ALLOWLIST_PREFIXES = (
    "backend/app/pipeline/scoring_engine.py",
    "backend/app/cross_check/ref_watcher.py",
    "backend/app/cross_check/ref_importer.py",
    "tests/unit/",
)

_CROSSABLE_FIELDS = {
    "of", "ov", "cliente", "modelo", "lote",
    "comp_mm", "larg_mm", "lbase", "ltopo", "esp", "dbase", "dtopo",
}

# Regras do contrato injetadas em ambos os modelos (anti-batota / generalização).
_CONTRACT_RULES = textwrap.dedent(
    """
    REGRAS INEGOCIÁVEIS (Regra Anti-Batota / Generalização Obrigatória):
    - O motor NÃO pode aprender a amostra. Tem de aprender uma regra GERAL sobre
      como validar e escolher referências (Plano / StockSAP).
    - PROIBIDO qualquer lógica específica baseada em nomes reais de clientes,
      modelos, OFs, OVs, lotes, IDs de folhas, ou padrões que só funcionam para um
      caso concreto dos últimos 150 OCRs. Nada de listas internas de aliases reais,
      nada de "se cliente==X então Y", nada de "se OF==263181 então Z".
    - OCR NUNCA é a fonte final de um campo cruzável. O valor final vem sempre de
      referência validada (Plano, StockSAP ou referência derivada auditável).
    - PROIBIDO "normalizar" o OCR para parecer certo sem validação por referência.
    - Uma regra só é aceite se puder ser explicada como: "melhora a forma como o
      motor escolhe entre referências válidas do plano/StockSAP em QUALQUER caso
      semelhante" — e não "corrige o cliente X / modelo Y / folha Z".
    ESTRATÉGIAS PERMITIDAS: melhor ranking entre candidatos do plano; ponderar
    identidade vs medidas; usar unicidade de referência; validar contra StockSAP;
    distinguir referência em falta de erro real; usar margem/confiança entre 1º e
    2º candidato; exigir evidência independente em conflito OF/OV/modelo/cliente.
    """
).strip()


# --------------------------------------------------------------------------- #
# Infra
# --------------------------------------------------------------------------- #
def _utcstamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int | None = None,
    check: bool = False,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=merged,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=check,
    )


def _git(args: list[str], *, check: bool = True) -> str:
    proc = _run(["git", *args], cwd=_REPO, check=check)
    return proc.stdout.strip()


def _die(msg: str, code: int = 1) -> "None":
    print(f"\nERRO: {msg}", file=sys.stderr)
    raise SystemExit(code)


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _read_manifest(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh, delimiter=";"))


def _read_cells(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh, delimiter=";"))


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Amostra: valores reais (para o detetor anti-batota) e split dev/blind
# --------------------------------------------------------------------------- #
def _collect_sheet_values(sheet: dict) -> list[str]:
    out: list[str] = []
    for section in ("header", "footer"):
        for value in (sheet.get(section) or {}).values():
            if value not in (None, ""):
                out.append(str(value))
    for row in sheet.get("rows") or []:
        for value in (row or {}).values():
            if value not in (None, ""):
                out.append(str(value))
    return out


def _norm_literal(text: str) -> str:
    """Normaliza um literal/valor para comparação (upper, só alfanuméricos)."""
    return "".join(ch for ch in str(text).upper() if ch.isalnum())


def build_sample_value_set(sample_dir: Path) -> set[str]:
    """Conjunto de tokens que aparecem como VALORES nos 150 OCRs.

    Serve para detetar batota: se o Codex introduzir no código um literal novo
    que coincide com um valor concreto da amostra (um cliente, modelo, OF, OV ou
    lote real), isso é decorar a amostra e é rejeitado. É genérico: não usa
    nenhuma denylist de nomes reais, deriva o conjunto dinamicamente dos dados.
    """
    manifest = _read_manifest(sample_dir / "manifest.csv")
    values: set[str] = set()
    for item in manifest:
        for key in ("ocr_original", "resultado_atual"):
            rel = str(item.get(key) or "").replace("\\", "/")
            if not rel:
                continue
            path = sample_dir / rel
            if not path.exists():
                continue
            for raw in _collect_sheet_values(_read_json(path)):
                norm = _norm_literal(raw)
                if len(norm) >= 4 and any(ch.isdigit() for ch in norm):
                    values.add(norm)            # OF/OV/lote/model-code like
                for token in re.split(r"[^A-Za-zÀ-ÿ]+", str(raw)):
                    tok = _norm_literal(token)
                    if len(tok) >= 4 and not tok.isdigit():
                        values.add(tok)         # client/model name tokens
    return values


def holdout_split(sheet_ids: list[str], frac: float) -> set[str]:
    """Subconjunto cego determinístico por hash estável do sheet_id."""
    if frac <= 0:
        return set()
    blind: set[str] = set()
    for sid in sheet_ids:
        bucket = int(hashlib.sha256(sid.encode("utf-8")).hexdigest(), 16) % 100
        if bucket < round(frac * 100):
            blind.add(str(sid))
    return blind


# --------------------------------------------------------------------------- #
# Preflight
# --------------------------------------------------------------------------- #
def preflight(args: argparse.Namespace) -> dict[str, Any]:
    print("== Preflight ==")
    # 1) git repo + baseline ref
    top = _git(["rev-parse", "--show-toplevel"], check=False)
    if not top or Path(top).resolve() != _REPO.resolve():
        _die(f"{_REPO} não é a raiz de um repo git (got: {top!r}).")
    if _run(["git", "cat-file", "-t", args.baseline_ref], cwd=_REPO).returncode != 0:
        _die(f"baseline-ref {args.baseline_ref!r} não existe no repo.")
    print(f"  git OK — baseline {args.baseline_ref} presente; HEAD {_git(['rev-parse', '--short', 'HEAD'])}")

    # 2) ficheiros do pacote e refs
    sample_dir = args.sample_dir
    manifest = sample_dir / "manifest.csv"
    if not manifest.exists():
        _die(f"manifest.csv não encontrado em {sample_dir}")
    rows = _read_manifest(manifest)
    if not rows:
        _die("manifest.csv vazio.")
    # abrir 1-2 JSONs de cada tipo
    checked = 0
    for item in rows[:2]:
        for key in ("ocr_original", "resultado_atual"):
            rel = str(item.get(key) or "").replace("\\", "/")
            p = sample_dir / rel
            if not p.exists():
                _die(f"JSON do manifest não existe: {p}")
            _read_json(p)
            checked += 1
    for name in ("plan_colunas_cpis.xlsx",):
        if not (args.refs_dir / name).exists():
            _die(f"referência obrigatória em falta: {args.refs_dir / name}")
    if not any((args.refs_dir / n).exists() for n in ("StockSAP.xlsx", "StockSAP_Dinamico.xlsx")):
        _die(f"StockSAP.xlsx / StockSAP_Dinamico.xlsx em falta em {args.refs_dir}")
    try:
        import openpyxl  # noqa: F401
        openpyxl.load_workbook(args.refs_dir / "plan_colunas_cpis.xlsx", read_only=True).close()
    except Exception as exc:  # pragma: no cover - dependência do projeto
        _die(f"não consegui abrir o plano com openpyxl: {exc}")
    print(f"  pacote OK — {len(rows)} folhas no manifest; {checked} JSONs legíveis; plano/StockSAP abertos")

    # 3) modelos obrigatórios (hard-stop, sem fallback silencioso)
    if not args.skip_model_probe:
        _probe_claude(args.claude_model)
        _probe_codex(args.codex_model)
    else:
        print("  (probe de modelos ignorado por --skip-model-probe)")

    sheet_ids = [str(r.get("sheet_id") or "").strip() for r in rows if r.get("sheet_id")]
    return {"sheet_ids": sheet_ids, "n_sheets": len(rows)}


def _probe_claude(model: str) -> None:
    print(f"  a validar Claude ({model}) ...", end="", flush=True)
    proc = _run(
        ["claude", "-p", "--model", model, "--output-format", "json",
         "Reply ONLY with {\"ok\": true}"],
        cwd=_REPO, timeout=180,
    )
    if proc.returncode != 0:
        _die("Modelo obrigatório não disponível: opus-4.8 ou gpt-5.5. "
             "Não continuar com fallback silencioso.\n" + proc.stderr[-500:])
    try:
        env = json.loads(proc.stdout)
        used = env.get("modelUsage") or {}
    except Exception:
        used = {}
    if not any("opus-4-8" in k or "opus-4.8" in k for k in used):
        _die("Modelo obrigatório não disponível: opus-4.8 ou gpt-5.5. "
             "Não continuar com fallback silencioso.\n"
             f"(modelUsage reportou: {list(used)})")
    print(" OK")


def _probe_codex(model: str) -> None:
    print(f"  a validar Codex ({model}) ...", end="", flush=True)
    if shutil.which("codex") is None:
        _die("Modelo obrigatório não disponível: opus-4.8 ou gpt-5.5. "
             "Não continuar com fallback silencioso. (codex CLI não instalado)")
    with _tmp_scratch() as scratch:
        last = scratch / "last.txt"
        proc = _run(
            ["codex", "exec", "--skip-git-repo-check", "--sandbox", "read-only",
             "-C", str(scratch), "-m", model, "-c", "model_reasoning_effort=xhigh",
             "--output-last-message", str(last), "Reply with exactly: DONE"],
            cwd=scratch, timeout=240,
        )
        if proc.returncode != 0 or "DONE" not in (last.read_text() if last.exists() else ""):
            _die("Modelo obrigatório não disponível: opus-4.8 ou gpt-5.5. "
                 "Não continuar com fallback silencioso.\n" + proc.stderr[-500:])
    print(" OK")


class _tmp_scratch:
    def __enter__(self) -> Path:
        import tempfile
        self._dir = Path(tempfile.mkdtemp(prefix="agentic-cross."))
        return self._dir

    def __exit__(self, *exc: object) -> None:
        shutil.rmtree(self._dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Benchmark (reutiliza compare_cross_engines.py) + gate estendido
# --------------------------------------------------------------------------- #
def run_benchmark(args: argparse.Namespace, out_dir: Path) -> dict[str, Any]:
    """Corre R231 vs candidato (working tree) e devolve o comparison + paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    proc = _run(
        ["uv", "run", "python", str(_COMPARE),
         "--baseline-ref", args.baseline_ref,
         "--candidate-repo", str(_REPO),
         "--sample-dir", str(args.sample_dir),
         "--doc-dir", str(args.refs_dir),
         "--out-dir", str(out_dir),
         "--sections", args.sections,
         "--gate-pp", str(args.target_delta_pp)],
        cwd=_REPO, timeout=args.benchmark_timeout,
    )
    # compare escreve num sub-run-id: <out_dir>/<runid>/comparison.json
    comps = sorted(out_dir.glob("*/comparison.json"), key=lambda p: p.stat().st_mtime)
    if not comps:
        _die("benchmark não produziu comparison.json.\n" + proc.stdout[-800:] + proc.stderr[-800:])
    run_root = comps[-1].parent
    comparison = _read_json(run_root / "comparison.json")
    comparison["_run_root"] = str(run_root)
    return comparison


def _subset_accuracy(cells_csv: Path, subset: set[str]) -> float:
    if not cells_csv.exists() or not subset:
        return 0.0
    ok = tot = 0
    for row in _read_cells(cells_csv):
        if str(row.get("sheet_id") or "") not in subset:
            continue
        tot += 1
        if str(row.get("output_equals_truth") or "") == "1":
            ok += 1
    return round(ok / tot * 100.0, 2) if tot else 0.0


def _find_cells(run_root: Path, which: str) -> Path | None:
    hits = sorted((run_root / which).glob("*/cells.csv"))
    return hits[-1] if hits else None


@dataclass
class GateResult:
    baseline_acc: float
    candidate_acc: float
    delta_pp: float
    violations: int
    regressed_good_raw_delta: int
    corrected_to_truth_delta: int
    blind_baseline_acc: float
    blind_candidate_acc: float
    conditions: dict[str, bool] = field(default_factory=dict)

    @property
    def global_pass(self) -> bool:
        return all(self.conditions.values())

    def improves(self, prev_acc: float) -> bool:
        """Aceitação incremental: melhora estritamente sem causar dano."""
        return (
            self.candidate_acc > prev_acc + 1e-9
            and self.conditions.get("no_violations", False)
            and self.conditions.get("no_regression_good_raw", False)
            and self.conditions.get("corrected_not_worse", False)
            and self.conditions.get("holdout_not_worse", False)
        )


def evaluate_gate(
    comparison: dict[str, Any], blind: set[str], target_delta: float
) -> GateResult:
    base = comparison["baseline"]["summary"]
    cand = comparison["candidate"]["summary"]
    run_root = Path(comparison["_run_root"])
    b_acc = float(base["output_accuracy_vs_resultado_atual_pct"])
    c_acc = float(cand["output_accuracy_vs_resultado_atual_pct"])
    viol = int(cand.get("cross_contract_violations", 0))
    reg_delta = int(cand.get("regressed_good_raw", 0)) - int(base.get("regressed_good_raw", 0))
    cor_delta = int(cand.get("corrected_to_truth", 0)) - int(base.get("corrected_to_truth", 0))
    b_cells = _find_cells(run_root, "baseline")
    c_cells = _find_cells(run_root, "candidate")
    blind_b = _subset_accuracy(b_cells, blind) if b_cells else 0.0
    blind_c = _subset_accuracy(c_cells, blind) if c_cells else 0.0
    conditions = {
        "delta_ge_target": (c_acc - b_acc) >= target_delta,
        "no_violations": viol == 0,
        "no_regression_good_raw": reg_delta <= 0,
        "corrected_not_worse": cor_delta >= 0,
        "holdout_not_worse": (blind_c >= blind_b) if blind else True,
    }
    return GateResult(
        baseline_acc=b_acc, candidate_acc=c_acc, delta_pp=round(c_acc - b_acc, 2),
        violations=viol, regressed_good_raw_delta=reg_delta,
        corrected_to_truth_delta=cor_delta,
        blind_baseline_acc=blind_b, blind_candidate_acc=blind_c,
        conditions=conditions,
    )


# --------------------------------------------------------------------------- #
# Claude (cientista) e Codex (engenheiro)
# --------------------------------------------------------------------------- #
def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("{")
    depth = 0
    for i in range(start, len(text)) if start >= 0 else []:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("resposta não continha JSON válido")


def _dev_csv_sample(run_root: Path, name: str, dev: set[str], cap: int) -> list[dict[str, str]]:
    path = run_root / name
    if not path.exists():
        return []
    rows = [r for r in _read_cells(path) if str(r.get("sheet_id") or "") in dev]
    return rows[:cap]


def call_claude(
    args: argparse.Namespace,
    comparison: dict[str, Any],
    gate: GateResult,
    dev: set[str],
    rejected: list[dict[str, Any]],
) -> dict[str, Any]:
    run_root = Path(comparison["_run_root"])
    cand = comparison["candidate"]["summary"]
    lost = _dev_csv_sample(run_root, "lost_vs_baseline.csv", dev, args.dev_sample_cap)
    gained = _dev_csv_sample(run_root, "gained_vs_baseline.csv", dev, args.dev_sample_cap)
    metrics_view = {
        "baseline_acc_pct": gate.baseline_acc,
        "candidate_acc_pct": gate.candidate_acc,
        "delta_pp": gate.delta_pp,
        "target_delta_pp": args.target_delta_pp,
        "cross_contract_violations": gate.violations,
        "regressed_good_raw": cand.get("regressed_good_raw"),
        "corrected_to_truth": cand.get("corrected_to_truth"),
        "crossable_output_accuracy_pct": cand.get("crossable_output_accuracy_pct"),
        "validated_output_accuracy_pct": cand.get("validated_output_accuracy_pct"),
        "truth_ref_unavailable_by_field": cand.get("truth_ref_unavailable_by_field"),
    }
    system = _CONTRACT_RULES + (
        "\n\nAges como CIENTISTA/AUDITOR. Só podes ver células do subconjunto DEV "
        "(o subconjunto cego nunca te é mostrado, para não decorares a amostra). "
        "Devolve APENAS um objeto JSON (sem prosa, sem ```)."
    )
    prompt = json.dumps({
        "task": "Propõe UMA hipótese genérica e CIRÚRGICA (poucas linhas, implementável num "
                "único patch pequeno; ex.: ajustar um threshold/tiebreak de ranking existente) "
                "que melhore output_accuracy_vs_resultado_atual sem violar o contrato. "
                "Objetivo: subir >= target_delta_pp sobre o R231.",
        "current_metrics": metrics_view,
        "lost_vs_baseline_dev_sample": lost,
        "gained_vs_baseline_dev_sample": gained,
        "already_rejected_hypotheses": [
            {"hypothesis": r.get("hypothesis"), "reason": r.get("_reject_reason")}
            for r in rejected
        ],
        "required_output_schema": {
            "hypothesis_id": "H###",
            "hypothesis": "descrição curta",
            "why_it_is_generic": "porquê genérica",
            "data_evidence": ["evidência quantitativa do dev sample"],
            "forbidden_risks": ["riscos a evitar"],
            "implementation_request_for_codex": "pedido exato e mínimo para o Codex",
            "expected_metric_movement": {"accuracy_pp": 0.0, "risk": "low|medium|high"},
            "acceptance_checks": ["contract_violations == 0", "accuracy melhora"],
            "anti_specificity": {
                "is_generic": True, "uses_real_client_name": False,
                "uses_real_model_name": False, "uses_real_of_or_ov": False,
                "uses_sheet_id": False, "depends_on_sample_specific_pattern": False,
                "why_generic": "..."
            },
        },
    }, ensure_ascii=False)
    proc = _run(
        ["claude", "-p", "--model", args.claude_model, "--output-format", "json",
         "--append-system-prompt", system, prompt],
        cwd=_REPO, timeout=args.llm_timeout,
        env={"MAX_THINKING_TOKENS": "31999"},   # extended thinking / budget máximo p/ CLI
    )
    if proc.returncode != 0:
        _die("chamada ao Claude falhou:\n" + proc.stderr[-800:])
    envelope = json.loads(proc.stdout)
    return _extract_json(str(envelope.get("result") or ""))


def hypothesis_is_generic(hyp: dict[str, Any]) -> tuple[bool, str]:
    a = hyp.get("anti_specificity") or {}
    if not a.get("is_generic", False):
        return False, "anti_specificity.is_generic != true"
    for flag in ("uses_real_client_name", "uses_real_model_name",
                 "uses_real_of_or_ov", "uses_sheet_id",
                 "depends_on_sample_specific_pattern"):
        if a.get(flag, False):
            return False, f"anti_specificity.{flag} == true"
    return True, ""


def call_codex(args: argparse.Namespace, hypothesis: dict[str, Any], work: Path) -> dict[str, Any]:
    """Corre o Codex como engenheiro. Não depende do auto-report: o loop deteta as
    alterações por git e re-valida tudo. Captura stdout/stderr para auditoria."""
    last = work / "codex_last.txt"
    impl_req = hypothesis.get("implementation_request_for_codex") or hypothesis.get("hypothesis")
    prompt = textwrap.dedent(f"""
        És engenheiro de software no motor de cross-check deste repositório
        (ficheiro principal: backend/app/pipeline/scoring_engine.py). Implementa
        AGORA, editando os ficheiros diretamente no disco, a MENOR alteração que
        satisfaça esta hipótese:

        {impl_req}

        REGRAS (obrigatórias):
        {_CONTRACT_RULES}

        RESTRIÇÕES DE EXECUÇÃO:
        - Edita SÓ: backend/app/pipeline/scoring_engine.py,
          backend/app/cross_check/ref_watcher.py, backend/app/cross_check/ref_importer.py,
          e/ou um teste em tests/unit/.
        - Alteração mínima (poucas linhas), com parâmetro(s) global(is); NUNCA
          literais de clientes/modelos/OFs/OVs/lotes/folhas reais.
        - Cria ou atualiza UM teste unitário genérico em tests/unit/ que cubra a regra.
        - NÃO corras o benchmark.
        - É obrigatório editar ficheiros; se decidires que não é seguro implementar,
          não edites e explica porquê.
        No fim, escreve 1-3 frases a resumir exatamente o que alteraste (ou porque não).
    """).strip()
    proc = _run(
        ["codex", "exec", "-C", str(_REPO), "-m", args.codex_model,
         "-c", "model_reasoning_effort=xhigh", "--sandbox", "workspace-write",
         "--output-last-message", str(last), prompt],
        cwd=_REPO, timeout=args.llm_timeout,
    )
    (work / "codex_stdout.txt").write_text(
        (proc.stdout or "")[-40000:] + "\n\n---STDERR---\n" + (proc.stderr or "")[-6000:],
        encoding="utf-8")
    summary = "(sem output)"
    if last.exists() and last.read_text().strip():
        summary = last.read_text().strip()
    elif proc.stdout.strip():
        summary = proc.stdout.strip()[-2000:]
    return {
        "summary": summary,
        "files_changed": [],           # preenchido pelo loop a partir do git
        "_returncode": proc.returncode,
        "_stderr_tail": (proc.stderr or "")[-500:],
    }


# --------------------------------------------------------------------------- #
# Gates anti-batota estáticos sobre o diff produzido
# --------------------------------------------------------------------------- #
def _changed_files() -> list[tuple[str, bool]]:
    """Lista (path, is_untracked) das alterações no working tree."""
    out = _git(["status", "--porcelain"])
    files: list[tuple[str, bool]] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        code, path = line[:2], line[3:].strip().strip('"')
        if " -> " in path:                      # rename: fica com o destino
            path = path.split(" -> ", 1)[1].strip().strip('"')
        if path:
            files.append((path, code == "??"))
    return files


def _untracked_set() -> set[str]:
    return {p for p, untracked in _changed_files() if untracked}


def _iteration_files(pre_untracked: set[str]) -> tuple[list[str], list[str], list[str]]:
    """Ficheiros alterados NESTA iteração (ignora untracked pré-existentes).

    Devolve (tracked_modificados, novos_untracked, fora_da_allowlist), todos
    relativos ao repo — só o delta do Codex, nunca WIP/untracked pré-existente.
    """
    tracked, new_untracked, outside = [], [], []
    for path, untracked in _changed_files():
        if untracked and path in pre_untracked:
            continue                             # pré-existente; não é desta iteração
        if not any(path.startswith(pfx) for pfx in _ALLOWLIST_PREFIXES):
            outside.append(path)
            continue
        (new_untracked if untracked else tracked).append(path)
    return tracked, new_untracked, outside


def scan_literals_hitting_sample(added_lines: list[str], sample_values: set[str]) -> list[str]:
    """Literais novos (strings/números longos) que coincidem com valores da amostra.

    Função pura (testável): extrai literais de linhas adicionadas e devolve os que
    batem em valores concretos da amostra — sinal de decorar (batota).
    """
    literals: set[str] = set()
    for line in added_lines:
        for m in re.findall(r"""["']([^"']{3,})["']""", line):       # string literals
            literals.add(_norm_literal(m))
            for token in re.split(r"[^A-Za-zÀ-ÿ]+", m):              # tokens dentro de strings
                literals.add(_norm_literal(token))
        for m in re.findall(r"\b\d{4,}\b", line):                    # números longos (OF/OV/lote)
            literals.add(_norm_literal(m))
    return sorted(v for v in literals if v and v in sample_values)


def static_anti_cheat(sample_values: set[str], pre_untracked: set[str]) -> tuple[bool, str]:
    tracked, new_untracked, outside = _iteration_files(pre_untracked)
    if outside:
        return False, f"tocou ficheiro não permitido: {outside[0]}"
    if not tracked and not new_untracked:
        return False, "Codex não produziu alterações"
    # linhas adicionadas nos ficheiros tracked (diff) + ficheiros novos inteiros
    diff = _git(["diff", "--unified=0", "HEAD"], check=False)
    added_lines = [ln[1:] for ln in diff.splitlines()
                   if ln.startswith("+") and not ln.startswith("+++")]
    for path in new_untracked:
        p = _REPO / path
        if p.is_file():
            try:
                added_lines.extend(p.read_text(encoding="utf-8", errors="ignore").splitlines())
            except Exception:
                pass
    hits = scan_literals_hitting_sample(added_lines, sample_values)
    if hits:
        return False, f"literais que coincidem com valores da amostra (batota): {hits[:8]}"
    return True, ""


def run_pytest(pre_untracked: set[str]) -> tuple[bool, str]:
    targets = {"tests/unit/test_scoring_engine.py"}
    tracked, new_untracked, _ = _iteration_files(pre_untracked)
    for path in tracked + new_untracked:
        if path.startswith("tests/unit/") and path.endswith(".py"):
            targets.add(path)
    proc = _run(
        ["uv", "run", "pytest", "-q", "-m", "not vllm", *sorted(targets)],
        cwd=_REPO, timeout=1200,
    )
    tail = (proc.stdout + proc.stderr)[-1500:]
    return proc.returncode == 0, tail


# --------------------------------------------------------------------------- #
# Relatórios por iteração
# --------------------------------------------------------------------------- #
def write_iter_reports(
    iter_dir: Path, comparison: dict[str, Any], gate: GateResult,
    package_manifest: dict[str, Any], hypothesis: dict[str, Any] | None,
    codex_status: dict[str, Any] | None, decision: str, notes: list[str],
) -> None:
    iter_dir.mkdir(parents=True, exist_ok=True)
    run_root = Path(comparison["_run_root"])
    # copiar artefactos do compare com os nomes exatos do contrato
    (iter_dir / "package_manifest.json").write_text(
        json.dumps(package_manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    comp_public = {k: v for k, v in comparison.items() if not k.startswith("_")}
    (iter_dir / "comparison.json").write_text(
        json.dumps(comp_public, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    b_sum = comparison["baseline"]["summary"]
    c_sum = comparison["candidate"]["summary"]
    (iter_dir / "benchmark_baseline.json").write_text(
        json.dumps(b_sum, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (iter_dir / "benchmark_candidate.json").write_text(
        json.dumps(c_sum, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for name in ("gained_vs_baseline.csv", "lost_vs_baseline.csv"):
        src = run_root / name
        if src.exists():
            shutil.copy2(src, iter_dir / name)
    c_cells = _find_cells(run_root, "candidate")
    if c_cells:
        shutil.copy2(c_cells, iter_dir / "cells.csv")
        viol_rows = [r for r in _read_cells(c_cells)
                     if str(r.get("cross_contract_violation") or "") == "1"]
        with (iter_dir / "contract_violations.csv").open("w", newline="", encoding="utf-8") as fh:
            fields = list(viol_rows[0].keys()) if viol_rows else ["sheet_id", "path", "field", "output"]
            w = csv.DictWriter(fh, fieldnames=fields, delimiter=";")
            w.writeheader()
            w.writerows(viol_rows)
    if hypothesis is not None:
        (iter_dir / "claude_hypothesis.json").write_text(
            json.dumps(hypothesis, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if codex_status is not None:
        summary = codex_status.get("summary") or ""
        files = codex_status.get("files_changed") or []
        md = ["# Codex patch summary", "",
              f"**returncode:** {codex_status.get('_returncode')}",
              f"**files_changed (git):** {files}", "",
              "## Resumo do Codex", summary, "",
              "## git diff --stat", "```",
              _git(["diff", "--stat"], check=False), "```", "",
              "## stderr (tail)", "```", codex_status.get("_stderr_tail") or "", "```"]
        (iter_dir / "codex_patch_summary.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    # decision log
    cond = "\n".join(f"  - {k}: {'PASS' if v else 'FAIL'}" for k, v in gate.conditions.items())
    log = [
        f"# Decision log — {iter_dir.name}",
        "",
        f"Decisão: **{decision}**",
        "",
        "## Métricas",
        f"- baseline R231: {gate.baseline_acc:.2f}%",
        f"- candidate:     {gate.candidate_acc:.2f}%",
        f"- delta:         {gate.delta_pp:+.2f}pp (alvo {package_manifest.get('target_delta_pp')}pp)",
        f"- contract violations: {gate.violations}",
        f"- regressed_good_raw delta vs baseline: {gate.regressed_good_raw_delta}",
        f"- corrected_to_truth delta vs baseline: {gate.corrected_to_truth_delta}",
        f"- holdout cego: baseline {gate.blind_baseline_acc:.2f}% / candidate {gate.blind_candidate_acc:.2f}%",
        "",
        "## Gate",
        cond,
        f"  => global_pass = {gate.global_pass}",
        "",
        "## Notas",
        *[f"- {n}" for n in notes],
    ]
    (iter_dir / "decision_log.md").write_text("\n".join(log) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def _package_manifest(args: argparse.Namespace, run_root: Path,
                      sha_before: str, sha_after: str) -> dict[str, Any]:
    base = {}
    compare_pm = run_root / "package_manifest.json"
    if compare_pm.exists():
        base = _read_json(compare_pm)
    return {
        "sample_dir": str(args.sample_dir),
        "refs_dir": str(args.refs_dir),
        "sample_hash": (base.get("sample") or {}).get("sha256", ""),
        "refs_hash": (base.get("refs") or {}).get("sha256", ""),
        "baseline_ref": args.baseline_ref,
        "candidate_git_sha_before": sha_before,
        "candidate_git_sha_after": sha_after,
        "target_delta_pp": args.target_delta_pp,
    }


def _revert_iteration(pre_untracked: set[str]) -> None:
    """Reverte SÓ o delta desta iteração: repõe os tracked no HEAD e remove os
    untracked NOVOS criados pelo Codex — nunca toca em untracked pré-existentes."""
    for pfx in _ALLOWLIST_PREFIXES:
        _run(["git", "checkout", "HEAD", "--", pfx], cwd=_REPO)
        _run(["git", "reset", "-q", "HEAD", "--", pfx], cwd=_REPO)
    _, new_untracked, _ = _iteration_files(pre_untracked)
    for path in new_untracked:
        try:
            (_REPO / path).unlink()
        except FileNotFoundError:
            pass


def _print_final(gate: GateResult, decision: str, report_path: Path) -> None:
    print("\n" + "=" * 56)
    print(f"Baseline R231: {gate.baseline_acc:.2f}%")
    print(f"Candidate:     {gate.candidate_acc:.2f}%")
    print(f"Delta:         {gate.delta_pp:+.2f}pp")
    print(f"Contract violations: {gate.violations}")
    print(f"Gate: {'PASS' if gate.global_pass else 'FAIL'}  ({decision})")
    print(f"Report: {report_path}")
    print("=" * 56)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sample-dir", type=Path, default=_DEFAULT_SAMPLE_DIR)
    ap.add_argument("--refs-dir", type=Path, default=_DEFAULT_REFS_DIR)
    ap.add_argument("--baseline-ref", default="601fe7d")
    ap.add_argument("--target-delta-pp", type=float, default=3.0)
    ap.add_argument("--max-iterations", type=int, default=20)
    ap.add_argument("--claude-model", default="claude-opus-4-8")
    ap.add_argument("--codex-model", default="gpt-5.5")
    ap.add_argument("--holdout-frac", type=float, default=0.30)
    ap.add_argument("--sections", default="rows")
    ap.add_argument("--reports-dir", type=Path, default=_DEFAULT_REPORTS_DIR)
    ap.add_argument("--dev-sample-cap", type=int, default=60)
    ap.add_argument("--llm-timeout", type=int, default=1800)
    ap.add_argument("--benchmark-timeout", type=int, default=3600)
    ap.add_argument("--skip-model-probe", action="store_true",
                    help="não faz o probe live dos modelos (ainda exige os CLIs)")
    args = ap.parse_args()
    args.sample_dir = args.sample_dir.resolve()
    args.refs_dir = args.refs_dir.resolve()

    info = preflight(args)
    blind = holdout_split(info["sheet_ids"], args.holdout_frac)
    dev = {s for s in info["sheet_ids"]} - blind
    print(f"  holdout: {len(dev)} dev / {len(blind)} cego (frac={args.holdout_frac})")

    sample_values = build_sample_value_set(args.sample_dir)
    print(f"  anti-batota: {len(sample_values)} tokens de valores da amostra carregados")

    run_stamp = _utcstamp()
    run_dir = args.reports_dir / run_stamp
    run_dir.mkdir(parents=True, exist_ok=True)

    # ---- iteração 0: candidato = working tree atual --------------------- #
    print("\n== Iteração 0 (candidato = working tree atual) ==")
    comparison = run_benchmark(args, run_dir / "bench_iter000")
    gate = evaluate_gate(comparison, blind, args.target_delta_pp)
    sha = _git(["rev-parse", "HEAD"])
    pm = _package_manifest(args, Path(comparison["_run_root"]), sha, sha)
    iter_dir = run_dir / "iteration_000"
    write_iter_reports(iter_dir, comparison, gate, pm, None, None,
                       "baseline_candidate", ["ponto de partida; sem alteração LLM"])
    print(f"  baseline {gate.baseline_acc:.2f}% | candidate {gate.candidate_acc:.2f}% "
          f"| delta {gate.delta_pp:+.2f}pp | holdout {gate.blind_candidate_acc:.2f}%")

    if gate.global_pass:
        _print_final(gate, "PASS já no ponto de partida", iter_dir / "comparison.json")
        return 0
    if args.max_iterations <= 0:
        _print_final(gate, "preflight-only", iter_dir / "comparison.json")
        return 2

    # ---- setup da branch isolada --------------------------------------- #
    orig_branch = _git(["rev-parse", "--abbrev-ref", "HEAD"])
    loop_branch = f"agentic_cross_loop/{run_stamp}"
    _git(["switch", "-c", loop_branch])
    dirty = bool(_git(["status", "--porcelain"]))
    if dirty:
        _run(["git", "commit", "-am",
              "agentic loop: snapshot working tree (iteração 0 candidato)"], cwd=_REPO)
        print(f"  WIP não commitado foi capturado no commit inicial de {loop_branch}")
    print(f"  branch original: {orig_branch} | branch do loop: {loop_branch}")

    # Estado "corrente" = último candidato aceite (alimenta o Claude). O estado
    # "trial" é o candidato sob teste em cada iteração; só vira corrente se aceite.
    cur_comparison, cur_gate, cur_pm = comparison, gate, pm
    prev_acc = gate.candidate_acc
    rejected: list[dict[str, Any]] = []
    best_gate = gate
    best_iter_dir = iter_dir

    for i in range(1, args.max_iterations + 1):
        tag = f"iteration_{i:03d}"
        iter_dir = run_dir / tag
        iter_dir.mkdir(parents=True, exist_ok=True)
        print(f"\n== {tag} ==")
        sha_before = _git(["rev-parse", "HEAD"])

        # 1) Claude -> hipótese (só dev), sobre o estado corrente
        print("  Claude a propor hipótese ...")
        try:
            hypothesis = call_claude(args, cur_comparison, cur_gate, dev, rejected)
        except Exception as exc:
            print(f"  Claude falhou/parse inválido: {exc}; a repetir na próxima iteração")
            rejected.append({"hypothesis": "(erro claude)", "_reject_reason": str(exc)[:200]})
            continue
        (iter_dir / "claude_hypothesis.json").write_text(
            json.dumps(hypothesis, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        ok, why = hypothesis_is_generic(hypothesis)
        if not ok:
            hypothesis["_reject_reason"] = f"anti-especificidade: {why}"
            rejected.append(hypothesis)
            print(f"  hipótese rejeitada (anti-especificidade): {why}")
            write_iter_reports(iter_dir, cur_comparison, cur_gate, cur_pm, hypothesis, None,
                               "rejected_hypothesis", [f"anti-especificidade: {why}"])
            continue
        print(f"  hipótese {hypothesis.get('hypothesis_id')}: {hypothesis.get('hypothesis')}")

        # 2) Codex -> patch (regista untracked pré-existentes p/ isolar o delta)
        pre_untracked = _untracked_set()
        print("  Codex a implementar ...")
        codex_status = call_codex(args, hypothesis, iter_dir)
        _tracked, _new, _outside = _iteration_files(pre_untracked)
        codex_status["files_changed"] = _tracked + _new + _outside
        print(f"    codex rc={codex_status.get('_returncode')} | ficheiros: {codex_status['files_changed'] or '(nenhum)'}")

        # 3) anti-batota estático
        ok, why = static_anti_cheat(sample_values, pre_untracked)
        if not ok:
            _revert_iteration(pre_untracked)
            hypothesis["_reject_reason"] = f"anti-batota: {why}"
            rejected.append(hypothesis)
            print(f"  REVERT — {why}")
            write_iter_reports(iter_dir, cur_comparison, cur_gate, cur_pm, hypothesis,
                               codex_status, "rejected_anticheat", [why])
            continue

        # 4) pytest
        print("  a correr pytest ...")
        tests_ok, tail = run_pytest(pre_untracked)
        if not tests_ok:
            _revert_iteration(pre_untracked)
            hypothesis["_reject_reason"] = "pytest falhou"
            rejected.append(hypothesis)
            print("  REVERT — pytest falhou")
            write_iter_reports(iter_dir, cur_comparison, cur_gate, cur_pm, hypothesis,
                               codex_status, "rejected_tests", [f"pytest FAIL:\n{tail}"])
            continue

        # 5) benchmark do candidato (trial) + gate estendido
        print("  a correr benchmark do candidato ...")
        trial_comparison = run_benchmark(args, run_dir / f"bench_{tag}")
        trial_gate = evaluate_gate(trial_comparison, blind, args.target_delta_pp)
        trial_pm = _package_manifest(args, Path(trial_comparison["_run_root"]), sha_before, sha_before)
        print(f"    candidate {trial_gate.candidate_acc:.2f}% | delta {trial_gate.delta_pp:+.2f}pp "
              f"| holdout {trial_gate.blind_candidate_acc:.2f}% | viol {trial_gate.violations}")

        if trial_gate.improves(prev_acc):
            tracked, new_untracked, _ = _iteration_files(pre_untracked)
            for path in tracked + new_untracked:      # stage SÓ o delta desta iteração
                _run(["git", "add", "--", path], cwd=_REPO)
            _run(["git", "commit", "-m",
                  f"{tag}: {hypothesis.get('hypothesis_id')} "
                  f"{hypothesis.get('hypothesis')} (acc {trial_gate.candidate_acc:.2f}%)"], cwd=_REPO)
            sha_after = _git(["rev-parse", "HEAD"])
            trial_pm = _package_manifest(args, Path(trial_comparison["_run_root"]), sha_before, sha_after)
            cur_comparison, cur_gate, cur_pm = trial_comparison, trial_gate, trial_pm
            prev_acc = trial_gate.candidate_acc
            best_gate, best_iter_dir = trial_gate, iter_dir
            decision = "accepted_pass" if trial_gate.global_pass else "accepted"
            write_iter_reports(iter_dir, trial_comparison, trial_gate, trial_pm, hypothesis,
                               codex_status, decision, ["candidato aceite (melhora sem dano)"])
            print(f"  ACCEPT — commit {sha_after[:7]}")
            if trial_gate.global_pass:
                _print_final(trial_gate, "PASS", iter_dir / "comparison.json")
                print(f"\nSugestão: rever e fazer merge da branch {loop_branch}.")
                return 0
        else:
            _revert_iteration(pre_untracked)
            reasons = [k for k, v in trial_gate.conditions.items() if not v]
            hypothesis["_reject_reason"] = f"gate: {reasons}"
            rejected.append(hypothesis)
            # trial rejeitado; o estado corrente (último aceite) mantém-se — sem re-benchmark
            write_iter_reports(iter_dir, trial_comparison, trial_gate, trial_pm, hypothesis,
                               codex_status, "rejected_gate", [f"gate falhou: {reasons}"])
            print(f"  REVERT — gate falhou: {reasons}")

    # esgotou iterações sem atingir +target_delta_pp
    _print_final(best_gate, "FAIL (max-iterations atingido)", best_iter_dir / "comparison.json")
    nxt = rejected[-1].get("implementation_request_for_codex") if rejected else None
    if nxt:
        print(f"Melhor próxima hipótese a tentar: {nxt}")
    print(f"Branch com o progresso auditável: {loop_branch} (original: {orig_branch})")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

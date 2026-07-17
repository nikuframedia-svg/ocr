#!/usr/bin/env python3
"""R258 — Smoke da descoberta de campos (Task C E4) contra o Ollama REAL.

A descoberta do wizard de registo de kanbans nunca correu fora de mocks
(gate conhecido da Task C). Este CLI fecha esse gate na fábrica: recebe a
fotografia de um template, corre o MESMO caminho do worker
(ocr_runner.run_discovery → template_store.suggest_spec_from_discovery)
e imprime o resultado legível. Read-only: não toca na DB nem no registry.

Uso (na fábrica o Ollama vive no host de LAN, não em localhost):
    OLLAMA_URL=http://192.168.1.224:11434 \\
      uv run python scripts/diag/discovery_smoke.py caminho/foto_template.jpg

Exit codes (padrão check_vllm.py):
- 0 — descoberta OK (parse_ok=True, colunas encontradas)
- 2 — descoberta falhou (parse_ok=False) ou imagem inexistente
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "backend"))
sys.path.insert(0, str(_REPO))

import ocr6  # noqa: E402
from app.web import ocr_runner, template_store  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Smoke da descoberta de campos contra o Ollama real")
    ap.add_argument("image", type=Path, help="fotografia do template (.jpg/.png)")
    ap.add_argument("--nome", default="smoke_test",
                    help="nome fictício para a sugestão de spec")
    args = ap.parse_args()

    if not args.image.exists():
        print(f"✗ imagem não encontrada: {args.image}", file=sys.stderr)
        return 2

    # Imprimir o alvo EFETIVO evita smokes contra o servidor errado
    # (lição R227/factory: /health local não prova o OCR da fábrica).
    print(f"Ollama: {ocr6.OLLAMA_URL} · modelo: {ocr6.MODEL}")
    print(f"imagem: {args.image}")

    t0 = time.monotonic()
    disc = ocr_runner.run_discovery(args.image)  # nunca levanta
    dt = time.monotonic() - t0

    print(f"\nparse_ok: {disc['parse_ok']}  ({dt:.1f}s)")
    print(f"title:  {disc.get('title')!r}")
    print(f"columns ({len(disc['columns'])}): {disc['columns']}")
    print(f"header: {disc['header']}")
    print(f"footer: {disc['footer']}")

    sug = template_store.suggest_spec_from_discovery(
        disc, name=args.nome, unidade_id=0)
    print("\nfield_map (label impresso → campo):")
    for m in sug["field_map"]:
        tag = "canónico" if m["matched"] else "CUSTOM"
        print(f"  [{m['section']:>6}] {m['label']!r:<28} → "
              f"{m['field'] or '—':<16} {tag}")
    for w in sug["warnings"]:
        print(f"  ⚠ {w}")
    if not disc["parse_ok"]:
        raw = (disc.get("raw") or "")[:400]
        print(f"\nraw (primeiros 400 chars):\n{raw}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
